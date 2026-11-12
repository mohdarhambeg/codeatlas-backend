"""Persisted project extraction v1, containing references rather than entity payloads."""
from collections import Counter
from typing import Literal
from pydantic import Field, model_validator
from app.metadata.upstream import (Versioned, Contract, UpstreamAnalysis, ASTReference,
                                   PROJECT, ANALYSIS, HASH, source_path, timestamp)

EXTRACTION_VERSION = 'project-metadata-v1/javascript-syntax-v1'
SUCCESS = {'complete', 'partial'}


class ProjectDiagnostic(Contract):
    code: str = Field(min_length=1, max_length=128)
    message: str = Field(min_length=1, max_length=512)
    relativePath: str | None = None
    astArtifactId: str | None = Field(default=None, pattern=HASH)


class MetadataReference(Contract):
    artifactId: str = Field(pattern=HASH)
    astArtifactId: str = Field(pattern=HASH)
    projectId: str = Field(pattern=PROJECT)
    analysisId: str = Field(pattern=ANALYSIS)
    relativePath: str
    contentHash: str = Field(pattern=HASH)
    documentHash: str = Field(pattern=HASH)
    extractionStatus: Literal['complete', 'partial']
    entityCounts: dict[str, int]


class FileMetadataOutcome(Contract):
    relativePath: str
    upstreamStatus: str
    status: Literal['complete', 'partial', 'unsupported', 'unavailable', 'invalid',
                    'failed', 'upstream_failed', 'skipped', 'resource_limited']
    sourceArtifact: ASTReference | None = None
    metadataArtifact: MetadataReference | None = None
    diagnostics: list[ProjectDiagnostic] = Field(default_factory=list, max_length=16)

    @model_validator(mode='after')
    def consistency(self):
        source_path(self.relativePath)
        if (self.status in SUCCESS) != (self.metadataArtifact is not None):
            raise ValueError('Metadata outcome/artifact mismatch')
        if self.metadataArtifact:
            ref = self.metadataArtifact
            if (self.sourceArtifact is None or ref.astArtifactId != self.sourceArtifact.artifactId or
                    ref.relativePath != self.relativePath or ref.contentHash != self.sourceArtifact.contentHash
                    or ref.extractionStatus != self.status):
                raise ValueError('Metadata reference mismatch')
        return self


def analysis_status(files, upstream_status):
    successes = sum(f.status in SUCCESS for f in files)
    # An empty successful analysis is valid and distinct from an all-unsupported one.
    if not files and upstream_status == 'completed':
        return 'complete'
    if successes == len(files) and all(f.status == 'complete' for f in files) and upstream_status == 'completed':
        return 'complete'
    return 'partial' if successes else 'failed'


class MetadataAnalysis(Versioned):
    projectId: str = Field(pattern=PROJECT)
    analysisId: str = Field(pattern=ANALYSIS)
    extractionVersion: Literal['project-metadata-v1/javascript-syntax-v1'] = EXTRACTION_VERSION
    status: Literal['complete', 'partial', 'failed']
    createdAt: str
    completedAt: str
    sourceAnalysis: UpstreamAnalysis
    sourceAnalysisHash: str = Field(pattern=HASH)
    files: list[FileMetadataOutcome] = Field(max_length=1000)
    entityCounts: dict[str, int]
    outcomeCounts: dict[str, int]
    diagnostics: list[ProjectDiagnostic] = Field(default_factory=list, max_length=1000)

    @model_validator(mode='after')
    def consistency(self):
        from app.metadata.upstream import digest
        timestamp(self.createdAt)
        timestamp(self.completedAt)
        if self.completedAt < self.createdAt:
            raise ValueError('Invalid metadata lifecycle')
        source = self.sourceAnalysis
        if (source.projectId != self.projectId or source.analysisId != self.analysisId
                or source.status not in ('completed', 'completed_with_errors')
                or digest(source.model_dump(exclude_unset=True)) != self.sourceAnalysisHash):
            raise ValueError('Invalid analysis provenance')
        expected = {f.relativePath: f for f in source.files}
        if len({f.relativePath for f in self.files}) != len(self.files) or set(expected) != {f.relativePath for f in self.files}:
            raise ValueError('Lost/duplicate file outcomes')
        counts = Counter()
        metadata_ids = set()
        for file in self.files:
            original = expected[file.relativePath]
            if file.upstreamStatus != original.status or file.sourceArtifact != original.artifact:
                raise ValueError('Changed file provenance')
            if file.metadataArtifact:
                ref = file.metadataArtifact
                if ref.projectId != self.projectId or ref.analysisId != self.analysisId or ref.artifactId in metadata_ids:
                    raise ValueError('Invalid metadata ownership/identity')
                metadata_ids.add(ref.artifactId)
                if any(type(v) is not int or v < 0 for v in ref.entityCounts.values()):
                    raise ValueError('Invalid file counts')
                counts.update(ref.entityCounts)
        if dict(counts) != self.entityCounts or dict(Counter(f.status for f in self.files)) != self.outcomeCounts:
            raise ValueError('Invalid aggregate counts')
        if self.status != analysis_status(self.files, source.status):
            raise ValueError('Invalid aggregate status')
        return self
