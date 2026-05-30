# How the IETF works

Interpretive norms for reading an IETF corpus. Call `read_ietf_norms`
before characterising decisions, attributing positions, or summarising
where a Working Group stands.

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
2. **Email domain ≠ affiliation.** `mnot.net` is Mark Nottingham's
   personal domain; he ships drafts as Cloudflare or Independent
   depending on the draft. Use the `affiliations` field on Person /
   the people digest, never the From-header domain.

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
