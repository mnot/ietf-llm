"""Structure-aware snippet rendering for search hits.

The default "take the first N chars" snippet hides the most useful
content in many chunks — markdown tables (pro/con whiteboards,
comparison matrices, vote tallies) and lists (outlines, summaries
of options) carry far more information per byte than the surrounding
prose. A consuming LLM ranking hits wants to see *what kind* of
content matched, not just the first sentence of the wrapper text.

`make_snippet(text, max_chars)` returns a one-line snippet that
prefers, in order:

1. The first markdown table in the chunk (rendered compactly with
   header + up to two data rows, prefixed `[table: R rows × C cols]`).
2. A bulleted / numbered list of ≥3 items (prefixed `[list: N items]`,
   showing the first three).
3. The chunk's prose with newlines collapsed to spaces (original
   behaviour).

Output is single-line. Structured snippets (tables / lists) get a
bigger character budget than prose snippets — each row or bullet
carries more ranking signal per byte than the same number of bytes
of paragraph text, and the consumer of the snippet (a ranking LLM)
benefits from seeing the whole structure rather than a truncated
half.
"""

from __future__ import annotations

import re
from typing import List, Optional

#: A run of |…| rows must include a separator row matching this to
#: qualify as a markdown table. Allows en-dashes / em-dashes from
#: copy-pasted minutes too — anything in the `[-– —]` class.
_TABLE_SEP_RE = re.compile(r"^\|[\s\-–—:|]+\|$")

#: Bulleted (`- `, `* `, `+ `) or numbered (`1.`, `1)`) list item.
#: Anchored to start-of-line + optional leading whitespace, so quoted
#: blocks (`> - foo`) don't match.
_LIST_ITEM_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(.+)$")


#: Character budget for prose snippets. Tight, since paragraph text
#: has low information density per byte for ranking purposes.
PROSE_CHARS = 280

#: Character budget for structured snippets (tables, lists). Larger —
#: a whole pro/con table or option list is the actual ranking signal,
#: and truncating it mid-bullet (as consumer feedback noted) costs a
#: needless round-trip to get_chunk_text.
STRUCTURED_CHARS = 600


def make_snippet(
    text: str,
    max_chars: Optional[int] = None,
) -> str:
    """Compose a single-line snippet for a search hit.

    `max_chars` lets a caller override the budget — tests use it to
    pin truncation behaviour. The default lets prose and structured
    content pick their own appropriate budgets (see PROSE_CHARS,
    STRUCTURED_CHARS).
    """
    if not text.strip():
        return ""
    structured_budget = max_chars if max_chars is not None else STRUCTURED_CHARS
    prose_budget = max_chars if max_chars is not None else PROSE_CHARS
    table = _table_preview(text, structured_budget)
    if table:
        return table
    listing = _list_preview(text, structured_budget)
    if listing:
        return listing
    return _prose_preview(text, prose_budget)


def _prose_preview(text: str, max_chars: int) -> str:
    snippet = text.strip().replace("\n", " ")
    # Collapse runs of whitespace produced by joined paragraphs.
    snippet = re.sub(r"\s+", " ", snippet)
    if len(snippet) > max_chars:
        # Explicit truncation marker: the consumer's previous failure
        # mode was re-fetching a chunk because they couldn't tell if
        # the snippet was complete. "..." alone was ambiguous (could be
        # part of the prose). The bracketed `[truncated]` tag is
        # unmistakable.
        snippet = snippet[: max_chars - len(" [truncated]")] + " [truncated]"
    return snippet


def _looks_table_row(line: str) -> bool:
    """A row qualifies as part of a markdown table if it starts and
    ends with `|` and has ≥2 cells (i.e. ≥3 pipes counting the borders).
    """
    stripped = line.strip()
    return (
        stripped.startswith("|")
        and stripped.endswith("|")
        and stripped.count("|") >= 3
    )


def _find_table_runs(text: str) -> List[List[str]]:
    """All runs of ≥3 consecutive table-shaped lines."""
    runs: List[List[str]] = []
    current: List[str] = []
    for line in text.splitlines():
        if _looks_table_row(line):
            current.append(line.strip())
        else:
            if len(current) >= 3:
                runs.append(current)
            current = []
    if len(current) >= 3:
        runs.append(current)
    return runs


def _table_preview(text: str, max_chars: int) -> Optional[str]:
    """First markdown table in the chunk, rendered compactly.

    Returns None if no run of ≥3 table-shaped lines includes a
    separator row (i.e. the run is just stray `| … |` lines, not a
    proper table). This guards against false positives in pipe-heavy
    prose like quoted code.
    """
    for run in _find_table_runs(text):
        # Drop separator rows. A real markdown table has at least one;
        # if none, it's probably not a table — skip this run.
        non_sep = [ln for ln in run if not _TABLE_SEP_RE.match(ln)]
        if len(non_sep) == len(run):
            continue  # no separator row found → not a real table
        if len(non_sep) < 2:
            continue  # header only, no data rows worth previewing
        header = non_sep[0]
        n_data_total = len(non_sep) - 1
        # Column count: count cells in the header (pipes minus the two
        # borders). max(1, …) so we never report 0 cols on malformed input.
        n_cols = max(1, header.count("|") - 1)
        prefix = f"[table: {n_data_total} rows × {n_cols} cols] "
        # Pack rows greedily until they wouldn't fit in the budget — so
        # a bigger budget actually surfaces more rows, not just more
        # padding on the same 2 rows.
        parts: List[str] = [header]
        for row in non_sep[1:]:
            candidate = prefix + " · ".join(parts + [row])
            if len(candidate) > max_chars:
                break
            parts.append(row)
        preview = prefix + " · ".join(parts)
        # Collapse internal whitespace runs that come from padded cells.
        preview = re.sub(r" {2,}", " ", preview)
        # The `[table: R rows × C cols]` prefix already names the total
        # row count. If `parts` doesn't contain every row, append a
        # truncation marker so the consumer knows the chunk has more
        # rows than what's shown — saving a redundant get_chunk_text
        # call to verify completeness.
        if len(parts) - 1 < n_data_total:
            preview = preview + " [truncated]"
        return preview
    return None


def _list_preview(text: str, max_chars: int) -> Optional[str]:
    """A flat bulleted / numbered list of ≥3 items, rendered compactly.

    "Flat" means we only count top-level items — nested sub-bullets
    don't add to the count, since a single point with three sub-bullets
    isn't a list-of-three. Returns None if no qualifying list found.
    """
    items: List[str] = []
    for line in text.splitlines():
        # Only count top-level (no leading whitespace) list items.
        # Nested sub-bullets indent past column 0.
        if line and line[0] in "-*+" or re.match(r"^\d+[.)]\s", line):
            match = _LIST_ITEM_RE.match(line)
            if match:
                items.append(match.group(1).strip())
    if len(items) < 3:
        return None
    count = len(items)
    # Pack greedily into the budget, mirroring _table_preview. The old
    # hard-coded "first 3 + (N - 3) more" wasted budget on short items
    # and obscured content the consumer specifically needed — feedback
    # cited a 20-item list where they had to fetch the full chunk just
    # to see what was past item 3. Per-item truncation at 60 chars
    # keeps individual cells readable.
    prefix = f"[list: {count} items] "
    shown: List[str] = []
    for item in items:
        truncated_item = _truncate(item, 60)
        candidate = prefix + " · ".join(
            [*[f"• {x}" for x in shown], f"• {truncated_item}"]
        )
        # Reserve budget for a potential "+N more" tail so we don't
        # squeeze it out at the very end.
        reserve = len(f" + {count - len(shown) - 1} more") if shown else 0
        if len(candidate) + reserve > max_chars:
            break
        shown.append(truncated_item)
    if not shown:
        # Even one item didn't fit. Fall back to the first item plus
        # the count tag — better than no list preview at all.
        shown = [_truncate(items[0], max_chars - len(prefix) - 4)]
    remaining = count - len(shown)
    suffix = f" + {remaining} more" if remaining > 0 else ""
    preview = prefix + " · ".join(f"• {it}" for it in shown) + suffix
    if len(preview) > max_chars:
        preview = preview[: max_chars - len(" [truncated]")] + " [truncated]"
    return preview


def _truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"
