"""Bounded, awaited project extraction; no detached jobs or semantic resolution."""
import asyncio
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
import logging
import os
import time

from app.metadata.ast_client import ASTClient, UpstreamError
from app.metadata.persistence import (MetadataStore, MetadataStorageError, canonical,
                                      validate_document, MAX_JSON_BYTES)
from app.metadata.project_models import (MetadataAnalysis, FileMetadataOutcome,
    ProjectDiagnostic, analysis_status)
from app.metadata.service import retrieve_ast_metadata
from app.metadata.upstream import UpstreamAnalysis, ArtifactList, parse_upstream, digest
from app.metadata.validation import ASTValidationError, UnsupportedLanguageError, NORMALIZATION

logger = logging.getLogger(__name__)


class ProjectExtractionError(Exception):
    def __init__(self, code, status_code=502):
        self.code = code
        self.status_code = status_code
        super().__init__(code)


@dataclass(frozen=True)
class ProjectLimits:
    max_files: int = 1000
    max_total_bytes: int = 128 * 1024 * 1024
    max_file_bytes: int = MAX_JSON_BYTES
    timeout_seconds: float = 120.0

    def __post_init__(self):
        if (type(self.max_files) is not int or not 1 <= self.max_files <= 1000
                or not 1 <= self.max_file_bytes <= MAX_JSON_BYTES
                or not 1 <= self.max_total_bytes <= 1024 * 1024 * 1024
                or not 0 < self.timeout_seconds <= 600):
            raise ValueError('Invalid metadata extraction limits')

    @classmethod
    def from_environment(cls):
        return cls(max_files=int(os.getenv('CODEATLAS_METADATA_MAX_FILES', '1000')),
                   max_total_bytes=int(os.getenv('CODEATLAS_METADATA_MAX_BYTES', str(128 * 1024 * 1024))),
                   max_file_bytes=int(os.getenv('CODEATLAS_METADATA_FILE_BYTES', str(MAX_JSON_BYTES))),
                   timeout_seconds=float(os.getenv('CODEATLAS_METADATA_TIMEOUT_SECONDS', '120')))


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')


def _diagnostic(code, path, artifact_id=None):
    messages = {
        'UPSTREAM_UNSUPPORTED': 'The upstream parser does not support this file.',
        'UPSTREAM_SKIPPED': 'The upstream analysis skipped this file.',
        'UPSTREAM_FILE_FAILED': 'The upstream analysis did not produce an AST for this file.',
        'AST_NOT_LISTED': 'The expected AST artifact is absent from the upstream list.',
        'UNSUPPORTED_LANGUAGE': 'No verified metadata adapter is available for this language.',
        'INVALID_AST_DOCUMENT': 'The upstream AST is invalid or has inconsistent provenance.',
        'METADATA_RESOURCE_LIMIT': 'The configured extraction resource limit was reached.',
        'METADATA_FILE_FAILED': 'Metadata extraction failed for this file.',
    }
    return ProjectDiagnostic(code=code, message=messages.get(code, 'The upstream AST artifact is unavailable.'),
                             relativePath=path, astArtifactId=artifact_id)


class ProjectMetadataService:
    def __init__(self, client: ASTClient, store: MetadataStore, limits: ProjectLimits | None = None):
        self.client = client
        self.store = store
        self.limits = limits or ProjectLimits.from_environment()

    async def extract(self, project_id: str, analysis_id: str, *, retry_failed=False):
        ASTClient._identity(project_id, analysis_id)
        with self.store.lock(project_id, analysis_id):
            try:
                previous = self.store.load_analysis(project_id, analysis_id)
            except MetadataStorageError as error:
                if error.status_code != 404:
                    raise
                previous = None
            if previous and (not retry_failed or previous.status == 'complete'):
                return previous
            created_at = previous.createdAt if previous else utc_now()
            deadline = time.monotonic() + self.limits.timeout_seconds
            async def within_budget(awaitable):
                remaining = deadline - time.monotonic()
                # wait_for cancels HTTP work on timeout; no background task survives.
                return await asyncio.wait_for(awaitable, timeout=max(remaining, 0))
            try:
                source = parse_upstream(UpstreamAnalysis,
                    await within_budget(self.client.get_analysis(project_id, analysis_id)), project_id, analysis_id)
                if source.status not in ('completed', 'completed_with_errors'):
                    raise ProjectExtractionError('UPSTREAM_ANALYSIS_NOT_READY', 409)
                if len(source.files) > self.limits.max_files:
                    raise ProjectExtractionError('METADATA_FILE_LIMIT', 413)
                listing = parse_upstream(ArtifactList,
                    await within_budget(self.client.get_ast_list(project_id, analysis_id)), project_id, analysis_id)
            except TimeoutError as error:
                raise ProjectExtractionError('METADATA_DEADLINE', 504) from error
            source_hash = digest(source.model_dump(exclude_unset=True))
            if previous and previous.sourceAnalysisHash != source_hash:
                raise ProjectExtractionError('METADATA_ANALYSIS_CONFLICT', 409)
            expected = {f.artifact.artifactId: f.artifact for f in source.files if f.artifact}
            listed = {r.artifactId: r for r in listing.artifacts}
            if set(listed) - set(expected):
                raise ASTValidationError('Artifact list contains artifacts outside the analysis snapshot')
            for artifact_id, ref in listed.items():
                original = expected[artifact_id]
                if (ref.relativePath != original.relativePath or ref.contentHash != original.contentHash
                        or any(getattr(original, k) != getattr(ref, k) for k in original.model_fields_set
                               if k not in ('artifactId', 'relativePath', 'contentHash', 'schemaVersion'))):
                    raise ASTValidationError('Artifact list/summary mismatch')
            old_files = {f.relativePath: f for f in previous.files} if previous else {}
            outcomes, totals = [], Counter()
            total_bytes = 0
            for file in source.files:
                path, upstream_ref = file.relativePath, file.artifact
                outcome = FileMetadataOutcome(relativePath=path, upstreamStatus=file.status,
                    status='failed', sourceArtifact=upstream_ref,
                    diagnostics=[_diagnostic('METADATA_FILE_FAILED', path)])
                old = old_files.get(path)
                if old and old.metadataArtifact is not None:
                    # load_analysis already verified the persisted document and checksums.
                    outcomes.append(old)
                    total_bytes += len(canonical(self.store.load_file(project_id, analysis_id, old.metadataArtifact.artifactId)))
                    totals.update(old.metadataArtifact.entityCounts)
                    continue
                if upstream_ref is None:
                    status, code = ('unsupported', 'UPSTREAM_UNSUPPORTED') if file.status == 'unsupported' else (
                        ('skipped', 'UPSTREAM_SKIPPED') if file.status == 'skipped' else ('upstream_failed', 'UPSTREAM_FILE_FAILED'))
                    outcome.status = status
                    outcome.diagnostics = [_diagnostic(code, path)]
                elif upstream_ref.artifactId not in listed:
                    outcome.status = 'unavailable'
                    outcome.diagnostics = [_diagnostic('AST_NOT_LISTED', path, upstream_ref.artifactId)]
                elif time.monotonic() >= deadline or total_bytes >= self.limits.max_total_bytes:
                    outcome.status = 'resource_limited'
                    outcome.diagnostics = [_diagnostic('METADATA_RESOURCE_LIMIT', path, upstream_ref.artifactId)]
                else:
                    try:
                        document = await within_budget(retrieve_ast_metadata(self.client, project_id, analysis_id, upstream_ref.artifactId))
                        provenance = document.sourceAst
                        if (provenance.relativePath != path or provenance.contentHash != upstream_ref.contentHash
                                or (file.status == 'syntax_errors') != (provenance.parseStatus == 'syntax_errors')):
                            raise ASTValidationError('Document/analysis mismatch')
                        ref = listed[upstream_ref.artifactId]
                        for key in ('parserId', 'parserVersion', 'grammarVersion', 'normalizationVersion'):
                            if key in ref.model_fields_set:
                                actual = getattr(provenance, key)
                                if key == 'normalizationVersion':
                                    actual = actual or NORMALIZATION
                                if actual != getattr(ref, key):
                                    raise ASTValidationError('Document/reference provenance mismatch')
                        payload_size = len(canonical(document))
                        if (payload_size > self.limits.max_file_bytes or total_bytes + payload_size > self.limits.max_total_bytes
                                or time.monotonic() >= deadline):
                            raise ProjectExtractionError('METADATA_RESOURCE_LIMIT', 413)
                        # The store validates entities and counts before publication.
                        validate_document(document)
                        metadata_ref = self.store.save_file(document, upstream_ref.artifactId)
                        outcome = FileMetadataOutcome(relativePath=path, upstreamStatus=file.status,
                            status=document.extractionStatus, sourceArtifact=upstream_ref, metadataArtifact=metadata_ref,
                            diagnostics=([ProjectDiagnostic(code='PARTIAL_EXTRACTION',
                                message='File metadata contains extraction diagnostics; retrieve its document for details.',
                                relativePath=path, astArtifactId=upstream_ref.artifactId)] if document.diagnostics else []))
                        total_bytes += payload_size
                        totals.update(document.summary)
                    except UnsupportedLanguageError:
                        outcome.status = 'unsupported'
                        outcome.diagnostics = [_diagnostic('UNSUPPORTED_LANGUAGE', path, upstream_ref.artifactId)]
                    except ASTValidationError:
                        outcome.status = 'invalid'
                        outcome.diagnostics = [_diagnostic('INVALID_AST_DOCUMENT', path, upstream_ref.artifactId)]
                    except UpstreamError as error:
                        outcome.status = 'invalid' if error.code in ('AST_INVALID_JSON', 'AST_RESPONSE_TOO_LARGE') else 'unavailable'
                        outcome.diagnostics = [_diagnostic(error.code, path, upstream_ref.artifactId)]
                    except (TimeoutError, ProjectExtractionError):
                        outcome.status = 'resource_limited'
                        outcome.diagnostics = [_diagnostic('METADATA_RESOURCE_LIMIT', path, upstream_ref.artifactId)]
                    except MetadataStorageError:
                        # Persistence failures must not publish a successful summary.
                        raise
                    except Exception:
                        logger.exception('Project metadata extraction failed for one artifact')
                outcomes.append(outcome)
            analysis = MetadataAnalysis(schemaVersion=1, projectId=project_id, analysisId=analysis_id,
                status=analysis_status(outcomes, source.status), createdAt=created_at,
                completedAt=utc_now(), sourceAnalysis=source, sourceAnalysisHash=source_hash,
                files=outcomes, entityCounts=dict(sorted(totals.items())),
                outcomeCounts=dict(Counter(f.status for f in outcomes)))
            self.store.save_analysis(analysis)
            return analysis
