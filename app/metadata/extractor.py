"""Compatibility projection of validated metadata into the legacy remote response."""


def legacy_metadata(document) -> dict:
    entities = document.entities
    classes = [e for e in entities if e.kind == 'class']
    class_map = {e.entityId: e for e in classes}
    entity_map = {e.entityId: e for e in entities}
    members, imported_names = {}, {}
    for entity in entities:
        if entity.kind in ('method', 'constructor'):
            members.setdefault(entity.parentEntityId, []).append(entity.name)
        elif entity.kind == 'import_specifier':
            imported_names.setdefault(entity.parentEntityId, []).append(entity.name)
    parameters = {}
    for entity in entities:
        if entity.kind == 'parameter':
            parameters.setdefault(entity.parentEntityId, []).append(entity.name)
    functions = []
    for entity in entities:
        if entity.kind not in ('function', 'method', 'constructor'):
            continue
        owner = class_map.get(entity.parentEntityId)
        binding = entity_map.get(entity.parentEntityId)
        legacy_name = entity.name
        if entity.properties.get('syntaxKind') == 'arrow_function' and binding and binding.kind == 'variable':
            legacy_name = binding.name
        functions.append({
            'name': legacy_name,
            'function_type': 'method' if entity.kind in ('method', 'constructor') else
                'arrow' if entity.properties.get('syntaxKind') == 'arrow_function' else
                'declaration' if entity.properties.get('syntaxKind') in ('function_declaration', 'generator_function_declaration') else 'function',
            'class_name': owner.name if owner else None,
            'parameters': parameters.get(entity.entityId, []),
            'parameter_count': len(parameters.get(entity.entityId, [])),
            'ast_node_id': entity.astNodeId,
            'body_node_id': entity.properties.get('bodyAstNodeId'),
            'start': entity.sourceRange.start.model_dump(), 'end': entity.sourceRange.end.model_dump(),
        })
    method_count = sum(e.kind in ('method', 'constructor') for e in entities)
    imports = [e for e in entities if e.kind == 'import']
    source = document.sourceAst
    return {
        'project_id': source.projectId, 'analysis_id': source.analysisId,
        'file': source.relativePath, 'language': source.language, 'content_hash': source.contentHash,
        'summary': {'total_imports': len(imports), 'total_classes': len(classes),
                    'total_functions': len(functions) - method_count, 'total_methods': method_count},
        'imports': [{'names': imported_names.get(e.entityId, []),
                     'ast_node_id': e.astNodeId} for e in imports],
        'classes': [{'name': e.name, 'ast_node_id': e.astNodeId,
                     'start': e.sourceRange.start.model_dump(), 'end': e.sourceRange.end.model_dump(),
                     'methods': members.get(e.entityId, [])}
                    for e in classes],
        'functions': functions,
        # Additive fields prevent partial output from appearing complete to consumers.
        'extraction_status': document.extractionStatus,
        'diagnostics': [d.model_dump() for d in document.diagnostics],
    }


def extract_metadata_from_ast(ast_document: dict) -> dict:
    """Existing public signature; remote Python has no verified producer/adapter."""
    from app.metadata.service import extract_ast_metadata
    return legacy_metadata(extract_ast_metadata(ast_document))
