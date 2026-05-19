"""Async HTTP client for Dify Service API + Console API.

Thin wrapper around ``httpx.AsyncClient``; one instance per Dify deployment
(keyed by ``base_url``). Translates HTTP failures into gateway domain errors.

Service API (per-App, ``app-*`` token):
    * POST ``/v1/chat-messages`` (blocking + streaming)

Console API authentication (cookie + CSRF, not a bearer JWT):
    Dify's ``POST /console/api/login`` returns ``{"result":"success"}`` and
    sets three cookies: ``access_token``, ``refresh_token``, ``csrf_token``.
    Subsequent console requests must:

        * Send the ``access_token`` cookie (or the same value as a Bearer
          ``Authorization`` header — Dify's ``extract_access_token`` accepts
          either).
        * Send the ``csrf_token`` cookie **and** mirror it in the
          ``X-CSRF-Token`` header. Mismatched values trigger 401.

    See ``api/libs/token.py`` and ``api/controllers/console/auth/login.py``
    in the Dify source.

Console API endpoints used:
    * POST ``/console/api/login`` (returns cookies, body is just ``{result:"success"}``)
    * POST ``/console/api/apps/imports``
    * POST ``/console/api/apps/{app_id}/api-keys``
    * DELETE ``/console/api/apps/{app_id}``

These endpoints are not officially public; behavior is empirically stable in
v1.x but pinning a Dify version is recommended.
"""

from __future__ import annotations

import base64
import json
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import httpx
import structlog

from gateway.errors import DifyTimeoutError, DifyUpstreamError, UpstreamClientError

# Dify Service API 4xx statuses that describe a *client* mistake on the
# dataset / document path (wrong UUID, duplicate name, oversized file, bad
# schema). These should pass through to the SDK caller as 4xx envelopes,
# not 502s. 401/403/429 stay as upstream errors (those are gateway-side
# credential/rate-limit issues, not the caller's fault) — same philosophy
# as the embeddings client (PR #2 review-3).
_DATASET_CLIENT_STATUSES: frozenset[int] = frozenset({400, 404, 409, 413, 422})

logger = structlog.get_logger(__name__)

# Stripe of body that gets logged on upstream errors. We avoid logging full
# bodies because they may contain user prompts or secrets.
_ERR_BODY_TRUNCATE = 500


@dataclass(frozen=True)
class ConsoleSession:
    """Session state needed to call Dify Console API endpoints.

    The values originate from cookies set by ``/console/api/login``. We also
    preserve the *cookie names* the server used (bare or ``__Host-``-prefixed)
    so subsequent requests can echo them verbatim. Dify's
    ``check_csrf_token`` resolves the cookie name through ``_real_cookie_name``,
    which switches to ``__Host-csrf_token`` on HTTPS deploys without a custom
    cookie domain; sending the wrong name causes the cookie/header mismatch
    check to fail with 401.

    ``csrf_token`` must additionally be echoed in the ``X-CSRF-Token`` header
    on every state-changing request (header name is *always* the same; only
    the cookie name varies).
    """

    access_token: str
    csrf_token: str
    access_token_cookie_name: str = "access_token"
    csrf_token_cookie_name: str = "csrf_token"


class DifyClient:
    """Async client bound to a single Dify deployment.

    Use as an async context manager (``async with DifyClient(...) as c``) for
    deterministic shutdown; alternatively call :meth:`aclose` explicitly.
    """

    def __init__(
        self,
        base_url: str,
        *,
        timeout_s: float = 60.0,
        stream_timeout_s: float = 300.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout_s = timeout_s
        self._stream_timeout_s = stream_timeout_s
        self._http = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=httpx.Timeout(timeout_s, read=timeout_s),
            follow_redirects=False,
        )

    @property
    def base_url(self) -> str:
        return self._base_url

    async def __aenter__(self) -> "DifyClient":
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._http.aclose()

    # ------------------------------------------------------------------ #
    # Service API                                                        #
    # ------------------------------------------------------------------ #

    async def chat_messages_blocking(
        self,
        *,
        app_key: str,
        query: str,
        user: str,
        inputs: Mapping[str, Any] | None = None,
        conversation_id: str | None = None,
    ) -> dict[str, Any]:
        """Call ``POST /v1/chat-messages`` in blocking mode.

        Returns the parsed JSON body (Dify ``chat_message`` event payload).

        Raises:
            DifyTimeoutError: timeout while waiting for Dify.
            DifyUpstreamError: Dify returned non-2xx.
        """
        body: dict[str, Any] = {
            "inputs": dict(inputs or {}),
            "query": query,
            "user": user,
            "response_mode": "blocking",
        }
        if conversation_id:
            body["conversation_id"] = conversation_id

        try:
            resp = await self._http.post(
                "/v1/chat-messages",
                headers=_bearer(app_key),
                json=body,
            )
        except httpx.TimeoutException as e:
            raise DifyTimeoutError("Dify chat-messages timed out") from e
        except httpx.RequestError as e:
            raise DifyUpstreamError(f"Dify request failed: {e}") from e

        _raise_for_dify_status(resp)
        return resp.json()

    @asynccontextmanager
    async def open_chat_stream(
        self,
        *,
        app_key: str,
        query: str,
        user: str,
        inputs: Mapping[str, Any] | None = None,
        conversation_id: str | None = None,
    ) -> AsyncIterator[AsyncIterator[str]]:
        """Open a streaming ``chat-messages`` request and yield a line iterator.

        Implemented as an async context manager so the caller can perform
        pre-flight error handling **before** any response bytes are sent
        downstream. The HTTP request is fully sent and response headers are
        received when entering the context; non-2xx responses raise
        :class:`DifyUpstreamError` here, *not* mid-iteration. Once the
        context yields, iteration produces SSE lines until exhaustion or
        timeout.

        Example::

            async with client.open_chat_stream(...) as lines:
                async for line in lines:
                    ...

        Raises (during context entry):
            DifyTimeoutError: connect/read timeout before headers received.
            DifyUpstreamError: non-2xx HTTP response.
        """
        body: dict[str, Any] = {
            "inputs": dict(inputs or {}),
            "query": query,
            "user": user,
            "response_mode": "streaming",
        }
        if conversation_id:
            body["conversation_id"] = conversation_id

        cm = self._http.stream(
            "POST",
            "/v1/chat-messages",
            headers=_bearer(app_key),
            json=body,
            timeout=httpx.Timeout(self._stream_timeout_s, read=self._stream_timeout_s),
        )
        try:
            resp = await cm.__aenter__()
        except httpx.TimeoutException as e:
            raise DifyTimeoutError("Dify streaming chat-messages timed out") from e
        except httpx.RequestError as e:
            raise DifyUpstreamError(f"Dify streaming request failed: {e}") from e

        # Status check happens *here*, before any caller has started writing
        # bytes downstream. _raise_for_dify_status accesses resp.text, which
        # for streaming responses requires resp.aread() first.
        if not resp.is_success:
            try:
                await resp.aread()
            except Exception:  # noqa: BLE001
                pass  # body unreadable; fall through to status-only error
            try:
                _raise_for_dify_status(resp)
            finally:
                await cm.__aexit__(None, None, None)

        async def iter_lines() -> AsyncIterator[str]:
            try:
                async for line in resp.aiter_lines():
                    yield line
            except httpx.TimeoutException as e:
                raise DifyTimeoutError("Dify streaming chat-messages timed out") from e
            except httpx.RequestError as e:
                raise DifyUpstreamError(f"Dify streaming read failed: {e}") from e

        try:
            yield iter_lines()
        finally:
            await cm.__aexit__(None, None, None)

    # ------------------------------------------------------------------ #
    # Service API: Datasets (knowledge base CRUD)                        #
    # ------------------------------------------------------------------ #
    #
    # Datasets use a different auth key than chat-messages: the customer's
    # ``dataset_api_key`` from their Dify deployment (set per-customer in
    # the registry's ``DifyConnection``). The wire protocol is the same
    # ``Authorization: Bearer <key>`` pattern, just a different key value.
    # Dataset IDs in URLs are Dify-issued UUIDs.

    async def create_dataset(
        self,
        *,
        dataset_api_key: str,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        """``POST /v1/datasets`` — create a new dataset.

        ``payload`` shape (subset of Dify's ``DatasetCreatePayload``):
            * ``name`` (required)
            * ``description``
            * ``indexing_technique`` ("high_quality" | "economy")
            * ``embedding_model``, ``embedding_model_provider``
            * ``permission``, ``provider``, ``retrieval_model``, ...

        The gateway router fills in ``embedding_model_provider`` based on
        the customer registry; callers should pass the resolved payload.
        """
        try:
            resp = await self._http.post(
                "/v1/datasets",
                headers=_bearer(dataset_api_key),
                json=dict(payload),
            )
        except httpx.TimeoutException as e:
            raise DifyTimeoutError("Dify create-dataset timed out") from e
        except httpx.RequestError as e:
            raise DifyUpstreamError(f"Dify create-dataset failed: {e}") from e
        _raise_for_dify_status(resp, pass_client_errors=True)
        return resp.json()

    async def list_datasets(
        self,
        *,
        dataset_api_key: str,
        page: int = 1,
        limit: int = 20,
        keyword: str | None = None,
    ) -> dict[str, Any]:
        """``GET /v1/datasets`` — list datasets visible to the API key.

        Returns the raw Dify envelope:
        ``{"data": [...], "has_more": bool, "limit": int, "total": int, "page": int}``.
        The gateway router reshapes this into the OpenAI-style response.
        """
        params: dict[str, Any] = {"page": page, "limit": limit}
        if keyword:
            params["keyword"] = keyword
        try:
            resp = await self._http.get(
                "/v1/datasets",
                headers=_bearer(dataset_api_key),
                params=params,
            )
        except httpx.TimeoutException as e:
            raise DifyTimeoutError("Dify list-datasets timed out") from e
        except httpx.RequestError as e:
            raise DifyUpstreamError(f"Dify list-datasets failed: {e}") from e
        _raise_for_dify_status(resp, pass_client_errors=True)
        return resp.json()

    async def get_dataset(
        self,
        *,
        dataset_api_key: str,
        dataset_id: str,
    ) -> dict[str, Any]:
        """``GET /v1/datasets/{uuid}`` — fetch a single dataset's metadata."""
        try:
            resp = await self._http.get(
                f"/v1/datasets/{dataset_id}",
                headers=_bearer(dataset_api_key),
            )
        except httpx.TimeoutException as e:
            raise DifyTimeoutError("Dify get-dataset timed out") from e
        except httpx.RequestError as e:
            raise DifyUpstreamError(f"Dify get-dataset failed: {e}") from e
        _raise_for_dify_status(resp, pass_client_errors=True)
        return resp.json()

    async def delete_dataset(
        self,
        *,
        dataset_api_key: str,
        dataset_id: str,
    ) -> None:
        """``DELETE /v1/datasets/{uuid}`` — remove a dataset.

        Idempotent in the GC sense: a 404 from Dify is treated as success
        (the dataset is already gone), matching the chat-app delete pattern.
        """
        try:
            resp = await self._http.delete(
                f"/v1/datasets/{dataset_id}",
                headers=_bearer(dataset_api_key),
            )
        except httpx.TimeoutException as e:
            raise DifyTimeoutError("Dify delete-dataset timed out") from e
        except httpx.RequestError as e:
            raise DifyUpstreamError(f"Dify delete-dataset failed: {e}") from e
        if resp.status_code == 404:
            return
        _raise_for_dify_status(resp, pass_client_errors=True)

    async def create_document_by_file(
        self,
        *,
        dataset_api_key: str,
        dataset_id: str,
        filename: str,
        content: bytes,
        content_type: str,
        indexing_technique: str = "high_quality",
        process_mode: str = "automatic",
    ) -> dict[str, Any]:
        """``POST /v1/datasets/{uuid}/document/create-by-file`` — upload a file.

        Dify expects ``multipart/form-data`` with two parts:
            * ``file`` — the binary content (with filename + Content-Type)
            * ``data`` — a JSON string carrying ``indexing_technique`` +
              ``process_rule`` and any other knobs.

        We default ``process_rule.mode = "automatic"`` so Dify picks sensible
        chunking; customers needing custom chunking go through Dify directly
        (out of PR #3 scope).

        Memory note: ``content`` is a bytes blob (the caller has already
        read the request stream into memory). Sufficient for typical KB
        docs (<100 MB); large-file streaming is a follow-up if needed.
        """
        data_payload = {
            "indexing_technique": indexing_technique,
            "process_rule": {"mode": process_mode},
        }
        files = {"file": (filename, content, content_type)}
        data = {"data": json.dumps(data_payload)}
        # Don't use the Content-Type from _bearer — httpx sets multipart
        # boundary automatically; an explicit application/json header here
        # would silently override it and break the upload.
        headers = {"Authorization": f"Bearer {dataset_api_key}"}
        try:
            resp = await self._http.post(
                f"/v1/datasets/{dataset_id}/document/create-by-file",
                headers=headers,
                files=files,
                data=data,
            )
        except httpx.TimeoutException as e:
            raise DifyTimeoutError("Dify create-by-file timed out") from e
        except httpx.RequestError as e:
            raise DifyUpstreamError(f"Dify create-by-file failed: {e}") from e
        _raise_for_dify_status(resp, pass_client_errors=True)
        return resp.json()

    async def list_documents(
        self,
        *,
        dataset_api_key: str,
        dataset_id: str,
        page: int = 1,
        limit: int = 20,
        keyword: str | None = None,
    ) -> dict[str, Any]:
        """``GET /v1/datasets/{uuid}/documents`` — list documents in a dataset."""
        params: dict[str, Any] = {"page": page, "limit": limit}
        if keyword:
            params["keyword"] = keyword
        try:
            resp = await self._http.get(
                f"/v1/datasets/{dataset_id}/documents",
                headers=_bearer(dataset_api_key),
                params=params,
            )
        except httpx.TimeoutException as e:
            raise DifyTimeoutError("Dify list-documents timed out") from e
        except httpx.RequestError as e:
            raise DifyUpstreamError(f"Dify list-documents failed: {e}") from e
        _raise_for_dify_status(resp, pass_client_errors=True)
        return resp.json()

    async def delete_document(
        self,
        *,
        dataset_api_key: str,
        dataset_id: str,
        document_id: str,
    ) -> None:
        """``DELETE /v1/datasets/{uuid}/documents/{document_id}`` — remove a document.

        404 is treated as idempotent success (matches the dataset / app
        delete pattern).
        """
        try:
            resp = await self._http.delete(
                f"/v1/datasets/{dataset_id}/documents/{document_id}",
                headers=_bearer(dataset_api_key),
            )
        except httpx.TimeoutException as e:
            raise DifyTimeoutError("Dify delete-document timed out") from e
        except httpx.RequestError as e:
            raise DifyUpstreamError(f"Dify delete-document failed: {e}") from e
        if resp.status_code == 404:
            return
        _raise_for_dify_status(resp, pass_client_errors=True)

    async def retrieve_dataset(
        self,
        *,
        dataset_api_key: str,
        dataset_id: str,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        """``POST /v1/datasets/{uuid}/retrieve`` — pure-retrieval (hit-testing).

        ``payload`` shape:
            * ``query`` (required)
            * ``retrieval_model`` (optional override)
            * ``external_retrieval_model``

        Returns ``{"query": {...}, "records": [{segment, score, ...}]}``.
        """
        try:
            resp = await self._http.post(
                f"/v1/datasets/{dataset_id}/retrieve",
                headers=_bearer(dataset_api_key),
                json=dict(payload),
            )
        except httpx.TimeoutException as e:
            raise DifyTimeoutError("Dify dataset-retrieve timed out") from e
        except httpx.RequestError as e:
            raise DifyUpstreamError(f"Dify dataset-retrieve failed: {e}") from e
        _raise_for_dify_status(resp, pass_client_errors=True)
        return resp.json()

    # ------------------------------------------------------------------ #
    # Console API (App management)                                       #
    # ------------------------------------------------------------------ #

    async def console_login(self, email: str, password: str) -> ConsoleSession:
        """Authenticate against the console and return cookie-derived tokens.

        Dify's ``/console/api/login`` returns ``{"result":"success"}`` and
        sets ``access_token`` + ``csrf_token`` cookies. We extract both from
        the response cookie jar (httpx parses ``Set-Cookie`` automatically).

        Note:
            Dify's ``@decrypt_password_field`` decorator expects the password
            payload to be base64-encoded by the client (the Web UI does this
            client-side). Sending plaintext yields a 401 ``Invalid encrypted
            data`` because the server's base64 decode step fails. See
            ``api/controllers/console/wraps.py`` and ``api/libs/encryption.py``
            in the Dify source.

        Raises:
            DifyUpstreamError: login failed or cookies missing.
        """
        encoded_password = base64.b64encode(password.encode("utf-8")).decode("ascii")
        try:
            resp = await self._http.post(
                "/console/api/login",
                json={"email": email, "password": encoded_password, "language": "en-US"},
            )
        except httpx.RequestError as e:
            raise DifyUpstreamError(f"Dify console login failed: {e}") from e
        _raise_for_dify_status(resp)

        # Dify uses two cookie naming variants: bare ("access_token") for
        # http/non-secure deployments, and ``__Host-`` prefixed for secure
        # deployments without a custom cookie domain. Read whichever was sent
        # AND remember the name so we can echo it on subsequent requests.
        access_token, access_name = _read_cookie_with_name(resp, "access_token")
        csrf_token, csrf_name = _read_cookie_with_name(resp, "csrf_token")

        if not access_token or not csrf_token:
            raise DifyUpstreamError(
                "Dify console login did not set expected cookies "
                "(access_token / csrf_token); response cookies: "
                f"{sorted(resp.cookies.keys())}"
            )
        return ConsoleSession(
            access_token=access_token,
            csrf_token=csrf_token,
            access_token_cookie_name=access_name,
            csrf_token_cookie_name=csrf_name,
        )

    async def console_import_app(self, session: ConsoleSession, yaml_content: str) -> str:
        """Create an App from a DSL YAML string. Returns the new ``app_id``."""
        self._set_session_cookies(session)
        try:
            resp = await self._http.post(
                "/console/api/apps/imports",
                headers=_console_headers(session),
                json={"mode": "yaml-content", "yaml_content": yaml_content},
            )
        except httpx.RequestError as e:
            raise DifyUpstreamError(f"Dify app import failed: {e}") from e
        _raise_for_dify_status(resp)
        data = resp.json()
        # Dify returns either {app_id: ...} or {id: ...} depending on version;
        # we accept both to stay resilient across minor upgrades.
        app_id = data.get("app_id") or data.get("id")
        if not app_id:
            raise DifyUpstreamError("Dify app import response missing app_id")
        return str(app_id)

    async def console_create_app_api_key(self, session: ConsoleSession, app_id: str) -> str:
        """Generate a new ``app-*`` token bound to ``app_id``."""
        self._set_session_cookies(session)
        try:
            resp = await self._http.post(
                f"/console/api/apps/{app_id}/api-keys",
                headers=_console_headers(session),
            )
        except httpx.RequestError as e:
            raise DifyUpstreamError(f"Dify app api-key creation failed: {e}") from e
        _raise_for_dify_status(resp)
        data = resp.json()
        token = data.get("token")
        if not token:
            raise DifyUpstreamError("Dify api-key response missing token")
        return str(token)

    async def console_delete_app(self, session: ConsoleSession, app_id: str) -> None:
        """Delete an App (used by the GC sweep)."""
        self._set_session_cookies(session)
        try:
            resp = await self._http.delete(
                f"/console/api/apps/{app_id}",
                headers=_console_headers(session),
            )
        except httpx.RequestError as e:
            raise DifyUpstreamError(f"Dify app delete failed: {e}") from e
        # 404 is fine—App was already removed (idempotent GC).
        if resp.status_code == 404:
            return
        _raise_for_dify_status(resp)

    def _set_session_cookies(self, session: ConsoleSession) -> None:
        """Sync the session's cookies onto the underlying ``AsyncClient`` jar.

        httpx deprecated per-request ``cookies=`` kwargs (the behavior is
        ambiguous when the client also has its own jar). The supported path
        is: mutate ``client.cookies`` and let httpx auto-include them on the
        next request.

        We clear both naming variants (bare and ``__Host-``-prefixed) before
        setting so that switching deployments — or a session that just
        re-logged into a server with a different cookie convention — does
        not leak two cookies under different names.
        """
        jar = self._http.cookies
        for name in ("access_token", "__Host-access_token", "csrf_token", "__Host-csrf_token"):
            if name in jar:
                jar.delete(name)
        jar.set(session.access_token_cookie_name, session.access_token, path="/")
        jar.set(session.csrf_token_cookie_name, session.csrf_token, path="/")


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def _console_headers(session: ConsoleSession) -> dict[str, str]:
    """Headers for an authenticated console API request.

    Dify's ``extract_access_token`` accepts either cookie or ``Authorization``
    bearer; sending both is harmless. ``X-CSRF-Token`` must equal the value
    of the ``csrf_token`` cookie (verified by ``check_csrf_token``).
    """
    return {
        "Authorization": f"Bearer {session.access_token}",
        "Content-Type": "application/json",
        "X-CSRF-Token": session.csrf_token,
    }


def _read_cookie_with_name(
    resp: httpx.Response, base_name: str
) -> tuple[str | None, str]:
    """Return ``(value, actual_name_used)`` for ``base_name`` from response cookies.

    Falls back to the bare ``base_name`` for the returned name when the cookie
    is missing — this gives sensible defaults but the caller must check the
    value separately.
    """
    if base_name in resp.cookies:
        return resp.cookies[base_name], base_name
    host_prefixed = f"__Host-{base_name}"
    if host_prefixed in resp.cookies:
        return resp.cookies[host_prefixed], host_prefixed
    return None, base_name


def _raise_for_dify_status(
    resp: httpx.Response,
    *,
    pass_client_errors: bool = False,
) -> None:
    """Translate non-2xx HTTP responses into gateway domain errors.

    ``pass_client_errors=True`` (used by dataset / document methods) preserves
    expected client-shape 4xx (``_DATASET_CLIENT_STATUSES`` — wrong UUID,
    duplicate name, oversized file, schema error) as ``UpstreamClientError``,
    so the SDK caller sees the right 4xx instead of a misleading 502.
    Other 4xx (401/403/429) and 5xx still surface as ``DifyUpstreamError``;
    those are gateway-side credential / rate-limit / outage signals.
    """
    if resp.is_success:
        return

    body_preview: str = ""
    try:
        body_preview = resp.text[:_ERR_BODY_TRUNCATE]
    except Exception:  # noqa: BLE001
        # ``resp.text`` may raise on streaming responses; fall through.
        body_preview = "<body unreadable>"

    logger.warning(
        "dify.upstream_error",
        status=resp.status_code,
        method=resp.request.method,
        url=str(resp.request.url),
        body=body_preview,
    )

    if pass_client_errors and resp.status_code in _DATASET_CLIENT_STATUSES:
        raise UpstreamClientError(
            f"Dify rejected request (HTTP {resp.status_code}): {body_preview}",
            upstream_status=resp.status_code,
        )

    raise DifyUpstreamError(
        f"Dify returned HTTP {resp.status_code}: {body_preview}",
    )
