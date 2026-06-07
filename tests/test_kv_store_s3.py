"""Tests for the S3-backed KvStore against moto, plus precondition handling.

The compare-and-swap *rejection* path is exercised with a stub that raises
PreconditionFailed rather than against moto, which does not reliably enforce the
conditional-write headers — confirming real CAS on R2 is an integration item
(issue #83). What is tested here is the translation: a 412 becomes a `None`
return, a 404 becomes a `None` get, and the ETag is carried as the token.
"""

from __future__ import annotations

from typing import Iterator

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

from ietf_llm.kv_store import ABSENT
from ietf_llm.kv_store_s3 import S3KvStore
from ietf_llm.s3_backend import S3Bucket


@pytest.fixture
def bucket(monkeypatch: pytest.MonkeyPatch) -> Iterator[S3Bucket]:
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.delenv("IETF_LLM_STORE_ENDPOINT_URL", raising=False)
    with mock_aws():
        boto3.client("s3").create_bucket(Bucket="test-bucket")
        yield S3Bucket("s3://test-bucket/ctl")


def test_put_get_roundtrip_carries_etag(bucket: S3Bucket) -> None:
    kv = S3KvStore(bucket)
    assert kv.get("corpora/tls/pointer") is None  # 404 → None
    token = kv.put("corpora/tls/pointer", b"v1")
    assert token is not None
    record = kv.get("corpora/tls/pointer")
    assert record is not None
    assert record[0] == b"v1"
    assert record[1] == token  # the ETag is the opaque version token


def test_conditional_put_success_paths(bucket: S3Bucket) -> None:
    kv = S3KvStore(bucket)
    created = kv.put("k", b"1", expect=ABSENT)
    assert created is not None
    updated = kv.put("k", b"2", expect=created)
    assert updated is not None
    assert kv.get("k")[0] == b"2"  # type: ignore[index]


def test_delete_idempotent(bucket: S3Bucket) -> None:
    kv = S3KvStore(bucket)
    kv.put("k", b"1")
    kv.delete("k")
    assert kv.get("k") is None
    kv.delete("k")  # no-op


def test_list_children_via_delimiter(bucket: S3Bucket) -> None:
    kv = S3KvStore(bucket)
    kv.put("corpora/tls/pointer", b"v1")
    kv.put("corpora/tls/versions/v1/a", b"x")
    kv.put("corpora/httpbis/pointer", b"v9")
    kv.put("fleet/slots", b"{}")
    assert kv.list_children("corpora/") == ["httpbis", "tls"]
    assert kv.list_children("corpora/tls/") == ["pointer", "versions"]


class _PreconditionClient:
    """A stub S3 client whose put rejects the precondition (412)."""

    def put_object(self, **_kw: object) -> None:
        raise ClientError(
            {"Error": {"Code": "PreconditionFailed", "Message": "no"}}, "PutObject"
        )


def test_failed_precondition_returns_none(bucket: S3Bucket) -> None:
    kv = S3KvStore(bucket)
    bucket.client = _PreconditionClient()  # type: ignore[assignment]
    assert kv.put("k", b"1", expect=ABSENT) is None
    assert kv.put("k", b"1", expect='"deadbeef"') is None
