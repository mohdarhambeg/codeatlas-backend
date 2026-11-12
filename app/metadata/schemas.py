
from pydantic import BaseModel, Field


class MetadataRequest(BaseModel):
    file_path: str = Field(
        ...,
        min_length=1,
        description="Path to a Python file",
        examples=[r"C:\Users\Hp\codeatlas-backend\tests\sample_code.py"],
    )


class ProjectScanRequest(BaseModel):
    project_path: str = Field(
        ...,
        min_length=1,
        description="Path to the project folder",
        examples=[r"C:\Users\Hp\codeatlas-backend"],
    )


class MetadataSummary(BaseModel):
    total_imports: int = 0
    total_classes: int = 0
    total_functions: int = 0
    total_methods: int = 0


class MetadataError(BaseModel):
    file: str
    error: str


class ProjectMetadataResponse(BaseModel):
    project_path: str
    files_scanned: int
    files_with_errors: int
    summary: MetadataSummary
    results: list[dict] = Field(default_factory=list)
    errors: list[MetadataError] = Field(default_factory=list)


class ASTMetadataRequest(BaseModel):
    project_id: str = Field(..., min_length=1)
    analysis_id: str = Field(..., min_length=1)
    artifact_id: str = Field(..., min_length=1)


from app.metadata.models import MetadataDocument


class MetadataDocumentResponse(BaseModel):
    status: str = 'success'
    metadata: MetadataDocument
