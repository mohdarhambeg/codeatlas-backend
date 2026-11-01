
import ast
from pathlib import Path


IGNORED_DIRS = {
    ".git",
    ".venv",
    "venv",
    "__pycache__",
    "node_modules",
}


def extract_metadata(file_path: str) -> dict:
    """Extract imports, classes, functions, and methods from a Python file."""

    if not file_path or not file_path.strip():
        raise ValueError("File path cannot be empty.")

    path = Path(file_path).expanduser().resolve()

    if not path.exists() or not path.is_file():
        raise FileNotFoundError(f"File not found: {file_path}")

    if path.suffix.lower() != ".py":
        raise ValueError("Only Python files are supported.")

    source_code = path.read_text(encoding="utf-8")
    tree = ast.parse(source_code, filename=str(path))

    imports = []
    classes = []
    functions = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.append({
                    "name": alias.name,
                    "line": node.lineno,
                })

        elif isinstance(node, ast.ImportFrom):
            imports.append({
                "name": node.module or "",
                "line": node.lineno,
            })

        elif isinstance(node, ast.ClassDef):
            methods = []

            for child in node.body:
                if isinstance(
                    child,
                    (ast.FunctionDef, ast.AsyncFunctionDef),
                ):
                    methods.append({
                        "name": child.name,
                        "parameters": [
                            arg.arg for arg in child.args.args
                        ],
                        "line": child.lineno,
                    })

            classes.append({
                "name": node.name,
                "line": node.lineno,
                "methods": methods,
            })

        elif isinstance(
            node,
            (ast.FunctionDef, ast.AsyncFunctionDef),
        ):
            functions.append({
                "name": node.name,
                "parameters": [
                    arg.arg for arg in node.args.args
                ],
                "line": node.lineno,
            })

    # Identify methods by their source line and exclude them from functions.
    method_lines = {
        method["line"]
        for class_info in classes
        for method in class_info["methods"]
    }

    functions = [
        function
        for function in functions
        if function["line"] not in method_lines
    ]

    return {
        "file": str(path),
        "imports": imports,
        "classes": classes,
        "functions": functions,
    }


def scan_project(project_path: str) -> dict:
    """Scan a project folder and summarize its Python metadata."""

    if not project_path or not project_path.strip():
        raise ValueError("Project path cannot be empty.")

    root = Path(project_path).expanduser().resolve()

    if not root.exists():
        raise NotADirectoryError(
            f"Project folder does not exist: {project_path}"
        )

    if not root.is_dir():
        raise NotADirectoryError(
            f"Provided path is not a folder: {project_path}"
        )

    results = []
    errors = []

    for file_path in root.rglob("*.py"):
        # Ignore virtual environments, Git data, and cache directories.
        if any(part in IGNORED_DIRS for part in file_path.parts):
            continue

        try:
            results.append(extract_metadata(str(file_path)))

        except (
            SyntaxError,
            UnicodeDecodeError,
            OSError,
            ValueError,
        ) as error:
            errors.append({
                "file": str(file_path),
                "error": str(error),
            })

    total_imports = sum(
        len(item["imports"]) for item in results
    )

    total_classes = sum(
        len(item["classes"]) for item in results
    )

    total_functions = sum(
        len(item["functions"]) for item in results
    )

    total_methods = sum(
        len(class_info["methods"])
        for item in results
        for class_info in item["classes"]
    )

    return {
        "project_path": str(root),
        "files_scanned": len(results),
        "files_with_errors": len(errors),
        "summary": {
            "total_imports": total_imports,
            "total_classes": total_classes,
            "total_functions": total_functions,
            "total_methods": total_methods,
        },
        "results": results,
        "errors": errors,
    }
