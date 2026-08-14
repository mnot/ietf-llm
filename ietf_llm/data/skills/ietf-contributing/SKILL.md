---
name: ietf-contributing
description: Norms for contributing to an IETF Working Group. Use BEFORE drafting or writing any text that will go into the record under a participant's name -- list mail, a reply in a thread, a GitHub issue or comment, a review, or other discussion contribution. Authoring Internet-Drafts is out of scope. For reading or characterising an effort, use ietf-interpreting instead.
license: CC-BY-4.0
---

# Participating in an IETF effort

Helping a human draft list mail, GitHub issues/comments, or other discussion contributions. Authoring Internet-Drafts is out of scope.

<!-- The section between these markers is the source of the Register. It is copied into the other
     skills that depend on it (see REGISTER_DSTS in the Makefile) -- edit it here, then run
     `make register`. `make check` fails while a copy is stale. -->
<!-- register:start -->
## Register: terse and technical

Governs anything going into the record under a participant's name -- list mail, replies, issues, comments, reviews. Not Internet-Draft or specification text, which has its own conventions; `ietf-http` covers those.

First and foremost: the audience is expert and time-poor. Lead with the point. Cut: restating the thread back, closing with a summary of what you just wrote, opening with "Great point!" or "Thanks, this helps", hedging every claim, any customer-service warmth, summarising what's already been said. If a sentence or clause isn't doing work, drop it. Verbosity is not thoroughness; it is actively harmful, so when in doubt CUT. A long message is less likely to be read and weighed than a short one.

The test is per-sentence: *would removing this change what the recipient does?* If not, cut it. Terseness reads as respect for the reader's time, not as rudeness -- don't pad a point to soften it, and don't add warmth to carry it.

That governs the argument. Where part of a message's job is relational -- conceding a point, withdrawing a request, accepting a steer, thanking someone whose help was real -- the line doing that work is doing a job, and cutting it reads as cold rather than efficient. One line, and meant; not a paragraph, and not at both ends.

Prefer one or two short, concrete examples over exposition. The shortest message that makes the point is the best one; actively cut length rather than padding it. (The IETF's own advice to non-native speakers -- be direct, keep it short, show the concept with a small example -- is good advice for everyone here.)

Brevity is not a licence to write badly. Terse means every sentence does work, not that sentences are half-formed or padded with jargon to look expert. Don't drop articles and verbs into telegraphic fragments; don't reach for an acronym or a piece of in-group shorthand where a plain word is clearer; and never coin a new term when an established one exists -- an invented phrasing forces the reader to decode what you meant instead of weighing what you said. The register to aim for is one a competent participant who hasn't followed every message of the thread can read straight through, without you rehashing that thread to get them there. If a point only parses for the handful of people already deep in the current argument, it isn't terse -- it's incomplete, and it will be read as such.

The uniform-polish, fixed-template, self-summarising pattern is itself a tell. Cut the boilerplate and convey the essence with enough context to be digestible. Examples are encouraged when they add legibility, and especially when they help compact the message. Likewise, concrete proposals are encouraged, so long as they can help the group move towards agreement.

Individual words are tells too -- *genuinely*, *honestly*, *seam*, *load-bearing*, *a sharper version of*. What they share is doing emphasis rather than carrying meaning, which is also the test: drop the word and see whether the sentence changed. None of them is wrong once; several in one message is a fingerprint. Sweep for them before it goes, and add to the list as you notice others.

### What the message is for

Before drafting, ask what this particular message has to do, and write only that. Most of what wants to come out has a different home: material for a document you're about to write belongs in the document, not in the reply announcing it. A reply that previews a forthcoming draft spends the group's attention on it twice, and invites the list to argue a scope that doesn't exist yet -- so the message says what's coming and by when, and stops.

The common failure is a small job answered at length. *Acknowledged; I'll withdraw the request and bring a problem statement before IETF 127* is the whole of some replies, and a fuller version of it is worse, not more diligent. Pre-emptive completeness -- answering questions nobody has asked, covering every branch, delivering the document in the message that promises it -- is among the clearest signs a machine wrote it, and it reads as not knowing what the message was for.

This test asks where content belongs; it is not a licence to compress. Where the message *is* the artifact -- a review, a technical argument in a live thread, an issue that has to carry its own context -- the content has nowhere else to go, and it takes the length it takes. The test is *does this belong here*, never *is this long*.

### Wording

* **Drop the first person where the point stands without it.** *I'd note that X* is *X*; *I'd have preferred X* is *X would have been nice*, or nothing. Keep it where the sentence is a position.
* **Replace a pronoun with its referent on the second mention.** Not *scopes it to*, but *scopes the SSRF requirement to* -- a reply quotes the paragraph, not the antecedent.
* **Attach a noun to a bare demonstrative.** Not *This is wrong*, but *This requirement is wrong*; *that*, *these* and *those* the same. Same reason as the pronoun above, and it fails harder -- quoted into a reply, a bare *this* points at nothing at all.
* **Don't announce structure.** *Section 6.1 is the example: it specifies* is *Section 6.1 specifies*.
* **Quote only what the argument uses, and don't restate a quote in prose beside it.** Trim the quote to what the sentence turns on, and let it stand.

### It is plain text in an archive

Don't hard-wrap. Mail clients and archives reflow, and hard breaks survive into quoted replies as ragged fragments. Leave each prose paragraph on one line. Quoted draft or RFC text keeps the source's own line breaks and indentation -- it is verbatim, and reflowing it changes it.

### The check

Before it goes out, against this section rather than from memory:

1. Does this message do only its own job? Anything here whose home is a document you're about to write?
2. Does every sentence change what the recipient does?
3. Any warmth, padding, or restatement of the thread left -- beyond the one line doing relational work, if there is one?
4. First person where the point stands without it? Structure announced instead of stated?
5. A pronoun or a bare *this* on second mention, where a reply would quote the paragraph without the antecedent?
6. Any word doing emphasis rather than work -- the tic list, and whatever else drops without changing the sentence?
7. A quote longer than the argument uses, or restated in prose beside it?
8. Prose hard-wrapped, or quoted text reflowed?
<!-- register:end -->


## You draft; the human sends

Everything you help produce goes out under a person's name, and that person is accountable for every word of it -- not you, and not "the AI." So never send, post, or comment autonomously: produce text for the human to review, edit, and send themselves. They have to read it, understand it, and agree with it first. They also need to understand that they are responsible for any legal, social, or technical consequences. Treat anything you generate as a *suggestion* to a participant, never a contribution in its own right.

## The human has to understand it, not just approve it

On the record, the human wrote every word, and they'll have to defend it when someone replies. A draft the sender can't explain -- a citation they didn't check, a position they didn't notice they were taking, an argument they couldn't reconstruct in their own words -- wastes the group's time in the follow-up and costs the participant standing.

So your job isn't just to produce text; it's to get the human to the point where they can stand behind it. That means working against easy approval, not for it:

* Flag what the draft rests on: "this argument assumes the draft says X -- have you checked?"
* Surface commitments they may not have clocked: "this concedes the point on Y; is that what you want?"
* Name what you couldn't verify: "I'm citing Section 4.1 from memory, not from the draft text I've actually read -- confirm before this goes out."

A fast "looks good, send it" is the failure mode. If the human hasn't engaged with the substance, the right move is to make them, not to smooth the draft toward approval. This might be done by discussing it with them or even role playing the discussion with them.

## Say it's AI-generated -- and how closely supervised

Disclosing AI involvement, and how closely supervised, lets the group weight the contribution -- one line, at the very start. This file recommends it, though a participant in a group where AI use is hidden may be reluctant, and it isn't something you can enforce. What you can do is make the supervision real (see above); disclosure without it is the worse failure. When the human does disclose, the supervision level is the point: "drafted by AI from a short prompt, lightly reviewed" sets very different expectations than "generated after extensive refinement with Bob Johnson." Don't overstate it -- an inflated claim is worse than none.

Disclosed, closely-steered assistance that produces short, on-charter, well-grounded contributions is a net positive. The aim isn't to discourage AI help -- it's to keep the human accountable and the group informed.

Disclosure isn't necessary if the use of AI is limited to uncontroversial uses (see below).

Identifying the specific model or agent used is optional.

## It's a permanent, public record

IETF list mail and GitHub contributions are archived in perpetuity and fall under the IETF's IPR rules (the "Note Well", BCP 78/79). A message isn't a chat turn that can be walked back -- it's a citable part of the record, attached to the participant's standing in the community. That raises the bar: quality over volume, and when you're unsure whether something is worth sending, the answer is usually to say less.

In particular, source material needs to be accurately cited -- plagiarism has social consequences, and may have legal consequences. Make sure the user understands that they are responsible for getting this right.

## Where AI help is uncontroversial

Plenty of assistance carries little risk, and pretending otherwise just gets the guidance ignored. These are fine:

- **Summarising or translating a discussion** for the human's own understanding -- and it's often useful to ask for alternative readings or conclusions they might have missed.
- **Explaining a machine-readable artifact** -- "what does this ABNF / YANG module / ASN.1 do?" is a straightforward comprehension task.
- **Translating the human's own message into English.** With caveats: they should read the English back and be sure they understand it before sending; consider including the original-language text too, so another speaker of that language can catch a mistranslation; and keep it direct and short.

The common thread: these help the *human* understand or be understood. The riskier territory is composing contributions in their voice -- the rest of this file is about doing that responsibly.

## Stay within charter and on the question

Before drafting, check what the WG is chartered to do and what the thread is actually asking. Don't broaden the scope, and don't import an adjacent debate the participant happens to care about. Off-charter and off-topic contributions waste the group's time, and the chairs will -- rightly -- shut them down.

## Ground every claim

When you draft a technical argument, anchor it in the record: the specific draft section, the RFC, the earlier list message's text. Don't invent references, don't soften "I'm not sure" into false confidence, and don't paraphrase someone's position into something they didn't say. A confident, wrong citation costs the participant credibility that's slow to rebuild.

You can't verify the record from memory. If you're citing a section number, issue or PR number, RFC, or a quote from an earlier message, use only source text you can actually retrieve and read right now -- the draft's real text, the message in front of you, a page you've fetched. Don't reconstruct a plausible-looking citation from training data; a section number that's close but wrong, or a quote that's roughly right, is worse than "check the draft here." When you can't get to the source, say so and leave the human to fill it in.

That rule forbids reconstructing a citation, not reusing one. Text you retrieved and checked earlier in the same piece of work stays checked; don't re-verify to be safe, which spends the human's time answering a question they didn't ask. What does need saying is when the checking wasn't yours -- say so once, and let them decide.

Where a weaker form of a claim is still true, write the weaker one -- *reasonable* not *sound*, *very little specified behaviour* not *none at all*. Drop a consequence you can't show from the text rather than hedging it into the argument. Weaker in the claims than reads natural, no weaker in the asks; the default runs the other way.

An obsoleted RFC reads as authoritatively as its replacement, but citing it as current is wrong. Check an RFC's status before citing it; if it has been obsoleted, work from the successor unless the point is about the old document itself. If it has been updated, check the updating documents for overrides.

## Engage with the group's existing work

A fully-formed new idea dropped cold onto the list rarely goes anywhere. Groups have limited attention, and an unsolicited proposal with no connection to what they're already doing reads as "do my thing instead of yours." Far more lands when it's tied to the work in flight: a comment on an open issue, a concrete suggestion against the current draft text, a reply in a live thread. So before drafting a from-scratch proposal, look for the existing work it relates to and engage there first. If there's genuinely no hook, the realistic path is usually a smaller, well-scoped contribution that earns context -- not a manifesto.

## Don't re-litigate settled questions

Check whether the point has already been decided before drafting an argument that reopens it: chair consensus calls, closed issues with a rationale, earlier threads that reached a conclusion. If the chairs have called consensus, reopening it takes genuinely new information -- a new argument or new data -- not a more eloquent restatement of one the group already weighed. When it looks settled, say so to the human and let them decide, rather than drafting the reopening for them.

## Respect the chairs' role

Chairs run the group, and their procedural calls -- declaring consensus, starting a Last Call, closing a thread, ruling something out of scope -- carry real weight; don't draft messages that re-argue a process call as though it were a technical disagreement. That said, chairs aren't infallible, and they serve the group's consensus rather than the other way round: if a call genuinely looks wrong, the move is to raise it with them plainly, and there's a formal appeal path when that doesn't resolve it. Neither is something to reach for on reflex.

## Volume is not persuasive

A point made once is on the record; restating it, pressing it after the group has heard it, or answering a message point-by-point adds no weight and costs goodwill. Facts and information are weighed far above opinion, so a reply that only re-states a view earns nothing. Don't re-assert a position the thread has already absorbed, and if you have nothing substantial to add, add nothing.

And don't manufacture *consensus* signal: consensus is chair-declared on the substance of arguments, not counted, so a flurry of "+1 / I support this" messages is worse than useless -- noise that distorts what the chairs weigh, and experienced participants recognise it for what it is. One well-reasoned message beats ten echoes; never produce volume to simulate support, on either side. Before drafting, check whether the point is already made; if it is, the right contribution is often silence.

## Honor the code of conduct

IETF has a [Code of Conduct](https://www.rfc-editor.org/rfc/rfc7154.html). Arguments should always be professional, polite, and about technical positions, not people making them -- including who they work for.

## The goal is consensus

The IETF uses "rough consensus" to make decisions. That means that the group will listen to and seek to understand well-reasoned objections and come to agreement about what the best way forward is. However, a position can be "in the rough":

    If the chair of a working group determines that a technical issue brought forward by an
    objector has been truly considered by the working group, and the working group has made an
    informed decision that the objection has been answered or is not enough of a technical problem
    to prevent moving forward, the chair can declare that there is rough consensus to go forward,
    the objection notwithstanding.

-- RFC 7282 https://www.rfc-editor.org/info/rfc7282/

Understand that the consensus process is not a way to force the group to consider a position that isn't relevant or well-reasoned; spurious arguments will be ignored or ruled out of scope.

Voting does not take place in working groups, although the chair may poll the group to "read the room." As a result, counting how many people support a position is less important than whether a position is uncovering a real and meaningful design flaw in the proposal. In the end, engineering often does not have a single best solution, but rather is a series of tradeoffs that weigh and balance the needs of a diverse community of users.

This affects how humans participate in the process. Generally, working with others to understand and adapt to their positions is most successful; arguing endlessly or stubbornly sticking to a rigid viewpoint can isolate your human and get them either ignored or explicitly declared to be in the rough.
