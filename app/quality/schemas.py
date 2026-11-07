
from pydantic import BaseModel, Field


class QualityRequest(BaseModel):
    project_path: str = Field(
        ...,
        min_length=1,
        description="Path to the project folder",
        examples=[r"C:\Users\Hp\codeatlas-backend"],
    )


class QualitySummary(BaseModel):
    total_files: int = 0
    total_lines: int = 0
    total_functions: int = 0
    total_classes: int = 0
    long_functions: int = 0


class QualityIssue(BaseModel):
    file: str
    issue: str
    line: int | None = None


class QualityResponse(BaseModel):
    project_path: str
    summary: QualitySummary
    issues: list[QualityIssue] = Field(default_factory=list)
