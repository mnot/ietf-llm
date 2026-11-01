"""Code mode: `select_chunks` filters, and `execute` end to end through the
Monty sandbox against a real (stub-embedded) index."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable, List

import pytest

from ietf_llm import embeddings
from ietf_llm.embeddings.search import build_index
from ietf_llm.log import Verbosity
from ietf_llm.mcp import codemode
from ietf_llm.paths import get_wg_file_cache_dir

from conftest import write_cache_file


class _StubModel:
    def embed(self, _text: str) -> Iterable[float]:
        return [1.0] + [0.0] * 7

    def embed_multi(self, texts: List[str]) -> Iterable[List[float]]:
        return [list(self.embed(t)) for t in texts]


def _seed(home: Path) -> None:
    write_cache_file(
        home, "wg", "threads/2026-04-10-mlkem.md",
        "# MLKEM\n\n## Messages\n\n"
        "### [1] 2026-04-10 09:00 — Alice (Chair)\n\n"
        "_Subject:_ MLKEM\n\nMLKEM opening message body.\n\n"
        "### [2] 2026-04-11 09:00 — Bob (reply to [1])\n\n"
        "_Subject:_ Re: MLKEM\n\nReply body from Bob, see PR #12.\n",
    )
    embeddings._MODEL_CACHE["stub"] = _StubModel()
    build_index("wg", get_wg_file_cache_dir("wg"), model_name="stub",
                verbose=Verbosity.QUIET)


def test_select_chunks_filters_in_sql(isolated_home: Path) -> None:
    _seed(isolated_home)
    msgs = codemode.chunks("wg", kind="threads", since="2026-04-10")
    assert [(m["idx"], m["sender"], m["reply_to"]) for m in msgs] == [
        (1, "Alice (Chair)", None), (2, "Bob", 1)]
    assert [m["idx"] for m in codemode.chunks("wg", author="bob")] == [2]
    assert [m["idx"] for m in codemode.chunks("wg", contains="pr #12")] == [2]
    assert codemode.chunks("wg", until="2026-04-10", kind="threads")[-1]["idx"] == 1
    assert {m["text"] for m in codemode.chunks("wg", kind="threads", chars=0)} == {""}
    assert codemode.chunks("wg", kind="drafts") == []


def test_execute_runs_sandboxed_code(isolated_home: Path) -> None:
    _seed(isolated_home)
    out = codemode.tool_execute(
        "from ietf import corpora, chunks\n"
        "print('corpora', corpora())\n"
        "{m['sender']: m['idx'] for m in chunks('wg', kind='threads', chars=0)"
        " if m['sender']}"
    )
    printed, _, value = out.partition("\n---")[0].partition("\n")
    assert printed == "corpora ['wg']"
    assert json.loads(value) == {"Alice (Chair)": 1, "Bob": 2}


def test_execute_reports_errors_and_stays_sandboxed(isolated_home: Path) -> None:
    _seed(isolated_home)
    assert "unknown corpus" in codemode.tool_execute(
        "from ietf import chunks\nchunks('nope')").lower()
    assert codemode.tool_execute("open('/etc/passwd').read()").startswith("Error:")


def test_chunks_refuses_names_outside_the_cache(isolated_home: Path) -> None:
    _seed(isolated_home)
    for name in ("../wg", "wg/../../x", "/", ""):
        with pytest.raises(ValueError, match="(?i)unknown corpus|not a valid corpus"):
            codemode.chunks(name)


def test_replaced_tools_exist() -> None:
    # remove_tool raises on an unknown name: catches a rename breaking the swap.
    from mcp.server.mcpserver import MCPServer
    from ietf_llm.mcp import chunks, search, topic
    server = MCPServer("t")
    for mod in (search, topic, chunks, codemode): mod.register(server)
    for name in codemode.REPLACES: server.remove_tool(name)


def test_execute_keeps_the_read_tool_guards(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Same guards as the read tools it replaces: grounding frame on top for a
    # big thread, participation nudge as a footer for thread material, and the
    # first-gather refusal.
    _seed(isolated_home)
    write_cache_file(
        isolated_home, "wg", "digests/threads.md",
        "# Threads\n\n| Subject | Msgs | Participants | First | Last | File |\n"
        "|---|---|---|---|---|---|\n"
        "| MLKEM | 325 | 62 | 2026-04-10 | 2026-05-01 | `threads/2026-04-10-mlkem.md` |\n")
    out = codemode.tool_execute("from ietf import chunks\nlen(chunks('wg', chars=0))")
    assert out.startswith("> **Before characterising any decision")
    assert out.rstrip().endswith(")_") and "About to draft a contribution" in out
    assert "About to draft" not in codemode.tool_execute("1 + 1")

    from ietf_llm.gather import runner as gather_runner
    monkeypatch.setattr(
        gather_runner, "local_inflight",
        lambda wg: {"corpus": wg, "state": "running", "stage_index": 5,
                    "stage_total": 19, "stage": "drafts"})
    assert "first gather" in codemode.tool_execute(
        "from ietf import chunks\nchunks('wg')").lower()


def test_code_mode_loads_only_when_flagged(monkeypatch: pytest.MonkeyPatch) -> None:
    from ietf_llm.mcp import server
    monkeypatch.delenv("IETF_LLM_CODE_MODE", raising=False)
    assert server._load_codemode() is None
    monkeypatch.setenv("IETF_LLM_CODE_MODE", "1")
    assert server._load_codemode() is codemode


def test_count_and_capped_lists_are_flagged(isolated_home: Path) -> None:
    # len(chunks()) stops at `limit`: count() is the total, and a full list
    # gets a warning so a capped length is not read as a total.
    _seed(isolated_home)
    assert codemode.count("wg", kind="threads") == len(codemode.chunks("wg", kind="threads"))
    assert codemode.count("wg", author="bob") == 1
    out = codemode.tool_execute("from ietf import chunks\nlen(chunks('wg', limit=1))")
    assert "may be cut off" in out
    assert "may be cut off" not in codemode.tool_execute(
        "from ietf import count\ncount('wg')")


def test_missing_index_is_an_error_not_an_absence(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_cache_file(isolated_home, "wg", "threads/2026-04-10-x.md", "# X\n")
    out = codemode.tool_execute("from ietf import count\ncount('wg', author='bob')")
    assert out.startswith("Error:") and "not an absence" in out
    # With an index, a filter that matches nothing is a real empty result...
    _seed(isolated_home)
    assert codemode.chunks("wg", author="nobody") == []
    # ...but an unfiltered search with no hits means the query was not embedded.
    monkeypatch.setattr(codemode, "_search", lambda *a, **k: [])
    with pytest.raises(RuntimeError, match="could not embed"):
        codemode.search("wg", "anything")
