"""`python -m ietf_llm` entry point.

The `ietf-llm` command's implementation lives in `ietf_llm.cli.main` alongside
the other console-script mains (`cli.export`, `cli.search`); this module is the
thin `__main__` shim that lets `python -m ietf_llm` run it, and re-exports
`main` so the `ietf_llm.__main__:main` console-script target keeps resolving.
"""

from __future__ import annotations

from .cli.main import main

if __name__ == "__main__":  # pragma: no cover
    main()
