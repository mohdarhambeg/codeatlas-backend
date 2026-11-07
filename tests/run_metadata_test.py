
import sys
from pathlib import Path

# Find the main CodeAtlas project folder.
project_root = Path(__file__).resolve().parent.parent

# Add the project folder to Python's import search path.
sys.path.insert(0, str(project_root))

from app.metadata.service import extract_metadata

sample_file = project_root / "tests" / "sample_code.py"

try:
    result = extract_metadata(str(sample_file))

    print("\n--- Metadata Extraction Result ---")
    print(result)

except Exception as error:
    print(f"Error: {error}")
