"""HTTP-only communication with the trusted local CodeAtlas AST service."""
import json
import os
import re
from urllib.parse import urlsplit

import httpx


def _invalid_constant(_):
    raise ValueError('Non-JSON numeric constant')


class ASTRequestError(ValueError):
    pass


class UpstreamError(Exception):
    def __init__(self, code: str, status_code: int, message: str):
        self.code = code
        self.status_code = status_code
        self.message = message
        super().__init__(message)


class ASTClient:
    def __init__(self, base_url: str | None = None, *, transport=None):
        self.base_url = (base_url or os.getenv('CODEATLAS_AST_BASE_URL', 'http://127.0.0.1:5000')).rstrip('/')
        parsed = urlsplit(self.base_url)
        if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError('Invalid AST service base URL')
        self.timeout = httpx.Timeout(30.0, connect=5.0)
        self.transport = transport

    @staticmethod
    def _identity(project_id, analysis_id, artifact_id=None):
        if (not re.fullmatch(r'PRJ-[A-Fa-f0-9]{6}', project_id) or
                not re.fullmatch(r'AN-[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}', analysis_id) or
                (artifact_id is not None and not re.fullmatch(r'[a-f0-9]{64}', artifact_id))):
            raise ASTRequestError('Invalid AST request identity')

    async def _get(self, path):
        try:
            async with httpx.AsyncClient(timeout=self.timeout, transport=self.transport,
                                         follow_redirects=False, trust_env=False) as client:
                async with client.stream('GET', self.base_url + path) as response:
                    if not response.is_success:
                        status = response.status_code
                        raise UpstreamError('AST_NOT_FOUND' if status == 404 else 'AST_UPSTREAM_HTTP_ERROR',
                            404 if status == 404 else 502, 'AST resource is unavailable.' if status == 404 else 'AST service rejected the request.')
                    # Match the upstream storage budget and stop before buffering oversized data.
                    chunks, size = [], 0
                    async for chunk in response.aiter_bytes():
                        size += len(chunk)
                        if size > 64 * 1024 * 1024:
                            raise UpstreamError('AST_RESPONSE_TOO_LARGE', 502, 'AST response exceeds the supported size.')
                        chunks.append(chunk)
                    try:
                        return json.loads(b''.join(chunks), parse_constant=_invalid_constant)
                    except (ValueError, UnicodeDecodeError, RecursionError) as error:
                        raise UpstreamError('AST_INVALID_JSON', 502, 'AST service returned invalid JSON.') from error
        except httpx.TimeoutException as error:
            raise UpstreamError('AST_TIMEOUT', 504, 'AST service timed out.') from error
        except httpx.RequestError as error:
            raise UpstreamError('AST_UNAVAILABLE', 503, 'Cannot connect to the AST service.') from error

    async def get_analysis(self, project_id: str, analysis_id: str):
        self._identity(project_id, analysis_id)
        return await self._get(f'/api/projects/{project_id}/analysis/{analysis_id}')

    async def get_ast_list(self, project_id: str, analysis_id: str):
        self._identity(project_id, analysis_id)
        return await self._get(f'/api/projects/{project_id}/analysis/{analysis_id}/asts')

    async def get_ast_document(self, project_id: str, analysis_id: str, artifact_id: str):
        self._identity(project_id, analysis_id, artifact_id)
        return await self._get(f'/api/projects/{project_id}/analysis/{analysis_id}/asts/{artifact_id}')
