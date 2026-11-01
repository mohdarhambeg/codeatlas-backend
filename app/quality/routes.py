
from fastapi import APIRouter, HTTPException

from app.quality.schemas import (
    QualityRequest,
    QualityResponse,
)

from app.quality.service import analyze_project


router = APIRouter(
    prefix="/api/v1/quality",
    tags=["Software Quality Analysis"],
)


@router.post(
    "/analyze",
    response_model=QualityResponse,
)
def analyze_project_endpoint(request: QualityRequest):
    """Analyze software quality of a Python project."""

    try:
        return analyze_project(request.project_path)

    except NotADirectoryError as error:
        raise HTTPException(
            status_code=404,
            detail=str(error),
        )

    except ValueError as error:
        raise HTTPException(
            status_code=400,
            detail=str(error),
        )
