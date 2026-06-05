"""Tests for the S3-compatible blob store, run against moto's in-process S3."""

from __future__ import annotations

from pathlib import Path
from typing import Iterator

import boto3
import pytest
from botocore.exceptions import ClientError, NoCredentialsError
from moto import mock_aws

from ietf_llm.corpus_blobs_s3 import S3AuthError, S3BlobStore


@pytest.fixture
def s3_bucket(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.delenv("IETF_LLM_BLOB_ENDPOINT_URL", raising=False)
    with mock_aws():
        boto3.client("s3").create_bucket(Bucket="test-bucket")
        yield


def test_put_get_exists(s3_bucket: None) -> None:
    store = S3BlobStore("s3://test-bucket/base")
    assert store.exists("tls/v1/a.md") is False
    store.put("tls/v1/a.md", b"hello")
    assert store.exists("tls/v1/a.md") is True
    assert store.get("tls/v1/a.md") == b"hello"


def test_list_prefix(s3_bucket: None) -> None:
    store = S3BlobStore("s3://test-bucket/base")
    store.put("tls/v1/a.md", b"1")
    store.put("tls/v1/sub/b.md", b"2")
    store.put("tls/v2/c.md", b"3")
    assert store.list_prefix("tls/v1") == ["tls/v1/a.md", "tls/v1/sub/b.md"]
    assert store.list_prefix("nope") == []


def test_materialise_prefix(s3_bucket: None, tmp_path: Path) -> None:
    store = S3BlobStore("s3://test-bucket/base")
    store.put("tls/v1/files/digests/index.md", b"idx")
    store.put("tls/v1/files/group.md", b"grp")
    dest = tmp_path / "scratch"
    store.materialise_prefix("tls/v1/files/", str(dest))
    assert (dest / "digests" / "index.md").read_bytes() == b"idx"
    assert (dest / "group.md").read_bytes() == b"grp"


def test_no_prefix_locator(s3_bucket: None) -> None:
    store = S3BlobStore("s3://test-bucket")
    store.put("k.md", b"v")
    assert store.get("k.md") == b"v"
    assert store.list_prefix("") == ["k.md"]


def test_unsafe_key_rejected(s3_bucket: None) -> None:
    store = S3BlobStore("s3://test-bucket/base")
    with pytest.raises(ValueError):
        store.put("a/../b", b"x")


def test_invalid_locator() -> None:
    with pytest.raises(ValueError):
        S3BlobStore("not-s3://x")


class _DenyClient:
    """A stub S3 client that rejects credentials, to exercise the auth guard."""

    def put_object(self, **_kw: object) -> None:
        raise ClientError(
            {"Error": {"Code": "AccessDenied", "Message": "denied"}}, "PutObject"
        )

    def get_object(self, **_kw: object) -> None:
        raise NoCredentialsError()


def test_access_denied_raises_actionable_auth_error(s3_bucket: None) -> None:
    store = S3BlobStore("s3://test-bucket/base")
    store._s3 = _DenyClient()  # type: ignore[assignment]
    with pytest.raises(S3AuthError) as exc:
        store.put("k.md", b"x")
    assert "AccessDenied" in str(exc.value)
    assert "AWS_ACCESS_KEY_ID" in str(exc.value)


def test_missing_credentials_raises_actionable_auth_error(s3_bucket: None) -> None:
    store = S3BlobStore("s3://test-bucket/base")
    store._s3 = _DenyClient()  # type: ignore[assignment]
    with pytest.raises(S3AuthError) as exc:
        store.get("k.md")
    assert "AWS_ACCESS_KEY_ID" in str(exc.value)
