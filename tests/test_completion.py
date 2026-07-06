"""Tests for shell tab-completion plumbing.

The completer for the `wg` positional offers already-gathered
shortnames from the cache. We test the completer + the shared
`cached_wg_names()` helper directly; the argcomplete wiring itself
(`maybe_autocomplete`) is exercised by the end-to-end completion
run in CI rather than unit-tested here.
"""

from __future__ import annotations

from pathlib import Path

from ietf_llm.paths import cached_wg_names
from ietf_llm.cli.completion import maybe_autocomplete, wg_completer

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
