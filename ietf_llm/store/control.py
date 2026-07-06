"""The cloud control plane over the `KvStore` seam: per-corpus current-version
pointers, gather leases, fleet-wide gather slots, and gather status — each a
`get` plus a conditional `put`, no SQL and no transaction.

Layout (keys in the shared object store; see `docs/storage.md`):

    corpora/<name>/pointer          current version token   (compare-and-swap)
    corpora/<name>/lease            gather lease, TTL in-value (compare-and-swap)
    corpora/<name>/status           latest gather status JSON (last-writer-wins)
    corpora/<name>/access           last read-path access time (last-writer-wins)
    corpora/<name>/config/<scope>   per-WG config JSON      (last-writer-wins)
    fleet/slots                     cross-corpora gather semaphore (compare-and-swap)

The `access` key is the one control-plane key the *read* fleet writes (an
ISO timestamp, stamped coarsely off the read path so a refresh cron can skip
unused corpora). Last-writer-wins like `status`: the value is "the most recent
access any reader saw", so a lost race only drops an older stamp for a newer
one — no CAS needed. See `docs/storage.md`.

Per-corpus control lives under that corpus's own prefix; the only cross-corpora
state — the gather-slot semaphore — lives outside any corpus, at `fleet/slots`.
Version content and its manifest are immutable blobs under
`corpora/<name>/versions/<version>/`, owned by the blob plane, not here.
"""

from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional, Tuple

from .kv import ABSENT, KvStore, Record

_CORPORA = "corpora/"
_SLOTS_KEY = "fleet/slots"
#: Bounded retries for the multi-writer compare-and-swap on `fleet/slots`.
_CAS_RETRIES = 8


def _pointer_key(corpus: str) -> str:
    return f"corpora/{corpus}/pointer"


def _lease_key(corpus: str) -> str:
    return f"corpora/{corpus}/lease"


def _status_key(corpus: str) -> str:
    return f"corpora/{corpus}/status"


def _access_key(corpus: str) -> str:
    return f"corpora/{corpus}/access"


def _config_prefix(corpus: str) -> str:
    return f"corpora/{corpus}/config/"


def _config_key(corpus: str, scope: str) -> str:
    return f"corpora/{corpus}/config/{scope}"


def _loads(record: Record) -> Tuple[Any, str]:
    data, token = record
    return json.loads(data), token


def _dumps(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True).encode("utf-8")


class KvControlPlane:
    """Pointer + lease + slot + status over a `KvStore`. The manifest is a blob,
    owned by the cloud store, so the control plane no longer records it."""

    def __init__(self, store: KvStore) -> None:
        self._kv = store

    # --- current pointer ---------------------------------------------------

    def resolve_current(self, corpus: str) -> Optional[str]:
        record = self._kv.get(_pointer_key(corpus))
        return record[0].decode("utf-8") if record else None

    def set_current(self, corpus: str, version: str) -> None:
        # The publisher holds the per-corpus lease, so a last-writer-wins put is
        # safe — no other writer flips this pointer concurrently.
        self._kv.put(_pointer_key(corpus), version.encode("utf-8"))

    def list_corpora(self) -> List[str]:
        names = self._kv.list_children(_CORPORA)
        return [n for n in names if self._kv.get(_pointer_key(n)) is not None]

    # --- gather lease ------------------------------------------------------

    def acquire_lease(
        self, corpus: str, owner: str, ttl: float, now: Optional[float] = None
    ) -> bool:
        clock = time.time() if now is None else now
        key = _lease_key(corpus)
        record = self._kv.get(key)
        new = _dumps({"owner": owner, "acquired_at": clock, "expires_at": clock + ttl})
        if record is None:
            return self._kv.put(key, new, expect=ABSENT) is not None
        held, token = _loads(record)
        # Take it over only if the held lease has expired or is already ours.
        if held["expires_at"] <= clock or held["owner"] == owner:
            return self._kv.put(key, new, expect=token) is not None
        return False

    def renew_lease(
        self, corpus: str, owner: str, ttl: float, now: Optional[float] = None
    ) -> bool:
        clock = time.time() if now is None else now
        key = _lease_key(corpus)
        record = self._kv.get(key)
        if record is None:
            return False
        held, token = _loads(record)
        if held["owner"] != owner:
            return False
        held["expires_at"] = clock + ttl
        return self._kv.put(key, _dumps(held), expect=token) is not None

    def release_lease(self, corpus: str, owner: str) -> None:
        # Free the lease by stamping it expired (compare-and-swap on our token),
        # so release needs only a conditional PUT, never a conditional DELETE.
        record = self._kv.get(_lease_key(corpus))
        if record is None:
            return
        held, token = _loads(record)
        if held["owner"] == owner:
            held["expires_at"] = 0.0
            self._kv.put(_lease_key(corpus), _dumps(held), expect=token)

    def lease_holder(self, corpus: str, now: Optional[float] = None) -> Optional[str]:
        clock = time.time() if now is None else now
        record = self._kv.get(_lease_key(corpus))
        if record is None:
            return None
        held, _ = _loads(record)
        return held["owner"] if held["expires_at"] > clock else None

    # --- fleet-wide gather slots (cross-corpora concurrency cap) -----------

    def acquire_gather_slot(
        self,
        owner: str,
        corpus: str,
        ttl: float,
        max_inflight: int,
        now: Optional[float] = None,
    ) -> bool:
        clock = time.time() if now is None else now
        for _ in range(_CAS_RETRIES):
            record = self._kv.get(_SLOTS_KEY)
            expect: object = ABSENT if record is None else record[1]
            slots: Dict[str, Any] = {} if record is None else json.loads(record[0])
            live = {o: s for o, s in slots.items() if s["expires_at"] > clock}
            # Self-exclude so a re-acquire is idempotent and never self-blocks.
            if owner not in live and len(live) >= max_inflight:
                return False
            live[owner] = {"corpus": corpus, "expires_at": clock + ttl}
            if self._kv.put(_SLOTS_KEY, _dumps(live), expect=expect) is not None:
                return True
        return False

    def renew_gather_slot(
        self, owner: str, ttl: float, now: Optional[float] = None
    ) -> bool:
        clock = time.time() if now is None else now
        for _ in range(_CAS_RETRIES):
            record = self._kv.get(_SLOTS_KEY)
            if record is None:
                return False
            slots, token = _loads(record)
            if owner not in slots:
                return False
            slots[owner]["expires_at"] = clock + ttl
            if self._kv.put(_SLOTS_KEY, _dumps(slots), expect=token) is not None:
                return True
        return False

    def release_gather_slot(self, owner: str) -> None:
        for _ in range(_CAS_RETRIES):
            record = self._kv.get(_SLOTS_KEY)
            if record is None:
                return
            slots, token = _loads(record)
            if owner not in slots:
                return
            del slots[owner]
            if self._kv.put(_SLOTS_KEY, _dumps(slots), expect=token) is not None:
                return

    # --- gather status (fleet-visible) -------------------------------------

    def set_gather_status(self, corpus: str, payload: str) -> None:
        self._kv.put(_status_key(corpus), payload.encode("utf-8"))

    def get_gather_status(self, corpus: str) -> Optional[str]:
        record = self._kv.get(_status_key(corpus))
        return record[0].decode("utf-8") if record else None

    def list_gather_statuses(self) -> List[Tuple[str, str]]:
        result: List[Tuple[str, str]] = []
        for name in self._kv.list_children(_CORPORA):
            record = self._kv.get(_status_key(name))
            if record is not None:
                result.append((name, record[0].decode("utf-8")))
        return result

    # --- read-path access marker (fleet-visible, last-writer-wins) ---------

    def set_access(self, corpus: str, payload: str) -> None:
        self._kv.put(_access_key(corpus), payload.encode("utf-8"))

    def get_access(self, corpus: str) -> Optional[str]:
        record = self._kv.get(_access_key(corpus))
        return record[0].decode("utf-8") if record else None

    # --- per-WG config (fleet-visible, last-writer-wins) -------------------
    #
    # One key per scope under corpora/<name>/config/. A plain put: config is
    # written during a gather, which holds that corpus's gather lease, so the
    # writer is already serialised — no compare-and-swap needed here.

    def set_config(self, corpus: str, scope: str, payload: str) -> None:
        self._kv.put(_config_key(corpus, scope), payload.encode("utf-8"))

    def get_config(self, corpus: str, scope: str) -> Optional[str]:
        record = self._kv.get(_config_key(corpus, scope))
        return record[0].decode("utf-8") if record else None

    def clear_config(self, corpus: str) -> bool:
        """Delete every config scope key for `corpus`. Returns True if any
        existed (mirrors the local whole-directory clear)."""
        scopes = self._kv.list_children(_config_prefix(corpus))
        for scope in scopes:
            self._kv.delete(_config_key(corpus, scope))
        return bool(scopes)
