import copy
import json
from pathlib import Path

import pytest
from app.metadata.extractor import extract_metadata_from_ast
from app.metadata.service import extract_ast_metadata
from app.metadata.validation import ASTValidationError, UnsupportedLanguageError, validate_ast

FIXTURES = Path(__file__).parent / 'fixtures' / 'metadata'


@pytest.fixture
def ast_document():
    return json.loads((FIXTURES / 'coverage.jsx.ast.json').read_text())


@pytest.mark.parametrize('filename', ['coverage.jsx', 'errors.js', 'empty.js'])
def test_verified_documents(filename):
    raw = json.loads((FIXTURES / (filename + '.ast.json')).read_text())
    document = validate_ast(raw)
    source = (FIXTURES / filename).read_text().encode('utf-16-le')
    for node in document.nodes:
        if node.text is not None:
            assert source[node.start.offset * 2: node.end.offset * 2].decode('utf-16-le') == node.text
    metadata = extract_ast_metadata(raw)
    assert metadata.sourceAst.contentHash == raw['contentHash']
    assert metadata.extractionStatus == ('partial' if filename == 'errors.js' else 'complete')


@pytest.mark.parametrize('field', ['projectId', 'nodes', 'rootId', 'coordinates', 'parserVersion',
                                  'grammarVersion', 'parseStatus', 'diagnostics', 'createdAt'])
def test_missing_required_field(ast_document, field):
    del ast_document[field]
    with pytest.raises(ASTValidationError):
        extract_ast_metadata(ast_document)


@pytest.mark.parametrize('mutation', [
    lambda d: d['nodes'].append(copy.deepcopy(d['nodes'][0])),
    lambda d: d['nodes'][0]['children'].append('n999999'),
    lambda d: d['nodes'][1].update(parentId='n999999'),
    lambda d: d['nodes'][1]['start'].update(offset=-1),
    lambda d: d['nodes'][1]['start'].update(column=999999),
    lambda d: d['nodes'][1]['end'].update(offset=0),
    lambda d: d.update(schemaVersion=2),
    lambda d: d.update(schemaVersion=True),
    lambda d: d.update(contentHash='bad'),
    lambda d: d.update(normalizationVersion='unknown'),
    lambda d: d.update(parserId='unknown'),
    lambda d: d.update(parserVersion='999'),
    lambda d: d.update(grammarVersion=None),
    lambda d: d.update(relativePath='../escape.js'),
    lambda d: d['nodes'][0].update(parentId='n0'),
    lambda d: d['nodes'][1].update(text=None),
    lambda d: d['nodes'][1].update(error=True),
    lambda d: d.update(diagnostics=[{'nodeId':'n0', 'code':'ERROR_NODE', 'start':d['nodes'][0]['start'], 'end':d['nodes'][0]['end']}]),
    lambda d: d.update(nodes=[]),
])
def test_invalid_contract(ast_document, mutation):
    mutation(ast_document)
    with pytest.raises(ASTValidationError):
        extract_ast_metadata(ast_document)


def test_unknown_language(ast_document):
    ast_document['language'] = 'Python'
    with pytest.raises(UnsupportedLanguageError):
        extract_ast_metadata(ast_document)


def test_declarations_and_ownership(ast_document):
    metadata = extract_ast_metadata(ast_document)
    entities = metadata.entities
    by_id = {e.entityId: e for e in entities}
    assert len(by_id) == len(entities)
    assert all(e.entityId != e.astNodeId for e in entities)
    assert [e.entityId for e in extract_ast_metadata(ast_document).entities] == list(by_id)
    for e in entities:
        assert e.projectId == ast_document['projectId']
        assert e.analysisId == ast_document['analysisId']
        assert e.relativePath == ast_document['relativePath']
        assert e.contentHash == ast_document['contentHash']
        if e.kind != 'module':
            assert e.parentEntityId in by_id
    cls = next(e for e in entities if e.kind == 'class' and e.name == 'Example')
    members = [e for e in entities if e.parentEntityId == cls.entityId]
    assert any(e.kind == 'constructor' for e in members)
    assert any(e.kind == 'field' and e.name == '#secret' and e.attributes['private'] for e in members)
    assert any(e.kind == 'field' and e.attributes['static'] for e in members)
    assert any(e.kind == 'method' and e.attributes['getter'] for e in members)
    assert any(e.kind == 'method' and e.attributes['setter'] for e in members)
    assert any(e.kind == 'method' and e.attributes['generator'] and e.attributes['static'] for e in members)
    assert any(e.kind == 'method' and e.name == '#privateMethod' for e in members)
    assert any(e.kind == 'method' and e.name is None and e.attributes['computed'] for e in members)
    run = next(e for e in members if e.name == 'run')
    assert run.attributes['async']
    nested = next(e for e in entities if e.name == 'nestedFunction')
    assert nested.parentEntityId == run.entityId and nested.kind == 'function'
    params = [e for e in entities if e.parentEntityId == run.entityId and e.kind == 'parameter']
    assert len(params) == 4
    assert params[0].properties['destructured']
    assert params[0].properties['bindings'][0]['name'] == 'name'
    assert params[2].name == 'flag' and params[2].properties['default']
    assert params[3].properties['rest']
    assert all(e.parentEntityId in by_id for e in entities if e.kind == 'parameter')
    # One callable per AST syntax node; methods never become standalone functions.
    callables = [e for e in entities if e.kind in ('function', 'method', 'constructor')]
    assert len({e.astNodeId for e in callables}) == len(callables)
    classes = [e for e in entities if e.kind == 'class']
    assert len(classes) == 5 and any(e.name is None for e in classes)
    assert any(e.kind == 'function' and e.name == 'inner' for e in entities)
    assert any(e.kind == 'function' and e.name is None and e.properties['anonymous'] for e in entities)
    assert any(e.kind == 'function' and e.attributes['generator'] for e in entities)
    arrow = next(e for e in entities if e.kind == 'function' and by_id[e.parentEntityId].name == 'arrow')
    assert arrow.name is None and arrow.attributes['async']
    assert next(e for e in entities if e.kind == 'parameter' and e.parentEntityId == arrow.entityId).name == 'x'


def test_bindings_imports_exports_comments(ast_document):
    entities = extract_ast_metadata(ast_document).entities
    variables = {e.name for e in entities if e.kind == 'variable'}
    assert {'first', 'others', 'alias', 'shorthand', 'deep', 'fallback'} <= variables
    assert not {'key', 'nested', 'input', 'values'} & variables
    specifiers = {e.name: e for e in entities if e.kind == 'import_specifier'}
    assert specifiers['primary'].properties['importedName'] == 'default'
    assert specifiers['read'].properties['importedName'] == 'readFile'
    assert specifiers['helpers'].properties['importKind'] == 'namespace'
    assert len([e for e in entities if e.kind == 'import']) == 3
    exports = [e for e in entities if e.kind == 'export']
    assert any(e.properties['star'] and e.properties['moduleSpecifier'] for e in exports)
    assert any(e.properties['default'] for e in exports)
    assert any(e.kind == 'export_specifier' and e.name == 'renamed' for e in entities)
    assert any(e.kind == 'documentation' for e in entities)
    assert any(e.kind == 'comment' for e in entities)
    assert not any('resolved' in k.lower() for e in entities for k in e.properties)


def test_partial_empty_and_legacy():
    broken = json.loads((FIXTURES / 'errors.js.ast.json').read_text())
    output = extract_ast_metadata(broken)
    assert output.extractionStatus == 'partial'
    assert any(e.name == 'reliable' for e in output.entities)
    assert output.diagnostics
    empty = extract_ast_metadata(json.loads((FIXTURES / 'empty.js.ast.json').read_text()))
    assert empty.summary == {'module': 1}
    legacy = extract_metadata_from_ast(broken)
    assert legacy['extraction_status'] == 'partial'
    assert {'project_id', 'analysis_id', 'file', 'language', 'content_hash', 'summary', 'imports', 'classes', 'functions'} <= legacy.keys()


def test_revision_changes_ids(ast_document):
    previous = extract_ast_metadata(ast_document)
    ast_document['contentHash'] = 'a' * 64
    changed = extract_ast_metadata(ast_document)
    assert {e.entityId for e in previous.entities}.isdisjoint(e.entityId for e in changed.entities)


def test_entity_ownership_has_ast_containment(ast_document):
    document = extract_ast_metadata(ast_document)
    entities = {e.entityId: e for e in document.entities}
    nodes = {n['id']: n for n in ast_document['nodes']}
    for entity in document.entities:
        if entity.parentEntityId is None:
            continue
        owner = entities[entity.parentEntityId]
        node = nodes[entity.astNodeId]
        ancestors = set()
        while node['parentId'] is not None:
            ancestors.add(node['parentId'])
            node = nodes[node['parentId']]
        assert owner.astNodeId in ancestors, (entity.kind, entity.name, owner.kind)


def test_repeated_class_names_are_owned_by_identity(ast_document):
    entities = extract_ast_metadata(ast_document).entities
    classes = [e for e in entities if e.kind == 'class' and e.name == 'Same']
    assert len(classes) == 2
    assert classes[0].entityId != classes[1].entityId
    assert classes[0].parentEntityId != classes[1].parentEntityId
    for cls in classes:
        assert len([e for e in entities if e.kind == 'method' and e.parentEntityId == cls.entityId]) == 1
    assert {e.properties['declarationKind'] for e in entities if e.kind == 'variable_declaration'} == {'var', 'let', 'const'}
    assert any(e.kind == 'export_specifier' and e.name == 'namespace' and e.properties['namespace'] for e in entities)
    function = next(e for e in entities if e.kind == 'function' and e.name == 'patterns')
    parameter = next(e for e in entities if e.kind == 'parameter' and e.parentEntityId == function.entityId)
    assert parameter.properties['destructured'] and parameter.properties['default']
    assert [b['name'] for b in parameter.properties['bindings']] == ['local']
