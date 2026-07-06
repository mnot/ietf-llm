"""A small key-value seam with compare-and-swap — the substrate the cloud
control plane is built on.

Four operations — `get`, `put` (with an optional precondition), `delete`, and
`list_children` — are exactly what an object store with conditional writes
(`If-Match` / `If-None-Match`) provides natively, so the same `KvControlPlane`
runs over an in-memory double (tests) or an S3-compatible bucket (production).
There is no transaction and no `batch`: every control-plane operation is one
`get` plus one conditional `put`, or a single bounded compare-and-swap retry
loop.

A value's version token is opaque (an object-store ETag, say). Pass the token
from a prior `get` back as `expect` to make a write conditional on the value not
having changed underneath you. See `docs/storage.md`.
"""

from __future__ import annotations

import threading
from abc import ABC, abstractmethod
from typing import Dict, List, Optional, Tuple

#: A stored value and its opaque version token (ETag-like).
Record = Tuple[bytes, str]

#: Preconditions for `put` / `delete`. `ABSENT` requires the key not to exist
#: (create-only, like `If-None-Match: *`); a version-token string requires the
#: current value to still carry that token (compare-and-swap, like `If-Match`);
#: `ANY` is unconditional.
ABSENT = object()
ANY = object()


class KvStore(ABC):
    """A durable map with compare-and-swap. The contract a cloud control plane
    actually needs — no SQL, no relational features, no transactions."""

    @abstractmethod
    def get(self, key: str) -> Optional[Record]:
        """The value and version token at `key`, or None if absent."""

    @abstractmethod
    def put(self, key: str, value: bytes, *, expect: object = ANY) -> Optional[str]:
        """Store `value` at `key` subject to `expect`; return the new version
        token, or None if the precondition failed."""

    @abstractmethod
    def delete(self, key: str) -> None:
        """Delete `key` if present; idempotent (a no-op if absent). Unconditional
        by design — the control plane never needs a conditional delete, so an
        object store is not asked to support one."""

    @abstractmethod
    def list_children(self, prefix: str) -> List[str]:
        """The immediate child segment names directly under `prefix` (one level,
        like a directory listing). `prefix` should end with '/'."""


class InMemoryKvStore(KvStore):
    """A dict-backed `KvStore` for tests: real compare-and-swap semantics, no
    durability. Thread-safe, so concurrency tests are meaningful."""

    def __init__(self) -> None:
        self._data: Dict[str, Record] = {}
        self._seq = 0
        self._lock = threading.Lock()

    def _next_token(self) -> str:
        self._seq += 1
        return str(self._seq)

    def get(self, key: str) -> Optional[Record]:
        with self._lock:
            return self._data.get(key)

    def put(self, key: str, value: bytes, *, expect: object = ANY) -> Optional[str]:
        with self._lock:
            current = self._data.get(key)
            if expect is ABSENT:
                if current is not None:
                    return None
            elif expect is not ANY:
                if current is None or current[1] != expect:
                    return None
            token = self._next_token()
            self._data[key] = (value, token)
            return token

    def delete(self, key: str) -> None:
        with self._lock:
            self._data.pop(key, None)

    def list_children(self, prefix: str) -> List[str]:
        with self._lock:
            keys = list(self._data.keys())
        seen = set()
        for key in keys:
            if not key.startswith(prefix):
                continue
            head = key[len(prefix) :].split("/", 1)[0]
            if head:
                seen.add(head)
        return sorted(seen)
