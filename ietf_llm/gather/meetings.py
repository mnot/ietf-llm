import os
import re
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup, Tag

from ..paths import (
    ORPHAN_MEETING_CODE,
    meeting_dir,
    meetings_dir,
    minutes_path,
    slides_dir,
)
from ..utils import (
    LogLevel,
    Verbosity,
    clean_html,
    fetch_resource,
    fetch_url,
    format_filename,
    log,
    write_if_changed,
)


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


def get_meeting_links(
    wg_name: str, verbose: Verbosity = Verbosity.STATUS
) -> List[Dict[str, Any]]:
    """Crawl meeting materials page and return list of primary links to minutes and materials."""
    url = f"https://datatracker.ietf.org/group/{wg_name}/meetings/"
    log(f"Crawling meeting materials for {wg_name}...", verbose, level=LogLevel.STATUS)
    html = fetch_url(url)
    if not html:
        return []

    bs_soup = BeautifulSoup(html, "html.parser")
    meetings = []

    # The datatracker uses id='pastmeets' for the header section
    header = bs_soup.find(id="pastmeets")
    if not header:
        return []

    # Find the first table after this header
    if not isinstance(header, Tag):
        return []
    table = header.find_next("table")
    if not isinstance(table, Tag):
        return []

    for row in table.find_all("tr"):
        cells = row.find_all("td")
        if not cells:
            continue

        # Extraction: Use 'data-start-utc' if available in a span, otherwise text
        date_cell = cells[1]
        date_text = date_cell.get_text(strip=True)
        if not date_text:
            # Fallback to data-start-utc attribute if present (common for JS-populated fields)
            span = date_cell.find("span", attrs={"data-start-utc": True})
            if isinstance(span, Tag):
                attr_val = span.get("data-start-utc")
                if isinstance(attr_val, str):
                    date_text = attr_val

        meeting_info: Dict[str, Any] = {
            "number": cells[0].get_text(strip=True),
            "date": date_text,
            "links": [],
        }

        # Refinement: only return links that exactly match 'Minutes' or 'Materials'
        # in a link with class btn-primary
        links = row.find_all("a", class_="btn-primary")
        for link in links:
            href_attr = link.get("href")
            if not href_attr or isinstance(href_attr, list):
                continue
            href = str(href_attr)
            text = link.get_text(strip=True)

            # Resolve relative URLs
            href = urljoin(url, href)

            if text == "Minutes":
                meeting_info["links"].append({"type": "minutes", "url": href})
            elif text == "Materials":
                meeting_info["links"].append({"type": "material", "url": href})

        if meeting_info["links"]:
            meetings.append(meeting_info)

    return meetings


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
                    start=span, end=span, sessions=[meeting],
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
        code=code, start=start, end=max(dates), sessions=sessions,
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
    if not os.path.isdir(src_dir) or os.path.realpath(src_dir) == os.path.realpath(dst_dir):
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
            verbose, level=LogLevel.PROGRESS,
        )
        return

    span = (
        cluster.start.strftime("%Y-%m-%d")
        if cluster.start.date() == cluster.end.date()
        else f"{cluster.start.strftime('%Y-%m-%d')} → {cluster.end.strftime('%Y-%m-%d')}"
    )
    parts: List[str] = [
        f"# Meeting Materials: {code} ({wg_name})\n",
        f"Date: {span}\n",
        f"Sessions: {len(cluster.sessions)}\n\n",
    ]
    for session in cluster.sessions:
        session_date = session.get("date", "")
        for link in session["links"]:
            # Slides / PDFs → canonical slides dir.
            _handle_pdfs(link["url"], destination, code, verbose)
            # Session polls (same materials-page walk).
            if link["type"] == "material":
                from .session_polls import (  # pylint: disable=import-outside-toplevel
                    fetch_polls_from_materials_page,
                )
                fetch_polls_from_materials_page(
                    link["url"], destination, wg_name, verbose,
                )
            # Minutes text, headed per-session so multi-session
            # clusters stay legible (same-day sessions get the
            # number to disambiguate the date).
            if link["type"] == "minutes":
                content = _extract_minutes_content(link["url"], verbose)
                if content:
                    parts.append(
                        f"## Session {session_date} ({session['number']})\n"
                    )
                    parts.append(f"URL: {link['url']}\n\n")
                    parts.append(content + "\n\n---\n\n")

    total_text = "".join(parts)
    # Only write minutes if a session actually contributed content
    # (header alone is ~3 short lines); otherwise this is a slides-only
    # cluster and we leave no minutes.md.
    if len(total_text.strip()) > 150:
        if write_if_changed(output_file, total_text):
            log(f"Wrote {output_file}", verbose, level=LogLevel.PROGRESS)


def _cleanup_absorbed_dirs(
    destination: str,
    clusters: List[MeetingCluster],
    canonical_codes: "set[str]",
) -> None:
    """Remove interim meeting dirs that were absorbed into a cluster
    (any prior-gather interim dir whose code isn't a canonical code).
    """
    absorbed = {
        _safe_meeting_code(s["number"])
        for c in clusters for s in c.sessions
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


def _handle_pdfs(url: str, dest: str, safe_num: str, verbose: Verbosity) -> List[str]:
    """Crawl a URL for PDF slide links and download them into the
    meeting's slides/ subdir."""
    log(f"Checking for PDFs at {url}...", verbose, level=LogLevel.PROGRESS)
    res = fetch_resource(url)
    if not res:
        return []

    updated = []
    soup = BeautifulSoup(res.text, "html.parser")
    # Look for potential slide/PDF links
    potential = soup.find_all("a", href=re.compile(r"slides-|/materials/|\.pdf$", re.I))

    # Slides for this meeting all land under meetings/<code>/slides/.
    out_dir = slides_dir(dest, safe_num)
    os.makedirs(out_dir, exist_ok=True)

    for p_link in potential:
        href = p_link.get("href")
        if not href or isinstance(href, list):
            continue
        p_url = str(href)
        p_text = p_link.get_text(strip=True).lower()

        # Skip non-slide links
        if (
            "slides" not in p_text
            and "slides-" not in p_url
            and not p_url.lower().endswith(".pdf")
        ):
            continue

        p_url = urljoin(url, p_url)
        p_base = os.path.basename(p_url)
        if not p_base.lower().endswith(".pdf"):
            p_base += ".pdf"

        # Directory disambiguates the meeting; basename can stand alone.
        pdf_dest = os.path.join(out_dir, p_base)
        if not os.path.exists(pdf_dest):
            if _download_if_pdf(p_url, pdf_dest, verbose):
                updated.append(pdf_dest)
    return updated


def _download_if_pdf(url: str, dest_path: str, verbose: Verbosity) -> bool:
    """Check head/stream for PDF content type and download."""
    try:
        p_res = requests.get(
            url,
            timeout=60,
            stream=True,
            headers={"User-Agent": "ietf-llm/0.1.0"},
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


def _extract_minutes_content(url: str, verbose: Verbosity) -> Optional[str]:
    """Find and return markdown/text minutes from a meeting minutes page."""
    log(f"Fetching minutes content from {url}...", verbose, level=LogLevel.PROGRESS)
    res = fetch_resource(url)
    if not res:
        return None

    # Already markdown?
    if "text/markdown" in res.headers.get("Content-Type", "").lower():
        return str(res.text)

    soup = BeautifulSoup(res.text, "html.parser")
    # Check for explicit markdown links
    md_link = None
    for a_tag in soup.find_all("a"):
        a_text = a_tag.get_text(strip=True).lower()
        a_href_attr = a_tag.get("href", "")
        if not a_href_attr or isinstance(a_href_attr, list):
            continue
        a_href = str(a_href_attr)
        if (
            "markdown" in a_text
            or a_href.lower().endswith(".md")
            or ".md?" in a_href.lower()
        ):
            md_link = a_tag
            break

    if md_link:
        md_url_attr = md_link.get("href")
        if md_url_attr and not isinstance(md_url_attr, list):
            md_url = urljoin(url, str(md_url_attr))
            md_res = fetch_resource(md_url, headers={"Accept": "text/markdown"})
            if (
                md_res
                and "text/markdown" in md_res.headers.get("Content-Type", "").lower()
            ):
                return str(md_res.text)

    # Final fallback: clean card-body or full text
    body_div = soup.find("div", class_="card-body")
    final_text = clean_html(str(body_div)) if body_div else clean_html(res.text)
    return str(final_text) if final_text else None


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
