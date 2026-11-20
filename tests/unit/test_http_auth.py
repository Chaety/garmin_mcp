"""Unit tests for the HTTP shared-secret middleware."""

import json
import os

import pytest
from unittest.mock import patch

from garmin_mcp import http_auth

KEY = "s3cr3t"


async def _ok_app(scope, receive, send):
    """Minimal downstream ASGI app; records that it was reached."""
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"reached"})


class Recorder:
    """Collects the ASGI messages the middleware sends."""

    def __init__(self):
        self.messages = []

    async def __call__(self, message):
        self.messages.append(message)

    @property
    def status(self):
        return self.messages[0]["status"]

    @property
    def body(self):
        return b"".join(m.get("body", b"") for m in self.messages[1:])

    @property
    def headers(self):
        return {k.decode().lower(): v.decode() for k, v in self.messages[0]["headers"]}


async def _noop_receive():  # pragma: no cover - never consumed by these paths
    return {"type": "http.request", "body": b"", "more_body": False}


def http_scope(path="/mcp", headers=None):
    return {
        "type": "http",
        "path": path,
        "headers": [(k.encode(), v.encode()) for k, v in (headers or {}).items()],
    }


async def call(middleware, scope):
    recorder = Recorder()
    await middleware(scope, _noop_receive, recorder)
    return recorder


class TestApiKeyResolution:
    def test_unset_means_no_authentication(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("GARMIN_MCP_API_KEY", None)
            assert http_auth.api_key() is None

    def test_empty_is_treated_as_unset(self):
        """An empty value must not become a key every caller can guess."""
        with patch.dict(os.environ, {"GARMIN_MCP_API_KEY": ""}):
            assert http_auth.api_key() is None

    def test_value_is_returned(self):
        with patch.dict(os.environ, {"GARMIN_MCP_API_KEY": KEY}):
            assert http_auth.api_key() == KEY


@pytest.mark.anyio
class TestApiKeyMiddleware:
    @pytest.fixture
    def middleware(self):
        return http_auth.ApiKeyMiddleware(_ok_app, KEY)

    async def test_correct_key_in_x_api_key_passes(self, middleware):
        result = await call(middleware, http_scope(headers={"x-api-key": KEY}))
        assert result.status == 200
        assert result.body == b"reached"

    async def test_correct_key_as_bearer_passes(self, middleware):
        result = await call(
            middleware, http_scope(headers={"authorization": f"Bearer {KEY}"})
        )
        assert result.status == 200

    async def test_bearer_scheme_is_case_insensitive(self, middleware):
        result = await call(
            middleware, http_scope(headers={"authorization": f"bearer {KEY}"})
        )
        assert result.status == 200

    async def test_missing_key_is_rejected(self, middleware):
        result = await call(middleware, http_scope())
        assert result.status == 401
        assert json.loads(result.body)["error"] == "unauthorized"

    async def test_wrong_key_is_rejected(self, middleware):
        result = await call(middleware, http_scope(headers={"x-api-key": "nope"}))
        assert result.status == 401

    async def test_key_as_prefix_is_rejected(self, middleware):
        """A truncated key must not pass; compare_digest is not a prefix match."""
        result = await call(middleware, http_scope(headers={"x-api-key": KEY[:3]}))
        assert result.status == 401

    async def test_basic_auth_is_not_accepted(self, middleware):
        """Only Bearer counts, so a Basic header cannot smuggle the key through."""
        result = await call(
            middleware, http_scope(headers={"authorization": f"Basic {KEY}"})
        )
        assert result.status == 401

    async def test_rejection_names_the_scheme(self, middleware):
        result = await call(middleware, http_scope())
        assert "bearer" in result.headers["www-authenticate"].lower()

    async def test_health_endpoint_is_exempt(self, middleware):
        """Probes run before anything can hold a credential."""
        result = await call(middleware, http_scope(path="/healthz"))
        assert result.status == 200

    async def test_exempt_paths_are_configurable(self):
        middleware = http_auth.ApiKeyMiddleware(_ok_app, KEY, exempt_paths=("/up",))
        assert (await call(middleware, http_scope(path="/up"))).status == 200
        assert (await call(middleware, http_scope(path="/healthz"))).status == 401

    async def test_non_http_scopes_pass_through(self):
        """Lifespan startup carries no headers and must not be rejected."""
        seen = []

        async def lifespan_app(scope, receive, send):
            seen.append(scope["type"])

        middleware = http_auth.ApiKeyMiddleware(lifespan_app, KEY)
        await middleware({"type": "lifespan"}, _noop_receive, Recorder())
        assert seen == ["lifespan"]


class TestProtect:
    def test_wraps_both_app_factories(self):
        """Both HTTP transports must end up guarded, not just the default one."""

        class FakeFastMCP:
            def streamable_http_app(self):
                return "streamable"

            def sse_app(self, mount_path=None):
                return "sse"

        fastmcp = FakeFastMCP()
        http_auth.protect(fastmcp, KEY)

        for factory in (fastmcp.streamable_http_app(), fastmcp.sse_app()):
            assert isinstance(factory, http_auth.ApiKeyMiddleware)

    def test_wrapping_survives_repeated_calls(self):
        """FastMCP builds a fresh app inside run(); each one must be wrapped."""

        class FakeFastMCP:
            def streamable_http_app(self):
                return object()

        fastmcp = FakeFastMCP()
        http_auth.protect(fastmcp, KEY)
        assert isinstance(fastmcp.streamable_http_app(), http_auth.ApiKeyMiddleware)
        assert isinstance(fastmcp.streamable_http_app(), http_auth.ApiKeyMiddleware)
