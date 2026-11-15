"""A GitHub archive is third-party content: its `repo` and each record's `number`
become path components, and a wrong type anywhere must not abort a gather.

The tests drive the real readers and writers over archives on disk, not
hand-built dicts passed straight to a renderer.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from ietf_llm.digest.issues import _build_issues_digest
from ietf_llm.digest.pulls import _build_pulls_digest
from ietf_llm.digest.summarizer import _Summarizer
from ietf_llm.gather.sources import github
from ietf_llm.gather.sources.github_records import write_record_files
from ietf_llm.gather.sources.github_schema import normalise_archive, normalise_cached
from ietf_llm.gather.sources.issue_files import write_issue_files
from ietf_llm.gather.sources.pull_files import write_pull_files
from ietf_llm.log import Verbosity
from ietf_llm.paths import get_wg_file_cache_dir
from ietf_llm.people import Registry, _ingest_github

from conftest import make_issue, make_pull

Q = Verbosity.QUIET


def _write_archive(wg: str, name: str, payload: Any) -> str:
    """Put `payload` at `<cache>/<wg>/files/github/<name>`; return the files dir."""
    cache = get_wg_file_cache_dir(wg)
    os.makedirs(os.path.join(cache, "github"), exist_ok=True)
    with open(os.path.join(cache, "github", name), "w", encoding="utf-8") as fh:
        json.dump(payload, fh)
    return cache


def _everything_reads(wg: str, cache: str) -> None:
    """Every consumer of the archives, none of which may raise."""
    write_issue_files(wg, cache, verbose=Q)
    write_pull_files(cache, verbose=Q)
    _ingest_github(wg, Registry(), Q)
    _build_issues_digest(cache, wg, _Summarizer(None, Q), Q)
    _build_pulls_digest(cache, wg, _Summarizer(None, Q), Q)
    out = os.path.join(cache, "out.txt")
    github.process_github_issues(os.path.join(cache, "github", "owner-repo.json"), out)
    assert not list(github.iter_issue_archives(os.path.join(cache, "nonexistent")))


# --- traversal --------------------------------------------------------------


def test_a_traversing_number_writes_nothing_outside_the_corpus(isolated_home: Path) -> None:
    outside = isolated_home / "OUTSIDE"
    outside.mkdir()
    cache = get_wg_file_cache_dir("wg")
    # Relative to issues/<slug>/ and pulls/<slug>/, so on main this lands in OUTSIDE.
    hostile = os.path.relpath(outside / "pwned", os.path.join(cache, "issues", "owner-repo"))
    _write_archive(
        "wg",
        "owner-repo.json",
        {
            "repo": "owner/repo",
            "issues": [make_issue(1, "ok"), make_issue(hostile, "bad")],  # type: ignore[arg-type]
            "pulls": [make_pull(2, "ok"), make_pull(hostile, "bad")],  # type: ignore[arg-type]
        },
    )
    write_issue_files("wg", cache, verbose=Q)
    write_pull_files(cache, verbose=Q)
    assert os.listdir(outside) == []
    assert os.path.isfile(os.path.join(cache, "issues", "owner-repo", "1.md"))
    assert os.path.isfile(os.path.join(cache, "pulls", "owner-repo", "2.md"))
    assert sorted(os.listdir(os.path.join(cache, "issues", "owner-repo"))) == ["1.md"]


@pytest.mark.parametrize("repo", ["..", "../..", "/etc", "a/../..", "", None, 7])
def test_a_repo_that_is_not_the_archives_own_name_is_ignored(
    isolated_home: Path, repo: Any
) -> None:
    cache = _write_archive(
        "wg",
        "owner-repo.json",
        {"repo": repo, "issues": [make_issue(7, "t")], "pulls": [make_pull(8, "t")]},
    )
    write_issue_files("wg", cache, verbose=Q)
    write_pull_files(cache, verbose=Q)
    assert os.path.isfile(os.path.join(cache, "issues", "owner-repo", "7.md"))
    assert os.path.isfile(os.path.join(cache, "pulls", "owner-repo", "8.md"))
    assert not os.path.exists(os.path.join(cache, "7.md"))  # not the corpus root


def test_the_write_loop_refuses_a_directory_outside_its_root(isolated_home: Path) -> None:
    """A destination the writer is handed can still be wrong (a file named
    `..json` makes the slug `.`); the check, not the normaliser, stops it."""
    cache = _write_archive("wg", "owner-repo.json", {"repo": "o/r", "issues": [make_issue(1, "t")]})
    outside = isolated_home / "elsewhere"
    outside.mkdir()
    written = write_record_files(
        os.path.join(cache, "github"),
        os.path.join(cache, "issues"),
        "issues",
        lambda _repo: str(outside),
        lambda _repo, n: os.path.join(str(outside), f"{n}.md"),
        lambda _repo, record: "x",
        "issue",
        Q,
    )
    assert written == []
    assert os.listdir(outside) == []


# --- malformed shapes --------------------------------------------------------

_SHAPES = {
    "top-level string": "text",
    "top-level list": [1, 2],
    "issues null": {"repo": "owner/repo", "issues": None},
    "issues entry not a dict": {"repo": "owner/repo", "issues": [1, "x", None]},
    "label as object": {
        "repo": "owner/repo",
        "issues": [make_issue(1, "t") | {"labels": [{"name": "bug"}, 3, None]}],
    },
    "comment as string": {
        "repo": "owner/repo",
        "issues": [make_issue(1, "t") | {"comments": ["hi", {"author": ["x"]}]}],
    },
    "author as object": {
        "repo": "owner/repo",
        "issues": [make_issue(1, "t") | {"author": {"login": "x"}, "title": 5, "body": []}],
    },
    "pulls shapes": {
        "repo": "owner/repo",
        "pulls": [
            make_pull(1, "t") | {"reviews": ["x", {"comments": "y", "author": 4}]},
            make_pull(2, "t") | {"mergeCommit": "abc", "comments": {"a": 1}, "labels": "x"},
            "x",
        ],
    },
    "labels not a list": {"repo": "owner/repo", "issues": [], "labels": {"name": "x"}},
    "timestamp not a string": {"repo": "owner/repo", "timestamp": 12, "issues": []},
}


@pytest.mark.parametrize("shape", list(_SHAPES))
def test_no_malformed_shape_aborts_a_reader(isolated_home: Path, shape: str) -> None:
    cache = _write_archive("wg", "owner-repo.json", _SHAPES[shape])
    _everything_reads("wg", cache)


def test_a_dropped_record_leaves_the_rest(isolated_home: Path) -> None:
    cache = _write_archive(
        "wg",
        "owner-repo.json",
        {"repo": "owner/repo", "issues": [make_issue(1, "kept"), {"number": "x"}, make_issue(2, "kept")]},
    )
    write_issue_files("wg", cache, verbose=Q)
    assert sorted(os.listdir(os.path.join(cache, "issues", "owner-repo"))) == ["1.md", "2.md"]


# --- the normaliser itself ---------------------------------------------------


def test_numbers_are_ints_or_the_record_is_dropped() -> None:
    out = normalise_archive(
        {"issues": [{"number": "12"}, {"number": 3}, {"number": True}, {"number": -1},
                    {"number": "1/../2"}, {"number": 1.5}, {"number": None}, {}]},
        "o/r",
    )
    assert out is not None
    assert [i["number"] for i in out["issues"]] == [12, 3]


def test_labels_become_names_and_text_fields_are_text() -> None:
    out = normalise_archive(
        {"issues": [{"number": 1, "labels": ["a", {"name": "b"}, {"x": 1}, 4], "title": 9, "author": None}]},
        "o/r",
    )
    assert out is not None
    issue = out["issues"][0]
    assert issue["labels"] == ["a", "b"]
    assert "title" not in issue
    assert issue["author"] is None


def test_the_input_is_not_mutated_and_absent_keys_stay_absent() -> None:
    raw = {"repo": "x/y", "issues": [{"number": "5", "comments": ["bad"]}]}
    out = normalise_archive(raw, "o/r")
    assert raw == {"repo": "x/y", "issues": [{"number": "5", "comments": ["bad"]}]}
    assert out is not None
    assert "pulls" not in out and "labels" not in out
    assert out["repo"] == "o/r"
    assert out["issues"][0]["comments"] == []


def test_a_cached_archive_keeps_a_well_formed_repo_for_display() -> None:
    renamed = normalise_cached({"repo": "someone/else", "issues": []}, "owner-repo.json")
    assert renamed is not None and renamed["repo"] == "someone/else"
    for claimed in ("..", "a/..", "/etc", None, 7):
        bad = normalise_cached({"repo": claimed, "issues": []}, "owner-repo.json")
        assert bad is not None and bad["repo"] == "owner-repo"


def test_a_renamed_repo_keeps_its_name_and_files_under_the_file_name_slug(
    isolated_home: Path,
) -> None:
    cache = _write_archive(
        "wg",
        "quicwg-qmux.json",
        {"repo": "kazuho/draft-qmux", "issues": [make_issue(1, "t")], "pulls": [make_pull(2, "t")]},
    )
    write_issue_files("wg", cache, verbose=Q)
    write_pull_files(cache, verbose=Q)
    issue = open(os.path.join(cache, "issues", "quicwg-qmux", "1.md"), encoding="utf-8").read()
    assert "**Repository:** kazuho/draft-qmux" in issue
    assert "https://github.com/kazuho/draft-qmux/issues/1" in issue
    assert os.path.isfile(os.path.join(cache, "pulls", "quicwg-qmux", "2.md"))
    assert not os.path.exists(os.path.join(cache, "issues", "kazuho-draft-qmux"))
    _build_issues_digest(cache, "wg", _Summarizer(None, Q), Q)
    assert "issues/quicwg-qmux/1.md" in open(os.path.join(cache, "digests", "issues.md")).read()


def test_an_archive_file_named_to_make_the_slug_dots_writes_nothing_outside(
    isolated_home: Path,
) -> None:
    cache = _write_archive("wg", "...json", {"repo": "o/r", "issues": [make_issue(1, "t")]})
    write_issue_files("wg", cache, verbose=Q)
    assert not os.path.exists(os.path.join(cache, "1.md"))
    assert not os.path.exists(os.path.join(cache, "issues", "1.md"))


def test_the_orphan_sweep_does_not_follow_a_symlinked_repo_dir(isolated_home: Path) -> None:
    precious = isolated_home / "precious"
    precious.mkdir()
    (precious / "keep.md").write_text("x")
    cache = _write_archive("wg", "owner-repo.json", {"repo": "o/r", "issues": [make_issue(1, "t")]})
    os.makedirs(os.path.join(cache, "issues"))
    os.symlink(precious, os.path.join(cache, "issues", "owner-repo"))
    write_issue_files("wg", cache, verbose=Q)
    assert os.listdir(precious) == ["keep.md"]


# --- hostile numbers and parsers --------------------------------------------


@pytest.mark.parametrize("number", [10**300, 10**12, "9" * 300, "٣"])
def test_a_number_too_large_to_be_a_file_name_is_dropped(isolated_home: Path, number: Any) -> None:
    cache = _write_archive(
        "wg", "owner-repo.json", {"repo": "o/r", "issues": [make_issue(1, "ok"), {"number": number}]}
    )
    write_issue_files("wg", cache, verbose=Q)
    assert os.listdir(os.path.join(cache, "issues", "owner-repo")) == ["1.md"]


@pytest.mark.parametrize(
    "body", ["[" * 100000 + "]" * 100000, '{"issues": [{"number": ' + "9" * 5000 + "}]}"]
)
def test_a_pathological_archive_on_disk_is_skipped_not_fatal(isolated_home: Path, body: str) -> None:
    cache = get_wg_file_cache_dir("wg")
    os.makedirs(os.path.join(cache, "github"))
    with open(os.path.join(cache, "github", "owner-repo.json"), "w", encoding="utf-8") as fh:
        fh.write(body)
    _everything_reads("wg", cache)


@pytest.mark.parametrize(
    "name, ok",
    [("owner/repo", True), ("owner/.github", True), ("own_er/repo.js", True), ("a/..", False),
     ("a/.", False), ("ö/ü", False), ("a/b/c", False), ("../b", False), ("a b/c", False), ("a", False)],
)
def test_valid_repo(name: str, ok: bool) -> None:
    from ietf_llm.gather.sources.github_schema import valid_repo

    assert valid_repo(name) is ok


# --- download ----------------------------------------------------------------


class _Resp:
    def __init__(self, status: int, data: Any = None) -> None:
        self.status_code = status
        self._data = data

    def json(self) -> Any:
        return self._data


def _download(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, repo: str, body: Any) -> Any:
    monkeypatch.setattr(github, "governed_get", lambda url, **_kw: _Resp(200, body))
    dest = tmp_path / "archive.json"
    ok = github.download_github_issues(repo, str(dest), verbose=Q)
    return ok, (json.loads(dest.read_text()) if dest.exists() else None)


def test_download_stores_the_configured_repo_and_clean_records(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    ok, saved = _download(
        monkeypatch,
        tmp_path,
        "owner/repo",
        {"repo": "../..", "issues": [make_issue("../../x", "bad"), make_issue(4, "good")]},
    )
    assert ok
    assert saved["repo"] == "owner/repo"
    assert [i["number"] for i in saved["issues"]] == [4]


def test_download_of_a_non_object_archive_falls_back_to_the_api(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(github, "_fetch_all_issues", lambda *a, **k: [make_issue(1, "api")])
    ok, saved = _download(monkeypatch, tmp_path, "owner/repo", "just a string")
    assert ok
    assert [i["number"] for i in saved["issues"]] == [1]


@pytest.mark.parametrize("name", ["owner", "../repo", "a/b/c", "own er/repo", "o/r r"])
def test_download_refuses_a_repo_name_that_is_not_owner_slash_repo(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, name: str
) -> None:
    seen: list[str] = []
    monkeypatch.setattr(github, "governed_get", lambda url, **_kw: seen.append(url))
    assert github.download_github_issues(name, str(tmp_path / "a.json"), verbose=Q) is False
    assert seen == []


def test_an_archive_whose_records_all_fail_is_not_saved_and_the_api_is_used(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An honest format change must not replace good data with an empty archive."""
    (tmp_path / "archive.json").write_text('{"repo": "owner/repo", "issues": [{"number": 1}]}')
    monkeypatch.setattr(github, "_fetch_all_issues", lambda *a, **k: [make_issue(5, "api")])
    ok, saved = _download(monkeypatch, tmp_path, "owner/repo", {"issues": [{"number": "#12"}]})
    assert ok
    assert [i["number"] for i in saved["issues"]] == [5]


def test_an_archive_that_cannot_be_parsed_falls_back_to_the_api(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class _Bad:
        status_code = 200

        def json(self) -> Any:
            raise RecursionError

    monkeypatch.setattr(github, "governed_get", lambda url, **_kw: _Bad())
    monkeypatch.setattr(github, "_fetch_all_issues", lambda *a, **k: [make_issue(5, "api")])
    dest = tmp_path / "a.json"
    assert github.download_github_issues("owner/repo", str(dest), verbose=Q)
    assert [i["number"] for i in json.loads(dest.read_text())["issues"]] == [5]
