"""Verified TypeScript analysis-summary and AST-reference HTTP contracts."""
from collections import Counter
from datetime import datetime
from hashlib import sha256
import json
import re
from typing import Literal

from pydantic import Field, ValidationError, model_validator
from app.metadata.validation import Contract, ASTValidationError, NORMALIZATION

PROJECT = r'^PRJ-[A-Fa-f0-9]{6}$'
ANALYSIS = r'^AN-[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$'
HASH = r'^[a-f0-9]{64}$'
FILE_STATUSES = ('parsed', 'syntax_errors', 'unsupported', 'skipped', 'read_failed',
                 'parser_failed', 'ast_failed', 'storage_failed')


def source_path(value: str):
    if (not value or len(value) > 16384 or '\0' in value or '\\' in value or value.startswith('/')
            or re.match(r'^[A-Za-z]:', value) or any(p in ('', '.', '..') for p in value.split('/'))):
        raise ValueError('Invalid source path')
    return value


def timestamp(value: str):
    if not re.fullmatch(r'\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z', value):
        raise ValueError('Invalid timestamp')
    datetime.fromisoformat(value.replace('Z', '+00:00'))
    return value


def digest(value) -> str:
    return sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'),
                             allow_nan=False).encode('utf-8')).hexdigest()


class Versioned(Contract):
    schemaVersion: Literal[1]

    @model_validator(mode='before')
    @classmethod
    def strict_version(cls, value):
        if isinstance(value, dict) and type(value.get('schemaVersion')) is not int:
            raise ValueError('Invalid schema version')
        return value


class ASTReference(Versioned):
    artifactId: str = Field(pattern=HASH)
    relativePath: str
    contentHash: str = Field(pattern=HASH)
    projectId: str | None = Field(default=None, pattern=PROJECT)
    analysisId: str | None = Field(default=None, pattern=ANALYSIS)
    parserId: str | None = None
    parserVersion: str | None = None
    grammarVersion: str | None = None
    normalizationVersion: str | None = None

    @model_validator(mode='after')
    def identity(self):
        source_path(self.relativePath)
        provenance = {'projectId', 'analysisId', 'parserId', 'parserVersion', 'grammarVersion', 'normalizationVersion'}
        supplied = self.model_fields_set & provenance
        if supplied and (supplied != provenance or self.projectId is None or self.analysisId is None
                         or not self.parserId or re.search(r'\s', self.parserId)
                         or self.normalizationVersion != NORMALIZATION):
            raise ValueError('Incomplete or unsupported reference provenance')
        legacy = sha256(f'1:{self.relativePath}:{self.contentHash}'.encode()).hexdigest()
        current = digest([1, self.normalizationVersion, self.projectId, self.analysisId,
                          self.relativePath, self.contentHash, self.parserId, self.parserVersion, self.grammarVersion])
        if self.artifactId != legacy and (not supplied or self.artifactId != current):
            raise ValueError('Artifact key/provenance mismatch')
        return self

    def owned_by(self, project_id, analysis_id):
        if (self.projectId is not None and (self.projectId != project_id or self.analysisId != analysis_id)):
            raise ValueError('Foreign artifact reference')


class UpstreamFile(Contract):
    relativePath: str
    status: Literal['parsed', 'syntax_errors', 'unsupported', 'skipped', 'read_failed',
                    'parser_failed', 'ast_failed', 'storage_failed']
    code: str | None = Field(default=None, pattern=r'^[A-Z][A-Z0-9_]{0,127}$')
    message: str | None = Field(default=None, min_length=1, max_length=16384)
    artifact: ASTReference | None = None

    @model_validator(mode='after')
    def consistency(self):
        source_path(self.relativePath)
        parsed = self.status in ('parsed', 'syntax_errors')
        if parsed != ('artifact' in self.model_fields_set and self.artifact is not None):
            raise ValueError('Artifact/file status mismatch')
        if parsed:
            if 'code' in self.model_fields_set or 'message' in self.model_fields_set:
                raise ValueError('Parsed file with error')
            if self.artifact.relativePath != self.relativePath:
                raise ValueError('Artifact path mismatch')
        elif self.code is None:
            raise ValueError('Missing outcome code')
        return self


class UpstreamAnalysis(Versioned):
    projectId: str = Field(pattern=PROJECT)
    analysisId: str = Field(pattern=ANALYSIS)
    status: Literal['queued', 'running', 'completed', 'completed_with_errors', 'failed', 'interrupted']
    startedAt: str
    completedAt: str | None = None
    failureCode: str | None = Field(default=None, pattern=r'^[A-Z][A-Z0-9_]{0,127}$')
    counts: dict[str, int]
    files: list[UpstreamFile] = Field(max_length=1000)

    @model_validator(mode='after')
    def consistency(self):
        timestamp(self.startedAt)
        terminal = self.status not in ('queued', 'running')
        if terminal != ('completedAt' in self.model_fields_set and self.completedAt is not None):
            raise ValueError('Invalid completion state')
        if self.completedAt is not None:
            timestamp(self.completedAt)
            if self.completedAt < self.startedAt:
                raise ValueError('Invalid lifecycle timestamps')
        if (self.status in ('failed', 'interrupted')) != (self.failureCode is not None):
            raise ValueError('Invalid failure state')
        if len({f.relativePath for f in self.files}) != len(self.files):
            raise ValueError('Duplicate file outcomes')
        actual = Counter(f.status for f in self.files)
        if (set(self.counts) != set(FILE_STATUSES) or any(type(v) is not int or v < 0 for v in self.counts.values())
                or any(self.counts[k] != actual[k] for k in FILE_STATUSES)):
            raise ValueError('Invalid upstream counts')
        ids = []
        for file in self.files:
            if file.artifact:
                file.artifact.owned_by(self.projectId, self.analysisId)
                ids.append(file.artifact.artifactId)
        if len(ids) != len(set(ids)):
            raise ValueError('Duplicate artifact references')
        if self.status == 'queued' and self.files:
            raise ValueError('Queued analysis has outcomes')
        errors = any(actual[k] for k in ('syntax_errors', 'read_failed', 'parser_failed', 'ast_failed', 'storage_failed'))
        errors = errors or any(f.code == 'ANALYSIS_TIME_BUDGET' for f in self.files)
        if self.status == 'completed' and errors or self.status == 'completed_with_errors' and not errors:
            raise ValueError('Upstream status/count mismatch')
        return self


class ArtifactList(Contract):
    projectId: str = Field(pattern=PROJECT)
    analysisId: str = Field(pattern=ANALYSIS)
    artifacts: list[ASTReference] = Field(max_length=1000)

    @model_validator(mode='after')
    def consistency(self):
        if len({r.artifactId for r in self.artifacts}) != len(self.artifacts):
            raise ValueError('Duplicate artifact references')
        if len({r.relativePath for r in self.artifacts}) != len(self.artifacts):
            raise ValueError('Multiple artifacts for one source path')
        for ref in self.artifacts:
            ref.owned_by(self.projectId, self.analysisId)
        return self


def parse_upstream(model, value, project_id, analysis_id):
    try:
        parsed = model.model_validate(value)
        if parsed.projectId != project_id or parsed.analysisId != analysis_id:
            raise ValueError('Foreign upstream response')
        return parsed
    except (ValidationError, ValueError, TypeError) as error:
        raise ASTValidationError('Invalid upstream analysis or artifact list') from error
