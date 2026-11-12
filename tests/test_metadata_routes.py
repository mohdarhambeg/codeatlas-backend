import asyncio
from hashlib import sha256
import json
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient
import httpx
import pytest

from app.metadata.ast_client import ASTClient, UpstreamError
from app.metadata.routes import router, v2_router, get_ast_client
from app.metadata.service import retrieve_ast_metadata
from app.metadata.validation import ASTValidationError

PROJECT = 'PRJ-123ABC'
ANALYSIS = 'AN-11111111-1111-4111-8111-111111111111'
FIXTURE = Path(__file__).parent / 'fixtures/metadata/coverage.jsx.ast.json'


def artifact(raw):
    values = [raw['schemaVersion'], raw.get('normalizationVersion', 'syntax-preorder-v1'),
              raw['projectId'], raw['analysisId'], raw['relativePath'], raw['contentHash'],
              raw['parserId'], raw['parserVersion'], raw['grammarVersion']]
    return sha256(json.dumps(values, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()


@pytest.fixture
def raw():
    return json.loads(FIXTURE.read_text())


def app_client(handler):
    application = FastAPI()
    application.include_router(router)
    application.include_router(v2_router)
    upstream = ASTClient(transport=httpx.MockTransport(handler))
    application.dependency_overrides[get_ast_client] = lambda: upstream
    return TestClient(application)


def test_route_success_and_legacy(raw):
    seen = []
    def handler(request):
        seen.append(request.url.path)
        return httpx.Response(200, json=raw)
    client = app_client(handler)
    request = dict(project_id=PROJECT, analysis_id=ANALYSIS, artifact_id=artifact(raw))
    response = client.post('/api/v2/metadata/extract-from-ast', json=request)
    assert response.status_code == 200
    result = response.json()['metadata']
    assert result['schemaVersion'] == 1
    assert result['sourceAst']['projectId'] == PROJECT
    assert result['sourceAst']['analysisId'] == ANALYSIS
    assert result['sourceAst']['relativePath'] == raw['relativePath']
    assert seen == [f'/api/projects/{PROJECT}/analysis/{ANALYSIS}/asts/{artifact(raw)}']
    legacy = client.post('/api/v1/metadata/extract-from-ast', json=request)
    assert legacy.status_code == 200
    metadata = legacy.json()['metadata']
    assert metadata['project_id'] == PROJECT
    assert metadata['summary']['total_classes'] == 5
    assert metadata['extraction_status'] == 'complete'


@pytest.mark.parametrize('case,status,code', [
    ('404', 404, 'AST_NOT_FOUND'), ('500', 502, 'AST_UPSTREAM_HTTP_ERROR'),
    ('403', 502, 'AST_UPSTREAM_HTTP_ERROR'), ('302', 502, 'AST_UPSTREAM_HTTP_ERROR'),
    ('connect', 503, 'AST_UNAVAILABLE'), ('timeout', 504, 'AST_TIMEOUT'),
    ('json', 502, 'AST_INVALID_JSON'), ('invalid', 502, 'INVALID_AST_DOCUMENT'),
    ('unknown', 422, 'UNSUPPORTED_LANGUAGE'), ('parser', 502, 'INVALID_AST_DOCUMENT'),
    ('foreign', 502, 'INVALID_AST_DOCUMENT'), ('wrongartifact', 502, 'INVALID_AST_DOCUMENT'),
])
def test_remote_errors(raw, case, status, code):
    requested_artifact = artifact(raw)
    def handler(request):
        if case.isdigit():
            return httpx.Response(int(case), text='/secret/internal/path upstream details')
        if case == 'connect':
            raise httpx.ConnectError('secret', request=request)
        if case == 'timeout':
            raise httpx.ReadTimeout('secret', request=request)
        if case == 'json':
            return httpx.Response(200, text='not json /secret/internal/path')
        if case == 'invalid':
            return httpx.Response(200, json={})
        if case == 'unknown':
            raw['language'] = 'Python'
        if case == 'parser':
            raw['parserId'] = 'unverified'
        if case == 'foreign':
            raw['projectId'] = 'PRJ-ABCDEF'
        if case == 'wrongartifact':
            raw['contentHash'] = 'a' * 64
        return httpx.Response(200, json=raw)
    # For adapter-policy tests use an artifact matching the changed provenance.
    if case == 'parser':
        raw['parserId'] = 'unverified'
        requested_artifact = artifact(raw)
    client = app_client(handler)
    result = client.post('/api/v2/metadata/extract-from-ast', json=dict(
        project_id=PROJECT, analysis_id=ANALYSIS, artifact_id=requested_artifact))
    assert result.status_code == status, result.text
    assert result.json()['detail']['code'] == code
    assert 'secret' not in result.text


def test_invalid_identifiers_never_contact_upstream():
    def handler(request):
        pytest.fail('Invalid identifiers must not reach upstream')
    response = app_client(handler).post('/api/v1/metadata/extract-from-ast', json=dict(
        project_id='../secret', analysis_id=ANALYSIS, artifact_id='a' * 64))
    assert response.status_code == 400


def test_local_routes(tmp_path):
    client = app_client(lambda _: pytest.fail('Local routes must not use HTTP'))
    source = tmp_path / 'example.py'
    source.write_text('class A:\n    def method(self): pass\ndef function(): pass\n')
    extracted = client.post('/api/v1/metadata/extract', json={'file_path': str(source)})
    assert extracted.status_code == 200
    assert extracted.json()['metadata']['functions'][0]['name'] == 'function'
    scanned = client.post('/api/v1/metadata/scan-project', json={'project_path': str(tmp_path)})
    assert scanned.status_code == 200
    assert scanned.json()['summary']['total_methods'] == 1
    for path, payload in [('/extract', {'file_path': str(tmp_path / 'secret.py')}),
                          ('/scan-project', {'project_path': str(tmp_path / 'secret')})]:
        response = client.post('/api/v1/metadata' + path, json=payload)
        assert response.status_code == 404
        assert str(tmp_path) not in response.text
    source.write_bytes(b'\xff')
    response = client.post('/api/v1/metadata/extract', json={'file_path': str(source)})
    assert response.status_code == 400
    assert 'UTF-8' in response.text and str(tmp_path) not in response.text
    source.write_text('def (')
    response = client.post('/api/v1/metadata/extract', json={'file_path': str(source)})
    assert response.status_code == 400 and str(tmp_path) not in response.text


def test_config_and_other_http_methods(monkeypatch):
    monkeypatch.setenv('CODEATLAS_AST_BASE_URL', 'http://localhost:5050/')
    paths = []
    def handler(request):
        paths.append(str(request.url))
        return httpx.Response(200, json={'ok': True})
    client = ASTClient(transport=httpx.MockTransport(handler))
    assert client.base_url == 'http://localhost:5050'
    assert asyncio.run(client.get_analysis(PROJECT, ANALYSIS)) == {'ok': True}
    assert asyncio.run(client.get_ast_list(PROJECT, ANALYSIS)) == {'ok': True}
    assert paths[1].endswith('/asts')
    assert client.timeout.connect == 5.0 and client.timeout.read == 30.0
    with pytest.raises(ValueError):
        ASTClient('file:///private')


def test_legacy_artifact_and_foreign_analysis(raw):
    legacy = sha256(f"1:{raw['relativePath']}:{raw['contentHash']}".encode()).hexdigest()
    upstream = ASTClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=raw)))
    assert asyncio.run(retrieve_ast_metadata(upstream, PROJECT, ANALYSIS, legacy)).sourceAst.projectId == PROJECT
    raw['analysisId'] = 'AN-22222222-2222-4222-8222-222222222222'
    with pytest.raises(ASTValidationError):
        asyncio.run(retrieve_ast_metadata(upstream, PROJECT, ANALYSIS, artifact(raw)))


def test_unexpected_failure_is_safe(raw, monkeypatch):
    async def fail(*args):
        raise RuntimeError('/secret/private/path')
    monkeypatch.setattr('app.metadata.routes.retrieve_ast_metadata', fail)
    response = app_client(lambda _: None).post('/api/v2/metadata/extract-from-ast', json=dict(
        project_id=PROJECT, analysis_id=ANALYSIS, artifact_id=artifact(raw)))
    assert response.status_code == 500
    assert response.json()['detail']['code'] == 'METADATA_EXTRACTION_FAILED'
    assert 'secret' not in response.text


def test_response_budget_and_non_json_constants():
    class Oversized(httpx.AsyncByteStream):
        async def __aiter__(self):
            for _ in range(65):
                yield b'x' * (1024 * 1024)
    client = ASTClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=Oversized())))
    with pytest.raises(UpstreamError) as error:
        asyncio.run(client.get_analysis(PROJECT, ANALYSIS))
    assert error.value.code == 'AST_RESPONSE_TOO_LARGE'
    client = ASTClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, content=b'{"x":NaN}')))
    with pytest.raises(UpstreamError) as error:
        asyncio.run(client.get_analysis(PROJECT, ANALYSIS))
    assert error.value.code == 'AST_INVALID_JSON'


def test_main_registers_both_metadata_versions():
    from main import app
    schema = app.openapi()
    assert '/api/v1/metadata/extract' in schema['paths']
    assert '/api/v1/metadata/scan-project' in schema['paths']
    assert '/api/v1/metadata/extract-from-ast' in schema['paths']
    assert '/api/v2/metadata/extract-from-ast' in schema['paths']
