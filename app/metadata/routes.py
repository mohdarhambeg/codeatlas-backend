import httpx

from app.metadata.schemas import ASTMetadataRequest
from app.metadata.ast_client import ASTClient
from app.metadata.extractor import extract_metadata_from_ast
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
ast_client = ASTClient()


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


@router.post("/extract-from-ast")
async def extract_metadata_from_ast_endpoint(
    request: ASTMetadataRequest,
):
    """Extract metadata using AST from the TypeScript backend."""

    try:
        # Step 1: Retrieve AST document.
        ast_document = await ast_client.get_ast_document(
            request.project_id,
            request.analysis_id,
            request.artifact_id,
        )

        # Step 2: Extract metadata from AST.
        metadata = extract_metadata_from_ast(ast_document)

        return {
            "status": "success",
            "metadata": metadata,
        }

    except httpx.HTTPStatusError as error:
        raise HTTPException(
            status_code=error.response.status_code,
            detail="Failed to retrieve AST document from TypeScript backend.",
        )

    except httpx.RequestError:
        raise HTTPException(
            status_code=503,
            detail="Cannot connect to the TypeScript backend.",
        )

    except (ValueError, KeyError, TypeError) as error:
        raise HTTPException(
            status_code=500,
            detail=f"Metadata extraction failed: {error}",
        )