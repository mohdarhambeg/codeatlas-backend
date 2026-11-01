
from fastapi import APIRouter, HTTPException

from app.metadata.schemas import (
    MetadataRequest,
    ProjectScanRequest,
    ProjectMetadataResponse,
)

from app.metadata.service import extract_metadata, scan_project


router = APIRouter(
    prefix="/api/v1/metadata",
    tags=["Metadata"],
)


@router.post("/extract")
def extract_metadata_endpoint(request: MetadataRequest):
    """Extract metadata from a single Python file."""

    try:
        result = extract_metadata(request.file_path)

        return {
            "status": "success",
            "metadata": result,
        }

    except FileNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error))

    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error))

    except SyntaxError as error:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid Python syntax: {error}",
        )

    except UnicodeDecodeError:
        raise HTTPException(
            status_code=400,
            detail="The file is not valid UTF-8 text.",
        )


@router.post(
    "/scan-project",
    response_model=ProjectMetadataResponse,
)
def scan_project_endpoint(request: ProjectScanRequest):
    """Scan a project folder and return validated metadata."""

    try:
        return scan_project(request.project_path)

    except NotADirectoryError as error:
        raise HTTPException(status_code=404, detail=str(error))

    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error))

    except PermissionError:
        raise HTTPException(
            status_code=403,
            detail="Permission denied while accessing the project folder.",
        )
