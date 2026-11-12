import logging

from app.metadata.schemas import ASTMetadataRequest
from app.metadata.ast_client import ASTClient, ASTRequestError, UpstreamError
from app.metadata.extractor import legacy_metadata
from app.metadata.validation import ASTValidationError, UnsupportedLanguageError
from app.metadata.schemas import MetadataDocumentResponse
from app.metadata.service import retrieve_ast_metadata
from fastapi import APIRouter, HTTPException, Depends

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
v2_router = APIRouter(prefix="/api/v2/metadata", tags=["Metadata v2"])
logger = logging.getLogger(__name__)


def get_ast_client():
    return ASTClient()


@router.post("/extract")
def extract_metadata_endpoint(request: MetadataRequest):
    """Extract metadata from a single Python file."""

    try:
        result = extract_metadata(request.file_path)

        return {
            "status": "success",
            "metadata": result,
        }

    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Python file was not found.")

    except UnicodeDecodeError:
        raise HTTPException(status_code=400, detail="The file is not valid UTF-8 text.")

    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid path or unsupported file type.")

    except SyntaxError as error:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid Python syntax at line {error.lineno}.",
        )

    except PermissionError:
        raise HTTPException(status_code=403, detail="Permission denied while accessing the file.")

    except OSError:
        raise HTTPException(status_code=400, detail="Cannot read the Python file.")


@router.post(
    "/scan-project",
    response_model=ProjectMetadataResponse,
)
def scan_project_endpoint(request: ProjectScanRequest):
    """Scan a project folder and return validated metadata."""

    try:
        return scan_project(request.project_path)

    except NotADirectoryError:
        raise HTTPException(status_code=404, detail="Project folder was not found.")

    except UnicodeDecodeError:
        raise HTTPException(status_code=400, detail="The file is not valid UTF-8 text.")

    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid path or unsupported file type.")

    except PermissionError:
        raise HTTPException(
            status_code=403,
            detail="Permission denied while accessing the project folder.",
        )


async def _remote(request: ASTMetadataRequest, client: ASTClient):
    try:
        return await retrieve_ast_metadata(client, request.project_id, request.analysis_id, request.artifact_id)
    except UpstreamError as error:
        raise HTTPException(status_code=error.status_code,
                            detail={"code": error.code, "message": error.message}) from error
    except UnsupportedLanguageError as error:
        raise HTTPException(status_code=422, detail={"code": "UNSUPPORTED_LANGUAGE",
                            "message": "No verified AST adapter is available for this language."}) from error
    except ASTValidationError as error:
        raise HTTPException(status_code=502, detail={"code": "INVALID_AST_DOCUMENT",
                            "message": "AST service returned invalid or incompatible data."}) from error
    except ASTRequestError as error:
        raise HTTPException(status_code=400, detail={"code": "INVALID_AST_REQUEST",
                            "message": "Invalid project, analysis, or artifact identifier."}) from error
    except Exception as error:
        logger.exception("Unexpected metadata extraction failure")
        raise HTTPException(status_code=500, detail={"code": "METADATA_EXTRACTION_FAILED",
                            "message": "Metadata extraction failed."}) from error


@router.post("/extract-from-ast")
async def extract_metadata_from_ast_endpoint(request: ASTMetadataRequest, client: ASTClient = Depends(get_ast_client)):
    document = await _remote(request, client)
    return {"status": "success", "metadata": legacy_metadata(document)}


@v2_router.post("/extract-from-ast", response_model=MetadataDocumentResponse)
async def extract_metadata_document_endpoint(request: ASTMetadataRequest, client: ASTClient = Depends(get_ast_client)):
    return MetadataDocumentResponse(metadata=await _remote(request, client))


# Project endpoints return bounded outcomes/references; entity payloads stay per-file.
from app.metadata.persistence import FileMetadataStore, MetadataStorageError
from app.metadata.project_models import MetadataAnalysis
from app.metadata.project_service import ProjectMetadataService, ProjectExtractionError
from fastapi import Query


def get_metadata_store():
    return FileMetadataStore()


def _project_http_error(error):
    if isinstance(error, (MetadataStorageError, ProjectExtractionError, UpstreamError)):
        code, status = error.code, error.status_code
    elif isinstance(error, ASTRequestError):
        code, status = 'INVALID_AST_REQUEST', 400
    elif isinstance(error, ASTValidationError):
        code, status = 'INVALID_UPSTREAM_ANALYSIS', 502
    else:
        logger.exception('Project metadata operation failed')
        code, status = 'METADATA_OPERATION_FAILED', 500
    messages = {
        'METADATA_NOT_FOUND': 'Persisted metadata was not found.',
        'METADATA_STORAGE_CORRUPT': 'Persisted metadata is corrupt.',
        'METADATA_STORAGE_INCOMPATIBLE': 'Persisted metadata uses an unsupported version.',
        'METADATA_EXTRACTION_BUSY': 'Metadata extraction capacity is busy; retry later.',
        'UPSTREAM_ANALYSIS_NOT_READY': 'Upstream analysis must be completed before metadata extraction.',
        'METADATA_ANALYSIS_CONFLICT': 'Stored metadata belongs to a different source snapshot.',
    }
    return HTTPException(status_code=status, detail={'code': code, 'message': messages.get(code, 'Metadata operation could not be completed.')})


@v2_router.post('/projects/{project_id}/analyses/{analysis_id}/extract', response_model=MetadataAnalysis)
async def extract_project_metadata_endpoint(project_id: str, analysis_id: str,
        retry_failed: bool = Query(False), client: ASTClient = Depends(get_ast_client),
        store: FileMetadataStore = Depends(get_metadata_store)):
    try:
        return await ProjectMetadataService(client, store).extract(project_id, analysis_id, retry_failed=retry_failed)
    except Exception as error:
        raise _project_http_error(error) from error


@v2_router.get('/projects/{project_id}/analyses/{analysis_id}', response_model=MetadataAnalysis)
def get_project_metadata_endpoint(project_id: str, analysis_id: str,
        store: FileMetadataStore = Depends(get_metadata_store)):
    try:
        return store.load_analysis(project_id, analysis_id)
    except Exception as error:
        raise _project_http_error(error) from error


@v2_router.get('/projects/{project_id}/analyses/{analysis_id}/files')
def list_project_metadata_files_endpoint(project_id: str, analysis_id: str,
        store: FileMetadataStore = Depends(get_metadata_store)):
    try:
        analysis = store.load_analysis(project_id, analysis_id)
        return {'projectId': project_id, 'analysisId': analysis_id, 'files': analysis.files}
    except Exception as error:
        raise _project_http_error(error) from error


@v2_router.get('/projects/{project_id}/analyses/{analysis_id}/files/{artifact_id}', response_model=MetadataDocumentResponse)
def get_project_metadata_file_endpoint(project_id: str, analysis_id: str, artifact_id: str,
        store: FileMetadataStore = Depends(get_metadata_store)):
    try:
        ASTClient._identity(project_id, analysis_id, artifact_id)
        analysis = store.load_analysis(project_id, analysis_id)
        if not any(f.metadataArtifact and f.metadataArtifact.artifactId == artifact_id for f in analysis.files):
            raise MetadataStorageError('METADATA_NOT_FOUND', 404)
        return MetadataDocumentResponse(metadata=store.load_file(project_id, analysis_id, artifact_id))
    except Exception as error:
        raise _project_http_error(error) from error
