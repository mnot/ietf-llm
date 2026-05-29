import filecmp
import os
import re
import shutil
import sys
from enum import Enum
from functools import lru_cache
from typing import Any, Callable, Dict, List, Optional, Tuple

import requests
from bs4 import BeautifulSoup

from . import __version__

DEFAULT_HEADERS = {"User-Agent": f"ietf-llm/{__version__}"}
DEFAULT_MONTHS = 12


def cached_wg_names() -> List[str]:
    """Shortnames of every gathered WG / corpus — directories with a
    `files/` subdir under the cache root, sorted. Skips dot- and
    underscore-prefixed entries (machinery like `_github-users.json`).

    Shared by `ietf-llm --all` / `--list` and the shell-completion
    completer for the `wg` positional, so they can't drift.
    """
    root = get_cache_dir()
    if not os.path.isdir(root):
        return []
    out: List[str] = []
    for name in sorted(os.listdir(root)):
        if name.startswith(".") or name.startswith("_"):
            continue
        if os.path.isdir(os.path.join(root, name, "files")):
            out.append(name)
    return out


def wg_completer(prefix: str, **_kwargs: Any) -> List[str]:
    """argcomplete completer for a `wg` positional: cached shortnames
    matching `prefix`. Keep it fast — argcomplete spins a fresh
    interpreter per <TAB>, so this is just a directory listing.
    """
    return [w for w in cached_wg_names() if w.startswith(prefix)]


def maybe_autocomplete(parser: Any) -> None:
    """Wire argcomplete into `parser` if the package is installed.

    Called right before `parse_args()`. A no-op (not an error) when
    argcomplete isn't present, so a minimal / editable install
    without the dependency still runs the CLI normally.
    """
    try:
        import argcomplete  # pylint: disable=import-outside-toplevel
    except ImportError:
        return
    argcomplete.autocomplete(parser)


def is_synthetic_wg(name: str) -> bool:
    """True for synthetic / non-WG corpora (the `x-` prefix convention).

    Some collections of drafts and mailing lists predate (or sit
    parallel to) any formal WG, but it's still useful to gather them
    into the same corpus shape so the MCP server and search tools
    can answer questions about them. The `x-` prefix opts out of
    every Datatracker / WG-page lookup (no charter, no leadership,
    no auto-discovered drafts or mailing list, no transcripts) while
    leaving everything else — mail thread reconstruction, GitHub
    issue gathering, the explicit `--draft` / `--mailing-list`
    additions, indexing — working as normal.

    Naming convention chosen for brevity and zero risk of colliding
    with a real IETF WG shortname (none start with `x-`).
    """
    return name.startswith("x-")


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


def write_if_changed(path: str, content: str) -> bool:
    """Write `content` to `path` only if it differs from what's there
    (or the file is missing). Returns True if a write happened.

    Load-bearing for the incremental embedder, which re-embeds any
    file whose mtime advanced. The per-thread / per-issue writers
    regenerate every file each gather; without this guard a byte-
    identical re-render would still bump mtime and force a full
    re-embed of the whole corpus on every update.
    """
    try:
        with open(path, "r", encoding="utf-8") as fh:
            if fh.read() == content:
                return False
    except OSError:
        pass  # missing / unreadable → fall through and write
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)
    return True


@lru_cache(maxsize=128)
def fetch_group_object(wg_name: str) -> Optional[Dict[str, Any]]:
    """Fetch a group's Datatracker record by acronym, or None.

    One JSON call to `/api/v1/group/group/?acronym=<wg>` backs all the
    group-metadata helpers (type, title, mailing list) so we read
    structured fields instead of scraping the group's About page.
    Cached per process, so a single gather resolves each group once
    across charter / drafts / mbox / index / export.

    Synthetic (`x-`) corpora have no Datatracker record, so the lookup
    is skipped entirely (returns None; callers fall back to defaults).
    """
    if is_synthetic_wg(wg_name):
        return None
    url = (
        "https://datatracker.ietf.org/api/v1/group/group/"
        f"?acronym={wg_name}&format=json"
    )
    res = fetch_resource(url)
    if not res:
        return None
    try:
        objects = res.json().get("objects") or []
    except ValueError:
        return None
    return objects[0] if objects else None


@lru_cache(maxsize=128)
def get_group_resources(wg_name: str) -> Tuple[Tuple[str, str, str], ...]:
    """A group's "Additional Resources" as `((slug, label, value), …)`.

    `slug` is the resource type from the extresourcename URI
    (`github_org`, `webpage`, `zulip`, `mailing_list_archive`, …);
    `label` is the human display name ("repositories", "alternate
    list archives", …), falling back to the slug; `value` is its
    URL / string. Empty for synthetic corpora or groups with no
    resources. Read from `/api/v1/group/groupextresource/`, cached
    per run. Returns a tuple so it stays hashable for the cache.
    """
    group = fetch_group_object(wg_name)
    if not group or group.get("id") is None:
        return ()
    url = (
        "https://datatracker.ietf.org/api/v1/group/groupextresource/"
        f"?group={group['id']}&format=json&limit=200"
    )
    res = fetch_resource(url)
    if not res:
        return ()
    try:
        objects = res.json().get("objects") or []
    except ValueError:
        return ()
    out: List[Tuple[str, str, str]] = []
    for obj in objects:
        slug = (obj.get("name") or "").rstrip("/").rsplit("/", 1)[-1]
        value = obj.get("value") or ""
        if slug and value:
            out.append((slug, obj.get("display_name") or slug, value))
    # Sort for deterministic output: group.md is write-if-changed, so a
    # non-deterministic API ordering would churn the file (and re-embed)
    # on every gather.
    return tuple(sorted(out))


#: List name embedded in a mailarchive.ietf.org browse URL.
_MAILARCHIVE_BROWSE_RE = re.compile(
    r"mailarchive\.ietf\.org/arch/browse/([^/?#]+)", re.IGNORECASE
)


def get_mailing_list_name(wg_name: str) -> str:
    """Return the WG's mailing list name for the IMAP archive.

    Normally the local part of the Datatracker `list_email` (e.g.
    `tls` for tls@ietf.org). When the list is hosted off the IETF
    infrastructure — httpbis runs at w3.org — the IETF keeps a mirror
    under a different name; the "alternate list archives" Additional
    Resource points at `mailarchive.ietf.org/arch/browse/<name>/`,
    which is what the IMAP server exposes, so we prefer that `<name>`
    (httpbis → `httpbisa`). Falls back to the WG shortname when no
    record / address is found.
    """
    group = fetch_group_object(wg_name)
    if not group:
        return wg_name
    list_email = group.get("list_email") or ""
    if "@" not in list_email:
        return wg_name
    primary, domain = list_email.split("@", 1)
    if domain.lower() not in ("ietf.org", "irtf.org"):
        for slug, _label, value in get_group_resources(wg_name):
            if slug == "mailing_list_archive":
                match = _MAILARCHIVE_BROWSE_RE.search(value)
                if match:
                    return match.group(1)
    return primary or wg_name


def get_group_type(wg_name: str) -> str:
    """'ietf' for a Working Group, 'irtf' for a Research Group.

    Read from the group's `type` field on Datatracker
    (`.../grouptypename/wg|rg/`). Defaults to 'ietf'.
    """
    group = fetch_group_object(wg_name)
    if group:
        type_uri = (group.get("type") or "").rstrip("/")
        if type_uri.endswith("/rg"):
            return "irtf"
    return "ietf"


def get_group_state(wg_name: str) -> Optional[str]:
    """Group state slug — `active`, `concluded`, `replaced`, … — from
    the Datatracker `state` field, or None when there's no record.

    Worth surfacing because it changes how a consumer reads the
    corpus: a concluded WG won't see new activity, so 'latest thread'
    being old is expected rather than a staleness signal.
    """
    group = fetch_group_object(wg_name)
    if not group:
        return None
    state_uri = (group.get("state") or "").rstrip("/")
    return state_uri.rsplit("/", 1)[-1] or None if state_uri else None


def get_group_area(wg_name: str) -> Optional[Tuple[str, str]]:
    """The group's parent area as `(acronym, name)`, or None.

    Resolves the `parent` link on the group record (e.g. httpbis →
    `('wit', 'Web and Internet Transport')`). Returns None for groups
    with no parent or when the lookup fails.
    """
    group = fetch_group_object(wg_name)
    if not group:
        return None
    parent_uri = group.get("parent")
    if not parent_uri:
        return None
    res = fetch_resource(f"https://datatracker.ietf.org{parent_uri}?format=json")
    if not res:
        return None
    try:
        parent = res.json()
    except ValueError:
        return None
    acronym = parent.get("acronym") or ""
    name = parent.get("name") or ""
    return (acronym, name) if (acronym or name) else None


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
    """Full group name from the IETF Datatracker (e.g. 'Transport Layer
    Security'), or a generic fallback when no record is found."""
    group = fetch_group_object(wg_name)
    if group and group.get("name"):
        return str(group["name"])
    return f"{wg_name.upper()} Working Group"
