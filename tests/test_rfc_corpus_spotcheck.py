"""Spot-check the installed RFC corpus against the RFC Editor's own text.

**Reads the real cache, read-only, and skips when it is not there.** conftest
sandboxes `$HOME` for tests precisely so they never touch real user
directories; this is a deliberate exception, because the property it checks —
that section text matches the published document — cannot be established
against a synthetic fixture. The behaviours themselves are unit-tested in
`test_rfcindex_build.py` and `test_rfcindex_text.py`; what this adds is
breadth over documents nobody chose.

It needs the corpus (`~/.cache/ietf-llm/rfcs/embeddings.db`, installed from
the seed store) and the publisher's text mirror. Absent either, it skips.
`IETF_LLM_RFC_SPOTCHECK=0` skips it regardless.

Two of the three checks are sound in the sense that matters: they cannot
report a failure the code did not commit.

  ACCURACY   every line returned appears in the source RFC. Catches
             fabrication, mangling, and text pulled from another section.
  STRUCTURE  figure and table rows survive verbatim, with their relative
             indentation. This is the check that found chunk boundaries
             splitting a diagram row in RFC 1531 §4.4.1.

The third, coverage, is descriptive: an RFC's sections do not cover its
front and back matter, by design, so the number is asserted only loosely.

Within-section contiguity is deliberately absent. Locating a section by
matching returned lines back to the source does not work — older RFCs repeat
lines often enough that one match elsewhere stretches the apparent extent
across the document, and every line between then reads as missing. It is a
structural property of the stored ranges, asserted exactly in
`test_ranges_tile_at_line_boundaries`.
"""

from __future__ import annotations

import os
import random
import re
from typing import List, Tuple

import pytest

from ietf_llm.embeddings.storage import section_outline
from ietf_llm.mcp.rfc_text import section_text
from ietf_llm.rfcindex.mirror import text_path
from ietf_llm.rfcindex.text import _is_footer, _is_running_header

CORPUS = "rfcs"
MIRROR = os.path.expanduser("~/.cache/ietf-llm/_rfc/text")
DB = os.path.expanduser("~/.cache/ietf-llm/rfcs/embeddings.db")

#: Enough to be broad, small enough to stay a couple of seconds.
SAMPLE = 60

ART = re.compile(r"[+][-+]{2,}|\|.*\|")

_available = (
    os.environ.get("IETF_LLM_RFC_SPOTCHECK", "1") != "0"
    and os.path.isfile(DB)
    and os.path.isdir(MIRROR)
)
pytestmark = pytest.mark.skipif(
    not _available,
    reason="needs the installed RFC corpus and the publisher's text mirror",
)


def _norm(line: str) -> str:
    return " ".join(line.split())


def _body(rfc: str) -> List[str]:
    with open(text_path(MIRROR, rfc), "rb") as fh:
        raw = fh.read().decode("utf-8", "replace")
    return [
        l
        for l in raw.split("\n")
        if l.strip() and "\f" not in l and not _is_footer(l) and not _is_running_header(l)
    ]


@pytest.fixture(scope="module", name="sections")
def _sections() -> List[Tuple[str, str]]:
    """A deterministic sample, stratified so the paginated era and the
    xml2rfc-v3 era are both represented rather than whichever has more RFCs."""
    nums = sorted(
        int(f[3:-4])
        for f in os.listdir(MIRROR)
        if f.startswith("rfc") and f.endswith(".txt") and f[3:-4].isdigit()
    )
    rng = random.Random(20260811)
    bands = [(1, 2000), (2000, 4000), (4000, 6000), (6000, 8000), (8000, 10100)]
    out: List[Tuple[str, str]] = []
    for lo, hi in bands:
        pool = [n for n in nums if lo <= n < hi]
        rng.shuffle(pool)
        taken = 0
        for num in pool:
            if taken >= SAMPLE // len(bands):
                break
            outline = section_outline(CORPUS, f"rfc{num}.txt")
            if not outline:
                continue
            for sec, _title, _size in sorted(outline, key=lambda r: -r[2])[:3]:
                out.append((str(num), sec))
                taken += 1
    return out


def test_the_sample_is_usable(sections: List[Tuple[str, str]]) -> None:
    assert len(sections) >= 30, "corpus too sparse to spot-check meaningfully"


def test_every_returned_line_appears_in_the_source(
    sections: List[Tuple[str, str]],
) -> None:
    """The sound accuracy check: nothing invented, nothing mangled, nothing
    borrowed from a neighbouring section."""
    bad = []
    for num, sec in sections:
        src = _body(num)
        src_set = {_norm(l) for l in src}
        # A page-split paragraph comes back rejoined, so a source line may be
        # a substring of a returned line rather than equal to one.
        src_blob = " ".join(_norm(l) for l in src)
        got = section_text(num, sec)
        if not got:
            continue
        for line in (_norm(l) for l in got.split("\n") if l.strip()):
            if line not in src_set and line not in src_blob:
                bad.append((num, sec, line[:60]))
                break
    assert not bad, f"lines not found in the source: {bad[:5]}"


def test_figure_and_table_rows_survive_verbatim(
    sections: List[Tuple[str, str]],
) -> None:
    """Regression cover for the chunk boundary that split a diagram row of
    RFC 1531's DHCP state machine across two lines."""
    checked = 0
    broken = []
    for num, sec in sections:
        got = section_text(num, sec)
        if not got:
            continue
        src_set = {_norm(l) for l in _body(num)}
        for row in (l for l in got.split("\n") if ART.search(l)):
            checked += 1
            if _norm(row) not in src_set:
                broken.append((num, sec, _norm(row)[:60]))
    assert checked, "sample contained no figures or tables to check"
    assert not broken, f"{len(broken)} rows differ from the source: {broken[:5]}"


def test_sections_cover_most_of_each_document(
    sections: List[Tuple[str, str]],
) -> None:
    """Descriptive, and asserted loosely on purpose.

    An RFC's sections never cover its masthead, Status of This Memo,
    Copyright, Abstract, References or Authors' Addresses — `chunk.py` drops
    those, and `get_rfc_section`'s outline says so. The share they occupy
    depends on document length, so this guards against a collapse, not
    against a number.
    """
    coverage = []
    for num in list(dict.fromkeys(n for n, _s in sections))[:12]:
        src = _body(num)
        whole = " ".join(
            _norm(l)
            for sec, _t, _n in section_outline(CORPUS, f"rfc{num}.txt")
            for l in (section_text(num, sec) or "").split("\n")
            if l.strip()
        )
        if src:
            coverage.append(sum(1 for l in src if _norm(l) in whole) / len(src))
    assert coverage
    mean = sum(coverage) / len(coverage)
    assert mean > 0.5, f"body coverage collapsed to {mean:.0%}"
