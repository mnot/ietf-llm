---
name: ietf-reviewing
description: How to review an Internet-Draft. Use when asked to review, critique, or assess a draft or specification -- a directorate or IETF Last Call review, a WGLC response, a dispatch or call-for-adoption assessment, or an informal "what do you make of this draft". Produces findings for a human reviewer to consider -- each concern, what it rests on, and what would resolve it -- rather than finished review text; writing the review is a separate, optional step.
compatibility: Built around the ietf-llm MCP server for the gathered IETF record and live Datatracker state; an informal read works without it, but a review for the record degrades badly -- see the skill body. Uses rfcdiff for revision diffs, and the ietf-http skill for two of the architectural lens texts; both degrade gracefully.
license: CC-BY-4.0
---

# Reviewing an Internet-Draft

**You draft; the human sends.** A review goes into the record under a person's name and
they are accountable for every word of it. See `ietf-contributing`.

So the default output of these steps is **findings**, not a finished review: the concerns,
what each rests on, and what would resolve it. The reviewer decides what to raise and what
it adds up to. Producing the review itself -- as prose or as issues -- is a separate step in
`delivering.md`, taken after those decisions.

## How the steps run

Ten steps, but not ten in sequence. Step 3 fires with the Step 4 dispatches; Steps 5 to 7 run while
those are out; Step 6 waits on the actor table that Step 4 dispatched. Everything meets at Step 8.

Four files sit beside this one, each read at the point it is called for:

- `stages.md` -- reviews of a particular kind: a directorate assignment, a bis, a revision answering
  a closed round, a document past IESG approval. Step 1.
- `quoting.md` -- checking a quote against the text it came from. Step 8.
- `findings.md` -- the shape of the output. Step 10.
- `delivering.md` -- writing the review, or filing issues. Optional, and after Step 10.

## Out of scope

Neither of these is a finding: silence that was not this document's to fill, and IANA's own
procedure. Both are gates at Step 8, stated in full there.

## Without the tooling

- **No `ietf-llm`.** An informal read is unaffected; Step 8 is not. The web archives and the
  GitHub API are slow and miss silently, and silence is the very thing the step exists to assert.
  On a directorate, WGLC or Last Call review, announce the gap before dispatching, treat every
  "never raised" as unverified, and carry it beside the provisional view as well as in *Could not
  obtain*.
- **No `review_record`.** It reaches the network, so it is absent wherever gathering is off. Two
  Datatracker endpoints carry the same join by hand:

  ```
  /api/v1/review/reviewassignment/?review_request__doc__name=draft-...&format=json   # reviewed_rev, result, reviewer, state
  /api/v1/doc/ballotpositiondocevent/?doc__name=draft-...&format=json                # rev, pos, balloter, time
  ```

  They take different filters, and neither takes the obvious `?doc=`. On the ballot endpoint that
  errors; on the assignment endpoint it is *ignored*, returning every assignment in the datatracker
  in a well-formed response. Keep the revision field on both -- it is the whole value of the join.
- **No `rfcdiff`.** A single script, no install:
  <https://raw.githubusercontent.com/ietf-tools/rfcdiff/main/rfcdiff>
  Failing that, strip page headers, footers and form feeds before a raw `diff`.
- **No subagents.** Step 4 is written around dispatch. Without it, run the passes serially in the
  order they are listed and expect several times the wall-clock. Nothing in the method depends on
  concurrency -- only the cost does.

## RFC text

Wherever a step says to fetch, open, or read an RFC: `get_rfc_section` returns a section by
number, and `search_rfc_text` finds the section when the number is unknown. The lens RFCs are
also shipped beside their rubrics -- each rubric's **Text:** line names its copy -- and are the
fallback without `ietf-llm`. For any other RFC without `ietf-llm`, use the plain-text file at
`https://www.rfc-editor.org/rfc/rfcNNNN.txt`.

Name this route in every dispatch that may need RFC words; a fresh context does not know it.

## 1. Establish the review question

A review answers the question in front of the audience:

| Stage | The question |
|---|---|
| Individual draft / Dispatch / BoF proposal | Should the IETF take this on, and if so where? |
| Call for adoption | Should this WG adopt *this document* as its starting point? |
| WG document, in progress | Is the design right, and what has to change? |
| WGLC | Does this document have a coherent and complete design that addresses the stated need? |
| IETF Last Call / directorate | Is there a blocking problem? |
| Approved, RFC Editor queue | Must anything be fixed before publication, and can AUTH48 fix it? |

The table is about the document's stage. The review type is a separate axis: an early directorate
review asks "what has to change" whatever the stage.

Where the group is heading weights the stage. Chairs contemplating a Last Call on a document
nominally in progress means the stage's question is still the one to answer, and the answer should
say whether the document is ready for the next one. A Last Call already declared is visible:
`draft_status` reports the WG stream state, so a running WGLC shows as the WG state line. Intent
short of a declaration is not; the minutes and the list carry it.

Write the question down before reading. Your provisional view, in Step 9, is the answer to it. If
the request carried a note from the AD or chair, read it first -- it usually says what they actually
want looked at.

**Establish the live revision and stage before you read anything.** If you are working from a
gathered corpus, compare it against the live state now. If the corpus is behind or missing, start
the gather now. Verify the text you are about to read cold against the authoritative source.

**At or past WGLC, read the review record before you read the draft.** `review_record(name)` gives
every review and ballot position with the revision it was cast against, and leads with whether
anything has examined the current one.

Read the two halves separately, as it reports them. Reviews and ballot routinely disagree -- an AD
who has re-balloted on the current text while the directorate reviews stop two revisions back is not
a document anyone has re-read. Where a half has examined nothing current, say so first: a concern in
text no reviewer has seen is a different concern.

Read the rows that produced nothing, too. A rejected assignment and a directorate that never
returned are facts about the coverage, and they are the rows the eye skips.

**Check the corpus's source inventory, not the gather's status.** A gather reports `done` while
saying nothing about a source it never had, so `gather_status` will not tell you the issue tracker
is absent. The inventory will: `list_corpora`'s trailing `(list · issues · drafts · minutes)`, or
the `Sources:` line from `overview`. Read it before dispatching anything and know which sources you
have, and from `overview`'s Coverage, where each one stops.

A corpus with no gathered issues is common and is not a defect, but it changes where the record
lives -- a group can run a 400-issue tracker that was never gathered. A review run without the
tracker looks exactly like a review run against a group that does not use one, so establish which
you are in.

**Then ask which corpora the stage needs**, which is not the same as what this one holds. Step 8
licenses claims that a point was never raised anywhere:

| Stage | A "never raised" has to have scanned |
|---|---|
| WG document, WGLC | the WG list, and its issues and pull requests |
| IETF Last Call, and after | the above, plus `last-call` -- Last Call comments go to `last-call@ietf.org`, gathered as a list corpus of its own |
| IESG evaluation | the above, plus the ballot record |

Gather what is missing now. What you cannot gather bounds every negative the review makes -- Step 8.

### The reviewer's own standing

Two things about the person whose name this goes out under change what the findings have to say.
What is their role in this group -- chair, AD, author or editor of this document? And did they write
or edit anything the draft normatively references? Ask rather than infer; the reference list and the
RFCs' own author lines settle the second.

Where either holds it goes in *Your call* at Step 10. Neither softens a finding.

### Reviews of a particular kind

Four kinds carry extra work at this step, in `stages.md`: a directorate assignment, a revision of a
published document, a revision answering a review round that has closed, and a document already
approved by the IESG. Read the section that applies before going on.

## 2. Read the draft cold

Read the entire draft without consulting anything else. Keep each concern found as a record with
four fields, filled when you create it:

- **ID** -- C1, C2, … Keep the ID if the concern becomes an issue; the mapping is how you show your
  work.
- **Claim** -- one sentence about the document.
- **Basis** -- the quotes it rests on, with section numbers. For an absence, what stands in its place
  and where you expected the missing thing to be; the search that confirms it belongs to Step 8,
  since the cold read is not consulting anything yet.
- **From** -- where it came from: the cold read, the charter, a named lens, a question of your own.

Fill nothing else yet. Severity is calibrated against the whole set, and a resolution written before
you understand the problem is one you will defend out of sunk cost.

This list is the control against which everything later gets filtered, and **From** is what makes
that check possible.

**Quote before you claim.** Never build an objection on your paraphrase -- that is where fabricated
findings come from. A concern whose **Basis** you cannot locate is not yet a concern. Locating it is
what has to be true at creation; transcribing it verbatim can wait for the Step 8 sweep.

**Never rule a concern out on a source you could not read.** If a document you would need to
confirm or refute a point is unavailable -- an RFC body, a registration template, a referenced
specification -- raise the point with the uncertainty stated. Say "I could not verify X" and make
the concern anyway. Better still, get the text and cite by section.

## 3. Verify the draft's fit to the venue

_Abandon the Step 4 dispatches if this step ends the review._

If this is a proposal to or an adopted draft of an active Working Group, that group's charter is a
primary artefact and the draft should be evaluated within it. If the draft is out of defined scope
of the charter, point out why and stop here.

The charter check does not apply to a DISPATCH group, which exists to figure out where the proposal
should go.

### The institutional questions

Ask each of these questions against the cold read and the charter. Each one the document does not
answer is a concern.

- Does something this depends on live in another SDO, and if so, what is its status? Who holds
  change control over the semantics?
- Does this depend on policy defined elsewhere? Can the technical work be evaluated without seeing
  that policy?
- Is the mechanism narrower than its framing? If chartered under framing X, would the artefact
  serve purposes X disclaims?
- What is the incremental deployment story, and what does the first implementer get?

On an adoption or DISPATCH review, consider: is the IETF the right venue for this work, and is the
necessary locus of expertise here? Add a concern where the answer is not clearly yes.

## 4. Dispatch tasks

**Say what you are about to do, and how long, before you dispatch.** One short message: the revision
and where it came from, the question you took from the table, which sources exist, and the lenses
going out. Then the estimate -- fifteen to thirty minutes, longer if a gather has to run first.

Step 4 is where the cost is, and everything that makes a run worthless is settled before it: the
wrong revision, the wrong document, the wrong question. Saying them out loud is the last cheap
moment to be corrected.

_Do not dispatch A until any gather kicked off in Step 1 has completed. Nothing in B needs the
gather -- send it regardless._

Dispatch the items below to subagents -- they are our slowest tasks and independent. While they
run, perform Steps 5-7.

Three things go out immediately: **A**, the **actor pass**, and **`bcp14.md`** -- none needs more
than the cold read, and `bcp14.md` always fires, so there is nothing to pre-filter. The remaining
lenses wait on the rubric read, which is what you do while those three run; send them as soon as it
is done, before you start Step 5.

### A. Research other views

Use isolated contexts (e.g., subagents), so other people's material doesn't contaminate your
context. Split the work:

- the mailing list, focusing on directorate reviews, draft discussion, author replies, and chair/AD
  statements
- ballots (none before IESG processing), issues, the repo
- meeting minutes and transcripts

Tell each what the others have. Without it they will reach for the same artefacts -- the ballot file
reads like list traffic, and a Last Call thread reads like a review.

Give each a sentence on what the draft is **and what stage it is at**, then your concerns as
numbered questions. The stage tells a context which artefacts to look for. Provide them with the
following guidance:

- Only provide answers, not a survey. A digest of content pollutes your context.
- Disposition first (prior reviews, chair and AD statements, author replies, the changelog) since
  that settles whether a concern is live.
- Argument threads are expensive: go into one only when a concern turns on what was said there.
- Quote any statement about who controls, decides, benefits from, or is bound by the mechanism,
  whoever made it. These are seldom in the document.
- Stop when a source is stale, empty or absent, and say so -- an empty source is an answer.

On an adoption or DISPATCH review, add a question: who has said they will implement and deploy?
Whether the parties who would *have* to implement it are among them, and whether that adds up to
sufficient support, is yours to judge -- a shortfall is allowed as a new concern.

### B. Architectural review

What is this system, who are the parties, and does the design match positions the IETF has already
written down? Two passes, and the actors come first: it is the general one, and it is what the
second is checked against.

#### The actors

Dispatch this with A. For any draft describing a system, list *every* party the architecture names
or implies, and for each:

- What do they control?
- What can they not refuse?
- What state do they have?
- What protocol affordances are available to them?
- How are their interests potentially aligned with or against other actors?
- What happens when **they** are the adversary?

Then check the draft's own adversary classes against the table: which parties are absent from
theirs, and is each absence defensible? Ask without deciding the answer in advance. The gap may be
a layer confusion, a party the authors assume benign, or nothing at all.

It returns the table and the absences. On a short draft ask for the absences and one line per party
-- the full questionnaire on a one-field extension runs to thousands of words you will not use. The
ranking is yours -- see Step 6.

Stage the table to a file when it lands and pass the path; unlike the draft text, inline does not
count -- Step 10 hands the file over. Only one lens waits on it -- see below.

#### The lenses

Does existing IETF architectural guidance or policy bear on any aspect of the draft? There is one
lens per document in `reference/`, each a short rubric beside the text it covers:

`bcp14.md` normative language · `rfc2804.md` wiretapping · `rfc3552.md` endpoint assumptions ·
`rfc6709.md` protocol extensions · `rfc6950.md` application features in the DNS · `rfc6973.md`
privacy · `rfc7258.md` pervasive monitoring · `rfc7754.md` filtering and blocking · `rfc8820.md` URI
design and ownership · `rfc8890.md` end users · `rfc9170.md` extension viability · `rfc9205.md`
building protocols with HTTP · `rfc9614.md` partitioning for privacy

Read every rubric yourself first, **four to a call** -- batched wider, the output overflows and
spills to a file you then have to read anyway.

Each one's `Firing` section is written to be decidable against the draft text. Where a rubric leaves
you unsure, dispatch it: a wasted lens costs little and a missed one costs a finding. Two things
override that default. If your reason for being unsure is *another lens probably carries this
better*, rule it out and record which lens. And where the rubric's own "rarely fires" language says
this is not its case, follow the rubric.

**A rule-out quotes the criterion it fails.** Name the rubric, quote the sentence from its `Firing`
section that excludes this draft, and say why. One line each. *This is a caching header, not DNS* is
not a rule-out: a rubric's topic does not predict whether its concern applies, which is what the
`Firing` sections are for. Ruling out on another lens is the exception -- name that lens instead.

Off a directorate assignment, scope every dispatch to the team's lane -- `stages.md`, at Step 1. The
lens set itself is not cut to the lane.

**The actor table.** Only `rfc8890.md` waits for it: its fire test depends on the table outright.
Hold that one and dispatch the rest; if the table has landed by the time you finish the rubrics,
which is the usual case, send it with them. It is not reliably the first thing back, so dispatch
`rfc8890.md` when it arrives rather than holding the batch for it. Every other lens, `bcp14.md`
included, takes the table only if it is already in hand.

Write the surviving list down before dispatching anything, and dispatch exactly that list. A lens
held back for the actor table is on the list, not off it.

Send them all at once, and **give each the draft text in the dispatch** rather than making it fetch
its own. Staging it once to a file and passing the path counts, provided you verified that copy
against the authoritative source first. Either way, name the authoritative source alongside it so
the lens can verify anything it means to quote, and spot-check the returns yourself. Name the *RFC
text* route in the same dispatch.

Where the cache is behind the live revision, the live text is the authoritative one -- fetch it and
hand the lenses that, whatever the corpus tool returns.

Paths in a rubric are relative to the rubric, not to anyone's working directory -- two of them point
into the `ietf-http` skill alongside this one. Resolve them where you read the rubric and pass the
result down; a dispatched context has no idea where its rubric came from. If that skill is not
installed and the lens fires, fetch the RFC text yourself and pass it down; if you cannot, the lens
goes in *Could not obtain*, not in *Checked, not raised*.

Each reads its rubric and returns:

- whether it fires on a proper reading, and why;
- the sections it read;
- **the passages it relies on, quoted verbatim with section numbers**;
- what it produces -- a concern, support for one you already hold, a result that the lens does not
  apply, or a result that the lens *supports* the design.

## 5. Review related material

Write down what you currently expect the answer to the Step 1 question to be. The verdict is Step 9
and has a specification; this is here only so you notice when something moves your expectation, and
can say what moved it.

Then, probe the charter, the slides, prior revisions, related and competing drafts, the author's
introductory mail, and the referenced specifications a concern of yours actually turns on -- just
those sections, not the entire reference list.

In particular read the draft's diffs. Use `rfcdiff --diff --stdout old new`, which strips page
headers, footers and form feeds before comparing; the mode matters, because the default writes a
two-column HTML file and emits nothing. Where a finding needs the before and after text, `--ab-diff
--stdout` gives labelled OLD/NEW blocks per section instead. (See *Without the tooling* if you do
not have it.) A section deleted two revisions ago, or a slide the draft does not reflect, hands you a
concern you did not know to ask for. Diff the current revision against the one
before it as a matter of course, and go further back only when a concern turns on *when* something
changed. This step is on the critical path and every extra revision is another read, so when you do
go back, grep the revisions for the one string (flattened, per `quoting.md`) rather than diffing them
pairwise.

**Fetch both sides rather than assuming either is on disk.** A gather stores the revision that was
current when it ran, so most corpora hold exactly one per draft -- a long back-catalogue is the
exception, not the rule, and the *current* revision is missing whenever the draft moved since the
gather. Older revisions are at `https://www.ietf.org/archive/id/draft-...-NN.txt`. `get_draft`
returns what was gathered, which is neither reliably the old side nor reliably the new one, so check
which revision it gave you before diffing it.

If you have reviewed this draft before, your own prior findings are an input -- but after the cold
read, not before it. Anything on that list which did not reappear either gets re-derived or gets
dropped with a reason. A concern does not survive on the strength of having been raised once.

Record what you could not obtain, and what it stops the review from concluding.

## 6. Rank the actors

When the actor table returns, rank it by **mismatch between a party's stated role and their actual
power**. That judgement is yours rather than the dispatched context's, and it sharpens once the
lenses are back.

A mismatch you cannot explain, or an absence the draft does not defend, is a new concern. Add it.

## 7. Ask what you cannot answer

A reviewer's contribution includes the questions they do not know the answer to: what scale is
expected, who is going to operate this, what happens when the deployment is larger than the authors
imagined.

Do not talk yourself out of asking them. You can usually build a case that the concern is fine --
the rate is low, the case is rare, the deployment is bounded -- and building it feels like
diligence. It isn't, when the document has not established the answer itself. If the draft never
says what scale it expects, and you find yourself working out that the scale is probably
acceptable, stop: the finding is that the draft never says. Ask.

Each question you are left with is a new concern. Add it as the question rather than as a claim.

## 8. Assess disposition

For each concern, assess its disposition from the answers you receive:

- Already raised elsewhere: note that.
- Answered by the author: evaluate their answer.
- Position changed *and* an intent to update the draft expressed: update the concern to the
  disposition of that intent.

What the research returns is disposition, not findings: what the record did to concerns you already
have. A point you did not find yourself does not become yours by reading it. Check each returned
point against your Step 2 list before adopting it. A returned point with no matching record is a new
concern or someone else's. Give it a record with **From** naming the lens, or attribute it.

If another reviewer's point is right and unaddressed, support it by attribution -- "I agree with
X's point about Y" -- rather than restating it as your own; if it moves your disposition, say what
moved it.

The authors' own statements are the exception -- applicability claims and clarifications especially,
since those often say something the draft does not. Those are about the document, not the record,
and a concern they hand you is yours.

### Take every concern to the record

The lenses read only the draft, so a finding arrives not knowing whether the point has already been
raised, decided or ruled elsewhere. Close that here, over **every** concern.

**Start with the two scope questions.** Neither needs anything fetched, and they drop concerns
before you spend anything on them.

**Was the silence this document's to fill?** A gap is only a gap where the answer was this
document's to give -- not where it belongs to IANA, a Designated Expert, a deployment, or another
document. A definition deferred to a companion draft, a normative reference in progress, or work the
charter puts elsewhere is not missing: say where it went, or narrow the finding to whether this
document can publish before that one does. A deferral shows up first in the draft's own recent
commits and references.

**Is this IANA's own procedure?** Whether the instructions are well-formed, whether the template
matches the registry, whether IANA will accept them -- IANA reviews that itself.

Instructions to IANA are one-shot. A document replacing another does not restate instructions the
earlier one already executed; the registry exists, and not re-creating it is not a finding.

Reference updates are the exception. A bis has to ask IANA to repoint the entries it inherits at
itself. Where it does not, the registry goes on citing an RFC this document obsoletes, and reviewers
do raise it. The rule above covers instructions already *carried out*, not the ones publication
newly requires.

Delegating a decision to IANA is normal, and more so where what gets decided changes -- the set of
live registrations, entries in flight, whatever is stale by publication. Setting the policy is the
document's job; applying it to a moving set is IANA's. "The document does not say how IANA should
handle these entries" is not a finding.

The **policy** is in scope: the registration policy, the criteria, who decides, what governs the
registry from here. A replacing document that drops those has dropped something still doing work.

Then two questions per surviving concern:

- What introduced or changed this text?
- Where has this *concept* been discussed -- under its own name, not yours?

Search whatever the effort has:

- **Issues and threads** -- `search_corpus` for the concept, `get_issue` to read one out. Where
  issues *are* gathered, do not re-fetch them over the network: a gathered issue already carries its
  closing rationale and each participant's role. Where they are not -- check the inventory, Step 1 --
  the repo's API is the only route and the cost is real, so budget for it. Where a repo holds several
  drafts, a per-draft label narrows the sweep; `list_labels` carries each label's description where
  the repo wrote one, and check how consistently the label is applied before relying on it, since a
  partly-applied label reads as an empty record.
- **Pull requests**, gathered beside the issues -- on a cache gathered since they were added, so
  confirm with `list_files` on `pulls/` rather than assuming. A gathered pull request carries its
  reviews, its disposition and its merge commit, and `get_issue` reads one out by number. The pull
  request is where the reasoning behind a change lives; the issue records the complaint.
- **Commits**, which are *not* gathered. Clone once and the walk below is local; reach for the
  repo's API only where the pull requests are not gathered either.
- **The revision history** -- diff around the text for the revision that introduced it. This is the
  whole of the change record when there is no repo.
- **The draft's changes section.** Its absence beside a substantive change is a finding.
- **Minutes, and the transcript where one exists.** Minutes summarise and drop things; a transcript
  has carried design questions the minutes of the same session omitted entirely.

A search returning nothing is ambiguous -- it can mean the point is unraised, or that the index is
missing or the filter wrong. Confirm the search works before recording silence. And `search_corpus`
is semantic, so a miss there is not evidence of absence: confirm a negative with `grep_corpus` on a
distinctive string, which scans every gathered file, embedded or not, and states what it scanned.
Match within a line -- search `8890`, not `RFC 8890` -- since a phrase split across a mail wrap
misses.

**A negative names the corpora it scanned**, from the stage's list at Step 1: grep over the wrong
corpus is a false negative carrying the confidence of a checked one. A stage-required corpus you
could not gather goes in *Could not obtain* and beside the provisional view, and bounds every
negative resting on it.

**And it carries the ceiling `grep_corpus` printed**, quoted rather than paraphrased. A zero over
`issues/` or `pulls/` states where the GitHub record ends -- `owner/repo through #N (archive built
<date>)` -- and anything above that number was never fetched, so the zero is silent about it. Two
details are easy to restate wrongly: that date is the archive's, not the gather's, and issues and
pull requests share one number sequence, so the ceiling is not per-kind.

Each concern comes back **settled**, **out of scope**, **wrong**, **live**, or **reframed** -- it
holds but is about something else now, usually a condition the change was agreed under.

Dispatch it. Hand over the concerns and the sources, and require evidence per verdict: issue number,
commit, revision, message URL.

What the gate returns changes with how much record exists. Past Last Call, with years of issues, it
prunes hard. On a young document it kills nothing, and the product is the opposite: **being able to
say a concern has never been raised by anyone, anywhere, under any name.** A claim of that shape
rests on `grep_corpus`, not on a semantic miss. That is what licenses a verdict against the
authors' own account of how settled the document is. Record the silence as deliberately as you
would record a decision.

### Text that changed: find the commit, not the keyword

**Any concern about text that was deleted, weakened or retargeted needs the commit that did it.**
Searching the tracker by the finding's own vocabulary will miss it: the issue that authorised the
change is titled in the language of the *decision*, not of the text, so a finding about a charset
requirement never matches an issue called "review the top-level type descriptions".

The reliable path is the repo, and it is three steps: the commit, its pull request, the issue that
pull request closes.

```
git log -S "<a distinctive phrase from the removed text>" -- <draft source>
```

For the second step, `grep_corpus` the sha -- `digests/pulls.md` carries each pull request's merge
commit, so one grep finds it -- falling back to `gh api repos/<org>/<repo>/commits/<sha>/pulls`
where the pulls are not gathered. Read the issue, not just the diff -- it carries the condition the
change was agreed under, and that is usually where the finding actually lives.

What this turns up, in rough order of how often:

- **The change was agreed, and the finding is dead.** Drop it.
- **The change was agreed under a condition that was not met** -- a check the issue said to do
  first, a mitigation proposed alongside. Reopen that issue rather than filing a new one; a new
  issue loses the framing and reads as though nobody had considered it.
- **The commit message or pull request body flags its own follow-up** -- *omitted X, should discuss
  whether it is needed*. If no issue tracks it, that is the finding, and it is stronger than
  anything you would have written yourself.
- **Nothing behind it at all.** Now you can say so.

Findings on text with a decision behind it are unreliable until you have found the decision. A lens
reads the document, so it cannot see one, and will report a deliberate removal in the same voice as
an accidental one.

**The originating issue is not the whole answer.** For text that was *added*, the issue that added
it is easy to find and often says nothing about your concern -- the question you are raising was
settled next door. Search the tracker for the *concept*, not your own words for it, and read the
adjacent issues out. A finding about a stranded contact address can be settled in an issue titled
about an unresponsive change controller, and in another about whether a mailing list can be a
contact -- neither of which matches the words of the finding.

### Processing lens returns

A lens you dispatched is working on your behalf: what it returns is yours -- including a concern you
did not have before -- and you carry it under your own name.

**Never cite a lens you did not read.** Anything you want to cite that no lens returned, open
yourself. A passage returned by the lens that read it is citable. A passage *described* by a lens
that did not read it is not: lenses cross-refer each other, and a description is not the source. A
rubric is a citable source about itself -- recording that a lens does not fire because its rubric
names this as a non-case needs no reading of the RFC. The RFC's own words do.

Spot-check the quotes your findings rest on against the files before anything goes out, following
`quoting.md`. This applies to the draft text you hand the lenses: copy it and you are the extraction
bug, so check what comes back against the authoritative fetch.

Record the lenses you ruled out without dispatching -- each with the criterion it fails, quoted from
its rubric's `Firing` section, per Step 4 -- and the ones that came back not applying or supporting
the design. Keep both so the next reviewer does not rediscover them; `findings.md` writes them up on
request rather than by default.

## 9. Rank the concerns, and form a provisional view

**Settle what you think of the document before deciding how many concerns to surface.** The number
of concerns you find measures how hard you looked, not how bad the document is; a thorough review of
a good document produces a long list, and a long list is not a verdict.

**The bar is set by intended status and stage, not only by contents.** An Experimental
document with a stated experiment and a plan to revise on results is held to a different
standard than a Proposed Standard, and saying so is part of the verdict. Ask what the
document is *for* before asking what is wrong with it.

**A document can be fine.** A review can legitimately be three lines: "you checked the thing you
were asked to check, it holds up, here are two requests for clarification". If that is the honest
answer, give it and stop. Under-reviewing a bad document and over-reviewing a good one are both
failures.

Then rank each concern into **Issues**, **Comments** or **Nits** -- the three headings most IETF
reviews use. They have different economies.

**Issues.** The test: *would this remain regardless of editorial work?* The test is about the
question, not the sentence. An ambiguity that leaves two implementers building different things is
an issue even though a sentence fixes it, because the authors have to decide something first. A
concern where the decision is already made and only the wording is missing is not.

Then ask what breaks if it is not fixed. An issue names one of three things: a decision the authors
must take and have not, an implementation that goes wrong, or a deployment that fails. Any one is
enough.

Watch for the first. A document that has simply not promised anything either way breaks nothing you
can point at, so an undecided question is the easiest of the three to talk yourself out of.

Where the consequence is only that the document's own argument is unpersuasive, it is a comment: the
authors can accept or reject it and nothing else changes.

Both tests have to pass. The survivability test alone will keep findings a reviewer then drops.

Rank on the tests alone; there is no target number of issues.

Step 8's gate has already dropped what was settled or out of scope, so do not re-adjudicate. Two
things come back to you. Whether a closing reason genuinely *engaged* the point is a judgement a
dispatched pass is poor at -- check the ones it called settled. And a concern it reframed ranks on
its new shape, not the one you started with.

Concerns with the same root cause are one issue. If two would be fixed by the same decision, merge
them.

Severity is independent of both tests. They decide the category; severity says how much rides on
the decision.

**Comments.** The survivability test does **not** apply -- being trivially fixable is what they are
for. Keep every concrete, checkable, quotable one.

Keep each comment to a line or two: if one needs a paragraph of setup, it is an issue in disguise or
you are not ready to say it yet. And never fold or delete an issue to make room for a comment.

**Nits.** Typos, stale references, wrong section numbers -- anything an editor fixes without asking
anyone a question.

Form: quoted text, then one short question or one flat statement. No numbering, no interleaved
thanks. Prefer the question -- asking whether you have read it correctly is both more accurate and
harder to dismiss than asserting a defect.

**Finish this step before you write a line of Step 10.**

- **Merge.** Find the concerns that are one decision before writing either up. Where one decision
  fixes only part of two concerns, merge the shared part and leave the residue as its own issue,
  cross-linked by *Related*.
- **Chain.** Where several issues trace to one revision change, work that out now and decide which
  carry a *Caused by*.
- **Calibrate.** Rate the whole set in one pass; severity and confidence are comparative.

The pass will not catch everything. Writing an argument out sometimes shows that a lens assumed
something the draft never says: filling a gap without noticing it is evidence the gap is real.
Expect one or two at Step 10, and count them as findings rather than rework.

## 10. Assemble the findings

The output. Its shape is specified in `findings.md` -- follow it. Each issue is a record you already
have, merged, chained and calibrated at Step 9; assembling is rendering, and fresh thinking here
means Step 9 is unfinished.

That governs *analysis*, not sources. **Re-print every record fact from the tool here** -- revision
numbers, dates, reviewer and balloter names, review results, ballot positions, issue and pull
request numbers -- rather than copying it from your notes. A summary of a tool return is recall.

## Delivering the review

**Optional, and second.** When the reviewer asks for output -- prose, or issues against the
draft's repo -- read `delivering.md` beside this file and follow it. Do it only after they have
chosen what to raise, and only on what they chose. Nothing there should reach back and change a
finding.
