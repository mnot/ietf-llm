import os
import re
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup, Tag

from ..utils import (
    LogLevel,
    Verbosity,
    clean_html,
    fetch_resource,
    fetch_url,
    format_filename,
    log,
)


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


def process_meetings(
    wg_name: str,
    destination: str,
    verbose: Verbosity = Verbosity.STATUS,
    months: Optional[int] = None,
) -> List[str]:
    """Fetch meeting minutes and materials and write to destination."""
    updated_files = []
    meetings = get_meeting_links(wg_name, verbose)
    if not meetings:
        log(
            f"No meeting materials found for {wg_name}.", verbose, level=LogLevel.STATUS
        )
        return []

    # Filter meetings by date if months is specified
    if months is not None:
        cutoff_date = datetime.now() - timedelta(days=months * 30)
        filtered_meetings = []
        for meeting in meetings:
            m_date = _parse_meeting_date(meeting["date"], meeting["number"])
            if m_date and m_date >= cutoff_date:
                filtered_meetings.append(meeting)
        meetings = filtered_meetings

    for meeting in meetings:
        safe_num = format_filename(meeting["number"]).replace("_", "").replace("-", "")
        output_file = os.path.join(destination, f"{safe_num}-minutes.md")

        # Check if we already have files for this meeting to avoid extra requests
        if os.path.exists(output_file):
            log(
                f"Skipping meeting {meeting['number']}: already downloaded.",
                verbose,
                level=LogLevel.PROGRESS,
            )
            continue

        meeting_text_parts = []
        meeting_text_parts.append(
            f"# Meeting Materials for IETF {meeting['number']} ({wg_name})\n"
        )
        if meeting.get("date"):
            meeting_text_parts.append(f"Date: {meeting['date']}\n")
        meeting_text_parts.append("\n")

        for link in meeting["links"]:
            # 1. Look for and download PDFs from any meeting pages
            updated_files.extend(
                _handle_pdfs(link["url"], destination, safe_num, verbose)
            )

            # 2. Look for and download session poll records (same hub
            # walk as #1; no extra Datatracker endpoint involved).
            # Lazy import so meetings.py stays the entry point and
            # session_polls.py can grow without circular concerns.
            if link["type"] == "material":
                from .session_polls import (  # pylint: disable=import-outside-toplevel
                    fetch_polls_from_materials_page,
                )
                updated_files.extend(
                    fetch_polls_from_materials_page(
                        link["url"], destination, wg_name, verbose,
                    )
                )

            # 3. Extract minutes text
            if link["type"] == "minutes":
                content = _extract_minutes_content(link["url"], verbose)
                if content:
                    meeting_text_parts.append(f"## {link['type'].capitalize()}\n")
                    meeting_text_parts.append(f"URL: {link['url']}\n\n")
                    meeting_text_parts.append(content + "\n\n---\n\n")

        if len(meeting_text_parts) > 1:
            total_text = "".join(meeting_text_parts)
            if len(total_text.strip()) > 150:
                log(f"Writing {output_file}...", verbose, level=LogLevel.PROGRESS)
                with open(output_file, "w", encoding="utf-8") as out_fh:
                    out_fh.write(total_text)
                updated_files.append(output_file)
            else:
                log(
                    f"Skipping nearly empty minutes for {safe_num}.",
                    verbose,
                    level=LogLevel.PROGRESS,
                )

    log(
        f"Done! Extracted materials from meetings into {destination}.",
        verbose,
        level=LogLevel.STATUS,
    )
    return updated_files


def _handle_pdfs(url: str, dest: str, safe_num: str, verbose: Verbosity) -> List[str]:
    """Crawl a URL for PDF slide links and download them."""
    log(f"Checking for PDFs at {url}...", verbose, level=LogLevel.PROGRESS)
    res = fetch_resource(url)
    if not res:
        return []

    updated = []
    soup = BeautifulSoup(res.text, "html.parser")
    # Look for potential slide/PDF links
    potential = soup.find_all("a", href=re.compile(r"slides-|/materials/|\.pdf$", re.I))

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

        pdf_dest = os.path.join(dest, f"{safe_num}-{p_base}")
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
