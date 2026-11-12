import asyncio
from collections import Counter
import copy
import json
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient
import httpx
import pytest

from app.metadata.ast_client import ASTClient, UpstreamError
from app.metadata.persistence import (FileMetadataStore, MetadataStorageError, canonical,
                                      metadata_id)
from app.metadata.project_service import ProjectMetadataService, ProjectLimits, ProjectExtractionError
from app.metadata.routes import v2_router, get_ast_client, get_metadata_store
from app.metadata.service import extract_ast_metadata
from app.metadata.upstream import digest
from app.metadata.validation import ASTValidationError

PROJECT = 'PRJ-123ABC'
ANALYSIS = 'AN-11111111-1111-4111-8111-111111111111'
FIXTURES = Path(__file__).parent / 'fixtures/metadata'


def reference(raw):
    ref = {k: raw[k] for k in ('schemaVersion', 'normalizationVersion', 'projectId', 'analysisId',
                              'relativePath', 'contentHash', 'parserId', 'parserVersion', 'grammarVersion')}
    ref['artifactId'] = digest([ref[k] for k in ('schemaVersion', 'normalizationVersion', 'projectId',
        'analysisId', 'relativePath', 'contentHash', 'parserId', 'parserVersion', 'grammarVersion')])
    return ref


class Upstream:
    def __init__(self, filenames=('coverage.jsx', 'empty.js')):
        self.documents = [json.loads((FIXTURES / (name + '.ast.json')).read_text()) for name in filenames]
        self.refs = [reference(d) for d in self.documents]
        self.bodies = {r['artifactId']: d for r, d in zip(self.refs, self.documents)}
        self.files = [dict(relativePath=d['relativePath'], status='syntax_errors' if d['parseStatus']=='syntax_errors' else 'parsed', artifact=r)
                      for r, d in zip(self.refs, self.documents)]
        self.calls = []
        self.unavailable = set()
        self.list_override = None
        self.summary_override = None
        self.transport_down = False

    def summary(self):
        counts = dict.fromkeys(('parsed','syntax_errors','unsupported','skipped','read_failed','parser_failed','ast_failed','storage_failed'), 0)
        counts.update(Counter(f['status'] for f in self.files))
        errors = any(counts[k] for k in ('syntax_errors','read_failed','parser_failed','ast_failed','storage_failed'))
        return dict(schemaVersion=1, projectId=PROJECT, analysisId=ANALYSIS,
                    status='completed_with_errors' if errors else 'completed',
                    startedAt='2026-11-09T00:00:00.000Z', completedAt='2026-11-09T00:00:01.000Z',
                    counts=counts, files=copy.deepcopy(self.files))

    def handler(self, request):
        self.calls.append(request.url.path)
        if self.transport_down:
            raise httpx.ConnectError('private details', request=request)
        if request.url.path.endswith('/asts'):
            payload = self.list_override if self.list_override is not None else dict(projectId=PROJECT, analysisId=ANALYSIS, artifacts=self.refs)
        elif '/asts/' in request.url.path:
            artifact_id = request.url.path.rsplit('/',1)[-1]
            if artifact_id in self.unavailable:
                return httpx.Response(404, json={'private':'detail'})
            payload = self.bodies[artifact_id]
        else:
            payload = self.summary_override if self.summary_override is not None else self.summary()
        return httpx.Response(200, json=payload)

    def client(self):
        return ASTClient(transport=httpx.MockTransport(self.handler))


def run(upstream, store, retry=False, limits=None):
    return asyncio.run(ProjectMetadataService(upstream.client(), store, limits).extract(PROJECT, ANALYSIS, retry_failed=retry))


def test_multi_file_persistence_and_counts(tmp_path):
    upstream, store = Upstream(), FileMetadataStore(tmp_path)
    analysis = run(upstream, store)
    assert analysis.status == 'complete'
    assert analysis.outcomeCounts == {'complete':2}
    assert len(analysis.files) == 2
    counts = Counter()
    ids = set()
    reconstructed = FileMetadataStore(tmp_path)
    for file in reconstructed.load_analysis(PROJECT, ANALYSIS).files:
        doc = reconstructed.load_file(PROJECT, ANALYSIS, file.metadataArtifact.artifactId)
        counts.update(e.kind for e in doc.entities)
        assert ids.isdisjoint(e.entityId for e in doc.entities)
        ids.update(e.entityId for e in doc.entities)
    assert dict(counts) == analysis.entityCounts
    assert analysis == reconstructed.load_analysis(PROJECT, ANALYSIS)


def test_syntax_errors_preserve_success(tmp_path):
    upstream, store = Upstream(('coverage.jsx','errors.js')), FileMetadataStore(tmp_path)
    analysis = run(upstream, store)
    assert analysis.status == 'partial'
    assert analysis.outcomeCounts == {'complete':1,'partial':1}
    partial = next(f for f in analysis.files if f.status == 'partial')
    document = store.load_file(PROJECT, ANALYSIS, partial.metadataArtifact.artifactId)
    assert document.diagnostics and partial.diagnostics
    assert any(e.name == 'reliable' for e in document.entities)
    assert any(e.name is not None for e in document.entities if e.kind == 'function')


def test_partial_unavailable_retry_and_idempotency(tmp_path):
    upstream, store = Upstream(), FileMetadataStore(tmp_path)
    upstream.unavailable.add(upstream.refs[1]['artifactId'])
    partial = run(upstream, store)
    assert partial.status == 'partial'
    assert partial.outcomeCounts == {'complete':1,'unavailable':1}
    calls = list(upstream.calls)
    assert run(upstream, store) == partial
    assert upstream.calls == calls
    upstream.unavailable.clear()
    upstream.calls.clear()
    completed = run(upstream, store, retry=True)
    assert completed.status == 'complete'
    assert completed.createdAt == partial.createdAt
    assert completed.files[0].metadataArtifact == partial.files[0].metadataArtifact
    ast_calls = [p for p in upstream.calls if '/asts/' in p]
    assert len(ast_calls) == 1 and ast_calls[0].endswith(upstream.refs[1]['artifactId'])
    upstream.transport_down = True
    assert run(upstream, store, retry=True) == completed


@pytest.mark.parametrize('status,expected', [('unsupported','unsupported'),('skipped','skipped'),('read_failed','upstream_failed')])
def test_no_supported_artifacts_are_not_silently_successful(tmp_path, status, expected):
    upstream, store = Upstream(()), FileMetadataStore(tmp_path)
    upstream.files = [dict(relativePath='source.ts',status=status,code='UNSUPPORTED_LANGUAGE' if status=='unsupported' else 'SOURCE_UNAVAILABLE')]
    analysis = run(upstream, store)
    assert analysis.status == 'failed'
    assert analysis.entityCounts == {}
    assert analysis.outcomeCounts == {expected:1}
    assert analysis.files[0].diagnostics


def test_empty_analysis(tmp_path):
    analysis = run(Upstream(()), FileMetadataStore(tmp_path))
    assert analysis.status == 'complete' and analysis.files == [] and analysis.entityCounts == {}


@pytest.mark.parametrize('case', ['bad-counts','missing-fields','foreign-summary','foreign-list','duplicate-list','duplicate-summary','extra-list','reference-mismatch'])
def test_invalid_upstream_contract(tmp_path, case):
    upstream, store = Upstream(), FileMetadataStore(tmp_path)
    summary = upstream.summary()
    listing = dict(projectId=PROJECT, analysisId=ANALYSIS, artifacts=copy.deepcopy(upstream.refs))
    if case == 'bad-counts': summary['counts']['parsed'] += 1
    if case == 'missing-fields': del summary['counts']
    if case == 'foreign-summary': summary['projectId'] = 'PRJ-ABCDEF'
    if case == 'foreign-list': listing['projectId'] = 'PRJ-ABCDEF'
    if case == 'duplicate-list': listing['artifacts'].append(listing['artifacts'][0])
    if case == 'duplicate-summary':
        summary['files'].append(copy.deepcopy(summary['files'][0])); summary['counts']['parsed'] += 1
    if case == 'extra-list':
        extra = json.loads((FIXTURES/'errors.js.ast.json').read_text()); listing['artifacts'].append(reference(extra))
    if case == 'reference-mismatch': listing['artifacts'][0]['projectId']='PRJ-ABCDEF'
    upstream.summary_override, upstream.list_override = summary, listing
    with pytest.raises(ASTValidationError): run(upstream,store)
    with pytest.raises(MetadataStorageError) as error: store.load_analysis(PROJECT,ANALYSIS)
    assert error.value.status_code == 404


@pytest.mark.parametrize('state', ['running','queued','failed','interrupted'])
def test_unsuitable_analysis(tmp_path, state):
    upstream = Upstream(())
    summary = upstream.summary(); summary['status'] = state
    if state in ('running','queued'): del summary['completedAt']
    else: summary['failureCode']='ANALYSIS_INTERRUPTED'
    upstream.summary_override = summary
    with pytest.raises(ProjectExtractionError) as error: run(upstream,FileMetadataStore(tmp_path))
    assert error.value.status_code == 409


def test_missing_list_artifact_is_recorded(tmp_path):
    upstream = Upstream()
    upstream.list_override = dict(projectId=PROJECT,analysisId=ANALYSIS,artifacts=[upstream.refs[0]])
    analysis = run(upstream,FileMetadataStore(tmp_path))
    assert analysis.status == 'partial' and analysis.files[1].status == 'unavailable'
    assert analysis.files[1].diagnostics[0].code == 'AST_NOT_LISTED'


@pytest.mark.parametrize('case,status', [('schema','invalid'),('language','unsupported'),('foreign','invalid'),('json','invalid')])
def test_bad_ast_keeps_other_file(tmp_path, case, status):
    upstream = Upstream()
    raw = copy.deepcopy(upstream.documents[1])
    if case == 'schema': raw['schemaVersion']=99
    if case == 'language': raw['language']='Python'
    if case == 'foreign': raw['projectId']='PRJ-ABCDEF'
    if case == 'json': raw={}
    upstream.bodies[upstream.refs[1]['artifactId']] = raw
    analysis = run(upstream, FileMetadataStore(tmp_path))
    assert analysis.status == 'partial'
    assert analysis.files[0].metadataArtifact
    assert analysis.files[1].status == status


def test_unavailable_upstream(tmp_path):
    upstream = Upstream(); upstream.transport_down=True
    with pytest.raises(UpstreamError) as error: run(upstream,FileMetadataStore(tmp_path))
    assert error.value.status_code == 503


def test_corrupt_document_and_summary(tmp_path):
    store = FileMetadataStore(tmp_path)
    analysis = run(Upstream(),store)
    directory = tmp_path/PROJECT/ANALYSIS
    filename = directory/(analysis.files[0].metadataArtifact.artifactId+'.metadata.json')
    payload=json.loads(filename.read_text()); payload['entities'][0]['projectId']='PRJ-ABCDEF'
    filename.write_text(json.dumps(payload))
    with pytest.raises(MetadataStorageError) as error: store.load_analysis(PROJECT,ANALYSIS)
    assert error.value.code == 'METADATA_STORAGE_CORRUPT'
    with pytest.raises(MetadataStorageError): run(Upstream(),store,retry=True)
    filename.write_text('{')
    with pytest.raises(MetadataStorageError): store.load_file(PROJECT,ANALYSIS,filename.name.split('.')[0])


@pytest.mark.parametrize('case', ['counts','missing-outcome','schema','missing-document','checksum'])
def test_summary_integrity(tmp_path, case):
    store=FileMetadataStore(tmp_path); analysis=run(Upstream(),store)
    directory=tmp_path/PROJECT/ANALYSIS; path=directory/'analysis.json'
    payload=json.loads(path.read_text())
    if case=='counts': payload['entityCounts']['module']+=1
    if case=='missing-outcome': payload['files'].pop()
    if case=='schema': payload['schemaVersion']=2
    if case=='checksum': payload['files'][0]['metadataArtifact']['documentHash']='a'*64
    if case=='missing-document': (directory/(analysis.files[0].metadataArtifact.artifactId+'.metadata.json')).unlink()
    else: path.write_text(json.dumps(payload))
    with pytest.raises(MetadataStorageError) as error: store.load_analysis(PROJECT,ANALYSIS)
    assert error.value.code == ('METADATA_STORAGE_INCOMPATIBLE' if case == 'schema' else 'METADATA_STORAGE_CORRUPT')


def test_immutable_metadata_and_source_conflict(tmp_path):
    upstream,store=Upstream(),FileMetadataStore(tmp_path)
    analysis=run(upstream,store)
    raw=upstream.documents[0]; document=extract_ast_metadata(raw)
    ref=store.save_file(document,upstream.refs[0]['artifactId'])
    assert ref == analysis.files[0].metadataArtifact
    changed=document.model_copy(deep=True)
    changed.entities[0].properties['incompatible']=True
    with pytest.raises(MetadataStorageError) as error: store.save_file(changed,upstream.refs[0]['artifactId'])
    assert error.value.code == 'METADATA_ARTIFACT_CONFLICT'


def test_limits_and_exclusive_locks(tmp_path,monkeypatch):
    upstream,store=Upstream(),FileMetadataStore(tmp_path)
    with pytest.raises(ProjectExtractionError) as error: run(upstream,store,limits=ProjectLimits(max_files=1))
    assert error.value.status_code==413
    result=run(upstream,store,limits=ProjectLimits(max_file_bytes=1))
    assert result.status=='failed' and result.outcomeCounts=={'resource_limited':2}
    with store.lock(PROJECT,ANALYSIS):
        with pytest.raises(MetadataStorageError) as error: run(upstream,store)
        assert error.value.code=='METADATA_EXTRACTION_BUSY'
    monkeypatch.setenv('CODEATLAS_METADATA_MAX_ACTIVE','1')
    with store.lock(PROJECT,ANALYSIS):
        with pytest.raises(MetadataStorageError) as error:
            with store.lock('PRJ-ABCDEF',ANALYSIS): pass
        assert error.value.code=='METADATA_EXTRACTION_BUSY'


def test_route_persistence_without_upstream(tmp_path):
    upstream=Upstream(); store=FileMetadataStore(tmp_path)
    app=FastAPI(); app.include_router(v2_router)
    app.dependency_overrides[get_ast_client]=upstream.client
    app.dependency_overrides[get_metadata_store]=lambda: FileMetadataStore(tmp_path)
    client=TestClient(app)
    prefix=f'/api/v2/metadata/projects/{PROJECT}/analyses/{ANALYSIS}'
    assert client.get(prefix).status_code==404
    response=client.post(prefix+'/extract'); assert response.status_code==200,response.text
    expected=response.json()
    upstream.transport_down=True
    assert client.get(prefix).json()==expected
    assert client.post(prefix+'/extract').json()==expected
    listing=client.get(prefix+'/files'); assert listing.status_code==200
    artifact_id=listing.json()['files'][0]['metadataArtifact']['artifactId']
    response=client.get(prefix+'/files/'+artifact_id)
    assert response.status_code==200 and response.json()['metadata']['sourceAst']['projectId']==PROJECT
    assert client.get(prefix+'/files/'+'a'*64).status_code==404
    assert client.get(prefix+'/files/invalid').status_code==400
    assert client.post('/api/v2/metadata/projects/invalid/analyses/'+ANALYSIS+'/extract').status_code==400
    directory=tmp_path/PROJECT/ANALYSIS
    (directory/'analysis.json').write_text('{')
    response=client.get(prefix)
    assert response.status_code==500 and response.json()['detail']['code']=='METADATA_STORAGE_CORRUPT'
    assert str(tmp_path) not in response.text


def test_retry_rejects_changed_source_snapshot(tmp_path):
    upstream,store=Upstream(),FileMetadataStore(tmp_path)
    upstream.unavailable.add(upstream.refs[1]['artifactId'])
    previous=run(upstream,store)
    changed=upstream.summary(); changed['completedAt']='2026-11-09T00:00:02.000Z'
    upstream.summary_override=changed
    with pytest.raises(ProjectExtractionError) as error: run(upstream,store,retry=True)
    assert error.value.code=='METADATA_ANALYSIS_CONFLICT'
    assert store.load_analysis(PROJECT,ANALYSIS)==previous


def test_deadline_records_remaining_outcomes(tmp_path,monkeypatch):
    async def delayed(*args):
        await asyncio.sleep(5)
    monkeypatch.setattr('app.metadata.project_service.retrieve_ast_metadata',delayed)
    result=run(Upstream(),FileMetadataStore(tmp_path),limits=ProjectLimits(timeout_seconds=.05))
    assert result.status=='failed'
    assert result.outcomeCounts=={'resource_limited':2}


def test_storage_symlinks_and_invalid_names(tmp_path):
    store=FileMetadataStore(tmp_path/'root')
    (tmp_path/'outside').mkdir()
    store.root.mkdir()
    (store.root/PROJECT).symlink_to(tmp_path/'outside',target_is_directory=True)
    with pytest.raises(MetadataStorageError) as error: store.load_analysis(PROJECT,ANALYSIS)
    assert error.value.code=='METADATA_STORAGE_UNSAFE'
    with pytest.raises(ValueError): store.load_analysis('../outside',ANALYSIS)
    with pytest.raises(ValueError): store.load_file(PROJECT,ANALYSIS,'../unsafe')


def test_file_checksum_detects_valid_shaped_tampering(tmp_path):
    store=FileMetadataStore(tmp_path); analysis=run(Upstream(),store)
    artifact_id=analysis.files[0].metadataArtifact.artifactId
    path=tmp_path/PROJECT/ANALYSIS/(artifact_id+'.metadata.json')
    payload=json.loads(path.read_text()); payload['entities'][0]['properties']['tampered']=True
    path.write_text(json.dumps(payload))
    # Its shape still validates, but the published summary's content checksum does not.
    with pytest.raises(MetadataStorageError) as error: store.load_analysis(PROJECT,ANALYSIS)
    assert error.value.code=='METADATA_STORAGE_CORRUPT'


def test_file_schema_incompatibility(tmp_path):
    store=FileMetadataStore(tmp_path); analysis=run(Upstream(),store)
    artifact_id=analysis.files[0].metadataArtifact.artifactId
    path=tmp_path/PROJECT/ANALYSIS/(artifact_id+'.metadata.json')
    payload=json.loads(path.read_text()); payload['extractorVersion']='future'
    path.write_text(json.dumps(payload))
    with pytest.raises(MetadataStorageError) as error: store.load_file(PROJECT,ANALYSIS,artifact_id)
    assert error.value.code=='METADATA_STORAGE_INCOMPATIBLE'


def test_unexpected_file_failure_does_not_discard_success(tmp_path,monkeypatch):
    from app.metadata.project_service import retrieve_ast_metadata as original
    async def fail_one(client, project, analysis, artifact_id):
        if artifact_id==upstream.refs[1]['artifactId']:
            raise RuntimeError('private internal failure')
        return await original(client,project,analysis,artifact_id)
    upstream=Upstream()
    monkeypatch.setattr('app.metadata.project_service.retrieve_ast_metadata',fail_one)
    result=run(upstream,FileMetadataStore(tmp_path))
    assert result.outcomeCounts=={'complete':1,'failed':1}
    assert 'private' not in result.model_dump_json()


def test_legacy_upstream_references_are_supported(tmp_path):
    from hashlib import sha256
    upstream=Upstream(('empty.js',))
    raw=upstream.documents[0]
    legacy_id=sha256(f"1:{raw['relativePath']}:{raw['contentHash']}".encode()).hexdigest()
    ref=dict(artifactId=legacy_id,relativePath=raw['relativePath'],contentHash=raw['contentHash'],schemaVersion=1)
    upstream.refs=[ref]
    upstream.files[0]['artifact']=ref
    upstream.bodies={legacy_id:raw}
    analysis=run(upstream,FileMetadataStore(tmp_path))
    assert analysis.status=='complete'
    assert FileMetadataStore(tmp_path).load_analysis(PROJECT,ANALYSIS)==analysis


def test_disk_write_failure_is_explicit(tmp_path,monkeypatch):
    import app.metadata.persistence as persistence
    def fail(*args,**kwargs):
        raise OSError('private disk location')
    monkeypatch.setattr(persistence.tempfile,'mkstemp',fail)
    with pytest.raises(MetadataStorageError) as error: run(Upstream(),FileMetadataStore(tmp_path))
    assert error.value.code=='METADATA_STORAGE_UNAVAILABLE'


def test_failed_analysis_saved_then_retry_can_succeed(tmp_path):
    upstream=Upstream(('empty.js',)); store=FileMetadataStore(tmp_path)
    upstream.unavailable.add(upstream.refs[0]['artifactId'])
    failed=run(upstream,store)
    assert failed.status=='failed'
    upstream.unavailable.clear()
    assert run(upstream,store,retry=True).status=='complete'
