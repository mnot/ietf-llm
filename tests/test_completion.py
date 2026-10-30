"""Tests for shell tab-completion plumbing.

The completer for the `wg` positional offers already-gathered
shortnames from the cache. We test the completer + the shared
`cached_wg_names()` helper directly; the argcomplete wiring itself
(`maybe_autocomplete`) is exercised by the end-to-end completion
run in CI rather than unit-tested here.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

import pytest

from ietf_llm.paths import cached_wg_names
from ietf_llm.cli import export as export_cli
from ietf_llm.cli import search as search_cli
from ietf_llm.cli.completion import maybe_autocomplete, wg_completer
from ietf_llm.gather import cli as gather_cli

from conftest import write_cache_file


def test_cached_wg_names_lists_gathered(isolated_home: Path) -> None:
    write_cache_file(isolated_home, "httpbis", "digests/index.md", "# x\n")
    write_cache_file(isolated_home, "x-foo", "digests/index.md", "# x\n")
    assert cached_wg_names() == ["httpbis", "x-foo"]


def test_cached_wg_names_skips_machinery(isolated_home: Path) -> None:
    write_cache_file(isolated_home, "tls", "digests/index.md", "# x\n")
    write_cache_file(isolated_home, "_scratch", "digests/index.md", "# x\n")
    assert cached_wg_names() == ["tls"]


def test_cached_wg_names_empty_when_no_cache(isolated_home: Path) -> None:
    assert cached_wg_names() == []


def test_wg_completer_returns_all_for_empty_prefix(isolated_home: Path) -> None:
    write_cache_file(isolated_home, "httpbis", "digests/index.md", "# x\n")
    write_cache_file(isolated_home, "tls", "digests/index.md", "# x\n")
    assert wg_completer("") == ["httpbis", "tls"]


def test_wg_completer_filters_by_prefix(isolated_home: Path) -> None:
    write_cache_file(isolated_home, "httpbis", "digests/index.md", "# x\n")
    write_cache_file(isolated_home, "tls", "digests/index.md", "# x\n")
    write_cache_file(isolated_home, "x-foo", "digests/index.md", "# x\n")
    assert wg_completer("x-") == ["x-foo"]
    assert wg_completer("htt") == ["httpbis"]
    assert wg_completer("zzz") == []


def test_wg_completer_accepts_argcomplete_kwargs(isolated_home: Path) -> None:
    # argcomplete calls completers with extra kwargs (parsed_args,
    # action, parser). The completer must tolerate them.
    write_cache_file(isolated_home, "tls", "digests/index.md", "# x\n")
    assert wg_completer("", parsed_args=None, action=None, parser=None) == ["tls"]


def test_print_completion_emits_registration_for_all_commands(
    capsys: object,
) -> None:
    # `ietf-llm --completion zsh` must print a snippet that registers
    # all three commands — driven through ietf-llm so it works under
    # pipx (which doesn't expose argcomplete's own scripts).
    from ietf_llm.cli.completion import print_completion_snippet
    rc = print_completion_snippet("zsh")
    assert rc == 0
    out = capsys.readouterr().out  # type: ignore[attr-defined]
    assert "ietf-llm" in out
    assert "ietf-llm-export" in out
    assert "ietf-llm-search" in out


def test_print_completion_fish_format(capsys: object) -> None:
    from ietf_llm.cli.completion import print_completion_snippet
    rc = print_completion_snippet("fish")
    assert rc == 0
    out = capsys.readouterr().out  # type: ignore[attr-defined]
    # fish output uses a different shape than bash/zsh; just confirm
    # it produced something command-specific.
    assert "ietf-llm" in out


def test_maybe_autocomplete_is_noop_without_env(isolated_home: Path) -> None:
    # Outside a completion context (no _ARGCOMPLETE env), calling
    # maybe_autocomplete on a parser must return cleanly without
    # exiting or printing — it just registers and returns.
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("wg")
    # Should not raise or sys.exit.
    maybe_autocomplete(parser)


class _Captured(Exception):
    def __init__(self, parser: argparse.ArgumentParser) -> None:
        super().__init__()
        self.parser = parser


def _wg_completer_of(parser: argparse.ArgumentParser) -> object:
    (wg_action,) = [a for a in parser._actions if a.dest == "wg"]
    return getattr(wg_action, "completer", None)


def _parser_from_main(module: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    # `main()` builds its parser inline; stop it at the autocomplete hook, which
    # runs right after the arguments are declared and before parsing.
    def _capture(parser: argparse.ArgumentParser) -> None:
        raise _Captured(parser)

    monkeypatch.setattr(module, "maybe_autocomplete", _capture)
    with pytest.raises(_Captured) as caught:
        module.main()
    return caught.value.parser


def test_every_cli_attaches_the_wg_completer(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # argcomplete reads `.completer` off the action; if one command drops the
    # hookup, nothing else fails and its tab completion silently stops.
    assert _wg_completer_of(gather_cli.build_parser()) is wg_completer
    assert _wg_completer_of(_parser_from_main(export_cli, monkeypatch)) is wg_completer
    assert _wg_completer_of(_parser_from_main(search_cli, monkeypatch)) is wg_completer
