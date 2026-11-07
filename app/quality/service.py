
import ast
from pathlib import Path


IGNORED_DIRS = {
    ".git",
    ".venv",
    "venv",
    "__pycache__",
    "node_modules",
}

MAX_FUNCTION_LINES = 20


def analyze_project(project_path: str) -> dict:
    """Analyze basic software quality metrics of a Python project."""

    if not project_path or not project_path.strip():
        raise ValueError("Project path cannot be empty.")

    root = Path(project_path).expanduser().resolve()

    if not root.exists() or not root.is_dir():
        raise NotADirectoryError(
            f"Project folder does not exist: {project_path}"
        )

    total_files = 0
    total_lines = 0
    total_functions = 0
    total_classes = 0
    long_functions = 0
    issues = []

    for file_path in root.rglob("*.py"):

        if any(part in IGNORED_DIRS for part in file_path.parts):
            continue

        try:
            source_code = file_path.read_text(encoding="utf-8")
            tree = ast.parse(source_code, filename=str(file_path))

            total_files += 1
            total_lines += len(source_code.splitlines())

            for node in ast.walk(tree):

                if isinstance(node, ast.ClassDef):
                    total_classes += 1

                elif isinstance(
                    node,
                    (ast.FunctionDef, ast.AsyncFunctionDef),
                ):
                    total_functions += 1

                    function_lines = (
                        node.end_lineno - node.lineno + 1
                    )

                    if function_lines > MAX_FUNCTION_LINES:
                        long_functions += 1
                        issues.append({
                            "file": str(file_path),
                            "issue": (
                                f"Long function '{node.name}' "
                                f"contains {function_lines} lines."
                            ),
                            "line": node.lineno,
                        })

                    if len(node.args.args) > 5:
                        issues.append({
                            "file": str(file_path),
                            "issue": (
                                f"Function '{node.name}' has "
                                "more than 5 parameters."
                            ),
                            "line": node.lineno,
                        })

        except SyntaxError as error:
            issues.append({
                "file": str(file_path),
                "issue": f"Syntax error: {error.msg}",
                "line": error.lineno,
            })

        except (OSError, UnicodeDecodeError) as error:
            issues.append({
                "file": str(file_path),
                "issue": f"Could not read file: {error}",
                "line": None,
            })

    return {
        "project_path": str(root),
        "summary": {
            "total_files": total_files,
            "total_lines": total_lines,
            "total_functions": total_functions,
            "total_classes": total_classes,
            "long_functions": long_functions,
        },
        "issues": issues,
    }
