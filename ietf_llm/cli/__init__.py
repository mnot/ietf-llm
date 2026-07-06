"""Command-line surfaces — every `ietf-llm*` console script's implementation.

  main.py     — the `ietf-llm` gather/refresh command (its `main`); the
                top-level `ietf_llm.__main__` is a thin `python -m` shim over it
  export.py   — `ietf-llm-export`
  search.py   — `ietf-llm-search`
  list.py     — the `--list` / `--all` corpus-listing helpers `main` uses
  completion.py    — argcomplete wiring shared by all the ietf-llm CLIs
  skill_install.py — install the bundled Agent Skills (the `--install-skills` path)

(The MCP server's `ietf-llm-mcp` main lives in `ietf_llm.mcp`, with its code.)
"""
