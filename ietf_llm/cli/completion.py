"""Shell tab-completion for the ietf-llm command family.

The argcomplete wiring shared by the CLI entry points (`ietf-llm`,
`ietf-llm-export`, `ietf-llm-search`): the `wg`-positional completer, the
per-parser hookup, and the registration-snippet printer. argcomplete is
optional — each function degrades to a no-op / clear message when it is not
installed, so a minimal install still runs the CLI.
"""

from __future__ import annotations

import sys
from typing import Any, List

from ..paths import cached_wg_names


def wg_completer(prefix: str, **_kwargs: Any) -> List[str]:
    """argcomplete completer for a `wg` positional: cached shortnames
    matching `prefix`. Keep it fast — argcomplete spins a fresh
    interpreter per <TAB>, so this is just a directory listing.
    """
    return [w for w in cached_wg_names() if w.startswith(prefix)]


def maybe_autocomplete(parser: Any) -> None:
    """Wire argcomplete into `parser` if the package is installed.

    Called right before `parse_args()`. A no-op (not an error) when
    argcomplete isn't present, so a minimal / editable install
    without the dependency still runs the CLI normally.
    """
    try:
        import argcomplete  # pylint: disable=import-outside-toplevel
    except ImportError:
        return
    argcomplete.autocomplete(parser)


def print_completion_snippet(shell: str) -> int:
    """Print the argcomplete registration snippet for every ietf-llm
    command, for the given shell. Returns an exit code.

    Routed through `ietf-llm` itself (not argcomplete's own
    `register-python-argcomplete` script) because under `pipx` only
    this package's declared entry points are on PATH — a dependency's
    scripts aren't exposed. `eval "$(ietf-llm --completion zsh)"`
    works regardless of how the package was installed.
    """
    try:
        import argcomplete  # pylint: disable=import-outside-toplevel
    except ImportError:
        print(
            "argcomplete is not installed (it ships with ietf-llm; "
            "try reinstalling).",
            file=sys.stderr,
        )
        return 1
    commands = ["ietf-llm", "ietf-llm-export", "ietf-llm-search"]
    # `shellcode` is public API but isn't re-exported in a way mypy's strict
    # `no_implicit_reexport` accepts, so reaching it directly needs a
    # `type: ignore` — and *which* codes it needs varies with the argcomplete
    # version pip resolves for the running interpreter: 3.13 wants
    # `attr-defined,no-untyped-call`, while 3.11 resolves one where the call is
    # typed and `no-untyped-call` is then flagged as an unused ignore. A fixed
    # code list is wrong on some version of the matrix either way, so go through
    # `getattr`: it yields `Any` on every version and needs no ignore at all.
    shellcode = getattr(argcomplete, "shellcode")
    snippet = shellcode(commands, shell=shell)
    print(snippet)
    return 0
