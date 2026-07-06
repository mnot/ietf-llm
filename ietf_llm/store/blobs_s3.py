"""S3-compatible object store for the cloud blob plane.

`S3BlobStore` implements the `BlobStore` interface against any S3-compatible
service (AWS S3, Cloudflare R2, MinIO) using `boto3` (the `[s3]` extra). It
shares one `S3Bucket` (`s3_backend`) — and so one boto3 client, endpoint, and
credential set — with the control plane's `S3KvStore`. Addressed by an
`IETF_LLM_STORE_URL` locator of the form `s3://<bucket>/<prefix>`; for a non-AWS
endpoint (R2, MinIO) set `IETF_LLM_STORE_ENDPOINT_URL`. Credentials come from the
standard AWS environment / instance-role chain (a secret — the environment
only). Only whole-object operations are used, so it needs no store-special
features. See `docs/storage.md`.
"""

from __future__ import annotations

import os
from functools import partial
from typing import Dict, List, Union, cast

from botocore.exceptions import ClientError  # type: ignore[import-untyped]

from .blobs import BlobStore, parallel_each
from .s3 import NOT_FOUND, S3AuthError, S3Bucket

# Re-exported for callers / tests that import it from here.
__all__ = ["S3AuthError", "S3BlobStore"]


class S3BlobStore(BlobStore):
    """`BlobStore` over an S3-compatible bucket. Keys are stored under the
    bucket's prefix; the public key space is the same base-relative one as
    `FileBlobStore`. Accepts a shared `S3Bucket` (so it uses the same client as
    the control plane) or, for convenience, an `s3://` locator string."""

    def __init__(self, bucket: Union[str, S3Bucket]) -> None:
        self._b = S3Bucket(bucket) if isinstance(bucket, str) else bucket
        # Exposed so a test can swap the client to exercise the auth guard.
        self._s3 = self._b.client

    def put(self, key: str, data: bytes) -> None:
        bkt = self._b
        bkt.call(
            "write an object",
            lambda: self._s3.put_object(Bucket=bkt.bucket, Key=bkt.key(key), Body=data),
        )

    def get(self, key: str) -> bytes:
        bkt = self._b
        obj = bkt.call(
            "read an object",
            lambda: self._s3.get_object(Bucket=bkt.bucket, Key=bkt.key(key)),
        )
        return cast(bytes, obj["Body"].read())

    def exists(self, key: str) -> bool:
        bkt = self._b
        try:
            bkt.call(
                "stat an object",
                lambda: self._s3.head_object(Bucket=bkt.bucket, Key=bkt.key(key)),
            )
            return True
        except ClientError as err:
            if err.response.get("Error", {}).get("Code") in NOT_FOUND:
                return False
            raise

    def list_prefix(self, prefix: str) -> List[str]:
        bkt = self._b

        def _list() -> List[str]:
            keys: List[str] = []
            paginator = self._s3.get_paginator("list_objects_v2")
            for page in paginator.paginate(
                Bucket=bkt.bucket, Prefix=bkt.prefixed(prefix)
            ):
                for obj in page.get("Contents", []):
                    keys.append(bkt.strip(obj["Key"]))
            return sorted(keys)

        return bkt.call("list objects", _list)

    def materialise_prefix(self, prefix: str, dest_dir: str) -> None:
        strip = prefix.rstrip("/") + "/"
        keys = [k for k in self.list_prefix(prefix) if k.startswith(strip)]

        def _fetch(key: str) -> None:
            dest = os.path.join(dest_dir, key[len(strip) :])
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            data = self.get(key)
            with open(dest, "wb") as handle:
                handle.write(data)

        # Per-object GETs over the network: fan out so a version's hundreds of
        # blobs don't download one-round-trip-at-a-time (the cold-replica
        # `overview` hydration stall). Raises on the first lost blob.
        parallel_each(_fetch, keys)

    def delete_prefix(self, prefix: str) -> None:
        bkt = self._b
        keys = self.list_prefix(prefix)

        def _delete(objects: List[Dict[str, str]]) -> None:
            self._s3.delete_objects(
                Bucket=bkt.bucket, Delete={"Objects": objects, "Quiet": True}
            )

        # S3 DeleteObjects takes at most 1000 keys per request; a version is a
        # whole corpus copy, so chunk. `Quiet` suppresses the per-key result list.
        for start in range(0, len(keys), 1000):
            batch = [{"Key": bkt.key(k)} for k in keys[start : start + 1000]]
            bkt.call("delete objects", partial(_delete, batch))
