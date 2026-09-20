"""Durable Garmin token storage backed by a Google Cloud Storage object.

garminconnect keeps a DI access token that lives roughly 27 hours and refreshes
it automatically before a request when it is about to expire. The refreshed
tokens are written back to the client's tokenstore path -- which on Cloud Run is
a container filesystem that disappears on the next cold start. Every new
instance would then fall back to whatever static token it was deployed with, and
once that one aged out the server would stop authenticating entirely.

Pointing the store at a GCS object gives those refreshes somewhere durable to
land, so instances hand the current token to each other instead of each starting
from the deployed snapshot.

Enable it by setting ``GARMIN_TOKENS_GCS`` to a ``gs://bucket/object.json`` URI.
Unset, every function here is a no-op and the server behaves exactly as before.
"""

import contextlib
import os
import sys

# Sentinel handed to garminconnect as the client's tokenstore path. The library
# only checks it for truthiness before calling dump(), and our wrapper routes
# that call to GCS instead of the filesystem.
_GCS_SENTINEL = "__garmin_mcp_gcs_token_store__"


def gcs_uri() -> str | None:
    """The configured GCS token URI, or None when the feature is off."""
    return os.getenv("GARMIN_TOKENS_GCS") or None


class GcsTokenStore:
    """Read/write the token JSON held in a single GCS object.

    Writes use generation preconditions so two instances refreshing at the same
    moment cannot interleave and leave a torn or stale object behind: the loser
    of the race simply re-reads and keeps the winner's tokens.
    """

    def __init__(self, uri: str):
        if not uri.startswith("gs://"):
            raise ValueError(f"GARMIN_TOKENS_GCS must be a gs:// URI, got {uri!r}")
        bucket, _, name = uri[len("gs://") :].partition("/")
        if not bucket or not name:
            raise ValueError(
                f"GARMIN_TOKENS_GCS must name a bucket and an object, got {uri!r}"
            )
        self.uri = uri
        self.bucket_name = bucket
        self.object_name = name
        self._generation: int | None = None
        self._client = None

    def _blob(self):
        if self._client is None:
            # Imported lazily so the package still works without the optional
            # google-cloud-storage dependency when the feature is unused.
            from google.cloud import storage

            self._client = storage.Client()
        return self._client.bucket(self.bucket_name).blob(self.object_name)

    def read(self) -> str | None:
        """Return the stored token JSON, or None if the object does not exist."""
        from google.cloud.exceptions import NotFound

        blob = self._blob()
        try:
            data = blob.download_as_text()
        except NotFound:
            self._generation = None
            return None
        self._generation = blob.generation
        return data

    def write(self, token_json: str) -> bool:
        """Store token JSON. Returns False if another instance got there first."""
        from google.api_core.exceptions import PreconditionFailed

        blob = self._blob()
        # generation 0 means "only if absent", which keeps the seeding path from
        # clobbering tokens another instance already refreshed.
        precondition = 0 if self._generation is None else self._generation
        try:
            blob.upload_from_string(
                token_json,
                content_type="application/json",
                if_generation_match=precondition,
            )
        except PreconditionFailed:
            # Someone else wrote a newer token. Re-sync so the next write builds
            # on theirs rather than retrying against a generation that is gone.
            self.read()
            return False
        self._generation = blob.generation
        return True

    def attach(self, garmin) -> None:
        """Route the client's token persistence to this store.

        garminconnect calls ``client.dump(client._tokenstore_path)`` after every
        successful token refresh. Wrapping dump is what turns that periodic,
        otherwise-discarded write into a durable one.
        """
        client = garmin.client
        original_dump = client.dump
        store = self

        def dump(path=None, *args, **kwargs):
            if path is not None and path != _GCS_SENTINEL:
                # A caller asked for a real file as well; honour it, but never
                # let a filesystem problem cost us the GCS write.
                with contextlib.suppress(Exception):
                    original_dump(path, *args, **kwargs)
            try:
                store.write(client.dumps())
            except Exception as err:  # pragma: no cover - network failure path
                print(
                    f"Warning: could not persist refreshed Garmin tokens to {store.uri}: {err}",
                    file=sys.stderr,
                )

        client.dump = dump
        client._tokenstore_path = _GCS_SENTINEL
