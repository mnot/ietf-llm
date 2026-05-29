import os
import re
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import requests

from ..paths import (
    ORPHAN_MEETING_CODE,
    agenda_path,
    meeting_dir,
    meetings_dir,
    minutes_path,
    slides_dir,
)
from ..utils import (
    DEFAULT_HEADERS,
    LogLevel,
    Verbosity,
    clean_html,
    fetch_resource,
    format_filename,
    log,
    write_if_changed,
)
from .datatracker import _get_json
from .session_polls import process_session_polls


@dataclass
class MeetingCluster:
    """One logical meeting, possibly spanning several Datatracker rows.

    Datatracker lists each interim *session* as its own row. A
    multi-day interim event (or several sessions on one day) is one
    meeting; we cluster contiguous-date interim rows into a single
    `MeetingCluster` with one canonical `code` (the earliest
    session's) and a date span. Numbered IETF meetings are never
    clustered — each is already one event, in its own singleton
    cluster.

    `start` / `end` are date-level (time stripped) so transcript
    matching can ask "does this transcript's date fall in the span?".
    """

    code: str  # canonical safe-code, e.g. "interim2026aipref05"
    start: datetime
    end: datetime
    sessions: List[Dict[str, Any]] = field(default_factory=list)

    def covers(self, when: datetime) -> bool:
        """True if `when`'s date falls within [start, end] inclusive."""
        return self.start.date() <= when.date() <= self.end.date()


def _safe_meeting_code(number: str) -> str:
    """Datatracker meeting number → filesystem-safe code.
    `IETF 125` → `ietf125`; `interim-2026-aipref-05` →
    `interim2026aipref05`."""
    return format_filename(number).replace("_", "").replace("-", "")


def _is_interim(number: str) -> bool:
    return "interim" in number.lower()


# Datatracker doc-name prefixes we ingest as meeting materials. Other
# prefixes (recording, chatlog, bluesheets) are skipped; polls are
# gathered separately via the polls doctype (see session_polls).
_MATERIAL_KINDS = ("minutes", "agenda", "slides")

_SESSION_API = (
    "https://datatracker.ietf.org/api/v1/meeting/session/"
    "?group__acronym={wg}&limit=100"
)


def _material_kind(docname: str) -> Optional[str]:
    """Map a Datatracker material doc name to the kind we ingest, or None.

    Doc names are `<kind>-<meeting>-<wg>[-…]`, so the leading token is
    the material kind (`agenda-125-httpbis`, `slides-125-httpbis-…`)."""
    head = docname.split("-", 1)[0].lower()
    return head if head in _MATERIAL_KINDS else None


def _material_url(meeting_number: str, docname: str) -> str:
    """Direct content URL for a material doc. Resolves to the latest
    rendered file (markdown / text / PDF) — no revision or extension
    needed, Datatracker redirects to the current version."""
    return (
        "https://datatracker.ietf.org/meeting/"
        f"{meeting_number}/materials/{docname}"
    )


def _meeting_meta(
    meeting_uri: str, cache: Dict[str, Tuple[Optional[str], str, str]]
) -> Tuple[Optional[str], str, str]:
    """Resolve a meeting URI to `(display_number, raw_number, date)`.

    `raw_number` ("125" / "interim-2026-httpbis-01") builds content
    URLs; `display_number` ("IETF 125" / the raw interim) is what
    `_safe_meeting_code` / `_is_interim` / `_parse_meeting_date`
    consume downstream. Cached so a multi-session meeting resolves once.
    """
    if meeting_uri in cache:
        return cache[meeting_uri]
    body = _get_json(meeting_uri) or {}
    number = body.get("number")
    if number is None:
        result: Tuple[Optional[str], str, str] = (None, "", "")
    else:
        raw = str(number)
        mtype = (body.get("type") or "").rstrip("/")
        display = f"IETF {raw}" if mtype.endswith("/ietf") else raw
        result = (display, raw, str(body.get("date") or ""))
    cache[meeting_uri] = result
    return result


def get_meeting_links(
    wg_name: str, verbose: Verbosity = Verbosity.STATUS
) -> List[Dict[str, Any]]:
    """List a WG's meetings and their materials via the Datatracker API.

    Walks `/api/v1/meeting/session/?group__acronym=<wg>`, resolves each
    session's meeting (number, date) and materials (agenda / minutes /
    slides), and returns one entry per meeting:

        {"number": "IETF 125" | "interim-…",
         "date": "YYYY-MM-DD",
         "links": [{"type": "minutes"|"agenda"|"slides", "url": <content>}]}

    Content URLs are built from the document name and resolve to the
    latest rendered file — no HTML scraping. Meetings are merged across
    their sessions so a multi-session meeting is one entry.
    """
    log(
        f"Fetching meetings for {wg_name} via Datatracker API...",
        verbose,
        level=LogLevel.STATUS,
    )
    meetings_by_num: Dict[str, Dict[str, Any]] = {}
    meeting_cache: Dict[str, Tuple[Optional[str], str, str]] = {}
    path: Optional[str] = _SESSION_API.format(wg=wg_name)
    while path:
        body = _get_json(path)
        if not body:
            break
        for sess in body.get("objects") or []:
            meeting_uri = sess.get("meeting")
            if not meeting_uri:
                continue
            display, raw, date_str = _meeting_meta(meeting_uri, meeting_cache)
            if not display:
                continue
            entry = meetings_by_num.setdefault(
                display, {"number": display, "date": date_str, "links": []}
            )
            for muri in sess.get("materials") or []:
                docname = str(muri).rstrip("/").rsplit("/", maxsplit=1)[-1]
                kind = _material_kind(docname)
                if kind is None:
                    continue
                entry["links"].append(
                    {"type": kind, "url": _material_url(raw, docname)}
                )
        path = (body.get("meta") or {}).get("next") or None

    return [m for m in meetings_by_num.values() if m["links"]]


def cluster_meetings(meetings: List[Dict[str, Any]]) -> List[MeetingCluster]:
    """Group Datatracker meeting rows into logical meetings.

    Numbered IETF meetings (and interims with no parseable date)
    become singleton clusters keyed by their Datatracker number.
    Interims with dates are sorted and greedily merged whenever
    consecutive dates are ≤ 1 day apart, so a multi-day interim — or
    several sessions on one day — collapses into one cluster. Each
    dated-interim cluster is keyed by its **start date**
    (`interim<YYYYMMDD>`), which is stable and meaningful regardless
    of how Datatracker numbers the underlying sessions. (The WG is
    implicit in the cache path, so it's not repeated in the code.)
    """
    singletons: List[MeetingCluster] = []
    dated_interims: List[tuple[Dict[str, Any], datetime]] = []
    for meeting in meetings:
        when = _parse_meeting_date(meeting["date"], meeting["number"])
        if _is_interim(meeting["number"]) and when is not None:
            dated_interims.append((meeting, when))
        else:
            # Numbered IETF meeting, or an interim we couldn't date —
            # either way it stands alone. Use `now` as a placeholder
            # span for the undated case; it won't match transcripts.
            span = when or datetime.now()
            singletons.append(
                MeetingCluster(
                    code=_safe_meeting_code(meeting["number"]),
                    start=span,
                    end=span,
                    sessions=[meeting],
                )
            )

    # Sort by date, then number for a deterministic earliest-first
    # order, then sweep merging contiguous (≤ 1 day) neighbours.
    dated_interims.sort(key=lambda md: (md[1], md[0]["number"]))
    clustered: List[MeetingCluster] = []
    run: List[tuple[Dict[str, Any], datetime]] = []
    for meeting, when in dated_interims:
        if run and (when.date() - run[-1][1].date()).days <= 1:
            run.append((meeting, when))
        else:
            if run:
                clustered.append(_cluster_from_run(run))
            run = [(meeting, when)]
    if run:
        clustered.append(_cluster_from_run(run))

    return singletons + clustered


def _cluster_from_run(
    run: List[tuple[Dict[str, Any], datetime]],
) -> MeetingCluster:
    sessions = [m for m, _ in run]
    dates = [d for _, d in run]
    start = min(dates)
    # Key the clustered interim by its start date — stable and
    # meaningful, vs. the arbitrary Datatracker session sequence.
    # WG is implicit in the cache path, so it's not in the code.
    code = _safe_meeting_code(f"interim-{start.strftime('%Y%m%d')}")
    return MeetingCluster(
        code=code,
        start=start,
        end=max(dates),
        sessions=sessions,
    )


def _collision_free_path(path: str) -> str:
    """Return `path`, or `path` with a `-2`/`-3`/… suffix before the
    extension if it already exists. Used when absorbing one meeting
    dir's files into another so a same-named slide isn't clobbered."""
    if not os.path.exists(path):
        return path
    root, ext = os.path.splitext(path)
    idx = 2
    while os.path.exists(f"{root}-{idx}{ext}"):
        idx += 1
    return f"{root}-{idx}{ext}"


def _absorb_meeting_dir(src_dir: str, dst_dir: str) -> None:
    """Move slides / polls / transcripts from `src_dir` into `dst_dir`
    (collision-renaming), then remove `src_dir`.

    Migration path: a prior gather wrote each interim session to its
    own dir (`interim…06/`, `…07/`); clustering now folds them into
    the canonical dir. Moving (not re-downloading) keeps it lossless
    even when the canonical minutes already exist and the network
    re-crawl is skipped.
    """
    if not os.path.isdir(src_dir) or os.path.realpath(src_dir) == os.path.realpath(
        dst_dir
    ):
        return
    for sub in ("slides", "polls", "transcripts"):
        src_sub = os.path.join(src_dir, sub)
        if not os.path.isdir(src_sub):
            continue
        dst_sub = os.path.join(dst_dir, sub)
        os.makedirs(dst_sub, exist_ok=True)
        for name in os.listdir(src_sub):
            shutil.move(
                os.path.join(src_sub, name),
                _collision_free_path(os.path.join(dst_sub, name)),
            )
    shutil.rmtree(src_dir, ignore_errors=True)


def process_meetings(
    wg_name: str,
    destination: str,
    verbose: Verbosity = Verbosity.STATUS,
    months: Optional[int] = None,
) -> List[MeetingCluster]:
    """Fetch meeting minutes and materials and write to destination.

    Returns the meeting clusters (so the caller can hand date-spans
    to `process_transcripts` for matching interim transcripts).
    """
    meetings = get_meeting_links(wg_name, verbose)
    if not meetings:
        log(
            f"No meeting materials found for {wg_name}.", verbose, level=LogLevel.STATUS
        )
        return []

    # Filter meetings by date if months is specified.
    if months is not None:
        cutoff_date = datetime.now() - timedelta(days=months * 30)
        filtered = []
        for meeting in meetings:
            m_date = _parse_meeting_date(meeting["date"], meeting["number"])
            if m_date and m_date >= cutoff_date:
                filtered.append(meeting)
        meetings = filtered

    clusters = cluster_meetings(meetings)
    canonical_codes = {c.code for c in clusters}

    for cluster in clusters:
        _process_cluster(cluster, wg_name, destination, verbose)

    # Session polls are their own Datatracker doctype, gathered directly
    # by group rather than per-meeting (they don't ride the materials
    # walk any more).
    process_session_polls(wg_name, destination, verbose)

    # Migration / cleanup: an interim dir from a previous (un-clustered)
    # gather whose code was absorbed into a canonical cluster is folded
    # in and removed. Numbered-meeting and orphan dirs are left alone.
    _cleanup_absorbed_dirs(destination, clusters, canonical_codes)

    log(
        f"Done! {len(clusters)} meeting(s) processed into {destination}.",
        verbose,
        level=LogLevel.STATUS,
    )
    return clusters


def _process_cluster(
    cluster: MeetingCluster,
    wg_name: str,
    destination: str,
    verbose: Verbosity,
) -> None:
    """Download all sessions' materials into the cluster's canonical
    dir and write a combined minutes.md."""
    code = cluster.code
    canonical = meeting_dir(destination, code)
    os.makedirs(canonical, exist_ok=True)

    # Fold in any pre-existing per-session dirs (migration from the
    # old one-dir-per-row layout) BEFORE the skip check, so absorbed
    # materials survive even when we skip the re-crawl.
    for session in cluster.sessions[1:]:
        _absorb_meeting_dir(
            meeting_dir(destination, _safe_meeting_code(session["number"])),
            canonical,
        )

    output_file = minutes_path(destination, code)
    if os.path.exists(output_file):
        # Already have this cluster's minutes — skip the re-crawl
        # (matches the historical per-meeting skip). Per-file PDF
        # existence checks still make slides-only clusters incremental.
        log(
            f"Skipping meeting {code}: minutes already present.",
            verbose,
            level=LogLevel.PROGRESS,
        )
        return

    span = (
        cluster.start.strftime("%Y-%m-%d")
        if cluster.start.date() == cluster.end.date()
        else f"{cluster.start.strftime('%Y-%m-%d')} → {cluster.end.strftime('%Y-%m-%d')}"
    )

    def _header(title: str) -> List[str]:
        return [
            f"# {title}: {code} ({wg_name})\n",
            f"Date: {span}\n",
            f"Sessions: {len(cluster.sessions)}\n\n",
        ]

    # "Meeting Materials" is the historical minutes-file title; keep it
    # verbatim so existing minutes.md files don't churn / re-embed.
    minutes_parts: List[str] = _header("Meeting Materials")
    agenda_parts: List[str] = _header("Meeting Agenda")
    slides_out = slides_dir(destination, code)
    for session in cluster.sessions:
        session_date = session.get("date", "")
        for link in session["links"]:
            kind = link["type"]
            if kind == "slides":
                _download_slide(link["url"], slides_out, verbose)
                continue
            # Minutes / agenda text, headed per-session so multi-session
            # clusters stay legible (same-day sessions get the number to
            # disambiguate the date).
            content = _fetch_text(link["url"], verbose)
            if content:
                bucket = minutes_parts if kind == "minutes" else agenda_parts
                bucket.append(f"## Session {session_date} ({session['number']})\n")
                bucket.append(f"URL: {link['url']}\n\n")
                bucket.append(content.strip() + "\n\n---\n\n")

    # Only write a file if a session actually contributed content (the
    # header alone is ~3 short lines); otherwise this is a slides-only
    # cluster, or the meeting has no agenda / no minutes yet, and we
    # leave that file absent.
    for path, parts in (
        (output_file, minutes_parts),
        (agenda_path(destination, code), agenda_parts),
    ):
        text = "".join(parts)
        if len(text.strip()) > 150 and write_if_changed(path, text):
            log(f"Wrote {path}", verbose, level=LogLevel.PROGRESS)


def _cleanup_absorbed_dirs(
    destination: str,
    clusters: List[MeetingCluster],
    canonical_codes: "set[str]",
) -> None:
    """Remove interim meeting dirs that were absorbed into a cluster
    (any prior-gather interim dir whose code isn't a canonical code).
    """
    absorbed = {
        _safe_meeting_code(s["number"]) for c in clusters for s in c.sessions
    } - canonical_codes
    root = meetings_dir(destination)
    if not os.path.isdir(root):
        return
    for name in os.listdir(root):
        full = os.path.join(root, name)
        if not os.path.isdir(full) or name == ORPHAN_MEETING_CODE:
            continue
        if name in absorbed:
            shutil.rmtree(full, ignore_errors=True)


def _download_slide(url: str, out_dir: str, verbose: Verbosity) -> bool:
    """Download one slide deck (a material content URL that resolves to
    a PDF) into the meeting's slides/ subdir. The doc name is the
    filename; existing files are left alone so re-gathers are cheap."""
    os.makedirs(out_dir, exist_ok=True)
    base = url.rstrip("/").rsplit("/", maxsplit=1)[-1]
    if not base.lower().endswith(".pdf"):
        base += ".pdf"
    pdf_dest = os.path.join(out_dir, base)
    if os.path.exists(pdf_dest):
        return False
    return _download_if_pdf(url, pdf_dest, verbose)


def _download_if_pdf(url: str, dest_path: str, verbose: Verbosity) -> bool:
    """Check head/stream for PDF content type and download."""
    try:
        p_res = requests.get(
            url,
            timeout=60,
            stream=True,
            headers=DEFAULT_HEADERS,
        )
        p_res.raise_for_status()
        c_type = p_res.headers.get("Content-Type", "").lower()
        if "application/pdf" in c_type:
            log(f"Downloading PDF: {dest_path}...", verbose, level=LogLevel.PROGRESS)
            with open(dest_path, "wb") as pdf_fh:
                for chunk in p_res.iter_content(chunk_size=8192):
                    pdf_fh.write(chunk)
            return True
        p_res.close()
    except (requests.RequestException, OSError):
        pass
    return False


def _fetch_text(url: str, verbose: Verbosity) -> Optional[str]:
    """Fetch a minutes / agenda material and return its body as text.

    The materials endpoint serves markdown when we ask for it
    (`Accept: text/markdown`) for recent docs and plain text for some
    older ones — both pass through untouched. Without the header it
    would serve the HTML doc-viewer page; a few older minutes are also
    authored directly in HTML even with the header. We clean any HTML
    response down to readable text so the corpus stays LLM-friendly."""
    log(f"Fetching {url}...", verbose, level=LogLevel.PROGRESS)
    res = fetch_resource(url, headers={"Accept": "text/markdown"})
    if not res:
        return None
    if "text/html" in res.headers.get("Content-Type", "").lower():
        cleaned = clean_html(res.text)
        return cleaned or None
    return str(res.text)


def _parse_meeting_date(date_str: str, meeting_num: str) -> Optional[datetime]:
    """Parse meeting date from string or estimate based on meeting number."""
    if date_str:
        try:
            # Example: 2026-03-20 12:00-14:00 AEDT
            # We only care about YYYY-MM-DD
            ymd = date_str.split(" ")[0]
            return datetime.strptime(ymd, "%Y-%m-%d")
        except (ValueError, IndexError):
            pass

    # Fallback to estimation based on IETF meeting number
    # IETF 125 is March 2026
    # IETF 124 is Nov 2025
    match = re.search(r"IETF\s*(\d+)", meeting_num, re.I)
    if match:
        num = int(match.group(1))
        # Base: IETF 125 = March 2026
        diff = num - 125
        # 3 meetings per year
        # 125: year 2026, month 3
        # 124: year 2025, month 11 (3 - 4 = -1 -> 11)
        # 123: year 2025, month 7 (11 - 4 = 7)
        # 122: year 2025, month 3 (7 - 4 = 3)
        total_months = diff * 4
        year_diff = (3 + total_months - 1) // 12
        new_month = (3 + total_months - 1) % 12 + 1
        return datetime(2026 + year_diff, new_month, 1)

    return None
