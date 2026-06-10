---
name: ietf-interpreting
description: Interpretive norms for reading the record of an IETF/IRTF effort. Use BEFORE characterising what a working group decided, whether there is consensus, who supports or opposes a proposal, or where the group stands — any sentence asserting a collective outcome (settled, decided, agreed, rejected, consensus, "the WG thinks/wants"). Covers consensus is chair-declared not vote-counted, decisions happen on the mailing list not in meetings, positions belong to individuals not their employers, and what Internet-Draft names and RFC streams do and don't imply. Reporting what a named individual said does not require this; any claim about where the group landed does. For drafting a contribution, use ietf-contributing instead.
---

# How the IETF works

Interpretive norms for reading an IETF corpus. The trigger for reading
this is grammatical, not a self-assessment: **before you write any
sentence asserting a collective outcome** — that something is settled,
decided, resolved, agreed, or rejected, that there is consensus, or what
"the WG thinks/wants" — you should have read these norms. Reporting what a
named individual said is free; any claim about where the *group* landed is
gated. For the write side — helping a human draft list mail, issues, or
comments — see the `ietf-contributing` skill (the same norms are also the
`read_ietf_participation_norms` MCP tool, where the server is connected).

This is not enforced. Nothing stops you from skipping it; the point is to
make the skip something you have to choose, not something that feels like
ordinary efficient judgment.

## Read this first: the trap is what you already know

Most of what follows you probably already know — chair-declared
consensus, individuals-not-employers, list-over-meetings. That is exactly
the trap. The rule that gets lost is not a fact you're missing; it's that
a discussion *feeling* resolved is not resolution. When prominent
participants converge and the tone goes calm, you will be tempted to write
"settled" or "the WG decided." No one decided until a chair declared it
on-list or a closed issue records it. Convergence among vocal participants
— even unanimous-sounding — is signal, not outcome. If you are confident
the matter is closed, that confidence is the cue to verify the chair's
words, not to skip the check.

## Individuals, not employers — but implementer signal is real

People participate as individuals, not as company representatives.
Don't attribute a position to a company ("Cloudflare opposes X") based
on the author's affiliation. Only treat something as a *company*
position when the author explicitly frames it that way ("my company…",
"speaking for X…", "as an employee of Y…").

*That said*: who ships running code matters. "Rough consensus and
running code" weighs implementer voices, and clustering of stated
affiliations across an argument is itself news. The `people` digest
records affiliations from drafts and GitHub with source provenance
(`Cloudflare (draft, github)` = corroborated; `(github)` alone =
self-reported only); blank = no documented signal, NOT "Independent."

Two rules of thumb:

1. **Aggregate, don't attribute.** "8 of 12 stated supporters are
   from organisations shipping TLS stacks" — fine. "Cloudflare
   supports X" — not fine, unless they said so.
2. **Email domain ≠ affiliation.** Participants often use personal
   email for IETF communications, and some hold multiple affiliations,
   representing some or none of those interests in a given discussion.
   Affiliation cannot be inferred from the From-header domain. Use the
   `affiliations` field on Person / the people digest instead.

## Decisions happen on the mailing list, not in meetings

A meeting might map out a proposal; the binding move is confirmation
on the list. When the user asks "what did the WG decide", look at
chair statements and closed-issue resolutions that reference list
discussion, not meeting minutes alone. A proposal that "got agreement
in the room" isn't a decision until it's confirmed on list.

## Consensus is chair-declared, not vote-counted

Only the chairs declare consensus, and they weigh argument substance,
not headcounts. A session poll showing 28-4 isn't a decision — it's a
tool the chairs use to gauge the room. Report polls and raise-of-hands
as *signal*, not outcomes; report chair declarations on list as
outcomes.

Prefer the chair's own words over a third-party summary of them: chair
characterisations are themselves sometimes disputed on list. The
`tally_positions` tool's **Chair statements** section surfaces the
procedural messages (consensus call, WGLC, closure) for one thread.

**Worked example.** A draft author raises an objection; a respected
cryptographer posts an analysis answering it; three well-known
participants reply approvingly. It is tempting to write: *"the objection
was settled."* Correct: *"the objection appears answered to the
satisfaction of several vocal participants; no chair has declared it
closed, and the draft author was still pressing open points."* The first
is a consensus claim you have not earned; the second reports what is on
the record. The difference is not pedantry — chair characterisations
themselves get disputed on-list, so even a chair's summary is weaker
evidence than the chair's actual procedural message.

## Internet-Draft names carry structure — but not gravity

The prefix tells you the draft's posture in the process:

- **`draft-ietf-<wg>-...`** — adopted by an IETF Working Group. The
  segment after `ietf-` is the WG shortname (`draft-ietf-httpbis-…`
  is in `httpbis`, `draft-ietf-tls-…` is in `tls`).
- **`draft-irtf-<rg>-...`** — adopted by an IRTF Research Group.
  Same shape, different stream (research, not standards).
- **`draft-<author-id>-...`** — an *individual* draft. The author-id
  is whatever the author chose; it often does NOT match how the
  author is otherwise identified (login, email local-part, surname),
  and the segment after it sometimes hints at the target group
  (e.g. `draft-rescorla-tls-…` is aimed at `tls`) but sometimes
  doesn't. Don't infer authorship or group membership from the
  filename alone — confirm against the draft's metadata.

**Unadopted drafts have no IETF status.** Anything not starting with
`draft-ietf-` / `draft-irtf-` is an individual submission: a
proposal, a strawman, a personal hobby. It is NOT a WG document, NOT
on a standards-track path, and carries no community endorsement. Be
careful about overstating its gravity ("the IETF is working on X"
when X is one person's `draft-author-…`).

## RFCs come from several streams — not all are consensus standards

The path a draft takes to RFC determines what kind of endorsement
the resulting RFC carries. The major paths:

- **IETF stream, WG-adopted.** The default: a WG adopts the draft,
  iterates to consensus, then ships it through IESG review.
- **IETF stream, AD-sponsored.** An Area Director picks up an
  individual draft and shepherds it through the IETF consensus
  process without a WG. Same rigour, smaller venue.
- **IRTF stream.** An IRTF Research Group adopts and ships the
  draft. Research output, not a standard.
- **Independent Stream (ISE).** The Independent Submissions Editor
  publishes drafts that don't fit any WG/RG, after a lightweight
  editorial review. **Not a consensus standard** — explicitly so.
  Treating an Independent Stream RFC as "the IETF says…" is wrong.

Minor paths: the **IAB stream** (architectural statements from the
Internet Architecture Board) and the **Editorial Stream** (process /
RFC-series mechanics from the RFC Editor). Both small, both
non-standards.

When the user asks "is this an IETF standard?", check the stream and
the WG provenance — an RFC number alone doesn't tell you.

## New work enters via DISPATCH or a BoF

Proposals don't typically land directly in a WG. The usual routes:

- **DISPATCH WG.** New work is often presented to the DISPATCH
  Working Group, which assesses it and points the author at the
  right home (an existing WG, a new BoF, the Independent Stream,
  or "not now").
- **Domain DISPATCH-equivalents.** Some WGs do this triage for
  their own area instead of routing through DISPATCH itself —
  `httpbis` for HTTP, `tls` for TLS, `dnsop` for DNS operations.
  Work in those domains usually goes to the area WG first.
- **BoF (Birds of a Feather).** Before chartering a new WG, the
  community usually holds one or two BoF sessions to gauge
  interest, scope the problem, and shape a draft charter. A BoF
  is *not* a WG — it has no documents, no consensus authority,
  no list of adopted drafts. Treat it as exploration.

So when a user asks "is the IETF doing X?", a topic discussed at a
BoF or pitched at DISPATCH is *being considered*, not *being
worked on*.
