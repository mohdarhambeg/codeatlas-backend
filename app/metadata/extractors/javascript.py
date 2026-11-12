"""Tree-sitter JavaScript 0.25.0 syntax adapter, including JSX source files.

No reference resolution. Ownership means AST containment, not lexical scope resolution.
"""
from collections import Counter

from app.metadata.models import (ASTProvenance, Entity, ExtractionDiagnostic,
                                 MetadataDocument, SourceRange, deterministic_id)
from app.metadata.validation import AstDocument

FUNCTIONS = {'function_declaration', 'function_expression', 'generator_function',
             'generator_function_declaration', 'arrow_function'}
PATTERNS = {'identifier', 'shorthand_property_identifier_pattern', 'array_pattern',
            'object_pattern', 'assignment_pattern', 'object_assignment_pattern', 'rest_pattern', 'pair_pattern'}


class JavaScriptExtractor:
    def __init__(self, document: AstDocument):
        self.doc = document
        self.nodes = {n.id: n for n in document.nodes}
        self.entities = []
        self.diagnostics = []
        self.owners = {}
        self.entity_map = {}
        self.fields = {n.id: {self.nodes[c].fieldName: self.nodes[c] for c in n.children
                              if self.nodes[c].fieldName} for n in document.nodes}

    def children(self, node):
        return [self.nodes[c] for c in node.children]

    def field(self, node, name):
        return self.fields[node.id].get(name)

    def syntax(self, node):
        """Lossless leaf tokens/ranges; gaps are not guessed without source text."""
        if node is None:
            return None
        tokens = []
        stack = [node]
        while stack:
            current = stack.pop()
            if current.text is not None:
                tokens.append({'text': current.text, 'astNodeId': current.id})
            stack.extend(reversed(self.children(current)))
        return {'astNodeId': node.id, 'type': node.type, 'tokens': tokens}

    def bindings(self, node):
        """Only binding-position identifiers; never property keys/default expressions."""
        if node is None:
            return []
        found = []
        stack = [node]
        while stack:
            current = stack.pop()
            if current.type in ('identifier', 'shorthand_property_identifier_pattern'):
                if current.text and not current.error and not current.missing:
                    found.append(current)
            elif current.type in ('assignment_pattern', 'object_assignment_pattern'):
                left = self.field(current, 'left')
                if left:
                    stack.append(left)
            elif current.type == 'pair_pattern':
                value = self.field(current, 'value')
                if value:
                    stack.append(value)
            elif current.type in ('array_pattern', 'object_pattern', 'rest_pattern'):
                stack.extend(reversed([c for c in self.children(current) if c.named]))
        return found

    def add(self, node, kind, name, parent, *, attributes=None, properties=None, role=''):
        entity_id = deterministic_id(self.doc, node.id, kind, role)
        owner = self.entity_map.get(parent)
        qualified = None
        # These are syntactic display paths, never globally resolved symbols.
        if name is not None and owner and owner.qualifiedName:
            qualified = owner.qualifiedName + '.' + name
        elif name is not None and (owner is None or owner.kind == 'module'):
            qualified = name
        entity = Entity(
            entityId=entity_id, kind=kind, name=name, qualifiedName=qualified,
            language=self.doc.language, projectId=self.doc.projectId,
            analysisId=self.doc.analysisId, relativePath=self.doc.relativePath,
            contentHash=self.doc.contentHash, astNodeId=node.id, parentEntityId=parent,
            sourceRange=SourceRange(start=node.start, end=node.end),
            attributes=attributes or {}, properties=properties or {},
        )
        self.entities.append(entity)
        self.entity_map[entity_id] = entity
        self.owners[node.id] = entity_id
        return entity_id

    def diagnostic(self, node, code, message):
        self.diagnostics.append(ExtractionDiagnostic(code=code, message=message,
            astNodeId=node.id, sourceRange=SourceRange(start=node.start, end=node.end)))

    def name(self, node):
        return node.text if node and node.type in ('identifier', 'property_identifier',
                'private_property_identifier', 'shorthand_property_identifier_pattern') else None

    def parameters(self, node, owner):
        params = self.field(node, 'parameters')
        single = self.field(node, 'parameter')
        candidates = [c for c in self.children(params) if c.named and c.type != 'comment'] if params else ([single] if single else [])
        for index, parameter in enumerate(candidates):
            bindings = self.bindings(parameter)
            simple = parameter.type == 'identifier'
            left = self.field(parameter, 'left')
            name = self.name(parameter) if simple else self.name(left) if left else None
            self.add(parameter, 'parameter', name, owner, properties={
                'index': index, 'pattern': self.syntax(parameter),
                'bindings': [{'name': b.text, 'astNodeId': b.id} for b in bindings],
                'rest': parameter.type == 'rest_pattern',
                'default': self.syntax(self.field(parameter, 'right')),
                'destructured': parameter.type in ('array_pattern', 'object_pattern') or
                    (left is not None and left.type in ('array_pattern', 'object_pattern')),
            })
            if parameter.error or parameter.missing or parameter.type not in PATTERNS:
                self.diagnostic(parameter, 'INCOMPLETE_PARAMETER', 'Parameter structure is not reliably supported.')

    def extract_node(self, node, parent):
        if node.error or node.missing:
            self.diagnostic(node, 'UNRELIABLE_NODE', 'An erroneous or missing syntax node was skipped.')
            return parent, True
        children = self.children(node)
        tokens = {c.type for c in children if not c.named}
        name_node = self.field(node, 'name')
        name = self.name(name_node)
        properties = {}
        attributes = {}
        kind = None
        if node.id == self.doc.rootId:
            kind, name = 'module', self.doc.relativePath
        elif node.type in ('class_declaration', 'class') and node.named:
            kind = 'class'
            properties = {'expression': node.type == 'class', 'anonymous': name is None,
                          'heritage': [self.syntax(c) for c in children if c.type == 'class_heritage']}
        elif node.type in FUNCTIONS:
            kind = 'function'
            attributes = {'async': 'async' in tokens, 'generator': '*' in tokens}
            properties = {'syntaxKind': node.type, 'anonymous': name is None,
                          'bodyAstNodeId': self.field(node, 'body').id if self.field(node, 'body') else None}
        elif node.type == 'method_definition':
            # Object literal methods are methods too, but never constructors of a class.
            direct_parent = self.nodes.get(node.parentId)
            class_member = direct_parent is not None and direct_parent.type == 'class_body'
            kind = ('constructor' if class_member and name == 'constructor' and
                    'static' not in tokens and 'get' not in tokens and 'set' not in tokens else 'method')
            attributes = {'async': 'async' in tokens, 'generator': '*' in tokens, 'static': 'static' in tokens,
                          'getter': 'get' in tokens, 'setter': 'set' in tokens,
                          'private': name_node is not None and name_node.type == 'private_property_identifier',
                          'computed': name_node is not None and name_node.type == 'computed_property_name'}
            properties = {'nameSyntax': self.syntax(name_node), 'classMember': class_member,
                          'bodyAstNodeId': self.field(node, 'body').id if self.field(node, 'body') else None}
        elif node.type == 'field_definition':
            name_node = self.field(node, 'property')
            name = self.name(name_node)
            kind = 'field'
            attributes = {'static': 'static' in tokens,
                          'private': name_node is not None and name_node.type == 'private_property_identifier',
                          'computed': name_node is not None and name_node.type == 'computed_property_name'}
            properties = {'nameSyntax': self.syntax(name_node)}
        elif node.type in ('lexical_declaration', 'variable_declaration'):
            kind, name = 'variable_declaration', None
            declaration = self.field(node, 'kind')
            properties = {'declarationKind': declaration.text if declaration else 'var'}
        elif node.type == 'variable_declarator':
            bindings = self.bindings(name_node)
            pattern = self.syntax(name_node)
            # One entity per declared binding. An initializer is owned by its sole
            # binding when unambiguous, otherwise by the declaration container.
            ids = [self.add(node if len(bindings) == 1 else binding, 'variable', binding.text, parent,
                           properties={'pattern': pattern, 'declaratorAstNodeId': node.id,
                                       'bindingAstNodeId': binding.id}) for binding in bindings]
            if not ids:
                self.diagnostic(node, 'UNKNOWN_BINDING', 'No reliable variable bindings were identified.')
            if len(ids) == 1:
                self.owners[node.id] = ids[0]
                return ids[0], False
            return parent, False
        elif node.type == 'import_statement':
            kind = 'import'
            properties = {'moduleSpecifier': self.syntax(self.field(node, 'source')),
                          'sideEffectOnly': not any(c.type == 'import_clause' for c in children)}
        elif node.type == 'import_specifier':
            kind = 'import_specifier'
            alias = self.field(node, 'alias')
            properties = {'importedName': name, 'importKind': 'named'}
            name = self.name(alias) if alias else name
        elif node.type == 'namespace_import':
            kind = 'import_specifier'
            name = next((c.text for c in children if c.type == 'identifier'), None)
            properties = {'importedName': '*', 'importKind': 'namespace'}
        elif node.type == 'identifier' and node.parentId and self.nodes[node.parentId].type == 'import_clause':
            kind, name = 'import_specifier', node.text
            properties = {'importedName': 'default', 'importKind': 'default'}
        elif node.type == 'export_statement':
            kind = 'export'
            properties = {'default': 'default' in tokens, 'star': '*' in tokens,
                          'moduleSpecifier': self.syntax(self.field(node, 'source')),
                          'declarationAstNodeId': self.field(node, 'declaration').id if self.field(node, 'declaration') else None,
                          'valueSyntax': self.syntax(self.field(node, 'value'))}
        elif node.type == 'namespace_export':
            kind = 'export_specifier'
            name = next((c.text for c in children if c.type == 'identifier'), None)
            properties = {'localOrImportedName': '*', 'exportedName': name, 'namespace': True}
        elif node.type == 'export_specifier':
            kind = 'export_specifier'
            alias = self.field(node, 'alias')
            properties = {'localOrImportedName': name, 'exportedName': self.name(alias) if alias else name}
            name = properties['exportedName']
        elif node.type == 'comment':
            kind = 'documentation' if (node.text or '').startswith('/**') else 'comment'
            properties = {'text': node.text, 'attachment': None}
        if kind:
            entity_id = self.add(node, kind, name, parent, attributes=attributes, properties=properties)
            if kind in ('function', 'method', 'constructor'):
                self.parameters(node, entity_id)
                if not self.field(node, 'body'):
                    self.diagnostic(node, 'MISSING_BODY', 'Callable body could not be identified.')
            if node.type in ('class_declaration', 'function_declaration', 'generator_function_declaration') and name is None:
                self.diagnostic(node, 'INCOMPLETE_NAME', 'A required declaration name could not be identified.')
            if name_node and (name_node.error or name_node.missing):
                self.diagnostic(node, 'INCOMPLETE_NAME', 'Declaration name is unreliable.')
            return entity_id, False
        return parent, False

    def extract(self):
        # Iterative traversal: linear node visits, ownership follows containment.
        stack = [(self.nodes[self.doc.rootId], None)]
        while stack:
            node, parent = stack.pop()
            owner, skip = self.extract_node(node, parent)
            if not skip:
                # Parameter ownership was already assigned before traversing bodies.
                stack.extend((c, self.owners.get(c.id, owner)) for c in reversed(self.children(node)))
        if self.doc.parseStatus == 'syntax_errors':
            self.diagnostics.insert(0, ExtractionDiagnostic(code='SOURCE_SYNTAX_ERRORS',
                message='The source AST contains syntax errors; extraction is partial.'))
        counts = Counter(e.kind for e in self.entities)
        provenance = {k: getattr(self.doc, k) for k in ASTProvenance.model_fields}
        return MetadataDocument(sourceAst=ASTProvenance(**provenance),
            extractionStatus='partial' if self.diagnostics else 'complete',
            entities=self.entities, diagnostics=self.diagnostics, summary=dict(sorted(counts.items())))
