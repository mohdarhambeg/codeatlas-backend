
def extract_metadata_from_ast(ast_document: dict) -> dict:
    """
    Extract metadata from JavaScript and Python AST documents.

    Supports:
    - JavaScript function declarations and arrow functions
    - Python functions and methods
    - Python classes
    - Python imports
    - Function and method parameters
    """

    nodes = ast_document.get("nodes", [])

    # Create a lookup table for AST nodes.
    node_map = {
        node["id"]: node
        for node in nodes
        if "id" in node
    }

    # Create a parent lookup table.
    parent_map = {}

    for parent in nodes:
        for child_id in parent.get("children", []):
            parent_map[child_id] = parent["id"]

    functions = []
    classes = []
    imports = []

    def get_field_child(node, field_name):
        """Find a direct child using its field name."""
        for child_id in node.get("children", []):
            child = node_map.get(child_id)

            if child and child.get("fieldName") == field_name:
                return child

        return None

    def get_parameters(node):
        """Extract simple identifier parameters."""
        parameters_node = get_field_child(node, "parameters")
        parameters = []

        if not parameters_node:
            return parameters

        for child_id in parameters_node.get("children", []):
            parameter = node_map.get(child_id)

            if (
                parameter
                and parameter.get("type") == "identifier"
                and parameter.get("named") is True
            ):
                parameters.append(parameter.get("text"))

        return parameters

    def get_enclosing_class(node_id):
        """Find the class containing a Python method."""
        parent_id = parent_map.get(node_id)

        while parent_id:
            parent = node_map.get(parent_id)

            if not parent:
                break

            if parent.get("type") == "class_definition":
                name_node = get_field_child(parent, "name")
                return name_node.get("text") if name_node else None

            parent_id = parent_map.get(parent_id)

        return None

    def get_descendant_identifiers(node):
        """Collect identifier names inside an import statement."""
        names = []

        def visit(current):
            if current.get("type") == "identifier":
                name = current.get("text")

                if name:
                    names.append(name)

            for child_id in current.get("children", []):
                child = node_map.get(child_id)

                if child:
                    visit(child)

        visit(node)
        return names

    # Extract imports.
    for node in nodes:
        if node.get("type") in (
            "import_statement",
            "import_from_statement",
        ):
            import_names = get_descendant_identifiers(node)

            imports.append({
                "names": import_names,
                "ast_node_id": node.get("id"),
            })

    # Extract Python classes.
    for node in nodes:
        if node.get("type") != "class_definition":
            continue

        name_node = get_field_child(node, "name")
        class_name = name_node.get("text") if name_node else None

        classes.append({
            "name": class_name,
            "ast_node_id": node.get("id"),
            "start": node.get("start"),
            "end": node.get("end"),
            "methods": [],
        })

    # Extract JavaScript and Python functions.
    for node in nodes:
        node_type = node.get("type")

        if node_type not in (
            "function_declaration",
            "arrow_function",
            "function_definition",
        ):
            continue

        function_name = None
        body_node_id = None
        parent_id = parent_map.get(node.get("id"))
        parent_node = node_map.get(parent_id)

        # JavaScript function declaration.
        if node_type == "function_declaration":
            name_node = get_field_child(node, "name")

            if name_node:
                function_name = name_node.get("text")

        # JavaScript arrow function.
        elif node_type == "arrow_function":
            if parent_node and parent_node.get("type") == "variable_declarator":
                name_node = get_field_child(parent_node, "name")

                if name_node:
                    function_name = name_node.get("text")

        # Python function or method.
        elif node_type == "function_definition":
            name_node = get_field_child(node, "name")

            if name_node:
                function_name = name_node.get("text")

        parameters = get_parameters(node)

        body_node = get_field_child(node, "body")

        if body_node:
            body_node_id = body_node.get("id")

        # Determine whether this Python function is a method.
        class_name = None

        if node_type == "function_definition":
            class_name = get_enclosing_class(node.get("id"))

        function_type = (
            "method" if class_name else
            "arrow" if node_type == "arrow_function" else
            "declaration" if node_type == "function_declaration" else
            "function"
        )

        function_data = {
            "name": function_name,
            "function_type": function_type,
            "class_name": class_name,
            "parameters": parameters,
            "parameter_count": len(parameters),
            "ast_node_id": node.get("id"),
            "body_node_id": body_node_id,
            "start": node.get("start"),
            "end": node.get("end"),
        }

        functions.append(function_data)

        # Add Python methods to their corresponding classes.
        if class_name:
            for class_data in classes:
                if class_data["name"] == class_name:
                    class_data["methods"].append(function_name)
                    break

    total_methods = sum(
        1 for function in functions
        if function["function_type"] == "method"
    )

    total_functions = len(functions) - total_methods

    return {
        "project_id": ast_document.get("projectId"),
        "analysis_id": ast_document.get("analysisId"),
        "file": ast_document.get("relativePath"),
        "language": ast_document.get("language"),
        "content_hash": ast_document.get("contentHash"),
        "summary": {
            "total_imports": len(imports),
            "total_classes": len(classes),
            "total_functions": total_functions,
            "total_methods": total_methods,
        },
        "imports": imports,
        "classes": classes,
        "functions": functions,
    }
