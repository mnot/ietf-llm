"""Per-generation seed-store paths (#230).

A store carries one compatibility tuple, so a schema bump makes every bundle
in it unusable to the new code. Serving each generation under its own path is
what lets an old client and a new one both keep seeding through a bump
instead of both cold-gathering.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from ietf_llm.embeddings.storage import _SCHEMA_VERSION
from ietf_llm.seed import generation
from ietf_llm.seed.publish import MemberSpec, generation_dir, load_members, save_members


def test_the_segment_tracks_the_schema_version() -> None:
    """Derived, not written down, so the next bump moves clients by itself."""
    assert generation.generation() == f"v{_SCHEMA_VERSION}"


def test_the_store_url_is_the_base_plus_the_generation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("IETF_LLM_SEED_URL", "https://example.invalid/seed/")
    assert generation.store_url() == (
        f"https://example.invalid/seed/v{_SCHEMA_VERSION}/"
    )


def test_a_base_without_a_trailing_slash_still_composes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("IETF_LLM_SEED_URL", "https://example.invalid/seed")
    assert generation.store_url() == (
        f"https://example.invalid/seed/v{_SCHEMA_VERSION}/"
    )


def test_a_custom_mirror_is_versioned_too(monkeypatch: pytest.MonkeyPatch) -> None:
    """Applied to whatever base is configured, so someone running their own
    mirror does not have to track schema numbers."""
    monkeypatch.setenv("IETF_LLM_SEED_URL", "https://mirror.invalid/")
    assert generation.store_url().startswith("https://mirror.invalid/v")


def test_disabled_seeding_has_no_store_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("IETF_LLM_SEED_URL", "off")
    assert generation.store_url() is None


def test_publishing_goes_into_the_generation_subdirectory(tmp_path: Any) -> None:
    base = str(tmp_path / "store")
    target = generation_dir(base)
    assert target == os.path.join(base, f"v{_SCHEMA_VERSION}")
    assert os.path.isdir(target)


def test_a_new_generation_inherits_membership(tmp_path: Any) -> None:
    """Asking an operator to re-add two dozen members after a schema bump is
    a way to lose one."""
    base = str(tmp_path / "store")
    os.makedirs(base, exist_ok=True)
    save_members(base, {"httpbis": MemberSpec(window_months=12), "tls": MemberSpec()})
    target = generation_dir(base)
    assert sorted(load_members(target)) == ["httpbis", "tls"]
    assert load_members(target)["httpbis"].window_months == 12


def test_inheritance_does_not_overwrite_an_existing_generation(
    tmp_path: Any,
) -> None:
    base = str(tmp_path / "store")
    os.makedirs(base, exist_ok=True)
    save_members(base, {"httpbis": MemberSpec()})
    target = generation_dir(base)
    save_members(target, {"quic": MemberSpec()})
    assert sorted(load_members(generation_dir(base))) == ["quic"]


def test_the_previous_generation_is_left_alone(tmp_path: Any) -> None:
    """The point of the layout: an older store keeps serving old clients from
    where it already is, so the operator's rsync needs no new rule."""
    base = str(tmp_path / "store")
    os.makedirs(base, exist_ok=True)
    with open(os.path.join(base, "index.json"), "w", encoding="utf-8") as fh:
        fh.write('{"format": 1, "schema_version": 9}')
    generation_dir(base)
    with open(os.path.join(base, "index.json"), encoding="utf-8") as fh:
        assert '"schema_version": 9' in fh.read()


def test_a_dry_run_creates_nothing(tmp_path: Any) -> None:
    """`--dry-run` prints the plan and writes nothing — creating the
    generation directory and copying membership into it are both writes."""
    base = str(tmp_path / "store")
    os.makedirs(base, exist_ok=True)
    save_members(base, {"httpbis": MemberSpec()})
    target = generation_dir(base, dry_run=True)
    assert not os.path.exists(target)


def test_the_rfc_corpus_gets_its_own_store() -> None:
    """A store carries one compatibility tuple. This corpus's chunks come from
    rfc.fyi's chunker while every gathered corpus carries ours, so a shared
    store always refuses one of them — as observed: 24 members skipped with
    "chunker_version rfcfyi-1 vs 2". It is a different embedding generation
    and gets a different store.
    """
    import os as _os

    _os.environ["IETF_LLM_SEED_URL"] = "https://example.invalid/seed/"
    try:
        assert generation.store_url() == (
            f"https://example.invalid/seed/v{_SCHEMA_VERSION}/"
        )
        assert generation.rfc_store_url() == (
            f"https://example.invalid/seed/rfcs/v{_SCHEMA_VERSION}/"
        )
        # Siblings, not nested: neither is a prefix of the other's contents.
        assert generation.store_url() not in generation.rfc_store_url()
    finally:
        del _os.environ["IETF_LLM_SEED_URL"]


def test_both_stores_move_together_on_a_bump(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("IETF_LLM_SEED_URL", "https://example.invalid/seed/")
    assert generation.generation() in generation.store_url()
    assert generation.generation() in generation.rfc_store_url()


def test_no_rfc_store_when_seeding_is_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("IETF_LLM_SEED_URL", "off")
    assert generation.rfc_store_url() is None
