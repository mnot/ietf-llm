"""Tests for GitHub repo discovery (ietf_llm.gather.repo_discovery).

Covers the contract:
- enumerate a group's repos from its Datatracker `github_org` / `github_repo`
  resources, pre-filter on the cheap listing fields, probe survivors for
  draft sources + issue activity, and split into high-confidence (auto-track)
  vs lower-confidence suggestions;
- a GitHub throttle / outage marks the result `incomplete` and is surfaced as
  a caveat rather than a misleading "nothing found";
- `autotrack_github` folds the high-confidence repos into a gather's
  `--github` once per corpus, withholding the run-once marker when a throttle
  left the scan incomplete.

The network is stubbed by monkeypatching `_GhClient.get` (URL → canned
response) and `get_group_resources`; `discover_group_repos` is given
`live_draft_keys` directly so it never touches the documents API.
"""

from __future__ import annotations

import argparse
from typing import Any, Dict, Optional, Tuple

import pytest

import ietf_llm.gather.repo_discovery as rd
from ietf_llm import config
from ietf_llm.utils import Verbosity

# A timestamp far in the future is always "recent" relative to the staleness
# window; one far in the past is always stale — so these tests don't depend on
# the wall clock.
RECENT = "2099-01-01T00:00:00Z"
STALE = "2000-01-01T00:00:00Z"


def _repo(
    name: str,
    *,
    has_issues: bool = True,
    open_issues: int = 3,
    pushed_at: str = RECENT,
    archived: bool = False,
    fork: bool = False,
) -> Dict[str, Any]:
    return {
        "full_name": f"fakewg/{name}",
        "has_issues": has_issues,
        "open_issues_count": open_issues,
        "pushed_at": pushed_at,
        "archived": archived,
        "fork": fork,
    }


def _install_github(
    monkeypatch: pytest.MonkeyPatch, routes: Dict[str, "Tuple[Optional[int], Any]"]
) -> None:
    """Replace `_GhClient.get` with a fake matching `routes` by URL substring
    (first match wins). Records rate-limit / unreachable incidents like the
    real client so the `incomplete` path can be exercised."""

    def fake_get(
        self: rd._GhClient,
        url: str,
        params: Optional[Dict[str, Any]] = None,  # noqa: ARG001
    ) -> "Tuple[Optional[int], Any]":
        for needle, (status, data) in routes.items():
            if needle in url:
                if status in (403, 429):
                    self.incidents.add("rate_limited")
                elif status is None:
                    self.incidents.add("unreachable")
                return status, data
        return 404, None

    monkeypatch.setattr(rd._GhClient, "get", fake_get)


# --- pure helpers ---------------------------------------------------------


@pytest.mark.parametrize(
    "name,expected",
    [
        ("draft-ietf-fake-thing.md", "draft-ietf-fake-thing"),
        ("draft-ietf-fake-thing.xml", "draft-ietf-fake-thing"),
        ("draft-foo-bar.org", "draft-foo-bar"),
        ("README.md", None),
        ("rfc9110.xml", None),
        ("notes.txt", None),
    ],
)
def test_draft_file_regex(name: str, expected: Optional[str]) -> None:
    match = rd._DRAFT_FILE_RE.match(name)
    assert (match.group(1) if match else None) == expected


def test_match_key_strips_revision_and_latest() -> None:
    assert rd._match_key("draft-ietf-fake-thing-07") == "draft-ietf-fake-thing"
    assert rd._match_key("draft-ietf-fake-cache-latest") == "draft-ietf-fake-cache"
    assert rd._match_key("Draft-IETF-Fake-Thing") == "draft-ietf-fake-thing"


def test_parse_resources_splits_orgs_and_repos() -> None:
    owners, repos = rd._parse_resources(
        (
            ("github_org", "repositories", "https://github.com/httpwg/"),
            ("github_repo", "repo", "https://github.com/owner/spec"),
            ("webpage", "home", "https://example.org/"),
            ("github_org", "dup", "https://github.com/httpwg"),  # deduped
        )
    )
    assert owners == ["httpwg"]
    assert repos == ["owner/spec"]


def test_prefilter_rejects_archived_fork_no_issues_and_stale() -> None:
    assert rd._passes_prefilter(rd._candidate_from_obj(_repo("ok")), STALE) is True
    assert (
        rd._passes_prefilter(rd._candidate_from_obj(_repo("a", archived=True)), STALE)
        is False
    )
    assert (
        rd._passes_prefilter(rd._candidate_from_obj(_repo("f", fork=True)), STALE)
        is False
    )
    assert (
        rd._passes_prefilter(
            rd._candidate_from_obj(_repo("n", has_issues=False)), STALE
        )
        is False
    )
    # pushed_at older than the threshold -> stale.
    assert (
        rd._passes_prefilter(rd._candidate_from_obj(_repo("old", pushed_at=STALE)), RECENT)
        is False
    )


def test_high_confidence_needs_draft_match_and_active_issues() -> None:
    cand = rd.RepoCandidate("o/r", True, 1, RECENT, False, False)
    cand.draft_files = ["draft-x.md"]
    assert cand.has_drafts is True
    assert cand.high_confidence is False  # no WG match yet
    cand.wg_draft_matches = ["draft-x"]
    assert cand.high_confidence is False  # issues not active
    cand.issues_active = True
    assert cand.high_confidence is True


def test_incident_note() -> None:
    assert rd._incident_note(set()) is None
    assert "GITHUB_TOKEN" in (rd._incident_note({"rate_limited"}) or "")
    assert "network" in (rd._incident_note({"unreachable"}) or "")


# --- discover_group_repos -------------------------------------------------


def _full_org(monkeypatch: pytest.MonkeyPatch) -> None:
    """A fake httpwg-shaped org: one tracked repo, one drafts-but-no-match
    repo, one draft-match-but-quiet-issues repo, plus repos that pre-filter or
    draft-probe out."""
    monkeypatch.setattr(
        rd,
        "get_group_resources",
        lambda wg: (("github_org", "repos", "https://github.com/fakewg/"),),
    )
    _install_github(
        monkeypatch,
        {
            "/orgs/fakewg/repos": (
                200,
                [
                    _repo("spec-active", open_issues=9),
                    _repo("spec-finished"),
                    _repo("spec-quiet"),
                    _repo("theme"),  # no draft files
                    _repo("wiki", has_issues=False),  # pre-filtered
                    _repo("mirror", archived=True),  # pre-filtered
                ],
            ),
            "spec-active/contents": (
                200,
                [
                    {"type": "file", "name": "draft-ietf-fake-thing.md"},
                    {"type": "file", "name": "README.md"},
                ],
            ),
            "spec-active/issues": (200, [{"updated_at": RECENT}]),
            "spec-finished/contents": (
                200,
                [{"type": "file", "name": "draft-ietf-fake-old.md"}],
            ),
            "spec-finished/issues": (200, [{"updated_at": RECENT}]),
            "spec-quiet/contents": (
                200,
                [{"type": "file", "name": "draft-ietf-fake-other.md"}],
            ),
            "spec-quiet/issues": (200, [{"updated_at": STALE}]),
            "theme/contents": (200, [{"type": "file", "name": "style.css"}]),
            "theme/issues": (200, [{"updated_at": RECENT}]),
        },
    )


def test_discovery_classifies_repos(monkeypatch: pytest.MonkeyPatch) -> None:
    _full_org(monkeypatch)
    result = rd.discover_group_repos(
        "fakewg",
        live_draft_keys={"draft-ietf-fake-thing", "draft-ietf-fake-other"},
        verbose=Verbosity.QUIET,
    )
    # spec-active: draft matches an active WG draft AND issues live -> track.
    assert result.high_confidence == ["fakewg/spec-active"]
    # spec-finished (draft, no WG match) and spec-quiet (WG match, dead issues)
    # are surfaced but not tracked. theme (no drafts) / wiki / mirror are gone.
    assert set(result.suggestions) == {"fakewg/spec-finished", "fakewg/spec-quiet"}
    names = {c.full_name for c in result.candidates}
    assert names == {"fakewg/spec-active", "fakewg/spec-finished", "fakewg/spec-quiet"}
    assert result.incomplete is False
    assert result.note is None


def test_discovery_no_resources(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rd, "get_group_resources", lambda wg: ())
    result = rd.discover_group_repos("fakewg", live_draft_keys=set())
    assert result.candidates == []
    assert result.high_confidence == []
    assert "no GitHub org" in (result.note or "")


def test_discovery_rate_limited_is_incomplete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        rd,
        "get_group_resources",
        lambda wg: (("github_org", "repos", "https://github.com/fakewg/"),),
    )
    _install_github(monkeypatch, {"/orgs/fakewg/repos": (403, None)})
    result = rd.discover_group_repos(
        "fakewg", live_draft_keys=set(), verbose=Verbosity.QUIET
    )
    assert result.candidates == []
    assert result.incomplete is True
    assert "rate-limited" in (result.note or "")


def test_ghclient_records_5xx_as_incident(monkeypatch: pytest.MonkeyPatch) -> None:
    """A GitHub 5xx (after the session's own retries) is recorded as an
    incident, not treated as a real negative — otherwise a partial scan looks
    complete and burns the auto-track one-shot."""

    class _Resp:
        status_code = 503

        @staticmethod
        def json() -> Any:  # pragma: no cover - not reached on a 5xx
            return None

    monkeypatch.setattr(
        rd, "http_session", lambda: type("S", (), {"get": lambda *a, **k: _Resp()})()
    )
    client = rd._GhClient(token=None)
    status, data = client.get("https://api.github.com/orgs/fakewg/repos")
    assert status == 503
    assert data is None
    assert "unreachable" in client.incidents


# --- format_discovery -----------------------------------------------------


def test_format_lists_candidates_and_recommendation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _full_org(monkeypatch)
    text = rd.format_discovery(
        rd.discover_group_repos(
            "fakewg",
            live_draft_keys={"draft-ietf-fake-thing", "draft-ietf-fake-other"},
            verbose=Verbosity.QUIET,
        )
    )
    assert "✓ track" in text and "fakewg/spec-active" in text
    assert 'github=["fakewg/spec-active"]' in text


def test_format_appends_throttle_caveat() -> None:
    result = rd.DiscoveryResult(
        wg="fakewg",
        candidates=[rd.RepoCandidate("fakewg/r", True, 1, RECENT, False, False)],
        suggestions=["fakewg/r"],
        note="Some GitHub requests were rate-limited",
        incomplete=True,
    )
    result.candidates[0].draft_files = ["draft-x.md"]
    text = rd.format_discovery(result)
    assert "rate-limited" in text


# --- autotrack_github -----------------------------------------------------


def _args(wg: str = "fakewg", github: Optional[list] = None) -> argparse.Namespace:
    return argparse.Namespace(wg=wg, github=github)


def test_autotrack_folds_high_confidence_and_marks_done(
    isolated_home: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        rd,
        "discover_group_repos",
        lambda wg, verbose=Verbosity.STATUS: rd.DiscoveryResult(
            wg=wg, high_confidence=["fakewg/spec-active"]
        ),
    )
    args = _args()
    notes: list = []
    rd.autotrack_github(
        args, {}, group_backed=True, scope="gather", verbose=Verbosity.QUIET,
        note_fn=notes.append,
    )
    assert args.github == ["fakewg/spec-active"]
    assert config.load("fakewg", "gather").get("github_discovered") is True
    # The outcome is surfaced for the status record, not just stderr.
    assert any("fakewg/spec-active" in n for n in notes)


def test_autotrack_skips_when_github_already_configured(
    isolated_home: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    called = {"n": 0}

    def _spy(wg: str, verbose: object = None) -> rd.DiscoveryResult:  # noqa: ARG001
        called["n"] += 1
        return rd.DiscoveryResult(wg=wg)

    monkeypatch.setattr(rd, "discover_group_repos", _spy)
    # Persisted github -> no-op.
    rd.autotrack_github(
        _args(), {"github": ["o/r"]}, group_backed=True, scope="gather",
        verbose=Verbosity.QUIET,
    )
    # Marker already set -> no-op.
    rd.autotrack_github(
        _args(), {"github_discovered": True}, group_backed=True, scope="gather",
        verbose=Verbosity.QUIET,
    )
    # Not group-backed -> no-op.
    rd.autotrack_github(
        _args(), {}, group_backed=False, scope="gather", verbose=Verbosity.QUIET
    )
    assert called["n"] == 0


def test_autotrack_does_not_burn_oneshot_when_incomplete(
    isolated_home: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        rd,
        "discover_group_repos",
        lambda wg, verbose=Verbosity.STATUS: rd.DiscoveryResult(
            wg=wg, incomplete=True, note="throttled"
        ),
    )
    args = _args()
    notes: list = []
    rd.autotrack_github(
        args, {}, group_backed=True, scope="gather", verbose=Verbosity.QUIET,
        note_fn=notes.append,
    )
    assert args.github is None
    # Marker withheld so the next gather retries discovery.
    assert "github_discovered" not in config.load("fakewg", "gather")
    # The throttle is surfaced (so the client isn't left thinking it has repos).
    assert any("throttled" in n for n in notes)


def test_autotrack_commits_when_incomplete_but_some_found(
    isolated_home: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A partial scan that still auto-tracked repos commits them: the marker is
    written (the persisted set now owns the choice), and the note points at an
    explicit re-scan rather than a forced re-gather, which would short-circuit
    on that persisted set."""
    monkeypatch.setattr(
        rd,
        "discover_group_repos",
        lambda wg, verbose=Verbosity.STATUS: rd.DiscoveryResult(
            wg=wg, high_confidence=["fakewg/spec-active"], incomplete=True
        ),
    )
    args = _args()
    notes: list = []
    rd.autotrack_github(
        args, {}, group_backed=True, scope="gather", verbose=Verbosity.QUIET,
        note_fn=notes.append,
    )
    assert args.github == ["fakewg/spec-active"]
    assert config.load("fakewg", "gather").get("github_discovered") is True
    blob = " ".join(notes).lower()
    assert "discover-github" in blob or "suggest_github_repos" in blob
    # The misleading "re-gather with force" recovery hint is gone.
    assert "force" not in blob
