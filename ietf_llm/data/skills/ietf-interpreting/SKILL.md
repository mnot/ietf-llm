---
name: ietf-interpreting
description: Interpretive norms for reading the record of an IETF/IRTF effort. Use BEFORE characterising what a working group decided, whether there is consensus, who supports or opposes a proposal, or where the group stands -- any sentence asserting a collective outcome (settled, decided, agreed, rejected, consensus, "the WG thinks/wants"). Covers consensus is chair-declared not vote-counted, decisions happen on the mailing list not in meetings, positions belong to individuals not their employers, and what Internet-Draft names and RFC streams do and don't imply. Reporting what a named individual said does not require this; any claim about where the group landed does. For drafting a contribution, use ietf-contributing instead.
license: CC-BY-4.0
---

# Reading the IETF record

The trigger is grammatical, not a self-assessment: **before you write any
sentence asserting a collective outcome** -- settled, decided, agreed,
rejected, "there is consensus," or "the WG thinks/wants" -- you should have read
these norms. Reporting what a *named individual* said is free; any claim about
where the *group* landed is gated. Not enforced -- the point is to make skipping
the check something you notice choosing. For the write side -- drafting
contributions -- see the `ietf-contributing` skill.

## The trap is what you already know

You know the headlines -- consensus is chair-declared, decisions are confirmed
on the list, participants speak as individuals. That is the trap. The gap isn't
in what you know: a discussion can *feel* completely resolved and not be. When
prominent participants converge and the tone goes calm, no one has decided
until a chair declares it on-list or a closed issue records it. A session poll
(28-4) gauges the room; it decides nothing. Confidence that the matter is
closed is the cue to go and verify the chair's words.

**Worked example.** A draft author raises an objection; a respected
cryptographer posts an analysis answering it; three well-known participants
reply approvingly. Tempting: *"the objection was settled."* Earned: *"the
objection appears answered to the satisfaction of several vocal participants;
no chair has declared it closed, and the draft author was still pressing open
points."* Even a chair's *summary* is weaker evidence than the chair's actual
procedural message -- chair characterisations get disputed on-list too. Look for
the chair's actual procedural messages for a thread -- the consensus call, WGLC,
or closure -- rather than relying on a summary of them.

A draft revised to address feedback carries a signal of its own. Silence
afterwards falls short of consensus, though it does suggest the group accepts
the edits.

## Not every decision is called, and none is final until Last Call

Two symmetric mistakes about finality. First, the *absence* of a consensus
call isn't an open issue: chairs don't poll every point, leaving many formally
open as the work proceeds and sweeping them up at **Working Group Last Call
(WGLC)** -- the wrap-up that confirms the document whole. Second, a consensus
already declared isn't a closed door -- new relevant information (a fresh
implementation report, a security analysis, a use case nobody weighed) can
reopen it at the chair's discretion. Report a past consensus as the current
state of play, not a permanent fact.

And WGLC itself isn't the last word. WG agreement only earns the draft an
**IETF Last Call**, where the whole community -- not just WG participants --
weighs in, then IESG review. A group can agree among themselves and still see
the work reshaped or blocked from outside. "The WG agreed" is not "the IETF
agreed"; a document past WGLC but not yet an RFC is still open to wider
scrutiny.

## "Never raised" needs more than an empty search

Semantic search answers "what was said about X"; it is structurally bad at
"was X ever said". Retrieval ranks by similarity, so a query coming back
empty means nothing scored close -- not that the words are absent from the
record. A claim cannot rest on a semantic search returning nothing.

The negative needs an exact-string sweep -- `grep_corpus` where the tooling
provides one, otherwise whatever literal search is available -- and the claim
should state the sweep's scope: "no matches for `8890` in the gathered
httpbis archive" is checkable; "no one ever cited RFC 8890" is not. Search
the shortest distinctive token rather than a phrase -- a phrase split across
a wrapped mail line won't match.

Even a clean sweep bounds the claim rather than proving absence: it covers
what was gathered (a window in time) and one corpus (the point may have been
raised on another list, in a meeting, or in a hallway). The earned form of
"never raised" is "not found in <stated scope>" -- report it that way.

## Affiliation: aggregate, don't attribute

Don't pin a position on a company ("Cloudflare opposes X") from an author's
affiliation -- only when they frame it that way ("speaking for X…"). But
implementer signal is real ("rough consensus and running code"), so
*aggregate* instead: "8 of 12 stated supporters ship TLS stacks" is fine;
"Cloudflare supports X" is not, unless they said so. One non-obvious bit:

**Email domain ≠ affiliation.** Participants use personal email and may hold
several affiliations. Rely on stated affiliation data where you have it, never
on the From-header domain.

## Draft names carry structure, not gravity

The prefix says where the draft sits in the process:

- **`draft-ietf-<wg>-…`** -- adopted by an IETF WG (the segment after `ietf-` is
  the WG shortname). **`draft-irtf-<rg>-…`** -- adopted by an IRTF Research Group
  (research, not standards).
- **`draft-<author-id>-…`** -- an *individual* submission. The author-id is
  arbitrary, and a hint after it (`draft-rescorla-tls-…`) is unreliable -- don't
  infer authorship or WG membership from the filename; check the draft's
  metadata.

**Unadopted drafts have no IETF status** -- not a WG document, not
standards-track, no community endorsement. Don't say "the IETF is working on X"
when X is one person's `draft-author-…`.

## Not every RFC is a consensus standard

The stream the draft took determines the endorsement the RFC carries:

- **IETF stream** -- WG-adopted, or AD-sponsored for an individual draft: the
  consensus standards track.
- **IRTF stream** -- research output, not a standard.
- **Independent Stream (ISE)** -- lightweight editorial review, **explicitly not
  a consensus standard**; reading one as "the IETF says…" is wrong.
- **IAB / Editorial streams** -- architectural statements / RFC-series process;
  both non-standards.

An RFC number alone doesn't tell you -- check the stream and the WG provenance.

## DISPATCH and BoFs are "being considered," not "being worked on"

Proposals rarely land straight in a WG. New work is triaged through the
**DISPATCH** WG (or a domain equivalent that runs its own triage -- `httpbis`
for HTTP, `tls` for TLS, `dnsop` for DNS) and explored in one or two **BoF**
sessions before a WG is chartered. A BoF is *not* a WG -- no documents, no
consensus authority. So a topic at DISPATCH or a BoF is *being considered*, not
*being worked on*.
