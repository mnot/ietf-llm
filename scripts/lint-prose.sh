#!/usr/bin/env bash
# Lint the prose a client actually receives: the MCP `instructions` field and
# every tool description.
#
#     scripts/lint-prose.sh
#
# The descriptions are docstrings — indented inside a `def`, so vale would read
# them as code blocks. `mcp_surface_report.py --extract` dedents them into
# markdown first; this script wires the two together.
#
# Needs `vale` (brew install vale) and a built venv. Not part of `make lint`:
# the rules in styles/ietf-llm/ are advisory, and the hard ceiling on this
# surface is tests/test_mcp_surface_budget.py.
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
python="${PYTHON:-$root/.venv/bin/python}"

if ! command -v vale >/dev/null 2>&1; then
    echo "vale not found on PATH — brew install vale" >&2
    exit 127
fi
if [ ! -x "$python" ]; then
    echo "no interpreter at $python — run 'make venv', or set PYTHON=" >&2
    exit 127
fi

out="$(mktemp -d)"
trap 'rm -rf "$out"' EXIT

"$python" "$root/scripts/mcp_surface_report.py" --extract "$out" >/dev/null
exec vale --config "$root/.vale.ini" "$out"
