"""Tests for the `ietf-llm-query` read-only CLI (dispatch + exit-code contract).

These exercise the CLI layer — argument dispatch and the stable exit codes a
skill branches on — not the underlying tool_* internals (covered elsewhere).
"""

from __future__ import annotations

import subprocess
import sys

import pytest

from ietf_llm import __version__, query_cli

from conftest import write_cache_file


def _run(argv, monkeypatch):
    monkeypatch.setattr("sys.argv", ["ietf-llm-query", *argv])
    with pytest.raises(SystemExit) as exc:
        query_cli.main()
    return exc.value.code


def test_version_prints_plain_semver(capsys, monkeypatch):
    assert _run(["--version"], monkeypatch) == 0
    assert capsys.readouterr().out.strip() == __version__


def test_missing_subcommand_is_usage_error(monkeypatch):
    assert _run([], monkeypatch) == query_cli.EXIT_USAGE


def test_unknown_corpus_exits_with_distinct_code(isolated_home, capsys, monkeypatch):
    # The gather seam: a not-yet-gathered corpus gets its own exit code so the
    # skill can route to `ietf-llm <corpus>` without parsing the message.
    code = _run(["overview", "x-nope-not-real-zzz"], monkeypatch)
    assert code == query_cli.EXIT_NO_CORPUS
    err = capsys.readouterr().err
    assert "Gather it first" in err


def test_unknown_corpus_never_creates_a_cache(isolated_home, monkeypatch):
    # Read-only: a mistyped name must not materialise a junk cache dir.
    _run(["list-labels", "x-typo-zzz"], monkeypatch)
    assert not (isolated_home / ".cache" / "ietf-llm" / "x-typo-zzz").exists()


def test_list_corpora_dispatches(isolated_home, capsys, monkeypatch):
    write_cache_file(isolated_home, "httpbis", "digests/index.md", "# x\n")
    assert _run(["list-corpora"], monkeypatch) == 0
    assert "httpbis" in capsys.readouterr().out


def test_read_digest_reads_seeded_corpus(isolated_home, capsys, monkeypatch):
    write_cache_file(
        isolated_home, "httpbis", "digests/index.md", "# Index\n\nhello world\n"
    )
    assert _run(["read-digest", "httpbis", "--kind", "index"], monkeypatch) == 0
    assert "hello world" in capsys.readouterr().out


def test_read_digest_rejects_bad_kind(isolated_home, monkeypatch):
    write_cache_file(isolated_home, "httpbis", "digests/index.md", "# x\n")
    # --kind is a locked enum; an off-list value is an argparse usage error.
    assert _run(["read-digest", "httpbis", "--kind", "bogus"], monkeypatch) == (
        query_cli.EXIT_USAGE
    )


def test_search_reports_unreachable_backend(isolated_home, capsys, monkeypatch):
    # A down embed backend must get its own exit code, not read as no-results:
    # search() swallows the failure, so the CLI probes the backend first.
    write_cache_file(isolated_home, "httpbis", "digests/index.md", "# x\n")
    monkeypatch.setattr(query_cli, "probe_embed_backend", lambda *a, **k: "boom")
    code = _run(["search", "httpbis", "caching"], monkeypatch)
    assert code == query_cli.EXIT_EMBED_UNREACHABLE
    assert "backend unreachable" in capsys.readouterr().err.lower()


def test_search_healthy_backend_dispatches(isolated_home, capsys, monkeypatch):
    write_cache_file(isolated_home, "httpbis", "digests/index.md", "# x\n")
    monkeypatch.setattr(query_cli, "probe_embed_backend", lambda *a, **k: None)
    monkeypatch.setattr(query_cli, "tool_search", lambda *a, **k: "the hits")
    assert _run(["search", "httpbis", "caching"], monkeypatch) == 0
    assert "the hits" in capsys.readouterr().out


def test_search_unknown_corpus_precedes_embed_probe(isolated_home, monkeypatch):
    # The corpus-absent check (exit 3) must fire before the embed probe, so a
    # typo is not masked by a backend error.
    called = []
    monkeypatch.setattr(
        query_cli, "probe_embed_backend", lambda *a, **k: called.append(1) or "boom"
    )
    assert _run(["search", "x-nope-zzz", "caching"], monkeypatch) == (
        query_cli.EXIT_NO_CORPUS
    )
    assert not called


def test_which_corpus_reports_unreachable_backend(isolated_home, monkeypatch):
    # Cross-corpus verbs probe any indexed corpus's backend.
    monkeypatch.setattr(query_cli, "any_indexed_wg", lambda: "httpbis")
    monkeypatch.setattr(query_cli, "probe_embed_backend", lambda *a, **k: "boom")
    assert _run(["which-corpus", "quic"], monkeypatch) == (
        query_cli.EXIT_EMBED_UNREACHABLE
    )


def test_list_sessions(isolated_home, capsys, monkeypatch):
    write_cache_file(
        isolated_home, "httpbis", "meetings/ietf125/minutes.md", "Date: 2026-03-16\n\nx\n"
    )
    write_cache_file(
        isolated_home, "httpbis", "meetings/ietf125/polls/2026.md", "adopt? 20-4\n"
    )
    assert _run(["list-sessions", "httpbis"], monkeypatch) == 0
    out = capsys.readouterr().out
    assert "ietf125" in out and "2026-03-16" in out and "poll" in out


def test_read_minutes_includes_polls(isolated_home, capsys, monkeypatch):
    write_cache_file(
        isolated_home,
        "httpbis",
        "meetings/ietf125/minutes.md",
        "Date: 2026-03-16\n\nwe discussed X\n",
    )
    write_cache_file(
        isolated_home, "httpbis", "meetings/ietf125/polls/2026.md", "adopt draft-foo? 20-4\n"
    )
    assert _run(["read-minutes", "httpbis", "ietf125"], monkeypatch) == 0
    out = capsys.readouterr().out
    assert "we discussed X" in out and "Polls" in out and "20-4" in out


def test_read_minutes_no_code_lists_sessions(isolated_home, capsys, monkeypatch):
    write_cache_file(
        isolated_home, "httpbis", "meetings/ietf125/minutes.md", "Date: 2026-03-16\n\nx\n"
    )
    assert _run(["read-minutes", "httpbis"], monkeypatch) == 0
    assert "Sessions available" in capsys.readouterr().out


def test_probe_embed_backend_none_when_no_index(isolated_home):
    from ietf_llm.embeddings import probe_embed_backend

    # No index for this corpus -> not a reachability question -> None, so the
    # CLI does not false-positive an exit-4 on a merely un-embedded corpus.
    assert probe_embed_backend("x-no-index-zzz") is None


def test_import_graph_stays_lean():
    # A separate read-only binary earns its keep by a lean import graph:
    # importing it must not drag in torch, the embedding stack, the gather
    # pipeline (dulwich), or the cloud backends (boto3 / google-auth). Run in
    # a fresh subprocess so an already-imported module in this process can't
    # mask a regression.
    probe = (
        "import sys, ietf_llm.query_cli\n"
        "heavy = ('torch', 'sentence_transformers', 'dulwich', 'boto3', 'google')\n"
        "leaked = sorted(m for m in heavy if m in sys.modules)\n"
        "assert not leaked, leaked\n"
        "print('OK')\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=False
    )
    assert proc.returncode == 0, proc.stderr
    assert "OK" in proc.stdout
