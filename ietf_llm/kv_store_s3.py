"""S3-backed `KvStore`: the control plane's compare-and-swap over object-store
conditional writes.

`put` maps the precondition onto S3 headers — `ABSENT` → `If-None-Match: *`
(create-only), a version token → `If-Match: <etag>` (compare-and-swap) — and
turns a 412 PreconditionFailed into a `None` return, so a lost CAS reads the
same as on the in-memory store. The object's **ETag is the opaque version
token**. `delete` is unconditional (the control plane never needs a conditional
delete; a lease is released by stamping it expired, not removing it).
`list_children` is a delimiter listing, so enumerating corpora does not scan the
version blobs beneath them. Shares one `S3Bucket` with the blob plane. See
`docs/storage.md`.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, cast

from botocore.exceptions import ClientError  # type: ignore[import-untyped]

from .kv_store import ABSENT, ANY, KvStore, Record
from .s3_backend import NOT_FOUND, PRECONDITION_FAILED, S3Bucket


class S3KvStore(KvStore):
    """A `KvStore` over an S3-compatible bucket, using conditional writes for
    compare-and-swap. Keys live under the shared bucket's prefix."""

    def __init__(self, bucket: S3Bucket) -> None:
        self._b = bucket

    def get(self, key: str) -> Optional[Record]:
        bkt = self._b
        try:
            obj = bkt.call(
                "read an object",
                lambda: bkt.client.get_object(Bucket=bkt.bucket, Key=bkt.key(key)),
            )
        except ClientError as err:
            if err.response.get("Error", {}).get("Code") in NOT_FOUND:
                return None
            raise
        return cast(bytes, obj["Body"].read()), str(obj["ETag"])

    def put(self, key: str, value: bytes, *, expect: object = ANY) -> Optional[str]:
        bkt = self._b
        params: Dict[str, Any] = {
            "Bucket": bkt.bucket,
            "Key": bkt.key(key),
            "Body": value,
        }
        if expect is ABSENT:
            params["IfNoneMatch"] = "*"
        elif expect is not ANY:
            params["IfMatch"] = cast(str, expect)
        try:
            resp = bkt.call("write an object", lambda: bkt.client.put_object(**params))
        except ClientError as err:
            if err.response.get("Error", {}).get("Code") in PRECONDITION_FAILED:
                return None
            raise
        return str(resp["ETag"])

    def delete(self, key: str) -> None:
        bkt = self._b
        bkt.call(
            "delete an object",
            lambda: bkt.client.delete_object(Bucket=bkt.bucket, Key=bkt.key(key)),
        )

    def list_children(self, prefix: str) -> List[str]:
        bkt = self._b
        full = bkt.prefixed(prefix)

        def _list() -> List[str]:
            names = set()
            paginator = bkt.client.get_paginator("list_objects_v2")
            for page in paginator.paginate(
                Bucket=bkt.bucket, Prefix=full, Delimiter="/"
            ):
                # Sub-"directories" come back as CommonPrefixes; objects sitting
                # directly at this level (e.g. `pointer`) come back as Contents.
                for common in page.get("CommonPrefixes", []):
                    name = common["Prefix"][len(full) :].rstrip("/")
                    if name:
                        names.add(name)
                for obj in page.get("Contents", []):
                    name = obj["Key"][len(full) :]
                    if name:
                        names.add(name)
            return sorted(names)

        return bkt.call("list objects", _list)
