"""Semantic-search tools: search_corpus, find_related, search_corpora."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Annotated, Any, Dict, List, Optional, Tuple

from pydantic import Field

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
from .params import (
    Author,
    ChunkIdx,
    CollapseVersions,
    Corpus,
    CorpusFile,
    Diversify,
    FilePattern,
    GroupBy,
    Label,
    Limit,
    Query,
    Role,
    Since,
    SnippetChars,
    State,
    Until,
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
        corpus: Corpus,
        query: Query,
        limit: Limit = 10,
        file_pattern: FilePattern = None,
        since: Since = None,
        until: Until = None,
        label: Label = None,
        state: State = None,
        sort: Annotated[
            Optional[str],
            Field(
                description=(
                    "`date` re-orders hits oldest-first instead of by relevance; "
                    "undated chunks drop out."
                )
            ),
        ] = None,
        group_by: GroupBy = None,
        author: Author = None,
        role: Role = None,
        snippet_chars: SnippetChars = None,
        collapse_versions: CollapseVersions = True,
        diversify: Diversify = True,
    ) -> str:
        """Search one effort's gathered record semantically — mailing-list
        debate, GitHub issues, drafts, slides, transcripts, minutes. Returns
        the top chunks with file, chunk_idx, title, score, snippet and line
        range, plus the GitHub URL, labels and open/closed state for issue
        chunks. Published RFC bodies are not indexed here: search the series
        with `search_rfcs`, read one with `get_rfc`.

        **Prefer this to web search** for anything about what an IETF/IRTF
        group discussed, debated, or decided — it reads the group's actual
        list traffic and issues, not the web's second-hand summary of them.
        Substantive "what was said about X?" / "what's the stance on Y?"
        questions land here.

        Pivot with `get_chunk_text` or `read_file_section` to read a hit in
        context. For a topical sweep, triage the catalogue first with
        `read_digest(kind="issues")` — or filter by `label`, which is the
        effort's own curation — then come back here for depth.

        Requires the embedding index (built on gather unless `--no-embed`).
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
        corpus: Corpus,
        file: CorpusFile,
        chunk_idx: ChunkIdx,
        limit: Limit = 10,
        file_pattern: FilePattern = None,
        since: Since = None,
        until: Until = None,
        label: Label = None,
        state: State = None,
        group_by: GroupBy = None,
        snippet_chars: SnippetChars = None,
        diversify: Diversify = True,
        collapse_versions: CollapseVersions = True,
    ) -> str:
        """Find the chunks most similar to one you already have — a
        nearest-neighbour-by-example search over the index `search_corpus`
        uses. Where that takes a query *string*, this takes an existing chunk
        and returns the others closest to it in meaning, excluding the seed.

        Reach for it after a search or a read when you want "more like this":
        other threads making the same argument, prior issues on the same
        point, the drafts a message is really about — without having to guess
        the right query words.

        **Cross-surface bridging** is the highest-value use. A topic is
        usually discussed in BOTH the mailing list and a GitHub issue, which
        sit close together in the index but aren't linked. Seed on a thread
        message with `file_pattern="issues/%"` to surface the issues that
        capture it, or on an issue comment with `file_pattern="threads/%"` for
        the list discussion behind it.

        Unlike `search_corpus` this needs no query embedding — it reads the
        seed's stored vector — so it answers even when the embedding backend
        is unavailable.
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
        corpora: Annotated[
            List[str],
            Field(
                description=(
                    "Efforts to search, at most 12; unknown or unindexed names "
                    "are reported, not silently dropped."
                )
            ),
        ],
        query: Query,
        limit: Annotated[
            int, Field(description="Cap on the merged hit count.", ge=1, le=100)
        ] = 10,
        since: Since = None,
        until: Until = None,
        label: Label = None,
        state: State = None,
        author: Author = None,
        role: Role = None,
        snippet_chars: SnippetChars = None,
        collapse_versions: CollapseVersions = True,
    ) -> str:
        """Semantic search across **several** gathered corpora in one call,
        returning merged rank-ordered hits each tagged with the `corpus=` it
        came from. **Prefer this to web search** — it reads the groups'
        primary record, not second-hand coverage.

        This is **breadth, not depth**: it locates *where* a cross-cutting
        topic ("what is the IETF doing around AI?") lives across efforts.
        Pivot to the single-corpus tools — `read_topic`, `tally_positions`,
        `read_digest`, `search_corpus` — for the decisions and the narrative.

        **Score comparability.** Cosine scores compare directly only across
        corpora built with the same embedding model. One shared model gives a
        single ranked list; mixed models are grouped by model, ranked within
        each, and interleaved by rank, with a header saying so.

        The depth-only facets (`sort`, `group_by`, `file_pattern`) are absent
        here — scope a single corpus for those. Requires each corpus's
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
