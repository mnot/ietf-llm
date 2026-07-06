"""Tests for `corpus.describe` — the brief 'what this corpus is about'
line shown by `ietf-llm --list` and the MCP `list_corpora` tool.
"""

from __future__ import annotations

from pathlib import Path

from ietf_llm import config, corpus

from conftest import write_cache_file

SCOPE = "gather"


def test_group_uses_stored_name(isolated_home: Path) -> None:
    write_cache_file(isolated_home, "httpbis", "digests/index.md", "# x\n")
    write_cache_file(
        isolated_home, "httpbis", "group.md", "# httpbis\n**Name:** HTTP\n"
    )
    assert corpus.describe("httpbis") == "HTTP"


def test_group_without_name_is_empty(isolated_home: Path) -> None:
    # Older caches: group.md predates the **Name:** field (charter.txt
    # marks it a group). Subject degrades to empty until re-gathered.
    write_cache_file(isolated_home, "tls", "digests/index.md", "# x\n")
    write_cache_file(isolated_home, "tls", "charter.txt", "charter\n")
    assert corpus.kind_status("tls")[0] == "group"
    assert corpus.describe("tls") == ""


def test_list_names_the_list(isolated_home: Path) -> None:
    write_cache_file(isolated_home, "last-call", "digests/index.md", "# x\n")
    config.save("last-call", SCOPE, {"mailing_list": ["last-call"]})
    assert corpus.describe("last-call") == "list last-call"


def test_list_strips_at_domain(isolated_home: Path) -> None:
    write_cache_file(isolated_home, "a2a", "digests/index.md", "# x\n")
    config.save("a2a", SCOPE, {"mailing_list": ["agent2agent@ietf.org"]})
    assert corpus.describe("a2a") == "list agent2agent"


def test_multiple_lists_pluralise(isolated_home: Path) -> None:
    write_cache_file(isolated_home, "multi", "digests/index.md", "# x\n")
    config.save("multi", SCOPE, {"mailing_list": ["ietf", "last-call"]})
    assert corpus.describe("multi") == "lists ietf, last-call"


def test_author_uses_resolved_name(isolated_home: Path) -> None:
    write_cache_file(isolated_home, "ekr", "digests/index.md", "# x\n")
    config.save(
        "ekr", SCOPE, {"author": "ekr@example.com", "author_name": "Eric Rescorla"}
    )
    assert corpus.describe("ekr") == "author: Eric Rescorla"


def test_author_falls_back_to_spec(isolated_home: Path) -> None:
    # No resolved name persisted (corpus predates the feature).
    write_cache_file(isolated_home, "ekr", "digests/index.md", "# x\n")
    config.save("ekr", SCOPE, {"author": "ekr@example.com"})
    assert corpus.describe("ekr") == "author: ekr@example.com"


def test_new_drafts_states_window(isolated_home: Path) -> None:
    write_cache_file(isolated_home, "recent", "digests/index.md", "# x\n")
    config.save("recent", SCOPE, {"new_drafts": True, "months": 3})
    assert corpus.describe("recent") == "new Internet-Drafts (last 3 mo)"


def test_single_draft(isolated_home: Path) -> None:
    write_cache_file(isolated_home, "one", "digests/index.md", "# x\n")
    config.save("one", SCOPE, {"draft": ["draft-foo-bar"]})
    assert corpus.describe("one") == "draft draft-foo-bar"


def test_two_drafts_listed(isolated_home: Path) -> None:
    write_cache_file(isolated_home, "two", "digests/index.md", "# x\n")
    config.save("two", SCOPE, {"draft": ["draft-a", "draft-b"]})
    assert corpus.describe("two") == "drafts draft-a, draft-b"


def test_many_drafts_summarised(isolated_home: Path) -> None:
    write_cache_file(isolated_home, "many", "digests/index.md", "# x\n")
    config.save("many", SCOPE, {"draft": ["draft-a", "draft-b", "draft-c"]})
    assert corpus.describe("many") == "3 drafts (draft-a, …)"


def test_synthetic_combines_sources(isolated_home: Path) -> None:
    write_cache_file(isolated_home, "x-foo", "digests/index.md", "# x\n")
    config.save(
        "x-foo", SCOPE, {"draft": ["draft-a"], "mailing_list": ["foo@ietf.org"]}
    )
    assert corpus.describe("x-foo") == "draft draft-a · list foo"


def test_github_repos_counted(isolated_home: Path) -> None:
    write_cache_file(isolated_home, "gh", "digests/index.md", "# x\n")
    config.save("gh", SCOPE, {"github": ["org/one", "org/two"]})
    assert corpus.describe("gh") == "2 GitHub repos"


# --- status_cell: never blank, non-groups say "not an effort" -------------


def test_status_cell_group_shows_its_state() -> None:
    assert corpus.status_cell("group", "active") == "active"
    assert corpus.status_cell("group", "concluded") == "concluded"
    assert corpus.status_cell("group", "bof") == "bof"


def test_status_cell_group_without_state_is_unknown() -> None:
    # Older cache: group.md predates the status field — say "unknown"
    # rather than a blank that reads as "active by omission".
    assert corpus.status_cell("group", "") == "unknown"


def test_status_cell_non_group_kinds_disclaim_chartered_status() -> None:
    # The cells a reader must never mistake for an active Working Group.
    assert "not an IETF effort" in corpus.status_cell("synthetic", "")
    assert "not a chartered group" in corpus.status_cell("list", "")
    assert "not a chartered group" in corpus.status_cell("custom", "")


def test_persist_author_name_records_resolved_identity(isolated_home: Path) -> None:
    from ietf_llm.gather import sequencer as main_mod

    config.save("ekr", SCOPE, {"author": "ekr@example.com"})
    main_mod._persist_author_name("ekr", "Eric Rescorla")
    assert config.load("ekr", SCOPE) == {
        "author": "ekr@example.com",
        "author_name": "Eric Rescorla",
    }
