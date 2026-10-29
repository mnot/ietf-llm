"""Heuristic position extraction from mailing-list / issue messages.

Consumer pain point: when characterising "levels of support" in a WG
debate, an LLM has to read N message chunks and tally them by hand,
which is both expensive in context and prone to taking the chair's
characterisation at face value. This module gives the MCP tool surface
a primitive — `tally_positions(wg, file)` — that walks one thread or
issue file and tags each message author's stance.

The extraction is deliberately **heuristic**, not semantic:

  - We match canonical IETF position phrasings near the message start:
    `+1`, `-1`, `I support`, `I object`, `I agree`, `I concur`, `LGTM`,
    `I oppose`, `DISCUSS` (IESG ballot), `Support`, `Oppose`, etc.
  - Conditional support — `I support with…`, `agree but…` — gets its
    own bucket so a tally doesn't conflate "yes" with "yes if".
  - A match followed by a hedge in the same sentence ("I support this,
    but …"), a bare `I agree`, or a poll marker with a qualifier ("my
    second choice") is kept at low confidence; a negated intent ("not in
    favor of option 2") is not a position at all.
  - Anything not matching shows as `no-position`. That's honest: the
    message may be a technical clarification, a question, or a stance
    expressed in non-standard phrasing. The tool reports the unmatched
    count so the consumer can decide whether to dig in.

Quoted lines (anything starting with `>`) are stripped before matching
so we don't read a quote of someone else's `+1` as the current author
agreeing. Bodies are otherwise read as-is from the thread file (which
has already been quote-elided at gather time, leaving inline `>`
citations untouched).

This is intentionally conservative: a high-precision/low-recall counter
beats a high-recall one because the consumer's downstream claim is
*the count*. We'd rather under-count than over-count.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from ..digest.query import parse_md_tables
from ..gather.sources.mail_threads import THREAD_MSG_HEADER_RE
from ..paths import digest_path
from .poll_choice import (
    HEDGE_RE,
    contrast_follows,
    extract_poll_choice,
    first_unnegated,
    rest_of_sentence,
)

# Number of leading non-quoted lines we scan for the strong patterns.
# Long messages bury their position under preamble; a real declaration
# normally lives in the first few lines. 12 is enough for a greeting
# + opening sentence without dragging in detailed argumentation.
_LEADING_LINES = 12
# Window of non-quoted text we scan for the weak (low-confidence)
# patterns. Picked to be generous enough to catch "I support" buried
# below a quoted snippet, while small enough to avoid matching a
# response to someone else's `+1`.
_WEAK_SCAN_CHARS = 800


# --- Pattern catalogues ----------------------------------------------------
#
# Strong patterns are anchored at line start (after quote-strip). High
# confidence: a message that opens "I support X" is virtually never
# expressing the opposite. Weak patterns scan the message body and are
# marked as low confidence — they can land on quoted-out fragments or
# rhetorical phrasing the author then walks back.

#
# `bare` marks the self-contained votes (`+1`, a lone "Support"); every other
# opener is checked by `_support_confidence`: a bare "I agree" is often
# sub-point agreement that the rest of the sentence then qualifies, so only
# `I agree with|that this|to` can be high, and any hedge in the same sentence
# ("LGTM, but …"), or a negative word after an "I agree", drops a match to
# low. "Support" counts as strong only as the whole line — mid-sentence
# "support …" is usually a verb ("support FTP URIs"); see `_SUPPORT_IDIOM`.
_STRONG_SUPPORT = re.compile(
    r"""^\s*(?:
        (?P<bare>\+1\s*[.!]?\s*$)                           # bare +1
      | I\s+(?:
            (?:strongly\s+)?support
          | concur | approve | endorse
          | am\s+in\s+favo[u]?r
          | (?P<agree>agree(?P<agree_ok>\s+(?:with|that\s+this|to)\b)?)
        )\b
      | (?P<bare_support>Support\s*[.!]?\s*$)              # a vote on its own line
      | LGTM\b                                             # looks-good-to-me
      | Ship\s+it\b
      | Yes,?\s+(?:I\s+(?:support|agree)|let'?s|we\s+should)
    )
    """,
    re.IGNORECASE | re.MULTILINE | re.VERBOSE,
)

# Capitalised "Support for -04 …" / "Support -" / "support adopting this work"
# are real votes in call-for-adoption and last-call threads, but too close to
# the verb ("Support for X is lacking") to rank with the strong openers: low
# confidence.
_SUPPORT_IDIOM = re.compile(
    r"""^\s*(?:
        (?-i:Support)\s*(?:for\b|[-–—:])
      | support\s+(?:adopt(?:ing|ion)|publication|the\s+draft
                   |this\s+(?:work|draft|document|proposal))\b
    )""",
    re.IGNORECASE | re.MULTILINE | re.VERBOSE,
)
# "I agree that this is a terrible idea" has no hedge word but is not support:
# agreement with something negative is at best ambiguous.
_NEGATIVE_AGREE_RE = re.compile(
    r"\b(?:terrible|awful|bad|wrong|mistake|worse|harmful|dangerous|broken|flawed"
    r"|unwise|useless|pointless|objections?|concerns?|problems?|issues?)\b",
    re.IGNORECASE,
)

_STRONG_OPPOSE = re.compile(
    r"""^\s*(
        -1\s*[.!]?\s*$                                     # bare -1
      | I\s+(?:object|oppose|disagree|don'?t\s+support)
      | I\s+am\s+(?:opposed|against)
      | (?:Oppose|Object)d?\.?\s*$                         # standalone Oppose.
      | (?-i:DISCUSS)\b                                    # IESG ballot; upper-case only
      | No,?\s+(?:I\s+(?:object|don'?t|disagree)|this\s+(?:should\s+not|must\s+not))
    )
    """,
    re.IGNORECASE | re.MULTILINE | re.VERBOSE,
)

_STRONG_CONDITIONAL = re.compile(
    r"""^\s*(
        I\s+support\s+(?:this\s+)?(?:with|but|if|provided|on\s+condition|only\s+if)
      | I\s+agree\s+(?:with\s+)?(?:but|provided|if|only\s+if|with\s+caveat)
      | (?:Support|Agree)\s+with\s+(?:caveat|reservation|condition|the\s+caveat)
    )
    """,
    re.IGNORECASE | re.MULTILINE | re.VERBOSE,
)

# Weaker patterns — matched against the body proper (not just the first
# few lines) when the strong patterns miss. Conservative wording
# because a body-anywhere match catches rhetorical / hypothetical uses
# that the strong-line-start patterns wouldn't.
# Includes WGLC-review idioms ("ready to progress", "in good shape",
# "looks good", "no objection"): a Working Group Last Call rarely uses
# `+1`/`-1`, so without these a clearly-supportive review thread tallied
# 0% coverage. Low confidence, since a body-anywhere match also catches
# negated ("not ready to progress") or quoted uses.
_WEAK_SUPPORT = re.compile(
    r"""\b(?:
        I\s+(?:do\s+)?support
      | in\s+favo[u]?r\s+of
      | fine\s+with\s+me
      | ready\s+to\s+(?:progress|advance|publish|be\s+published|go\s+forward|ship)
      | ready\s+for\s+(?:publication|last\s+call|wglc|the\s+rfc\s+editor)
      | (?:in\s+)?good\s+shape
      | looks?\s+good(?:\s+to\s+me)?
      | no\s+objection(?:s)?
    )\b""",
    re.IGNORECASE | re.VERBOSE,
)
_WEAK_OPPOSE = re.compile(
    r"\b(?:I\s+(?:do\s+)?(?:oppose|object)|strongly\s+against|cannot\s+support)\b",
    re.IGNORECASE,
)


def _support_confidence(match: re.Match[str], text: str) -> str:
    """ "high" for an unqualified strong-support opener, else "low"."""
    if match.group("bare") or match.group("bare_support"):
        return "high"
    if match.group("agree") and not match.group("agree_ok"):
        return "low"
    rest = rest_of_sentence(text, match.end())
    if HEDGE_RE.search(rest) or contrast_follows(text, match.end()):
        return "low"
    if match.group("agree") and _NEGATIVE_AGREE_RE.search(rest):
        return "low"
    return "high"


# Chair-decision language. When a chair (role tag carries "Chair")
# posts a message containing one of these phrases, it's likely a
# procedural declaration — consensus call, adoption call, WGLC
# announcement, "no rough consensus" outcome, thread closure. The
# consumer feedback that prompted this surfacing was that a chair's
# "rough consensus" call was at position 40 of 42 in a 42-message
# thread and could easily have been missed in scrolling.
_CHAIR_DECISION_RE = re.compile(
    r"""\b(
        (?:no\s+)?rough\s+consensus
      | consensus\s+call
      | (?:working\s+group\s+)?last\s+call
      | WGLC
      | adoption\s+call
      | adopt(?:ing|ed)\s+as\s+(?:a\s+)?(?:working\s+group|WG)
      | the\s+(?:WG|working\s+group)\s+(?:has\s+)?(?:agreed|decided|adopted)
      | (?:I|we|the\s+chairs?)\s+(?:hereby\s+)?(?:declare|conclude|close|are\s+closing)
      | (?:has\s+been|is\s+(?:now\s+)?)\s+(?:closed|concluded)
      | I'?ll?\s+(?:be\s+)?closing\s+(?:this|the)
    )\b""",
    re.IGNORECASE | re.VERBOSE,
)


@dataclass
class ChairStatement:
    """One chair message that looks like a procedural declaration."""

    sender: str
    chunk_idx: int
    excerpt: str  # ~400-char window around the matched phrase
    matched_phrase: str  # the exact substring that triggered the match
    role: str  # the role tag from the registry ("Chair", "Chair/Author")


# Sender display includes a role tag in parens — e.g. "Alice (Chair)".
# Strip that for the tally name; the role is recovered separately from
# the Registry.
_SENDER_ROLE_SUFFIX = re.compile(r"\s*\([^)]+\)\s*$")


@dataclass
class Position:
    """One author's stance on the file's topic, with the matched phrase."""

    sender: str  # canonical name as it appears in the section header
    label: str  # "support" / "oppose" / "conditional" / "poll" / "no-position"
    confidence: str  # "high" / "low" / "" (no-position)
    excerpt: str  # the matching line, trimmed; "" for no-position
    chunk_idx: int  # message index in the file (= chunk_idx)
    message_count_in_file: int  # how many messages this sender posted
    # Only set when label == "poll": the option the author picked,
    # uppercased (so "Option 2", "#2", "2" all normalise to "2";
    # "option B" → "B"). Used by the renderer to group poll responses
    # by choice ("Option 1: 5, Option 2: 8, …").
    poll_choice: Optional[str] = None


def _strip_quoted(body: str) -> str:
    """Drop quoted lines (`>` after optional whitespace) so the pattern
    matcher doesn't read a quote of someone else's `+1` as the current
    author agreeing. Run before strong / weak matching.
    """
    out: List[str] = []
    for line in body.splitlines():
        if line.lstrip().startswith(">"):
            continue
        out.append(line)
    return "\n".join(out)


def _strip_metadata_lines(body: str) -> str:
    """Drop the _Subject:_ / _Archived-At:_ italic preamble that
    `mail_threads._render_thread` puts at the top of each section. They
    aren't quoted but they're not part of the author's text either —
    leaving them in would let a strong pattern match a subject like
    `Re: I support adoption` as the body's position.
    """
    out: List[str] = []
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("_Subject:_") or stripped.startswith("_Archived-At:_"):
            continue
        out.append(line)
    return "\n".join(out)


def _excerpt_for(match: re.Match[str], text: str) -> str:
    """Pull the matching line out of `text`, trimmed to 200 chars."""
    start = text.rfind("\n", 0, match.start()) + 1
    end = text.find("\n", match.end())
    if end < 0:
        end = len(text)
    line = text[start:end].strip()
    if len(line) > 200:
        line = line[:199] + "…"
    return line


def extract_position(  # pylint: disable=too-many-return-statements
    body: str, allow_bare_hash: bool = True
) -> Tuple[str, str, str, Optional[str]]:
    """Return (label, confidence, excerpt, poll_choice) for one message
    body. `poll_choice` is only non-None when label == "poll".
    `allow_bare_hash` is forwarded to the poll detector (see
    `poll_choice.extract_poll_choice`); callers pass False for GitHub issue / PR files.

    Order of precedence:
      1. Strong conditional (overrides plain support if both match)
      2. Poll-syntax (option N / #N / I prefer N) — option polls are
         common WG decision mechanisms and the choices are explicit;
         catching them before support/oppose avoids "I'm for 2" being
         miscounted as plain support.
      3. Strong support / oppose at line start in the first N lines
      4. Weak support / oppose anywhere in the first window
      5. no-position
    """
    cleaned = _strip_quoted(_strip_metadata_lines(body))
    leading_text = "\n".join(cleaned.splitlines()[:_LEADING_LINES])

    cond_match = _STRONG_CONDITIONAL.search(leading_text)
    if cond_match:
        return (
            "conditional",
            "high",
            _excerpt_for(cond_match, leading_text),
            None,
        )

    poll = extract_poll_choice(leading_text, allow_bare_hash)
    if poll is not None:
        choice, match, poll_conf = poll
        return ("poll", poll_conf, _excerpt_for(match, leading_text), choice)

    support_match = _STRONG_SUPPORT.search(leading_text)
    oppose_match = _STRONG_OPPOSE.search(leading_text)
    # When both somehow fire (rare — e.g. "I support X. I object to Y"),
    # we surface the *first* one, since that's the framing of the post.
    if support_match and (
        not oppose_match or support_match.start() < oppose_match.start()
    ):
        return (
            "support",
            _support_confidence(support_match, leading_text),
            _excerpt_for(support_match, leading_text),
            None,
        )
    if oppose_match:
        return ("oppose", "high", _excerpt_for(oppose_match, leading_text), None)
    idiom = _SUPPORT_IDIOM.search(leading_text)
    if idiom:
        return ("support", "low", _excerpt_for(idiom, leading_text), None)

    # Weak patterns — scan a bigger window in the body proper, marked
    # low confidence so the consumer can weight accordingly. A negated hit
    # ("not ready to progress") does not count.
    window = cleaned[:_WEAK_SCAN_CHARS]
    weak_support = first_unnegated(_WEAK_SUPPORT, window)
    weak_oppose = first_unnegated(_WEAK_OPPOSE, window)
    if weak_support and not weak_oppose:
        return ("support", "low", _excerpt_for(weak_support, window), None)
    if weak_oppose and not weak_support:
        return ("oppose", "low", _excerpt_for(weak_oppose, window), None)

    return ("no-position", "", "", None)


def _split_messages(text: str) -> List[Tuple[int, str, str]]:
    """Walk a thread / issue file's text. Returns [(chunk_idx, sender,
    body)] in document order.

    `chunk_idx` is the message number (= chunk_idx in the embedding
    index). `sender` is the section header's name part with any
    trailing `(Role)` suffix stripped. `body` is everything between
    this section's header and the next.
    """
    matches = list(THREAD_MSG_HEADER_RE.finditer(text))
    out: List[Tuple[int, str, str]] = []
    for i, match in enumerate(matches):
        msg_idx = int(match.group("idx"))
        sender_raw = match.group("sender").strip()
        sender = _SENDER_ROLE_SUFFIX.sub("", sender_raw).strip()
        body_start = match.end()
        body_end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[body_start:body_end]
        out.append((msg_idx, sender, body))
    return out


def extract_chair_statements(
    file_text: str,
    role_lookup: Dict[str, str],
) -> List[ChairStatement]:
    """Walk a thread / issue file and return chair messages that look
    like procedural declarations (consensus calls, WGLC announcements,
    "rough consensus" outcomes, thread closures).

    Surfaces the highest-signal posts in a long contentious thread:
    a chair's verdict can live at message 40 of 42 and the consumer
    must not miss it because they got tired of scrolling. The
    returned list is in document order, which for thread files is
    chronological — early procedural moves (adoption call, WGLC
    announcement) precede later ones (resolution, closure).

    `role_lookup` maps canonical sender names to their role tag (from
    the people digest). Only messages from senders whose role
    contains "Chair" are considered.
    """
    if not role_lookup:
        return []
    out: List[ChairStatement] = []
    for msg_idx, sender, body in _split_messages(file_text):
        role = role_lookup.get(sender, "")
        if "Chair" not in role:
            continue
        cleaned = _strip_quoted(_strip_metadata_lines(body))
        match = _CHAIR_DECISION_RE.search(cleaned)
        if match is None:
            continue
        # ~400-char window centred on the match. Generous because the
        # surrounding sentence carries context (which draft, what
        # decision); a strict line excerpt would often clip mid-claim.
        start = max(0, match.start() - 120)
        end = min(len(cleaned), match.end() + 300)
        excerpt = cleaned[start:end].strip()
        if len(excerpt) > 500:
            excerpt = excerpt[:499] + "…"
        out.append(
            ChairStatement(
                sender=sender,
                chunk_idx=msg_idx,
                excerpt=excerpt,
                matched_phrase=match.group(0),
                role=role,
            )
        )
    return out


def tally_thread(
    file_text: str, allow_bare_hash: bool = True
) -> Tuple[List[Position], Dict[str, int]]:
    """Walk one thread / issue file's text and return per-message
    positions plus a label→count summary.

    The summary counts every Position (including no-position rows) so
    the consumer can see what fraction of messages the heuristic
    couldn't classify. That coverage number is load-bearing — the
    user feedback that prompted this tool explicitly flagged "I
    relayed the chair's characterisation instead of counting" as the
    accuracy gap to close.

    `allow_bare_hash=False` (GitHub issue / PR files) stops a line-start
    `#N` cross-reference from being read as a poll vote.
    """
    # Single pass: split_messages is O(file_size) — re-running it for
    # the message-count tally was doubling the work on large WGLC
    # threads (the 282-message TLS MLKEM WGLC was timing out the MCP
    # client). Build the messages list once, then derive both
    # msg_counts and positions from it.
    messages = _split_messages(file_text)
    msg_counts: Dict[str, int] = {}
    for _idx, sender, _body in messages:
        msg_counts[sender] = msg_counts.get(sender, 0) + 1
    positions: List[Position] = []
    for msg_idx, sender, body in messages:
        label, conf, excerpt, poll_choice = extract_position(body, allow_bare_hash)
        positions.append(
            Position(
                sender=sender,
                label=label,
                confidence=conf,
                excerpt=excerpt,
                chunk_idx=msg_idx,
                message_count_in_file=msg_counts[sender],
                poll_choice=poll_choice,
            )
        )
    summary: Dict[str, int] = {
        "support": 0,
        "oppose": 0,
        "conditional": 0,
        "poll": 0,
        "no-position": 0,
    }
    for pos in positions:
        summary[pos.label] = summary.get(pos.label, 0) + 1
    return positions, summary


# Coverage below which the support/oppose counts are withheld: the
# heuristic missed most of the thread, so quoting "N oppose" would
# misrepresent a discussion whose positions live in prose. Applied only to
# non-trivial threads — a 3-message thread at 33% isn't a signal worth a
# warning. Chair statements are never withheld; poll rows carry a caveat
# instead (see `render_tally`).
_LOW_COVERAGE_PCT = 25
_LOW_COVERAGE_MIN_MESSAGES = 5


def _is_low_coverage(total: int, coverage_pct: int) -> bool:
    """Whether the keyword tally classified too little of a non-trivial
    thread for its support/oppose counts to be quotable."""
    return total >= _LOW_COVERAGE_MIN_MESSAGES and coverage_pct < _LOW_COVERAGE_PCT


def _coverage_banner(
    total: int, classified: int, coverage_pct: int, has_chair_statements: bool
) -> List[str]:
    """A prominent top-of-output warning when coverage is too low to trust
    the counts. Empty when coverage is adequate. Points the reader at the
    grounded signals (chair statements, the messages themselves) instead of
    the numbers, which are withheld below. The Chair statements clause is only
    included when that section is actually rendered — chair prose can stay
    elided in older caches, leaving no chair statements to point at."""
    if not _is_low_coverage(total, coverage_pct):
        return []
    if coverage_pct == 0:
        lead = (
            f"**The keyword heuristic classified none of the {total} messages.** "
            "The support / oppose counts are withheld below and tell you nothing "
            "about where the thread stands"
        )
    else:
        lead = (
            f"**Low coverage ({coverage_pct}%):** the keyword heuristic classified "
            f"only {classified}/{total} messages, so the support / oppose counts "
            "are withheld below"
        )
    if has_chair_statements:
        pointer = (
            "read the **Chair statements** section and the messages at the chunk "
            "indices listed below"
        )
    else:
        pointer = "read the messages at the chunk indices listed below"
    return [
        f"> ⚠️ {lead} — this is **not** evidence the thread is unopinionated. "
        "IETF participants here likely express support or opposition in prose, "
        f"which this matcher cannot see. For the real state, {pointer} (or use "
        "`read_topic` / `read_file_section`). Do not quote the counts.",
        "",
    ]


def _render_poll_section(
    positions: List[Position], low_coverage: bool, coverage_pct: int
) -> List[str]:
    """The per-option poll section: keyword matches with a confidence note,
    plus a caveat when the thread's overall coverage is low. Empty when the
    thread has no poll rows."""
    poll_positions = [p for p in positions if p.label == "poll"]
    if not poll_positions:
        return []
    by_choice: Dict[str, List[Position]] = {}
    for pos in poll_positions:
        by_choice.setdefault(pos.poll_choice or "?", []).append(pos)
    out = [
        f"## Poll choices ({len(poll_positions)})\n",
        "_Per-option tally of keyword matches (`low` = a qualifier such as "
        '"second choice" or "but" followed the marker). The same author '
        "may show up under multiple choices if they voted more than once — "
        "the most recent message wins for the by-author rollup below._\n",
    ]
    if low_coverage:
        out.append(
            f"> ⚠️ Coverage is low ({coverage_pct}%): most messages in this "
            "thread were not classified, so votes cast in prose are missing "
            "from these counts. Treat them as a lower bound on explicit "
            "statements, not the split of the room.\n"
        )

    def _choice_sort(k: str) -> Tuple[int, str]:
        # Numerics ascending, then alphabetic.
        return (0, k.zfill(8)) if k.isdigit() else (1, k)

    for choice in sorted(by_choice, key=_choice_sort):
        voters = by_choice[choice]
        names = ", ".join(sorted({p.sender for p in voters}))
        n_low = sum(1 for p in voters if p.confidence == "low")
        low_note = f" ({n_low} low confidence)" if n_low else ""
        out.append(
            f"- **Option {choice}** — {len(voters)} response(s){low_note}: {names}"
        )
    out.append("")
    return out


def _confidence_split(positions: List[Position], label: str) -> Tuple[int, int]:
    """(high, low) counts for `label`. Low-confidence rows are hedged or
    idiom-only matches, so they are reported beside the high count, never
    folded into one headline number."""
    rows = [p for p in positions if p.label == label]
    low = sum(1 for p in rows if p.confidence == "low")
    return len(rows) - low, low


def render_tally(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    file: str,
    positions: List[Position],
    summary: Dict[str, int],
    role_lookup: Optional[Dict[str, str]] = None,
    affiliation_lookup: Optional[Dict[str, str]] = None,
    chair_statements: Optional[List[ChairStatement]] = None,
) -> str:
    """Render the tally as a markdown response. `role_lookup` and
    `affiliation_lookup` map canonical sender names to inline tags
    (e.g. "Chair", "Cloudflare") that get appended to each entry.
    Either or both may be empty; missing entries are silently skipped.
    """
    total = len(positions)
    c_count = summary.get("conditional", 0)
    n_count = summary.get("no-position", 0)
    coverage_pct = int(100 * (total - n_count) / total) if total else 0

    out: List[str] = []
    out.append(f"# Position tally: `{file}`\n")
    # Banner first when coverage is low — it must be read before the (withheld)
    # numbers so a 0 is never lifted out of context.
    out.extend(
        _coverage_banner(total, total - n_count, coverage_pct, bool(chair_statements))
    )
    out.append(
        "_Best for **surfacing the chair's procedural call** (see Chair "
        "statements) and for **explicit option polls** (`option N` / `#N` / "
        "`I prefer N`). Every row below is a keyword match, not a read of the "
        "message, and carries a confidence tag: verify poll rows against the "
        "messages before quoting a split. The support/oppose tally "
        "matches canonical phrasings (`+1`, `-1`, `I support`, `I object`, "
        "`LGTM`, `DISCUSS`, …) near the start of each message and **misses "
        "prose-form positions** — a thread can be full of clearly-argued "
        "opposition and still tally as no-position. A count is meaningful only "
        "when coverage is high; otherwise read the messages. Never quote a "
        "low-coverage count as the level of support._\n"
    )
    p_count = summary.get("poll", 0)
    out.append("**Summary:**")
    if _is_low_coverage(total, coverage_pct):
        out.append(
            "- Support / Conditional / Oppose: _withheld — coverage too low for "
            "the counts to mean anything (see warning above; read the messages)._"
        )
    else:
        s_high, s_low = _confidence_split(positions, "support")
        o_high, o_low = _confidence_split(positions, "oppose")
        out.append(
            f"- Support: **{s_high}** high confidence, **{s_low}** low confidence"
        )
        out.append(f"- Conditional: **{c_count}**  (yes-but-only-if)")
        out.append(
            f"- Oppose: **{o_high}** high confidence, **{o_low}** low confidence"
        )
    if p_count:
        p_low = sum(1 for p in positions if p.label == "poll" and p.confidence == "low")
        low_note = f", {p_low} low confidence" if p_low else ""
        out.append(
            f"- Poll choices: **{p_count}**  (`option N` / `#N` / "
            f"`I prefer N` keyword matches{low_note} — see Poll choices "
            "section below)"
        )
    out.append(
        f"- No-position: **{n_count}**  (the heuristic couldn't classify; "
        "may include technical clarifications, questions, or non-standard "
        "phrasings)"
    )
    out.append(
        f"- **Coverage: {coverage_pct}%** ({total - n_count}/{total} messages classified)"
    )
    out.append("")

    # Chair statements rendered FIRST when present — they're the
    # load-bearing posts in a contentious thread. A consumer reading
    # top-down sees the chair's procedural declarations before
    # scrolling through hundreds of position rows.
    if chair_statements:
        out.append(f"## Chair statements ({len(chair_statements)})\n")
        out.append(
            "_Messages from someone tagged as Chair that contain "
            "procedural language (`rough consensus`, `consensus call`, "
            "`WGLC`, `adopting`, `closing this thread`, etc.). These "
            "are the chairs' load-bearing posts — verify the chair's "
            "declaration against the per-author tally below before "
            "relaying their characterisation._\n"
        )
        for cs in chair_statements:
            aff = affiliation_lookup.get(cs.sender) if affiliation_lookup else None
            tag_bits = [cs.role]
            if aff:
                tag_bits.append(aff)
            out.append(
                f"- **{cs.sender} ({' · '.join(tag_bits)})**  "
                f"[chunk {cs.chunk_idx}]  _matched:_ `{cs.matched_phrase}`"
            )
            out.append(f"  > {cs.excerpt}")
        out.append("")

    def _render_section(label: str, title: str) -> None:
        rows = [p for p in positions if p.label == label]
        if not rows:
            return
        # At low coverage the aggregate count is withheld in the summary; drop
        # it from the section header too so a quotable number does not leak here
        # while the grounded per-author rows below still show.
        high, low = _confidence_split(positions, label)
        count = (
            ""
            if _is_low_coverage(total, coverage_pct)
            else f" ({high} high, {low} low)" if low else f" ({high})"
        )
        out.append(f"## {title}{count}\n")
        for pos in rows:
            tag_bits: List[str] = []
            if role_lookup and pos.sender in role_lookup:
                tag_bits.append(role_lookup[pos.sender])
            if affiliation_lookup and pos.sender in affiliation_lookup:
                tag_bits.append(affiliation_lookup[pos.sender])
            tag = f" ({' · '.join(tag_bits)})" if tag_bits else ""
            conf_tag = f" — *{pos.confidence} confidence*" if pos.confidence else ""
            out.append(
                f"- **{pos.sender}{tag}**{conf_tag}  " f"[chunk {pos.chunk_idx}]"
            )
            if pos.excerpt:
                out.append(f"  > {pos.excerpt}")
        out.append("")

    # Poll choices rendered before per-side breakdowns. For an
    # option-poll thread this IS the answer — "Option 1: 5, Option 2:
    # 8" tells the consumer what the room chose at a glance.
    out.extend(
        _render_poll_section(
            positions, _is_low_coverage(total, coverage_pct), coverage_pct
        )
    )

    # By-author rollup answers "who's in which camp" in one block —
    # one row per author, listing the distinct stances they expressed
    # across all their messages. Skips authors who never registered
    # a classifiable position.
    by_author: Dict[str, List[Position]] = {}
    for pos in positions:
        if pos.label == "no-position":
            continue
        by_author.setdefault(pos.sender, []).append(pos)
    if by_author:
        # Same as the per-side sections: withhold the aggregate count at low
        # coverage; the grounded per-author rows still render.
        out.append(
            "## By author"
            + ("" if _is_low_coverage(total, coverage_pct) else f" ({len(by_author)})")
            + "\n"
        )
        for sender in sorted(by_author):
            entries = by_author[sender]
            # Collapse to distinct labels (with poll choices spelled
            # out) in the order they first appeared.
            seen: List[str] = []
            for pos in entries:
                if pos.label == "poll" and pos.poll_choice:
                    tag = f"poll:{pos.poll_choice}"
                else:
                    tag = pos.label
                if tag not in seen:
                    seen.append(tag)
            attr_bits: List[str] = []
            if role_lookup and sender in role_lookup:
                attr_bits.append(role_lookup[sender])
            if affiliation_lookup and sender in affiliation_lookup:
                attr_bits.append(affiliation_lookup[sender])
            tag = f" ({' · '.join(attr_bits)})" if attr_bits else ""
            n_msgs = entries[0].message_count_in_file
            out.append(
                f"- **{sender}{tag}** — {', '.join(seen)}  "
                f"_(in {n_msgs} message(s))_"
            )
        out.append("")

    _render_section("support", "Support")
    _render_section("conditional", "Conditional support")
    _render_section("oppose", "Oppose")

    if n_count:
        out.append(f"## No detectable position ({n_count})\n")
        out.append(
            "_The heuristic found no canonical position phrasing in these "
            "messages. They may be technical clarifications, questions, "
            "or stances expressed in non-standard wording. Inspect the "
            "messages directly via `get_chunk_text` if the count above "
            "looks low._\n"
        )
        # Compact: one line per author with their chunk indices, sorted
        # by author for stability.
        by_sender: Dict[str, List[int]] = {}
        for pos in positions:
            if pos.label != "no-position":
                continue
            by_sender.setdefault(pos.sender, []).append(pos.chunk_idx)
        for sender in sorted(by_sender):
            chunks = ", ".join(str(i) for i in by_sender[sender])
            out.append(f"- {sender}  [chunks {chunks}]")
        out.append("")

    return "\n".join(out)


def file_supports_tally(relpath: str) -> bool:
    """True for thread / issue / PR files (the ones with `### [N] DATE — …`
    section headers). Used by the MCP tool to refuse gracefully on
    drafts, transcripts, digests, etc. — those don't have a tallyable
    structure.

    PR files qualify on the same terms as issues, and a PR review thread
    is often exactly where positions get stated.
    """
    lower = relpath.lower()
    return lower.startswith(("threads/", "issues/", "pulls/")) and lower.endswith(".md")


def load_people_context(
    cache_dir: str,
) -> Tuple[Dict[str, str], Dict[str, str]]:
    """Parse the WG's people digest into two name→tag dicts.

    Returns (role_by_name, affiliation_by_name). Either map may be
    empty if the digest is missing or has no relevant rows. Used by
    `tally_positions` to enrich each entry with implementer signal at
    the same time it surfaces the count.

    We parse the digest (not the registry) because the MCP server
    doesn't carry a registry — and rebuilding one would walk the IMAP
    cache. The digest is the durable, fast-to-read projection.
    """
    role_by_name: Dict[str, str] = {}
    aff_by_name: Dict[str, str] = {}
    path = digest_path(cache_dir, "people")
    if not os.path.isfile(path):
        return role_by_name, aff_by_name
    try:
        with open(path, "r", encoding="utf-8") as fh:
            text = fh.read()
    except OSError:
        return role_by_name, aff_by_name

    for section in parse_md_tables(text):
        cols_lower = [c.lower() for c in section.columns]
        try:
            name_idx = cols_lower.index("name")
        except ValueError:
            continue
        role_idx = (
            cols_lower.index("roles")
            if "roles" in cols_lower
            else cols_lower.index("role") if "role" in cols_lower else None
        )
        aff_idx = (
            cols_lower.index("affiliation") if "affiliation" in cols_lower else None
        )
        for row in section.rows:
            if name_idx >= len(row):
                continue
            name = row[name_idx].strip()
            if not name:
                continue
            if role_idx is not None and role_idx < len(row):
                role = row[role_idx].strip()
                if role and name not in role_by_name:
                    # First-seen wins (leadership table appears first
                    # and is the most authoritative role source).
                    role_by_name[name] = role
            if aff_idx is not None and aff_idx < len(row):
                aff = row[aff_idx].strip()
                if aff and name not in aff_by_name:
                    aff_by_name[name] = aff
    return role_by_name, aff_by_name


def read_file_text(cache_dir: str, relpath: str) -> Optional[str]:
    """Read a file from the WG cache. Returns None on any failure
    (the MCP tool will surface a user-friendly error).
    """
    path = os.path.realpath(os.path.join(cache_dir, relpath))
    if not path.startswith(os.path.realpath(cache_dir) + os.sep):
        return None
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return None
