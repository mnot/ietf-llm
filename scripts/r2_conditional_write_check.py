#!/usr/bin/env python3
"""Validate that an S3-compatible object store (target: Cloudflare R2) enforces
the conditional writes the ietf-llm cloud control plane relies on:

  - If-None-Match: *   create-only            (lease / slot acquisition)
  - If-Match: <etag>   compare-and-swap       (pointer flip, lease / slot update)

moto does not reliably enforce these headers, so this is the real-backend check
called for in issue #83. The dangerous failure it catches is a store that
*silently ignores* the precondition and lets the write succeed — which would
make two replicas think they both hold a lease. It writes a few small objects
under a throwaway prefix and deletes them at the end.

Exit 0 = every check passed (safe for the CAS control plane).
Exit 1 = a check failed (header ignored, wrong error, or not atomic).

Usage:
  pip install boto3
  export AWS_ACCESS_KEY_ID=...        # R2 access key id
  export AWS_SECRET_ACCESS_KEY=...    # R2 secret access key
  export STORE_BUCKET=my-test-bucket  # an existing bucket you can write to
  export STORE_ENDPOINT_URL=https://<account_id>.r2.cloudflarestorage.com
  python scripts/r2_conditional_write_check.py

For plain AWS S3 instead, leave STORE_ENDPOINT_URL unset and set AWS_DEFAULT_REGION.
"""

from __future__ import annotations

import concurrent.futures as cf
import os
import sys
import uuid

import boto3
from botocore.exceptions import ClientError

_BOGUS_ETAG = '"00000000000000000000000000000000"'
_results: list[tuple[bool, str]] = []


def _is_precondition_failed(err: ClientError) -> bool:
    """True iff the error is a 412 PreconditionFailed — exactly what S3KvStore
    maps to a None (lost CAS). A different code means the store does not behave
    the way the control plane assumes."""
    resp = err.response or {}
    code = str(resp.get("Error", {}).get("Code", ""))
    status = resp.get("ResponseMetadata", {}).get("HTTPStatusCode")
    return code in ("PreconditionFailed", "412") or status == 412


def _record(name: str, ok: bool, detail: str = "") -> None:
    _results.append((ok, name))
    mark = "PASS" if ok else "FAIL"
    print(f"  [{mark}] {name}" + (f"  -- {detail}" if detail else ""))


def _expect_blocked(name: str, fn) -> None:
    """Assert that a conditional write is *rejected* with a 412."""
    try:
        fn()
    except ClientError as err:
        if _is_precondition_failed(err):
            _record(name, True)
        else:
            resp = err.response or {}
            code = resp.get("Error", {}).get("Code")
            status = resp.get("ResponseMetadata", {}).get("HTTPStatusCode")
            _record(name, False, f"wrong error {code} / HTTP {status} (wanted 412)")
        return
    _record(name, False, "PUT SUCCEEDED — the store ignored the precondition")


def main() -> int:
    bucket = os.environ.get("STORE_BUCKET")
    if not bucket:
        print("set STORE_BUCKET (an existing, writable bucket)", file=sys.stderr)
        return 2
    endpoint = os.environ.get("STORE_ENDPOINT_URL") or None
    region = os.environ.get("AWS_DEFAULT_REGION") or "auto"
    s3 = boto3.client("s3", endpoint_url=endpoint, region_name=region)

    key = f"_cond-check/{uuid.uuid4().hex}"
    print(f"target: bucket={bucket!r} endpoint={endpoint or 'AWS'} key={key!r}\n")

    try:
        # 1. Create-only on an absent key succeeds and returns an ETag.
        r1 = s3.put_object(Bucket=bucket, Key=key, Body=b"v1", IfNoneMatch="*")
        etag1 = r1["ETag"]
        _record("If-None-Match:* creates an absent key", True)

        # 2. Create-only on an existing key is rejected.
        _expect_blocked(
            "If-None-Match:* rejects an existing key",
            lambda: s3.put_object(Bucket=bucket, Key=key, Body=b"x", IfNoneMatch="*"),
        )

        # 3. The ETag round-trips: GET reports the same token and body.
        got = s3.get_object(Bucket=bucket, Key=key)
        _record(
            "GET returns the same ETag and body (token round-trips)",
            got["ETag"] == etag1 and got["Body"].read() == b"v1",
            f"put={etag1} get={got['ETag']}",
        )

        # 4. If-Match with a stale ETag is rejected.
        _expect_blocked(
            "If-Match with a stale ETag is rejected",
            lambda: s3.put_object(
                Bucket=bucket, Key=key, Body=b"x", IfMatch=_BOGUS_ETAG
            ),
        )

        # 5. If-Match with the current ETag succeeds and rotates the ETag.
        r2 = s3.put_object(Bucket=bucket, Key=key, Body=b"v2", IfMatch=etag1)
        etag2 = r2["ETag"]
        _record("If-Match with the current ETag updates the value", etag2 != etag1)

        # 6. The previously-current ETag is now stale and rejected.
        _expect_blocked(
            "If-Match with the now-superseded ETag is rejected",
            lambda: s3.put_object(Bucket=bucket, Key=key, Body=b"x", IfMatch=etag1),
        )

        # 7. Atomicity: many concurrent CAS writers on the same ETag -> exactly
        # one wins. This is the property lease / slot correctness rests on.
        def attempt(i: int) -> bool:
            try:
                s3.put_object(
                    Bucket=bucket, Key=key, Body=f"t{i}".encode(), IfMatch=etag2
                )
                return True
            except ClientError as err:
                if _is_precondition_failed(err):
                    return False
                raise

        workers = 8
        with cf.ThreadPoolExecutor(max_workers=workers) as pool:
            wins = sum(pool.map(attempt, range(workers)))
        _record(
            f"{workers} concurrent CAS writers -> exactly one wins",
            wins == 1,
            f"{wins} succeeded",
        )
    finally:
        s3.delete_object(Bucket=bucket, Key=key)

    failed = [name for ok, name in _results if not ok]
    print()
    if failed:
        print(f"RESULT: {len(failed)} check(s) FAILED — the CAS control plane is")
        print("NOT safe on this store as-is.")
        return 1
    print(f"RESULT: all {len(_results)} checks passed — conditional writes are safe.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
