
import httpx
import asyncio
from app.metadata.extractor import extract_metadata_from_ast

AST_BACKEND_URL = "http://127.0.0.1:5000"


class ASTClient:

    def __init__(self):
        self.base_url = AST_BACKEND_URL
        self.timeout = 30.0

    async def get_analysis(self, project_id: str, analysis_id: str):
        url = (
            f"{self.base_url}/api/projects/"
            f"{project_id}/analysis/{analysis_id}"
        )

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.get(url)
            response.raise_for_status()
            return response.json()

    async def get_ast_list(self, project_id: str, analysis_id: str):
        url = (
            f"{self.base_url}/api/projects/"
            f"{project_id}/analysis/{analysis_id}/asts"
        )

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.get(url)
            response.raise_for_status()
            return response.json()

    async def get_ast_document(
        self,
        project_id: str,
        analysis_id: str,
        artifact_id: str,
    ):
        url = (
            f"{self.base_url}/api/projects/"
            f"{project_id}/analysis/{analysis_id}/asts/{artifact_id}"
        )

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.get(url)
            response.raise_for_status()
            return response.json()





async def test_metadata_extraction():

    ast_client = ASTClient()

    project_id = "PRJ-2A4FB1"

    analysis_id = "AN-4d023d0a-d185-46aa-9890-9cd33d129ed9"

    artifact_id = "aef31e9ff76c85a6f0e4d9ae0b6a7137d634b46f0c5fb1476e325c800eccb83a"

    try:
        print("Step 1: Retrieving AST document...")

        ast_document = await ast_client.get_ast_document(
            project_id,
            analysis_id,
            artifact_id,
        )

        print("AST document retrieved successfully!")

        print("\nStep 2: Extracting metadata...")

        metadata = extract_metadata_from_ast(ast_document)

        print("\nMETADATA EXTRACTION SUCCESSFUL")
        print("==============================")

        print("Project:", metadata["project_id"])
        print("File:", metadata["file"])
        print("Language:", metadata["language"])

        print("\nSummary:")
        print(metadata["summary"])

        print("\nExtracted Functions:")

        for function in metadata["functions"]:
            print(function)

    except Exception as error:
        print("\nMetadata extraction failed:")
        print(type(error).__name__, error)


if __name__ == "__main__":
    asyncio.run(test_metadata_extraction())
