"""Orientation tools: list_corpora, find_efforts, which_corpus,
overview, list_labels, list_files."""

from __future__ import annotations

import fnmatch
import os
from typing import TYPE_CHECKING, List, Optional

from .. import coverage
from ..singletons.catalog import render_efforts
from ..corpus import describe, kind_status, status_cell
from ..digest.overview import (
    _label_frequencies,
    _subject_prefix_frequencies,
    build_overview,
)
from ..embeddings import chunk_counts
from ..freshness import gather_enabled, gather_suggestion, seed_source
from ..paths import digest_kind_from_relpath
from ..corpus.routing import DEFAULT_MIN_SCORE, route
from ..store.corpus import get_corpus_store
from .common import (
    _DIGEST_KINDS,
    _deployment_phrase,
    _files_dir,
    _gather_brief,
    _list_wgs,
    _offload,
    _requires_corpus,
    _with_freshness,
)

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP  # pragma: no cover


_NEXT_TOOLS_HINT = (
    "\n\n_Next: `overview(wg)` for orientation · "
    "`read_digest(wg, kind=..., ...filters)` for catalogue queries · "
    "`search_corpus(wg, query, ...)` for substantive content · "
    "`list_labels(wg)` for the corpus's curation vocabulary._"
)


def _session_facts_line() -> str:
    """Server-authoritative footer restating this session's deployment and
    capability (the same facts as the `instructions` session block, from the same
    phrase helpers) at the point a client decides whether/how to add a missing
    corpus — because the reports showed clients acting on `list_corpora` output
    without having read the instructions."""
    return f"\n\n_This session — {_deployment_phrase()}; {_gather_brief()}._"


def _corpus_sources(wg: str) -> str:
    """Compact source inventory for `wg` in `list_corpora`, read-only — resolves
    an already-materialised files dir (never forces a cloud download) and
    degrades to empty when the corpus isn't staged locally."""
    cache = get_corpus_store().materialised_cache_dir(wg)
    if cache is None:
        return ""
    return coverage.compact_sources_line(cache)


def _seed_marker(wg: str) -> str:
    """A compact `seeded <date>` provenance suffix for `list_corpora`, or '' if
    the corpus was not reconstituted from the seed store (issue #182)."""
    src = seed_source(wg)
    if not src:
        return ""
    date = str(src.get("gathered") or "")[:10]
    return f"seeded {date}" if date else "seeded"


def _available_to_seed(local: List[str]) -> str:
    """A one-line 'available to fast-start' hint listing seed-store corpora not
    yet gathered locally, or '' when none / no cache.

    Only shown where in-session gather is available (`gather_enabled`) and seeding
    is on — a read-only HTTP replica neither lists nor fetches. Uses the sanctioned
    networked-read exception, stale-while-revalidate: `refresh_mirror` serves the
    cached catalog and revalidates in the background, blocking only on a cold miss
    (nothing cached yet) so a fresh client still sees it. Empty when seeding is
    disabled or the store is unreachable."""
    # pylint: disable-next=import-outside-toplevel
    from ..config import service

    if not gather_enabled() or not service.seeding_enabled():
        return ""
    # pylint: disable-next=import-outside-toplevel
    from ..seed import catalog as seed_catalog

    seed_catalog.refresh_mirror()
    index = seed_catalog.cached_index()
    if index is None:
        return ""
    known = set(local)
    names = sorted(e.name for e in index.corpora if e.name not in known)
    if not names:
        return ""
    return (
        "\n\nAvailable to fast-start from the public seed store (not yet "
        f"gathered): {', '.join(names)}. Each pulls a prebuilt snapshot, so "
        f"gathering one is quick — {gather_suggestion('<name>')}."
    )


def tool_list_corpora() -> str:
    wgs = _list_wgs()
    available = _available_to_seed(wgs)
    if not wgs:
        return f"(no corpora gathered yet — {gather_suggestion('<name>')}){available}"
    rows = []
    for wg in wgs:
        kind, status = kind_status(wg)
        tag = f"{kind} · {status_cell(kind, status)}"
        rows.append((wg, tag, describe(wg), _corpus_sources(wg), _seed_marker(wg)))
    name_w = max(len(w) for w, _, _, _, _ in rows)
    tag_w = max(len(t) for _, t, _, _, _ in rows)
    lines = []
    for wg, tag, subject, sources, seed in rows:
        line = f"{wg.ljust(name_w)}  {tag.ljust(tag_w)}"
        if subject:
            line += f"  {subject}"
        if sources:
            line += f"  ({sources})"
        if seed:
            line += f"  · {seed}"
        lines.append(line.rstrip())
    return (
        "Gathered corpora (name · kind [· status] · what it's about · "
        "(sources)). **kind** is `group` (a WG/RG/edwg/BoF — accepts every "
        "tool), `list` (a mailing list gathered on its own), `custom` "
        "(explicit drafts/repos or a followed author), or `synthetic` (an "
        "`x-` corpus). **status** is the group state (`active` / `concluded` "
        "/ `bof` / …) for a `group`; a `list`, `custom`, or `synthetic` "
        "corpus is **not a chartered IETF effort**, and its status says so "
        "explicitly — a corpus existing here implies nothing about IETF "
        "standing, and an `x-` bundle or a standalone list is not a Working "
        "Group. The text after that is the corpus's "
        "subject — the group name, the list followed, the tracked author. "
        "The trailing `(…)` is the source inventory — which of mailing "
        "`list`, GitHub `issues`, `drafts`, `RFCs`, `minutes` are present — "
        "so you can tell what each corpus actually holds. A trailing `· seeded "
        "<date>` marks a corpus that was reconstituted from the public seed "
        "store (a prebuilt snapshot) and then freshened locally. Call "
        "`overview` for the gather window and the exact repos.\n\n"
        + "\n".join(lines)
        + available
        + _NEXT_TOOLS_HINT
        + _session_facts_line()
    )


def _overview_live_reconciliation(wg: str, live: bool) -> str:
    """The optional 'live draft reconciliation' section appended to overview.

    Empty on a read-only (gather-disabled) deployment — the live tools aren't
    available there, so neither is this. When gather is enabled but `live` is
    off, a one-line pointer to the live check (the default stays offline and
    fast). When `live` is on, it cross-checks the cache's active-draft list
    against Datatracker (`live_lookup.reconcile_active_drafts`) and reports any
    divergence, so a stale curated list can't silently mislead an agenda.
    """
    if not gather_enabled():
        return ""
    if not live:
        return (
            "\n\n_The active-draft list above is from the gather cache and can "
            "lag Datatracker. Call `overview(corpus, live=True)` to reconcile "
            "it live, or `draft_status(name)` to check one draft._"
        )

    from .. import live_lookup  # pylint: disable=import-outside-toplevel
    from ..digest.overview import (  # pylint: disable=import-outside-toplevel
        active_draft_names,
    )

    recon, fetched = live_lookup.reconcile_active_drafts(wg, active_draft_names(wg))
    lines = ["\n\n## Live draft reconciliation\n"]
    if not recon.advanced and not recon.revived:
        lines.append(
            f"The {recon.checked} active draft(s) above match Datatracker; no "
            "adopted draft is missing or has advanced past the WG."
        )
    else:
        if recon.advanced:
            lines.append(
                "**Listed active here but past the WG on Datatracker** "
                "(drop from a WG agenda):"
            )
            lines.extend(f"- `{name}` — {state}" for name, state in recon.advanced)
            lines.append("")
        if recon.revived:
            lines.append(
                "**Active adopted drafts on Datatracker missing from the list "
                "above** (a cached snapshot likely expired then revived — "
                "re-gather, and consider for the agenda):"
            )
            lines.extend(
                f"- `{name}` — expires {expires}" for name, expires in recon.revived
            )
            lines.append("")
    lines.append(live_lookup.age_stamp(fetched))
    return "\n".join(lines)


def _gather_notes_available(wg: str) -> bool:
    """Whether pointing an `overview` reader at `gather_status` would tell them
    anything for this corpus.

    Two conditions, both required. The tool has to exist — the read-only HTTP
    replica doesn't register it — and a gather has to have left a record. A
    corpus gathered by the `ietf-llm` CLI has neither, and `gather_status` would
    answer "no gather has been recorded", which reads as an invitation to start
    one; for the case the pointer exists to explain (a stalled upstream feed) a
    re-gather reads the same feed and changes nothing.

    `has_local_status` is the network-free check — see its docstring for why not
    `read_status` — so this stays inside the read path's offline contract.
    """
    if not gather_enabled():
        return False
    # pylint: disable-next=import-outside-toplevel
    from ..gather import runner as gather_runner

    return gather_runner.has_local_status(wg)


@_requires_corpus
def tool_overview(wg: str, live: bool = False) -> str:
    files_dir = _files_dir(wg)
    body = build_overview(wg, files_dir)
    # One full scan (incl. verbatim repo names) reused by both the inventory
    # below and the window line in _with_freshness.
    src = coverage.detect_sources(files_dir)
    inventory = coverage.sources_line(files_dir, sources=src)
    if inventory:
        deeper = (
            f'`start_gather(corpus="{wg}", months=N)`'
            if gather_enabled()
            else f"`ietf-llm {wg} --months N`"
        )
        # A source absent from the inventory gathered nothing — which is not the
        # same as the effort not having one. The gather records *why* per source
        # (an upstream feed that lags its archive is the common cause), so point
        # at that rather than let a reader conclude the WG doesn't use its list.
        missing = (
            " A source **not** listed gathered nothing — which isn't the same "
            "as the effort not having one; "
            f'`gather_status(corpus="{wg}")` notes say which and why.'
            if _gather_notes_available(wg)
            else ""
        )
        body += (
            "\n\n## Coverage\n\n"
            f"**Sources:** {inventory}.\n\n"
            "_GitHub issues and drafts are the full set, not limited by the "
            "gather window. For activity older than the window above, "
            f"re-gather deeper with {deeper} — don't read absence as proof it "
            f"didn't happen.{missing}_"
        )
    body += _overview_live_reconciliation(wg, live)
    return _with_freshness(wg, body, sources=src)


@_requires_corpus
def tool_list_labels(wg: str) -> str:
    """The corpus's curation vocabulary — GitHub issue labels AND mailing-
    list subject-prefix clusters — with their frequencies, sorted by
    count descending.

    Two sources because two WG-management styles exist: issue-driven
    groups (httpbis, aipref) tag with GitHub labels; mail-driven
    groups (TLS, with `[mlkem]` / `[ech]`) cluster on the list. The
    consumer doesn't have to know which the WG uses — both render.
    """
    cache = _files_dir(wg)
    labels = _label_frequencies(cache, wg)
    prefixes = _subject_prefix_frequencies(cache)
    if not labels and not prefixes:
        return _with_freshness(
            wg,
            f"No curation vocabulary recorded for {wg}. "
            "(No GitHub issue labels AND no `[xxx]`-style subject "
            "prefixes seen in mailing list traffic.)",
        )
    lines: List[str] = [f"# {wg}: curation vocabulary\n"]
    if labels:
        lines.append(f"## GitHub issue labels ({len(labels)} distinct)\n")
        lines.append("| Label | Issues |")
        lines.append("|-------|--------|")
        for label, count in labels:
            lines.append(f"| `{label}` | {count} |")
        lines.append("")
        lines.append(
            f'_Use with `read_digest("{wg}", kind="issues", '
            'label="X", include_bodies=True)` or '
            f'`search_corpus("{wg}", "...", label="X")`._'
        )
        lines.append("")
    if prefixes:
        lines.append(
            f"## Mailing list subject prefixes ({len(prefixes)} " "distinct)\n"
        )
        lines.append("| Prefix | Messages |")
        lines.append("|--------|----------|")
        for prefix, count in prefixes:
            lines.append(f"| `{prefix}` | {count} |")
        lines.append("")
        example_prefix = prefixes[0][0]
        lines.append(
            f'_Use with `read_digest("{wg}", kind="threads", '
            f'subject="{example_prefix}")` to read every thread carrying '
            "the prefix, or with subject in `search_corpus` `file_pattern`."
            "_"
        )
        lines.append("")
    return _with_freshness(wg, "\n".join(lines))


@_requires_corpus
def tool_list_files(wg: str, pattern: Optional[str] = None) -> str:
    cache = _files_dir(wg)
    if not os.path.isdir(cache):
        return f"No cache for {wg}."
    # chunk_counts() is cheap (one GROUP BY) and lets the consumer bound
    # get_chunk_text calls instead of blind-probing chunk_idx=0,1,2,…
    counts = chunk_counts(wg)
    # If the embedding DB has no chunks at all, the index hasn't been
    # built yet — distinguish that from "this file genuinely has no
    # indexable content" so the consumer isn't misled into thinking
    # there's nothing to search.
    index_built = bool(counts)
    # `pattern` is a glob over the relative path. Lets a consumer ask
    # for `threads/*mlkem*` or `meetings/ietf125/*` instead of grepping
    # a 600-line inventory dump. Glob is matched against the relpath
    # (so `threads/*` works), with fnmatch semantics.
    entries = []
    for dirpath, _dirnames, filenames in os.walk(cache):
        for name in filenames:
            path = os.path.join(dirpath, name)
            if not os.path.isfile(path):
                continue
            relpath = os.path.relpath(path, cache)
            if pattern is not None and not fnmatch.fnmatch(relpath, pattern):
                continue
            entries.append((relpath, path))
    entries.sort(key=lambda kv: kv[0])
    if pattern is not None and not entries:
        return _with_freshness(
            wg,
            f"(no files match `{pattern}`. Try a broader glob, e.g. "
            "`threads/*` or `*mlkem*`.)",
        )
    rows = []
    for relpath, path in entries:
        size = os.path.getsize(path)
        n_chunks = counts.get(relpath)
        kind = digest_kind_from_relpath(relpath)
        if n_chunks is not None:
            rows.append(f"{size:>10}  chunks={n_chunks:<4}  {relpath}")
        elif kind is not None and kind in _DIGEST_KINDS:
            # Digests are intentionally NOT chunked; flag them so
            # consumers know to use read_digest, not get_chunk_text.
            rows.append(
                f"{size:>10}  (digest)     {relpath}  "
                f"-> read_digest(wg, kind='{kind}')"
            )
        else:
            # "not indexed" when the DB itself is empty (build hasn't
            # run yet); "no chunks" for the rare case of an indexed
            # corpus where this specific file produced zero chunks.
            tag = "(not indexed)" if not index_built else "(no chunks)"
            rows.append(f"{size:>10}  {tag}  {relpath}")
    body = "\n".join(rows) or "(empty)"
    body += (
        f'\n\n_Next: `read_file_section("{wg}", "<filename>", '
        "start_line=1)` for a bounded read · "
        f'`get_chunk_text("{wg}", "<filename>", chunk_idx, end_chunk_idx)` '
        "for one (or a range of) indexed chunks._"
    )
    return _with_freshness(wg, body)


def tool_which_corpus(query: str, limit: int = 8) -> str:
    clean = (query or "").strip()
    if not clean:
        return (
            "which_corpus needs a question or topic to route, e.g. "
            '`which_corpus("0-RTT replay protection")`.'
        )
    result = route(clean, limit=limit)
    if result.error == "embed-failed":
        return (
            f"Could not embed the query with model `{result.model_id}` to route it. "
            "The embedding backend may be unavailable; try `find_efforts` "
            "(keyword-based) instead."
        )
    if (
        not result.matches
        and not result.skipped_other_model
        and not result.no_centroids
    ):
        return (
            "No gathered corpus has a topic map yet — routing centroids populate "
            "at gather time. Re-gather a corpus (`ietf-llm <name>`), then retry. "
            "Meanwhile `find_efforts(topic)` discovers efforts to gather and "
            "`list_corpora` shows what is cached."
        )

    lines: List[str] = []
    if result.confident:
        lines.append(
            f"**Which corpus** for {clean!r} — gathered corpora ranked by "
            f"topic-centroid similarity (embedding model `{result.model_id}`):"
        )
        lines.append("")
        for i, match in enumerate(result.matches, 1):
            weak = "" if match.score >= DEFAULT_MIN_SCORE else "  _(below floor)_"
            lines.append(f"{i}. **{match.corpus}** — {match.score:.3f}{weak}")
        top = result.matches[0].corpus
        lines.append("")
        lines.append(
            f'Search the best fit with `search_corpus("{top}", "...")`, or compare '
            'a few with `search_corpora([...], "...")`. These are routing hints '
            "(topic proximity), not proof the answer is there — confirm by searching."
        )
    else:
        closest = result.matches[0] if result.matches else None
        if closest is not None:
            lines.append(
                f"No confident match for {clean!r} among gathered corpora "
                f"(closest: **{closest.corpus}** {closest.score:.3f}, below the "
                f"{DEFAULT_MIN_SCORE:.2f} confidence floor)."
            )
        else:
            lines.append(f"No confident match for {clean!r} among gathered corpora.")
        lines.append("")
        lines.append(
            "The right effort may not be gathered yet — try "
            f"`find_efforts({clean!r})` to discover candidates and gather one, or "
            "the question may be off-topic for what is cached. Don't force a search "
            "against a low-confidence guess."
        )
        if result.matches:
            lines.append("")
            lines.append("_Closest (weak) matches:_")
            for match in result.matches:
                lines.append(f"- {match.corpus} — {match.score:.3f}")

    if result.skipped_other_model:
        lines.append(
            f"\n_Scored only corpora on the majority embedding model "
            f"`{result.model_id}`; not comparable, so skipped: "
            f"{', '.join(result.skipped_other_model)}._"
        )
    if result.no_centroids:
        lines.append(
            "\n_No topic map yet (re-gather to include in routing): "
            f"{', '.join(result.no_centroids)}._"
        )
    return "\n".join(lines)


def register(server: "FastMCP") -> None:
    @server.tool()
    async def list_corpora() -> str:
        """List the IETF/IRTF efforts gathered locally by ietf-llm —
        working groups, research groups, mailing lists, and draft sets —
        each tagged with its **kind** and **status**. **Call this first**
        (a cheap orientation step) before answering any question about
        IETF/IRTF work, and whenever you don't know which `corpus` the
        user means — it is how you discover that the purpose-built corpus
        tools apply instead of falling back to web search.

        A corpus is whatever someone gathered. Most are IETF Working
        Groups / IRTF Research Groups by shortname (`httpbis`, `cfrg`,
        …), but a corpus can also be a standalone mailing list (`list`,
        e.g. `last-call`), an explicit draft/repo set (`custom`), or a
        synthetic `x-` corpus. **Every tool here takes any kind** — the
        `corpus` argument is the corpus name, not specifically a WG.
        `status` flags group state (`active` / `concluded` / `bof`), so
        you can tell a wound-down WG or finished BoF at a glance. Each row
        also carries the corpus's **subject** — the group's name, the
        mailing list it follows, or the author it tracks — and a trailing
        `(…)` **source inventory** (which of mailing `list`, GitHub
        `issues`, `drafts`, `RFCs`, `minutes` are present) — so you can see
        what a corpus covers, and whether GitHub issues were gathered at
        all, without opening it. Call `overview` for the gather window and
        the exact repos.
        """
        return await _offload(tool_list_corpora)

    @server.tool()
    async def find_efforts(query: str, limit: int = 15) -> str:
        """Find active IETF/IRTF efforts by **topic** — the entry point
        for "what is the IETF doing around X?" when no working group is
        named. Returns a ranked markdown list of working/research groups,
        each tagged with whether it is **already gathered here** (`✓
        cached`); prefer those.

        Each row carries the effort's Datatracker **state**: a `bof` row,
        shown as **BoF — pre-WG, not chartered**, is *not* a Working Group —
        don't read it (or a stray agenda / draft) as one.

        This is the topic→effort discovery step the corpus-first tools
        lack. Reach here when the user gives a *subject* with no obvious
        home — "AI", "post-quantum", "congestion control", "email
        security" — instead of guessing a corpus or crawling Datatracker /
        the web. It ranks over the official Datatracker group list
        (acronym + name + charter description), mirrored locally; it covers
        **active** and **BoF** groups only, so a concluded effort or
        published work won't surface here — use `search_rfcs` for the RFC
        series, and `list_corpora` to see what is already cached.

        The playbook: `find_efforts(topic)` → present the candidates
        (prefer the cached ones) → gather the **few** efforts that
        dominate the topic (how to add one is in **This session**), not
        all of them, and tell the user what you skipped → query each
        gathered corpus → synthesize. Over-gathering is the failure mode
        to avoid — it is slow and wasteful.

        `limit` caps results (default 15).
        """
        return await _offload(render_efforts, query, limit)

    @server.tool()
    async def which_corpus(query: str, limit: int = 8) -> str:
        """Route a question to the **already-gathered** corpus it belongs to,
        when the user gives a topic but names no working group. Embeds the
        question and ranks gathered corpora by similarity to their topic-map
        centroids; returns the ranked names with scores, or **abstains** when
        nothing is close.

        This is the "which corpus did they mean" step. It is distinct from
        `find_efforts`, and the two answer different questions:
          - `which_corpus` ranks what is **already cached here**, by your
            actual gathered content — use it to pick the corpus for a question
            like "where is 0-RTT replay discussed?" without naming one.
          - `find_efforts` ranks the **Datatracker catalog** (mostly
            *un-gathered* efforts) to decide what to gather next.
        When unsure which to reach for: have a question about a topic that is
        probably already gathered → `which_corpus`; exploring what the IETF is
        doing about a subject you may not have gathered → `find_efforts`.

        It is a **router, not a searcher**: it does not read content. Follow a
        confident result with `search_corpus(corpus, query, ...)` (or
        `search_corpora` to compare a few) — the score is topic proximity, not
        proof the answer is there. When it abstains (top score below the
        confidence floor), fall back to `find_efforts` rather than forcing a
        search against a low-confidence guess. Corpora gathered before the
        topic map shipped have no centroids and are reported as such until
        re-gathered. `limit` caps results (default 8).
        """
        return await _offload(tool_which_corpus, query, limit)

    @server.tool()
    async def overview(corpus: str, live: bool = False) -> str:
        """**Prefer this to web search to orient on an IETF/IRTF effort** — a
        working group, research group, BoF, mailing list, or draft set — in
        one call: chairs/ADs, active drafts, main discussion themes, top open
        issues, recent mailing list threads, latest meeting and latest draft
        publication.

        The **main discussion themes** are topical clusters of the gathered
        record (computed at gather time from the embedding index); each
        names what the group keeps coming back to, with how much traffic and
        when it was last active. Themes that recur across many gathered
        corpora (meeting logistics, ballots) are demoted below the distinctive
        ones and tagged _common across WGs_. Search a theme's wording with
        `search_corpus`. (Absent on a corpus gathered before this shipped —
        re-gather to populate.)

        **Read this only once the gather is `done`.** These sections — the
        themes especially, built from the embedding index in the gather's
        *final* stage — are complete only after `gather_status` reports `done`.
        A *first* gather **refuses** the read (no prior snapshot to serve); a
        re-gather serves the previous, *stale* one. If you just called
        `start_gather`, don't reason "overview only needs structural facts, so I
        can read it mid-gather" — the whole cache is still being written; wait
        for `done`.

        **Call this first** (alongside `list_corpora`) to orient before
        answering — and **prefer it to web search** — for ORIENTING /
        STRUCTURAL questions about an IETF WG, IRTF RG, or other corpus by
        shortname (`httpbis`, `quic`, `tls`, `aipref`, `cfrg`, `hrpc`, …):
        "what's happening in X?", "tell me about X", "what's X up to?",
        "who's on X?", "what is X working on?". The corpus is the gathered
        primary record; web search only sees second-hand coverage. ~30
        lines of markdown instead of the 80-100 KB of context that reading
        every digest would burn.

        **Skip overview and go straight to the specialised tool for
        TOPICAL questions:**
          - "arguments for/against X" / "scope debate about X" →
            `read_digest(corpus, kind="issues", label="...",
            include_bodies=True)` — the issue catalogue plus each
            opening description in one call beats semantic search for
            coverage (`list_labels` first if you don't know the labels).
          - "what did the WG decide about X?" / "what's the WG's
            position on X?" → the outcome is whatever the chairs
            declared, so go to their words: `search_corpus(corpus, "X",
            role="Chair")` and `tally_positions(corpus, "<thread or
            issue file>")`. This corpus does not compute consensus.
          - "what's open?" / "who chairs this?" / "what happened in
            May?" → `read_digest(corpus, kind=..., ...filters)`.
          - "what did Alice say about X?" → `search_corpus` (semantic
            search, then pivot via `get_chunk_text` or
            `read_file_section`).
          - "how did the debate on X evolve?" / "walk me through the
            discussion of Y, chronologically" → `read_topic(corpus, "X")`.
            Returns full messages (not snippets) across threads and
            issues in date order; add `include_replies=True` for
            sub-thread descendants.

        Ends with a **## Coverage** section: which sources the corpus
        holds (mailing list, GitHub issues — by repo — drafts, RFCs,
        minutes) and, in the leading `Coverage:` line, how far back the
        windowed sources reach. The window bounds *mailing-list and
        meeting* recency only (default 12 months); issues and drafts are
        the full set. If the user asks about list/meeting activity older
        than the window, re-gather deeper rather than reporting nothing.

        Other ietf-llm tools: `read_digest`, `search_corpus`,
        `read_topic`, `get_chunk_text`, `read_file_section`,
        `list_files`, `list_labels`.

        **Collective-outcome claims are gated:** before asserting that
        something is settled / decided / agreed / rejected, that there is
        consensus, or what "the WG wants" (vs reporting what a *named
        individual* said, which is free), you must have called
        `read_ietf_interpretation_norms` this session — see it for the full
        rule. Write side (drafting a contribution):
        `read_ietf_participation_norms`.

        `live=True` appends a **## Live draft reconciliation** section that
        cross-checks the (cache-derived) active-draft list against Datatracker
        and flags divergence — a draft listed active here that has actually
        advanced past the WG (drop from an agenda), or an adopted draft active
        on Datatracker that the cached list omits (a revived draft to re-gather
        and consider). Use it when building an agenda or whenever the
        active-draft list must be exactly right; it hits Datatracker live (so
        it is only available where the live tools are enabled — see **This
        session**) and is slower than the default offline overview.

        Args:
            corpus: The corpus shortname (`httpbis`, `tls`, …).
            live: Reconcile the active-draft list against live Datatracker.
        """
        return await _offload(tool_overview, corpus, live)

    @server.tool()
    async def list_labels(corpus: str) -> str:
        """List the corpus's curation vocabulary — GitHub issue labels
        AND mailing-list `[xxx]`-style subject prefixes — with
        frequencies. Call this before picking a `label=` filter for
        `read_digest` / `search_corpus`, or a `subject="[xxx]"`
        filter for `read_digest(kind="threads")`.

        Two sections because IETF WGs split by management style:
        - **GitHub issue labels** — used by issue-driven groups
          (`httpbis`, `aipref`).
        - **Mailing list subject prefixes** — used by mail-driven
          groups (`tls` with `[mlkem]` / `[ech]`).

        A WG may have one, the other, or both. The empty case
        (neither) is rare and gets a clear "no vocabulary" message.
        """
        return await _offload(tool_list_labels, corpus)

    @server.tool()
    async def list_files(corpus: str, pattern: Optional[str] = None) -> str:
        """Inventory a corpus's ietf-llm cache: files with
        sizes and chunk counts.

        `pattern` is an optional glob over the relative path (fnmatch
        semantics), e.g. `"threads/*mlkem*"`, `"meetings/ietf125/*"`,
        `"issues/*/155.md"`. Use it instead of dumping the whole
        inventory when you already know roughly what you're after — a
        long-running corpus can have 1000+ files.

        `(digest)` rows are the per-corpus summary digests — read them via
        `read_digest`, not `get_chunk_text`.
        """
        return await _offload(tool_list_files, corpus, pattern=pattern)
