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

import os
import sys
import threading
from typing import Any, Iterable, Sequence

from .. import oai_compat
from ..utils import LogLevel, Verbosity, log

#: Default embedding model. Local, no API key, MPS-accelerated on Apple
#: Silicon via sentence-transformers. ~33M params, 384-dim. Good quality for
#: technical English with a small DB footprint and ~2-minute index time per WG.
DEFAULT_EMBED_MODEL = "sentence-transformers/BAAI/bge-small-en-v1.5"

_ST_PREFIX = "sentence-transformers/"

#: Protocol-neutral prefix selecting the OpenAI-compatible remote backend.
#: The id after the prefix is the model name sent to the endpoint, e.g.
#: "openai-embed/@cf/baai/bge-small-en-v1.5". Provider-neutral: Cloudflare
#: Workers AI, OpenAI, a self-hosted vLLM / TEI, etc. are all just config.
_OPENAI_EMBED_PREFIX = "openai-embed/"


def is_remote_embed_model(model_name: str) -> bool:
    """True if ``model_name`` selects the remote OpenAI-compatible backend.

    Such a backend has no local weights and is network-backed, so callers
    (e.g. the server's prewarm) can skip on-device-only work -- there is
    nothing to load, and a network round-trip must not gate readiness.
    """
    return model_name.startswith(_OPENAI_EMBED_PREFIX)


# Process-level cache of loaded embedding models, keyed by full model id.
_MODEL_CACHE: dict[str, Any] = {}
# Serialise concurrent loads of the same model. With background
# pre-warm now running in a daemon thread, a search call landing
# during startup would otherwise trigger a second redundant load.
_MODEL_LOAD_LOCK = threading.Lock()


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
        # pylint: disable=import-outside-toplevel,import-error,line-too-long
        from llm_sentence_transformers import (  # type: ignore[import-untyped,import-not-found,unused-ignore]
            SentenceTransformerModel,
            read_models,
            write_models,
        )
    except ImportError:
        log(
            "On-device embeddings need the optional `local-embeddings` extra "
            "(it pulls in sentence-transformers and torch):\n"
            "  pipx install 'ietf-llm[local-embeddings]'\n"
            "  # or with pip: pip install 'ietf-llm[local-embeddings]'\n"
            "Alternatively, set a remote OpenAI-compatible endpoint "
            "(IETF_LLM_EMBED_BASE_URL) and use an 'openai-embed/<model>' id, "
            "which needs no torch.",
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
        return SentenceTransformerModel(f"{_ST_PREFIX}{bare}", bare, False)
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


class _OpenAICompatEmbeddingModel:
    """Embeddings via an OpenAI-compatible ``POST {base}/embeddings`` endpoint.

    Exposes the same ``embed`` / ``embed_multi`` surface the rest of the
    package expects, so it drops in behind ``_get_embed_model`` exactly
    like the local sentence-transformers model. Provider-neutral: the
    endpoint, auth headers, and model id are configuration, so the
    identical code serves Cloudflare Workers AI, OpenAI, a self-hosted
    vLLM / TEI, etc.

    ``embed_multi`` batches to ``batch_size`` inputs per request and
    retries 429 / 5xx with exponential backoff + jitter (bulk ingest is
    rate-sensitive); ``embed`` is the single-input query-time case.
    """

    def __init__(
        self,
        model_id: str,
        base_url: str,
        headers: dict[str, str],
        *,
        batch_size: int,
        timeout: float,
        max_retries: int,
    ) -> None:
        self._model_id = model_id
        self._url = base_url.rstrip("/") + "/embeddings"
        self._headers = {"Content-Type": "application/json", **headers}
        self._batch_size = max(1, batch_size)
        self._timeout = timeout
        self._max_retries = max(0, max_retries)

    def embed(self, text: str) -> list[float]:
        return self._embed_batch([text])[0]

    def embed_multi(self, texts: Iterable[str]) -> list[list[float]]:
        items = list(texts)
        out: list[list[float]] = []
        for start in range(0, len(items), self._batch_size):
            out.extend(self._embed_batch(items[start : start + self._batch_size]))
        return out

    def _embed_batch(self, batch: Sequence[str]) -> list[list[float]]:
        if not batch:
            return []
        payload = {"model": self._model_id, "input": list(batch)}
        body = oai_compat.post_json_with_retry(
            self._url,
            payload,
            self._headers,
            timeout=self._timeout,
            max_retries=self._max_retries,
        )
        # OpenAI returns one object per input carrying an explicit `index`;
        # sort by it so the output order matches the input regardless of
        # what order the server happens to emit.
        rows = sorted(body["data"], key=lambda d: int(d.get("index", 0)))
        return [[float(x) for x in row["embedding"]] for row in rows]


def _load_openai_compat(model_name: str, verbose: Verbosity) -> Any:
    """Build the OpenAI-compatible remote embedding backend from the env.

    The model id is the part of ``model_name`` after the prefix; the
    endpoint, auth, and tuning come from the environment so secrets never
    live in code or persisted config. Returns None (with a logged reason)
    when the endpoint isn't configured, matching the other loaders.
    """
    model_id = model_name[len(_OPENAI_EMBED_PREFIX) :]
    base_url = os.environ.get("IETF_LLM_EMBED_BASE_URL", "").strip()
    if not base_url:
        log(
            "Remote embedding model configured but IETF_LLM_EMBED_BASE_URL "
            "is not set. Set the embeddings endpoint base URL (e.g. "
            "https://host/v1) in the environment.",
            verbose,
            level=LogLevel.ERROR,
        )
        return None
    headers = oai_compat.build_headers(
        os.environ.get("IETF_LLM_EMBED_TOKEN", ""),
        os.environ.get("IETF_LLM_EMBED_HEADERS", ""),
        "IETF_LLM_EMBED_HEADERS",
        verbose,
    )
    return _OpenAICompatEmbeddingModel(
        model_id,
        base_url,
        headers,
        batch_size=oai_compat.env_int("IETF_LLM_EMBED_BATCH", 96),
        timeout=oai_compat.env_float("IETF_LLM_EMBED_TIMEOUT", 10.0),
        max_retries=oai_compat.env_int("IETF_LLM_EMBED_RETRIES", 3),
    )


def _get_embed_model(model_name: str, verbose: Verbosity) -> Any:
    # Double-checked locking: the unlocked fast-path returns
    # immediately on warm-cache hits (the common case after first
    # load). The lock-then-re-check serialises concurrent loaders so
    # a search arriving mid-prewarm doesn't trigger a duplicate load.
    cached = _MODEL_CACHE.get(model_name)
    if cached is not None:
        return cached
    with _MODEL_LOAD_LOCK:
        cached = _MODEL_CACHE.get(model_name)
        if cached is not None:
            return cached
        model: Any = None
        # Local sentence-transformers path: construct directly, skip
        # llm's registry (see _load_sentence_transformer docstring).
        if model_name.startswith(_ST_PREFIX):
            model = _load_sentence_transformer(model_name, verbose)
        # Remote OpenAI-compatible path: no torch, no llm registry --
        # just an HTTP endpoint configured from the environment.
        elif model_name.startswith(_OPENAI_EMBED_PREFIX):
            model = _load_openai_compat(model_name, verbose)
        else:
            try:
                import llm  # pylint: disable=import-outside-toplevel,import-error
            except ImportError:
                log(
                    "`llm` package is missing — this should ship with "
                    "ietf-llm. Try reinstalling: pipx install --force ietf-llm",
                    verbose,
                    level=LogLevel.ERROR,
                )
                return None
            try:
                model = llm.get_embedding_model(  # type: ignore[no-untyped-call]
                    model_name
                )
            except Exception as err:  # pylint: disable=broad-except
                # `llm.get_embedding_model` and the various provider
                # plugins don't share a typed exception hierarchy.
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
