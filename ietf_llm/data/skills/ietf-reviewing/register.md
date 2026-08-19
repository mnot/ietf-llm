<!-- Generated from the Register section of ietf-contributing/SKILL.md.
     Do not edit this copy; edit the source and run: make register
     make check fails when it is stale. -->

## Register: terse and technical

Governs anything going into the record under a participant's name -- list mail, replies, issues, comments, reviews. Not Internet-Draft or specification text, which has its own conventions; `ietf-http` covers those.

First and foremost: the audience is expert and time-poor. Lead with the point. Cut: restating the thread back, closing with a summary of what you just wrote, opening with "Great point!" or "Thanks, this helps", hedging every claim, any customer-service warmth, summarising what's already been said. If a sentence or clause isn't doing work, drop it. Verbosity is not thoroughness; it is actively harmful, so when in doubt CUT. A long message is less likely to be read and weighed than a short one.

The test is per-sentence: *would removing this change what the recipient does?* If not, cut it. Terseness reads as respect for the reader's time, not as rudeness -- don't pad a point to soften it, and don't add warmth to carry it.

That governs the argument. Where part of a message's job is relational -- conceding a point, withdrawing a request, accepting a steer, thanking someone whose help was real -- the line doing that work is doing a job, and cutting it reads as cold rather than efficient. One line, and meant; not a paragraph, and not at both ends.

Prefer one or two short, concrete examples over exposition. The shortest message that makes the point is the best one; actively cut length rather than padding it. (The IETF's own advice to non-native speakers -- be direct, keep it short, show the concept with a small example -- is good advice for everyone here.)

Brevity is not a licence to write badly. Terse means every sentence does work, not that sentences are half-formed or padded with jargon to look expert. Don't drop articles and verbs into telegraphic fragments; don't reach for an acronym or a piece of in-group shorthand where a plain word is clearer; and never coin a new term when an established one exists -- an invented phrasing forces the reader to decode what you meant instead of weighing what you said. The register to aim for is one a competent participant who hasn't followed every message of the thread can read straight through, without you rehashing that thread to get them there. If a point only parses for the handful of people already deep in the current argument, it isn't terse -- it's incomplete, and it will be read as such.

Supply antecedents, not evidence. Name the document and revision, what an issue proposes, what a term refers to -- once each, where it is first used. The other message's numbers are not antecedents: the recipient has them, a third party doesn't need them to follow the argument, and quoting them back is restatement.

The uniform-polish, fixed-template, self-summarising pattern is itself a tell. Cut the boilerplate and convey the essence with enough context to be digestible. Examples are encouraged when they add legibility, and especially when they help compact the message. Likewise, concrete proposals are encouraged, so long as they can help the group move towards agreement.

Individual words are tells too -- *genuinely*, *honestly*, *seam*, *load-bearing*, *a sharper version of*. What they share is doing emphasis rather than carrying meaning, which is also the test: drop the word and see whether the sentence changed. None of them is wrong once; several in one message is a fingerprint. Sweep for them before it goes, and add to the list as you notice others. Grep the bare token and judge each hit; a narrowed pattern reports clean.

### What the message is for

Before drafting, ask what this particular message has to do, and write only that. Most of what wants to come out has a different home: material for a document you're about to write belongs in the document, not in the reply announcing it. A reply that previews a forthcoming draft spends the group's attention on it twice, and invites the list to argue a scope that doesn't exist yet -- so the message says what's coming and by when, and stops.

The common failure is a small job answered at length. *Acknowledged; I'll withdraw the request and bring a problem statement before IETF 127* is the whole of some replies, and a fuller version of it is worse, not more diligent. Pre-emptive completeness -- answering questions nobody has asked, covering every branch, delivering the document in the message that promises it -- is among the clearest signs a machine wrote it, and it reads as not knowing what the message was for.

This test asks where content belongs; it is not a licence to compress. Where the message *is* the artefact -- a review, a technical argument in a live thread, an issue that has to carry its own context -- the content has nowhere else to go, and it takes the length it takes. The test is *does this belong here*, never *is this long*.

### Wording

* **Drop the first person where the point stands without it.** *I'd note that X* is *X*; *I'd have preferred X* is *X would have been nice*, or nothing. Keep it where the sentence is a position.
* **Replace a pronoun with its referent on the second mention.** Not *scopes it to*, but *scopes the SSRF requirement to* -- a reply quotes the paragraph, not the antecedent.
* **Attach a noun to a bare demonstrative.** Not *This is wrong*, but *This requirement is wrong*; *that*, *these* and *those* the same. Same reason as the pronoun above, and it fails harder -- quoted into a reply, a bare *this* points at nothing at all.
* **Don't announce structure.** *Section 6.1 is the example: it specifies* is *Section 6.1 specifies*.
* **Quote only what the argument uses, and don't restate a quote in prose beside it.** Trim the quote to what the sentence turns on, and let it stand.
* **Answer in sentences.** Not *Un-enrolled signing cost: yes, as a deployment consideration*, but *The un-enrolled signing cost belongs in the draft as a deployment consideration*. A label and a colon is a heading; a message of them reads as curt, not terse.

### Lists

Bullet what is already a list: one item per divergence, per issue, per question asked. Answer numbered questions in the order asked, one item each, so a reader finds theirs without hunting. Don't bullet an argument -- structure imposed on prose is the template tell in another form.

Keep each item on one line, unwrapped. Separate items with a blank line once they run past a line or two; leave short ones tight.

### It is plain text in an archive

Don't hard-wrap. Mail clients and archives reflow, and hard breaks survive into quoted replies as ragged fragments. Leave each prose paragraph on one line. Quoted draft or RFC text keeps the source's own line breaks and indentation -- it is verbatim, and reflowing it changes it.

### The check

Before it goes out, against this section rather than from memory:

1. Does this message do only its own job? Anything here whose home is a document you're about to write?
2. Does every sentence change what the recipient does?
3. Any warmth, padding, or restatement of the thread left -- beyond the one line doing relational work, if there is one?
4. Can a participant who hasn't followed the thread read it straight through -- every document, issue and term given its antecedent once, and nothing quoted back that the recipient already has?
5. Every answer a sentence rather than a label and a colon?
6. Bullets only where the content is a list, spaced if the items run long?
7. First person where the point stands without it? Structure announced instead of stated?
8. A pronoun or a bare *this* on second mention, where a reply would quote the paragraph without the antecedent?
9. Any word doing emphasis rather than work -- the tic list grepped as bare tokens, and whatever else drops without changing the sentence?
10. A quote longer than the argument uses, or restated in prose beside it?
11. Prose hard-wrapped, or quoted text reflowed?
