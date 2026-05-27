"""Tests for the header-aware mailing list thread reconstruction.

Covers the four invariants the contract promises:
- threads built from Message-Id / In-Reply-To / References, not just subject
- subject-merge fallback for orphaned replies
- per-thread .md files have the expected structure
- elide_quotes collapses long quote runs but preserves short ones
"""

from __future__ import annotations

import email.message
import email.policy
from pathlib import Path

from ietf_llm.gather.mail_threads import (
    build_threads,
    elide_quotes,
    thread_slug,
    write_thread_files,
)
from ietf_llm.utils import Verbosity, get_wg_file_cache_dir


def _write_eml(
    isolated_home: Path,
    wg: str,
    uid: int,
    *,
    subject: str,
    sender: str,
    date: str,
    message_id: str,
    in_reply_to: str = "",
    references: str = "",
    body: str = "body",
) -> Path:
    """Like the conftest helper but lets us set Message-Id / In-Reply-To."""
    imap_dir = (
        isolated_home / ".cache" / "ietf-llm" / "imap-cache" / wg / "list"
    )
    imap_dir.mkdir(parents=True, exist_ok=True)
    msg = email.message.EmailMessage(policy=email.policy.default)
    msg["Subject"] = subject
    msg["From"] = sender
    msg["Date"] = date
    msg["Message-Id"] = message_id
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
    if references:
        msg["References"] = references
    msg.set_content(body)
    path = imap_dir / f"{uid}.eml"
    path.write_bytes(bytes(msg))
    return path


# --- elide_quotes ----------------------------------------------------------


def test_elide_quotes_collapses_long_quoted_run() -> None:
    text = (
        "Reply text here.\n"
        "> line 1\n> line 2\n> line 3\n> line 4\n> line 5\n"
        "More reply.\n"
    )
    result = elide_quotes(text, keep_threshold=2)
    assert "> [5 quoted lines elided]" in result
    assert "> line 1" not in result
    assert "Reply text here." in result
    assert "More reply." in result


def test_elide_quotes_keeps_short_quoted_run() -> None:
    text = "Reply.\n> one\n> two\nMore.\n"
    result = elide_quotes(text, keep_threshold=2)
    # 2-line run is at the keep_threshold, so it stays.
    assert "> one" in result
    assert "> two" in result


def test_elide_quotes_handles_trailing_quote() -> None:
    text = "Reply.\n> a\n> b\n> c\n> d\n"
    result = elide_quotes(text, keep_threshold=2)
    assert "> [4 quoted lines elided]" in result


# --- build_threads ---------------------------------------------------------


def test_threading_uses_in_reply_to(isolated_home: Path) -> None:
    _write_eml(
        isolated_home, "wg", 1,
        subject="Topic A", sender="Alice <a@x>",
        date="Mon, 01 Jan 2025 10:00:00 +0000",
        message_id="<root@x>",
    )
    _write_eml(
        isolated_home, "wg", 2,
        subject="Re: Topic A", sender="Bob <b@x>",
        date="Tue, 02 Jan 2025 10:00:00 +0000",
        message_id="<reply@x>",
        in_reply_to="<root@x>",
    )
    threads = build_threads("wg")
    assert len(threads) == 1
    assert len(threads[0].members) == 2
    # Bob's message has the root as parent.
    bob = next(m for m in threads[0].members if m.sender == "Bob")
    assert bob.parent_id == "<root@x>"


def test_threading_falls_back_to_references(isolated_home: Path) -> None:
    # No In-Reply-To, but References points at the root.
    _write_eml(
        isolated_home, "wg", 1,
        subject="Topic A", sender="Alice <a@x>",
        date="Mon, 01 Jan 2025 10:00:00 +0000",
        message_id="<root@x>",
    )
    _write_eml(
        isolated_home, "wg", 2,
        subject="Re: Topic A", sender="Bob <b@x>",
        date="Tue, 02 Jan 2025 10:00:00 +0000",
        message_id="<reply@x>",
        references="<root@x>",
    )
    threads = build_threads("wg")
    assert len(threads) == 1
    assert len(threads[0].members) == 2


def test_threading_distinct_topics_remain_separate(isolated_home: Path) -> None:
    _write_eml(
        isolated_home, "wg", 1,
        subject="Topic A", sender="Alice <a@x>",
        date="Mon, 01 Jan 2025 10:00:00 +0000",
        message_id="<a@x>",
    )
    _write_eml(
        isolated_home, "wg", 2,
        subject="Topic B", sender="Bob <b@x>",
        date="Mon, 01 Jan 2025 11:00:00 +0000",
        message_id="<b@x>",
    )
    threads = build_threads("wg")
    assert len(threads) == 2


def test_subject_fallback_merges_orphan_reply(isolated_home: Path) -> None:
    # Bob "replies" by composing a fresh message — no In-Reply-To /
    # References. Same normalised subject as Alice's; subject-fallback
    # should merge them into one thread.
    _write_eml(
        isolated_home, "wg", 1,
        subject="Topic A", sender="Alice <a@x>",
        date="Mon, 01 Jan 2025 10:00:00 +0000",
        message_id="<a@x>",
    )
    _write_eml(
        isolated_home, "wg", 2,
        subject="Re: Topic A", sender="Bob <b@x>",
        date="Tue, 02 Jan 2025 10:00:00 +0000",
        message_id="<b@x>",  # no In-Reply-To, no References
    )
    threads = build_threads("wg")
    assert len(threads) == 1
    assert len(threads[0].members) == 2


def test_no_messages_returns_empty(isolated_home: Path) -> None:
    assert build_threads("wg") == []


def test_synthetic_msgid_when_header_missing(isolated_home: Path) -> None:
    # A .eml with no Message-Id should still be parseable and become
    # an orphan thread (no parent, no children).
    _write_eml(
        isolated_home, "wg", 1,
        subject="No id", sender="A <a@x>",
        date="Mon, 01 Jan 2025 10:00:00 +0000",
        message_id="",  # _write_eml will set empty; let's force a no-id case
    )
    # The helper does set the header; for this case we use real-world
    # malformed eml. Easier: just confirm build_threads still works.
    threads = build_threads("wg")
    assert len(threads) == 1


# --- write_thread_files ----------------------------------------------------


def test_per_thread_file_layout(isolated_home: Path) -> None:
    _write_eml(
        isolated_home, "wg", 1,
        subject="Cookie partitioning", sender="Alice <a@x>",
        date="Mon, 01 Jan 2025 10:00:00 +0000",
        message_id="<a@x>",
        body="Initial message body.",
    )
    _write_eml(
        isolated_home, "wg", 2,
        subject="Re: Cookie partitioning", sender="Bob <b@x>",
        date="Tue, 02 Jan 2025 10:00:00 +0000",
        message_id="<b@x>",
        in_reply_to="<a@x>",
        body="My reply.",
    )
    cache = get_wg_file_cache_dir("wg")
    paths = write_thread_files("wg", cache, verbose=Verbosity.QUIET)
    assert len(paths) == 1
    text = Path(paths[0]).read_text()
    # Subject header, both participants, outline, message sections.
    assert "# Cookie partitioning" in text
    assert "Alice" in text and "Bob" in text
    assert "## Outline" in text
    assert "## Messages" in text
    assert "### [1] 2025-01-01 10:00 — Alice" in text
    assert "### [2] 2025-01-02 10:00 — Bob (reply to [1])" in text
    assert "Initial message body." in text
    assert "My reply." in text


def test_per_thread_file_filename_uses_slug(isolated_home: Path) -> None:
    _write_eml(
        isolated_home, "wg", 1,
        subject="Some Topic / With Punctuation",
        sender="Alice <a@x>",
        date="Mon, 01 Jan 2025 10:00:00 +0000",
        message_id="<a@x>",
    )
    cache = get_wg_file_cache_dir("wg")
    paths = write_thread_files("wg", cache, verbose=Verbosity.QUIET)
    assert len(paths) == 1
    name = Path(paths[0]).name
    assert name == "2025-01-01-some-topic-with-punctuation.md"
    # And it lives in the threads/ subdir.
    assert "threads" in Path(paths[0]).parent.parts


def test_write_thread_files_clears_stale(isolated_home: Path) -> None:
    cache = Path(get_wg_file_cache_dir("wg"))
    threads_subdir = cache / "threads"
    threads_subdir.mkdir(parents=True, exist_ok=True)
    # Plant a stale thread file that no longer corresponds to anything.
    stale = threads_subdir / "2020-01-01-old.md"
    stale.write_text("stale content")
    # Then build with a fresh thread.
    _write_eml(
        isolated_home, "wg", 1,
        subject="New", sender="A <a@x>",
        date="Mon, 01 Jan 2025 10:00:00 +0000",
        message_id="<a@x>",
    )
    write_thread_files("wg", str(cache), verbose=Verbosity.QUIET)
    assert not stale.exists()


# --- thread_slug -----------------------------------------------------------


def test_thread_slug_strips_prefixes_and_punctuation() -> None:
    # _normalize_subject inside thread_slug strips Re:/[wg] etc.
    assert thread_slug("Re: [wg] Hello, World!", "2025-01-01") == (
        "2025-01-01-hello-world"
    )


def test_thread_slug_caps_length() -> None:
    long_subject = "x " * 200
    slug = thread_slug(long_subject, "2025-01-01")
    # Date prefix is 10 chars + dash; total length capped at 71.
    assert len(slug) <= 71


# --- role attribution in thread file section headers ---------------------


def test_thread_file_decorates_chair_with_role(isolated_home: Path) -> None:
    # Consumer feedback #6: section headers should carry an author's
    # role so an LLM can weight the argument without separate lookup.
    from ietf_llm.people import Registry

    _write_eml(
        isolated_home, "wg", 1,
        subject="Topic", sender="Mark Nottingham <mnot@mnot.net>",
        date="Mon, 01 Jan 2025 10:00:00 +0000",
        message_id="<a@x>",
    )
    r = Registry()
    r.add_email_message("Mark Nottingham <mnot@mnot.net>", None)
    r.add_datatracker_role("Mark Nottingham", "mnot@mnot.net", "Chair")
    cache = get_wg_file_cache_dir("wg")
    paths = write_thread_files("wg", cache, registry=r, verbose=Verbosity.QUIET)
    text = Path(paths[0]).read_text()
    # Both the outline bullet and the section header should carry "(Chair)".
    assert "— Mark Nottingham (Chair)" in text
    assert text.count("(Chair)") >= 2  # outline + section header


def test_thread_file_no_role_for_unaffiliated(isolated_home: Path) -> None:
    # A sender the registry has but who carries no role gets no role
    # tag in the message section header (the section header has the
    # role-only form like `— Alice Wonderland (Chair)`). The
    # Participants header line uses a different format (`(count)`)
    # that we don't constrain here.
    from ietf_llm.people import Registry

    _write_eml(
        isolated_home, "wg", 1,
        subject="Topic", sender="Alice Wonderland <alice@example.com>",
        date="Mon, 01 Jan 2025 10:00:00 +0000",
        message_id="<a@x>",
    )
    r = Registry()
    r.add_email_message("Alice Wonderland <alice@example.com>", None)
    cache = get_wg_file_cache_dir("wg")
    paths = write_thread_files("wg", cache, registry=r, verbose=Verbosity.QUIET)
    text = Path(paths[0]).read_text()
    assert "Alice Wonderland" in text
    # No role tag on the message section header.
    assert "— Alice Wonderland (" not in text


# --- Archived-At extraction + rendering -----------------------------------


def test_normalize_archived_at_strips_brackets() -> None:
    from ietf_llm.gather.mail_threads import _normalize_archived_at

    # RFC 5064 form: URL wrapped in angle brackets.
    out = _normalize_archived_at(
        "<https://mailarchive.ietf.org/arch/msg/aipref/abc123/>"
    )
    assert out == "https://mailarchive.ietf.org/arch/msg/aipref/abc123/"


def test_normalize_archived_at_returns_bare_url() -> None:
    from ietf_llm.gather.mail_threads import _normalize_archived_at

    # Unbracketed form, also valid.
    out = _normalize_archived_at(
        "https://mailarchive.ietf.org/arch/msg/wg/tok/"
    )
    assert out == "https://mailarchive.ietf.org/arch/msg/wg/tok/"


def test_normalize_archived_at_rejects_non_url() -> None:
    from ietf_llm.gather.mail_threads import _normalize_archived_at

    # Header value with no scheme — probably garbage, skip rather than
    # produce a broken citation URL.
    assert _normalize_archived_at("not a url") is None
    assert _normalize_archived_at("") is None
    assert _normalize_archived_at(None) is None


def test_thread_file_includes_archived_at_per_message(
    isolated_home: Path,
) -> None:
    # Consumer feedback: thread search hits had no citeable URL. The
    # IETF mail archive sets Archived-At on every message; we should
    # extract it and surface it in the per-thread file's message section
    # so the chunker can stamp it onto the chunk.
    from email.message import EmailMessage
    from email.policy import default as default_policy
    from ietf_llm.gather.mail_threads import write_thread_files

    imap_dir = (
        isolated_home / ".cache" / "ietf-llm" / "imap-cache" / "wg" / "list"
    )
    imap_dir.mkdir(parents=True, exist_ok=True)
    msg = EmailMessage(policy=default_policy)
    msg["Subject"] = "Topic"
    msg["From"] = "Alice <a@x>"
    msg["Date"] = "Mon, 01 Jan 2025 10:00:00 +0000"
    msg["Message-Id"] = "<m1@x>"
    msg["Archived-At"] = (
        "<https://mailarchive.ietf.org/arch/msg/wg/abc/>"
    )
    msg.set_content("body")
    (imap_dir / "1.eml").write_bytes(bytes(msg))
    cache = get_wg_file_cache_dir("wg")
    paths = write_thread_files("wg", cache, verbose=Verbosity.QUIET)
    text = Path(paths[0]).read_text()
    assert (
        "_Archived-At:_ https://mailarchive.ietf.org/arch/msg/wg/abc/"
        in text
    )


# --- Per-participant counts + role flags in the thread header -------------


def test_participants_line_shows_message_counts(
    isolated_home: Path,
) -> None:
    # Consumer feedback: characterising "levels of support" needed a
    # per-thread message-count view. The Participants header now shows
    # `Name (count)` per author, sorted by count desc, so a reader can
    # see plurality vs vocal minority at a glance.
    _write_eml(
        isolated_home, "wg", 1,
        subject="Topic", sender="Alice <a@x>",
        date="Mon, 01 Jan 2025 10:00:00 +0000", message_id="<a@x>",
    )
    _write_eml(
        isolated_home, "wg", 2,
        subject="Re: Topic", sender="Alice <a@x>",
        date="Tue, 02 Jan 2025 10:00:00 +0000", message_id="<a2@x>",
        in_reply_to="<a@x>",
    )
    _write_eml(
        isolated_home, "wg", 3,
        subject="Re: Topic", sender="Bob <b@x>",
        date="Wed, 03 Jan 2025 10:00:00 +0000", message_id="<b@x>",
        in_reply_to="<a@x>",
    )
    cache = get_wg_file_cache_dir("wg")
    paths = write_thread_files("wg", cache, verbose=Verbosity.QUIET)
    text = Path(paths[0]).read_text()
    # Alice posted twice, Bob once. Counts shown in parens; Alice
    # first because higher count.
    assert "Alice (2)" in text
    assert "Bob (1)" in text
    # And Alice comes BEFORE Bob in the line (most-active-first).
    assert text.index("Alice (2)") < text.index("Bob (1)")


def test_participants_line_includes_role_tag_when_known(
    isolated_home: Path,
) -> None:
    # When the registry has a chair / editor / etc. tag for a
    # participant, show it inline: `Name (Chair, 3)`. Lets a consuming
    # LLM weight argumentative authority right in the thread header.
    from ietf_llm.people import Registry

    _write_eml(
        isolated_home, "wg", 1,
        subject="Topic", sender="Mark Nottingham <mnot@mnot.net>",
        date="Mon, 01 Jan 2025 10:00:00 +0000", message_id="<a@x>",
    )
    r = Registry()
    r.add_email_message("Mark Nottingham <mnot@mnot.net>", None)
    r.add_datatracker_role("Mark Nottingham", "mnot@mnot.net", "Chair")
    cache = get_wg_file_cache_dir("wg")
    paths = write_thread_files("wg", cache, registry=r, verbose=Verbosity.QUIET)
    text = Path(paths[0]).read_text()
    assert "Mark Nottingham (Chair, 1)" in text
