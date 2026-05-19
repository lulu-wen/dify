"""Tests for the Dify async HTTP client.

We use ``respx`` to intercept httpx calls without spinning up a real Dify.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from gateway.dify.client import ConsoleSession, DifyClient
from gateway.errors import DifyTimeoutError, DifyUpstreamError, UpstreamClientError


@pytest.fixture
async def client() -> DifyClient:
    c = DifyClient(base_url="http://dify.test", timeout_s=5.0, stream_timeout_s=5.0)
    try:
        yield c
    finally:
        await c.aclose()


@pytest.mark.asyncio
async def test_chat_messages_blocking_returns_parsed_body(client: DifyClient) -> None:
    expected = {"id": "msg-1", "answer": "hi", "conversation_id": "conv-1", "metadata": {}}
    with respx.mock(base_url="http://dify.test") as m:
        route = m.post("/v1/chat-messages").mock(return_value=httpx.Response(200, json=expected))
        result = await client.chat_messages_blocking(
            app_key="app-x", query="hello", user="u1"
        )
    assert result == expected
    assert route.called
    sent = route.calls.last.request
    assert sent.headers["authorization"] == "Bearer app-x"


@pytest.mark.asyncio
async def test_chat_messages_blocking_includes_conversation_id_when_provided(client: DifyClient) -> None:
    with respx.mock(base_url="http://dify.test") as m:
        route = m.post("/v1/chat-messages").mock(return_value=httpx.Response(200, json={}))
        await client.chat_messages_blocking(
            app_key="app-x", query="q", user="u", conversation_id="conv-9"
        )
    body = route.calls.last.request.read().decode()
    assert "conv-9" in body


@pytest.mark.asyncio
async def test_chat_messages_blocking_omits_conversation_id_when_absent(client: DifyClient) -> None:
    with respx.mock(base_url="http://dify.test") as m:
        route = m.post("/v1/chat-messages").mock(return_value=httpx.Response(200, json={}))
        await client.chat_messages_blocking(app_key="app-x", query="q", user="u")
    body = route.calls.last.request.read().decode()
    assert "conversation_id" not in body


@pytest.mark.asyncio
async def test_chat_messages_blocking_raises_on_5xx(client: DifyClient) -> None:
    with respx.mock(base_url="http://dify.test") as m:
        m.post("/v1/chat-messages").mock(return_value=httpx.Response(500, text="boom"))
        with pytest.raises(DifyUpstreamError, match="500"):
            await client.chat_messages_blocking(app_key="app-x", query="q", user="u")


@pytest.mark.asyncio
async def test_chat_messages_blocking_raises_on_timeout(client: DifyClient) -> None:
    with respx.mock(base_url="http://dify.test") as m:
        m.post("/v1/chat-messages").mock(side_effect=httpx.TimeoutException("read timeout"))
        with pytest.raises(DifyTimeoutError):
            await client.chat_messages_blocking(app_key="app-x", query="q", user="u")


# ---------------------------------------------------------------------------
# Dataset / document Service API — 4xx passthrough (codex review-1 P2)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [400, 403, 404, 409, 413, 415, 422])
async def test_dataset_create_4xx_raises_upstream_client_error(
    client: DifyClient, status: int
) -> None:
    """Dify 4xx on dataset operations must surface as ``UpstreamClientError``
    so the SDK caller sees the original 4xx (their request was bad), not a
    misleading 502 (gateway is broken).

    Codex review-1 P2 introduced the passthrough; codex review-3 P2
    widened it to cover **403** (Dify uses this for per-dataset-disabled
    and per-tenant-quota refusals) and **415** (UnsupportedFileTypeError
    on create-by-file). Both are client-actionable.
    """
    with respx.mock(base_url="http://dify.test") as m:
        m.post("/v1/datasets").mock(
            return_value=httpx.Response(status, json={"message": f"dify said {status}"})
        )
        with pytest.raises(UpstreamClientError) as exc_info:
            await client.create_dataset(
                dataset_api_key="ds-key",
                payload={"name": "kb"},
            )
    # ``status_code`` is overridden at instance level to preserve the
    # upstream status; verify it survived.
    assert exc_info.value.status_code == status


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 429])
async def test_dataset_create_non_shape_4xx_still_502(
    client: DifyClient, status: int
) -> None:
    """401 (bad dataset_api_key in registry) and 429 (upstream rate-limited
    the gateway) describe gateway-side problems the SDK caller can't fix.
    They must NOT mislead the caller into thinking their own request was
    wrong — keep them as ``DifyUpstreamError`` so the envelope is 502.

    This is the same philosophy as PR #2 review-3 for the embeddings
    client, with one carve-out: codex review-3 noted that 403 in Dify's
    dataset Service API means «this dataset is disabled / quota refused»
    which IS client-actionable, so 403 moved into the client-error set.
    """
    with respx.mock(base_url="http://dify.test") as m:
        m.post("/v1/datasets").mock(
            return_value=httpx.Response(status, json={"message": "auth fail"})
        )
        with pytest.raises(DifyUpstreamError):
            await client.create_dataset(
                dataset_api_key="ds-key",
                payload={"name": "kb"},
            )


@pytest.mark.asyncio
async def test_create_document_by_file_415_raises_upstream_client_error(
    client: DifyClient,
) -> None:
    """Codex review-3 P2: Dify raises ``UnsupportedFileTypeError`` (HTTP
    415) when the uploaded file's extension/MIME isn't in the allow-list.
    That's a client mistake (they uploaded .exe or a non-text format the
    embedding pipeline can't process); pass the 415 through so the SDK
    can show the user a 'try a different file' message."""
    with respx.mock(base_url="http://dify.test") as m:
        m.post("/v1/datasets/ds-uuid/document/create-by-file").mock(
            return_value=httpx.Response(415, text="unsupported file type")
        )
        with pytest.raises(UpstreamClientError) as exc_info:
            await client.create_document_by_file(
                dataset_api_key="ds-key",
                dataset_id="ds-uuid",
                filename="malware.exe",
                content=b"MZ\x90\x00",
                content_type="application/x-msdownload",
            )
    assert exc_info.value.status_code == 415


@pytest.mark.asyncio
async def test_dataset_403_disabled_raises_upstream_client_error(
    client: DifyClient,
) -> None:
    """Codex review-3 P2: Dify returns 403 for archived/disabled datasets
    and per-tenant quota refusals on dataset ops. Caller can act on this
    (use a different dataset, request a quota bump), so it must surface
    as 403 ``upstream_invalid_request``, not 502."""
    with respx.mock(base_url="http://dify.test") as m:
        m.delete("/v1/datasets/ds-uuid/documents/doc-uuid").mock(
            return_value=httpx.Response(403, json={"message": "dataset disabled"})
        )
        with pytest.raises(UpstreamClientError) as exc_info:
            await client.delete_document(
                dataset_api_key="ds-key",
                dataset_id="ds-uuid",
                document_id="doc-uuid",
            )
    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_dataset_get_404_raises_upstream_client_error(
    client: DifyClient,
) -> None:
    """A client passing a wrong dataset UUID gets a 404 from Dify that
    surfaces to the SDK caller as 404 ``upstream_invalid_request``, not 502."""
    with respx.mock(base_url="http://dify.test") as m:
        m.get("/v1/datasets/abc-uuid").mock(
            return_value=httpx.Response(404, json={"message": "dataset not found"})
        )
        with pytest.raises(UpstreamClientError) as exc_info:
            await client.get_dataset(dataset_api_key="ds-key", dataset_id="abc-uuid")
    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_create_document_by_file_413_raises_upstream_client_error(
    client: DifyClient,
) -> None:
    """413 (payload too large) is a client mistake — they uploaded a file
    bigger than Dify's per-customer limit. Must surface as 413 so the
    client knows to chunk."""
    with respx.mock(base_url="http://dify.test") as m:
        m.post("/v1/datasets/ds-uuid/document/create-by-file").mock(
            return_value=httpx.Response(413, text="file too large")
        )
        with pytest.raises(UpstreamClientError) as exc_info:
            await client.create_document_by_file(
                dataset_api_key="ds-key",
                dataset_id="ds-uuid",
                filename="big.pdf",
                content=b"x" * 100,
                content_type="application/pdf",
            )
    assert exc_info.value.status_code == 413


@pytest.mark.asyncio
async def test_chat_blocking_4xx_still_502_unchanged(client: DifyClient) -> None:
    """Sanity: the dataset 4xx fix must NOT bleed into the chat path. Chat
    has different semantics — a 4xx from Dify chat-messages typically means
    the app_key is wrong (gateway misconfig), not a client mistake. Keep
    it as DifyUpstreamError (502)."""
    with respx.mock(base_url="http://dify.test") as m:
        m.post("/v1/chat-messages").mock(return_value=httpx.Response(404, text="app not found"))
        with pytest.raises(DifyUpstreamError):
            await client.chat_messages_blocking(app_key="app-x", query="q", user="u")


def _login_response_with_cookies(
    access_token: str = "access-abc",
    csrf_token: str = "csrf-xyz",
    *,
    host_prefixed: bool = False,
) -> httpx.Response:
    """Build a login response that mimics Dify's Set-Cookie behavior."""
    access_name = "__Host-access_token" if host_prefixed else "access_token"
    csrf_name = "__Host-csrf_token" if host_prefixed else "csrf_token"
    headers = [
        ("set-cookie", f"{access_name}={access_token}; Path=/; HttpOnly"),
        ("set-cookie", f"{csrf_name}={csrf_token}; Path=/"),
    ]
    return httpx.Response(200, headers=headers, json={"result": "success"})


@pytest.mark.asyncio
async def test_console_login_returns_session_from_cookies(client: DifyClient) -> None:
    with respx.mock(base_url="http://dify.test") as m:
        m.post("/console/api/login").mock(return_value=_login_response_with_cookies())
        session = await client.console_login("admin@x", "pw")

    assert isinstance(session, ConsoleSession)
    assert session.access_token == "access-abc"
    assert session.csrf_token == "csrf-xyz"


@pytest.mark.asyncio
async def test_console_login_sends_base64_encoded_password(client: DifyClient) -> None:
    """Regression: Dify's @decrypt_password_field decorator first base64-decodes
    the password before bcrypt-checking it. A plaintext password yields a 401
    ``Invalid encrypted data`` from the server because base64 decoding fails.
    """
    import base64
    import json as json_module

    plaintext = "S3cret!P@ss"
    expected_b64 = base64.b64encode(plaintext.encode("utf-8")).decode("ascii")

    with respx.mock(base_url="http://dify.test") as m:
        route = m.post("/console/api/login").mock(return_value=_login_response_with_cookies())
        await client.console_login("admin@x", plaintext)

    sent_body = json_module.loads(route.calls.last.request.read().decode())
    assert sent_body["password"] == expected_b64
    assert sent_body["password"] != plaintext  # paranoid double-check
    assert sent_body["email"] == "admin@x"     # email untouched


@pytest.mark.asyncio
async def test_console_login_supports_host_prefixed_cookies(client: DifyClient) -> None:
    """Dify uses __Host- prefix on cookies for HTTPS deploys without cookie domain."""
    with respx.mock(base_url="http://dify.test") as m:
        m.post("/console/api/login").mock(
            return_value=_login_response_with_cookies(
                access_token="hosted-a", csrf_token="hosted-c", host_prefixed=True
            )
        )
        session = await client.console_login("admin@x", "pw")

    assert session.access_token == "hosted-a"
    assert session.csrf_token == "hosted-c"
    # Cookie names must round-trip so Dify's _real_cookie_name extractors find them.
    assert session.access_token_cookie_name == "__Host-access_token"
    assert session.csrf_token_cookie_name == "__Host-csrf_token"


@pytest.mark.asyncio
async def test_console_calls_echo_host_prefixed_cookie_names(client: DifyClient) -> None:
    """Regression: subsequent console calls must use the same cookie names as login.

    Without this, HTTPS Dify deployments (which set ``__Host-csrf_token``)
    would see ``X-CSRF-Token`` header valued correctly but the
    ``csrf_token`` cookie name unrecognised, failing CSRF check (401).
    """
    session = ConsoleSession(
        access_token="acc-secure",
        csrf_token="csrf-secure",
        access_token_cookie_name="__Host-access_token",
        csrf_token_cookie_name="__Host-csrf_token",
    )
    with respx.mock(base_url="http://dify.test") as m:
        route = m.post("/console/api/apps/imports").mock(
            return_value=httpx.Response(200, json={"app_id": "a-1"})
        )
        await client.console_import_app(session, "yaml: ...")

    cookie_hdr = route.calls.last.request.headers.get("cookie", "")
    # Both cookies must use the host-prefixed name on the wire.
    assert "__Host-access_token=acc-secure" in cookie_hdr
    assert "__Host-csrf_token=csrf-secure" in cookie_hdr
    # Header name itself is fixed; only cookie names vary.
    assert route.calls.last.request.headers["x-csrf-token"] == "csrf-secure"


@pytest.mark.asyncio
async def test_console_login_missing_cookies_raises(client: DifyClient) -> None:
    """If Dify's response lacks the expected cookies, surface a clear error."""
    with respx.mock(base_url="http://dify.test") as m:
        m.post("/console/api/login").mock(
            return_value=httpx.Response(200, json={"result": "success"})  # no Set-Cookie
        )
        with pytest.raises(DifyUpstreamError, match="did not set expected cookies"):
            await client.console_login("admin@x", "pw")


@pytest.mark.asyncio
async def test_console_import_app_sends_csrf_header_and_cookies(client: DifyClient) -> None:
    session = ConsoleSession(access_token="a-tok", csrf_token="c-tok")
    with respx.mock(base_url="http://dify.test") as m:
        route = m.post("/console/api/apps/imports").mock(
            return_value=httpx.Response(200, json={"app_id": "app-uuid-1"})
        )
        app_id = await client.console_import_app(session, "yaml: ...")

    assert app_id == "app-uuid-1"
    sent = route.calls.last.request
    assert sent.headers["x-csrf-token"] == "c-tok"
    assert sent.headers["authorization"] == "Bearer a-tok"
    # httpx normalizes the Cookie header; both names should appear in it.
    cookie_hdr = sent.headers.get("cookie", "")
    assert "access_token=a-tok" in cookie_hdr
    assert "csrf_token=c-tok" in cookie_hdr


@pytest.mark.asyncio
async def test_console_import_app_accepts_id_field(client: DifyClient) -> None:
    """Some Dify versions return ``id`` instead of ``app_id``."""
    session = ConsoleSession(access_token="a", csrf_token="c")
    with respx.mock(base_url="http://dify.test") as m:
        m.post("/console/api/apps/imports").mock(
            return_value=httpx.Response(200, json={"id": "app-uuid-2"})
        )
        app_id = await client.console_import_app(session, "yaml: ...")
    assert app_id == "app-uuid-2"


@pytest.mark.asyncio
async def test_console_create_app_api_key(client: DifyClient) -> None:
    session = ConsoleSession(access_token="a", csrf_token="c")
    with respx.mock(base_url="http://dify.test") as m:
        route = m.post("/console/api/apps/app-uuid-1/api-keys").mock(
            return_value=httpx.Response(200, json={"token": "app-token-abc"})
        )
        token = await client.console_create_app_api_key(session, "app-uuid-1")
    assert token == "app-token-abc"
    assert route.calls.last.request.headers["x-csrf-token"] == "c"


@pytest.mark.asyncio
async def test_console_delete_app_treats_404_as_idempotent(client: DifyClient) -> None:
    session = ConsoleSession(access_token="a", csrf_token="c")
    with respx.mock(base_url="http://dify.test") as m:
        m.delete("/console/api/apps/app-uuid-gone").mock(return_value=httpx.Response(404))
        await client.console_delete_app(session, "app-uuid-gone")


@pytest.mark.asyncio
async def test_console_delete_app_raises_on_other_failures(client: DifyClient) -> None:
    session = ConsoleSession(access_token="a", csrf_token="c")
    with respx.mock(base_url="http://dify.test") as m:
        m.delete("/console/api/apps/app-uuid-1").mock(return_value=httpx.Response(500))
        with pytest.raises(DifyUpstreamError):
            await client.console_delete_app(session, "app-uuid-1")


@pytest.mark.asyncio
async def test_open_chat_stream_yields_lines(client: DifyClient) -> None:
    body = b'data: {"event":"message","answer":"a"}\n\ndata: {"event":"message_end"}\n\n'
    with respx.mock(base_url="http://dify.test") as m:
        m.post("/v1/chat-messages").mock(
            return_value=httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body)
        )
        async with client.open_chat_stream(app_key="app-x", query="q", user="u") as lines:
            collected = [line async for line in lines]

    joined = "\n".join(collected)
    assert "message" in joined
    assert "message_end" in joined


@pytest.mark.asyncio
async def test_open_chat_stream_raises_before_yielding_on_5xx(client: DifyClient) -> None:
    """Regression: pre-flight error must surface at context entry, not iteration."""
    with respx.mock(base_url="http://dify.test") as m:
        m.post("/v1/chat-messages").mock(return_value=httpx.Response(503, text="dify down"))
        with pytest.raises(DifyUpstreamError, match="503"):
            async with client.open_chat_stream(app_key="app-x", query="q", user="u"):
                pass  # we never get here


@pytest.mark.asyncio
async def test_open_chat_stream_raises_on_connect_timeout(client: DifyClient) -> None:
    with respx.mock(base_url="http://dify.test") as m:
        m.post("/v1/chat-messages").mock(side_effect=httpx.ConnectTimeout("connect timeout"))
        with pytest.raises(DifyTimeoutError):
            async with client.open_chat_stream(app_key="app-x", query="q", user="u"):
                pass
