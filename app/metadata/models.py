"""Version 1 syntax metadata. IDs identify extraction snapshots, not semantic symbols."""
from hashlib import sha256
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from app.metadata.validation import AstDocument, Point


class Model(BaseModel):
    model_config = ConfigDict(extra='forbid')


class SourceRange(Model):
    start: Point
    end: Point


class Entity(Model):
    entityId: str
    kind: Literal['module', 'class', 'function', 'method', 'constructor', 'field',
                  'variable_declaration', 'variable', 'parameter', 'import', 'import_specifier',
                  'export', 'export_specifier', 'comment', 'documentation']
    name: str | None = None
    qualifiedName: str | None = None
    language: str
    projectId: str
    analysisId: str
    relativePath: str
    contentHash: str
    astNodeId: str
    parentEntityId: str | None
    sourceRange: SourceRange
    attributes: dict = Field(default_factory=dict)
    properties: dict = Field(default_factory=dict)


class ExtractionDiagnostic(Model):
    code: str
    message: str
    astNodeId: str | None = None
    sourceRange: SourceRange | None = None


class ASTProvenance(Model):
    schemaVersion: int
    normalizationVersion: str | None
    projectId: str
    analysisId: str
    relativePath: str
    language: str
    parserId: str
    parserVersion: str | None
    grammarVersion: str | None
    contentHash: str
    coordinates: str
    rootId: str
    parseStatus: str
    createdAt: str


class MetadataDocument(Model):
    schemaVersion: Literal[1] = 1
    extractorVersion: Literal['javascript-syntax-v1'] = 'javascript-syntax-v1'
    sourceAst: ASTProvenance
    extractionStatus: Literal['complete', 'partial']
    entities: list[Entity]
    diagnostics: list[ExtractionDiagnostic]
    summary: dict[str, int]


def deterministic_id(document: AstDocument, node_id: str, kind: str, role: str = '') -> str:
    identity = [document.schemaVersion, document.normalizationVersion, document.projectId,
                document.analysisId, document.relativePath, document.contentHash, document.language,
                document.parserId, document.parserVersion, document.grammarVersion,
                'javascript-syntax-v1', node_id, kind, role]
    return 'meta-' + sha256(json.dumps(identity, ensure_ascii=True, separators=(',', ':')).encode()).hexdigest()
