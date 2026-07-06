"""Tests for the in-memory KvStore: compare-and-swap and child listing."""

from __future__ import annotations

from ietf_llm.store.kv import ABSENT, InMemoryKvStore


def test_put_get_roundtrip() -> None:
    kv = InMemoryKvStore()
    token = kv.put("a/b", b"hello")
    assert token is not None
    record = kv.get("a/b")
    assert record is not None
    assert record[0] == b"hello"
    assert record[1] == token


def test_get_absent_is_none() -> None:
    assert InMemoryKvStore().get("nope") is None


def test_put_absent_precondition() -> None:
    kv = InMemoryKvStore()
    assert kv.put("k", b"1", expect=ABSENT) is not None
    # A second create-only put fails because the key now exists.
    assert kv.put("k", b"2", expect=ABSENT) is None
    assert kv.get("k")[0] == b"1"


def test_put_cas_on_token() -> None:
    kv = InMemoryKvStore()
    t1 = kv.put("k", b"1")
    # A stale token is rejected; the matching token wins and rotates the token.
    assert kv.put("k", b"2", expect="bogus") is None
    t2 = kv.put("k", b"2", expect=t1)
    assert t2 is not None and t2 != t1
    assert kv.put("k", b"3", expect=t1) is None
    assert kv.get("k")[0] == b"2"


def test_delete_is_unconditional_and_idempotent() -> None:
    kv = InMemoryKvStore()
    kv.put("k", b"1")
    kv.delete("k")
    assert kv.get("k") is None
    # Deleting an absent key is an idempotent no-op.
    kv.delete("k")


def test_list_children_one_level() -> None:
    kv = InMemoryKvStore()
    kv.put("corpora/tls/pointer", b"v1")
    kv.put("corpora/tls/versions/v1/files/a.md", b"x")
    kv.put("corpora/httpbis/pointer", b"v9")
    kv.put("fleet/slots", b"{}")
    assert kv.list_children("corpora/") == ["httpbis", "tls"]
    assert kv.list_children("corpora/tls/") == ["pointer", "versions"]
