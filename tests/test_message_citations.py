"""Tests for the message-citation graph (gather + reader tool).

Covers the writer→reader round-trip: build thread files, scan them into
the resolver/citation graph, render the digest, and read it back through
`find_message_citations`.
"""

from __future__ import annotations

from pathlib import Path

from ietf_llm import mcp_server
from ietf_llm.gather.message_citations import (
    canonical_archive_url,
    scan_message_citations,
    write_message_citations_digest,
)

from conftest import write_cache_file

_AURL = "https://mailarchive.ietf.org/arch/msg/tls/TOKENA"
_EXT = "https://mailarchive.ietf.org/arch/msg/uta/EXTERNALTOK"


def _origin(archived: str = _AURL) -> str:
    return (
        "# Origin\n\n## Messages\n\n"
        "### [1] 2026-01-01 10:00 — Alice\n\n"
        f"_Subject:_ Origin subject\n_Archived-At:_ {archived}\n\n"
        "the origin body.\n"
    )


def _fork() -> str:
    # Bob's message footnotes Alice's URL *with a trailing slash* (the
    # body form) plus an external (uta) link; it also carries its own
    # Archived-At, which must not count as a self-citation.
    return (
        "# Fork\n\n## Messages\n\n"
        "### [1] 2026-02-01 09:00 — Bob\n\n"
        "_Subject:_ Fork subject\n"
        "_Archived-At:_ https://mailarchive.ietf.org/arch/msg/tls/TOKENB\n\n"
        f"Forking per [1] {_AURL}/ and see also {_EXT}\n"
        "> quoted: ignore https://mailarchive.ietf.org/arch/msg/tls/QUOTED\n"
    )


def test_canonical_archive_url_within_scheme() -> None:
    base = "https://mailarchive.ietf.org/arch/msg/tls/Tok"
    for variant in (
        base + "/",
        base.replace("https://", "http://"),
        base.replace("mailarchive", "www.mailarchive"),
        f"<{base}>",
        base + "#frag",
        base + ").",  # trailing sentence punctuation
    ):
        assert canonical_archive_url(variant) == base, variant
    # Path case (a w3.org/mid Message-ID) is preserved.
    mid = "https://www.w3.org/mid/AbCd@Example.com"
    assert canonical_archive_url(mid) == "https://w3.org/mid/AbCd@Example.com"


def test_scan_resolves_classifies_and_skips_self_and_quoted(
    isolated_home: Path,
) -> None:
    write_cache_file(isolated_home, "wg", "threads/2026-01-01-origin.md", _origin())
    write_cache_file(isolated_home, "wg", "threads/2026-02-01-fork.md", _fork())
    cache = mcp_server._files_dir("wg")
    cits = scan_message_citations(cache)

    resolved = [c for c in cits if c.target]
    external = [c for c in cits if not c.target]

    # Bob → Alice resolves despite the trailing slash on the body URL.
    assert any(
        c.src_file == "threads/2026-02-01-fork.md"
        and c.target is not None
        and c.target.file == "threads/2026-01-01-origin.md"
        and c.target.chunk_idx == 1
        for c in resolved
    )
    # The uta link is external (not gathered here).
    assert any(c.url.endswith("EXTERNALTOK") for c in external)
    # Bob's own Archived-At is not a self-citation, and the quoted link
    # is excluded.
    assert not any(c.url.endswith("TOKENB") for c in cits)
    assert not any(c.url.endswith("QUOTED") for c in cits)


def test_digest_and_tool_roundtrip(isolated_home: Path) -> None:
    write_cache_file(isolated_home, "wg", "threads/2026-01-01-origin.md", _origin())
    write_cache_file(isolated_home, "wg", "threads/2026-02-01-fork.md", _fork())
    cache = mcp_server._files_dir("wg")
    write_message_citations_digest(cache, scan_message_citations(cache))

    # Outbound for Bob's message: the resolved Alice target + the external.
    out = mcp_server.tool_find_message_citations(
        "wg", "threads/2026-02-01-fork.md", 1
    )
    assert "Outbound" in out
    assert "threads/2026-01-01-origin.md` [chunk 1]" in out
    assert "Alice" in out  # target sender rendered
    assert "EXTERNALTOK" in out
    assert "gather `uta`?" in out  # external list hint

    # Inbound for Alice's message: Bob cites it.
    inb = mcp_server.tool_find_message_citations(
        "wg", "threads/2026-01-01-origin.md", 1
    )
    assert "Inbound" in inb
    assert "threads/2026-02-01-fork.md` [chunk 1]" in inb


def test_tool_missing_digest_message(isolated_home: Path) -> None:
    write_cache_file(isolated_home, "wg", "charter.txt", "x")
    out = mcp_server.tool_find_message_citations("wg", "threads/x.md")
    assert "No message-citations digest" in out


def test_write_digest_removes_stale_on_empty(isolated_home: Path) -> None:
    # Gather overwrites digests file-by-file (no digests/ wipe), so a
    # re-gather that drops to zero citations must delete the old digest
    # rather than leave it serving stale edges. Build one, then re-run
    # the writer with empty input and assert the file is gone and the
    # tool reports the no-digest message.
    write_cache_file(isolated_home, "wg", "threads/2026-01-01-origin.md", _origin())
    write_cache_file(isolated_home, "wg", "threads/2026-02-01-fork.md", _fork())
    cache = mcp_server._files_dir("wg")
    path = write_message_citations_digest(cache, scan_message_citations(cache))
    assert path is not None and Path(path).is_file()
    # Re-gather with nothing cited (narrower window / quotes only).
    assert write_message_citations_digest(cache, []) is None
    assert not Path(path).exists()
    out = mcp_server.tool_find_message_citations("wg", "threads/2026-02-01-fork.md", 1)
    assert "No message-citations digest" in out


def test_tool_no_citations_for_file(isolated_home: Path) -> None:
    # Digest exists but the queried file is not in the graph.
    write_cache_file(isolated_home, "wg", "threads/2026-01-01-origin.md", _origin())
    write_cache_file(isolated_home, "wg", "threads/2026-02-01-fork.md", _fork())
    cache = mcp_server._files_dir("wg")
    write_message_citations_digest(cache, scan_message_citations(cache))
    out = mcp_server.tool_find_message_citations("wg", "threads/nope.md")
    assert "No message citations recorded" in out
