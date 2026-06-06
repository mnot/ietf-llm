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

from .digest.query import parse_md_tables
from .paths import digest_path

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

_STRONG_SUPPORT = re.compile(
    r"""^\s*(
        \+1\s*[.!]?\s*$                                    # bare +1
      | I\s+(?:support|agree|concur|approve|endorse|am\s+in\s+favo[u]?r) # I support / agree / ...
      | (?:I\s+(?:strongly\s+)?support|Support)\b
      | LGTM\b                                             # looks-good-to-me
      | Ship\s+it\b
      | Yes,?\s+(?:I\s+(?:support|agree)|let'?s|we\s+should)
    )
    """,
    re.IGNORECASE | re.MULTILINE | re.VERBOSE,
)

_STRONG_OPPOSE = re.compile(
    r"""^\s*(
        -1\s*[.!]?\s*$                                     # bare -1
      | I\s+(?:object|oppose|disagree|don'?t\s+support)
      | I\s+am\s+(?:opposed|against)
      | (?:Oppose|Object)d?\.?\s*$                         # standalone Oppose.
      | DISCUSS\b                                          # IESG ballot
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
    r"\b(?:I\s+(?:do\s+)?oppose|strongly\s+against|cannot\s+support)\b",
    re.IGNORECASE,
)

# Poll / option-choice detection — two-stage design.
#
# Real IETF option polls produce three shapes of vote:
#   1. Terse line-start reply ("Option 2", "#2") — bare marker, no
#      surrounding intent language.
#   2. Intent + marker in the same window ("I want option 2",
#      "Strong preference for #2", "in favor of option 2",
#      "+1 on #1", "definitely anonymous first (#1)").
#   3. Bare numbered list reply ("1.", "2)") — too ambiguous, not
#      attempted; consumer didn't report misses of that shape.
#
# An earlier single-regex implementation had a parse bug: with the
# optional `(?:\s+is)?` capture, the engine would drop "is" from the
# optional group and grab "is" as the choice token in "my preference
# is #1", producing the bogus choice "IS". The current two-stage
# design separates marker detection (what's the choice?) from intent
# detection (is this a vote at all?), so the choice is always a
# clean alphanumeric token from a structurally-anchored capture.

# Markers — surface forms where a poll choice is explicitly tagged.
# `m_word` is the digit/letter after "option"/"opt"/"choice"/
# "alternative"; `m_hash` is the number after `#`. The lookbehind on
# `#` keeps us from matching `foo#1` mid-token.
_POLL_MARKER_RE = re.compile(
    r"""(?:
        \b (?: option | opt\.? | alternative | choice ) \s+ (?P<m_word>\d{1,2}|[A-Za-z]) \b
      | (?<![A-Za-z0-9]) \# (?P<m_hash>\d{1,2}) \b
    )""",
    re.IGNORECASE | re.VERBOSE,
)

# Direct: intent verb followed *immediately* by a choice (digit,
# `#N`, or `option N`). This is the bridge that catches bare-digit
# votes like "I prefer 2" / "preference is #1" / "+1 on #1" /
# "in favor of option 2" — no marker word required after the verb.
# The `+1 …` branch uses an explicit anchor instead of `\b\+1` since
# `+` is non-word and `\b` before `+` doesn't fire when preceded by
# whitespace.
_POLL_DIRECT_RE = re.compile(
    r"""(?:
        \b (?:
            I \s+ (?: want | prefer | favor | support | back | choose | pick )
          | (?: I'd | I \s+ (?: 'd | would ) ) \s+ (?: like | prefer | choose | go \s+ with )
          | I \s+ think \s+ the \s+ answer \s+ is
          | (?: my | the ) \s+ pref(?:erence)? \s+ (?: is | would \s+ be )
          | pref(?:erence)? \s+ would \s+ be
          | (?: strong(?:ly)? | slight(?:ly)? ) \s+ pref(?:erence)? \s+ for
          | in \s+ favor \s+ of
          | (?: my \s+ )? vote \s+ (?: is | for | goes \s+ to )
          | hum \s+ for
        ) \s+
      | (?: ^ | (?<=\s) ) \+1 \s+ (?: on | to | for )? \s*
    )
    (?: option \s+ | \# )?
    (?P<d_choice>\d{1,2})
    \b""",
    re.IGNORECASE | re.VERBOSE | re.MULTILINE,
)

# Intent phrases — used by the window-based fallback when a marker
# appears with looser surrounding language. Catches "definitely
# anonymous first (#1)" (intent "definitely", marker "(#1)") and
# similar shapes where intent and marker are separated by descriptive
# words. `\+1` uses an explicit anchor for the same reason as above.
_POLL_INTENT_RE = re.compile(
    r"""(?:
        \b I \s+ (?: want | prefer | favor | support | back | choose | pick ) \b
      | \b (?: I'd | I \s+ (?: 'd | would ) ) \s+ (?: like | prefer | choose | go \s+ with ) \b
      | \b I \s+ think \s+ the \s+ answer \b
      | \b (?: strong(?:ly)? | slight(?:ly)? ) \s+ pref(?:erence)? \b
      | \b (?: my | the ) \s+ pref(?:erence)? \b
      | \b pref(?:erence)? \s+ (?: is | would \s+ be ) \b
      | \b (?: my \s+ )? vote \b
      | \b in \s+ favor \s+ of \b
      | \b definitely \b
      | \b hum \s+ for \b
      | (?: ^ | (?<=\s) ) \+1 (?= \s | $ | [^\d] )
    )""",
    re.IGNORECASE | re.VERBOSE | re.MULTILINE,
)
_POLL_INTENT_WINDOW = 80

# Bare line-start marker — terse poll reply where the marker IS the
# message's opening content. ("Option 2.", "#2", "Option 2 because
# it's simpler.") No surrounding intent required; line-start position
# IS the signal.
_POLL_BARE_RE = re.compile(
    r"""^\s*
        (?:
            (?: option | opt\.? | alternative | choice ) \s+ (?P<b_word>\d{1,2}|[A-Za-z]) \b
          | \# (?P<b_hash>\d{1,2}) \b
        )""",
    re.IGNORECASE | re.MULTILINE | re.VERBOSE,
)


def _extract_poll_choice(text: str) -> Optional[Tuple[str, re.Match[str]]]:
    """Detect a poll vote in `text` and return (choice, match) or None.

    Three modes, tried in order:
      1. Terse line-start marker (`Option 2`, `#2` at line start) →
         bare poll vote. The line-start position is the signal.
      2. Direct intent verb immediately followed by a choice
         (`I prefer 2`, `preference is #1`, `+1 on #1`,
         `in favor of option 2`). The intent and marker are
         syntactically attached.
      3. Marker preceded by an intent phrase within
         `_POLL_INTENT_WINDOW` chars (`Definitely anonymous first
         (#1)`). Looser; the intent and marker can have descriptive
         text between them.

    The choice is uppercased ("Option 2" / "#2" / "I want 2" → "2";
    "option b" → "B") so the renderer can aggregate by choice.

    A previous single-regex design had a parse bug where the optional
    "(?:\\s+is)?" group could drop "is" and have it captured as the
    choice token ("preference is #1" → choice "IS"). The current
    structure captures the choice from a structurally-anchored
    digit/letter group, so this can't happen.
    """
    bare = _POLL_BARE_RE.search(text)
    if bare is not None:
        choice = (bare.group("b_word") or bare.group("b_hash") or "").upper()
        if choice:
            return (choice, bare)

    direct = _POLL_DIRECT_RE.search(text)
    if direct is not None:
        choice = (direct.group("d_choice") or "").upper()
        if choice:
            return (choice, direct)

    for marker in _POLL_MARKER_RE.finditer(text):
        window_start = max(0, marker.start() - _POLL_INTENT_WINDOW)
        preceding = text[window_start : marker.start()]
        if _POLL_INTENT_RE.search(preceding) is not None:
            choice = (marker.group("m_word") or marker.group("m_hash") or "").upper()
            if choice:
                return (choice, marker)
    return None


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


# Thread message section header (mirrors chunking.py's _THREAD_MSG_RE,
# but rewritten here to capture the bits this module cares about: the
# message number and the sender. Date isn't needed — we sort by file
# order, which is already chronological.) The optional trailing
# `_(opened issue)_` annotation an issue file appends to its opener must be
# consumed here, or it leaks into the sender name and splits the opener's
# identity from their commenter identity in the tally.
_THREAD_MSG_RE = re.compile(
    r"^### \[(\d+)\] (?:\S+(?:\s+\S+)?) — (.+?)"
    r"(?:\s+_\(opened issue\)_)?(?: \(reply to \[\d+\]\))?$",
    re.MULTILINE,
)
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
    body: str,
) -> Tuple[str, str, str, Optional[str]]:
    """Return (label, confidence, excerpt, poll_choice) for one message
    body. `poll_choice` is only non-None when label == "poll".

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

    poll = _extract_poll_choice(leading_text)
    if poll is not None:
        choice, match = poll
        return ("poll", "high", _excerpt_for(match, leading_text), choice)

    support_match = _STRONG_SUPPORT.search(leading_text)
    oppose_match = _STRONG_OPPOSE.search(leading_text)
    # When both somehow fire (rare — e.g. "I support X. I object to Y"),
    # we surface the *first* one, since that's the framing of the post.
    if support_match and oppose_match:
        if support_match.start() < oppose_match.start():
            return (
                "support",
                "high",
                _excerpt_for(support_match, leading_text),
                None,
            )
        return (
            "oppose",
            "high",
            _excerpt_for(oppose_match, leading_text),
            None,
        )
    if support_match:
        return (
            "support",
            "high",
            _excerpt_for(support_match, leading_text),
            None,
        )
    if oppose_match:
        return (
            "oppose",
            "high",
            _excerpt_for(oppose_match, leading_text),
            None,
        )

    # Weak patterns — scan a bigger window in the body proper, marked
    # low confidence so the consumer can weight accordingly.
    window = cleaned[:_WEAK_SCAN_CHARS]
    weak_support = _WEAK_SUPPORT.search(window)
    weak_oppose = _WEAK_OPPOSE.search(window)
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
    matches = list(_THREAD_MSG_RE.finditer(text))
    out: List[Tuple[int, str, str]] = []
    for i, match in enumerate(matches):
        msg_idx = int(match.group(1))
        sender_raw = match.group(2).strip()
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


def tally_thread(file_text: str) -> Tuple[List[Position], Dict[str, int]]:
    """Walk one thread / issue file's text and return per-message
    positions plus a label→count summary.

    The summary counts every Position (including no-position rows) so
    the consumer can see what fraction of messages the heuristic
    couldn't classify. That coverage number is load-bearing — the
    user feedback that prompted this tool explicitly flagged "I
    relayed the chair's characterisation instead of counting" as the
    accuracy gap to close.
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
        label, conf, excerpt, poll_choice = extract_position(body)
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
    s_count = summary.get("support", 0)
    o_count = summary.get("oppose", 0)
    c_count = summary.get("conditional", 0)
    n_count = summary.get("no-position", 0)
    coverage_pct = int(100 * (total - n_count) / total) if total else 0

    out: List[str] = []
    out.append(f"# Position tally: `{file}`\n")
    out.append(
        "_Heuristic extraction. Matches canonical IETF position "
        "phrasings (`+1`, `-1`, `I support`, `I object`, `LGTM`, "
        "`DISCUSS`, …) near the start of each message; quoted "
        "text and `_Subject:_` metadata are stripped first. **Imperfect**: "
        "subtle technical objections, ironic phrasings, and "
        "questions-that-imply-disagreement all show as no-position. "
        "Always sanity-check against the file before publishing a "
        "count._\n"
    )
    p_count = summary.get("poll", 0)
    out.append("**Summary:**")
    out.append(f"- Support: **{s_count}**")
    out.append(f"- Conditional: **{c_count}**  (yes-but-only-if)")
    out.append(f"- Oppose: **{o_count}**")
    if p_count:
        out.append(
            f"- Poll choices: **{p_count}**  (`option N` / `#N` / "
            "`I prefer N` — see Poll choices section below)"
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
        out.append(f"## {title} ({len(rows)})\n")
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
    poll_positions = [p for p in positions if p.label == "poll"]
    if poll_positions:
        by_choice: Dict[str, List[Position]] = {}
        for pos in poll_positions:
            key = pos.poll_choice or "?"
            by_choice.setdefault(key, []).append(pos)
        out.append(f"## Poll choices ({len(poll_positions)})\n")
        out.append(
            "_Per-option tally. The same author may show up under "
            "multiple choices if they voted more than once — the most "
            "recent message wins for the by-author rollup below._\n"
        )

        # Sort by choice key: numerics ascending, then alphabetic.
        def _choice_sort(k: str) -> Tuple[int, str]:
            return (0, k.zfill(8)) if k.isdigit() else (1, k)

        for choice in sorted(by_choice, key=_choice_sort):
            voters = by_choice[choice]
            names = ", ".join(sorted({p.sender for p in voters}))
            out.append(
                f"- **Option {choice}** — {len(voters)} response(s): " f"{names}"
            )
        out.append("")

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
        out.append(f"## By author ({len(by_author)})\n")
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
    """True for thread / issue files (the ones with `### [N] DATE — …`
    section headers). Used by the MCP tool to refuse gracefully on
    drafts, transcripts, digests, etc. — those don't have a tallyable
    structure.
    """
    lower = relpath.lower()
    return (
        lower.startswith("threads/") or lower.startswith("issues/")
    ) and lower.endswith(".md")


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
