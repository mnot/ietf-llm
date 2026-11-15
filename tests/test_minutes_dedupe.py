"""Minutes / agenda aggregates write each distinct body once.

Datatracker lists each interim session as its own row, and chairs often
attach one document to several of them. `_process_cluster` used to append
the body once per session; it now folds sessions with identical bodies
into one block. The guard is a writer->reader round trip: drive
`_process_cluster` with a stubbed fetch, then read the file back through
`tool_read_minutes` and the embedding chunker.
"""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

import ietf_llm.gather.sources.meetings as meetings
from ietf_llm.embeddings.chunking import _chunk_file
from ietf_llm.gather.sources.meetings import MeetingCluster, _process_cluster
from ietf_llm.log import Verbosity
from ietf_llm.mcp.meetings import tool_read_minutes
from ietf_llm.paths import agenda_path, get_wg_file_cache_dir, minutes_path

WG = "aipref"
CODE = "interim20250408"
BODY = ("Chairs opened the meeting. " + "The group discussed vocabulary. " * 40).strip()
OTHER = ("A different set of minutes. " + "Entirely separate content. " * 40).strip()


def _session(number: str, date: str, kind: str, docname: str) -> Dict[str, Any]:
    return {
        "number": number,
        "date": date,
        "links": [{"type": kind, "url": f"https://dt/{docname}", "docname": docname}],
    }


def _cluster(sessions: List[Dict[str, Any]]) -> MeetingCluster:
    return MeetingCluster(
        code=CODE,
        start=datetime(2025, 4, 8),
        end=datetime(2025, 4, 10),
        sessions=sessions,
    )


def _run(
    monkeypatch: pytest.MonkeyPatch,
    cluster: MeetingCluster,
    texts: Dict[str, str],
    manifest: Optional[Dict[str, str]] = None,
    fetches: Optional[List[str]] = None,
) -> Dict[str, str]:
    """Run `_process_cluster` against canned bodies keyed by URL."""

    def fake_fetch(url: str, verbose: Verbosity) -> Optional[str]:
        if fetches is not None:
            fetches.append(url)
        return texts.get(url)

    monkeypatch.setattr(meetings, "_fetch_text", fake_fetch)
    manifest = {} if manifest is None else manifest
    revs = {link["docname"]: "00" for s in cluster.sessions for link in s["links"]}
    _process_cluster(
        cluster, WG, get_wg_file_cache_dir(WG), Verbosity.QUIET, revs, manifest
    )
    return manifest


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def test_identical_bodies_written_once_with_every_url(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cluster = _cluster(
        [
            _session("interim-2025-aipref-01", "2025-04-08", "minutes", "m-01"),
            _session("interim-2025-aipref-02", "2025-04-08", "minutes", "m-02"),
            _session("interim-2025-aipref-03", "2025-04-09", "minutes", "m-03"),
        ]
    )
    texts = {f"https://dt/m-0{n}": BODY + "\n" for n in (1, 2, 3)}
    manifest = _run(monkeypatch, cluster, texts)
    cache = get_wg_file_cache_dir(WG)
    text = _read(minutes_path(cache, CODE))

    assert text.count(BODY) == 1
    assert text.count("\n## Session") == 1
    assert (
        "## Sessions 2025-04-08 (interim-2025-aipref-01, interim-2025-aipref-02); "
        "2025-04-09 (interim-2025-aipref-03)\n"
    ) in text
    for n in (1, 2, 3):
        assert f"URL: https://dt/m-0{n}\n" in text
    assert "Sessions: 3\n" in text  # the header still counts the sessions
    # Every document's rev is recorded, so none is refetched next time.
    assert {"m-01", "m-02", "m-03"} <= set(manifest)

    # Reader side: the tool serves the file as written, and the body is
    # chunked once rather than once per session.
    assert tool_read_minutes(WG, CODE).count(BODY) == 1
    chunks = _chunk_file(minutes_path(cache, CODE), f"meetings/{CODE}/minutes.md")
    assert "".join(c.text for c in chunks).count("Chairs opened the meeting.") == 1


def test_differing_bodies_stay_separate(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cluster = _cluster(
        [
            _session("interim-2025-aipref-01", "2025-04-08", "minutes", "m-01"),
            _session("interim-2025-aipref-02", "2025-04-09", "minutes", "m-02"),
            _session("interim-2025-aipref-03", "2025-04-10", "minutes", "m-03"),
        ]
    )
    texts = {
        "https://dt/m-01": BODY,
        "https://dt/m-02": OTHER,
        "https://dt/m-03": BODY,  # repeats the first, not the one before it
    }
    _run(monkeypatch, cluster, texts)
    text = _read(minutes_path(get_wg_file_cache_dir(WG), CODE))

    assert text.count(BODY) == 1
    assert text.count(OTHER) == 1
    assert (
        "## Session 2025-04-09 (interim-2025-aipref-02)\nURL: https://dt/m-02\n\n"
    ) in text
    assert (
        "## Sessions 2025-04-08 (interim-2025-aipref-01); "
        "2025-04-10 (interim-2025-aipref-03)\n"
        "URL: https://dt/m-01\nURL: https://dt/m-03\n\n"
    ) in text


def test_all_differing_bodies_keep_every_block(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cluster = _cluster(
        [
            _session("interim-2025-aipref-01", "2025-04-08", "minutes", "m-01"),
            _session("interim-2025-aipref-02", "2025-04-09", "minutes", "m-02"),
        ]
    )
    _run(monkeypatch, cluster, {"https://dt/m-01": BODY, "https://dt/m-02": OTHER})
    text = _read(minutes_path(get_wg_file_cache_dir(WG), CODE))
    assert text.count("\n## Session ") == 2
    assert "## Sessions" not in text


def test_whitespace_around_body_does_not_defeat_matching(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cluster = _cluster(
        [
            _session("interim-2025-aipref-01", "2025-04-08", "minutes", "m-01"),
            _session("interim-2025-aipref-02", "2025-04-08", "minutes", "m-02"),
        ]
    )
    texts = {"https://dt/m-01": BODY, "https://dt/m-02": "\n\n" + BODY + "\n\n\n"}
    _run(monkeypatch, cluster, texts)
    text = _read(minutes_path(get_wg_file_cache_dir(WG), CODE))
    assert text.count(BODY) == 1


def test_single_session_layout_is_unchanged(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cluster = _cluster([_session("IETF 125", "2026-03-14", "minutes", "m-01")])
    _run(monkeypatch, cluster, {"https://dt/m-01": BODY})
    text = _read(minutes_path(get_wg_file_cache_dir(WG), CODE))
    assert text.endswith(
        f"## Session 2026-03-14 (IETF 125)\nURL: https://dt/m-01\n\n{BODY}\n\n---\n\n"
    )


def test_one_document_attached_to_two_sessions(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A numbered meeting with two sessions sharing one minutes document
    # lists the same URL twice (the httpbis IETF 100 case in real caches).
    link = {"type": "minutes", "url": "https://dt/m-100", "docname": "m-100"}
    cluster = _cluster(
        [{"number": "IETF 100", "date": "2017-11-11", "links": [link, dict(link)]}]
    )
    fetches: List[str] = []
    _run(monkeypatch, cluster, {"https://dt/m-100": BODY}, fetches=fetches)
    text = _read(minutes_path(get_wg_file_cache_dir(WG), CODE))
    assert fetches == ["https://dt/m-100"]
    assert text.count(BODY) == 1
    assert text.count("URL: https://dt/m-100\n") == 1
    assert "## Session 2017-11-11 (IETF 100)\n" in text


def test_agenda_aggregate_dedupes_too(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cluster = _cluster(
        [
            _session("interim-2025-aipref-01", "2025-04-08", "agenda", "a-01"),
            _session("interim-2025-aipref-02", "2025-04-09", "agenda", "a-02"),
        ]
    )
    _run(monkeypatch, cluster, {"https://dt/a-01": BODY, "https://dt/a-02": BODY})
    text = _read(agenda_path(get_wg_file_cache_dir(WG), CODE))
    assert text.count(BODY) == 1
    assert "URL: https://dt/a-01\nURL: https://dt/a-02\n" in text
    assert "## Sessions 2025-04-08 (interim-2025-aipref-01); 2025-04-09 (" in text


def test_format_marker_regenerates_an_old_layout_file_once(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cluster = _cluster(
        [
            _session("interim-2025-aipref-01", "2025-04-08", "minutes", "m-01"),
            _session("interim-2025-aipref-02", "2025-04-08", "minutes", "m-02"),
        ]
    )
    texts = {"https://dt/m-01": BODY, "https://dt/m-02": BODY}
    out = minutes_path(get_wg_file_cache_dir(WG), CODE)

    # A file as the old writer left it: a block per session, revs current
    # in the manifest, and no format marker.
    os.makedirs(os.path.dirname(out), exist_ok=True)
    old = f"# Meeting Materials: {CODE} ({WG})\n\n"
    for n in (1, 2):
        old += f"## Session 2025-04-08 (interim-2025-aipref-0{n})\n"
        old += f"URL: https://dt/m-0{n}\n\n{BODY}\n\n---\n\n"
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(old)
    manifest = {"m-01": "00", "m-02": "00"}

    fetches: List[str] = []
    _run(monkeypatch, cluster, texts, manifest, fetches)
    assert fetches  # rebuilt although every rev matched
    assert _read(out).count(BODY) == 1

    # Now stamped: the next gather skips it, as it did before the marker.
    fetches.clear()
    _run(monkeypatch, cluster, texts, manifest, fetches)
    assert not fetches


def _old_layout_file(cluster: MeetingCluster, bodies: Dict[str, str]) -> str:
    """Write a minutes file as the pre-dedupe writer left it; return its path."""
    out = minutes_path(get_wg_file_cache_dir(WG), CODE)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    text = f"# Meeting Materials: {CODE} ({WG})\n\n"
    for session in cluster.sessions:
        link = session["links"][0]
        text += f"## Session {session['date']} ({session['number']})\n"
        text += f"URL: {link['url']}\n\n{bodies[link['url']]}\n\n---\n\n"
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(text)
    return out


def _two_distinct() -> MeetingCluster:
    return _cluster(
        [
            _session("interim-2025-aipref-01", "2025-04-08", "minutes", "m-01"),
            _session("interim-2025-aipref-02", "2025-04-09", "minutes", "m-02"),
        ]
    )


def _stamped(manifest: Dict[str, str]) -> bool:
    return any(key.startswith("format:") for key in manifest)


def test_failed_fetch_keeps_the_complete_file_and_is_retried(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cluster = _two_distinct()
    texts = {"https://dt/m-01": BODY, "https://dt/m-02": OTHER}
    out = _old_layout_file(cluster, texts)
    before = _read(out)
    manifest = {"m-01": "00", "m-02": "00"}

    # Forced (unstamped) rebuild with the second session failing to fetch.
    _run(monkeypatch, cluster, {"https://dt/m-01": BODY}, manifest)
    assert _read(out) == before  # not truncated
    assert not _stamped(manifest)

    # The next gather retries the whole cluster and converts it.
    fetches: List[str] = []
    _run(monkeypatch, cluster, texts, manifest, fetches)
    assert sorted(fetches) == ["https://dt/m-01", "https://dt/m-02"]
    assert _read(out).count(BODY) == 1
    assert _read(out).count(OTHER) == 1
    assert _stamped(manifest)


def test_failed_fetch_on_a_revision_change_keeps_the_old_file(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Already converted; m-01 is revised upstream while m-02 fails to fetch.
    cluster = _two_distinct()
    texts = {"https://dt/m-01": BODY, "https://dt/m-02": OTHER}
    manifest = _run(monkeypatch, cluster, texts)  # complete and stamped
    out = minutes_path(get_wg_file_cache_dir(WG), CODE)
    before = _read(out)
    manifest["m-01"] = "stale"  # upstream rev "00" now differs from this

    _run(monkeypatch, cluster, {"https://dt/m-01": BODY + " revised"}, manifest)
    assert _read(out) == before
    assert manifest["m-01"] == "stale"  # still looks changed, so it retries


def test_partial_first_write_stays_unstamped_and_completes_later(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cluster = _two_distinct()
    manifest = _run(monkeypatch, cluster, {"https://dt/m-01": BODY})
    out = minutes_path(get_wg_file_cache_dir(WG), CODE)
    assert BODY in _read(out)
    assert OTHER not in _read(out)
    assert not _stamped(manifest)

    texts = {"https://dt/m-01": BODY, "https://dt/m-02": OTHER}
    _run(monkeypatch, cluster, texts, manifest)
    assert OTHER in _read(out)
    assert _stamped(manifest)


def test_lone_link_file_is_stamped_without_refetching(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cluster = _cluster([_session("IETF 125", "2026-03-14", "minutes", "m-01")])
    out = _old_layout_file(cluster, {"https://dt/m-01": BODY})
    before = _read(out)
    manifest = {"m-01": "00"}
    fetches: List[str] = []
    _run(monkeypatch, cluster, {"https://dt/m-01": BODY}, manifest, fetches)
    assert not fetches
    assert _read(out) == before
    assert manifest[f"format:{CODE}/minutes"] == "2"


def test_same_url_on_two_sessions_is_still_rebuilt(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Two links to one URL: the old file holds the body twice, so stamping
    # alone would leave the duplicate.
    link = {"type": "minutes", "url": "https://dt/m-100", "docname": "m-100"}
    cluster = _cluster(
        [{"number": "IETF 100", "date": "2017-11-11", "links": [link, dict(link)]}]
    )
    out = minutes_path(get_wg_file_cache_dir(WG), CODE)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    block = f"## Session 2017-11-11 (IETF 100)\nURL: {link['url']}\n\n{BODY}\n\n---\n\n"
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(f"# Meeting Materials: {CODE} ({WG})\n\n{block}{block}")
    fetches: List[str] = []
    _run(monkeypatch, cluster, {link["url"]: BODY}, {"m-100": "00"}, fetches)
    assert fetches
    assert _read(out).count(BODY) == 1
