"""Option-poll vote detection for the position tally (`positions.py`).

Split out of `positions.py`, with the small sentence helpers both modules
share (hedge / negation / sentence-scope checks).
"""

from __future__ import annotations

import re
from typing import Optional, Tuple

# A hedge anywhere in the rest of the sentence ("I agree, but …", "I support
# this, although …") means the opener is not an unqualified position.
HEDGE_RE = re.compile(
    r"\b(?:but|not|however|although|though|except|unless|disagree)\b|n't",
    re.IGNORECASE,
)
# "not" (or n't / never) just before an intent phrase flips its meaning
# ("not in favor of option 2", "not ready to progress"). A short word may
# intervene ("not yet ready").
_NEGATION_BEFORE_RE = re.compile(
    r"(?:\bnot|n't|\bnever)\s+(?:\w{1,6}\s+)?$", re.IGNORECASE
)
# A full stop after an abbreviation ("e.g. as in 3.2, but …") does not end the
# sentence.
_SENTENCE_END_RE = re.compile(
    r"(?<!e\.g)(?<!i\.e)(?<!etc)(?<!Mr)(?<!Dr)[.!?](?:\s|$)|\n"
)
# Negation between an intent phrase and its marker ("preference is not
# option 2", "definitely not #2"): the vote is against, not for, the choice.
_NEGATION_IN_SPAN_RE = re.compile(r"\b(?:not|never)\b|n't", re.IGNORECASE)


def rest_of_sentence(text: str, start: int) -> str:
    """`text` from `start` to the end of its sentence or line."""
    end = _SENTENCE_END_RE.search(text, start)
    return text[start : end.start() if end else len(text)]


# A sentence that opens with an explicit contrast marker walks back the one
# before it ("I support adoption. However, ..."). Deliberately short:
# only unambiguous markers, since this lowers confidence on real votes.
_CONTRAST_OPENER_RE = re.compile(
    r"[.!?\s]*(?:however|but|that\s+said|although|though|except|unless)\b",
    re.IGNORECASE,
)


def contrast_follows(text: str, start: int) -> bool:
    """Whether the sentence after the one containing `start` opens with a
    contrast marker."""
    end = _SENTENCE_END_RE.search(text, start)
    return end is not None and _CONTRAST_OPENER_RE.match(text, end.start()) is not None


def _negated(text: str, start: int) -> bool:
    """Whether the phrase beginning at `start` is directly negated."""
    return _NEGATION_BEFORE_RE.search(text[max(0, start - 16) : start]) is not None


def first_unnegated(pattern: re.Pattern[str], text: str) -> Optional[re.Match[str]]:
    for match in pattern.finditer(text):
        if not _negated(text, match.start()):
            return match
    return None


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
            (?! \s+ (?: is | was | are | were | has | had | duplicate | dup
                       | fix(?:es|ed) | clos(?:es|ed) ) \b )
        )""",
    re.IGNORECASE | re.MULTILINE | re.VERBOSE,
)


# A qualifier in the rest of the sentence after a poll marker means the
# marker is not the author's first choice ("Option A would be my second
# choice; I dislike both"): kept, but at low confidence.
_POLL_QUALIFIER_RE = re.compile(
    r"\b(?:not|never|dislike|against|object|second|third|least|worst|rather"
    r"|but|although|however|though)\b|n't",
    re.IGNORECASE,
)


def _poll_confidence(text: str, match: re.Match[str]) -> str:
    if _POLL_QUALIFIER_RE.search(rest_of_sentence(text, match.end())):
        return "low"
    return "high"


def extract_poll_choice(
    text: str, allow_bare_hash: bool = True
) -> Optional[Tuple[str, re.Match[str], str]]:
    """Detect a poll vote in `text` and return (choice, match, confidence)
    or None.

    Three modes, tried in order:
      1. Terse line-start marker (`Option 2`, `#2` at line start) →
         bare poll vote. The line-start position is the signal. A bare
         `#N` is skipped unless `allow_bare_hash`: in GitHub issue / PR
         files it is nearly always a cross-reference to another issue.
      2. Direct intent verb immediately followed by a choice
         (`I prefer 2`, `preference is #1`, `+1 on #1`,
         `in favor of option 2`). The intent and marker are
         syntactically attached.
      3. Marker preceded by an intent phrase within
         `_POLL_INTENT_WINDOW` chars (`Definitely anonymous first
         (#1)`). Looser; the intent and marker can have descriptive
         text between them.

    An intent phrase directly negated ("not in favor of option 2"), or
    negated between the intent and the marker ("preference is not option
    2"), is not a vote. A qualifier later in the same sentence ("… my
    second choice"), or a hedge between intent and marker, keeps the vote
    but at "low" confidence.

    The choice is uppercased ("Option 2" / "#2" / "I want 2" → "2";
    "option b" → "B") so the renderer can aggregate by choice. The choice
    is always captured from a structurally-anchored digit/letter group, so
    an intent word such as "is" can never be taken as the choice.
    """
    for bare in _POLL_BARE_RE.finditer(text):
        if bare.group("b_hash") and not allow_bare_hash:
            continue
        choice = (bare.group("b_word") or bare.group("b_hash") or "").upper()
        if choice:
            return (choice, bare, _poll_confidence(text, bare))

    for direct in _POLL_DIRECT_RE.finditer(text):
        if _negated(text, direct.start()):
            continue
        choice = (direct.group("d_choice") or "").upper()
        if choice:
            return (choice, direct, _poll_confidence(text, direct))

    for marker in _POLL_MARKER_RE.finditer(text):
        window_start = max(0, marker.start() - _POLL_INTENT_WINDOW)
        preceding = text[window_start : marker.start()]
        intent = first_unnegated(_POLL_INTENT_RE, preceding)
        if intent is None:
            continue
        between = preceding[intent.end() :]
        if _NEGATION_IN_SPAN_RE.search(between):
            continue
        choice = (marker.group("m_word") or marker.group("m_hash") or "").upper()
        if choice:
            conf = "low" if HEDGE_RE.search(between) else _poll_confidence(text, marker)
            return (choice, marker, conf)
    return None
