# Delivering the review

The optional, second half of `SKILL.md`. Read it only when the reviewer asks for output, after they
have chosen what to raise, and only on what they chose. Nothing here should reach back and change a
finding.

Two shapes -- prose, or issues against the draft's repo. Both rely on the findings rather than
re-verifying them, and in both the human sends.

Both are record text, so **read `register.md` beside this file before writing a line of either**.
It is a generated copy of the section in `ietf-contributing`, whose other sections govern
disclosure and what the sender has to be able to defend.

## Writing the review

The Register in short, which is not a substitute for reading it:

- Terse. No padding to soften a finding, no warmth to carry it.
- Per sentence: would removing this change what the recipient does? If not, cut it.
- No hard wrap. Quoted text keeps the source's line breaks.
- Drop the first person where the point stands without it.
- Replace a pronoun with its referent on the second mention; attach a noun to a bare *this*.
- Do not announce structure.
- Quote only what the argument uses; do not restate a quote in prose beside it.

Rely on the findings. The quotes were verified at Step 8 and re-checking them here buys nothing but
delay -- the reviewer is waiting, and friction here is what stops them asking next time. If the Step
8 verification was not yours, because the findings came from a context you did not audit, say so
once and let the reviewer decide -- do not silently re-derive.

Get the conventional form from a recent real review of the type you are writing -- for a
directorate, one by that team; for a Last Call comment, one from the last-call list. The headings
below are the directorate convention and will not always fit.

The review is a selection from the findings, not a rendering of them: it usually carries fewer
issues than the findings hold. Cut on relevance to the audience and the stage, not to a number.
Plain headings -- `Issues`, `Comments`, `Nits` -- carrying only what was chosen.

**Drop what the reviewer's role lets them simply do.** An unlabelled issue, a missing changes
section, a stale link -- where a chair or editor can fix it directly, it is a note to them, not a
review item. Step 1 established the role.

### Open with the verdict

Open with the disposition, in one or two sentences, answering the Step 1 question. Then
the concerns.

Do not open with methodology or a summary of the draft. A line of genuine thanks before the verdict
is often conventional in directorate reviews; what to avoid is praise standing in for a verdict, or
warmth calibrated to soften one.

**Where the reviewer's standing makes the verdict a pre-emption, disclaim instead.** A chair
reviewing the last call they declared, or an author or editor of the document, cannot open with a
disposition without pre-empting the consensus call they will have to make. Open by saying the
findings are input to the discussion and not a position. Something has to stand where the verdict
would -- one line does it, and leaving the slot empty reads as an omission.

State the concession after the verdict, not instead of it. *This should not be picked up in its
current state, though something of roughly this shape might be* is a verdict. *Here are some
concerns* is not.

**A favourable verdict is valid.** *This is fine, and here are two things worth clarifying* carries
the same obligation to say why -- usually by naming what the document is for. Do not bury approval
under the findings, do not phrase it as the absence of objections, and do not let a run of
clarifications imply a severity the disposition does not support.

On a directorate review the disposition also goes in the datatracker `Result` field. **The values
are per-team**, so take them from the same recent review you took the form from. Most teams --
httpdir, artart, tsvart, intdir, dnsdir -- offer `Ready`, `Ready with Nits`, `Ready with Issues`,
`Almost Ready`, `On the Right Track`, `Not Ready`. secdir and opsdir offer a different set: `Ready`,
`Has Nits`, `Has Issues`, `Serious Issues`, `Not Ready`. Others differ again, so check rather than
assume. Pick the result the document earns from the set that team actually uses; the prose verdict
should say the same thing the field does.

**Do not copy all finding contents** -- keep what is relevant to the review type being created.
*Severity*, *Confidence* and *Grounding* never ship in either shape; they were for the reviewer.

**Decline whole categories out loud** rather than skipping silently: "I'll skip editorial issues
given the state of the text."

**A departure from a BCP is raised as a question for the wider review, not as the group being
wrong.** The working group may have judged the BCP inapplicable or the exception warranted; what
it cannot do is make that judgement final. Say what the BCP requires, what the document does, and
that the two need reconciling before publication -- and say the group considered it, where the
record shows they did.

**A declared scope is permeable.** Reviewers announce a lane and then say the important thing
anyway, flagging it -- "this is for the security directorate, but". If a finding matters and sits
just outside the scope you declared, say it and mark it out of lane.

### Cut what does not earn its place

A short review means the document is fine or it is hopeless; the long ones are the salvageable
middle. Use it as a sanity check on where yours sits, never as a budget.

The Register's per-sentence test, with the authors as the recipient. Over-length comes from padding
the argument, not from having found a lot of things. Most of it comes from these:

- **Justifying the finding to the reviewer** -- why this is an issue rather than a nit, why it is
  worth raising. That was the findings' job and it is done.
- **Pre-empting the authors at length.** A clause where their best objection changes the ask;
  otherwise nothing.
- **Quoting past what the argument uses.** The Register rule, and where most of the length goes.
- **Drawing the inference for the author.** *The case* is built so the reviewer can check the
  claim; a review needs only enough for the author to locate the defect and see which decision is
  theirs. State what the document says and what it does not, and stop.

**Where the fix is a wording change, propose the wording.** *Should this be s/x/y/?* replaces the
argument for it -- once the reader sees the substitution, the case for it is usually unnecessary.

Delete prose. Do not delete defects. A finding the reviewer chose to raise stays in, even if the
prose around it goes.

**A finding short of its support goes back to the reviewer**, not into the review. Six fully
supported beat seven with one soft; the soft one is what the reply argues with.

### Final pass

Double check the review for each of these:

- **Every numbered item in the Register's own check, in order** -- open `register.md` and read them
  off it. A pass done from memory is the failure this list exists to catch.
- Is each claim about the **document** or about the **topic**? Cut comments on the topic.
- Does every piece of history earn its place? Recite a decision's history only where the ask
  depends on it. Provenance never ships as corroboration, and quoting a named participant's past
  position needs a reason beyond its being on the record -- most of all where they are an author
  of the document.
- Does the quoted text actually support the claim made about it?
- Does anything read as position-taking without reference to what the document says? Cut it.
- Does a sentence score off the authors or an earlier group? Name the prior work; cut the moral
  drawn from it.
- Have you answered the Step 1 question, in the first two sentences, unambiguously?

## Filing issues

The other output shape, in a group that runs its draft from a repo. Run the Register's check over
each issue body before showing it to the reviewer.

**One at a time, confirmed before each.** Show the reviewer the exact text -- title and body -- and
file it only when they have approved that one. Never file a batch on a single approval, and never
file ahead while they are still reading. Approval of one is not approval of the next.

### Search the tracker first

Every finding, against open and closed issues both, before drafting any of them. For anything about
text that changed, Step 8's `git log -S` walk finds the right issue where a keyword search will not.

- **Already open.** Comment there. Do not file again.
- **Closed, and the reasoning holds.** Drop the finding. Step 9 asked this; the tracker is where the
  answer is.
- **Closed, and the change agreed in it never landed in the text.** Reopen it. A new issue loses the
  thread that agreed the fix.
- **The position was argued and rejected.** Reframing to a different ask is fair; refiling the same
  one is not. Say in the issue that you have read the earlier thread.

### What gets filed as what

| | Issue | Pull request |
|---|---|---|
| **Issue** | one each | rarely -- see below |
| **Comment** | always, ganged or not | optional, where the answer is not in doubt |
| **Nit** | no | yes, chunked into coherent batches |

A nit whose fix requires choosing between two options is a comment. *These two spellings differ* is
a nit only if you can say which one goes.

Comments stand alone where each names a section and has a determinate answer, and gang where they
are editorial or cluster on one section. Twenty filed separately bury the issues; that is what to
weigh it against.

Each issue carries its own context -- the revision, the section, the quotes. There is no covering
message and no shared thread, so nothing may refer to the findings or to "the review".

### What transfers

*The case* and *Can be resolved if* go over almost verbatim. *Related* and *Caused by* become
issue links, which is the one thing the tracker does better than prose.

**Label the editorial ones.** Repos commonly carry an `editorial` label for issues that need no WG
discussion; check `gh label list` and apply it where it exists. It is how the chairs keep the
substantive list readable.

### Pull requests

Only where the fix is not a decision -- the Step 9 survivability test again. If the text follows
from something already settled, a pull request saves a round trip. If the fix embodies the decision,
it front-runs the group and belongs in an issue.

**A pull request against an Issue is a signal you misranked it.** An Issue is a finding where the
authors have to decide something first; if you can write the text yourself, the decision was already
made, and it was a Comment.

Open the issue first for anything substantive and reference it from the pull request. Substantive
text arriving as a pull request with nothing behind it is the pattern Step 1 tells you to flag; do
not create it.

### Where the verdict goes

A tracker has no slot for the Step 1 answer, and a set of issues without one reads as nits whatever
is in them. It goes in a covering message to the list, the ballot, or the Last Call comment. Say
which, rather than letting it drop.
