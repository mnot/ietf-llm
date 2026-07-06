"""Tests for ietf_llm.export.directory — the local-mirror sink.

Verifies the user-facing contract:
- fresh export copies eligible files (flattened from cache subdirs)
- re-export with no changes is a no-op
- stale files at the destination are pruned
- upstream file changes propagate
- .json archives and PDFs stay out of the mirror
- bundled mode (default) collapses per-thread / per-issue files into
  per-year / per-repo bundles for NotebookLM-friendly source counts
- bundle=False emits the fully granular per-file dump
"""

from __future__ import annotations

from pathlib import Path

from ietf_llm import export
from ietf_llm.log import Verbosity

from conftest import write_cache_file


def _mk_corpus(home: Path) -> None:
    """Build a small synthetic post-reorg cache for export tests."""
    write_cache_file(home, "wg", "charter.txt", "charter")
    write_cache_file(home, "wg", "drafts/draft-foo-00.txt", "draft")
    write_cache_file(home, "wg", "digests/index.md", "# digest")
    write_cache_file(home, "wg", "github/x.json", "{}")  # excluded


def test_fresh_export_copies_text_files(isolated_home: Path, tmp_path: Path) -> None:
    _mk_corpus(isolated_home)
    dest = tmp_path / "dest"
    export.directory("wg", str(dest), verbose=Verbosity.QUIET)
    names = sorted(p.name for p in dest.iterdir())
    # Flattened names: cache subdir + filename joined by `-`.
    assert names == [
        "charter.txt",
        "digests-index.md",
        "drafts-draft-foo-00.txt",
    ]


def test_export_excludes_json(isolated_home: Path, tmp_path: Path) -> None:
    _mk_corpus(isolated_home)
    dest = tmp_path / "dest"
    export.directory("wg", str(dest), verbose=Verbosity.QUIET)
    # The raw github archive JSON is internal and must not be exported.
    assert not any(p.suffix == ".json" for p in dest.iterdir())


def test_re_export_is_noop(isolated_home: Path, tmp_path: Path) -> None:
    _mk_corpus(isolated_home)
    dest = tmp_path / "dest"
    export.directory("wg", str(dest), verbose=Verbosity.QUIET)
    changes = export.directory("wg", str(dest), verbose=Verbosity.QUIET)
    assert changes == 0


def test_upstream_change_propagates(isolated_home: Path, tmp_path: Path) -> None:
    _mk_corpus(isolated_home)
    dest = tmp_path / "dest"
    export.directory("wg", str(dest), verbose=Verbosity.QUIET)
    write_cache_file(isolated_home, "wg", "charter.txt", "charter v2")
    changes = export.directory("wg", str(dest), verbose=Verbosity.QUIET)
    assert changes == 1
    assert (dest / "charter.txt").read_text() == "charter v2"


def test_stale_dest_file_is_pruned(isolated_home: Path, tmp_path: Path) -> None:
    _mk_corpus(isolated_home)
    dest = tmp_path / "dest"
    export.directory("wg", str(dest), verbose=Verbosity.QUIET)
    # Drop a file at the dest that isn't in the cache.
    (dest / "stale-leftover.txt").write_text("stale")
    changes = export.directory("wg", str(dest), verbose=Verbosity.QUIET)
    assert changes == 1
    assert not (dest / "stale-leftover.txt").exists()


def test_hidden_files_at_dest_preserved(isolated_home: Path, tmp_path: Path) -> None:
    # Pruning should leave dotfiles alone (e.g. .DS_Store, .gitignore).
    _mk_corpus(isolated_home)
    dest = tmp_path / "dest"
    dest.mkdir()
    (dest / ".DS_Store").write_text("macos cruft")
    export.directory("wg", str(dest), verbose=Verbosity.QUIET)
    assert (dest / ".DS_Store").exists()


def test_no_cache_returns_zero(isolated_home: Path, tmp_path: Path) -> None:
    # Calling export without any prior gather should not crash, just
    # report zero work done.
    dest = tmp_path / "dest"
    changes = export.directory("wg-never-gathered", str(dest), verbose=Verbosity.QUIET)
    assert changes == 0


# --- Bundling (default) ---------------------------------------------------


def test_bundled_export_collapses_threads_by_year(
    isolated_home: Path, tmp_path: Path,
) -> None:
    # 4 thread files spanning two years should collapse to two
    # year-bundle files in the export.
    for date in ("2025-03-01", "2025-09-15"):
        write_cache_file(
            isolated_home, "wg",
            f"threads/{date}-topic.md",
            f"# Topic ({date})\n\nbody\n",
        )
    for date in ("2026-04-10", "2026-05-20"):
        write_cache_file(
            isolated_home, "wg",
            f"threads/{date}-other-topic.md",
            f"# Topic ({date})\n\nbody\n",
        )
    dest = tmp_path / "dest"
    export.directory("wg", str(dest), verbose=Verbosity.QUIET)
    names = sorted(p.name for p in dest.iterdir())
    assert "threads-2025.md" in names
    assert "threads-2026.md" in names
    # And no per-thread files leaked through.
    assert not any(n.startswith("threads-2025-") for n in names)
    # Each bundle contains its year's threads.
    bundle_2025 = (dest / "threads-2025.md").read_text()
    assert "2025-03-01" in bundle_2025
    assert "2025-09-15" in bundle_2025
    assert "2026" not in bundle_2025  # year separation is strict


def test_bundled_export_collapses_issues_by_repo(
    isolated_home: Path, tmp_path: Path,
) -> None:
    # 3 per-issue files in one repo and 1 in another → two repo bundles.
    for n in (1, 100, 2):
        write_cache_file(
            isolated_home, "wg",
            f"issues/ietf-wg-aipref-drafts/{n}.md",
            f"# Issue #{n}\n\nbody\n",
        )
    write_cache_file(
        isolated_home, "wg",
        "issues/other-repo/1.md", "# Issue #1\n\nbody\n",
    )
    dest = tmp_path / "dest"
    export.directory("wg", str(dest), verbose=Verbosity.QUIET)
    names = sorted(p.name for p in dest.iterdir())
    assert "issues-ietf-wg-aipref-drafts.md" in names
    assert "issues-other-repo.md" in names
    # Issues sorted numerically inside the bundle (so #100 comes
    # after #2, not after #1 alphabetically).
    bundle = (dest / "issues-ietf-wg-aipref-drafts.md").read_text()
    pos1 = bundle.index("# Issue #1")
    pos2 = bundle.index("# Issue #2")
    pos100 = bundle.index("# Issue #100")
    assert pos1 < pos2 < pos100


def test_bundled_export_keeps_meetings_and_drafts_per_file(
    isolated_home: Path, tmp_path: Path,
) -> None:
    # Meetings, drafts, and digests stay one-per-file — only threads
    # and issues collapse. Each of those is a substantial standalone
    # artefact worth citing distinctly.
    write_cache_file(
        isolated_home, "wg",
        "meetings/ietf125/minutes.md", "# minutes\n",
    )
    write_cache_file(
        isolated_home, "wg",
        "drafts/draft-ietf-wg-foo-00.txt", "draft body",
    )
    write_cache_file(
        isolated_home, "wg", "digests/index.md", "# idx",
    )
    dest = tmp_path / "dest"
    export.directory("wg", str(dest), verbose=Verbosity.QUIET)
    names = sorted(p.name for p in dest.iterdir())
    assert "meetings-ietf125-minutes.md" in names
    assert "drafts-draft-ietf-wg-foo-00.txt" in names
    assert "digests-index.md" in names


def test_no_bundle_emits_per_file(
    isolated_home: Path, tmp_path: Path,
) -> None:
    # Explicit opt-out keeps every thread / issue as its own file.
    write_cache_file(
        isolated_home, "wg",
        "threads/2025-03-01-topic.md", "# t\n\nbody\n",
    )
    write_cache_file(
        isolated_home, "wg",
        "issues/repo/1.md", "# Issue #1\n\nbody\n",
    )
    dest = tmp_path / "dest"
    export.directory("wg", str(dest), verbose=Verbosity.QUIET, bundle=False)
    names = sorted(p.name for p in dest.iterdir())
    assert "threads-2025-03-01-topic.md" in names
    assert "issues-repo-1.md" in names
    # And NO year/repo bundles.
    assert "threads-2025.md" not in names
    assert "issues-repo.md" not in names


def test_bundle_file_count_for_active_wg_corpus(
    isolated_home: Path, tmp_path: Path,
) -> None:
    # The point of bundling: an active WG's 150 threads + 100 issues
    # collapse to ~2 year bundles + 1 repo bundle, well under
    # NotebookLM's 50-source free-tier ceiling. This test sanity-checks
    # the count at a representative scale.
    for i in range(75):
        write_cache_file(
            isolated_home, "wg",
            f"threads/2025-{(i % 12) + 1:02d}-{(i % 28) + 1:02d}-t{i}.md",
            "body\n",
        )
    for i in range(75):
        write_cache_file(
            isolated_home, "wg",
            f"threads/2026-{(i % 12) + 1:02d}-{(i % 28) + 1:02d}-t{i}.md",
            "body\n",
        )
    for i in range(100):
        write_cache_file(
            isolated_home, "wg",
            f"issues/the-repo/{i}.md", f"# Issue #{i}\n\nbody\n",
        )
    dest = tmp_path / "dest"
    export.directory("wg", str(dest), verbose=Verbosity.QUIET)
    names = list(dest.iterdir())
    # 250 source files → just 3 bundles.
    assert len(names) == 3
    assert sorted(p.name for p in names) == [
        "issues-the-repo.md",
        "threads-2025.md",
        "threads-2026.md",
    ]
