"""Tests for Cloud Storage token loading.

The Cloud Storage client is faked so these run without credentials or network.
"""

import base64
import json
import os

import pytest

from garmin_mcp import gcs_tokens

OAUTH1 = {"oauth_token": "t1", "oauth_token_secret": "s1", "domain": "garmin.com"}
OAUTH2 = {"access_token": "a2", "refresh_token": "r2", "expires_in": 3600}


class FakeBlob:
    def __init__(self, store, name):
        self._store = store
        self._name = name

    def exists(self):
        return self._name in self._store

    def download_as_text(self):
        return self._store[self._name]

    def upload_from_string(self, data, content_type=None):
        self._store[self._name] = data


class FakeBucket:
    def __init__(self, store):
        self._store = store

    def blob(self, name):
        return FakeBlob(self._store, name)


class FakeClient:
    def __init__(self, store, expected_bucket=None):
        self._store = store
        self._expected_bucket = expected_bucket
        self.requested_buckets = []

    def bucket(self, name):
        self.requested_buckets.append(name)
        if self._expected_bucket is not None:
            assert name == self._expected_bucket
        return FakeBucket(self._store)


@pytest.fixture
def store(monkeypatch):
    """An in-memory stand-in for a bucket, keyed by object name."""
    contents = {}
    monkeypatch.setattr(gcs_tokens, "_client", lambda: FakeClient(contents))
    return contents


def read_tokens(token_dir):
    out = {}
    for filename in (gcs_tokens.OAUTH1_FILE, gcs_tokens.OAUTH2_FILE):
        with open(os.path.join(token_dir, filename)) as handle:
            out[filename] = json.load(handle)
    return out


# --- parse_gcs_uri ---------------------------------------------------------


@pytest.mark.parametrize(
    "uri,expected",
    [
        ("gs://b/tokens.json", ("b", "tokens.json", False)),
        ("gs://b/nested/tokens.json", ("b", "nested/tokens.json", False)),
        ("gs://b/prefix/", ("b", "prefix/", True)),
    ],
)
def test_parse_gcs_uri(uri, expected):
    assert gcs_tokens.parse_gcs_uri(uri) == expected


@pytest.mark.parametrize("uri", ["s3://b/k", "gs://bucket-only", "gs://", "/local/path"])
def test_parse_gcs_uri_rejects_bad_input(uri):
    with pytest.raises(ValueError):
        gcs_tokens.parse_gcs_uri(uri)


# --- download --------------------------------------------------------------


def test_download_bundle_writes_both_token_files(store, tmp_path):
    store["tokens.json"] = json.dumps(
        {"oauth1_token": OAUTH1, "oauth2_token": OAUTH2}
    )

    assert gcs_tokens.download_tokens("gs://b/tokens.json", str(tmp_path)) is True
    assert read_tokens(tmp_path) == {
        gcs_tokens.OAUTH1_FILE: OAUTH1,
        gcs_tokens.OAUTH2_FILE: OAUTH2,
    }


def test_download_accepts_base64_bundle(store, tmp_path):
    raw = json.dumps({"oauth1_token": OAUTH1, "oauth2_token": OAUTH2})
    store["tokens.json"] = base64.b64encode(raw.encode()).decode()

    assert gcs_tokens.download_tokens("gs://b/tokens.json", str(tmp_path)) is True
    assert read_tokens(tmp_path)[gcs_tokens.OAUTH1_FILE] == OAUTH1


def test_download_accepts_suffixed_keys(store, tmp_path):
    """A bundle keyed by garth's filenames loads the same way."""
    store["tokens.json"] = json.dumps(
        {gcs_tokens.OAUTH1_FILE: OAUTH1, gcs_tokens.OAUTH2_FILE: OAUTH2}
    )

    assert gcs_tokens.download_tokens("gs://b/tokens.json", str(tmp_path)) is True
    assert read_tokens(tmp_path)[gcs_tokens.OAUTH2_FILE] == OAUTH2


def test_download_from_prefix(store, tmp_path):
    store[f"tok/{gcs_tokens.OAUTH1_FILE}"] = json.dumps(OAUTH1)
    store[f"tok/{gcs_tokens.OAUTH2_FILE}"] = json.dumps(OAUTH2)

    assert gcs_tokens.download_tokens("gs://b/tok/", str(tmp_path)) is True
    assert read_tokens(tmp_path) == {
        gcs_tokens.OAUTH1_FILE: OAUTH1,
        gcs_tokens.OAUTH2_FILE: OAUTH2,
    }


def test_download_creates_missing_directory(store, tmp_path):
    store["tokens.json"] = json.dumps(
        {"oauth1_token": OAUTH1, "oauth2_token": OAUTH2}
    )
    target = tmp_path / "does" / "not" / "exist"

    assert gcs_tokens.download_tokens("gs://b/tokens.json", str(target)) is True
    assert read_tokens(target)[gcs_tokens.OAUTH1_FILE] == OAUTH1


def test_download_expands_user_home(store, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    store["tokens.json"] = json.dumps(
        {"oauth1_token": OAUTH1, "oauth2_token": OAUTH2}
    )

    assert gcs_tokens.download_tokens("gs://b/tokens.json", "~/.garminconnect") is True
    assert (tmp_path / ".garminconnect" / gcs_tokens.OAUTH1_FILE).exists()


@pytest.mark.parametrize(
    "payload",
    [
        "not json and not base64 !!",
        json.dumps({"oauth1_token": OAUTH1}),  # oauth2 missing
        json.dumps(["a", "list"]),
    ],
)
def test_download_returns_false_on_bad_payload(store, tmp_path, payload):
    """A malformed object must not raise: startup falls back to normal login."""
    store["tokens.json"] = payload

    assert gcs_tokens.download_tokens("gs://b/tokens.json", str(tmp_path)) is False
    assert not os.path.exists(os.path.join(tmp_path, gcs_tokens.OAUTH1_FILE))


def test_download_returns_false_when_object_absent(store, tmp_path):
    assert gcs_tokens.download_tokens("gs://b/missing.json", str(tmp_path)) is False


def test_download_returns_false_on_bad_uri(store, tmp_path):
    assert gcs_tokens.download_tokens("not-a-gs-uri", str(tmp_path)) is False


def test_download_returns_false_when_client_unavailable(monkeypatch, tmp_path):
    def boom():
        raise RuntimeError("google-cloud-storage is not installed")

    monkeypatch.setattr(gcs_tokens, "_client", boom)
    assert gcs_tokens.download_tokens("gs://b/tokens.json", str(tmp_path)) is False


def test_download_reports_failure_on_stderr(store, tmp_path, capsys):
    gcs_tokens.download_tokens("gs://b/missing.json", str(tmp_path))
    assert "ERROR" in capsys.readouterr().err


# --- upload ----------------------------------------------------------------


def test_upload_writes_bundle(store, tmp_path):
    gcs_tokens._write_token_files(
        {gcs_tokens.OAUTH1_FILE: OAUTH1, gcs_tokens.OAUTH2_FILE: OAUTH2}, str(tmp_path)
    )

    assert gcs_tokens.upload_tokens("gs://b/tokens.json", str(tmp_path)) is True
    assert json.loads(store["tokens.json"]) == {
        "oauth1_token": OAUTH1,
        "oauth2_token": OAUTH2,
    }


def test_upload_writes_prefix_layout(store, tmp_path):
    gcs_tokens._write_token_files(
        {gcs_tokens.OAUTH1_FILE: OAUTH1, gcs_tokens.OAUTH2_FILE: OAUTH2}, str(tmp_path)
    )

    assert gcs_tokens.upload_tokens("gs://b/tok/", str(tmp_path)) is True
    assert json.loads(store[f"tok/{gcs_tokens.OAUTH1_FILE}"]) == OAUTH1
    assert json.loads(store[f"tok/{gcs_tokens.OAUTH2_FILE}"]) == OAUTH2


def test_upload_returns_false_when_local_tokens_missing(store, tmp_path):
    assert gcs_tokens.upload_tokens("gs://b/tokens.json", str(tmp_path)) is False
    assert store == {}


def test_round_trip_bundle_then_prefix(store, tmp_path):
    """Tokens uploaded as a bundle come back unchanged."""
    source = tmp_path / "src"
    gcs_tokens._write_token_files(
        {gcs_tokens.OAUTH1_FILE: OAUTH1, gcs_tokens.OAUTH2_FILE: OAUTH2}, str(source)
    )
    gcs_tokens.upload_tokens("gs://b/tokens.json", str(source))

    restored = tmp_path / "dst"
    assert gcs_tokens.download_tokens("gs://b/tokens.json", str(restored)) is True
    assert read_tokens(restored) == read_tokens(source)
