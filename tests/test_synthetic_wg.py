"""Tests for the `x-` synthetic-WG prefix.

A synthetic / non-WG corpus opts out of every Datatracker / WG-page
lookup (no charter, no leadership, no auto-discovered drafts or
mailing list, no transcripts, no ballots) while keeping the rest of
the gather pipeline functional. The prefix convention is the only
signal — there's no separate state file to track "is this synthetic."
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ietf_llm.gather.sources.mbox import sync_mailing_list
from ietf_llm.people import Registry, build_registry
from ietf_llm.log import Verbosity
from ietf_llm.paths import is_synthetic_wg


# --- is_synthetic_wg ------------------------------------------------------


def test_is_synthetic_recognises_x_prefix() -> None:
    assert is_synthetic_wg("x-foo") is True
    assert is_synthetic_wg("x-webbotauth-precursor") is True


def test_is_synthetic_does_not_match_real_wgs() -> None:
    # Real WG names never start with `x-`.
    assert is_synthetic_wg("httpbis") is False
    assert is_synthetic_wg("tls") is False
    assert is_synthetic_wg("xchacha") is False  # `x` alone isn't enough
    assert is_synthetic_wg("xmpp") is False


def test_is_synthetic_rejects_uppercase_prefix() -> None:
    # The prefix is case-sensitive; `X-foo` would not be conventional.
    # Keep the rule strict so we don't silently accept variants.
    assert is_synthetic_wg("X-foo") is False


# --- build_registry skips Datatracker roles when asked --------------------


def test_build_registry_skips_datatracker_when_with_dt_roles_false(
    isolated_home: Path, monkeypatch: Any,
) -> None:
    # Capture whether _ingest_datatracker_roles ever runs.
    from ietf_llm import people as people_mod
    calls: list[str] = []

    def fake_dt_ingest(wg: str, _r: Registry, _v: Verbosity) -> None:
        calls.append(wg)

    monkeypatch.setattr(
        people_mod, "_ingest_datatracker_roles", fake_dt_ingest,
    )
    build_registry("x-foo", verbose=Verbosity.QUIET, with_datatracker_roles=False)
    assert calls == []  # never called

    # Confirm the default branch DOES call it (sanity check on the
    # plumbing — the autouse no_datatracker fixture in conftest already
    # blocks it via _get_json, so this just verifies the dispatch).
    build_registry("httpbis", verbose=Verbosity.QUIET)
    assert calls == ["httpbis"]


# --- sync_mailing_list skips auto-discovery when asked --------------------


def test_sync_mailing_list_auto_discover_false_skips_get_list_name(
    isolated_home: Path, monkeypatch: Any,
) -> None:
    # Stub get_mailing_list_name so we can detect whether it's called.
    from ietf_llm.gather.sources import mbox
    calls: list[str] = []

    def fake_get_list_name(wg: str) -> str:
        calls.append(wg)
        return f"{wg}-list"

    monkeypatch.setattr(mbox, "get_mailing_list_name", fake_get_list_name)
    # Also stub _sync_one_list so we don't try to IMAP-connect.
    monkeypatch.setattr(mbox, "_sync_one_list", lambda *a, **kw: [])
    monkeypatch.setattr(mbox, "process_cache", lambda *a, **kw: {})

    # auto_discover=False: get_mailing_list_name never called.
    sync_mailing_list(
        "x-foo", str(isolated_home / "files"),
        auto_discover=False,
        extra_lists=["foo@ietf.org"],
        verbose=Verbosity.QUIET,
    )
    assert calls == []

    # auto_discover=True (the default): it IS called.
    sync_mailing_list(
        "httpbis", str(isolated_home / "files"),
        verbose=Verbosity.QUIET,
    )
    assert calls == ["httpbis"]


# --- sync_mailing_list raw/ suppression (#92) -----------------------------


def _stub_mail_sync(monkeypatch: Any) -> None:
    """Stub the IMAP sync + cache read so sync_mailing_list reaches the
    merge/write step with one year of content, without any network."""
    from ietf_llm.gather.sources import mbox

    monkeypatch.setattr(mbox, "get_mailing_list_name", lambda wg: f"{wg}-list")
    # Non-empty UIDs so the per-list branch runs process_cache.
    monkeypatch.setattr(mbox, "_sync_one_list", lambda *a, **kw: ["1", "2"])
    monkeypatch.setattr(mbox, "process_cache", lambda *a, **kw: {2025: "body"})


def test_sync_mailing_list_writes_raw_by_default(
    isolated_home: Path, monkeypatch: Any,
) -> None:
    _stub_mail_sync(monkeypatch)
    dest = isolated_home / "files"
    written = sync_mailing_list("httpbis", str(dest), verbose=Verbosity.QUIET)
    archive = dest / "raw" / "mail-archive-2025.txt"
    assert archive.exists()
    assert str(archive) in written


def test_sync_mailing_list_suppress_raw_skips_archive(
    isolated_home: Path, monkeypatch: Any,
) -> None:
    _stub_mail_sync(monkeypatch)
    dest = isolated_home / "files"
    written = sync_mailing_list(
        "httpbis", str(dest), verbose=Verbosity.QUIET, suppress_raw=True
    )
    assert written == []
    assert not (dest / "raw").exists()


def test_sync_mailing_list_suppress_raw_sweeps_preexisting(
    isolated_home: Path, monkeypatch: Any,
) -> None:
    # A cache gathered before suppression carries a merged dump; a later
    # suppressed gather must sweep it so it doesn't ride into the served
    # version (mirrors the .pdf sweep).
    _stub_mail_sync(monkeypatch)
    dest = isolated_home / "files"
    stale = dest / "raw" / "mail-archive-2024.txt"
    stale.parent.mkdir(parents=True)
    stale.write_text("old dump")
    written = sync_mailing_list(
        "httpbis", str(dest), verbose=Verbosity.QUIET, suppress_raw=True
    )
    assert written == []
    assert not stale.exists()


def test_sync_mailing_list_skips_rebuild_when_nothing_new(
    isolated_home: Path, monkeypatch: Any,
) -> None:
    # Nothing downloaded, so the second run must reuse the dumps, not re-parse.
    from ietf_llm.gather.sources import mbox

    _stub_mail_sync(monkeypatch)
    dest = isolated_home / "files"
    first = sync_mailing_list("httpbis", str(dest), verbose=Verbosity.QUIET)
    archive = dest / "raw" / "mail-archive-2025.txt"
    assert str(archive) in first

    calls: list[int] = []
    monkeypatch.setattr(
        mbox, "process_cache", lambda *a, **kw: calls.append(1) or {2025: "body"}
    )
    again = sync_mailing_list("httpbis", str(dest), verbose=Verbosity.QUIET)
    assert calls == []                     # no re-parse
    assert again == []                     # no files written
    assert archive.exists()
