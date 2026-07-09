import json
import os
import re
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Set, Tuple

import requests

from ...atomicio import atomic_open_binary, write_if_changed
from ...log import LogLevel, Verbosity, log
from ...net import DEFAULT_HEADERS, clean_html, fetch_resource, governed_get
from ...paths import (
    ORPHAN_MEETING_CODE,
    agenda_path,
    attendance_data_path,
    attendance_path,
    meeting_dir,
    meetings_dir,
    minutes_path,
    slides_dir,
)
from .datatracker import _get_json, iter_group_documents
from .materials_manifest import load_manifest, save_manifest
from .session_polls import process_session_polls


def format_filename(name: str) -> str:
    """Format a string to be a safe filename."""
    return re.sub(r"[^\w\s-]", "", name).strip().lower().replace(" ", "_")


# Material kinds we rev-gate (re-fetch content only when the document
# revision changes). Slides are large and rarely revised, polls are
# immutable — both stay skip-if-exists, so we don't pay to list their
# (numerous) revisions every gather.
_REV_GATED_KINDS = ("minutes", "agenda")


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
        "https://datatracker.ietf.org/meeting/" f"{meeting_number}/materials/{docname}"
    )


def _uri_id(uri: str) -> str:
    """Trailing numeric id of an API resource URI (`/…/meeting/4278/` → `4278`)."""
    return uri.rstrip("/").rsplit("/", maxsplit=1)[-1]


def _batch_fetch_meetings(
    meeting_ids: "set[str]",
) -> Dict[str, Tuple[str, str, str]]:
    """Resolve many meetings at once → `{id: (display, raw, date)}`.

    Uses the `id__in` filter so a WG's ~dozens of meetings come back in
    a couple of requests instead of one GET per session's meeting URI.
    `raw` ("125" / "interim-2026-httpbis-01") builds content URLs;
    `display` ("IETF 125" / the raw interim) is what the clustering /
    label / date helpers consume downstream.
    """
    out: Dict[str, Tuple[str, str, str]] = {}
    ordered = sorted(meeting_ids)
    for start in range(0, len(ordered), 100):
        chunk = ordered[start : start + 100]
        body = _get_json(
            "https://datatracker.ietf.org/api/v1/meeting/meeting/"
            f"?id__in={','.join(chunk)}&limit=100"
        )
        if not body:
            continue
        for meeting in body.get("objects") or []:
            number = meeting.get("number")
            if number is None:
                continue
            raw = str(number)
            mtype = (meeting.get("type") or "").rstrip("/")
            display = f"IETF {raw}" if mtype.endswith("/ietf") else raw
            out[_uri_id(meeting.get("resource_uri") or "")] = (
                display,
                raw,
                str(meeting.get("date") or ""),
            )
    return out


def get_meeting_links(
    wg_name: str, verbose: Verbosity = Verbosity.STATUS
) -> List[Dict[str, Any]]:
    """List a WG's meetings and their materials via the Datatracker API.

    Walks `/api/v1/meeting/session/?group__acronym=<wg>`, batch-resolves
    the referenced meetings (number, date), and returns one entry per
    meeting:

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
    # Pass 1: page through sessions, collecting (meeting id, materials).
    sessions: List[Tuple[str, List[str]]] = []
    meeting_ids: set[str] = set()
    path: Optional[str] = _SESSION_API.format(wg=wg_name)
    while path:
        body = _get_json(path)
        if not body:
            break
        for sess in body.get("objects") or []:
            meeting_uri = sess.get("meeting")
            if not meeting_uri:
                continue
            mid = _uri_id(meeting_uri)
            meeting_ids.add(mid)
            sessions.append((mid, sess.get("materials") or []))
        path = (body.get("meta") or {}).get("next") or None

    # One batched lookup for every referenced meeting.
    meta = _batch_fetch_meetings(meeting_ids)

    # Pass 2: assemble per-meeting entries from the resolved metadata.
    meetings_by_num: Dict[str, Dict[str, Any]] = {}
    for mid, materials in sessions:
        info = meta.get(mid)
        if not info:
            continue
        display, raw, date_str = info
        entry = meetings_by_num.setdefault(
            display, {"number": display, "date": date_str, "links": []}
        )
        for muri in materials:
            docname = _uri_id(str(muri))
            kind = _material_kind(docname)
            if kind is None:
                continue
            entry["links"].append(
                {
                    "type": kind,
                    "url": _material_url(raw, docname),
                    "docname": docname,
                }
            )

    return [m for m in meetings_by_num.values() if m["links"]]


def _material_revs(wg_name: str) -> Dict[str, str]:
    """Map `<doc-name> → rev` for the rev-gated material kinds, read from
    the document API (a few paginated calls, ETag-cached). Used to decide
    whether a cached minutes / agenda file needs re-fetching."""
    revs: Dict[str, str] = {}
    for kind in _REV_GATED_KINDS:
        for doc in iter_group_documents(wg_name, kind):
            name = doc.get("name")
            rev = doc.get("rev")
            if name and rev is not None:
                revs[name] = str(rev)
    return revs


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
        when = _parse_meeting_date(meeting["date"])
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
    re-fetch is skipped.
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
            m_date = _parse_meeting_date(meeting["date"])
            if m_date and m_date >= cutoff_date:
                filtered.append(meeting)
        meetings = filtered

    clusters = cluster_meetings(meetings)
    canonical_codes = {c.code for c in clusters}

    # Rev-gating: current revisions from the API vs. what we last wrote.
    revs = _material_revs(wg_name)
    manifest = load_manifest(wg_name)
    for cluster in clusters:
        _process_cluster(cluster, wg_name, destination, verbose, revs, manifest)
    save_manifest(wg_name, manifest)

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


def _needs_rebuild(
    out_file: str,
    links: List[Dict[str, Any]],
    revs: Dict[str, str],
    manifest: Dict[str, str],
) -> bool:
    """Whether a minutes / agenda aggregate must be re-fetched & rebuilt.

    True if the output file is missing, or any constituent document's
    current revision differs from what we last wrote (manifest). A doc
    with no known current rev (not in `revs`) is treated as changed so
    we don't silently freeze it."""
    if not os.path.isfile(out_file):
        return True
    for link in links:
        docname = link["docname"]
        # Only docs with a known current rev gate freshness; a doc we
        # can't get a rev for (e.g. a cross-group joint-session material)
        # falls back to the file-exists check rather than rebuilding
        # forever.
        if docname in revs and revs[docname] != manifest.get(docname):
            return True
    return False


def _process_cluster(
    cluster: MeetingCluster,
    wg_name: str,
    destination: str,
    verbose: Verbosity,
    revs: Dict[str, str],
    manifest: Dict[str, str],
) -> None:
    """Download a cluster's materials into its canonical dir.

    Minutes / agenda are rev-gated: rebuilt only when a constituent
    document's revision changed (or the file is missing), so revised
    minutes get picked up while unchanged ones aren't re-fetched.
    Slides download per-file (skip-if-exists)."""
    code = cluster.code
    canonical = meeting_dir(destination, code)
    os.makedirs(canonical, exist_ok=True)

    # Fold in any pre-existing per-session dirs (migration from the
    # old one-dir-per-row layout).
    for session in cluster.sessions[1:]:
        _absorb_meeting_dir(
            meeting_dir(destination, _safe_meeting_code(session["number"])),
            canonical,
        )

    # Slides: download each deck once (skip-if-exists).
    slides_out = slides_dir(destination, code)
    for session in cluster.sessions:
        for link in session["links"]:
            if link["type"] == "slides":
                _download_slide(link["url"], slides_out, verbose)

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
    for kind, out_file, title in (
        ("minutes", minutes_path(destination, code), "Meeting Materials"),
        ("agenda", agenda_path(destination, code), "Meeting Agenda"),
    ):
        links = [
            link
            for session in cluster.sessions
            for link in session["links"]
            if link["type"] == kind
        ]
        if not links:
            continue
        if not _needs_rebuild(out_file, links, revs, manifest):
            log(
                f"Skipping {code} {kind}: unchanged.",
                verbose,
                level=LogLevel.PROGRESS,
            )
            continue

        parts: List[str] = _header(title)
        fetched: List[Dict[str, Any]] = []
        for session in cluster.sessions:
            session_date = session.get("date", "")
            for link in session["links"]:
                if link["type"] != kind:
                    continue
                content = _fetch_text(link["url"], verbose)
                if content:
                    # Headed per-session so multi-session clusters stay
                    # legible (same-day sessions get the number too).
                    parts.append(f"## Session {session_date} ({session['number']})\n")
                    parts.append(f"URL: {link['url']}\n\n")
                    parts.append(content.strip() + "\n\n---\n\n")
                    fetched.append(link)

        # Only write if a session actually contributed content (the header
        # alone is ~3 short lines); otherwise the meeting has no agenda /
        # no minutes yet and we leave the file absent.
        text = "".join(parts)
        if len(text.strip()) > 150:
            if write_if_changed(out_file, text):
                log(f"Wrote {out_file}", verbose, level=LogLevel.PROGRESS)
            # Record the revs we successfully captured so a future gather
            # skips them; docs whose content failed stay stale and retry.
            for link in fetched:
                docname = link["docname"]
                if docname in revs:
                    manifest[docname] = revs[docname]


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
    # Skip if the .pdf is already present (local mode) OR its extracted
    # .pdf.txt is (suppressed mode dropped the .pdf, so the .txt is the
    # idempotency token — don't re-download a deck we've already extracted).
    if os.path.exists(pdf_dest) or os.path.exists(pdf_dest + ".txt"):
        return False
    return _download_if_pdf(url, pdf_dest, verbose)


def _download_if_pdf(url: str, dest_path: str, verbose: Verbosity) -> bool:
    """Check head/stream for PDF content type and download."""
    try:
        p_res = governed_get(
            url,
            timeout=60,
            stream=True,
            headers=DEFAULT_HEADERS,
        )
        p_res.raise_for_status()
        c_type = p_res.headers.get("Content-Type", "").lower()
        if "application/pdf" in c_type:
            log(f"Downloading PDF: {dest_path}...", verbose, level=LogLevel.PROGRESS)
            with atomic_open_binary(dest_path) as pdf_fh:
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


def _parse_meeting_date(date_str: str) -> Optional[datetime]:
    """Parse a meeting's `YYYY-MM-DD` date (the API always supplies one).

    The date may carry a trailing time / timezone (`2026-03-20
    12:00-14:00 AEDT`); we keep only the date. Returns None if absent
    or unparseable."""
    if not date_str:
        return None
    try:
        return datetime.strptime(date_str.split(" ")[0], "%Y-%m-%d")
    except (ValueError, IndexError):
        return None


# --- Attendance rosters ----------------------------------------------------
#
# The Datatracker `attended` API records who was present at each *session*,
# each row referencing a person id (not a name) — so attendance links to the
# people registry by id, collision-free. We gather it per meeting cluster
# into `attendance.json` (the machine sidecar the registry reads back to link
# attendees) and `attendance.md` (a human roster beside `minutes.md`). This is
# the "who was in the room" record; the registry's link-only enrichment then
# attaches it to people already known from mail / drafts / GitHub. Best-effort:
# any network failure leaves existing rosters untouched.

#: Sessions per `attended` request (session__in filter, one page).
_ATTENDED_CHUNK = 40
#: Person ids per `person` batch resolve.
_PERSON_CHUNK = 100


def _session_meeting_ids(wg_name: str) -> Dict[str, str]:
    """Walk the WG's sessions → `{session_id: meeting_id}` (all history).

    Mirrors `get_meeting_links`' pass 1 but keeps the *session* id (which
    the attendance API keys on) rather than the materials."""
    out: Dict[str, str] = {}
    path: Optional[str] = _SESSION_API.format(wg=wg_name)
    while path:
        body = _get_json(path)
        if not body:
            break
        for sess in body.get("objects") or []:
            meeting_uri = sess.get("meeting")
            sess_uri = sess.get("resource_uri")
            if meeting_uri and sess_uri:
                out[_uri_id(str(sess_uri))] = _uri_id(str(meeting_uri))
        path = (body.get("meta") or {}).get("next") or None
    return out


def _fetch_attendance(session_ids: List[str]) -> Dict[str, Set[str]]:
    """`{session_id: {person_uri, …}}` from `/meeting/attended/`."""
    out: Dict[str, Set[str]] = {}
    ordered = sorted(session_ids)
    for start in range(0, len(ordered), _ATTENDED_CHUNK):
        chunk = ",".join(ordered[start : start + _ATTENDED_CHUNK])
        path: Optional[str] = f"/api/v1/meeting/attended/?session__in={chunk}&limit=500"
        while path:
            body = _get_json(path)
            if not body:
                break
            for att in body.get("objects") or []:
                sess = att.get("session")
                person = att.get("person")
                if sess and person:
                    out.setdefault(_uri_id(str(sess)), set()).add(
                        str(person).rstrip("/")
                    )
            path = (body.get("meta") or {}).get("next") or None
    return out


def _resolve_person_names(person_uris: Set[str]) -> Dict[str, str]:
    """`{person_uri: display_name}` via batched `/person/person/?id__in=`."""
    out: Dict[str, str] = {}
    ids = sorted({_uri_id(u) for u in person_uris})
    for start in range(0, len(ids), _PERSON_CHUNK):
        chunk = ",".join(ids[start : start + _PERSON_CHUNK])
        body = _get_json(f"/api/v1/person/person/?id__in={chunk}&limit=100")
        if not body:
            continue
        for person in body.get("objects") or []:
            uri = str(person.get("resource_uri") or "").rstrip("/")
            name = person.get("name") or person.get("ascii") or ""
            if uri and name:
                out[uri] = str(name)
    return out


def _write_roster(
    destination: str,
    code: str,
    person_uris: Set[str],
    names: Dict[str, str],
    when: datetime,
) -> None:
    """Write `attendance.json` + `attendance.md` for one meeting code.

    The JSON is the registry's read-back source (name + person uri). The
    markdown is the human roster. Both are write-if-changed so an unchanged
    roster doesn't churn the cache (or re-embed)."""
    rows = sorted(
        ({"name": names.get(u, ""), "person": u} for u in person_uris),
        key=lambda r: ((r["name"] or "~").lower(), r["person"]),
    )
    os.makedirs(meeting_dir(destination, code), exist_ok=True)
    write_if_changed(
        attendance_data_path(destination, code),
        json.dumps(rows, indent=1, ensure_ascii=False) + "\n",
    )
    lines = [
        f"# Attendance — {code}",
        "",
        f"_{len(rows)} recorded attendees ({when.strftime('%Y-%m-%d')}), from "
        "the Datatracker attendance record. Presence in the room — NOT a "
        "position on any question; the chair declares consensus (see "
        "`read_ietf_interpretation_norms`)._",
        "",
    ]
    for row in rows:
        lines.append(f"- {row['name'] or row['person']}")
    lines.append("")
    write_if_changed(attendance_path(destination, code), "\n".join(lines))


def process_attendance(
    wg_name: str,
    destination: str,
    clusters: List[MeetingCluster],
    verbose: Verbosity = Verbosity.STATUS,
) -> None:
    """Fetch per-session attendance and write per-meeting rosters.

    `clusters` (from `process_meetings`) supplies the meeting→code mapping
    and bounds the work to the gathered window: attendance is written only
    for meetings that already have a cache dir. Best-effort — a Datatracker
    outage logs and returns, leaving existing rosters in place."""
    if not clusters:
        return
    # meeting display ("IETF 125" / interim raw) → canonical cache code.
    display_to_code = {
        str(sess["number"]): cluster.code
        for cluster in clusters
        for sess in cluster.sessions
    }
    sessions = _session_meeting_ids(wg_name)
    if not sessions:
        return
    meta = _batch_fetch_meetings(set(sessions.values()))
    # session id → code, dropping sessions outside the gathered window.
    session_code: Dict[str, str] = {}
    for sess_id, meeting_id in sessions.items():
        info = meta.get(meeting_id)
        if not info:
            continue
        code = display_to_code.get(info[0])
        if code:
            session_code[sess_id] = code
    if not session_code:
        return

    attendance = _fetch_attendance(list(session_code))
    by_code: Dict[str, Set[str]] = {}
    for sess_id, person_uris in attendance.items():
        code = session_code.get(sess_id)
        if code:
            by_code.setdefault(code, set()).update(person_uris)
    if not by_code:
        return

    all_uris: Set[str] = set().union(*by_code.values())
    names = _resolve_person_names(all_uris)
    code_when = {cluster.code: cluster.start for cluster in clusters}
    for code, person_uris in by_code.items():
        _write_roster(
            destination, code, person_uris, names, code_when.get(code, datetime.now())
        )
    log(
        f"Attendance: {len(by_code)} meeting roster(s), "
        f"{len(all_uris)} distinct attendees.",
        verbose,
        level=LogLevel.STATUS,
    )
