"""A shared S3 connection for the cloud store: one boto3 client bound to a
bucket and key prefix, with credential-error translation and key helpers.

Both planes of the cloud store address the same bucket through one `S3Bucket`:
the blob plane (`S3BlobStore`) for immutable version content, and the control
plane (`S3KvStore`) for the compare-and-swap pointer / lease / slot / status
keys. Sharing the connection is the point — one client, one endpoint, one
credential set. Addressed by an `s3://<bucket>/<prefix>` locator; for a non-AWS
endpoint (R2, MinIO) set `IETF_LLM_STORE_ENDPOINT_URL`. Credentials come from the
standard AWS environment / instance-role chain. See `docs/storage.md`.
"""

from __future__ import annotations

import os
from typing import Callable, Optional, Tuple, TypeVar

import boto3  # type: ignore[import-untyped]
from botocore.config import Config  # type: ignore[import-untyped]
from botocore.exceptions import (  # type: ignore[import-untyped]
    ClientError,
    NoCredentialsError,
)

from .blobs import _safe_key, blob_concurrency

_T = TypeVar("_T")

#: Error codes meaning the object is absent.
NOT_FOUND = ("404", "NoSuchKey", "NotFound")

#: Error codes meaning a conditional write's precondition did not hold.
PRECONDITION_FAILED = ("PreconditionFailed", "412")

#: Codes meaning credentials are missing, wrong, or lack access — a
#: configuration problem, not a transient or not-found condition.
AUTH_CODES = frozenset(
    {
        "403",
        "AccessDenied",
        "InvalidAccessKeyId",
        "SignatureDoesNotMatch",
        "InvalidToken",
        "ExpiredToken",
        "AccountProblem",
    }
)


class S3AuthError(RuntimeError):
    """The object store rejected our credentials (missing, invalid, or lacking
    access). Not retryable — the fix is a configuration change, so the message
    names the AWS credential chain rather than surfacing a botocore traceback."""


def parse_locator(locator: str) -> Tuple[str, str]:
    """`s3://bucket/prefix...` → (bucket, prefix); prefix may be empty."""
    if not locator.startswith("s3://"):
        raise ValueError(
            f"invalid S3 locator (expected s3://bucket[/prefix]): {locator!r}"
        )
    bucket, _, prefix = locator[len("s3://") :].partition("/")
    if not bucket:
        raise ValueError(f"invalid S3 locator (no bucket): {locator!r}")
    return bucket, prefix.strip("/")


class S3Bucket:
    """A boto3 S3 client bound to one bucket and key prefix, shared by the blob
    and control planes."""

    def __init__(self, locator: str, endpoint_url: Optional[str] = None) -> None:
        self.bucket, self.prefix = parse_locator(locator)
        endpoint = endpoint_url or os.environ.get("IETF_LLM_STORE_ENDPOINT_URL") or None
        # Pool sized to the publish / materialise worker count (floored at the
        # boto3 default of 10) so a fan-out of object ops doesn't queue behind a
        # too-small connection pool. `standard` retries cover throttling / 5xx;
        # a 412 from a CAS conditional put is a 4xx and is never auto-retried.
        self.client = boto3.client(
            "s3",
            endpoint_url=endpoint,
            config=Config(
                max_pool_connections=max(blob_concurrency(), 10),
                retries={"max_attempts": 5, "mode": "standard"},
            ),
        )

    def call(self, what: str, fn: Callable[[], _T]) -> _T:
        """Run an S3 operation, translating a rejected-credentials failure into a
        clear `S3AuthError`. Non-auth errors (not-found, precondition-failed)
        propagate unchanged for the caller to handle."""
        try:
            return fn()
        except NoCredentialsError as err:
            raise S3AuthError(
                f"no credentials available to {what} on s3://{self.bucket}: set "
                "AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY (or an instance role)."
            ) from err
        except ClientError as err:
            code = str(err.response.get("Error", {}).get("Code", ""))
            if code in AUTH_CODES:
                raise S3AuthError(
                    f"the object store rejected the request to {what} on "
                    f"s3://{self.bucket} ({code}): the credentials are missing, "
                    "invalid, or lack access. Check AWS_ACCESS_KEY_ID / "
                    "AWS_SECRET_ACCESS_KEY (or the instance role) and the "
                    "bucket policy."
                ) from err
            raise

    def key(self, key: str) -> str:
        safe = _safe_key(key)
        return f"{self.prefix}/{safe}" if self.prefix else safe

    def prefixed(self, prefix: str) -> str:
        return f"{self.prefix}/{prefix}" if self.prefix else prefix

    def strip(self, s3_key: str) -> str:
        head = f"{self.prefix}/" if self.prefix else ""
        return s3_key[len(head) :] if s3_key.startswith(head) else s3_key
