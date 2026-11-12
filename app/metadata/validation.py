"""Consumer boundary for CodeAtlas AstDocument v1 (verified against TS model.ts)."""
from datetime import datetime
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

COORDINATES = 'utf16-code-units; zero-based rows/columns; end-exclusive'
NORMALIZATION = 'syntax-preorder-v1'


class ASTValidationError(ValueError):
    """Malformed/incompatible upstream data; never include upstream values in HTTP errors."""


class UnsupportedLanguageError(ValueError):
    pass


class Contract(BaseModel):
    model_config = ConfigDict(strict=True, extra='forbid')


class Point(Contract):
    offset: int = Field(ge=0, le=2**53 - 1)
    row: int = Field(ge=0, le=2**53 - 1)
    column: int = Field(ge=0, le=2**53 - 1)

    @model_validator(mode='after')
    def bounds(self):
        if max(self.row, self.column) > self.offset:
            raise ValueError('Invalid point bounds')
        return self


def ordered(a: Point, b: Point) -> bool:
    return (
        a.offset <= b.offset and (a.row, a.column) <= (b.row, b.column)
        and ((a.offset == b.offset) == ((a.row, a.column) == (b.row, b.column)))
        and (b.column - a.column == b.offset - a.offset if a.row == b.row
             else b.row - a.row + b.column <= b.offset - a.offset)
    )


class ASTNode(Contract):
    id: str = Field(pattern=r'^n\d+$')
    type: str = Field(min_length=1, max_length=16384)
    parentId: str | None
    fieldName: str | None
    children: list[str] = Field(max_length=100000)
    named: bool
    error: bool
    missing: bool
    start: Point
    end: Point
    text: str | None = None

    @model_validator(mode='after')
    def shape(self):
        if not ordered(self.start, self.end):
            raise ValueError('Invalid node range')
        if len(set(self.children)) != len(self.children):
            raise ValueError('Repeated child')
        if 'text' in self.model_fields_set:
            if self.text is None or self.children:
                raise ValueError('Text is only allowed on leaves')
            units = len(self.text.encode('utf-16-le', errors='surrogatepass')) // 2
            lines = self.text.split('\n')
            column = (self.start.column + units if len(lines) == 1 else
                      len(lines[-1].encode('utf-16-le', errors='surrogatepass')) // 2)
            if (units != self.end.offset - self.start.offset or
                    self.end.row != self.start.row + len(lines) - 1 or self.end.column != column):
                raise ValueError('Leaf text/range mismatch')
        return self


class ASTDiagnostic(Contract):
    nodeId: str
    code: Literal['ERROR_NODE', 'MISSING_NODE']
    start: Point
    end: Point


class AstDocument(Contract):
    schemaVersion: Literal[1]
    normalizationVersion: str | None = None
    projectId: str = Field(pattern=r'^PRJ-[A-Fa-f0-9]{6}$')
    analysisId: str = Field(pattern=r'^AN-[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$')
    relativePath: str = Field(min_length=1, max_length=16384)
    language: str = Field(min_length=1, max_length=16384)
    parserId: str = Field(min_length=1, max_length=16384)
    parserVersion: str | None
    grammarVersion: str | None
    contentHash: str = Field(pattern=r'^[a-f0-9]{64}$')
    coordinates: Literal['utf16-code-units; zero-based rows/columns; end-exclusive']
    rootId: str
    nodes: list[ASTNode] = Field(min_length=1, max_length=100000)
    parseStatus: Literal['success', 'syntax_errors']
    diagnostics: list[ASTDiagnostic] = Field(max_length=100000)
    createdAt: str

    @model_validator(mode='before')
    @classmethod
    def strict_version(cls, value):
        if isinstance(value, dict) and type(value.get('schemaVersion')) is not int:
            raise ValueError('Schema version must be an integer')
        return value

    @model_validator(mode='after')
    def relationships(self):
        path = self.relativePath
        if ('\0' in path or '\\' in path or path.startswith('/') or re.match(r'^[A-Za-z]:', path)
                or any(p in ('', '.', '..') for p in path.split('/'))):
            raise ValueError('Invalid relative path')
        if ('normalizationVersion' in self.model_fields_set and self.normalizationVersion != NORMALIZATION):
            raise ValueError('Unsupported normalization policy')
        if self.language.strip() != self.language or re.search(r'\s', self.parserId):
            raise ValueError('Invalid language/parser identifier')
        if not re.fullmatch(r'\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z', self.createdAt):
            raise ValueError('Invalid timestamp')
        datetime.fromisoformat(self.createdAt.replace('Z', '+00:00'))
        by_id = {n.id: n for n in self.nodes}
        if len(by_id) != len(self.nodes):
            raise ValueError('Duplicate node IDs')
        root = by_id.get(self.rootId)
        if root is None or root.parentId is not None or root.fieldName is not None:
            raise ValueError('Invalid root')
        seen = set()
        stack = [(root, 0)]
        while stack:
            node, depth = stack.pop()
            if node.id in seen or depth > 512:
                raise ValueError('Cycle or depth limit')
            seen.add(node.id)
            previous = node.start
            for child_id in node.children:
                child = by_id.get(child_id)
                if (child is None or child.parentId != node.id or
                        not ordered(node.start, child.start) or not ordered(child.end, node.end)
                        or not ordered(previous, child.start)):
                    raise ValueError('Invalid child reference/range/order')
                previous = child.end
                stack.append((child, depth + 1))
        if len(seen) != len(by_id):
            raise ValueError('Unreachable nodes')
        covered = set()
        for diagnostic in self.diagnostics:
            node = by_id.get(diagnostic.nodeId)
            if (node is None or node.id in covered or diagnostic.start != node.start or diagnostic.end != node.end
                    or diagnostic.code != ('MISSING_NODE' if node.missing else 'ERROR_NODE' if node.error else None)):
                raise ValueError('Invalid diagnostic')
            covered.add(node.id)
        errors = {n.id for n in self.nodes if n.error or n.missing}
        if covered != errors or (self.parseStatus == 'success' and errors):
            raise ValueError('Invalid parse status/diagnostic coverage')
        return self


def validate_ast(value: object) -> AstDocument:
    """Validate even for service callers that do not pass through FastAPI."""
    try:
        # Revalidate models too: callers may have mutated their nested lists.
        payload = value.model_dump(exclude_unset=True) if isinstance(value, AstDocument) else value
        return AstDocument.model_validate(payload)
    except (ValidationError, ValueError, TypeError) as error:
        raise ASTValidationError('Invalid or incompatible AST document') from error


def select_language(document: AstDocument) -> str:
    # These exact labels are verified in the existing parser, including .jsx files.
    if document.language != 'JavaScript':
        raise UnsupportedLanguageError('No verified adapter for this language')
    if (document.parserId != 'tree-sitter-javascript' or
            document.parserVersion != '0.25.1' or document.grammarVersion != '0.25.0'):
        raise ASTValidationError('Unsupported parser provenance')
    return 'javascript'
