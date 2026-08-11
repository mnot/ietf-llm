"""Which generation of the seed store a build belongs to.

A store carries exactly one compatibility tuple, so a schema bump makes
every bundle in it unusable to the new code and every bundle the new code
writes unusable to the old. Serving each generation under its own path is
what lets both exist at once: an old client keeps reading the store built
for it, a new one reads the store built for it, and neither cold-gathers
through the changeover.

The segment is derived from the schema version rather than written down, so
the next bump moves clients to a new path by itself and an unpublished
generation simply soft-fails to a cold gather — which is the existing
behaviour for an uncovered corpus.

It lives here rather than in `config.service` because deriving it needs the
schema version, and `config` sits below `embeddings`: importing it there
closes a cycle (`config.service` → `embeddings.storage` → `store.corpus` →
`config.service`). `config.service.seed_url()` stays what a user configures
— a host, not one generation's contents — and this composes on top.
"""

from __future__ import annotations

from typing import Optional

from ..config import service as service_config


def generation() -> str:
    """The path segment for this build, e.g. `v11`.

    Stores published before this existed live at the base URL; those are the
    earlier generations and are simply no longer pointed at.
    """
    # Local: see the module docstring on the cycle this avoids.
    # pylint: disable-next=import-outside-toplevel
    from ..embeddings.storage import _SCHEMA_VERSION

    return f"v{_SCHEMA_VERSION}"


#: The RFC full-text corpus publishes to its own store under this segment.
#:
#: Not a member of the shared store, and it cannot be one: a store carries a
#: single compatibility tuple, and this corpus's chunks come from rfc.fyi's
#: chunker (`rfcfyi-1`) while every gathered corpus carries ours (`2`). Those
#: can never match, so putting them in one store means one of them is always
#: refused — which is exactly what the tuple is for. It is a different
#: embedding generation, so it gets a different store.
RFC_SEGMENT = "rfcs"


def store_url() -> Optional[str]:
    """The store a client should read: the configured base plus the generation.

    Applied to whatever base is configured, not just the default, so someone
    running their own mirror gets the same versioned layout without having to
    track schema numbers.
    """
    base = service_config.seed_url()
    if not base:
        return None
    return f"{base.rstrip('/')}/{generation()}/"


def rfc_store_url() -> Optional[str]:
    """The store holding the RFC full-text corpus.

    A sibling of the gathered store rather than a path inside it, for the
    reason `RFC_SEGMENT` gives. Versioned the same way, so a schema bump moves
    both together.
    """
    base = service_config.seed_url()
    if not base:
        return None
    return f"{base.rstrip('/')}/{RFC_SEGMENT}/{generation()}/"
