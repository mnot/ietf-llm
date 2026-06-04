"""S3-compatible object store for the cloud blob plane.

`S3BlobStore` implements the `BlobStore` interface against any S3-compatible
service (AWS S3, Cloudflare R2, MinIO) using `boto3` (the `[s3]` extra). It is
addressed by an `IETF_LLM_BLOB_DIR` locator of the form `s3://<bucket>/<prefix>`;
for a non-AWS endpoint (R2, MinIO) set `IETF_LLM_BLOB_ENDPOINT_URL`. Credentials
come from the standard AWS environment / instance-role chain (a secret — the
environment only). Only whole-object operations are used, so it needs no
store-special features. See `docs/storage.md`.
"""

from __future__ import annotations

import os
from typing import List, Optional, Tuple, cast

import boto3  # type: ignore[import-untyped]
from botocore.exceptions import ClientError  # type: ignore[import-untyped]

from .corpus_blobs import BlobStore, _safe_key

_NOT_FOUND = ("404", "NoSuchKey", "NotFound")


def _parse_locator(locator: str) -> Tuple[str, str]:
    """`s3://bucket/prefix...` → (bucket, prefix); prefix may be empty."""
    if not locator.startswith("s3://"):
        raise ValueError(
            f"invalid S3 locator (expected s3://bucket[/prefix]): {locator!r}"
        )
    bucket, _, prefix = locator[len("s3://") :].partition("/")
    if not bucket:
        raise ValueError(f"invalid S3 locator (no bucket): {locator!r}")
    return bucket, prefix.strip("/")


class S3BlobStore(BlobStore):
    """`BlobStore` over an S3-compatible bucket. Keys are stored under the
    locator's prefix; the public key space is the same base-relative one as
    `FileBlobStore`."""

    def __init__(self, locator: str, endpoint_url: Optional[str] = None) -> None:
        self._bucket, self._prefix = _parse_locator(locator)
        endpoint = endpoint_url or os.environ.get("IETF_LLM_BLOB_ENDPOINT_URL") or None
        self._s3 = boto3.client("s3", endpoint_url=endpoint)

    def _key(self, key: str) -> str:
        safe = _safe_key(key)
        return f"{self._prefix}/{safe}" if self._prefix else safe

    def _prefixed(self, prefix: str) -> str:
        return f"{self._prefix}/{prefix}" if self._prefix else prefix

    def _strip(self, s3_key: str) -> str:
        head = f"{self._prefix}/" if self._prefix else ""
        return s3_key[len(head) :] if s3_key.startswith(head) else s3_key

    def put(self, key: str, data: bytes) -> None:
        self._s3.put_object(Bucket=self._bucket, Key=self._key(key), Body=data)

    def get(self, key: str) -> bytes:
        obj = self._s3.get_object(Bucket=self._bucket, Key=self._key(key))
        return cast(bytes, obj["Body"].read())

    def exists(self, key: str) -> bool:
        try:
            self._s3.head_object(Bucket=self._bucket, Key=self._key(key))
            return True
        except ClientError as err:
            if err.response.get("Error", {}).get("Code") in _NOT_FOUND:
                return False
            raise

    def list_prefix(self, prefix: str) -> List[str]:
        keys: List[str] = []
        paginator = self._s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(
            Bucket=self._bucket, Prefix=self._prefixed(prefix)
        ):
            for obj in page.get("Contents", []):
                keys.append(self._strip(obj["Key"]))
        return sorted(keys)

    def materialise_prefix(self, prefix: str, dest_dir: str) -> None:
        strip = prefix.rstrip("/") + "/"
        for key in self.list_prefix(prefix):
            if not key.startswith(strip):
                continue
            dest = os.path.join(dest_dir, key[len(strip) :])
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with open(dest, "wb") as handle:
                handle.write(self.get(key))
