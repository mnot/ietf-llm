# Normative language -- BCP 14 (RFC 2119, RFC 8174)

**Text:** `rfc2119.txt`, `rfc8174.txt`

## Scope

Whether the document's conformance requirements can be conformed to -- who is bound, and whether
that party can tell.

A **BCP**, so a working group cannot settle a departure from it.

The text supplies the keywords' meaning rather than a position to test the design against, so
findings here are about the draft's own sentences.

**Not covered:** whether they are the right requirements, or whether the design is sound. Those are
the other lenses. Nor the RFC 8174 boilerplate itself -- idnits checks it against the keywords used,
and `nits.md` carries that.

## Firing

**Always.** A standards-track document with no BCP 14 keywords at all is itself the finding.

Calibrate on the kind of document. The keywords target protocol behaviour; policy and process
documents tend to carry fewer, and what they do carry binds registrants, expert reviewers, the IESG
-- parties reached by process rather than by an implementation. A tendency, not a rule. Where a
process document is dense with keywords, ask of each whether it binds a party the document can
actually reach, which is test 10.

The first three tests ask which party is bound, so take the actor table if it was passed to you. Do
not wait for it -- the parties are nameable from the draft, and the sweep does not need it at all.

## The sweep

List every sentence containing an uppercase MUST, MUST NOT, REQUIRED, SHALL, SHALL NOT, SHOULD,
SHOULD NOT, RECOMMENDED, NOT RECOMMENDED, MAY or OPTIONAL. Record section and sentence. Run every
test below against each.

Then sweep the other way for test 8, which the keyword list cannot reach.

## The tests

1. **Target.** Who is bound? Context counts -- a section scoped to one party carries down its
   sentences, and that is ordinary drafting. The finding is where the inference does not land: no
   candidate, two candidates, or one the sentence then contradicts.

   *Parameters MUST NOT be ignored* is the shape to catch. Read against the cache it fails test 3;
   read against the origin the verb is wrong, since origins declare which parameters may be ignored
   rather than ignoring them. Two candidates and neither works -- that is the finding, not the
   passive voice on its own.

   A requirement whose subject is the artefact rather than any party is test 2.

2. **Bound to an artefact, not a party.** The subject is a field, a value, a message, a URI --
   *the value MUST be a dictionary* -- rather than whoever sends or receives it. Common enough to
   read as convention, and it leaves error handling undefined: nothing says whether the sender is
   forbidden to emit it, or the recipient obliged to reject it, or what the recipient does instead.

   Ask what a conforming recipient does when the artefact arrives anyway. Where the document defines
   tolerant handling elsewhere the two have to be reconciled -- a MUST the receiver silently absorbs
   is a sender-side rule, and should say so.

3. **Decidable by the target.** Can the bound party evaluate its own conformance from information it
   holds? A cache told not to ignore a parameter *if doing so would bypass server processing* cannot
   -- it knows nothing of the server's processing. Check the terms the requirement turns on: one the
   document never defines makes the requirement undecidable even where the party is named.

   Not the same as observable on the wire. Requirements over internal behaviour are legitimate; a
   requirement its own subject cannot evaluate is not.

4. **SHOULD without exceptions.** RFC 2119 §3 puts the burden on understanding the implications
   before departing, so the document has to say what would justify it. Without that the requirement
   is a MUST or a MAY in disguise. §4 is the same for SHOULD NOT.

5. **MUST without consequence.** RFC 2119 §6 confines these keywords to what interoperation requires
   or what limits harm. What breaks when it is violated? Implicit is fine, unanswerable is not.

6. **MAY without the peer's obligation.** RFC 2119 §5 binds implementations on both sides of an
   option to interoperate across it. Where a MAY changes what a peer sees, is the peer's obligation
   stated?

7. **Keyword in a non-normative context.** A requirement in an example, a note, an appendix, or
   Security or Privacy Considerations. Requirements belong in the specification body. Older RFCs put
   them elsewhere, so precedent is not a defence.

   Worst where the rule appears *only* in a rationale paragraph: a reader cannot then tell a
   requirement from a restatement of one made elsewhere.

8. **Declarative doing normative work.** Behavioural statements carrying a requirement with no
   keyword. Look hardest at the sentence the specification exists for -- it is often the one written
   as description. RFC 8174 makes lowercase *must* and *should* non-normative, so a lowercase
   keyword in a requirement sentence is a defect.

9. **Contradiction and unreachability.** Two requirements that cannot both be satisfied; one whose
   antecedent cannot occur; a MUST some MAY elsewhere overrides.

   Before calling one unreachable, ask whether it is a robustness rule. The document's definitions
   govern its model, not what people send or register -- a prohibition the grammar already makes
   impossible still tells an implementation what to do when it meets one in the wild. Unreachability
   is a finding only where nothing outside the model can produce the case either.

10. **A party the protocol cannot bind.** Requirements on operators, content authors, deployments.
    Not wrong in itself -- raise it where the document's guarantees rest on it.

## Suppression

This fires on every draft and will always return something. Report only where the defect changes
what an implementer builds, or forces a decision the authors have not taken.

Expect Comments and Nits. Most findings here are defects in how a sentence is written, and rewriting
the sentence fixes them -- which is the survivability test failing.

Two shapes reach Issue, both because the fix is a decision rather than a rewrite. Test 9, where two
requirements conflict and the authors have to say which gives way. And tests 1, 3 and 8 landing on
the requirement the specification exists for -- no target, no decidable condition, or no keyword at
all -- where the authors have to settle who is bound and to what before anyone can write the sentence.

## Return

The sweep table -- section, sentence, target, verdict -- then the findings, each naming the test it
failed and quoting the text.
