"""Embedding-model loading and process-level caching.

Two providers handled:
  - sentence-transformers (the default) — constructed directly via
    llm-sentence-transformers and auto-registered in llm's config so
    the model also shows up in `llm embed-models`.
  - everything else `llm` knows about — looked up via llm.get_embedding_model.

The process-level cache (`_MODEL_CACHE`) is load-bearing for the MCP
server: without it, every search_corpus call would re-construct the
wrapper (and, for sentence-transformers, re-load ~130 MB of weights
on first embed()) and the agent would appear to hang.
"""

from __future__ import annotations

import sys
from typing import Any

from ..utils import LogLevel, Verbosity, log

#: Default embedding model. Local, no API key, MPS-accelerated on Apple
#: Silicon via sentence-transformers. ~33M params, 384-dim. Good quality for
#: technical English with a small DB footprint and ~2-minute index time per WG.
DEFAULT_EMBED_MODEL = "sentence-transformers/BAAI/bge-small-en-v1.5"

_ST_PREFIX = "sentence-transformers/"

# Process-level cache of loaded embedding models, keyed by full model id.
_MODEL_CACHE: dict[str, Any] = {}


def _load_sentence_transformer(model_name: str, verbose: Verbosity) -> Any:
    """Construct (and persist registration of) a sentence-transformers model.

    `llm-sentence-transformers` expects HF model names to be added to its
    config file via `llm sentence-transformers register <name>` before they
    become addressable through `llm.get_embedding_model()`. We do that
    write-through automatically and return a directly-constructed model
    instance (the plugin's `register_embedding_models` hook only runs at
    llm startup, so we can't make it visible to `get_embedding_model` in
    the current process).
    """
    bare = model_name[len(_ST_PREFIX) :]
    try:
        # pylint: disable=import-outside-toplevel,import-error
        from llm_sentence_transformers import (  # type: ignore[import-untyped]
            SentenceTransformerModel,
            read_models,
            write_models,
        )
    except ImportError:
        log(
            "`llm-sentence-transformers` is missing — this should ship "
            "with ietf-llm. Try reinstalling: pipx install --force ietf-llm",
            verbose,
            level=LogLevel.ERROR,
        )
        return None
    try:
        models = read_models()
        first_use = not any(m.get("name") == bare for m in models)
        if first_use:
            # Emit on stderr so it clusters with HuggingFace's tqdm bars
            # and survives stdout redirection. Always shown (even with
            # --quiet) because a multi-minute network operation deserves
            # a heads-up.
            print(
                f"\nFirst use of '{bare}': downloading model weights from "
                f"HuggingFace (typically 100-500 MB; one-time, then cached "
                f"in ~/.cache/huggingface/).\n",
                file=sys.stderr,
                flush=True,
            )
            models.append({"name": bare, "trust_remote_code": False})
            write_models(models)
        # Constructing the model triggers the HF download on first use.
        return SentenceTransformerModel(
            f"{_ST_PREFIX}{bare}", bare, False
        )
    except Exception as err:  # pylint: disable=broad-except
        # The underlying stack (huggingface_hub, sentence-transformers,
        # torch) doesn't expose a stable exception hierarchy, so we
        # catch broadly but include the type name for debuggability.
        log(
            f"Could not load sentence-transformers model '{bare}': "
            f"{type(err).__name__}: {err}. "
            f"Try manually: llm sentence-transformers register {bare}",
            verbose,
            level=LogLevel.ERROR,
        )
        return None


def _get_embed_model(model_name: str, verbose: Verbosity) -> Any:
    cached = _MODEL_CACHE.get(model_name)
    if cached is not None:
        return cached

    model: Any = None
    # Local sentence-transformers path: construct directly, skip llm's
    # registry (see _load_sentence_transformer docstring).
    if model_name.startswith(_ST_PREFIX):
        model = _load_sentence_transformer(model_name, verbose)
    else:
        try:
            import llm  # pylint: disable=import-outside-toplevel,import-error
        except ImportError:
            log(
                "`llm` package is missing — this should ship with ietf-llm. "
                "Try reinstalling: pipx install --force ietf-llm",
                verbose,
                level=LogLevel.ERROR,
            )
            return None
        try:
            model = llm.get_embedding_model(  # type: ignore[no-untyped-call]
                model_name
            )
        except Exception as err:  # pylint: disable=broad-except
            # `llm.get_embedding_model` and the various provider plugins
            # don't share a typed exception hierarchy.
            log(
                f"Could not load embedding model '{model_name}': "
                f"{type(err).__name__}: {err}",
                verbose,
                level=LogLevel.ERROR,
            )
            return None

    if model is not None:
        _MODEL_CACHE[model_name] = model
    return model
