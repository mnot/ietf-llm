"""LLM summaries in the digests: bounded, labelled as model output, and not
dropped for the rest of a gather by a single failed call."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ietf_llm.digest.issues import _build_issues_digest
from ietf_llm.digest.pulls import _build_pulls_digest
from ietf_llm.digest.summarizer import (
    _MAX_CONSECUTIVE_FAILURES,
    _MAX_SUMMARY_CHARS,
    SUMMARY_COLUMN,
    _Summarizer,
)
from ietf_llm.digest.threads import _build_threads_digest
from ietf_llm.log import Verbosity
from ietf_llm.paths import get_wg_file_cache_dir

from conftest import make_issue, make_pull, write_eml, write_github_archive

Q = Verbosity.QUIET


class _Reply:
    def __init__(self, text: str) -> None:
        self._text = text

    def text(self) -> str:
        return self._text


class _Model:
    """Stands in for an `llm` model: `prompt(text).text()`."""

    def __init__(self, *replies: Any) -> None:
        self._replies = list(replies)
        self.calls = 0

    def prompt(self, _text: str) -> _Reply:
        reply = self._replies[min(self.calls, len(self._replies) - 1)]
        self.calls += 1
        if isinstance(reply, Exception):
            raise reply
        return _Reply(reply)


def _summarizer(model: _Model) -> _Summarizer:
    summarizer = _Summarizer(None, Q)
    summarizer.model = model
    return summarizer


def test_summary_is_capped() -> None:
    long = "word " * 500
    out = _summarizer(_Model(long)).summarize("p")
    assert len(out) <= _MAX_SUMMARY_CHARS
    assert out.endswith("…")


def test_cap_counts_the_escaped_pipes() -> None:
    out = _summarizer(_Model("|" * 400)).summarize("p")
    assert len(out.replace("|", "\\|")) <= _MAX_SUMMARY_CHARS


def test_a_summary_at_the_limit_is_untouched() -> None:
    exact = "x" * _MAX_SUMMARY_CHARS
    assert _summarizer(_Model(exact)).summarize("p") == exact


def test_short_summary_is_untouched() -> None:
    assert _summarizer(_Model('"Fine."')).summarize("p") == "Fine."


def test_one_failure_does_not_disable_the_summariser() -> None:
    model = _Model(RuntimeError("503"), "Back again.")
    summarizer = _summarizer(model)
    assert summarizer.summarize("p") == ""
    assert summarizer.active()
    assert summarizer.summarize("p") == "Back again."


def test_consecutive_failures_disable_it() -> None:
    model = _Model(RuntimeError("no key"))
    summarizer = _summarizer(model)
    for _ in range(_MAX_CONSECUTIVE_FAILURES):
        assert summarizer.summarize("p") == ""
    assert not summarizer.active()
    calls = model.calls
    summarizer.summarize("p")
    assert model.calls == calls


def test_success_resets_the_failure_count() -> None:
    fail = RuntimeError("503")
    model = _Model(*([fail] * (_MAX_CONSECUTIVE_FAILURES - 1) + ["ok"] + [fail] * 2))
    summarizer = _summarizer(model)
    for _ in range(len(model._replies)):
        summarizer.summarize("p")
    assert summarizer.active()


def test_threads_digest_labels_and_caps_the_summary(isolated_home: Path) -> None:
    write_eml(
        isolated_home, "wg", "list", 1, "Cookie partitioning",
        "Alice <a@x>", "Mon, 01 Jan 2025 10:00:00 +0000",
    )
    cache = get_wg_file_cache_dir("wg")
    _build_threads_digest("wg", cache, _summarizer(_Model("x " * 600)), Q)
    text = (Path(cache) / "digests/threads.md").read_text()
    assert f"| Last | {SUMMARY_COLUMN} |" in text
    row = next(ln for ln in text.splitlines() if "Cookie partitioning" in ln)
    assert len(row) < 300 + 200


def test_issues_digest_labels_the_summary(isolated_home: Path) -> None:
    write_github_archive(isolated_home, "wg", "org/repo", [make_issue(1, "One")])
    cache = get_wg_file_cache_dir("wg")
    _build_issues_digest(cache, "wg", _summarizer(_Model("A summary.")), Q)
    text = (Path(cache) / "digests/issues.md").read_text()
    assert f"| File | {SUMMARY_COLUMN} |" in text


def test_unsummarised_digests_keep_the_file_column(isolated_home: Path) -> None:
    write_github_archive(isolated_home, "wg", "org/repo", [make_issue(1, "One")])
    cache = get_wg_file_cache_dir("wg")
    _build_issues_digest(cache, "wg", _Summarizer(None, Q), Q)
    text = (Path(cache) / "digests/issues.md").read_text()
    assert SUMMARY_COLUMN not in text


def test_pulls_digest_labels_the_summary(isolated_home: Path) -> None:
    write_github_archive(
        isolated_home, "wg", "org/repo", [make_issue(1, "One")], pulls=[make_pull(2, "A PR")]
    )
    cache = get_wg_file_cache_dir("wg")
    _build_pulls_digest(cache, "wg", _summarizer(_Model("A summary.")), Q)
    text = (Path(cache) / "digests/pulls.md").read_text()
    assert f"| File | {SUMMARY_COLUMN} |" in text
