"""Metadata-only filesystem store. JSON artifacts are immutable; summaries publish atomically."""
from collections import Counter
from contextlib import contextmanager
from hashlib import sha256
import fcntl
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Protocol

from pydantic import ValidationError
from app.metadata.ast_client import ASTClient
from app.metadata.models import MetadataDocument, deterministic_id
from app.metadata.project_models import MetadataAnalysis, MetadataReference
from app.metadata.upstream import digest, source_path, timestamp
from app.metadata.validation import NORMALIZATION, COORDINATES, ordered

MAX_JSON_BYTES = 16 * 1024 * 1024


class MetadataStorageError(Exception):
    def __init__(self, code, status_code=500):
        self.code = code
        self.status_code = status_code
        super().__init__(code)


class MetadataStore(Protocol):
    def lock(self, project_id: str, analysis_id: str): ...
    def save_file(self, document: MetadataDocument, ast_artifact_id: str) -> MetadataReference: ...
    def load_file(self, project_id: str, analysis_id: str, artifact_id: str) -> MetadataDocument: ...
    def save_analysis(self, analysis: MetadataAnalysis) -> None: ...
    def load_analysis(self, project_id: str, analysis_id: str) -> MetadataAnalysis: ...


def canonical(value) -> bytes:
    if isinstance(value, MetadataAnalysis):
        analysis = value
        value = analysis.model_dump()
        value['sourceAnalysis'] = analysis.sourceAnalysis.model_dump(exclude_unset=True)
        for payload, file in zip(value['files'], analysis.files):
            if file.sourceArtifact is not None:
                payload['sourceArtifact'] = file.sourceArtifact.model_dump(exclude_unset=True)
    elif hasattr(value, 'model_dump'):
        value = value.model_dump()
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def metadata_id(document: MetadataDocument) -> str:
    provenance = document.sourceAst.model_dump(exclude={'createdAt'})
    return digest(['metadata-file', document.schemaVersion, document.extractorVersion, provenance])


def validate_document(value) -> MetadataDocument:
    payload = value.model_dump() if isinstance(value, MetadataDocument) else value
    document = MetadataDocument.model_validate(payload, strict=True)
    source = document.sourceAst
    ASTClient._identity(source.projectId, source.analysisId)
    source_path(source.relativePath)
    timestamp(source.createdAt)
    if (type(source.schemaVersion) is not int or source.schemaVersion != 1 or
            source.normalizationVersion not in (None, NORMALIZATION) or source.coordinates != COORDINATES
            or source.language != 'JavaScript' or source.parserId != 'tree-sitter-javascript'
            or source.parserVersion != '0.25.1' or source.grammarVersion != '0.25.0'
            or not re.fullmatch(r'[a-f0-9]{64}', source.contentHash)
            or source.parseStatus not in ('success', 'syntax_errors')):
        raise ValueError('Invalid stored provenance')
    if (source.parseStatus == 'syntax_errors' or document.diagnostics) and document.extractionStatus != 'partial':
        raise ValueError('Lost partial extraction status')
    if len(document.entities) > 100000 or len(document.diagnostics) > 100000:
        raise ValueError('Metadata size limit')
    entities = {e.entityId: e for e in document.entities}
    if len(entities) != len(document.entities) or dict(Counter(e.kind for e in document.entities)) != document.summary:
        raise ValueError('Invalid metadata counts/IDs')
    roots = [e for e in document.entities if e.parentEntityId is None]
    if len(roots) != 1 or roots[0].kind != 'module' or roots[0].astNodeId != source.rootId:
        raise ValueError('Invalid metadata root')
    children = {}
    for entity in document.entities:
        if any(getattr(entity, field) != getattr(source, field) for field in ('projectId', 'analysisId', 'relativePath', 'contentHash', 'language')):
            raise ValueError('Foreign metadata entity')
        if (not re.fullmatch(r'n\d+', entity.astNodeId) or
                entity.entityId != deterministic_id(source, entity.astNodeId, entity.kind)):
            raise ValueError('Invalid entity identity')
        if not ordered(entity.sourceRange.start, entity.sourceRange.end):
            raise ValueError('Invalid entity range')
        if entity.parentEntityId is not None:
            parent = entities.get(entity.parentEntityId)
            if (parent is None or not ordered(parent.sourceRange.start, entity.sourceRange.start)
                    or not ordered(entity.sourceRange.end, parent.sourceRange.end)):
                raise ValueError('Invalid entity ownership')
            children.setdefault(parent.entityId, []).append(entity.entityId)
    seen, stack = set(), [(roots[0].entityId, 0)]
    while stack:
        entity_id, depth = stack.pop()
        if entity_id in seen or depth > 512:
            raise ValueError('Invalid metadata graph')
        seen.add(entity_id)
        stack.extend((c, depth + 1) for c in children.get(entity_id, []))
    if len(seen) != len(entities):
        raise ValueError('Disconnected entity graph')
    return document


class FileMetadataStore:
    def __init__(self, root: str | Path | None = None):
        self.root = Path(root or os.getenv('CODEATLAS_METADATA_ROOT', 'data/metadata')).expanduser().resolve()

    def _directory(self, project_id, analysis_id, create=False):
        ASTClient._identity(project_id, analysis_id)
        try:
            if create:
                self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
            current = self.root
            for part in (project_id, analysis_id):
                current = current / part
                if current.is_symlink():
                    raise MetadataStorageError('METADATA_STORAGE_UNSAFE')
                if create:
                    current.mkdir(exist_ok=True, mode=0o700)
            if not current.is_dir():
                raise MetadataStorageError('METADATA_NOT_FOUND', 404)
            return current
        except OSError as error:
            raise MetadataStorageError('METADATA_STORAGE_UNAVAILABLE') from error

    def _read(self, path):
        try:
            if path.is_symlink():
                raise ValueError('Symlink artifact')
            with path.open('rb') as stream:
                payload = stream.read(MAX_JSON_BYTES + 1)
            if len(payload) > MAX_JSON_BYTES:
                raise ValueError('Stored JSON exceeds limit')
            return json.loads(payload, parse_constant=lambda _: _bad_json())
        except FileNotFoundError as error:
            raise MetadataStorageError('METADATA_NOT_FOUND', 404) from error
        except (ValueError, UnicodeError, RecursionError) as error:
            raise MetadataStorageError('METADATA_STORAGE_CORRUPT') from error
        except OSError as error:
            raise MetadataStorageError('METADATA_STORAGE_UNAVAILABLE') from error

    @staticmethod
    def _lock_file(path):
        if path.is_symlink():
            raise MetadataStorageError('METADATA_STORAGE_UNSAFE')
        descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return descriptor
        except BlockingIOError:
            os.close(descriptor)
            return None

    @contextmanager
    def lock(self, project_id, analysis_id):
        directory = self._directory(project_id, analysis_id, True)
        capacity = int(os.getenv('CODEATLAS_METADATA_MAX_ACTIVE', '2'))
        if not 1 <= capacity <= 8:
            raise ValueError('Invalid metadata concurrency capacity')
        descriptor = self._lock_file(directory / '.extract.lock')
        if descriptor is None:
            raise MetadataStorageError('METADATA_EXTRACTION_BUSY', 409)
        slot = None
        try:
            for index in range(capacity):
                slot = self._lock_file(self.root / f'.active-{index}.lock')
                if slot is not None:
                    break
            if slot is None:
                raise MetadataStorageError('METADATA_EXTRACTION_BUSY', 409)
            yield
        finally:
            if slot is not None:
                os.close(slot)
            os.close(descriptor)  # OS releases locks on process exit or cancellation.

    def _write(self, path, payload, immutable=False):
        if len(payload) > MAX_JSON_BYTES:
            raise MetadataStorageError('METADATA_DOCUMENT_TOO_LARGE', 413)
        if path.is_symlink():
            raise MetadataStorageError('METADATA_STORAGE_UNSAFE')
        try:
            descriptor, temporary = tempfile.mkstemp(prefix='.metadata-', suffix='.tmp', dir=path.parent)
        except OSError as error:
            raise MetadataStorageError('METADATA_STORAGE_UNAVAILABLE') from error
        try:
            with os.fdopen(descriptor, 'wb') as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            if immutable:
                try:
                    os.link(temporary, path)
                except FileExistsError:
                    existing = canonical(self._read(path))
                    if existing != payload:
                        raise MetadataStorageError('METADATA_ARTIFACT_CONFLICT', 409)
            else:
                os.replace(temporary, path)
            folder = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(folder)
            finally:
                os.close(folder)
        except OSError as error:
            raise MetadataStorageError('METADATA_STORAGE_UNAVAILABLE') from error
        finally:
            Path(temporary).unlink(missing_ok=True)

    def save_file(self, document, ast_artifact_id):
        try:
            document = validate_document(document)
            source = document.sourceAst
            ASTClient._identity(source.projectId, source.analysisId, ast_artifact_id)
            # Ensure the provided upstream key matches this document's descriptor.
            from app.metadata.upstream import ASTReference
            ASTReference.model_validate(dict(artifactId=ast_artifact_id, relativePath=source.relativePath,
                contentHash=source.contentHash, schemaVersion=1, projectId=source.projectId,
                analysisId=source.analysisId, parserId=source.parserId, parserVersion=source.parserVersion,
                grammarVersion=source.grammarVersion, normalizationVersion=source.normalizationVersion or NORMALIZATION))
        except (ValidationError, ValueError, TypeError) as error:
            raise MetadataStorageError('METADATA_STORAGE_INVALID') from error
        payload = canonical(document)
        artifact_id = metadata_id(document)
        directory = self._directory(source.projectId, source.analysisId, True)
        self._write(directory / f'{artifact_id}.metadata.json', payload, immutable=True)
        return MetadataReference(artifactId=artifact_id, astArtifactId=ast_artifact_id,
            projectId=source.projectId, analysisId=source.analysisId, relativePath=source.relativePath,
            contentHash=source.contentHash, documentHash=sha256(payload).hexdigest(),
            extractionStatus=document.extractionStatus, entityCounts=document.summary)

    def load_file(self, project_id, analysis_id, artifact_id):
        ASTClient._identity(project_id, analysis_id, artifact_id)
        directory = self._directory(project_id, analysis_id)
        payload = self._read(directory / f'{artifact_id}.metadata.json')
        if isinstance(payload, dict) and (payload.get('schemaVersion') != 1 or payload.get('extractorVersion') != 'javascript-syntax-v1'):
            raise MetadataStorageError('METADATA_STORAGE_INCOMPATIBLE')
        try:
            document = validate_document(payload)
            if (document.sourceAst.projectId != project_id or document.sourceAst.analysisId != analysis_id
                    or metadata_id(document) != artifact_id):
                raise ValueError('Foreign stored metadata')
            return document
        except (ValueError, TypeError, ValidationError) as error:
            raise MetadataStorageError('METADATA_STORAGE_CORRUPT') from error

    def _verify_files(self, analysis):
        for file in analysis.files:
            if file.metadataArtifact is None:
                continue
            ref = file.metadataArtifact
            try:
                document = self.load_file(analysis.projectId, analysis.analysisId, ref.artifactId)
            except MetadataStorageError as error:
                if error.status_code == 404:
                    raise MetadataStorageError('METADATA_STORAGE_CORRUPT') from error
                raise
            source = document.sourceAst
            if any(getattr(source, key) != getattr(file.sourceArtifact, key) for key in
                   ('parserId', 'parserVersion', 'grammarVersion') if key in file.sourceArtifact.model_fields_set):
                raise MetadataStorageError('METADATA_STORAGE_CORRUPT')
            if (source.parseStatus == 'syntax_errors') != (file.upstreamStatus == 'syntax_errors'):
                raise MetadataStorageError('METADATA_STORAGE_CORRUPT')
            if (ref.documentHash != sha256(canonical(document)).hexdigest() or
                    ref.entityCounts != document.summary or ref.extractionStatus != document.extractionStatus
                    or ref.relativePath != source.relativePath or ref.contentHash != source.contentHash):
                raise MetadataStorageError('METADATA_STORAGE_CORRUPT')

    def save_analysis(self, analysis):
        try:
            analysis = MetadataAnalysis.model_validate(json.loads(canonical(analysis)), strict=True)
        except (ValidationError, ValueError, TypeError) as error:
            raise MetadataStorageError('METADATA_STORAGE_INVALID') from error
        self._verify_files(analysis)
        directory = self._directory(analysis.projectId, analysis.analysisId, True)
        path = directory / 'analysis.json'
        if path.exists():
            previous = self.load_analysis(analysis.projectId, analysis.analysisId)
            if (previous.sourceAnalysisHash != analysis.sourceAnalysisHash or previous.extractionVersion != analysis.extractionVersion
                    or previous.createdAt != analysis.createdAt):
                raise MetadataStorageError('METADATA_ANALYSIS_CONFLICT', 409)
            refs = {f.relativePath: f.metadataArtifact for f in analysis.files}
            if any(f.metadataArtifact and refs.get(f.relativePath) != f.metadataArtifact for f in previous.files):
                raise MetadataStorageError('METADATA_ANALYSIS_CONFLICT', 409)
            if previous.status == 'complete' and canonical(previous) != canonical(analysis):
                raise MetadataStorageError('METADATA_ANALYSIS_CONFLICT', 409)
        self._write(path, canonical(analysis))

    def load_analysis(self, project_id, analysis_id):
        directory = self._directory(project_id, analysis_id)
        payload = self._read(directory / 'analysis.json')
        if isinstance(payload, dict) and (payload.get('schemaVersion') != 1 or payload.get('extractionVersion') != 'project-metadata-v1/javascript-syntax-v1'):
            raise MetadataStorageError('METADATA_STORAGE_INCOMPATIBLE')
        try:
            analysis = MetadataAnalysis.model_validate(payload, strict=True)
            if analysis.projectId != project_id or analysis.analysisId != analysis_id:
                raise ValueError('Foreign summary')
        except (ValidationError, ValueError, TypeError) as error:
            raise MetadataStorageError('METADATA_STORAGE_CORRUPT') from error
        self._verify_files(analysis)
        return analysis


def _bad_json():
    raise ValueError('Invalid JSON constant')
