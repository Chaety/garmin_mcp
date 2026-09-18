"""Load and persist Garmin OAuth tokens in Google Cloud Storage.

A serverless container gets an ephemeral filesystem: whatever is written to the
token directory is lost when the instance is recycled. That matters here because
a container cannot recover on its own — a fresh Garmin login needs an MFA code
typed at a terminal. Keeping the tokens in Cloud Storage lets every cold start
restore the session instead.

Set ``GARMIN_TOKENS_GCS`` to either form:

``gs://bucket/path/tokens.json``
    A single object holding both tokens, as
    ``{"oauth1_token": {...}, "oauth2_token": {...}}``. A base64-encoded copy of
    that same JSON is also accepted, matching the ``GARMINTOKENS_BASE64`` flow.

``gs://bucket/path/``
    A prefix holding garth's two files, ``oauth1_token.json`` and
    ``oauth2_token.json``, exactly as ``garth.Client.dump()`` writes them.

Requires the optional dependency::

    pip install 'garmin-mcp[gcs]'

The service account running the server needs ``storage.objects.get`` on the
bucket, plus ``storage.objects.create`` if tokens are uploaded back.
"""

import base64
import binascii
import json
import os
import sys

# garth's own filenames; a prefix-style URI stores these verbatim.
OAUTH1_FILE = "oauth1_token.json"
OAUTH2_FILE = "oauth2_token.json"

# Keys accepted inside a bundle object, with and without the .json suffix.
_BUNDLE_KEYS = (
    (OAUTH1_FILE, ("oauth1_token", OAUTH1_FILE)),
    (OAUTH2_FILE, ("oauth2_token", OAUTH2_FILE)),
)


def parse_gcs_uri(uri):
    """Split a ``gs://`` URI into bucket, path and whether it names a prefix.

    Args:
        uri: A ``gs://bucket/object`` or ``gs://bucket/prefix/`` URI.

    Returns:
        tuple[str, str, bool]: bucket, path, and True when the URI names a
        prefix (it ended in ``/``) rather than a single object.

    Raises:
        ValueError: if the URI is not a ``gs://`` URI naming a bucket and path.
    """
    if not uri.startswith("gs://"):
        raise ValueError(f"GARMIN_TOKENS_GCS must start with gs:// — got {uri!r}")

    remainder = uri[len("gs://") :]
    bucket, _, path = remainder.partition("/")
    if not bucket or not path:
        raise ValueError(
            f"GARMIN_TOKENS_GCS must name a bucket and a path — got {uri!r}"
        )

    is_prefix = path.endswith("/")
    return bucket, path, is_prefix


def _client():
    """Build a Cloud Storage client, explaining the extra if it is missing."""
    try:
        from google.cloud import storage
    except ImportError as exc:  # pragma: no cover - depends on install extras
        raise RuntimeError(
            "GARMIN_TOKENS_GCS is set but google-cloud-storage is not installed. "
            "Install it with: pip install 'garmin-mcp[gcs]'"
        ) from exc

    return storage.Client()


def _decode_bundle(text):
    """Parse a token bundle that may be JSON or base64-encoded JSON.

    Raises:
        ValueError: if the payload is neither, or is not a JSON object.
    """
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        try:
            decoded = base64.b64decode(text, validate=True).decode()
        except (binascii.Error, UnicodeDecodeError, ValueError):
            raise ValueError("token bundle is neither JSON nor base64-encoded JSON")
        payload = json.loads(decoded)

    if not isinstance(payload, dict):
        raise ValueError(
            f"token bundle must be a JSON object, got {type(payload).__name__}"
        )
    return payload


def _split_bundle(payload):
    """Map a bundle object to ``{filename: token dict}``.

    Raises:
        ValueError: if either token is missing from the bundle.
    """
    files = {}
    for filename, accepted in _BUNDLE_KEYS:
        for key in accepted:
            if key in payload:
                files[filename] = payload[key]
                break
        else:
            raise ValueError(
                f"token bundle has no {' or '.join(accepted)} key "
                f"(found: {', '.join(sorted(payload)) or 'nothing'})"
            )
    return files


def _write_token_files(files, dest_dir):
    """Write ``{filename: token dict}`` into the local token directory."""
    expanded = os.path.expanduser(dest_dir)
    os.makedirs(expanded, exist_ok=True)
    for filename, token in files.items():
        with open(os.path.join(expanded, filename), "w") as handle:
            json.dump(token, handle, indent=4)
    return expanded


def download_tokens(uri, dest_dir):
    """Restore Garmin tokens from Cloud Storage into ``dest_dir``.

    Never raises for an absent or malformed object: the caller falls back to its
    normal login path, which reports the usual authentication guidance. Every
    failure is logged to stderr so it is visible in the platform's logs.

    Args:
        uri: The ``gs://`` URI from GARMIN_TOKENS_GCS.
        dest_dir: Local token directory, as passed to ``Garmin.login()``.

    Returns:
        bool: True when both token files were written.
    """
    try:
        bucket_name, path, is_prefix = parse_gcs_uri(uri)
        bucket = _client().bucket(bucket_name)

        if is_prefix:
            files = {}
            for filename in (OAUTH1_FILE, OAUTH2_FILE):
                blob = bucket.blob(path + filename)
                if not blob.exists():
                    print(
                        f"ERROR: {uri}{filename} not found in Cloud Storage.",
                        file=sys.stderr,
                    )
                    return False
                files[filename] = json.loads(blob.download_as_text())
        else:
            blob = bucket.blob(path)
            if not blob.exists():
                print(f"ERROR: {uri} not found in Cloud Storage.", file=sys.stderr)
                return False
            files = _split_bundle(_decode_bundle(blob.download_as_text()))

        expanded = _write_token_files(files, dest_dir)
        print(f"Restored Garmin tokens from {uri} into '{expanded}'.", file=sys.stderr)
        return True

    except Exception as exc:  # noqa: BLE001 - startup must not crash on this
        print(f"ERROR: could not load Garmin tokens from {uri}: {exc}", file=sys.stderr)
        return False


def upload_tokens(uri, src_dir):
    """Persist the token files in ``src_dir`` back to Cloud Storage.

    Used after a fresh login so the next cold start can reuse the session. Like
    :func:`download_tokens`, failures are logged rather than raised — the server
    is already authenticated at this point and should keep running.

    Args:
        uri: The ``gs://`` URI from GARMIN_TOKENS_GCS.
        src_dir: Local token directory holding garth's two files.

    Returns:
        bool: True when the tokens were uploaded.
    """
    try:
        bucket_name, path, is_prefix = parse_gcs_uri(uri)
        expanded = os.path.expanduser(src_dir)

        tokens = {}
        for filename in (OAUTH1_FILE, OAUTH2_FILE):
            local_path = os.path.join(expanded, filename)
            if not os.path.exists(local_path):
                print(
                    f"ERROR: {local_path} missing; not uploading tokens to {uri}.",
                    file=sys.stderr,
                )
                return False
            with open(local_path) as handle:
                tokens[filename] = json.load(handle)

        bucket = _client().bucket(bucket_name)
        if is_prefix:
            for filename, token in tokens.items():
                bucket.blob(path + filename).upload_from_string(
                    json.dumps(token, indent=4), content_type="application/json"
                )
        else:
            bundle = {
                "oauth1_token": tokens[OAUTH1_FILE],
                "oauth2_token": tokens[OAUTH2_FILE],
            }
            bucket.blob(path).upload_from_string(
                json.dumps(bundle, indent=4), content_type="application/json"
            )

        print(f"Saved Garmin tokens to {uri}.", file=sys.stderr)
        return True

    except Exception as exc:  # noqa: BLE001 - a failed save must not end the run
        print(f"ERROR: could not save Garmin tokens to {uri}: {exc}", file=sys.stderr)
        return False
