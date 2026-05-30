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
