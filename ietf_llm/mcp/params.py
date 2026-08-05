"""Reusable annotated parameter types for the MCP tools.

Every tool parameter carries a `description`, which is where per-parameter
semantics belong: MCP puts them in the tool's `inputSchema`, attached to the
argument they describe, instead of leaving a client to match a paragraph of
docstring prose against a name in a signature. That in turn lets a tool's
description say what the tool *is* and when to prefer it, rather than
re-documenting its own signature.

Keep them to **one line**. A description is serialized into every tool that
takes the parameter, so a paragraph here is paid for several times over; the
tool description is the place for anything that needs more room. State what the
argument means and what it changes, not when to reach for the tool.

The aliases here are the parameters that mean the same thing in every tool that
takes them — one wording, so `since` cannot drift between `search_corpus` and
`read_digest`. Anything tool-specific is annotated inline at its own definition.

Bounds (`ge` / `le`) are part of the contract too: a limit in the schema is
enforced by argument validation, where the same limit in prose is a suggestion
the client may or may not honour.
"""

from __future__ import annotations

from typing import Annotated, Optional

from pydantic import Field

from .common import MAX_SEARCH_K

#: The corpus every read tool is scoped to.
Corpus = Annotated[
    str,
    Field(
        description=(
            "Effort to read: a WG/RG shortname, standalone list, draft set, or "
            "`x-` topic. `list_corpora` names them; `find_efforts` resolves one."
        )
    ),
]

#: The (file, chunk_idx) pair that identifies a chunk everywhere in the API.
CorpusFile = Annotated[
    str,
    Field(description="Corpus-relative path, as shown in hits and `list_files`."),
]

ChunkIdx = Annotated[
    int,
    Field(description="0-based chunk index, shown in hits as `chunk=N`.", ge=0),
]

#: ISO-8601 window bounds, shared by the search and digest tools.
Since = Annotated[
    Optional[str],
    Field(
        description=(
            "Lower bound, ISO 8601. Only list and GitHub content is dated; "
            "undated draft/transcript chunks drop out when either bound is set."
        )
    ),
]

Until = Annotated[
    Optional[str],
    Field(description="Upper bound, ISO 8601. Same dating caveat as `since`."),
]

#: Structural role, stamped into section headers by the people registry.
Role = Annotated[
    Optional[str],
    Field(description="Restrict to a role: `Chair`, `Author`, `Editor`, `AD`."),
]

#: Author substring match against the section header.
Author = Annotated[
    Optional[str],
    Field(
        description=(
            "Restrict to a person, substring-matched, so a surname alone works. "
            "Undated draft/transcript chunks have no author and drop out."
        )
    ),
]

#: GitHub issue / thread state.
State = Annotated[
    Optional[str],
    Field(description="`closed` for settled positions, `open` for live debate."),
]

#: A GitHub label, as curated by the effort itself.
Label = Annotated[
    Optional[str],
    Field(description="Restrict to one GitHub label; `list_labels` names them."),
]

#: SQL LIKE pattern over the corpus-relative path.
FilePattern = Annotated[
    Optional[str],
    Field(
        description=(
            "SQL LIKE pattern over the path, `%` as wildcard: `threads/%`, "
            "`issues/%`, `drafts/%`."
        )
    ),
]

#: Shared search facets — same behaviour in `search_corpus`, `find_related`
#: and `search_corpora`, so they share one wording too.
GroupBy = Annotated[
    Optional[str],
    Field(description="`file` collapses hits to one row per file, with a count."),
]

SnippetChars = Annotated[
    Optional[int],
    Field(
        description="Per-hit snippet budget; raise it and lower `limit` to match.",
        ge=1,
    ),
]

Diversify = Annotated[
    bool,
    Field(
        description=(
            "Spread hits across matching threads instead of stacking the most "
            "relevant one. Ignored under `sort=date` / `group_by=file`."
        )
    ),
]

CollapseVersions = Annotated[
    bool,
    Field(description="Hide older draft revisions when a newer one also matched."),
]

#: The semantic-search query string.
Query = Annotated[
    str,
    Field(description="What to look for, in natural language — this is semantic."),
]

#: The chunk-count cap for the semantic-search tools. The bound is the same
#: constant the implementation clamps to — a bound in the schema must not
#: promise something the code then silently overrides.
Limit = Annotated[int, Field(description="Chunks to return.", ge=1, le=MAX_SEARCH_K)]

#: An unbounded row cap, for the tools whose implementation does not clamp.
#: Do not give this one an `le`: inventing a ceiling here would refuse calls
#: that used to work.
RowLimit = Annotated[int, Field(description="Rows to return.", ge=1)]

#: A file restricted to the per-message surfaces — the tools that read the
#: section structure refuse anything else.
ThreadFile = Annotated[
    str,
    Field(
        description=(
            "Corpus-relative path of a thread or issue file "
            "(`threads/….md`, `issues/org-repo/N.md`); others are refused."
        )
    ),
]

#: An Internet-Draft name. The version suffix is optional throughout — the
#: newest cached revision is used when it is absent.
DraftName = Annotated[
    str,
    Field(
        description=(
            "Draft name (`draft-ietf-httpbis-resumable-upload`); the version "
            "suffix is optional."
        )
    ),
]

#: Paging offset for the verbatim-text readers.
StartLine = Annotated[int, Field(description="First line, 1-based.", ge=1)]
