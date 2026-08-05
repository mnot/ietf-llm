"""Citation tools: find_citations, find_message_citations."""

from __future__ import annotations

import os
import re
from typing import TYPE_CHECKING, Annotated, Any, Dict, List, Optional, Tuple

from pydantic import Field

from ..freshness import gather_suggestion
from ..gather.sources.citations import normalize_draft_name
from ..paths import digest_path
from .common import _files_dir, _offload, _requires_corpus, _with_freshness
from .params import Corpus, DraftName, ThreadFile

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP  # pragma: no cover


@_requires_corpus
def tool_find_citations(wg: str, draft_name: str) -> str:
    """Return every thread / issue file that cites the given draft.

    A "citation" is one distinct source (thread or issue) that references
    the draft, de-duplicated per source — a thread mentioning it three
    times counts once. So the `cited in N` figure in `overview` is the
    cumulative count of such sources across the gathered corpus; it is not
    weighted by recency, so read it as accumulated attention, not
    necessarily current activity.

    Reads `digests/citations.md` (built at gather time by
    `gather.sources.citations.scan_citations`). Draft name is normalised the
    same way the scanner normalises matches (lowercase, version
    suffix stripped), so `draft-Foo-Bar-07` and `draft-foo-bar` both
    yield the same result.
    """
    cache = _files_dir(wg)
    citations_md = digest_path(cache, "citations")
    if not os.path.isfile(citations_md):
        return _with_freshness(
            wg,
            f"No citations digest for {wg}. Either no thread / issue "
            "files reference any drafts, or this corpus was gathered with "
            f"an older version — {gather_suggestion(wg, purpose='to rebuild', force=True)}.",
        )
    normalised = normalize_draft_name(draft_name)
    try:
        with open(citations_md, "r", encoding="utf-8") as fh:
            text = fh.read()
    except OSError:
        return f"Couldn't read citations digest for {wg}."
    # Find the section for this draft. Sections are
    # `## `<draft>` (N citation(s))` followed by bullet lines.
    section_re = re.compile(
        rf"^## `{re.escape(normalised)}` \([^)]+\)\s*\n+" r"(?P<body>(?:^- .*\n?)*)",
        re.MULTILINE,
    )
    match = section_re.search(text)
    if match is None:
        return _with_freshness(
            wg,
            f"No citations recorded for `{normalised}` in {wg}. "
            "(The scanner only sees draft references in cached thread "
            'and issue files; check `list_files("'
            f'{wg}", pattern="drafts/{normalised}*")` to confirm '
            "the draft itself is in the corpus.)",
        )
    body = match.group("body").strip()
    out = [f"# Citations for `{normalised}` in {wg}\n", body]
    return _with_freshness(wg, "\n".join(out))


# digests/message_citations.md structure (gather.sources.message_citations):
#   ## Resolved (target gathered here)
#   ### `threads/<file>.md` [chunk N] — Sender, DATE — "Subject"
#   cited by:
#   - `threads/<src>.md` [chunk M] — _context_
#   ## External (not gathered in this corpus)
#   ### https://... (gather `list`?)
#   cited by:
#   - `threads/<src>.md` [chunk M] — _context_
_MC_RESOLVED_HDR_RE = re.compile(
    r"^### `(?P<f>[^`]+)` \[chunk (?P<n>\d+)\] — (?P<rest>.*)$"
)


_MC_EXTERNAL_HDR_RE = re.compile(r"^### (?P<url>https?://\S+)(?P<hint>.*)$")


_MC_BULLET_RE = re.compile(
    r"^- `(?P<sf>[^`]+)` \[chunk (?P<m>\d+)\] — _(?P<ctx>.*)_\s*$"
)


def _parse_message_citations(
    text: str,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Parse message_citations.md into (resolved_edges, external_edges).

    resolved edge: {tgt_file, tgt_chunk, tgt_rest, src_file, src_chunk, ctx}
    external edge: {url, hint, src_file, src_chunk, ctx}
    """
    resolved: List[Dict[str, Any]] = []
    external: List[Dict[str, Any]] = []
    section = ""  # "resolved" | "external"
    cur: Optional[Dict[str, Any]] = None
    for line in text.splitlines():
        if line.startswith("## Resolved"):
            section, cur = "resolved", None
            continue
        if line.startswith("## External"):
            section, cur = "external", None
            continue
        res_hdr = _MC_RESOLVED_HDR_RE.match(line)
        if res_hdr and section == "resolved":
            cur = {
                "tgt_file": res_hdr.group("f"),
                "tgt_chunk": int(res_hdr.group("n")),
                "tgt_rest": res_hdr.group("rest").strip(),
            }
            continue
        ext_hdr = _MC_EXTERNAL_HDR_RE.match(line)
        if ext_hdr and section == "external":
            cur = {
                "url": ext_hdr.group("url"),
                "hint": ext_hdr.group("hint").strip(),
            }
            continue
        bullet = _MC_BULLET_RE.match(line)
        if bullet and cur is not None:
            edge = {
                "src_file": bullet.group("sf"),
                "src_chunk": int(bullet.group("m")),
                "ctx": bullet.group("ctx").strip(),
            }
            if section == "resolved":
                resolved.append({**cur, **edge})
            else:
                external.append({**cur, **edge})
    return resolved, external


@_requires_corpus
def tool_find_message_citations(
    wg: str, file: str, chunk_idx: Optional[int] = None
) -> str:
    """Walk the message reference graph for a thread / issue file.

    Mailing-list messages routinely cite *other messages* by archive
    permalink — an appeal links the message being appealed, a split thread
    links the message it forked from, a reply footnotes the one it
    answers. The gather step resolves those links (against the
    `Archived-At:` permalinks on the gathered messages) into
    `digests/message_citations.md`; this tool reads the graph for one
    file and returns:

      - **Inbound** — other messages that cite this file (optionally the
        specific message `chunk_idx`). The reverse index `get_by_url`
        can't give you: "who else references this message / decision?".
      - **Outbound** — the archive links this file cites, each resolved
        to its local `file` / `chunk_idx` (pivot with `read_file_section`
        / `get_chunk_text`) or flagged external (a message not gathered
        here — often another list; gather it and retry).

    `file` is a corpus-relative path like `threads/<file>.md` or
    `issues/<org-repo>/<n>.md`. Pass `chunk_idx` to scope to one message.

    Within-scheme resolution only: a `mailarchive.ietf.org` token is not
    bridged to a stored `www.w3.org/mid` Message-ID, so on a list that
    stamps one scheme while bodies cite the other, real targets can show
    as external. To fetch a single URL directly, use `get_by_url`.
    """
    cache = _files_dir(wg)
    md = digest_path(cache, "message_citations")
    if not os.path.isfile(md):
        return _with_freshness(
            wg,
            f"No message-citations digest for {wg}. Either no thread / "
            "issue files cite an archive permalink, or this corpus was "
            "gathered with an older version — "
            f"{gather_suggestion(wg, purpose='to rebuild', force=True)}.",
        )
    try:
        with open(md, "r", encoding="utf-8") as fh:
            text = fh.read()
    except OSError:
        return f"Couldn't read message-citations digest for {wg}."
    target = file.strip().strip("`").lstrip("./")
    resolved, external = _parse_message_citations(text)

    def _match(path: str, chunk: int) -> bool:
        return path == target and (chunk_idx is None or chunk == chunk_idx)

    inbound = [e for e in resolved if _match(e["tgt_file"], e["tgt_chunk"])]
    out_resolved = [e for e in resolved if _match(e["src_file"], e["src_chunk"])]
    out_external = [e for e in external if _match(e["src_file"], e["src_chunk"])]

    if not inbound and not out_resolved and not out_external:
        scope = f" [chunk {chunk_idx}]" if chunk_idx is not None else ""
        return _with_freshness(
            wg,
            f"No message citations recorded for `{target}`{scope} in {wg} "
            "— it neither cites an archive permalink nor is cited by one "
            "that resolves here. (The graph only covers archive-URL links "
            "between cached messages.)",
        )

    scope = f" [chunk {chunk_idx}]" if chunk_idx is not None else ""
    lines = [f"# Message citations for `{target}`{scope} in {wg}\n"]
    if inbound:
        lines.append("## Inbound — messages that cite this\n")
        for edge in sorted(inbound, key=lambda x: (x["src_file"], x["src_chunk"])):
            tgt = (
                "" if chunk_idx is not None else f" → cites [chunk {edge['tgt_chunk']}]"
            )
            lines.append(
                f"- `{edge['src_file']}` [chunk {edge['src_chunk']}]{tgt} "
                f"— _{edge['ctx']}_"
            )
        lines.append("")
    if out_resolved or out_external:
        lines.append("## Outbound — archive links this cites\n")
        for edge in sorted(out_resolved, key=lambda x: (x["tgt_file"], x["tgt_chunk"])):
            lines.append(
                f"- → `{edge['tgt_file']}` [chunk {edge['tgt_chunk']}] "
                f"({edge['tgt_rest']}) — _{edge['ctx']}_"
            )
        for edge in sorted(out_external, key=lambda x: x["url"]):
            hint = f" {edge['hint']}" if edge["hint"] else ""
            lines.append(f"- → external {edge['url']}{hint} — _{edge['ctx']}_")
        lines.append("")
    return _with_freshness(wg, "\n".join(lines))


def register(server: "FastMCP") -> None:
    @server.tool()
    async def find_citations(corpus: Corpus, draft_name: DraftName) -> str:
        """Find every mailing-list thread or GitHub issue in an IETF/IRTF
        effort that cites a given Internet-Draft.

        The gather step scans per-thread and per-issue markdown files
        for `draft-...` references and records them in
        `digests/citations.md`. This tool reads that digest for the
        given draft name and returns each citing file plus the
        chunk_idx and a short context excerpt.

        Use when:
          - Reading a draft and wanting the surrounding list discussion
            ("what threads engage with this draft?").
          - Reading a thread that mentions a draft and wanting to find
            the *other* threads that engage the same draft.
          - Triaging "is this draft actually being discussed in the WG"
            from a count alone (overview's Documents section shows the
            count inline; this tool drills into the locations).

        """
        return await _offload(tool_find_citations, corpus, draft_name)

    @server.tool()
    async def find_message_citations(
        corpus: Corpus,
        file: ThreadFile,
        chunk_idx: Annotated[
            Optional[int],
            Field(description="Scope to a single message.", ge=0),
        ] = None,
    ) -> str:
        """Walk the message reference graph for a thread / issue file —
        which messages cite it, and which archive links it cites.

        Messages cite *other messages* by archive permalink constantly:
        an appeal links the message being appealed, a split thread links the
        message it forked from, a reply footnotes the one it answers. The
        gather step resolves those links into `digests/message_citations.md`;
        this reads the graph for one file.

        Returns **Inbound** (other messages that cite this file — the
        reverse index `get_by_url` can't give you) and **Outbound** (the
        archive links this file cites, each resolved to a local
        `file` / `chunk_idx` to pivot on, or flagged external — a message
        not gathered here, often another list to gather and retry).

        Use when:
          - Reading a message whose body footnotes an archive URL and you
            want the message behind it (or `get_by_url` for one URL).
          - Tracing a dispute / appeal / split thread back to its origin.
          - Asking "who else referenced this message or decision?".

        Within-scheme resolution only (a `mailarchive` token is not bridged to a
        `w3.org/mid` Message-ID), so real targets can show as external on
        a list that stamps the opposite scheme from what bodies cite.
        """
        return await _offload(tool_find_message_citations, corpus, file, chunk_idx)
