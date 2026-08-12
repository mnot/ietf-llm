# Assembling the findings

Step 10 of `SKILL.md`, and the skill's output: what a reviewer needs in order to decide what to
raise, and to defend it once raised. It is not the review -- that is `delivering.md`, and it is
optional.

Each issue is a record you already have, merged, chained and calibrated at Step 9. Assembling is
rendering; fresh thinking here means Step 9 is unfinished.

Lists, not tables -- this gets read in a text editor as often as anywhere that renders markdown.
Use a table only where the content is genuinely a matrix.

Headings in this order; levels shift with context. Drop the qualifiers when the review is written,
and then only for what the reviewer chose.

```
## Provisional view
## Candidate Issues

_one-line index, then a subsection each_

### I1. <the concern, in one sentence>

## Potential Comments
## Observed Nits
## Could not obtain
## Your call
```

Four more are available on request and are not written unless asked -- see *On request*, below.

## Provisional view

What you would call it and why, answering the Step 1 question. Mark it as yours.

Name the datatracker `Result` you would pick whatever the review type; off a directorate assignment,
flag it as the one you would have picked. That vocabulary says "publish, but not this revision" more
precisely than a sentence does; the values differ per team, so see `delivering.md` before naming one.

In IESG Evaluation, give the `Result` and then say whether you think it warrants a DISCUSS -- that is
the instrument at that stage, and it is what the reviewer is deciding. Past approval the IESG has
already picked one, so give the `Result` only if it helps, and lead with the routes instead.

## Candidate Issues

Open with a one-line index to triage from. Each line: the sections, then the claim and what follows
from it. Nothing else -- what an issue rests on is *The case*, a few lines down, and a compressed
copy of it here reads as noise while saying less than the field does.

Without the consequence the line is an observation, and the reader has to open the subsection to
find out whether they care, which is what the index exists to save them.

Close each line with the ratings -- 🔥 severity, ⭐️ confidence, different glyphs so the pair reads
without a legend. They are what decides which issue a reviewer opens first.

- **I1** (§3.1, §6) Mandates a fixed hostname label, so an operator cannot name their own host.
  🔥4/⭐️5
- **I2** (§5) What a client may conclude from a failure is never stated, so two implementations
  diverge. 🔥3/⭐️4
- **I3** (§4.2) The retry bound went in -06 and nothing replaced it, so the limit is now unstated.
  🔥2/⭐️5

Then a subsection per issue, as labelled fields rather than paragraphs. The heading is the concern --
one sentence, stated as a claim about the document. Do not restate it as a field.

These fields and no others: an issue that has grown one of its own is telling you something belongs
in another section. The four marked *only where* are conditional; the rest are always present.

- **The case:** the argument, with each quote set off where it is used rather than gathered into a
  block of its own. Break it into short paragraphs, one point each: what the document says, what that
  produces. Where the argument turns on how this document and another fit together, a third -- what
  the *other* document says. Do not propose the fix. Keep it to what a reader
  needs in order to check the claim, and write it so they can agree or disagree rather than
  reassemble it.

  Answer the authors' best objection only where it changes the ask, and in a clause rather than a
  paragraph. A pre-emption already in the draft is not an answer.

  Quote only what the argument uses, and do not restate a quote in prose beside it. Every fact it
  rests on needs its quote here -- if you cannot quote it, you cannot claim it. Then re-read each
  sentence against the quote beside it and check it claims no more.

  Where a weaker form is still true, write the weaker one -- *reasonable* not *sound*, *very little
  specified behaviour* not *none at all*. Cut a consequence you cannot show from the text; do not
  hedge it into the case.

  That covers facts about anything, not just the draft: what a registry holds, what another RFC
  says, who wrote what and when. Fetch and read the source; recall is not a basis. Where such a fact
  carries the finding -- a registration, its date, its assignee -- ask whether the text is staying
  rather than building the case on it.

  Keep your reasoning about whether to raise it out of the case -- issue versus nit, whether it is
  worth the words. *Severity* carries that, or nothing does. Keep the significance out too: *there is
  a lowercase "should" in Section 2.2.1*, not *and that matters here*. Keep what the record already
  did with it out as well; that is *Disposition*.
- **Can be resolved if:** what would fix it. Imperative for specification craft -- *Describe the
  semantics*, *Point at RFC 9111 instead*; hedged where the fix is a structural judgement -- *the
  document needs to say (probably globally) whether ...*. Where the answer is the authors' intent,
  ask instead, on its own line: *Are these omissions intentional?*
- **Caused by:** only where a prior change created or sharpened this one -- which change, answering
  what, and which revision. It is often not a fix for this concern at all: a pull request answering
  a different issue that broke something in passing, or a review fix placed above text it
  contradicts. Name it here rather than restating it under *Prior concerns*.
- **Related:** only where another issue bears on this one without having caused it -- two findings
  on one mechanism that are not one decision, so the merge rule keeps them apart. Name the ID and
  the adjacency. If the reviewer raises only one, this is what tells them the other's framing
  changes.
- **Disposition:** only where the record has already answered this -- what was raised, by whom, what
  the closing reason was, and whether it engaged the point. A concern the authors have declined is
  raised differently from one they have never seen, and this is what carries that when *Prior
  concerns* is not written.
- **Severity:** how significant the issue is / how strong its impact is; scale of one to five 🔥.
  Five is *this cannot ship, and if it did the IETF would have to fix it afterwards*; three is
  *the authors should fix this before publication, and a reviewer would expect them to*; one is
  *worth saying, and the document survives without it*.
- **Confidence:** how confident you are in the issue; scale of one to five ⭐️. Five is *quoted text,
  and the reading is not open to dispute*; three is *the text supports this and another reading is
  available*; one is *I think this is true and cannot show it from the document*. It rates the
  reading, not the fate -- a finding can be five stars on the text and still be settled, out of
  scope, or answered elsewhere.
- **Grounding:** only where confidence is short of full -- the reason it is short, and what would
  change your mind. An inference rather than the quoted text; or a source you could not read, named.

Leave the ratings bare. Justifying one inline is *Grounding*'s job arriving early.

Across the fields: weaker in the claims than reads natural, no weaker in the asks. The default runs
the other way.

## The remaining sections

**Potential Comments**, one bullet each: the section, the quoted text, then one question or one flat
statement. If one needs more than that, it is an issue in disguise.

**Observed Nits**, one line each, and only ones quoted from the document verbatim and checked.

**Could not obtain** -- what, and what it means the analysis cannot say. A run without the gathered
record says so here *and* beside the provisional view: the reader weighs every "never raised"
differently.

**Your call** -- the forks only the reviewer can settle. Not a summary: each one is a question with
a consequence attached.

- A finding resting on an inference rather than on quoted text, and what turns on it.
- A finding where they have standing you do not -- they closed the issue, edited the RFC, chaired
  the thread. Say so; do not quietly raise their own point back at them.
- A finding whose framing commits them to a position they may not want to hold.
- Anything you could not verify, and what it would change.

Keep it to a few lines. If nothing is genuinely theirs to settle, say that rather than manufacturing
a list -- a ritual section trains them to skip it, and this is the one section that is asking them
for something.

Apply the economy test to *Your call*, and to *Checked, not raised* when it is asked for: would
removing this change what the reviewer decides to raise, or how they would defend it? It does not
govern Comments and Nits -- Step 9 says keep every concrete, checkable, quotable one, and that
stands.

## On request

Four things the run already has and does not write up. Do not draft any of them before the ask --
the material is in hand, so writing it out unasked is the whole of the waste.

Offer them in one line at the end of the findings, naming the four. No preamble, no description of
what each would contain.

**What changed and when** -- where the revision history bears on the findings, and as long as it
needs to be. Text that was removed, when, and whether anything replaced it; requirements that
changed strength; whatever the changelog does not record. This is what the *Caused by* fields point
at. Where a change is itself the defect, that is a Candidate Issue like any other -- this section is
the context, not a place to keep a finding.

**Prior concerns**, one bullet each: what was raised, by whom, and what this revision did with it --
addressed, partly, unaddressed, or answered on the list without a text change.

**Checked, not raised**, one bullet each: what, and why not -- a lens that did not apply, a concern
the draft's own text refuses, an objection that is about the topic. Only what a reader might expect
to see raised; not a log of everything you thought about.

A lens that came back *supporting* the design is exempt from that economy test: keep it in full. A
run of issues reads as an attack on the approach, and the supporting results are what stops it.

Two things in here the reviewer cannot know to ask for, so name them in the offer: a lens that came
back supporting the design, and a concern you killed that a reader would expect to see raised. The
second is the one that gets rediscovered -- a concern the draft's own text refuses still looks wrong
on its face, and the reviewer will arrive at it again unless told it is answered.

**The actor table** -- as Step 4 returned it, ranked at Step 6. Hand over the staged file; do not
re-render it. Stage it now if Step 4 did not.
