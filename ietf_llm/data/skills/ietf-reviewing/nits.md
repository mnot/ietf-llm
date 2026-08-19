# Nits

Read at Step 4 of `SKILL.md` and dispatch it there, with `bcp14.md` and the actor pass. Dispatch
matters here beyond cost: the tool's response carries a copy of the whole draft, and it has no
business in the reviewing context.

A nit is what an editor fixes without asking anyone a question -- Step 9's definition, and the bar
here. A tool finds most of them. This file is for the ones it cannot reach, and the ones that turn
out not to be nits.

**Not covered:** BCP 14 keyword defects, which are `reference/bcp14.md`'s. Whether the IANA
instructions are well-formed, which Step 8 rules out. A term spelled two ways is here, but only
where you can say which spelling goes -- `delivering.md` sets that test.

## Stage

The stage decides what the pass returns, and on two stages whether it runs at all. It is settled at
Step 1, so the caller applies this table before dispatching -- a dispatched context that reads it
here has already cost a run.

| Stage | The pass |
|---|---|
| Adoption, DISPATCH | Not dispatched. The question is whether to take the work on. |
| WG document in progress | Structural gaps only: a required section missing, a reference that does not resolve. |
| WGLC | Everything below. Fixes cost nobody anything at this stage. |
| IETF Last Call, directorate | Everything below, weighted to what a shepherd answers for -- references, downrefs, obsoletes and updates. |
| IESG evaluation, and after | Sort by route, per `special-cases.md`. Most of it belongs to AUTH48. |

A skip is recorded, not silent. Nothing else in the review reports on nits, so an adoption review
that says nothing about them reads as a pass that found none.

## The dispatch

Hand over the draft text, the draft's filename, the intended status, the stage row above, and this
file. The dispatched context runs the tool, interprets what comes back, and returns the surviving
items in the shape at the end of this file, scoped to the row it was given. The raw report stays
there.

Everything below is that context's brief.

## Run idnits rather than emulating it

```
curl -s -X POST https://author-tools.ietf.org/api/idnits \
     -F "file=@draft-<name>-<NN>.txt" | sed '/Summary:/q'
```

The local file must carry the draft's own name. idnits compares the filename against the document
name, so a copy saved as `d.txt` returns a `Mismatching filename` warning about your download.

The `sed` cuts the response at the summary line. What follows it is the entire draft, line-numbered,
and it is the bulk of the response. No warning is lost by cutting there.

The report's first line names the version. The hosted service still serves v2; `npx
@ietf-tools/idnits <file>` runs v3, a separate implementation with its own check set, so a clean v3
run is not a clean v2 run.

Between them the tool covers all of this. Do not hand-check any of it:

- the TLP and Status of This Memo boilerplate
- the filename, revision, document date and expiry
- required sections present and non-empty
- references cited but not listed, and listed but not cited
- obsoleted RFCs and superseded draft revisions
- references against the intended status
- the RFC 8174 boilerplate against the keywords used
- line and page length, tabs, form feeds and control characters
- example domains and addresses
- the author count

**Without the network**, expiry and document dates, line length, tabs and form feeds, boilerplate
presence, and citations against the reference list are still cheap. Reference status, obsoletion and
revision currency are not. Record what you could not run, for *Could not obtain*.

## Interpreting the output

idnits marks each line `**` error, `~~` flaw, `==` warning or `--` comment. That marker rates the
tool's own check and does not carry over to Step 9: a `--` comment can be an Issue and a `**` error
can be a nit. v2's markers are unreliable in their own terms too -- the shepherd write-up template
says so in its own question 14.

So open the text every line names and quote it. An idnits line restated unverified is an unchecked
quote.

Common reasons to drop a line:

- The document-date and expiry comments, on any draft a few months old. Report one only where
  expiry bears on the stage.
- The copyright year, which the RFC Editor sets.
- Anything *What the RFC Editor fixes* covers, below.
- A warning the document refutes on inspection.

**Dropped lines do not come back in the return.** They belong in `findings.md`'s *Checked, not
raised*. Return the count, and name only a line a reader would expect to see raised.

## What the tool does not check

The shepherd write-up asks these of the shepherd. The document facts behind them are the reviewer's.

### The document's account of itself

- The abstract stands alone and carries no citations (RFC 7322 §4.3). The authors' guideline is 50
  to 150 words.
- Where the draft obsoletes or updates an RFC, the header, the abstract and the introduction all say
  so, and the introduction says what changed and why. A header `Obsoletes:` the abstract does not
  mention is the common form.
- The datatracker's `Obsoletes` and `Updates` metadata agrees with the header.
- Abbreviations expanded on first use and in the title, and the abbreviation used thereafter (RFC
  7322 §3.6). Check the expansion, not its presence.
- The Requirements Language section sits in or after the introduction (RFC 7322 §4.8.2).
- A document obsoleting another has considered the errata filed against it.

### References

- Split normative and informative, and each reference on the right side of the split.
- Downward references against the DOWNREF registry at <https://datatracker.ietf.org/doc/downref/>.
  The tool flags candidates; whether one is registered already is a lookup you make.
- Normative references to drafts that are not ready, and to anything not freely available.
- Internet-Draft references are informative only, and carry the date, the full filename with its
  version, and "Work in Progress" (RFC 7322 §4.8.6.4).
- A reference to an RFC in the STD or BCP subseries carries the subseries number (RFC 7322 §4.8.6).
- A URI is not a reference on its own (RFC 7322 §4.8.6.1).

### Examples

Domains, addresses and telephone numbers come from the reserved ranges: `.example` and the RFC 2606
names, RFC 5737 for IPv4, RFC 3849 for IPv6, `+1-<area code>-555-<0100-0199>`. An example carrying
an address or domain someone actually holds is not a nit -- it will be used.

### Formal languages

ABNF, CDDL, YANG, JSON Schema and XML are validated by running the validator, never by reading. If
you cannot run it, say so rather than implying you did.

## What the RFC Editor fixes without being asked

RFC 7322 §3 marks its own two classes: a lowercase "must" is a change the RFC Production Center
applies automatically, a lowercase "should" one it may question. Nothing the RPC applies
automatically is a review finding. Spacing, series commas, punctuation against quotation marks,
title-case section headings, reference ordering, an empty IANA Considerations section, and the
choice between American and British spelling in an internally consistent document are all of that
class.

What survives is what the RPC cannot decide for the authors: whether the abstract says what the
document does, whether an expansion is the right expansion, whether a reference points at the right
document, whether an example address belongs to somebody.

Current guidance is at <https://www.rfc-editor.org/authors/rfc-style-guide/>, which updates RFC 7322
and carries the recent items -- inclusive language, HTTPS URIs, no hyphenated `[RFC5011]-style`
compounds, no didactic capitalisation. Read it there rather than from RFC 7322 alone.

## Nits that are not nits

These come out of the pass and rank above Nits at Step 9. Say which they are; a Nits heading invites
the authors to skim.

- **A missing or empty Security Considerations section**, or a claim that the document raises none,
  on anything specifying protocol behaviour. Issue.
- **A downref not in the registry, or a normative reference to a draft that is not ready.** Comment.
- **Header, abstract and introduction disagreeing about what is obsoleted or updated.** Which one is
  right is a question for the authors, so it is a Comment.
- **An IANA Considerations section inconsistent with the body.** Which side is right is a question
  for the authors, so it is a Comment. Whether the instructions are well-formed is IANA's, per
  Step 8; whether they match what the document specifies is not.

## Return

Short. The interpretation happened here, and its working stays here.

- Each surviving item: the section, the text quoted verbatim, one line on what is wrong. Step 9
  ranks and `findings.md` renders.
- The tool and version run.
- How many tool lines were dropped, and any one a reader would expect to see raised.
- What could not be checked.
