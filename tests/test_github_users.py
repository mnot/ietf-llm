"""Tests for GitHub username -> real name resolution.

We stub `_fetch_one` at the HTTP boundary so no real GitHub API call
is made. Tests cover:

- Cache hits short-circuit the HTTP layer entirely
- Resolved names are persisted to the cache file
- 404 misses are cached (no name) so we don't keep re-asking
- Rate-limit stops subsequent calls in the same batch
- Empty-string names are normalised to None
- The cache file survives a process restart
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Dict, List

import pytest

from ietf_llm.gather.sources import github_users
from ietf_llm.gather.sources.github_users import (
    _Outcome,
    _cache_path,
    resolve_logins,
)
from ietf_llm.utils import Verbosity


def _stub_fetch(
    monkeypatch: pytest.MonkeyPatch,
    responses: Dict[str, _Outcome],
    fallback: _Outcome = _Outcome(name=None, cacheable=False),
) -> List[str]:
    """Patch `_fetch_one` with a per-login canned-response map."""
    called: List[str] = []

    def fake(
        login: str, headers: Dict[str, str], verbose: Verbosity,  # noqa: ARG001
    ) -> _Outcome:
        called.append(login)
        return responses.get(login, fallback)

    monkeypatch.setattr(github_users, "_fetch_one", fake)
    return called


# --- Cache hit short-circuits the HTTP layer ------------------------------


def test_cache_hit_skips_http(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Pre-seed the cache with a known name.
    import json
    cache_file = Path(_cache_path())
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    cache_file.write_text(json.dumps({
        "alice": {"name": "Alice Wonderland", "fetched_at": "2026-01-01T00:00:00Z"},
    }))
    called = _stub_fetch(monkeypatch, {})

    out = resolve_logins(["alice"], verbose=Verbosity.QUIET)

    assert out["alice"].name == "Alice Wonderland"
    # Crucially, no HTTP call.
    assert called == []


# --- Resolved names get persisted ----------------------------------------


def test_resolved_names_persisted_to_cache(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_fetch(monkeypatch, {
        "bob": _Outcome(name="Bob Builder", cacheable=True),
    })
    out = resolve_logins(["bob"], verbose=Verbosity.QUIET)
    assert out["bob"].name == "Bob Builder"
    # Re-read by a *fresh* resolve call to prove persistence.
    import json
    saved = json.loads(Path(_cache_path()).read_text())
    assert saved["bob"]["name"] == "Bob Builder"
    assert "fetched_at" in saved["bob"]


def test_second_call_uses_cache(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # First call hits the API; second call must read from cache.
    _stub_fetch(monkeypatch, {
        "carol": _Outcome(name="Carol", cacheable=True),
    })
    resolve_logins(["carol"], verbose=Verbosity.QUIET)
    # Replace the fetcher with one that would FAIL if called — proves
    # the second call comes entirely from cache.
    called2 = _stub_fetch(monkeypatch, {})
    out = resolve_logins(["carol"], verbose=Verbosity.QUIET)
    assert out["carol"].name == "Carol"
    assert called2 == []


# --- 404 misses are cached too -------------------------------------------


def test_404_miss_cached_so_we_dont_retry(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # cacheable=True with name=None means "we asked and got nothing" —
    # this is the 404 / no-name path. Should NOT be re-asked.
    _stub_fetch(monkeypatch, {
        "ghost": _Outcome(name=None, cacheable=True),
    })
    out1 = resolve_logins(["ghost"], verbose=Verbosity.QUIET)
    assert out1["ghost"].name is None
    # Second call: cache says None, no HTTP.
    called2 = _stub_fetch(monkeypatch, {})
    out2 = resolve_logins(["ghost"], verbose=Verbosity.QUIET)
    assert out2["ghost"].name is None
    assert called2 == []


# --- Rate limiting stops the batch ---------------------------------------


def test_rate_limit_stops_subsequent_calls(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # First login rate-limits; the rest of the batch should NOT be
    # asked about (don't keep burning against the same window).
    called = _stub_fetch(monkeypatch, {
        "first": _Outcome(name=None, cacheable=False, rate_limited=True),
    })
    out = resolve_logins(
        ["first", "second", "third"], verbose=Verbosity.QUIET,
    )
    # `first` was attempted; the rest were not.
    assert called == ["first"]
    # And nothing got cached (transient failure on `first`, the rest
    # weren't asked) — so a future call could retry.
    assert "second" not in out
    assert "third" not in out


def test_rate_limit_doesnt_block_later_runs(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # If a run rate-limits without caching, the NEXT run can retry.
    _stub_fetch(monkeypatch, {
        "first": _Outcome(name=None, cacheable=False, rate_limited=True),
    })
    resolve_logins(["first"], verbose=Verbosity.QUIET)
    # Next "run": rate limit lifted, normal response.
    called2 = _stub_fetch(monkeypatch, {
        "first": _Outcome(name="Real Name", cacheable=True),
    })
    out = resolve_logins(["first"], verbose=Verbosity.QUIET)
    assert called2 == ["first"]
    assert out["first"].name == "Real Name"


# --- Edge cases ----------------------------------------------------------


def test_empty_logins_no_op(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = _stub_fetch(monkeypatch, {})
    out = resolve_logins([], verbose=Verbosity.QUIET)
    assert out == {}
    assert called == []


def test_falsy_login_filtered_out(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # An empty-string login in the batch must not become an empty-
    # path URL request. Same for None passed in.
    called = _stub_fetch(monkeypatch, {})
    out = resolve_logins(["", "alice"], verbose=Verbosity.QUIET)
    # Only `alice` is attempted; empty string is dropped silently.
    assert called == ["alice"]
    assert "" not in out


def test_corrupt_cache_file_is_ignored(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A cache file we can't parse is treated as empty rather than
    # crashing the gather. The next run will refetch and overwrite.
    cache_file = Path(_cache_path())
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    cache_file.write_text("{not json")
    _stub_fetch(monkeypatch, {
        "alice": _Outcome(name="Alice", cacheable=True),
    })
    out = resolve_logins(["alice"], verbose=Verbosity.QUIET)
    assert out["alice"].name == "Alice"


# --- End-to-end via build_registry ----------------------------------------


def test_build_registry_upgrades_login_to_real_name(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole point: a GitHub-only contributor with no matching
    email gets their canonical_name upgraded from `kmadhavan-msft` to
    a real name after build_registry runs."""
    from conftest import write_github_archive
    from ietf_llm.people import build_registry

    write_github_archive(
        isolated_home, "wg", "org/repo",
        [{
            "number": 1,
            "title": "Issue",
            "state": "open",
            "author": "kmadhavan-msft",
            "createdAt": "2026-01-01T00:00:00Z",
            "updatedAt": "2026-01-01T00:00:00Z",
            "labels": [],
            "body": "body",
            "comments": [],
        }],
    )
    monkeypatch.setattr(github_users, "_fetch_one", lambda login, headers, verbose: (
        _Outcome(name="Krishna Madhavan", cacheable=True)
        if login == "kmadhavan-msft"
        else _Outcome(name=None, cacheable=False)
    ))

    registry = build_registry("wg", verbose=Verbosity.QUIET)
    person = next(p for p in registry.persons if "kmadhavan-msft" in p.github_logins)
    assert person.canonical_name == "Krishna Madhavan"


def test_build_registry_leaves_login_when_no_name(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the GitHub user has no `name` set, we leave the canonical
    name as the login rather than emitting a placeholder."""
    from conftest import write_github_archive
    from ietf_llm.people import build_registry

    write_github_archive(
        isolated_home, "wg", "org/repo",
        [{
            "number": 1,
            "title": "T",
            "state": "open",
            "author": "anon-user",
            "createdAt": "2026-01-01T00:00:00Z",
            "updatedAt": "2026-01-01T00:00:00Z",
            "labels": [],
            "body": "body",
            "comments": [],
        }],
    )
    monkeypatch.setattr(github_users, "_fetch_one", lambda login, headers, verbose: (
        _Outcome(name=None, cacheable=True)  # API responded; no name set
    ))

    registry = build_registry("wg", verbose=Verbosity.QUIET)
    person = next(p for p in registry.persons if "anon-user" in p.github_logins)
    assert person.canonical_name == "anon-user"


def test_build_registry_skips_users_with_email_match(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Users where the existing email/local-part heuristic already
    found a canonical name don't need a GitHub API lookup. Avoid the
    wasted request."""
    from conftest import write_eml, write_github_archive
    from ietf_llm.people import build_registry

    # Mail-list message gives us "Mark Nottingham <mnot@mnot.net>".
    write_eml(
        isolated_home, "wg", "list", 1,
        subject="Topic", from_addr="Mark Nottingham <mnot@mnot.net>",
        date="Mon, 01 Jan 2025 10:00:00 +0000",
    )
    # GitHub issue author "mnot" — local-part of mnot@mnot.net matches.
    write_github_archive(
        isolated_home, "wg", "org/repo",
        [{
            "number": 1,
            "title": "T",
            "state": "open",
            "author": "mnot",
            "createdAt": "2026-01-01T00:00:00Z",
            "updatedAt": "2026-01-01T00:00:00Z",
            "labels": [],
            "body": "body",
            "comments": [],
        }],
    )
    called: List[str] = []
    monkeypatch.setattr(github_users, "_fetch_one", lambda login, headers, verbose: (
        called.append(login) or _Outcome(name=None, cacheable=False)
    ))

    build_registry("wg", verbose=Verbosity.QUIET)
    # The heuristic linker already gave `mnot` a real name; no API call.
    assert called == []
