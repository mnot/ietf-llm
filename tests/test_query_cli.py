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
