import os
import sys
import re
import shutil
import filecmp
from enum import Enum
from typing import Callable, Dict, Optional
import requests
from bs4 import BeautifulSoup, Tag

DEFAULT_HEADERS = {"User-Agent": "ietf-llm/0.1.0"}
DEFAULT_MONTHS = 12


def get_config_dir() -> str:
    """Return the configuration directory, creating it if necessary."""
    config_dir = os.path.expanduser("~/.config/ietf-llm")
    if not os.path.exists(config_dir):
        os.makedirs(config_dir, exist_ok=True)
    return config_dir


def get_cache_dir() -> str:
    """Return the cache directory, creating it if necessary."""
    cache_dir = os.path.expanduser("~/.cache/ietf-llm")
    if not os.path.exists(cache_dir):
        os.makedirs(cache_dir, exist_ok=True)
    return cache_dir


def get_wg_file_cache_dir(wg_name: str) -> str:
    """Get the local file cache directory for a Working Group."""
    cache_dir = os.path.join(get_cache_dir(), wg_name, "files")
    if not os.path.exists(cache_dir):
        os.makedirs(cache_dir, exist_ok=True)
    return cache_dir


def copy_if_updated(src_path: str, dest_path: str) -> bool:
    """
    Copy file from src to dest only if it's new or the content has changed.
    Returns True if copied, False otherwise.
    """
    if not os.path.exists(src_path):
        return False

    if os.path.exists(dest_path):
        if filecmp.cmp(src_path, dest_path, shallow=False):
            return False

    shutil.copy2(src_path, dest_path)
    return True


def get_mailing_list_name(wg_name: str) -> str:
    """
    Find the mailing list name from the group 'about' page.
    """
    url = f"https://datatracker.ietf.org/group/{wg_name}/about/"
    res = fetch_resource(url)
    if not res:
        return wg_name

    bs_soup = BeautifulSoup(res.text, "html.parser")

    # Strategy 1: Look for "List archive »" link
    archive_link = None
    for a_tag in bs_soup.find_all("a"):
        if "List archive" in a_tag.get_text():
            archive_link = a_tag
            break

    if archive_link:
        href_val = archive_link.get("href")
        if isinstance(href_val, str) and (
            "mailarchive.ietf.org/arch/browse/" in href_val
            or "mailman.irtf.org" in href_val
        ):
            parts = href_val.strip("/").split("/")
            return parts[-1]

    # Strategy 2: Look for "Address" row in mailing list table
    rows = bs_soup.find_all("tr")
    for row in rows:
        th = row.find("th")
        if th and "Address" in th.get_text():
            td = row.find("td")
            if td:
                email = td.get_text(strip=True)
                if "@" in email:
                    return str(email.split("@")[0])

    return wg_name


def get_group_type(wg_name: str) -> str:
    """
    Identify if the acronym is for a Working Group ('ietf') or Research Group ('irtf').
    """
    url = f"https://datatracker.ietf.org/group/{wg_name}/about/"
    res = fetch_resource(url)
    if not res:
        return "ietf"  # Default to ietf

    bs_soup = BeautifulSoup(res.text, "html.parser")
    # Strategy: look for 'WG' or 'RG' in the summary table
    table = bs_soup.find("table", class_="table-sm")
    if isinstance(table, Tag):
        first_td = table.find("td")
        if isinstance(first_td, Tag):
            text = first_td.get_text(strip=True)
            if text == "RG":
                return "irtf"
            if text == "WG":
                return "ietf"

    # Fallback: check for charter link pattern if table strategy fails
    charter_link = bs_soup.find("a", href=re.compile(r"/doc/charter-irtf-"))
    if charter_link:
        return "irtf"

    return "ietf"


def graceful_keyboard_interrupt(
    entry: "Callable[[], None]",
) -> "Callable[[], None]":
    """Decorator that wraps a CLI `main()` so Ctrl-C exits cleanly.

    A bare `ietf-llm` etc. would otherwise dump a KeyboardInterrupt
    traceback on Ctrl-C — ugly, and looks like a crash. The wrapped
    entry catches it, prints a one-line "Interrupted." to stderr, and
    exits with status 130 (the conventional "terminated by Ctrl-C" code).
    """
    def runner() -> None:
        try:
            entry()
        except KeyboardInterrupt:
            # Newline first because Ctrl-C usually lands mid-line.
            print("\nInterrupted.", file=sys.stderr)
            sys.exit(130)
    runner.__name__ = entry.__name__
    runner.__doc__ = entry.__doc__
    return runner


class Verbosity(Enum):
    """Logging verbosity settings."""

    QUIET = 0
    STATUS = 1
    VERBOSE = 2


class LogLevel(Enum):
    """Logging message levels."""

    ERROR = 0
    STATUS = 1
    PROGRESS = 2


def log(
    message: str,
    verbosity: Verbosity = Verbosity.STATUS,
    level: LogLevel = LogLevel.PROGRESS,
) -> None:
    """Print a status / progress / error message to stderr.

    Everything `log()` emits is narration about what the tool is doing,
    not program output; writing to stderr keeps it clear of any stdout
    a caller might be piping (e.g. `ietf-llm-search` results, or future
    stdout-data CLIs). Convention matches curl, git, wget, etc.

    - level: LogLevel.ERROR / STATUS / PROGRESS — ERROR always shows;
      STATUS shows unless --quiet; PROGRESS shows only under --verbose.
    """
    if level == LogLevel.ERROR:
        print(f"[ERROR] {message}", file=sys.stderr)
        return

    if verbosity == Verbosity.QUIET:
        return

    if verbosity == Verbosity.VERBOSE or (
        verbosity == Verbosity.STATUS and level == LogLevel.STATUS
    ):
        print(message, file=sys.stderr)


def fetch_resource(
    url: str, headers: Optional[Dict[str, str]] = None
) -> Optional[requests.Response]:
    """Fetch a resource and return the response object."""
    combined_headers = DEFAULT_HEADERS.copy()
    if headers:
        combined_headers.update(headers)
    try:
        res = requests.get(url, headers=combined_headers, timeout=30)
        res.raise_for_status()
        return res
    except requests.RequestException as err:
        log(f"Error fetching {url}: {err}", level=LogLevel.ERROR)
        return None


def fetch_url(url: str, headers: Optional[Dict[str, str]] = None) -> Optional[str]:
    """Fetch content from a URL."""
    res = fetch_resource(url, headers=headers)
    return str(res.text) if res else None


def clean_html(html_content: str) -> str:
    """Simple HTML to text conversion using BeautifulSoup with aggressive cleaning."""
    if not html_content:
        return ""
    bs_soup = BeautifulSoup(html_content, "html.parser")

    # Remove common navigation and header/footer tags
    for element in bs_soup(["script", "style", "nav", "header", "footer", "aside"]):
        element.decompose()

    # Strip specific navigation and alert components
    for cls_name in ["navbar", "alert", "modal", "visually-hidden"]:

        def match_class(cls_val: Optional[str], target: str = cls_name) -> bool:
            return bool(
                cls_val and any(val.startswith(target) for val in cls_val.split())
            )

        for element in bs_soup.find_all(class_=match_class):
            if element.name not in ["body", "html", "main"]:
                element.decompose()

    # Specifically remove the "Skip to main content" links
    for skip_link in bs_soup.find_all("a"):
        skip_text = skip_link.get_text(strip=True).lower()
        if "skip to" in skip_text:
            skip_link.decompose()

    # Get text
    text = bs_soup.get_text()

    # Break into lines and remove leading and trailing space on each
    lines = (line.strip() for line in text.splitlines())

    # Prohibited patterns (mostly IETF boilerplate/footer links)
    prohibited = [
        r"^Privacy Statement$",
        r"^About IETF Datatracker$",
        r"^Version \d",
        r"^System Status$",
        r"^Report a bug$",
        r"^IETF LLC$",
        r"^IETF Trust$",
        r"^RFC Editor$",
        r"^IANA$",
        r"^NomComs$",
        r"^Downref registry$",
        r"^Liaison statements$",
    ]
    prohibited_regex = re.compile("|".join(prohibited), re.I)

    # Filter out lines that match prohibited patterns or are empty
    filtered_lines = []
    for line in lines:
        if not line:
            continue
        if prohibited_regex.match(line):
            continue
        filtered_lines.append(line)

    # Reassemble and drop blank lines
    text = "\n".join(filtered_lines)

    return text.strip()


def format_filename(name: str) -> str:
    """Format a string to be a safe filename."""
    return re.sub(r"[^\w\s-]", "", name).strip().lower().replace(" ", "_")


def get_wg_title(wg_name: str) -> str:
    """Fetch the full WG title from the IETF Datatracker."""
    url = f"https://datatracker.ietf.org/group/{wg_name}/about/"
    res = fetch_resource(url)
    if res:
        soup = BeautifulSoup(res.text, "html.parser")
        h1 = soup.find("h1")
        if h1:
            title = h1.get_text(strip=True)
            # Clean up title if it contains the short name in parens
            if "(" in title and wg_name.lower() in title.lower():
                title = title.split("(")[0].strip()
            return title
    return f"{wg_name.upper()} Working Group"
