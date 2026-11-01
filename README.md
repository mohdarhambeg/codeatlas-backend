# CodeAtlas Backend

CodeAtlas is an AI-Assisted Codebase Analysis project developed using Python and FastAPI.

## Project Modules

1. Metadata Extraction Module
2. Software Quality Analysis Module
3. Storage Module
4. Frontend API Support Module
5. Notification Module
6. Job Processing Module

## Technology Stack

- Python
- FastAPI
- Uvicorn
- PostgreSQL (planned)
- SQLAlchemy (planned)

## Setup Instructions

### 1. Create and activate a virtual environment

On Windows PowerShell:

```powershell
py -m venv venv
.\venv\Scripts\Activate.ps1
```

### 2. Install dependencies

```powershell
python -m pip install -r requirements.txt
```

### 3. Run the backend

```powershell
python -m uvicorn main:app --reload
```

### 4. Open the API documentation

Visit `http://127.0.0.1:8000/docs`.

### 5. Check the health endpoint

Visit `http://127.0.0.1:8000/health`.

## Development Status

Initial FastAPI setup is complete. The six modules will be implemented incrementally.
