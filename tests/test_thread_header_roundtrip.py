"""The message-section header regex is shared: every writer's real output must
parse identically through the chunker, the position tally, the reply graph and
the message-citation resolver (thread files only), so `[N]` and the sender cannot drift apart.
"""

from __future__ import annotations

from pathlib import Path

from ietf_llm.embeddings.chunking import _chunk_thread_file
from ietf_llm.gather.sources.issue_files import write_issue_files
from ietf_llm.gather.sources.mail_threads import (
    THREAD_MSG_HEADER_RE,
    write_thread_files,
)
from ietf_llm.gather.sources.message_citations import build_resolver
from ietf_llm.gather.sources.pull_files import write_pull_files
from ietf_llm.log import Verbosity
from ietf_llm.mcp.topic import _parse_reply_graph
from ietf_llm.paths import get_wg_file_cache_dir
from ietf_llm.people.positions import tally_thread

from conftest import make_issue, make_pull, make_review, write_github_archive
from test_mail_threads import _write_eml


def _write_archived(isolated_home: Path, uid: int, **kwargs: str) -> None:
    """`_write_eml` plus an `Archived-At` header, so the thread file carries the
    `_Archived-At:_` line the citation resolver indexes on."""
    path = _write_eml(isolated_home, "wg", uid, **kwargs)
    url = f"<https://mailarchive.ietf.org/arch/msg/wg/tok{uid}/>"
    path.write_bytes(f"Archived-At: {url}\r\n".encode() + path.read_bytes())


def _headers(text: str) -> list[tuple[int, str]]:
    return [
        (int(m.group("idx")), m.group("sender").strip())
        for m in THREAD_MSG_HEADER_RE.finditer(text)
    ]


def _agree(text: str, filename: str) -> list[tuple[int, str]]:
    """Assert chunker and tally see the same (N, sender) sequence; return it."""
    headers = _headers(text)
    positions, _summary = tally_thread(text)
    assert [(p.chunk_idx, p.sender) for p in positions] == [
        (idx, sender.split(" (")[0]) for idx, sender in headers
    ]
    chunk_idxs = sorted({c.chunk_idx for c in _chunk_thread_file(text, filename) if c.chunk_idx})
    assert chunk_idxs == [idx for idx, _ in headers]
    return headers


def test_mail_thread_writer_output_parses_in_every_reader(
    isolated_home: Path,
) -> None:
    _write_archived(
        isolated_home, 1,
        subject="Cookie partitioning", sender="Alice <a@x>",
        date="Mon, 01 Jan 2025 10:00:00 +0000", message_id="<a@x>",
        body="Should we partition cookies?",
    )
    _write_archived(
        isolated_home, 2,
        subject="Re: Cookie partitioning", sender="Bob <b@x>",
        date="Tue, 02 Jan 2025 10:00:00 +0000", message_id="<b@x>",
        in_reply_to="<a@x>", body="+1",
    )
    _write_archived(
        isolated_home, 3,
        subject="Re: Cookie partitioning", sender="Carol <c@x>",
        date="Wed, 03 Jan 2025 10:00:00 +0000", message_id="<c@x>",
        in_reply_to="<b@x>", body="I object to this.",
    )
    cache = get_wg_file_cache_dir("wg")
    (path,) = write_thread_files("wg", cache, verbose=Verbosity.QUIET)
    text = Path(path).read_text()
    assert _agree(text, "threads/x.md") == [(1, "Alice"), (2, "Bob"), (3, "Carol")]
    assert _parse_reply_graph(text) == {1: [2], 2: [3]}
    refs = build_resolver(cache).values()
    assert sorted((r.chunk_idx, r.sender) for r in refs) == [
        (1, "Alice"), (2, "Bob"), (3, "Carol"),
    ]


def test_issue_writer_output_parses_in_chunker_and_tally(isolated_home: Path) -> None:
    write_github_archive(
        isolated_home,
        "wg",
        "org/repo",
        [
            make_issue(
                7,
                "Pick one",
                author="alice",
                comments=[
                    {"author": "bob", "createdAt": "2026-05-02T00:00:00Z", "body": "+1"},
                    {"author": "alice", "createdAt": "2026-05-03T00:00:00Z", "body": "ok"},
                ],
            )
        ],
    )
    cache = get_wg_file_cache_dir("wg")
    write_issue_files("wg", cache, verbose=Verbosity.QUIET)
    text = (Path(cache) / "issues" / "org-repo" / "7.md").read_text()
    # The opener's `_(opened issue)_` tag stays out of the sender.
    assert _agree(text, "issues/org-repo/7.md") == [(1, "alice"), (2, "bob"), (3, "alice")]


def test_pull_writer_output_parses_in_chunker_and_tally(isolated_home: Path) -> None:
    write_github_archive(
        isolated_home,
        "wg",
        "org/repo",
        [],
        pulls=[
            make_pull(
                3,
                "Third",
                author="alice",
                comments=[
                    {"author": "bob", "createdAt": "2026-05-05T00:00:00Z", "body": "+1"}
                ],
                reviews=[
                    make_review(
                        author="bob",
                        state="CHANGES_REQUESTED",
                        body="I object to this",
                        created_at="2026-05-03T00:00:00Z",
                    )
                ],
            )
        ],
    )
    cache = get_wg_file_cache_dir("wg")
    write_pull_files(cache, verbose=Verbosity.QUIET)
    text = (Path(cache) / "pulls" / "org-repo" / "3.md").read_text()
    senders = {sender for _idx, sender in _agree(text, "pulls/org-repo/3.md")}
    assert senders == {"alice", "bob"}


def test_header_with_empty_date_is_a_section_boundary() -> None:
    # A message whose timestamp rendered empty leaves `### [N]  — Sender`; the
    # chunker used to skip it, folding it into the previous message.
    text = (
        "# Subject\n\n## Messages\n\n"
        "### [1] 2026-05-14 10:00 — Alice\n\nfirst\n\n"
        "### [2]  — Bob (reply to [1])\n\nI support this.\n"
    )
    assert _agree(text, "threads/x.md") == [(1, "Alice"), (2, "Bob")]
