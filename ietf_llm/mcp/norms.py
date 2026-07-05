"""IETF norms tools (interpretation + participation)."""

from __future__ import annotations

from importlib import resources
from typing import TYPE_CHECKING

from .common import _offload, _strip_frontmatter

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP  # pragma: no cover


def _read_bundled_skill_body(skill: str) -> str:
    """Return the body (frontmatter stripped) of a bundled skill's `SKILL.md`,
    or a reinstall hint if it's missing.

    One source of truth: the same `data/skills/<skill>/SKILL.md` files that
    `--install-skills` installs are what the MCP norms tools (and the server
    `instructions` field) serve — so the guidance can't drift between the
    skill a Claude/Codex/Gemini/opencode user sees and the tool output."""
    try:
        path = resources.files("ietf_llm").joinpath(f"data/skills/{skill}/SKILL.md")
        return _strip_frontmatter(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError):
        return (
            f"(the {skill} skill is missing from the installed package — "
            "try reinstalling: pipx install --force ietf-llm)"
        )


def tool_read_interpretation_norms() -> str:
    """Return the `ietf-interpreting` skill body — interpretive norms for
    reading a corpus (consensus, who-speaks-for-whom, list-vs-meeting).

    The norms also ship as a standalone skill that auto-triggers on
    "what did the WG decide / who supports what"; this tool is the MCP
    surface for the same content, pulled on demand by clients that reach
    it as a tool rather than a skill.
    """
    return _read_bundled_skill_body("ietf-interpreting")


def tool_read_participation_norms() -> str:
    """Return the `ietf-contributing` skill body — norms for helping a human
    contribute to a corpus (drafting mailing-list messages, GitHub issues/comments,
    other discussion), the write-side companion to the reading norms.

    Pulled on demand when the question shifts from interpreting the
    record to composing something that goes into it under a person's
    name. Authoring Internet-Drafts is out of scope of the doc.
    """
    return _read_bundled_skill_body("ietf-contributing")


def register(server: "FastMCP") -> None:
    @server.tool()
    async def read_ietf_interpretation_norms() -> str:
        """Return the interpretive norms for reading an IETF corpus:
        how consensus works (chair-declared, not vote-counted), how
        to attribute positions (individuals, not employers), and
        why mailing-list confirmation — not meeting agreement —
        is the binding decision.

        **Call this before writing any sentence that asserts a collective
        outcome** — that something is settled, decided, resolved, agreed,
        or rejected, that there is consensus, or what "the WG thinks/wants".
        The trigger is grammatical, not a self-assessment: reporting what a
        named individual said is free; any claim about where the *group*
        landed is gated, however confident you are. Not needed for catalogue
        lookups (`read_digest`), text fetches (`read_file_section`), or
        structural questions (`overview`). The content is stable across
        corpora — one call per session is enough. For the write side
        (drafting a contribution), see `read_ietf_participation_norms`.
        """
        return await _offload(tool_read_interpretation_norms)

    @server.tool()
    async def read_ietf_participation_norms() -> str:
        """**Mandatory before drafting any contribution** — before you
        write a single line of a mailing-list message, a reply in a thread, a GitHub
        issue or comment, a review, or a consensus/position statement:
        any text that will go into the record under a person's name. Read
        this FIRST, not as an afterthought; reading the *interpretation*
        norms does not substitute. The moment a task turns from reading the
        corpus to producing a contribution — "write/draft an email to the
        working group", "reply to this thread", "respond on the list",
        "file/comment on an issue", "compose a mailing-list message" — call this.

        Covers: the human is accountable and sends (you only draft),
        disclosing AI involvement and how closely supervised, the register
        to match (terse, technical, no AI tells), staying on-charter,
        engaging existing work rather than dropping new ideas cold, not
        re-litigating settled questions or manufacturing consensus signal,
        and where AI help is uncontroversial (summarise/translate, explain
        ABNF/YANG). Authoring Internet-Drafts is out of scope. Stable
        across corpora — one call per session is enough. For
        reading/characterising a corpus, see
        `read_ietf_interpretation_norms`.
        """
        return await _offload(tool_read_participation_norms)
