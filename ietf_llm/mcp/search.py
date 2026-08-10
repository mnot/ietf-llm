"""Semantic-search tools: search_corpus, find_related, search_corpora."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

from ..embeddings import index_model, related, search
from ..freshness import gather_enabled, staleness_warning
from ..log import Verbosity
from .common import (
    _corpus_exists,
    _grounding_frame,
    _index_rebuilding_note,
    _invalid_date_message,
    _offload,
    _regather_call,
    _requires_corpus,
    _with_freshness,
)

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP  # pragma: no cover


#: Upper bound on `k` for search_corpus, so a huge value can't return
#: thousands of chunks and blow the context window.
_MAX_SEARCH_K = 100


# --- Tool implementations (plain functions, also usable for unit tests) -----


def _flatten_rationale(rationale: str, limit: int) -> str:
    """Strip blockquote markers and metadata lines for a one-line
    preview of a closing rationale. The full formatted rationale lives
    in the per-issue file; this is just the inline hint in search
    output, so we want the substance of the comment, not the chrome.
    """
    cleaned: List[str] = []
    for line in rationale.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        # Drop the "_by Author on Date:_" italic byline and the
        # leading `> ` blockquote markers — both are formatting that
        # doesn't carry information at preview size.
        if stripped.startswith("_by ") and stripped.endswith(":_"):
            continue
        if stripped.startswith("> "):
            stripped = stripped[2:]
        cleaned.append(stripped)
    flat = " ".join(cleaned)
    if len(flat) > limit:
        flat = flat[: limit - 1] + "…"
    return flat


def _render_file_grouped(hits: List[Any], limit: int) -> str:
    """Collapse a flat hit list to one row per file.

    Best (highest-scoring) chunk wins as the representative; the row
    carries the hit count so the consumer sees which threads are
    *consistently* relevant vs hit once on a stray keyword. Capped at
    `limit` files so the output stays bounded.
    """
    by_file: Dict[str, Any] = {}
    counts: Dict[str, int] = {}
    for hit in hits:
        counts[hit.file] = counts.get(hit.file, 0) + 1
        prev = by_file.get(hit.file)
        if prev is None or hit.score > prev.score:
            by_file[hit.file] = hit
    # Rank files by best-chunk score; tie-break on hit count.
    ranked = sorted(
        by_file.values(),
        key=lambda h: (h.score, counts[h.file]),
        reverse=True,
    )[:limit]
    out: List[str] = []
    out.append(
        f"_{len(ranked)} files (collapsed from {len(hits)} chunks). "
        "Per-file rollup: best chunk shown; hit count = matching "
        "chunks per file._"
    )
    out.append("")
    for i, hit in enumerate(ranked, 1):
        hit_count = counts[hit.file]
        out.append(f"[{i}] score={hit.score:.3f}  hits={hit_count}  file={hit.file}")
        out.append(f"     best chunk {hit.chunk_idx}: {hit.title}")
        if hit.url:
            out.append(f"     url: {hit.url}")
        out.append(f"     {hit.snippet}")
    return "\n".join(out)


def _render_hits(hits: List[Any], k: int, group_by: Optional[str]) -> str:
    """Render a list of `Hit`s to the text block shown to the caller.

    Shared by `tool_search` and `tool_find_related`. `group_by="file"`
    collapses to one row per file; otherwise the per-chunk view, led by a
    result-set state summary when every hit shares one issue state.
    """
    if group_by == "file":
        return _render_file_grouped(hits, k)
    hits = hits[:k]
    lines: List[str] = []
    # Result-set state summary. When every hit comes from a closed issue,
    # the answer the consumer cares about is "this debate is resolved"
    # — surfacing that once at the top stops an LLM from presenting an
    # archived debate as if it were live (and saves the per-hit `[closed]`
    # tags from being noise on a uniform result set).
    states = {h.state for h in hits if h.state}
    files_with_state = sum(1 for h in hits if h.state)
    if states and len(states) == 1 and files_with_state == len(hits):
        only_state = next(iter(states))
        lines.append(
            f"_All {len(hits)} hits are from {only_state} issues. "
            + (
                "This topic appears resolved; closed issues hold the "
                "chairs' resolution."
                if only_state == "closed"
                else "These issues are still under discussion."
            )
            + "_"
        )
        lines.append("")
    for i, hit in enumerate(hits, 1):
        loc = (
            f" lines={hit.start_line}-{hit.end_line}"
            if hit.start_line is not None
            else ""
        )
        # State goes on the header line — it's a one-word signal that
        # changes how the caller should weight the hit. Labels (longer
        # and only sometimes present) get their own line below.
        state_tag = f"  [{hit.state}]" if hit.state else ""
        lines.append(
            f"[{i}] score={hit.score:.3f}  file={hit.file}  "
            f"chunk={hit.chunk_idx}{loc}{state_tag}"
        )
        lines.append(f"     {hit.title}")
        if hit.labels:
            lines.append(f"     labels: {hit.labels}")
        # Cluster signals — saves a follow-up file read when scanning
        # results. dup-of nudges the LLM to skip duplicate issues;
        # the closing-rationale preview surfaces the "why" without
        # the consumer having to open the file.
        if hit.duplicate_of is not None:
            lines.append(f"     duplicate of: #{hit.duplicate_of}")
        if hit.closing_rationale:
            preview = _flatten_rationale(hit.closing_rationale, 140)
            lines.append(f"     closing: {preview}")
        # Citation URL straight from the chunk: GitHub URL for issue
        # chunks, IETF Archived-At permalink for thread message chunks.
        # NULL for drafts/transcripts and pre-v6 indexes — silently skip.
        if hit.url:
            lines.append(f"     url: {hit.url}")
        lines.append(f"     {hit.snippet}")
    return "\n".join(lines)


# --- read_topic --------------------------------------------------------------
#
# Cross-file chronological view for one topic. The mailing-list / GitHub
# corpus fragments a debate across many thread + issue files (subject lines
# fork, parallel issues open, replies branch). `search_corpus` is great
# for "which chunks match X" but reading 15 overlapping chunks doesn't
# reconstruct the *arc* of the debate. read_topic does:
#
#   1. semantic match against the query (widened fetch_k)
#   2. restrict to thread / issue chunks with a chunk_date (drafts and
#      transcripts have no place in a debate timeline)
#   3. top-k by relevance
#   4. optionally walk reply descendants in the same thread file
#   5. fetch full chunk text (NOT a snippet — the unit is a message)
#   6. sort merged set by chunk_date and render
#
# The output unit is a message, not a chunk: full body, attribution
# header, archived-at URL. A reading LLM gets "who said what when" with
# no follow-up tool calls.


@_requires_corpus
def tool_search(  # pylint: disable=too-many-arguments,too-many-positional-arguments,too-many-locals,too-many-branches
    wg: str,
    query: str,
    k: int = 10,
    file_pattern: Optional[str] = None,
    since: Optional[str] = None,
    until: Optional[str] = None,
    label: Optional[str] = None,
    state: Optional[str] = None,
    sort: Optional[str] = None,
    group_by: Optional[str] = None,
    author: Optional[str] = None,
    role: Optional[str] = None,
    snippet_chars: Optional[int] = None,
    collapse_versions: bool = True,
    diversify: bool = True,
) -> str:
    for field, value in (("since", since), ("until", until)):
        date_error = _invalid_date_message(value, field)
        if date_error:
            return date_error
    # Clamp k to a sane range so a huge value can't return thousands of
    # chunks (context bomb) and a negative one can't behave oddly.
    try:
        k = max(1, min(int(k), _MAX_SEARCH_K))
    except (TypeError, ValueError):
        k = 10
    # Over-fetch when we will thin the results — `group_by="file"`
    # rolls up per file, and `collapse_versions` drops older draft revs —
    # so the final list still reaches `k` distinct items.
    over_fetch = group_by == "file" or collapse_versions
    fetch_k = max(k * 4, 20) if over_fetch else k
    hits = search(
        wg,
        query,
        k=fetch_k,
        file_pattern=file_pattern,
        since=since,
        until=until,
        label=label,
        state=state,
        sort=sort,
        author=author,
        role=role,
        snippet_chars=snippet_chars,
        # `group_by="file"` is already a coarse diversification (one row
        # per file), so MMR on top of it is redundant churn — let the
        # rollup do the de-duplication and keep the per-file best chunks.
        diversify=diversify and group_by != "file",
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
        return _with_freshness(wg, f"(no results — {no_index})")
    dropped = 0
    if collapse_versions:
        hits, dropped = _collapse_draft_versions(hits)
    note = ""
    if dropped:
        note = (
            f"\n_{dropped} older draft revision(s) hidden — the latest "
            "matching revision is shown. Pass `collapse_versions=False`, or "
            "a versioned `file_pattern` (e.g. `drafts/%-04.txt`), for older "
            "revisions._"
        )
    # When the consumer is asking a breadth question ("which threads
    # discuss X?"), the default per-chunk hit list shows the same
    # thread five times — wasting context. group_by="file" collapses
    # to one row per file with hit count + best chunk; the per-chunk
    # view stays the default for depth questions.
    # Grounding frame at the TOP — read before the hits, with no separate
    # tool call to skip. Only the open-ended search path gets it (not the
    # shared `_render_hits`, which `tool_find_related` also uses). Fires only
    # when a result touches a thread big enough that its consensus / positions
    # shouldn't be read off snippets. We scan the chunk-level `hits[:k]`; in
    # `group_by="file"` mode the rendered rows collapse to files, so the named
    # thread may not be a displayed row — harmless, the frame is valid for the
    # result set either way.
    frame = _grounding_frame(wg, [h.file for h in hits[:k]])
    body = _render_hits(hits, k, group_by) + note
    return _with_freshness(wg, f"{frame}\n{body}" if frame else body)


def tool_find_related(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    wg: str,
    file: str,
    chunk_idx: int,
    k: int = 10,
    file_pattern: Optional[str] = None,
    since: Optional[str] = None,
    until: Optional[str] = None,
    label: Optional[str] = None,
    state: Optional[str] = None,
    group_by: Optional[str] = None,
    snippet_chars: Optional[int] = None,
    diversify: bool = True,
    collapse_versions: bool = True,
) -> str:
    for field, value in (("since", since), ("until", until)):
        date_error = _invalid_date_message(value, field)
        if date_error:
            return date_error
    try:
        k = max(1, min(int(k), _MAX_SEARCH_K))
    except (TypeError, ValueError):
        k = 10
    try:
        chunk_idx = int(chunk_idx)
    except (TypeError, ValueError):
        return f"(invalid chunk_idx {chunk_idx!r} — must be an integer)"
    # Over-fetch when we will thin the results — `group_by="file"` rolls up
    # per file, and `collapse_versions` drops older draft revs — so the
    # final list still reaches `k` distinct items (mirrors tool_search).
    over_fetch = group_by == "file" or collapse_versions
    fetch_k = max(k * 4, 20) if over_fetch else k
    hits = related(
        wg,
        file,
        chunk_idx,
        k=fetch_k,
        file_pattern=file_pattern,
        since=since,
        until=until,
        label=label,
        state=state,
        snippet_chars=snippet_chars,
        diversify=diversify and group_by != "file",
        verbose=Verbosity.QUIET,
    )
    if not hits:
        no_index = (
            f"no search index, or no chunk {chunk_idx} in {file} — "
            f"re-gather with {_regather_call(wg)}"
            if gather_enabled()
            else f"no chunk {chunk_idx} in {file}, or `ietf-llm {wg} --embed` "
            "has not been run"
        )
        return _with_freshness(wg, f"(no related chunks — {no_index})")
    dropped = 0
    if collapse_versions:
        hits, dropped = _collapse_draft_versions(hits)
    note = ""
    if dropped:
        note = (
            f"\n_{dropped} older draft revision(s) hidden — the latest "
            "matching revision is shown. Pass `collapse_versions=False`, or "
            "a versioned `file_pattern` (e.g. `drafts/%-04.txt`), for older "
            "revisions._"
        )
    return _with_freshness(wg, _render_hits(hits, k, group_by) + note)


#: Upper bound on how many corpora one `search_corpora` call will fan
#: across, so an over-long list can't turn a single call into a heavy
#: multi-index scan. Corpora past the cap are dropped with a note (never
#: silently truncated).
_MAX_SEARCH_CORPORA = 12


def _dedup_corpus_names(corpora: List[str]) -> List[str]:
    """Strip / drop blanks / de-dup the requested corpus names, preserving
    first-seen order. Non-string entries are ignored defensively."""
    seen: set[str] = set()
    out: List[str] = []
    for name in corpora:
        if not isinstance(name, str):
            continue
        name = name.strip()
        if name and name not in seen:
            seen.add(name)
            out.append(name)
    return out


def tool_search_corpora(  # pylint: disable=too-many-arguments,too-many-positional-arguments,too-many-locals,too-many-branches,too-many-statements
    corpora: List[str],
    query: str,
    k: int = 10,
    since: Optional[str] = None,
    until: Optional[str] = None,
    label: Optional[str] = None,
    state: Optional[str] = None,
    author: Optional[str] = None,
    role: Optional[str] = None,
    snippet_chars: Optional[int] = None,
    collapse_versions: bool = True,
) -> str:
    for field, value in (("since", since), ("until", until)):
        date_error = _invalid_date_message(value, field)
        if date_error:
            return date_error
    requested = _dedup_corpus_names(corpora or [])
    if not requested:
        return (
            "search_corpora needs an explicit `corpora` list — the few "
            "efforts that dominate the topic, not a blind scan. Use "
            "`find_efforts(topic)` to discover candidates and `list_corpora` "
            "to see what is already cached, then pass the chosen names."
        )
    # Bound the fan-out. Excess corpora are reported, not silently dropped.
    dropped_for_cap: List[str] = []
    if len(requested) > _MAX_SEARCH_CORPORA:
        dropped_for_cap = requested[_MAX_SEARCH_CORPORA:]
        requested = requested[:_MAX_SEARCH_CORPORA]
    # Read-only existence check first, so a typo'd name is reported rather
    # than materialising a junk cache (see `_corpus_exists`).
    unknown = [c for c in requested if not _corpus_exists(c)]
    known = [c for c in requested if _corpus_exists(c)]
    # Clamp k to the same sane range as single-corpus search.
    try:
        k = max(1, min(int(k), _MAX_SEARCH_K))
    except (TypeError, ValueError):
        k = 10
    # Per corpus: read its embedding-model id (governs score comparability),
    # then run the same single-corpus search() and tag the hits.
    per_corpus: Dict[str, List[Any]] = {}
    model_by_corpus: Dict[str, str] = {}
    no_index: List[str] = []
    empty: List[str] = []
    dropped_versions = 0
    for corpus in known:
        model = index_model(corpus)
        if model is None:
            no_index.append(corpus)
            continue
        # Over-fetch when collapsing draft revisions so each corpus can
        # still contribute k distinct items to the merge.
        fetch_k = max(k * 4, 20) if collapse_versions else k
        hits = search(
            corpus,
            query,
            k=fetch_k,
            since=since,
            until=until,
            label=label,
            state=state,
            author=author,
            role=role,
            snippet_chars=snippet_chars,
            verbose=Verbosity.QUIET,
        )
        if collapse_versions and hits:
            hits, dropped = _collapse_draft_versions(hits)
            dropped_versions += dropped
        if not hits:
            empty.append(corpus)
            continue
        per_corpus[corpus] = hits[:k]
        model_by_corpus[corpus] = model

    # Skip diagnostics — surfaced whether or not any hits came back, so a
    # caller always learns which requested corpora contributed nothing.
    skip_notes: List[str] = []
    if unknown:
        skip_notes.append(f"unknown (not gathered): {', '.join(unknown)}")
    if no_index:
        how = (
            're-gather each with `start_gather(corpus="<name>", force=True)`'
            if gather_enabled()
            else "run `ietf-llm <name>`"
        )
        skip_notes.append(f"no embedding index — {how}: " + ", ".join(no_index))
    if empty:
        skip_notes.append(f"no matching hits: {', '.join(empty)}")
    if dropped_for_cap:
        skip_notes.append(
            f"dropped over the {_MAX_SEARCH_CORPORA}-corpus cap: "
            + ", ".join(dropped_for_cap)
        )

    if not per_corpus:
        body = [f"(no results for {query!r} across the requested corpora)"]
        body += [f"_Skipped — {n}._" for n in skip_notes]
        return "\n".join(body)

    # Group corpora by embedding-model id (insertion order = first-seen).
    # Scores are directly comparable only within a group; see index_model.
    groups: Dict[str, List[str]] = {}
    for corpus in known:
        if corpus in per_corpus:
            groups.setdefault(model_by_corpus[corpus], []).append(corpus)

    # Rank within each group on raw score (comparable there).
    group_ranked: List[List[Tuple[str, Any]]] = []
    for members in groups.values():
        pool: List[Tuple[str, Any]] = [
            (corpus, hit) for corpus in members for hit in per_corpus[corpus]
        ]
        pool.sort(key=lambda ch: -ch[1].score)
        group_ranked.append(pool)

    if len(group_ranked) == 1:
        # One model across all corpora — a single comparable ranking.
        final = group_ranked[0][:k]
    else:
        # Differing models — interleave the per-group rankings round-robin
        # by rank position rather than merging on non-comparable scores.
        final = []
        idx = 0
        while len(final) < k and any(idx < len(g) for g in group_ranked):
            for grp in group_ranked:
                if idx < len(grp):
                    final.append(grp[idx])
                    if len(final) >= k:
                        break
            idx += 1

    queried = [c for c in known if c in per_corpus]
    lines: List[str] = []
    if len(groups) > 1:
        lines.append(
            "_Corpora use different embedding models, so scores are NOT "
            "comparable across them — grouped by model and interleaved by "
            "rank:_"
        )
        for model, members in groups.items():
            lines.append(f"_  • `{model}`: {', '.join(members)}_")
    else:
        only_model = next(iter(groups))
        lines.append(
            f"_Ranked across {len(queried)} corpora ({', '.join(queried)}); "
            f"all share embedding model `{only_model}`, so scores are "
            "directly comparable._"
        )
    # Surface stale corpora once, compactly — depth tools repeat the detail.
    stale = [c for c in queried if staleness_warning(c)]
    if stale:
        lines.append(f"_Stale (consider re-gathering): {', '.join(stale)}._")
    for note in skip_notes:
        lines.append(f"_Skipped — {note}._")
    lines.append("")

    for i, (corpus, hit) in enumerate(final, 1):
        loc = (
            f" lines={hit.start_line}-{hit.end_line}"
            if hit.start_line is not None
            else ""
        )
        state_tag = f"  [{hit.state}]" if hit.state else ""
        lines.append(
            f"[{i}] corpus={corpus}  score={hit.score:.3f}  file={hit.file}  "
            f"chunk={hit.chunk_idx}{loc}{state_tag}"
        )
        lines.append(f"     {hit.title}")
        if hit.labels:
            lines.append(f"     labels: {hit.labels}")
        if hit.url:
            lines.append(f"     url: {hit.url}")
        lines.append(f"     {hit.snippet}")

    if dropped_versions:
        lines.append(
            f"\n_{dropped_versions} older draft revision(s) hidden across "
            "corpora; pass `collapse_versions=False` for older revisions._"
        )
    lines.append(
        "\n_Breadth view — this finds WHERE a topic lives across efforts. "
        "Pivot to the single-corpus tools for depth, using the `corpus=` "
        "tag: `search_corpus(corpus, query, ...)`, `read_topic`, "
        "`tally_positions`, `read_digest`; read a hit with "
        "`get_chunk_text(corpus, file, chunk)` / `read_file_section`._"
    )
    return "\n".join(lines)


#: A draft file with a 2-digit revision suffix, e.g.
#: `drafts/draft-ietf-httpbis-rfc6265bis-04.txt`. RFC files
#: (`drafts/rfc9110.txt`) have no revision and are never collapsed.
_DRAFT_REV_RE = re.compile(r"^(?P<stem>drafts/draft-.+)-(?P<rev>\d{2})\.txt$")


def _collapse_draft_versions(hits: List[Any]) -> "Tuple[List[Any], int]":
    """Drop a hit from an older draft revision when a newer revision of the
    same draft is also in the result set.

    Searching across every gathered draft revision otherwise returns the
    same section several times (`…-rfc6265bis-04`, `-02`, `-22`, …). Keep
    the newest revision that actually matched per draft stem; older
    revisions stay reachable via `collapse_versions=False` or a versioned
    `file_pattern`. Non-draft hits (RFCs, threads, issues, meetings) pass
    through untouched. Returns `(kept, dropped_count)`.
    """
    latest: Dict[str, int] = {}
    for hit in hits:
        match = _DRAFT_REV_RE.match(hit.file)
        if match:
            stem, rev = match.group("stem"), int(match.group("rev"))
            latest[stem] = max(latest.get(stem, -1), rev)
    kept: List[Any] = []
    dropped = 0
    for hit in hits:
        match = _DRAFT_REV_RE.match(hit.file)
        if match and int(match.group("rev")) != latest[match.group("stem")]:
            dropped += 1
            continue
        kept.append(hit)
    return kept, dropped


def register(server: "FastMCP") -> None:
    @server.tool()
    async def search_corpus(  # pylint: disable=too-many-arguments,too-many-positional-arguments
        corpus: str,
        query: str,
        limit: int = 10,
        file_pattern: Optional[str] = None,
        since: Optional[str] = None,
        until: Optional[str] = None,
        label: Optional[str] = None,
        state: Optional[str] = None,
        sort: Optional[str] = None,
        group_by: Optional[str] = None,
        author: Optional[str] = None,
        role: Optional[str] = None,
        snippet_chars: Optional[int] = None,
        collapse_versions: bool = True,
        diversify: bool = True,
    ) -> str:
        """Search the gathered record of an IETF/IRTF effort — a working
        group, research group, mailing list, or set of Internet-Drafts —
        semantically across its mailing-list debate, GitHub issues,
        drafts, slides, transcripts, and minutes. (Published RFC bodies
        aren't indexed here by default — search the series with
        `search_rfcs` and read one with `get_rfc`.) Returns the top
        chunks with file, chunk_idx, title, score, snippet, line range,
        GitHub URL (for issue chunks), and (for issue chunks) the issue's
        GitHub labels + open/closed state.

        **Prefer this to web search** for any question about what an
        IETF/IRTF group discussed, debated, or decided about a topic —
        this reads the group's *actual* list traffic and issues, not the
        web's second-hand summary of them. Substantive "what was said
        about X?" / "what's the group's stance on Y?" questions land here.
        Pivot with `get_chunk_text` or `read_file_section` to read a hit
        in context.

        For topical questions ("arguments for/against X", "scope debate")
        try `label=` first — the corpus's own labels (e.g. "vocabulary",
        "top-level", "ready to close") are usually better curation than
        semantic ranking alone. Pair with `kind="issues"` in `read_digest`
        to get the issue catalogue, then `search_corpus` for depth inside
        the matching issues.

        `state="closed"` narrows to resolved issues — prefer this when
        the user wants the WG's settled position rather than ongoing
        debate. `state="open"` is the inverse: only unresolved threads.

        `sort="date"` re-orders the top hits chronologically (oldest
        first) instead of by relevance, so a consumer reading
        top-to-bottom sees an early objection → settled-position
        arc. Combine with `file_pattern="%-issue-…-N.md"` to scope to
        one issue, or `since`/`until` for a time window. NULL-dated
        chunks (drafts, transcripts) are excluded under `sort="date"`.

        `group_by="file"` collapses the per-chunk hit list to one row
        per file with a hit count, so a breadth question ("which
        threads discuss X?") returns four distinct threads instead of
        fifteen overlapping chunks. Use this when triaging WHERE a
        topic lives; switch back to the default per-chunk view for
        depth questions ("what did Alice say about Y?").

        `author="<substring>"` filters to chunks whose section header
        contains that name — "what did Rescorla say about X?" /
        "show me Mattsson's messages on Y" without needing the file path.
        Matches substrings, so partial / surname-only queries work.
        Windowed draft / transcript chunks have no author and drop out.
        It matches who **sent** the message, so someone quoted at length in
        another person's reply will not surface — their words are in the
        corpus, under the sender's name. An `author=` miss is therefore not
        evidence that a person said nothing; `grep_corpus` searches the text
        regardless of who sent it.

        `role="Chair"` (or `"Author"`, `"Editor"`, `"AD"`) filters to
        messages from people with that structural role. Useful for
        "what did the chairs decide" / "did the editor weigh in" —
        the registry stamps `(Role)` into each section header at
        gather time, and the filter matches against that tag.

        `snippet_chars=N` raises the snippet budget per hit. Default
        renders compact snippets that often `[truncated]` for long
        chunks; raise for long-form synthesis where the snippet
        itself should carry more context. Tradeoff: bigger budget
        means more bytes per hit, so dial `limit` down accordingly.

        `collapse_versions=True` (the default) hides older draft
        revisions when a newer one of the same draft also matched, so a
        query does not return the same section as `…-rfc6265bis-04`,
        `-02`, `-22`. Set it False, or pin a revision with `file_pattern`
        (e.g. `"drafts/%-04.txt"`), to search a specific older revision.

        `diversify=True` (the default) spreads the results across the
        threads/issues that match instead of returning five chunks of
        the one most-relevant thread — better for "what are the angles
        on X?". Set False for the raw relevance ranking when you want
        every closely-matching chunk even if they overlap. Has no effect
        under `sort="date"` (a timeline keeps adjacent messages) or
        `group_by="file"` (already one row per file).

        Requires the embedding index (built by default on gather;
        skipped only with `--no-embed`).

        Optional facets:
          - file_pattern: SQL LIKE pattern over the relative path
            (e.g. "threads/%" to restrict to mailing-list threads,
            "issues/%" for GitHub issues, "pulls/%" for pull
            requests, "drafts/%" for drafts).
            % is wildcard.
          - since / until: ISO 8601 dates (e.g. "2026-01-01"). Only
            mailing-list and GitHub chunks have dates; windowed draft
            chunks are excluded when either bound is set.
        """
        return await _offload(
            tool_search,
            corpus,
            query,
            k=limit,
            file_pattern=file_pattern,
            since=since,
            until=until,
            label=label,
            state=state,
            sort=sort,
            group_by=group_by,
            author=author,
            role=role,
            snippet_chars=snippet_chars,
            collapse_versions=collapse_versions,
            diversify=diversify,
        )

    @server.tool()
    async def find_related(  # pylint: disable=too-many-arguments,too-many-positional-arguments
        corpus: str,
        file: str,
        chunk_idx: int,
        limit: int = 10,
        file_pattern: Optional[str] = None,
        since: Optional[str] = None,
        until: Optional[str] = None,
        label: Optional[str] = None,
        state: Optional[str] = None,
        group_by: Optional[str] = None,
        snippet_chars: Optional[int] = None,
        diversify: bool = True,
        collapse_versions: bool = True,
    ) -> str:
        """Find the chunks most similar to one you already have — a
        nearest-neighbour-by-example search over the same index
        `search_corpus` uses. Where `search_corpus` takes a query
        *string*, this takes an existing chunk (`file` + `chunk_idx`, the
        identity in every search hit and `get_chunk_text` call) and
        returns the others closest to it in meaning. The seed chunk is
        excluded from its own results.

        Reach for this after a search or a read when you want "more like
        this": other threads making the same argument, prior issues on
        the same point, the drafts a message is really about — without
        having to guess the right query words.

        **Cross-surface bridging** is the highest-value use. A topic is
        usually discussed in BOTH the mailing list and a GitHub issue;
        they sit close together in the index but aren't linked. Seed on a
        thread message and pass `file_pattern="issues/%"` to surface the
        issue(s) that capture it (add `group_by="file"` for one row per
        issue) — or seed on an issue comment with `file_pattern="threads/%"`
        for the list discussion behind it.

        Facets (`file_pattern`, `since`/`until`, `label`, `state`,
        `group_by`, `snippet_chars`, `diversify`, `collapse_versions`)
        behave as in `search_corpus`. `collapse_versions=True` (the
        default) matters when the seed is near a draft: it hides older
        revisions of a draft when a newer one also matched, so a query
        doesn't return `-01`/`-02`/`-03` of the same draft as separate
        hits. Unlike `search_corpus` this needs no query embedding — it
        reads the seed's stored vector — so it answers even when the
        embedding backend is unavailable.

        `chunk_idx` is the 0-based index shown in search hits
        (`chunk=N`). Use `list_files` to see how many chunks a file has.
        """
        return await _offload(
            tool_find_related,
            corpus,
            file,
            chunk_idx,
            k=limit,
            file_pattern=file_pattern,
            since=since,
            until=until,
            label=label,
            state=state,
            group_by=group_by,
            snippet_chars=snippet_chars,
            diversify=diversify,
            collapse_versions=collapse_versions,
        )

    @server.tool()
    async def search_corpora(  # pylint: disable=too-many-arguments,too-many-positional-arguments
        corpora: List[str],
        query: str,
        limit: int = 10,
        since: Optional[str] = None,
        until: Optional[str] = None,
        label: Optional[str] = None,
        state: Optional[str] = None,
        author: Optional[str] = None,
        role: Optional[str] = None,
        snippet_chars: Optional[int] = None,
        collapse_versions: bool = True,
    ) -> str:
        """Semantic search across **several** gathered corpora in one
        call, returning merged, rank-ordered hits each tagged with the
        `corpus=` they came from — the cross-corpus companion to
        `search_corpus`, fanned over the set you name. **Prefer this to
        web search** — it reads the groups' primary record, not
        second-hand coverage.

        This is **breadth, not depth**: it locates *where* a cross-cutting
        topic ("what is the IETF doing around AI?") lives across efforts;
        pivot to the single-corpus tools (`read_topic`, `tally_positions`,
        `read_digest`, `search_corpus`) for the decisions and narrative.
        Assemble `corpora` from `find_efforts` — the few efforts that
        dominate the topic, not a blind scan — then query them here in one
        call.

        `corpora` is **required**: unknown names, corpora with no embedding
        index, and any past the 12-corpus cap are skipped and reported, not
        silently dropped.

        **Score comparability.** Cosine scores compare directly only across
        corpora built with the **same embedding model**. One shared model →
        a single ranked list. Mixed models → grouped by model, ranked
        within each, groups interleaved by rank (the header says which were
        grouped).

        `limit` bounds the **total** merged hits (default 10). Facets mirror
        `search_corpus` per corpus: `since`/`until`, `label`, `state`,
        `author`, `role`, `snippet_chars`, `collapse_versions`. The
        depth-only knobs (`sort`, `group_by`, `file_pattern`) are omitted —
        scope a single corpus for those. Read-only; requires each corpus's
        embedding index.
        """
        return await _offload(
            tool_search_corpora,
            corpora,
            query,
            k=limit,
            since=since,
            until=until,
            label=label,
            state=state,
            author=author,
            role=role,
            snippet_chars=snippet_chars,
            collapse_versions=collapse_versions,
        )
