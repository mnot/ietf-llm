"""Tests for per-PR Markdown files and the pull-requests digest.

`gather/sources/pull_files.py` writes one `pulls/<repo-slug>/<N>.md` per
pull request in the archive; `digest/pulls.py` catalogues them. These
tests cover:

- file shape (header, disposition, review verdicts, outline, sections)
- the two places PRs deliberately diverge from issues: State is
  normalised (MERGED → CLOSED) so the shared search facet keeps one
  meaning, and the closing note is only rendered for unmerged PRs
- closes-#N extraction from title and body
- review rendering: bodiless approvals summarised in the header only,
  inline comments nested under their review
- archives with no `pulls` key (the REST-fallback shape) write nothing
- the digest's merge-commit column and its state filter
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from ietf_llm.digest.pulls import _build_pulls_digest
from ietf_llm.digest.query import query_digest
from ietf_llm.digest.summarizer import _Summarizer
from ietf_llm.gather.sources.pull_files import (
    _closes,
    _normalised_state,
    _participants,
    _review_summary,
    write_pull_files,
)
from ietf_llm.log import Verbosity
from ietf_llm.paths import digest_path, get_wg_file_cache_dir
from ietf_llm.people import Registry

from conftest import make_pull, make_review, write_github_archive

REPO = "org/repo"
SLUG = "org-repo"


def _pull_text(wg: str, number: int) -> str:
    return (Path(get_wg_file_cache_dir(wg)) / "pulls" / SLUG / f"{number}.md").read_text()


def _seed(home: Path, pulls: list[dict], issues: list[dict] | None = None) -> str:
    write_github_archive(home, "wg", REPO, issues or [], pulls=pulls)
    return get_wg_file_cache_dir("wg")


# --- pure helpers ---------------------------------------------------------


def test_closes_reads_title_and_body_and_dedupes() -> None:
    pull = {"number": 5, "title": "Fixes #12", "body": "Also closes #7, fixes #12."}
    assert _closes(pull) == [12, 7]


def test_closes_ignores_self_reference() -> None:
    assert _closes({"number": 9, "title": "closes #9", "body": ""}) == []


def test_closes_needs_a_number() -> None:
    assert _closes({"number": 1, "title": "fixes the parser", "body": ""}) == []


def test_closes_accepts_the_url_and_cross_repo_forms() -> None:
    # 73 of 3684 real PRs declare their closure only as a URL; without
    # this the Closes column — the whole point of the blame walk — is
    # blank for them.
    pull = {
        "number": 5,
        "title": "",
        "body": (
            "Fixes https://github.com/org/repo/issues/12 and "
            "closes org/repo#7."
        ),
    }
    assert _closes(pull, REPO) == [12, 7]


def test_closes_ignores_another_repos_issue() -> None:
    # A genuine cross-repo closure names an issue this corpus may not
    # have, whose number means something else here — rendering it as a
    # bare #N would point the reader at the wrong record.
    pull = {
        "number": 5,
        "title": "",
        "body": "Fixes https://github.com/other/proj/issues/12",
    }
    assert _closes(pull, REPO) == []


def test_merged_normalises_to_closed_for_the_shared_facet() -> None:
    assert _normalised_state({"state": "MERGED"}) == "CLOSED"
    assert _normalised_state({"state": "CLOSED"}) == "CLOSED"
    assert _normalised_state({"state": "OPEN"}) == "OPEN"


def test_participants_includes_reviewers_and_merger() -> None:
    pull = {
        "author": "alice",
        "comments": [{"author": "bob"}],
        "reviews": [make_review(author="carol"), make_review(author="bob")],
        "mergedBy": "dave",
    }
    assert _participants(pull, None) == ["alice", "bob", "carol", "dave"]


def test_review_summary_groups_by_verdict_objections_first() -> None:
    pull = {
        "reviews": [
            make_review(author="bob", state="APPROVED"),
            make_review(author="carol", state="CHANGES_REQUESTED"),
            make_review(author="bob", state="APPROVED"),  # deduped
        ]
    }
    summary = _review_summary(pull, None)
    assert summary.startswith("changes requested by carol")
    assert "approved by bob" in summary


def test_review_summary_names_each_reviewer_once() -> None:
    # Reviewing is iterative — several COMMENTED reviews then an
    # APPROVED is the normal shape. Naming someone under every verdict
    # they ever used reads as more reviewers than there were.
    pull = {
        "reviews": [
            make_review(author="bob", state="COMMENTED"),
            make_review(author="bob", state="APPROVED"),
            make_review(author="carol", state="COMMENTED"),
        ]
    }
    summary = _review_summary(pull, None)
    assert summary == "approved by bob  ·  commented on by carol"


# --- write_pull_files end-to-end ------------------------------------------


def test_writes_one_file_per_pull(isolated_home: Path) -> None:
    cache = _seed(isolated_home, [make_pull(3, "Third"), make_pull(4, "Fourth")])
    written = write_pull_files(cache, verbose=Verbosity.QUIET)
    assert len(written) == 2
    assert "# Pull request #3: Third" in _pull_text("wg", 3)


def test_header_carries_disposition_and_merge_commit(isolated_home: Path) -> None:
    cache = _seed(isolated_home, [make_pull(3, "Third", merged_by="bob")])
    write_pull_files(cache, verbose=Verbosity.QUIET)
    text = _pull_text("wg", 3)
    # Normalised for the facet…
    assert "**State:** CLOSED" in text
    # …with the real outcome, the merger, and the full oid alongside.
    assert "**Disposition:** merged by bob" in text
    assert "0123456789abcdef0123456789abcdef01234567" in text
    assert "**URL:** https://github.com/org/repo/pull/3" in text
    assert "**Branch:** `branch-3` → `main`" in text


def test_unmerged_close_says_so_and_quotes_the_last_comment(
    isolated_home: Path,
) -> None:
    pull = make_pull(
        6,
        "Rejected idea",
        state="CLOSED",
        merged_by=None,
        comments=[
            {"author": "bob", "createdAt": "2026-05-03T00:00:00Z", "body": "early"},
            {
                "author": "carol",
                "createdAt": "2026-05-04T00:00:00Z",
                "body": "superseded by #57",
            },
        ],
    )
    cache = _seed(isolated_home, [pull])
    write_pull_files(cache, verbose=Verbosity.QUIET)
    text = _pull_text("wg", 6)
    assert "**Disposition:** closed without merging" in text
    assert "**Closing note:**" in text
    assert "> superseded by #57" in text


def test_merged_pull_has_no_closing_note(isolated_home: Path) -> None:
    """A merged PR's last comment is usually "thanks" — the merge itself is
    the resolution, so we don't dress a pleasantry up as rationale."""
    pull = make_pull(
        7,
        "Merged",
        comments=[
            {"author": "bob", "createdAt": "2026-05-04T00:00:00Z", "body": "thanks!"}
        ],
    )
    cache = _seed(isolated_home, [pull])
    write_pull_files(cache, verbose=Verbosity.QUIET)
    assert "**Closing note:**" not in _pull_text("wg", 7)


def test_closes_line_rendered(isolated_home: Path) -> None:
    cache = _seed(isolated_home, [make_pull(8, "Tidy up", body="Fixes #29.")])
    write_pull_files(cache, verbose=Verbosity.QUIET)
    assert "**Closes:** #29" in _pull_text("wg", 8)


def test_bodiless_approvals_summarised_not_sectioned(isolated_home: Path) -> None:
    """Two-thirds of real reviews are bare approvals; giving each one a
    section would bury the substance and litter the index with empty
    chunks. They belong in the header tally only."""
    pull = make_pull(
        9,
        "Reviewed",
        reviews=[make_review(author="bob"), make_review(author="carol")],
    )
    cache = _seed(isolated_home, [pull])
    write_pull_files(cache, verbose=Verbosity.QUIET)
    text = _pull_text("wg", 9)
    assert "**Review verdicts:** approved by bob, carol" in text
    assert "## Discussion" not in text
    assert "_(review: APPROVED)_" not in text


def test_review_with_body_gets_a_section(isolated_home: Path) -> None:
    pull = make_pull(
        10,
        "Contested",
        reviews=[
            make_review(author="carol", state="CHANGES_REQUESTED", body="no thanks")
        ],
    )
    cache = _seed(isolated_home, [pull])
    write_pull_files(cache, verbose=Verbosity.QUIET)
    text = _pull_text("wg", 10)
    assert "### [2] 2026-05-02 00:00 — carol _(review: CHANGES_REQUESTED)_" in text
    assert "no thanks" in text


def test_inline_comments_nest_under_their_review(isolated_home: Path) -> None:
    pull = make_pull(
        11,
        "Line notes",
        reviews=[
            make_review(
                author="carol",
                state="COMMENTED",
                comments=[
                    {"originalPosition": 7, "body": "MUST NOT?"},
                    {"originalPosition": 9, "body": "typo"},
                ],
            )
        ],
    )
    cache = _seed(isolated_home, [pull])
    write_pull_files(cache, verbose=Verbosity.QUIET)
    text = _pull_text("wg", 11)
    assert "_2 inline comment(s) on the diff:_" in text
    assert "- MUST NOT?" in text
    assert "- typo" in text


def test_comments_and_reviews_share_one_numbering(isolated_home: Path) -> None:
    """The chunker keys chunk_idx off `### [N]`, so the two event kinds
    have to interleave into a single ordered run."""
    pull = make_pull(
        12,
        "Mixed",
        comments=[
            {"author": "bob", "createdAt": "2026-05-03T00:00:00Z", "body": "hi"}
        ],
        reviews=[
            make_review(author="carol", body="first", created_at="2026-05-02T00:00:00Z")
        ],
    )
    cache = _seed(isolated_home, [pull])
    write_pull_files(cache, verbose=Verbosity.QUIET)
    text = _pull_text("wg", 12)
    assert text.index("### [2] 2026-05-02") < text.index("### [3] 2026-05-03")


def test_registry_canonicalises_names(isolated_home: Path) -> None:
    registry = Registry()
    person = registry.add_github_author("alice")
    assert person is not None
    person.canonical_name = "Alice Wonderland"
    cache = _seed(isolated_home, [make_pull(13, "Named", author="alice")])
    write_pull_files(cache, registry=registry, verbose=Verbosity.QUIET)
    assert "**Opened by:** Alice Wonderland" in _pull_text("wg", 13)


def test_archive_without_pulls_writes_nothing(isolated_home: Path) -> None:
    """The REST fallback builds `{repo, timestamp, issues}` and skips PRs;
    such a corpus gets no PR tree rather than an empty one."""
    write_github_archive(isolated_home, "wg", REPO, [])
    cache = get_wg_file_cache_dir("wg")
    assert write_pull_files(cache, verbose=Verbosity.QUIET) == []
    assert not (Path(cache) / "pulls").exists()


def test_stale_pull_files_are_swept(isolated_home: Path) -> None:
    cache = _seed(isolated_home, [make_pull(3, "Third"), make_pull(4, "Fourth")])
    write_pull_files(cache, verbose=Verbosity.QUIET)
    _seed(isolated_home, [make_pull(3, "Third")])
    write_pull_files(cache, verbose=Verbosity.QUIET)
    assert (Path(cache) / "pulls" / SLUG / "3.md").exists()
    assert not (Path(cache) / "pulls" / SLUG / "4.md").exists()


def test_missing_cache_dir_is_not_an_error(tmp_path: Path) -> None:
    assert write_pull_files(str(tmp_path / "nope"), verbose=Verbosity.QUIET) == []


def test_malformed_archive_is_skipped(isolated_home: Path) -> None:
    cache = _seed(isolated_home, [make_pull(3, "Third")])
    (Path(cache) / "github" / "broken.json").write_text("{not json")
    assert len(write_pull_files(cache, verbose=Verbosity.QUIET)) == 1


# --- digest ---------------------------------------------------------------


def _digest(cache: str) -> str:
    out = _build_pulls_digest(
        cache, "wg", _Summarizer(None, Verbosity.QUIET), Verbosity.QUIET
    )
    assert out is not None
    return Path(out).read_text()


def test_digest_carries_merge_commit_and_closes(isolated_home: Path) -> None:
    """The merge oid is what makes `git blame` → PR → issue possible
    offline, so it has to be in the catalogue, not just the per-PR file."""
    cache = _seed(isolated_home, [make_pull(3, "Third", body="Closes #1.")])
    text = _digest(cache)
    assert "| 0123456 | #1 |" in text
    assert "`pulls/org-repo/3.md`" in text


def test_digest_absent_when_no_archive_has_pulls(isolated_home: Path) -> None:
    write_github_archive(isolated_home, "wg", REPO, [])
    cache = get_wg_file_cache_dir("wg")
    assert (
        _build_pulls_digest(
            cache, "wg", _Summarizer(None, Verbosity.QUIET), Verbosity.QUIET
        )
        is None
    )


def test_stale_digest_removed_when_pulls_disappear(isolated_home: Path) -> None:
    cache = _seed(isolated_home, [make_pull(3, "Third")])
    _digest(cache)
    write_github_archive(isolated_home, "wg", REPO, [])
    assert (
        _build_pulls_digest(
            cache, "wg", _Summarizer(None, Verbosity.QUIET), Verbosity.QUIET
        )
        is None
    )
    assert not Path(digest_path(cache, "pulls")).exists()


def test_digest_state_filter_treats_merged_as_closed(isolated_home: Path) -> None:
    """`state="closed"` has to cover merged PRs — otherwise the single most
    common outcome is invisible to the obvious filter."""
    cache = _seed(
        isolated_home,
        [
            make_pull(3, "Merged"),
            make_pull(4, "Dropped", state="CLOSED", merged_by=None),
            make_pull(5, "Live", state="OPEN", merged_by=None),
        ],
    )
    _digest(cache)
    path = digest_path(cache, "pulls")
    rows = lambda md: [  # noqa: E731
        line for line in md.splitlines() if line.startswith("| ") and "|" in line[2:]
    ]
    closed = query_digest(path, "pulls", state="closed")
    assert "Merged" in closed and "Dropped" in closed and "Live" not in closed
    merged = query_digest(path, "pulls", state="merged")
    assert "Merged" in merged and "Dropped" not in merged
    assert len(rows(query_digest(path, "pulls", state="open"))) == 2  # header + 1


def test_digest_totals_split_merged_from_closed(isolated_home: Path) -> None:
    cache = _seed(
        isolated_home,
        [
            make_pull(3, "Merged"),
            make_pull(4, "Dropped", state="CLOSED", merged_by=None),
            make_pull(5, "Live", state="OPEN", merged_by=None),
        ],
    )
    assert "1 open, 1 merged, 1 closed without merging" in _digest(cache)


def test_digest_tolerates_a_pull_without_a_merge_commit(isolated_home: Path) -> None:
    cache = _seed(isolated_home, [make_pull(3, "Third", merge_oid=None)])
    assert "| 3 | MERGED |" in _digest(cache)


# --- reader-side plumbing -------------------------------------------------


def test_get_issue_resolves_a_pull_by_number(isolated_home: Path) -> None:
    """GitHub numbers issues and PRs in one sequence, so a caller citing
    "#34" has no way to know which it is. One tool, both trees."""
    from ietf_llm.gather.sources.issue_files import write_issue_files
    from ietf_llm.mcp.drafts import _resolve_issue_file
    from conftest import make_issue

    cache = _seed(isolated_home, [make_pull(3, "Third")], issues=[make_issue(4, "Four")])
    write_pull_files(cache, verbose=Verbosity.QUIET)
    write_issue_files("wg", cache, verbose=Verbosity.QUIET)

    path, note = _resolve_issue_file(cache, "3", "")
    assert path is not None and path.endswith("pulls/org-repo/3.md") and note == ""
    path, note = _resolve_issue_file(cache, "4", "")
    assert path is not None and path.endswith("issues/org-repo/4.md")
    # Explicit repo also spans both trees.
    path, _ = _resolve_issue_file(cache, "3", REPO)
    assert path is not None and path.endswith("pulls/org-repo/3.md")
    path, note = _resolve_issue_file(cache, "99", "")
    assert path is None and "issue or PR #99" in note


def test_registry_ingests_pull_reviewers(isolated_home: Path) -> None:
    """Someone who reviews steadily but never files an issue was invisible
    to the people registry while it walked `issues` alone."""
    from ietf_llm.people import _ingest_github
    from conftest import make_issue

    write_github_archive(
        isolated_home,
        "wg",
        REPO,
        [make_issue(1, "One", author="alice")],
        pulls=[
            make_pull(
                2,
                "Two",
                author="bob",
                merged_by="dave",
                reviews=[make_review(author="carol")],
            )
        ],
    )
    registry = Registry()
    _ingest_github("wg", registry, Verbosity.QUIET)
    logins = {login for p in registry.persons for login in p.github_logins}
    assert {"alice", "bob", "carol", "dave"} <= logins


def test_pull_bodies_appended_to_a_filtered_pulls_digest(
    isolated_home: Path, monkeypatch
) -> None:
    """`include_bodies=True` has to cut at the PR files' own discussion
    heading, and keep everything above it."""
    import ietf_llm.mcp.digest as digest_mod

    cache = _seed(
        isolated_home,
        [
            make_pull(
                3,
                "Third",
                body="why this change",
                reviews=[make_review(author="carol", body="looks wrong")],
            )
        ],
    )
    write_pull_files(cache, verbose=Verbosity.QUIET)
    monkeypatch.setattr(digest_mod, "_safe_path", lambda wg, f: os.path.join(cache, f))
    out = digest_mod._append_issue_bodies("wg", "| 3 | `pulls/org-repo/3.md` |\n")
    assert "**Disposition:** merged by bob" in out
    assert "why this change" in out
    assert "## Discussion" not in out
    assert "looks wrong" not in out


def test_issue_body_containing_a_discussion_heading_is_not_truncated(
    isolated_home: Path, monkeypatch
) -> None:
    """Regression: the cutoff marker must be chosen by the file's tree, not
    by whichever heading appears first. Issue bodies contain arbitrary
    markdown, and `## Discussion` inside one is common (two real cases in
    the httpbis cache) — taking the earlier of the two markers dropped the
    whole description."""
    import ietf_llm.mcp.digest as digest_mod
    from ietf_llm.gather.sources.issue_files import write_issue_files
    from conftest import make_issue

    body = "## Background\n\ntext\n\n## Discussion\n\nthe substance\n"
    write_github_archive(isolated_home, "wg", REPO, [make_issue(4, "Four", body=body)])
    cache = get_wg_file_cache_dir("wg")
    write_issue_files("wg", cache, verbose=Verbosity.QUIET)
    monkeypatch.setattr(digest_mod, "_safe_path", lambda wg, f: os.path.join(cache, f))
    out = digest_mod._append_issue_bodies("wg", "| 4 | `issues/org-repo/4.md` |\n")
    assert "the substance" in out


def test_tally_positions_reads_pr_authors_without_the_review_tag(
    isolated_home: Path,
) -> None:
    """Regression: `_THREAD_MSG_RE` has to consume `_(review: VERDICT)_` and
    `_(opened pull request)_`, or a reviewer who also comments splits into
    two identities and renders as `bob _(review: APPROVED)_`."""
    from ietf_llm.people.positions import file_supports_tally, tally_thread

    pull = make_pull(
        3,
        "Third",
        author="alice",
        comments=[
            {"author": "bob", "createdAt": "2026-05-05T00:00:00Z", "body": "+1"}
        ],
        reviews=[
            make_review(
                author="bob",
                state="CHANGES_REQUESTED",
                body="I object to this",
                created_at="2026-05-03T00:00:00Z",
            )
        ],
    )
    cache = _seed(isolated_home, [pull])
    write_pull_files(cache, verbose=Verbosity.QUIET)
    assert file_supports_tally("pulls/org-repo/3.md")
    text = (Path(cache) / "pulls" / SLUG / "3.md").read_text()
    positions, _summary = tally_thread(text)
    senders = {p.sender for p in positions}
    assert senders == {"alice", "bob"}
    assert not any("review:" in s or "opened" in s for s in senders)


def test_get_by_url_resolves_a_pull_link_off_disk(
    isolated_home: Path, monkeypatch
) -> None:
    """PR files are not embedded, so the index-backed path can never match
    a `…/pull/N` link. Losing PR URLs from get_by_url would be a silent
    side effect of the indexing decision rather than a chosen one."""
    import ietf_llm.mcp.chunks as chunks_mod

    cache = _seed(isolated_home, [make_pull(3, "Third", body="why this change")])
    write_pull_files(cache, verbose=Verbosity.QUIET)
    monkeypatch.setattr(chunks_mod, "_files_dir", lambda wg: cache)
    monkeypatch.setattr(chunks_mod, "find_chunks_by_url", lambda wg, url: [])

    out = chunks_mod._github_url_from_file("wg", "https://github.com/org/repo/pull/3")
    assert out is not None
    assert "why this change" in out
    assert "`pulls/org-repo/3.md`" in out
    # Not a GitHub record URL, and a number this corpus doesn't have.
    assert chunks_mod._github_url_from_file("wg", "https://example.com/x") is None
    assert (
        chunks_mod._github_url_from_file("wg", "https://github.com/org/repo/pull/99")
        is None
    )


def test_get_chunk_text_on_a_pull_file_explains_the_omission(
    isolated_home: Path,
) -> None:
    """An opaque "no chunks indexed" would read as a gather bug; it is a
    deliberate exclusion, so say so and name what does work."""
    from ietf_llm.mcp.chunks import _chunk_not_found_hint

    hint = _chunk_not_found_hint("wg", "pulls/org-repo/3.md", 0)
    assert "not in the embedding index by design" in hint
    assert "get_issue" in hint and "read_digest" in hint


def test_citation_scan_walks_pull_files(isolated_home: Path) -> None:
    """`iter_thread_issue_md_files` gained `pulls/`, so a draft cited only
    in a PR body is found."""
    from ietf_llm.gather.sources.citations import scan_citations

    cache = _seed(
        isolated_home,
        [make_pull(3, "Third", body="Implements draft-ietf-wg-foo-03.")],
    )
    write_pull_files(cache, verbose=Verbosity.QUIET)
    citations = scan_citations(cache, verbose=Verbosity.QUIET)
    assert "draft-ietf-wg-foo" in citations
    assert any(
        c.file == "pulls/org-repo/3.md" for c in citations["draft-ietf-wg-foo"]
    )


def test_json_shape_round_trips(isolated_home: Path) -> None:
    """Guard against the writer and the fixture drifting apart: what we
    seed is what a real archive looks like, keys and all."""
    path = write_github_archive(
        isolated_home, "wg", REPO, [], pulls=[make_pull(3, "Third")]
    )
    data = json.loads(path.read_text())
    assert set(data) == {"repo", "timestamp", "issues", "pulls"}
    assert data["pulls"][0]["mergeCommit"]["oid"].startswith("0123456")
