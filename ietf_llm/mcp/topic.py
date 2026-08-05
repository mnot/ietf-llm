"""Thread / topic tools: read_topic, find_replies, tally_positions."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Annotated, Any, Dict, List, Optional, Tuple

from pydantic import Field

from ..embeddings import get_messages, search
from ..freshness import gather_enabled
from ..log import Verbosity
from ..people.positions import (
    extract_chair_statements,
    file_supports_tally,
    load_people_context,
    read_file_text,
    render_tally,
    tally_thread,
)
from .common import (
    _append_participation_nudge,
    _files_dir,
    _grounding_frame,
    _index_rebuilding_note,
    _offload,
    _participation_nudge,
    _regather_call,
    _requires_corpus,
    _safe_path,
    _thread_sizes,
    _with_freshness,
)
from .params import (
    ChunkIdx,
    Corpus,
    FilePattern,
    Query,
    Since,
    ThreadFile,
    Until,
)

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP  # pragma: no cover


# Matches the thread message section header so we can build the reply
# graph for include_replies. Mirrors `_THREAD_MSG_RE` in chunking.py but
# captures the parent index instead of stripping it.
_THREAD_REPLY_RE = re.compile(
    r"^### \[(\d+)\] \S+(?:\s+\S+)? — .+? \(reply to \[(\d+)\]\)$",
    re.MULTILINE,
)


# Cap on rendered messages so a runaway include_replies expansion can't
# blow the context window. Sized at 3× the default k=20, so a typical
# call stays well under and an unusually deep arc still fits.
_READ_TOPIC_MAX_MESSAGES = 60


# Upper bound on caller-provided k. Without this, k=200 widens fetch_k
# to 600 (an unbounded SQL OR-chain into get_messages), only for the
# render-cap below to discard most of it. Clamping keeps the cost
# bounded; the cap is just over `_READ_TOPIC_MAX_MESSAGES` so the
# matched-vs-reply ratio stays sensible after replies are added.
_READ_TOPIC_MAX_K = 50


# Per-message body cap. Chunks are themselves capped at MAX_CHUNK_CHARS
# (8 KB) but stitching 60 of those is ~480 KB; truncate long messages
# so total output stays bounded. 4 KB is plenty for a typical post.
_READ_TOPIC_MAX_BODY_CHARS = 4000


#: `(reply to [P])` marker inside a message header — captures the
#: per-file parent index P.
_REPLY_TO_RE = re.compile(r"\(reply to \[(\d+)\]\)")


#: A leading `[N] ` per-file index on a stored message title, stripped
#: when read_topic re-numbers globally.
_LEADING_BRACKET_RE = re.compile(r"^\s*\[\d+\]\s*")


def _strip_message_header(text: str) -> str:
    """Drop a stored message chunk's leading `### [N] …` section-header
    line, keeping the `_Subject:_` / `_Archived-At:_` lines and body — so
    a caller that renders its own header does not show two."""
    first, _, rest = text.partition("\n")
    if first.lstrip().startswith("### ["):
        return rest.lstrip("\n")
    return text


def _parse_reply_graph(text: str) -> Dict[int, List[int]]:
    """Walk a thread file's text once and return {parent_idx: [child_idx, ...]}.

    Message section headers are `### [N] DATE — Sender (reply to [P])`.
    Message number N corresponds to chunk_idx N in the indexed file
    (chunk 0 is the thread header). Children are listed in document
    order (= chronological order, since the file is written that way).
    """
    graph: Dict[int, List[int]] = {}
    for match in _THREAD_REPLY_RE.finditer(text):
        child = int(match.group(1))
        parent = int(match.group(2))
        graph.setdefault(parent, []).append(child)
    return graph


def _descendants(graph: Dict[int, List[int]], root: int) -> List[int]:
    """All transitive children of `root` in the reply graph, BFS order."""
    out: List[int] = []
    # Seed `seen` with the root so a malformed self-reply marker
    # (`[5] … (reply to [5])` → graph[5] = [5]) can't list the root as its own
    # descendant, and the walk stays cycle-safe regardless of marker damage.
    seen = {root}
    queue = [child for child in graph.get(root, []) if child not in seen]
    seen.update(queue)
    while queue:
        node = queue.pop(0)
        out.append(node)
        for child in graph.get(node, []):
            if child not in seen:
                seen.add(child)
                queue.append(child)
    return out


def _read_thread_file_text(wg: str, file: str) -> Optional[str]:
    """Read a thread file's raw text. Returns None if the file isn't in
    the corpus cache (so include_replies degrades gracefully — we just skip
    the expansion rather than erroring the whole call)."""
    path = _safe_path(wg, file)
    if path is None:
        return None
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return None


def _topic_thread_map(
    wg: str, matched_hits: List[Any], rows: List[Any], limit: int = 8
) -> List[str]:
    """A saturation signal for read_topic: how the matches spread across
    threads, with each thread's match count, how many were shown, and its
    real size. Lets a caller see which cluster is major and which thread
    it has only partly seen — so it knows when to read one in full rather
    than keep slicing. Empty for a single-thread topic (no map needed)."""
    matched_per_file: Dict[str, int] = {}
    for hit in matched_hits:
        matched_per_file[hit.file] = matched_per_file.get(hit.file, 0) + 1
    if len(matched_per_file) <= 1:
        return []
    shown_per_file: Dict[str, int] = {}
    for row in rows:
        if row[6]:  # is_matched
            shown_per_file[row[1]] = shown_per_file.get(row[1], 0) + 1
    sizes = _thread_sizes(wg)
    ranked = sorted(matched_per_file.items(), key=lambda kv: kv[1], reverse=True)
    out = [
        f"## Threads in this topic ({len(matched_per_file)})",
        "_How the matches spread across threads — a thread with many matches "
        "you have only partly seen is the one to read in full "
        "(`read_file_section`)._",
    ]
    for file, n_matched in ranked[:limit]:
        shown = shown_per_file.get(file, 0)
        size = sizes.get(file)
        size_tag = (
            f" · thread has {size[0]} msgs, {size[1]} participants" if size else ""
        )
        out.append(f"- `{file}` — {n_matched} matched, {shown} shown{size_tag}")
    if len(ranked) > limit:
        out.append(f"_… and {len(ranked) - limit} more thread(s)._")
    out.append("")
    return out


@_requires_corpus
def tool_read_topic(  # pylint: disable=too-many-arguments,too-many-positional-arguments,too-many-locals,too-many-branches,too-many-statements
    wg: str,
    query: str,
    since: Optional[str] = None,
    until: Optional[str] = None,
    file_pattern: Optional[str] = None,
    k: int = 20,
    include_replies: bool = False,
    body_chars: Optional[int] = None,
) -> str:
    # Per-message body cap: default 4000, but a synthesis task can dial it
    # down to spend less context. Clamp to [200, default] — lowering only.
    body_cap = _READ_TOPIC_MAX_BODY_CHARS
    if body_chars is not None and body_chars > 0:
        body_cap = max(100, min(body_chars, _READ_TOPIC_MAX_BODY_CHARS))
    # Clamp k before widening, so a misuse (k=500) doesn't generate a
    # 1500-row SQL OR-chain that gets thrown away by the render cap.
    if k > _READ_TOPIC_MAX_K:
        k = _READ_TOPIC_MAX_K
    elif k < 1:
        k = 1
    # Widen the fetch so we have enough material to filter to dated
    # thread/issue chunks and still hit k. 3× covers most reasonable
    # WGs; the floor of 60 stops k=5 calls from over-narrowing.
    fetch_k = max(k * 3, 60)
    hits = search(
        wg,
        query,
        k=fetch_k,
        file_pattern=file_pattern,
        since=since,
        until=until,
        # sort="date" both excludes undated chunks (drafts, transcripts,
        # thread header chunks) and orders the survivors chronologically.
        # We re-sort after merging replies anyway, but the undated-filter
        # is load-bearing — it's the only way to drop the thread header
        # chunk (chunk_idx=0) from the relevance shortlist.
        sort="date",
        verbose=Verbosity.QUIET,
    )
    if not hits:
        rebuilding = _index_rebuilding_note(wg)
        if rebuilding is not None:
            return _with_freshness(wg, f"(no results yet — {rebuilding})")
        no_index = (
            f"the corpus may have no search index — re-gather it with {_regather_call(wg)}"
            if gather_enabled()
            else f"has `ietf-llm {wg} --embed` been run?"
        )
        return _with_freshness(wg, f"(no results for {query!r} — {no_index})")

    # Keep only chunks from thread/issue files that have a date — those
    # are the only chunks that represent a "message" in a debate.
    # Windowed draft / transcript chunks may match a query but they
    # aren't messages, so the chronological view skips them.
    thread_issue_hits = [
        h for h in hits if h.file.lower().startswith(("threads/", "issues/"))
    ]
    matched = thread_issue_hits[:k]
    # For the completeness signal: how many more matched than we show, and
    # whether the relevance shortlist itself was capped (so the true total
    # may be higher still). Per-match relevance scores let the caller spot
    # an off-topic match instead of silently discarding it.
    extra_matches = len(thread_issue_hits) - len(matched)
    fetch_capped = len(hits) >= fetch_k
    score_by_key = {(h.file, h.chunk_idx): float(h.score) for h in matched}
    if not matched:
        return _with_freshness(
            wg,
            f"(no thread / issue messages match {query!r}. "
            "Try `search_corpus` to see whether the topic lives in "
            "drafts or transcripts instead.)",
        )

    # Build the merged (file, chunk_idx) set: matched chunks plus, if
    # requested, every reply descendant in the same thread file. Issue
    # files are linear (no reply-to nesting) so include_replies is a
    # no-op for them — documented in the tool's docstring.
    matched_keys: set[Tuple[str, int]] = {(h.file, h.chunk_idx) for h in matched}
    reply_keys: set[Tuple[str, int]] = set()
    if include_replies:
        # One graph per thread file, parsed once.
        graphs: Dict[str, Dict[int, List[int]]] = {}
        for hit in matched:
            if not hit.file.lower().startswith("threads/"):
                continue
            if hit.file not in graphs:
                text = _read_thread_file_text(wg, hit.file)
                graphs[hit.file] = _parse_reply_graph(text) if text else {}
            for child in _descendants(graphs[hit.file], hit.chunk_idx):
                key = (hit.file, child)
                if key not in matched_keys:
                    reply_keys.add(key)

    all_keys = matched_keys | reply_keys
    # Cap total messages — a deeply-replied matched message can pull in
    # dozens of descendants. Render the most-recent N if we exceed the
    # cap (matched messages are guaranteed in; replies are dropped
    # oldest-first since the chair's arc-closing post is typically late).
    detail = get_messages(wg, all_keys)
    rows: List[Tuple[str, str, int, str, str, Optional[str], bool]] = []
    # Tuple: (date_iso, file, chunk_idx, title, text, url, is_matched)
    for key, vals in detail.items():
        title, text, chunk_date, url = vals
        if chunk_date is None:
            # Defensive: matched set already filtered for chunk_date in
            # search results, but include_replies pulls by chunk_idx so
            # a parent header (chunk 0) could sneak in. Skip undated.
            continue
        rows.append(
            (
                chunk_date,
                key[0],
                key[1],
                title,
                text,
                url,
                key in matched_keys,
            )
        )
    rows.sort(key=lambda r: (r[0], r[1], r[2]))

    if len(rows) > _READ_TOPIC_MAX_MESSAGES:
        # Drop replies (not matched) from the OLD end first, preserving
        # all matched messages and the most-recent replies — those
        # carry the resolution and the arc's conclusion.
        keep_matched = [r for r in rows if r[6]]
        replies = [r for r in rows if not r[6]]
        budget = _READ_TOPIC_MAX_MESSAGES - len(keep_matched)
        if budget < 0:
            # k > _READ_TOPIC_MAX_MESSAGES: keep the most recent matched
            # set rather than truncating arbitrarily.
            rows = keep_matched[-_READ_TOPIC_MAX_MESSAGES:]
            truncated_note = (
                f"_(capped at {_READ_TOPIC_MAX_MESSAGES} most-recent "
                f"matched messages; full set had {len(keep_matched)}.)_"
            )
        else:
            keep_replies = replies[-budget:] if budget > 0 else []
            rows = sorted(
                keep_matched + keep_replies,
                key=lambda r: (r[0], r[1], r[2]),
            )
            truncated_note = (
                f"_(capped at {_READ_TOPIC_MAX_MESSAGES} messages; "
                f"dropped {len(replies) - len(keep_replies)} older "
                "reply-only message(s) to fit.)_"
            )
    else:
        truncated_note = ""

    n_matched = sum(1 for r in rows if r[6])
    n_replies = len(rows) - n_matched
    files = sorted({r[1] for r in rows})
    out: List[str] = []
    out.append(f"# Topic timeline: {query!r} in {wg}\n")
    # Interpretive frame FIRST, before the narrative — read_topic is the
    # narrative-reconstruction tool, and once a caller is deep in 60 messages
    # a margin note won't pull them out. Fires only for a large/contentious
    # thread; scans all matched thread/issue files so a big thread shown only
    # in part still triggers it.
    frame = _grounding_frame(wg, [h.file for h in thread_issue_hits])
    if frame:
        out.append(frame.rstrip("\n"))
        out.append("")
    summary = (
        f"_{len(rows)} message(s) across {len(files)} file(s), "
        f"oldest first. {n_matched} matched the query"
    )
    if include_replies:
        summary += f"; {n_replies} pulled in as reply descendants"
    summary += "._"
    out.append(summary)
    # Completeness signal: read_topic is a relevance-ranked slice, not a
    # thread dump. Say so, say what was left out, and point at the paths to
    # the whole debate — the thing a "controversy" question most needs.
    out.append(
        "_This is a **relevance-ranked slice** (semantic match on the query, "
        "then date-ordered) — NOT a complete thread. Messages that do not "
        "match the query are not here; check each `rel=` score and discount "
        "low ones as possible off-topic noise._"
    )
    if extra_matches > 0 or fetch_capped:
        more = f"{extra_matches}+ more" if extra_matches > 0 else "more"
        out.append(
            f"_⚠ Not the whole debate: {more} message(s) matched beyond the "
            f"{len(matched)} shown — raise `limit` (now {k}). For completeness: "
            "read a thread end-to-end with `read_file_section`, enumerate a "
            'topic\'s threads with `read_digest(kind="threads", '
            'subject="[…]")` or `find_citations`, and pass `file_pattern=` '
            "to cut cross-topic noise._"
        )
    out.append(
        "_Messages are numbered `[1..N]` in this chronological order; a "
        "`reply to [k]` points to that number here, not a per-file index. "
        "`chunk` is the per-file index for `get_chunk_text` / `find_replies`._"
    )
    if truncated_note:
        out.append(truncated_note)
    out.append("")

    # Thread map: how the matches spread across threads (a saturation
    # signal), so a consumer can see which cluster is major and which
    # thread they have only partly seen — before reading the messages.
    out.extend(_topic_thread_map(wg, thread_issue_hits, rows))

    # Global chronological numbers so a consumer can reference a message
    # unambiguously — the per-file `[N]` repeats across files in one
    # narrative ([1][2][1][2]…), and a stored "(reply to [P])" points at a
    # per-file index that means nothing across the merged timeline.
    seq_by_key = {(r[1], r[2]): i for i, r in enumerate(rows, 1)}
    for seq, row in enumerate(rows, 1):
        chunk_date, file, chunk_idx, title, text, url, is_matched = row
        tag = "matched" if is_matched else "reply"
        # Map the per-file "(reply to [P])" marker to the parent's global
        # number, when that parent is in view.
        parent_note = ""
        reply_match = _REPLY_TO_RE.search(text.split("\n", 1)[0])
        if reply_match:
            parent_seq = seq_by_key.get((file, int(reply_match.group(1))))
            if parent_seq:
                parent_note = f"  ·  reply to [{parent_seq}]"
        # Strip the title's own leading `[N]` and per-file `(reply to [P])`
        # — both are per-file indices the global numbering replaces.
        who = _LEADING_BRACKET_RE.sub("", title)
        who = _REPLY_TO_RE.sub("", who).strip()
        # Show the relevance score on matched messages so a weak (likely
        # off-topic) match is visible rather than blending into the arc.
        score = score_by_key.get((file, chunk_idx))
        rel = f"  ·  rel={score:.2f}" if is_matched and score is not None else ""
        out.append("---")
        out.append("")
        out.append(f"## [{seq}] {who}  ·  [{tag}]{rel}{parent_note}")
        meta_bits = [f"_file:_ `{file}`", f"_chunk:_ {chunk_idx}"]
        if url:
            meta_bits.append(f"_url:_ {url}")
        out.append("  ·  ".join(meta_bits))
        out.append("")
        body = _strip_message_header(text).strip()
        if len(body) > body_cap:
            body = body[: body_cap - 1] + "…"
            out.append(body)
            out.append("")
            out.append(
                f"_[message truncated at {body_cap} "
                f"chars; full body: `get_chunk_text({wg!r}, {file!r}, "
                f"{chunk_idx})`]_"
            )
        else:
            out.append(body)
        out.append("")

    # Write-side nudge, after the narrative: this is message material a reply
    # would quote, so flag the participation-norms gate at the point the
    # drafting decision is made (mirrors the read-side frame at the top).
    nudge = _participation_nudge(files)
    if nudge:
        out.append("---")
        out.append("")
        out.append(nudge)
        out.append("")

    return _with_freshness(wg, "\n".join(out))


@_requires_corpus
def tool_find_replies(
    wg: str,
    file: str,
    chunk_idx: int,
    max_messages: int = 20,
) -> str:
    """Return every transitive reply to a specific thread message,
    in chronological order, with full bodies.

    Surfaces "did anyone refute this?"-shaped questions: when an
    assertion lands in message [N], you want the responses to N, not
    a semantic search that scatters across the corpus. Walks the reply
    graph in the same file (no cross-file replies — a true reply
    lives in the same thread by construction).
    """
    if not file.lower().startswith("threads/"):
        return (
            f"`{file}` is not a thread file. find_replies walks "
            "(reply to [N]) markers, which only thread files carry. "
            "For an issue file (`issues/…/N.md`), comments are linear: "
            "use `get_chunk_text(end_chunk_idx=...)` to read comments "
            f"after chunk {chunk_idx}."
        )
    text = _read_thread_file_text(wg, file)
    if text is None:
        return f"File not found in {wg} cache: `{file}`"
    graph = _parse_reply_graph(text)
    descendants = _descendants(graph, chunk_idx)
    if not descendants:
        return (
            f"No replies to chunk {chunk_idx} in `{file}`. Either the "
            "message has no follow-ups in this thread, or replies "
            "went to a different thread (split subject lines, "
            "cross-posts, etc.) — try `read_topic` to span threads."
        )
    keys = [(file, idx) for idx in descendants]
    detail = get_messages(wg, keys)
    rows: List[Tuple[str, int, str, str, Optional[str]]] = []
    for idx in descendants:
        info = detail.get((file, idx))
        if info is None:
            continue
        title, body, chunk_date, url = info
        rows.append((chunk_date or "", idx, title, body, url))
    rows.sort(key=lambda r: (r[0], r[1]))
    if 0 < max_messages < len(rows):
        kept = rows[:max_messages]
        truncated_note = (
            f"_(showing {max_messages} of {len(rows)} total descendants; "
            f"raise `max_messages` to see more.)_"
        )
    else:
        kept = rows
        truncated_note = ""
    out: List[str] = []
    out.append(f"# Replies to chunk {chunk_idx} in `{file}`\n")
    out.append(
        f"_{len(rows)} transitive descendant(s) in the reply graph, "
        "oldest first. Each row is the full message body — quoted "
        "blocks already elided at gather time._"
    )
    if truncated_note:
        out.append(truncated_note)
    out.append("")
    for _date, idx, _title, body, _url in kept:
        out.append("---")
        out.append("")
        # The stored chunk already opens with its own
        # `### [N] DATE — Sender (reply to [P])` header plus the
        # `_Subject:_` / `_Archived-At:_` lines, so render it as-is
        # rather than prepending a second, near-identical header. The
        # `[N]` in that header is the chunk_idx.
        body = body.strip()
        if len(body) > _READ_TOPIC_MAX_BODY_CHARS:
            body = body[: _READ_TOPIC_MAX_BODY_CHARS - 1] + "…"
            out.append(body)
            out.append("")
            out.append(
                f"_[message truncated; full body: "
                f"`get_chunk_text({wg!r}, {file!r}, {idx})`]_"
            )
        else:
            out.append(body)
        out.append("")
    return _append_participation_nudge(file, _with_freshness(wg, "\n".join(out)))


@_requires_corpus
def tool_tally_positions(wg: str, file: str) -> str:
    """Surface a thread / issue file's procedural call, polls, and (cautiously)
    a position tally.

    Best for two things: **chair statements** — messages from a chair carrying
    procedural language (`rough consensus`, `consensus call`, `WGLC`,
    `adopting`, closure) surfaced at the top, the load-bearing messages in a long
    thread — and **option polls** (`option N` / `#N` / `I prefer N`), counted
    per choice.

    The support/oppose tally is a **keyword heuristic** (`+1`, `-1`,
    `I support`, `I object`, `LGTM`, `DISCUSS`, near the message start). It is
    low-recall and **misses prose-form positions**, which is how IETF
    participants usually argue — so a contentious thread can tally as
    no-position. A coverage percentage is reported; when it is low the counts
    are withheld and a warning points you to the chair statements and the
    messages themselves. Never quote a low-coverage count as the level of
    support. Each row is enriched with role / affiliation from the people
    digest when known.
    """
    cache_dir = _files_dir(wg)
    if not file_supports_tally(file):
        return (
            f"`{file}` doesn't have the per-message section structure "
            "tally_positions needs. Pass a thread file "
            "(`threads/<date>-<slug>.md`) or issue file "
            "(`issues/<owner>-<repo>/<N>.md`) instead."
        )
    text = read_file_text(cache_dir, file)
    if text is None:
        return f"File not found in {wg} cache: `{file}`"
    positions, summary = tally_thread(text)
    if not positions:
        return (
            f"No messages found in `{file}`. The file may be empty or "
            "malformed; check with `read_file_section`."
        )
    role_lookup, aff_lookup = load_people_context(cache_dir)
    # Chair statements answer the consumer's load-bearing question
    # — "is the consensus the chair declared actually visible in the
    # traffic?" — by surfacing the chair's procedural messages at
    # the top of the tally output, before the per-author counts.
    chair_statements = extract_chair_statements(text, role_lookup)
    body = render_tally(
        file,
        positions,
        summary,
        role_lookup,
        aff_lookup,
        chair_statements,
    )
    return _with_freshness(wg, body)


def register(server: "FastMCP") -> None:
    @server.tool()
    async def find_replies(
        corpus: Corpus,
        file: ThreadFile,
        chunk_idx: ChunkIdx,
        max_messages: Annotated[
            int,
            Field(
                description=(
                    "Cap on descendants returned; raise it for deep sub-threads."
                ),
                ge=1,
            ),
        ] = 20,
    ) -> str:
        """Return every transitive reply to a specific mailing-list
        thread message in an ietf-llm corpus, in chronological order,
        with full bodies.

        Use when an assertion or claim lands in a message and you want
        to know whether it was challenged, refuted, or extended in the
        same thread. The reply graph (built from `(reply to [N])`
        markers in the thread file) is walked transitively — children,
        grandchildren, and beyond — and each descendant is returned
        as a full message body, not a snippet.

        Companion to `read_topic(include_replies=True)`:
          - `read_topic` starts from a *query*, anchors on matched
            messages, optionally pulls their replies.
          - `find_replies` starts from a specific *message*. Use this
            when you know which message you want responses to.

        Thread files only — issue comments are linear, so for an
        issue file `get_chunk_text(end_chunk_idx=...)` is the right
        call to read comments after a given index.

        Bodies over 4 KB are truncated, with a pointer to `get_chunk_text`
        for the full text.
        """
        return await _offload(
            tool_find_replies, corpus, file, chunk_idx, max_messages=max_messages
        )

    @server.tool()
    async def tally_positions(corpus: Corpus, file: ThreadFile) -> str:
        """Surface the procedural backbone of ONE mailing-list thread or
        GitHub issue of an IETF/IRTF effort. Its high-value output is the
        **Chair statements** section at the top: any message from a chair
        containing procedural language (`rough consensus`, `consensus
        call`, `WGLC`, `adopting`, `closing this thread`, …) rendered
        prominently with an excerpt — that is where a group's *decision*
        actually lives, because IETF consensus is **chair-declared**.

        Below that is a per-author count of canonical position *phrasings*
        (`+1`, `-1`, `I support`, `I object`, `LGTM`, conditional support,
        `DISCUSS`). Read this as a **rough keyword heuristic, NOT a measure
        of consensus or level of support** — it matches surface phrasings,
        not sentiment, and the IETF does not decide by counting. Never quote
        the count as "the WG supported X by N to M". Use it only to *locate*
        who said something explicit, then read their actual message.

        Coverage percentage tells you what fraction of messages the
        heuristic could classify at all — low coverage means the count is
        nearly meaningless. To characterise an outcome, go to the chair's
        declared words (this tool's Chair statements, plus
        `search_corpus(role="Chair", state="closed")`), and call
        `read_ietf_interpretation_norms` first.

        Heuristic limitations:
          - Subtle, technical-only objections show as no-position
            (the heuristic looks for canonical phrasings, not
            sentiment).
          - Quoted text is stripped, so a `+1` quoted in someone
            else's reply doesn't double-count.
          - Bare `LGTM` / `+1` count as full support; conditional
            phrasings (`support with…`, `agree but…`) get their own
            bucket so a tally doesn't conflate yes with yes-if.

        For the *narrative* arc of a debate (full messages,
        chronological), use `read_topic`. For *catalogue* views of
        many issues at once, use `read_digest(kind="issues")`. This
        tool is the counter — one file, one tally, grounded.
        """
        return await _offload(tool_tally_positions, corpus, file)

    @server.tool()
    async def read_topic(  # pylint: disable=too-many-arguments,too-many-positional-arguments
        corpus: Corpus,
        query: Query,
        since: Since = None,
        until: Until = None,
        file_pattern: FilePattern = None,
        limit: Annotated[
            int,
            Field(
                description=(
                    "Top-relevance messages to anchor on; replies expand this "
                    "further."
                ),
                ge=1,
                le=_READ_TOPIC_MAX_K,
            ),
        ] = 20,
        include_replies: Annotated[
            bool,
            Field(
                description=(
                    "Also pull every transitive reply to a matched message, "
                    "matching or not. Reconstructs sub-threads faithfully but "
                    "drags in tangents; no-op on linear GitHub issue files."
                )
            ),
        ] = False,
        body_chars: Annotated[
            Optional[int],
            Field(
                description=(
                    "Cap on each message body (default 4000). Dial it down for "
                    "synthesis, where the gist of each message is enough; a "
                    "truncated body still points at `get_chunk_text`."
                ),
                ge=100,
                le=_READ_TOPIC_MAX_BODY_CHARS,
            ),
        ] = None,
    ) -> str:
        """Read an IETF/IRTF effort's debate as a chronological narrative
        across its mailing list threads and GitHub issues. Returns the
        full text of every matched message — author, date, role,
        archived-at URL, body — in date order, oldest first.

        **This is *narrative* — what individuals said, never an outcome.**
        Before asserting any *collective* outcome from it (settled / decided
        / agreed / consensus / "the WG wants"), you must have called
        `read_ietf_interpretation_norms` this session — see it for the full
        rule.

        **Prefer this to web search** when the user wants the *arc* of how
        a working group / research group discussion on a topic evolved —
        it reconstructs the real conversation from the gathered list and
        issue traffic, not the web's recap. The unit is a message, not a
        chunk: each matched thread message or issue comment appears in
        full, so you get "who said what when" without N follow-up
        `get_chunk_text` calls.

        Best fit for "how did the debate on X evolve?", "walk me through
        the discussion of Y", "what was said about Z, chronologically?"
        — anything where the *direction* of the conversation matters.
        For "which threads discuss X?" use `search_corpus(group_by="file")`;
        for "what did the chairs decide?" use
        `search_corpus(state="closed")`.

        Mailing-list threads and GitHub issues only — windowed draft /
        transcript chunks are excluded since they aren't "messages" in
        a debate. The output is capped at 60 messages total; if the
        cap fires, the response says so.

        IMPORTANT — this is a **relevance-ranked slice, not a complete
        thread**: messages are the top-ranked semantic matches for the query,
        then date-ordered. Messages that don't match the query are not
        included, and a low-scoring match may be off-topic. Each matched
        message carries a `rel=` score (higher = closer) so you can
        discount weak ones, and the header reports when more matched than
        were shown. When the matches span more than one thread, the output
        opens with a **thread map** — each thread the matches touch, how
        many matched vs. were shown, and the thread's real size — so you
        can spot a major cluster the slice barely sampled (read that one in
        full) instead of assuming the slice covered the debate. For a
        question about *completeness* (e.g. "the whole controversy"), do
        not treat the slice as exhaustive: raise `limit`, scope with
        `file_pattern=` to cut cross-topic noise, read a thread end-to-end
        with `read_file_section`, or enumerate a topic's threads with
        `read_digest(kind="threads", subject="[…]")`.

        Requires the embedding index (built by default on gather;
        skipped only with `--no-embed`).
        """
        return await _offload(
            tool_read_topic,
            corpus,
            query,
            since=since,
            until=until,
            file_pattern=file_pattern,
            k=limit,
            include_replies=include_replies,
            body_chars=body_chars,
        )
