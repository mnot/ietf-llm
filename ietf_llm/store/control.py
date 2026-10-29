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

from ..paths import checked_corpus
from .kv import ABSENT, ANY, KvStore, Record

_CORPORA = "corpora/"
_SLOTS_KEY = "fleet/slots"
#: Bounded retries for the multi-writer compare-and-swap on `fleet/slots`.
_CAS_RETRIES = 8


def _pointer_key(corpus: str) -> str:
    return f"corpora/{checked_corpus(corpus)}/pointer"


def _lease_key(corpus: str) -> str:
    return f"corpora/{checked_corpus(corpus)}/lease"


def _status_key(corpus: str) -> str:
    return f"corpora/{checked_corpus(corpus)}/status"


def _access_key(corpus: str) -> str:
    return f"corpora/{checked_corpus(corpus)}/access"


def _config_prefix(corpus: str) -> str:
    return f"corpora/{checked_corpus(corpus)}/config/"


def _config_key(corpus: str, scope: str) -> str:
    return f"corpora/{checked_corpus(corpus)}/config/{scope}"


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

    def pointer_token(self, corpus: str) -> object:
        """The pointer's version token — `ABSENT` when the corpus has none yet.
        Read at the *start* of a publish and handed back to `set_current`, so the
        flip only lands if nobody else has flipped in between."""
        record = self._kv.get(_pointer_key(corpus))
        return ABSENT if record is None else record[1]

    def set_current(self, corpus: str, version: str, *, expect: object = ANY) -> bool:
        """Flip the pointer to `version`; False if `expect` (a `pointer_token`)
        no longer holds, i.e. another publisher got there first. The default
        `ANY` is an unconditional put, for callers that hold no token."""
        won = self._kv.put(_pointer_key(corpus), version.encode("utf-8"), expect=expect)
        return won is not None

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
        new = _dumps(
            {
                "owner": owner,
                "acquired_at": clock,
                "expires_at": clock + ttl,
                "ttl": ttl,
            }
        )
        if record is None:
            return self._kv.put(key, new, expect=ABSENT) is not None
        held, token = _loads(record)
        # Take it over only if the held lease has expired or is already ours.
        if held["expires_at"] <= clock or held["owner"] == owner:
            return self._kv.put(key, new, expect=token) is not None
        return False

    def _touch_lease(
        self, corpus: str, owner: str, ttl: Optional[float], clock: float
    ) -> bool:
        """Extend `owner`'s lease record to `clock + ttl` under compare-and-swap
        (`ttl` None: by the span the record itself was granted for, never
        shortening it) and report whether `owner` still held it. The bytes always
        change, so the object store's ETag does too and the write is a real CAS
        against a taker's `acquire_lease`. A lost CAS is retried against a fresh
        read: the only other writer of a live lease is its owner's other thread
        (the heartbeat vs. a publish), and a CAS lost to that must not read as a
        lost lease. A different owner on the record is the real answer."""
        key = _lease_key(corpus)
        for _ in range(_CAS_RETRIES):
            record = self._kv.get(key)
            if record is None:
                return False
            held, token = _loads(record)
            if held["owner"] != owner:
                return False
            if ttl is not None:
                held["ttl"] = ttl
                held["expires_at"] = clock + ttl
            else:
                span = held.get("ttl") or held["expires_at"] - held["acquired_at"]
                held["expires_at"] = max(held["expires_at"], clock + span)
            if self._kv.put(key, _dumps(held), expect=token) is not None:
                return True
        return False

    def renew_lease(
        self, corpus: str, owner: str, ttl: float, now: Optional[float] = None
    ) -> bool:
        """Extend `owner`'s lease; False once it is no longer `owner`'s (taken
        over, or never held). A lease that merely lapsed unclaimed is revived: no
        one else holds it, and the compare-and-swap decides any race with a
        taker."""
        clock = time.time() if now is None else now
        return self._touch_lease(corpus, owner, ttl, clock)

    def confirm_lease(self, corpus: str, owner: str) -> bool:
        """Whether `owner` holds the lease *right now*, proven by a compare-and-
        swap write rather than a read, which also extends the lease by the span
        it was granted for. The extension is what makes it a CAS on an object
        store (the ETag is a hash of the body, so a byte-identical rewrite would
        not change it and a taker holding the old token would still win), and it
        keeps the flip and the reap that follows under a live lease. A taker's
        `acquire_lease` and this write race on the record, so exactly one wins.
        Expiry is not consulted: an unclaimed lapsed lease is still `owner`'s
        and is revived; the publisher's pointer compare-and-swap, not the clock,
        orders it against any later taker."""
        return self._touch_lease(corpus, owner, None, time.time())

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

    def lease_holder(
        self, corpus: str, now: Optional[float] = None, *, expired_ok: bool = False
    ) -> Optional[str]:
        """The live lease holder. `expired_ok` returns whoever the record names
        even once it has lapsed: a lapsed lease still names its last owner until
        someone else takes it, which is what a publisher checks."""
        clock = time.time() if now is None else now
        record = self._kv.get(_lease_key(corpus))
        if record is None:
            return None
        held, _ = _loads(record)
        return held["owner"] if expired_ok or held["expires_at"] > clock else None

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
