"""Shared-secret authentication for the HTTP transports.

The MCP endpoint exposes full read and write access to a Garmin account and
performs no authentication of its own. That is tolerable while it is bound to
loopback, but not once it is published — self-hosting behind a home router or a
tunnel puts it on the open internet, where a URL is the only thing between a
stranger and the account.

Set ``GARMIN_MCP_API_KEY`` and every HTTP request must carry that secret, in
either header:

    Authorization: Bearer <key>
    X-API-Key: <key>

Unset, nothing here applies and the server behaves exactly as before, so the
stdio and loopback setups are unaffected.

This is a shared secret, not a login: it says the caller holds the key, nothing
more. Pair it with HTTPS — over plain HTTP the key travels in clear text and
anyone on the path can lift it.
"""

import hmac
import json
import os

# A health probe has to answer before the prober can hold any credential, and
# it discloses nothing but liveness.
DEFAULT_EXEMPT_PATHS = ("/healthz",)

_UNAUTHORIZED_BODY = json.dumps(
    {
        "error": "unauthorized",
        "detail": (
            "Send the server's key as 'Authorization: Bearer <key>' "
            "or 'X-API-Key: <key>'."
        ),
    }
).encode()


def api_key() -> str | None:
    """The configured shared secret, or None when authentication is off."""
    return os.getenv("GARMIN_MCP_API_KEY") or None


def _presented_key(headers: list[tuple[bytes, bytes]]) -> str | None:
    """Pull the caller's key out of either accepted header.

    Args:
        headers: ASGI raw headers, lowercase names to raw values.

    Returns:
        The key the caller presented, or None if neither header carried one.
    """
    for raw_name, raw_value in headers:
        name = raw_name.decode("latin-1").lower()
        value = raw_value.decode("latin-1").strip()

        if name == "x-api-key" and value:
            return value

        if name == "authorization":
            scheme, _, token = value.partition(" ")
            if scheme.lower() == "bearer" and token.strip():
                return token.strip()

    return None


class ApiKeyMiddleware:
    """ASGI middleware rejecting requests without the shared secret.

    Written against the raw ASGI interface rather than Starlette's
    ``BaseHTTPMiddleware``: that class buffers the response through a queue,
    which would defeat the streaming the MCP transports rely on.
    """

    def __init__(self, app, key: str, exempt_paths=DEFAULT_EXEMPT_PATHS):
        self._app = app
        self._key = key
        self._exempt_paths = tuple(exempt_paths)

    async def __call__(self, scope, receive, send):
        # Lifespan and websocket scopes carry no headers to check.
        if scope["type"] != "http" or scope.get("path") in self._exempt_paths:
            return await self._app(scope, receive, send)

        presented = _presented_key(scope.get("headers") or [])

        # compare_digest keeps the comparison constant-time, so a wrong key
        # cannot be refined one character at a time from response timings.
        if presented is not None and hmac.compare_digest(presented, self._key):
            return await self._app(scope, receive, send)

        await send(
            {
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(_UNAUTHORIZED_BODY)).encode()),
                    # Tells a client which scheme to retry with, per RFC 7235.
                    (b"www-authenticate", b'Bearer realm="garmin-mcp"'),
                ],
            }
        )
        await send({"type": "http.response.body", "body": _UNAUTHORIZED_BODY})


def protect(fastmcp, key: str) -> None:
    """Wrap ``fastmcp``'s HTTP app factories so every app they build is guarded.

    FastMCP builds a fresh Starlette app inside ``run()``, so middleware added
    to an app we build ourselves would be thrown away. Decorating the factories
    instead means the protection survives however the library chooses to serve,
    and leaves the run path itself untouched.
    """
    for factory_name in ("streamable_http_app", "sse_app"):
        original = getattr(fastmcp, factory_name, None)
        if original is None:  # pragma: no cover - depends on mcp version
            continue

        def guarded(*args, _original=original, **kwargs):
            return ApiKeyMiddleware(_original(*args, **kwargs), key)

        setattr(fastmcp, factory_name, guarded)
