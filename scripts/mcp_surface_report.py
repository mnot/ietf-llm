#!/usr/bin/env python
"""Report where the MCP surface's weight is, and what is duplicated in it.

The budget gate (`tests/test_mcp_surface_budget.py`) tells you the surface
grew. This tells you *where* — and, more usefully, which guidance is stated in
several places at once, which is how these things actually get fat: a rule
lands in `mcp-instructions.md`, then again in each of the five tool docstrings
it applies to.

    .venv/bin/python scripts/mcp_surface_report.py            # both shapes
    .venv/bin/python scripts/mcp_surface_report.py --shape stdio
    .venv/bin/python scripts/mcp_surface_report.py --extract /tmp/surface

`--extract` writes each description and the instructions out as markdown, which
is what `vale` lints (see `.vale.ini`); `scripts/lint-prose.sh` does both steps.

Token counts use `tiktoken` when it is installed (`pip install tiktoken`) and
fall back to chars/4 otherwise, saying which it used. tiktoken is deliberately
not a dev dependency: it fetches its BPE table over the network on first use,
which is fine for a human running a report and wrong for a gate — so the gate
counts characters (exact, hermetic) and only this script talks in tokens.
No tokenizer here is Claude's; they are all proxies, good to a few percent.
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Set, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# pylint: disable=wrong-import-position
from ietf_llm.mcp.surface import SHAPES, ToolSurface, build_surface, instructions_text

#: Width of the shared word-runs we hunt for. Eight is long enough that a hit is
#: a restated sentence rather than a coincidence of common words ("the working
#: group has"), and short enough to catch a rule that was reworded slightly on
#: its way into the second docstring.
NGRAM = 8

#: A phrase repeated across at least this many separate texts is guidance that
#: wants to live in one place -- the instructions field -- with the docstrings
#: pointing at it.
SPREAD = 3


def _tokenizer() -> Tuple[str, "object"]:
    """Return (label, encoder) where encoder has `.encode`, or a chars/4 stub."""
    try:
        import tiktoken  # pylint: disable=import-outside-toplevel,import-error

        return "tiktoken/o200k_base", tiktoken.get_encoding("o200k_base")
    except Exception:  # pylint: disable=broad-except

        class _Approx:
            @staticmethod
            def encode(text: str) -> Sequence[int]:
                return [0] * (len(text) // 4)

        return "approx chars/4", _Approx()


def _words(text: str) -> List[str]:
    """Normalise to comparable words: markdown, punctuation and case carry no
    meaning for "is this the same sentence twice"."""
    return re.findall(r"[a-z0-9_]+", text.lower())


def _ngrams(words: Sequence[str]) -> Set[Tuple[str, ...]]:
    return {tuple(words[i : i + NGRAM]) for i in range(len(words) - NGRAM + 1)}


def _texts(shape: str) -> Dict[str, str]:
    """Every distinct chunk of prose in the surface, keyed by where it lives."""
    out = {"(instructions)": instructions_text(shape)}
    for tool in build_surface(shape):
        out[tool.name] = tool.description
    return out


def _size_table(shape: str, tools: List[ToolSurface], instructions: str) -> None:
    label, enc = _tokenizer()
    total_chars = sum(t.chars for t in tools)
    instr_tok = len(enc.encode(instructions))  # type: ignore[attr-defined]
    tool_tok = [(t, len(enc.encode(t.serialized()))) for t in tools]  # type: ignore[attr-defined]
    total_tok = sum(n for _, n in tool_tok)

    print(f"\n=== {shape}: {len(tools)} tools ({label}) ===\n")
    print(f"  instructions   {len(instructions):7,} chars  {instr_tok:7,} tok")
    print(f"  tool list      {total_chars:7,} chars  {total_tok:7,} tok")
    print(
        f"  PREAMBLE       {len(instructions) + total_chars:7,} chars  "
        f"{instr_tok + total_tok:7,} tok   <- paid on every session\n"
    )
    print(f"  {'tool':34} {'tok':>7} {'desc':>7} {'schema':>7} {'params':>6} {'%':>5}")
    for tool, ntok in sorted(tool_tok, key=lambda p: -p[1]):
        share = 100.0 * tool.chars / total_chars
        schema_chars = tool.chars - len(tool.description)
        print(
            f"  {tool.name:34} {ntok:7,} {len(tool.description):7,} "
            f"{schema_chars:7,} {len(tool.params):6} {share:4.1f}%"
        )


def _duplication(texts: Dict[str, str], top: int) -> None:
    """Where the same words appear in more than one place.

    Two views, because they prompt different fixes: a phrase spread across many
    texts wants hoisting into the instructions, while a single heavily
    overlapping *pair* usually means one of the two tools should defer to the
    other.
    """
    grams: Dict[Tuple[str, ...], Set[str]] = defaultdict(set)
    per_text: Dict[str, Set[Tuple[str, ...]]] = {}
    for name, text in texts.items():
        per_text[name] = _ngrams(_words(text))
        for gram in per_text[name]:
            grams[gram].add(name)

    print(f"\n=== repeated phrasing ({NGRAM}-word runs in >= {SPREAD} places) ===\n")
    spread = {g: owners for g, owners in grams.items() if len(owners) >= SPREAD}
    if not spread:
        print("  none")
    for gram, owners in sorted(spread.items(), key=lambda kv: -len(kv[1]))[:top]:
        print(f"  {len(owners)}x  \"{' '.join(gram)}\"")
        print(f"        {', '.join(sorted(owners))}")

    print("\n=== most-overlapping pairs (shared runs / smaller text) ===\n")
    pairs: List[Tuple[float, int, str, str]] = []
    names = sorted(per_text)
    for i, left in enumerate(names):
        for right in names[i + 1 :]:
            shared = per_text[left] & per_text[right]
            if not shared:
                continue
            floor = min(len(per_text[left]), len(per_text[right])) or 1
            pairs.append((len(shared) / floor, len(shared), left, right))
    if not pairs:
        print("  none")
    for frac, shared, left, right in sorted(pairs, reverse=True)[:top]:
        print(f"  {frac:5.1%}  {shared:4} runs   {left}  <->  {right}")


def _extract(texts: Dict[str, str], where: Path) -> None:
    """Write each text out as markdown for a prose linter to read.

    Docstrings are indented inside a `def`, and every line after the first
    carries that indent; a linter would see a code block. Dedent on the way
    out so it lints as the prose it is.
    """
    where.mkdir(parents=True, exist_ok=True)
    for name, text in texts.items():
        lines = text.splitlines()
        body = "\n".join(line.strip() if line.strip() else "" for line in lines)
        stem = name.strip("()")
        (where / f"{stem}.md").write_text(body + "\n", encoding="utf-8")
    print(f"\nwrote {len(texts)} files to {where}")


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shape", choices=sorted(SHAPES), help="default: both")
    parser.add_argument("--top", type=int, default=12, help="rows per section")
    parser.add_argument("--extract", type=Path, help="write prose out for vale")
    args = parser.parse_args(argv)

    shapes = [args.shape] if args.shape else sorted(SHAPES)
    for shape in shapes:
        _size_table(shape, build_surface(shape), instructions_text(shape))

    # Duplication is a property of the widest surface, so report it once
    # against stdio: the http shape is a strict subset of these texts.
    texts = _texts("stdio")
    _duplication(texts, args.top)
    if args.extract:
        _extract(texts, args.extract)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
