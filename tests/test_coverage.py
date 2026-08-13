"""Tests for `coverage` — the reader-side descriptor of how far back a gather
reaches (the window) and which sources it holds.

Source detection reads on-disk artifacts; the window derives from the
`last-gathered` sentinel and the persisted `months`. The GitHub-archive
fixtures use the exact `{"repo": ..., "issues": [...]}` shape the gather
writer emits (and that `gather.sources.issue_files` reads back via `data["repo"]`),
so the repo-name reader is exercised against real writer output, not an
invented schema.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from ietf_llm import config, coverage
from ietf_llm.freshness import _sentinel_path
from ietf_llm.months import DEFAULT_MONTHS
from ietf_llm.paths import get_cache_dir

from conftest import write_cache_file

SCOPE = "gather"


def _files_dir(wg: str) -> str:
    return os.path.join(get_cache_dir(), wg, "files")


def _set_gathered(wg: str, when: str) -> None:
    """Point the sentinel at a fixed ISO date so the window is deterministic."""
    path = Path(_sentinel_path(wg))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(when, encoding="utf-8")


def _write_archive(  # pylint: disable=too-many-arguments
    home: Path,
    wg: str,
    slug: str,
    repo: str,
    issues: "list[int] | None" = None,
    pulls: "list[int] | None" = None,
    timestamp: str = "2026-01-01T00:00:00",
) -> None:
    """A GitHub archive in the writer's shape (the `repo` field is verbatim
    `owner/repo`; the slug filename is lossy and deliberately not relied on).

    `issues` and `pulls` are numbers only — the ceiling reader wants nothing
    else from a record, and the two arrays are separate in the real archive."""
    body = {
        "repo": repo,
        "timestamp": timestamp,
        "issues": [{"number": n} for n in issues or []],
        "pulls": [{"number": n} for n in pulls or []],
    }
    write_cache_file(home, wg, f"github/{slug}.json", json.dumps(body))


# --- window_months ---------------------------------------------------------


def test_window_months_defaults_when_unset(isolated_home: Path) -> None:
    # A default-window gather doesn't persist `months`; absence means default.
    assert coverage.window_months("wg") == DEFAULT_MONTHS


def test_window_months_reads_persisted(isolated_home: Path) -> None:
    config.save("wg", SCOPE, {"months": 6})
    assert coverage.window_months("wg") == 6


def test_window_months_preserves_zero(isolated_home: Path) -> None:
    # A forced `--months 0` persists 0 (all history); it must not be rounded
    # up to the default, which would mis-report unbounded coverage as 12 months.
    config.save("wg", SCOPE, {"months": 0})
    assert coverage.window_months("wg") == 0


# --- coverage_start_label --------------------------------------------------


def test_start_label_none_without_sentinel(isolated_home: Path) -> None:
    assert coverage.coverage_start_label("never") is None


def test_start_label_subtracts_window(isolated_home: Path) -> None:
    _set_gathered("wg", "2026-06-08T00:00:00Z")
    config.save("wg", SCOPE, {"months": 6})
    # 6 * 30 = 180 days before 2026-06-08 lands in December 2025.
    assert coverage.coverage_start_label("wg") == "2025-12"


def test_start_label_uses_default_window(isolated_home: Path) -> None:
    _set_gathered("wg", "2026-06-08T00:00:00Z")
    # No persisted months → 12-month default → 360 days back.
    assert coverage.coverage_start_label("wg") == "2025-06"


def test_start_label_none_for_all_history(isolated_home: Path) -> None:
    # months=0 is unbounded: there's no floor to report, even with a sentinel.
    _set_gathered("wg", "2026-06-08T00:00:00Z")
    config.save("wg", SCOPE, {"months": 0})
    assert coverage.coverage_start_label("wg") is None


# --- github_repos / github_repo_count --------------------------------------


def test_github_repos_reads_verbatim_name(isolated_home: Path) -> None:
    # The dir slug lowercases and replaces `/`; the reader must recover the
    # real mixed-case owner/repo from the archive's `repo` field instead.
    _write_archive(isolated_home, "wg", "httpwg-http-core", "httpwg/http-core")
    assert coverage.github_repos(_files_dir("wg")) == ["httpwg/http-core"]


def test_github_repos_sorted_and_deduped(isolated_home: Path) -> None:
    _write_archive(isolated_home, "wg", "b-two", "org/two")
    _write_archive(isolated_home, "wg", "a-one", "org/one")
    assert coverage.github_repos(_files_dir("wg")) == ["org/one", "org/two"]


def test_github_repos_skips_malformed(isolated_home: Path) -> None:
    write_cache_file(isolated_home, "wg", "github/bad.json", "{not json")
    _write_archive(isolated_home, "wg", "ok", "org/ok")
    assert coverage.github_repos(_files_dir("wg")) == ["org/ok"]


def test_github_repos_empty_without_archives(isolated_home: Path) -> None:
    write_cache_file(isolated_home, "wg", "digests/index.md", "# x\n")
    assert coverage.github_repos(_files_dir("wg")) == []


def test_github_repo_count_counts_without_parsing(isolated_home: Path) -> None:
    # The count is by filename, so even an unparseable archive still counts —
    # it's one tracked repo whose JSON happens to be malformed.
    _write_archive(isolated_home, "wg", "a", "org/a")
    write_cache_file(isolated_home, "wg", "github/bad.json", "{not json")
    assert coverage.github_repo_count(_files_dir("wg")) == 2


def test_github_repo_count_zero_without_archives(isolated_home: Path) -> None:
    assert coverage.github_repo_count(_files_dir("wg")) == 0


# --- github_records / record_edge_line -------------------------------------


def test_ceiling_spans_issues_and_pulls(isolated_home: Path) -> None:
    # GitHub numbers issues and PRs in one sequence, so the edge of the record
    # is the max across both arrays. Taking either alone names the wrong
    # number: on the real httpwg/http-extensions archive the issues stop at
    # #3501 while the PRs reach #3502.
    _write_archive(
        isolated_home, "wg", "a", "org/a", issues=[3499, 3501], pulls=[3500, 3502]
    )
    assert coverage.github_records(_files_dir("wg"))[0].ceiling == 3502


def test_ceiling_none_when_archive_holds_nothing(isolated_home: Path) -> None:
    _write_archive(isolated_home, "wg", "a", "org/a")
    record = coverage.github_records(_files_dir("wg"))[0]
    assert record.ceiling is None
    # No ceiling means nothing to bound, so the line stays silent rather than
    # rendering a repo with an empty number.
    assert coverage.record_edge_line([record]) == ""


def test_built_date_comes_from_the_archive(isolated_home: Path) -> None:
    # Dated from the archive's own timestamp, not the gather: the archive is
    # fetched from the repo's published `archive.json`, which can be days older
    # than the gather that pulled it.
    _set_gathered("wg", "2026-08-11T00:00:00Z")
    _write_archive(
        isolated_home,
        "wg",
        "a",
        "org/a",
        issues=[7],
        timestamp="2026-08-06T01:49:14.562886+00:00",
    )
    line = coverage.record_edge_line(coverage.github_records(_files_dir("wg")))
    assert line == "org/a through #7 (archive built 2026-08-06)"


def test_built_dropped_when_timestamp_unusable(isolated_home: Path) -> None:
    # A junk timestamp loses the date, not the ceiling — the number is the
    # load-bearing half.
    _write_archive(isolated_home, "wg", "a", "org/a", issues=[7], timestamp="lately")
    assert coverage.record_edge_line(coverage.github_records(_files_dir("wg"))) == (
        "org/a through #7"
    )


def test_record_edge_line_joins_repos(isolated_home: Path) -> None:
    _write_archive(isolated_home, "wg", "a", "org/a", issues=[7])
    _write_archive(isolated_home, "wg", "b", "org/b", pulls=[12])
    line = coverage.record_edge_line(coverage.github_records(_files_dir("wg")))
    assert line == (
        "org/a through #7 (archive built 2026-01-01); "
        "org/b through #12 (archive built 2026-01-01)"
    )


# --- detect_sources --------------------------------------------------------


def test_detect_sources_full(isolated_home: Path) -> None:
    write_cache_file(isolated_home, "wg", "threads/2026-01-01-x.md", "t")
    write_cache_file(isolated_home, "wg", "drafts/draft-foo-bar-00.txt", "d")
    write_cache_file(isolated_home, "wg", "drafts/rfc9999.txt", "r")
    write_cache_file(isolated_home, "wg", "meetings/ietf125/minutes.md", "m")
    _write_archive(isolated_home, "wg", "org-repo", "org/repo")
    src = coverage.detect_sources(_files_dir("wg"))
    assert src.mailing_list and src.drafts and src.rfcs and src.meetings
    assert src.repos == ["org/repo"]
    assert src.repo_count == 1


def test_detect_sources_drafts_only(isolated_home: Path) -> None:
    write_cache_file(isolated_home, "wg", "drafts/draft-foo-00.txt", "d")
    src = coverage.detect_sources(_files_dir("wg"))
    assert src.drafts
    assert not src.rfcs and not src.mailing_list and not src.meetings
    assert src.repos == []
    assert src.repo_count == 0


def test_detect_sources_compact_counts_repos_without_names(
    isolated_home: Path,
) -> None:
    # The compact scan reports presence and a repo count, but doesn't parse
    # archives for verbatim names — `repos` stays empty, `repo_count` is set.
    write_cache_file(isolated_home, "wg", "threads/2026-01-01-x.md", "t")
    _write_archive(isolated_home, "wg", "a", "org/a")
    _write_archive(isolated_home, "wg", "b", "org/b")
    src = coverage.detect_sources_compact(_files_dir("wg"))
    assert src.mailing_list
    assert src.repo_count == 2
    assert src.repos == []


# --- window_line -----------------------------------------------------------


def test_window_line_for_list_corpus(isolated_home: Path) -> None:
    _set_gathered("wg", "2026-06-08T00:00:00Z")
    config.save("wg", SCOPE, {"months": 6})
    write_cache_file(isolated_home, "wg", "threads/2026-01-01-x.md", "t")
    line = coverage.window_line("wg", _files_dir("wg"))
    assert line is not None
    assert "mailing-list activity" in line
    assert "~2025-12" in line
    assert "6-mo window" in line
    # A list-only corpus has no non-windowed sources, so that caveat is
    # omitted — the line must not cite issues/drafts it doesn't hold.
    assert "not windowed" not in line


def test_window_line_fullset_clause_names_present_sources(
    isolated_home: Path,
) -> None:
    _set_gathered("wg", "2026-06-08T00:00:00Z")
    write_cache_file(isolated_home, "wg", "threads/2026-01-01-x.md", "t")
    write_cache_file(isolated_home, "wg", "drafts/draft-foo-00.txt", "d")
    _write_archive(isolated_home, "wg", "org-repo", "org/repo")
    line = coverage.window_line("wg", _files_dir("wg"))
    assert line is not None
    assert "GitHub issues/PRs and drafts are not windowed" in line
    # Unwindowed is not unbounded: the caveat has to say the record still
    # stops somewhere, or a zero over it reads as absence from the record.
    assert "end where the last gather did" in line


def test_window_line_fullset_clause_omits_absent_source(
    isolated_home: Path,
) -> None:
    # Drafts present but no GitHub issues: the caveat names drafts only.
    _set_gathered("wg", "2026-06-08T00:00:00Z")
    write_cache_file(isolated_home, "wg", "threads/2026-01-01-x.md", "t")
    write_cache_file(isolated_home, "wg", "drafts/draft-foo-00.txt", "d")
    line = coverage.window_line("wg", _files_dir("wg"))
    assert line is not None
    assert "drafts are not windowed" in line
    assert "GitHub" not in line


def test_window_line_names_both_when_present(isolated_home: Path) -> None:
    _set_gathered("wg", "2026-06-08T00:00:00Z")
    write_cache_file(isolated_home, "wg", "threads/2026-01-01-x.md", "t")
    write_cache_file(isolated_home, "wg", "meetings/ietf125/minutes.md", "m")
    line = coverage.window_line("wg", _files_dir("wg"))
    assert line is not None
    assert "mailing-list & meeting activity" in line


def test_window_line_none_without_windowed_sources(isolated_home: Path) -> None:
    # A draft/issue-only corpus has nothing the window bounds — no window line.
    _set_gathered("wg", "2026-06-08T00:00:00Z")
    write_cache_file(isolated_home, "wg", "drafts/draft-foo-00.txt", "d")
    _write_archive(isolated_home, "wg", "org-repo", "org/repo")
    assert coverage.window_line("wg", _files_dir("wg")) is None


def test_window_line_none_without_sentinel(isolated_home: Path) -> None:
    write_cache_file(isolated_home, "wg", "threads/2026-01-01-x.md", "t")
    assert coverage.window_line("wg", _files_dir("wg")) is None


# --- sources_line / compact_sources_line -----------------------------------


def test_sources_line_lists_repos_by_name(isolated_home: Path) -> None:
    write_cache_file(isolated_home, "wg", "threads/2026-01-01-x.md", "t")
    _write_archive(isolated_home, "wg", "a", "org/a")
    _write_archive(isolated_home, "wg", "b", "org/b")
    line = coverage.sources_line(_files_dir("wg"))
    assert line == "mailing list · GitHub issues (org/a, org/b)"


def test_sources_line_caps_repos(isolated_home: Path) -> None:
    for i in range(8):
        _write_archive(isolated_home, "wg", f"r{i}", f"org/r{i}")
    line = coverage.sources_line(_files_dir("wg"), repo_limit=6)
    assert "+2 more" in line
    assert "org/r7" not in line


def test_sources_line_empty_when_bare(isolated_home: Path) -> None:
    write_cache_file(isolated_home, "wg", "digests/index.md", "# x\n")
    assert coverage.sources_line(_files_dir("wg")) == ""


def test_compact_sources_counts_repos(isolated_home: Path) -> None:
    write_cache_file(isolated_home, "wg", "threads/2026-01-01-x.md", "t")
    write_cache_file(isolated_home, "wg", "drafts/draft-foo-00.txt", "d")
    write_cache_file(isolated_home, "wg", "meetings/ietf125/minutes.md", "m")
    _write_archive(isolated_home, "wg", "a", "org/a")
    _write_archive(isolated_home, "wg", "b", "org/b")
    assert (
        coverage.compact_sources_line(_files_dir("wg"))
        == "list · issues×2 · drafts · minutes"
    )


def test_compact_sources_singular_issue(isolated_home: Path) -> None:
    _write_archive(isolated_home, "wg", "a", "org/a")
    assert coverage.compact_sources_line(_files_dir("wg")) == "issues"
