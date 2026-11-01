
from fastapi import FastAPI

from app.metadata.routes import router as metadata_router

from app.quality.routes import router as quality_router

app = FastAPI(
    title="CodeAtlas Backend",
    description="Backend API for AI-Assisted Codebase Analysis",
    version="1.0.0",
)


@app.get("/")
def home():
    return {"message": "CodeAtlas backend is running"}


@app.get("/health")
def health_check():
    return {
        "status": "healthy",
        "message": "CodeAtlas backend is running"
    }


app.include_router(metadata_router)
app.include_router(quality_router)