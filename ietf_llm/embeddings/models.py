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
import time
from typing import Any, Iterable, Sequence

import requests
from requests.adapters import HTTPAdapter

from .. import serve_metrics
from ..log import LogLevel, Verbosity, log
from . import oai_compat

#: Default embedding model. Local, no API key, via sentence-transformers.
#: ~33M params, 384-dim. Good quality for technical English with a small DB
#: footprint and ~2-minute index time per WG. Runs on CPU by default; MPS is
#: deliberately avoided (see `_embed_device`), overridable via
#: IETF_LLM_EMBED_DEVICE.
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


# requests' default per-host connection pool. We never size the remote
# embedding session below this, so the single-input query path keeps the
# stock headroom even when a gather asks for less concurrency.
_DEFAULT_POOL_MAXSIZE = 10


def embed_concurrency() -> int:
    """How many files a gather embeds in parallel on the remote backend.

    A remote embed is a network round-trip, and a gather sends one per file;
    for a mail-heavy corpus that serial wait is the bulk of the wall-clock,
    so the index build overlaps the round-trips through a bounded pool. This
    same number sizes the embedding session's connection pool (see
    `_OpenAICompatEmbeddingModel`), so the keep-alive connections never run
    short of the fan-out. Only the remote backend uses it (the on-device
    model is GPU-bound and stays serial). Override with
    `IETF_LLM_EMBED_CONCURRENCY`; floored at 1 (serial)."""
    try:
        return max(1, int(os.environ.get("IETF_LLM_EMBED_CONCURRENCY", "8")))
    except ValueError:
        return 8


# Process-level cache of loaded embedding models, keyed by full model id.
_MODEL_CACHE: dict[str, Any] = {}
# Serialise concurrent loads of the same model. With background
# pre-warm now running in a daemon thread, a search call landing
# during startup would otherwise trigger a second redundant load.
_MODEL_LOAD_LOCK = threading.Lock()


def _cuda_available() -> bool:
    """True if a CUDA device is usable. Isolated so the device default can be
    tested without importing torch (CI stays torch-free)."""
    try:
        # pylint: disable=import-outside-toplevel,import-error
        import torch  # type: ignore[import-untyped,import-not-found,unused-ignore]

        return bool(torch.cuda.is_available())
    except ImportError:
        return False


def _embed_device(verbose: Verbosity = Verbosity.QUIET) -> str:
    """Torch device for the local sentence-transformers backend.

    `IETF_LLM_EMBED_DEVICE` overrides; otherwise the default deliberately
    **avoids MPS**. PyTorch's MPS caching allocator fragments badly on our
    variable-length inputs: indexing one modest corpus (bge-small, ~11k chunks
    spanning ~50–50000 chars) drove the process footprint to ~11 GB on Apple
    Silicon — enough to push a co-resident reader into memory pressure — while
    CPU held ~1–2 GB for the same work, at a ~15–30% time cost and
    numerically-equivalent vectors (max abs diff ~3e-7, min cosine 0.99999982,
    so no re-embed). CUDA has no such pathology, so it is kept. See
    docs/architecture.md ("Embedding device") and the PyTorch MPS memory
    issues; revisit the default (drop back to MPS) if that is fixed.

    A recognised override (`cpu` / `mps` / `cuda`, optionally `:N`) is used
    verbatim; an unknown value (a typo like `gpu`) is warned about and dropped
    to the default rather than passed to torch to fail with a raw error."""
    override = os.environ.get("IETF_LLM_EMBED_DEVICE", "").strip().lower()
    if override:
        if override.split(":", 1)[0] in ("cpu", "mps", "cuda"):
            return override
        log(
            f"Ignoring unknown IETF_LLM_EMBED_DEVICE={override!r} "
            "(expected cpu, mps, or cuda); using the default.",
            verbose,
            level=LogLevel.ERROR,
        )
    return "cuda" if _cuda_available() else "cpu"


def _construct_sentence_transformer(bare: str, device: str, *, local_only: bool) -> Any:
    """Build the underlying SentenceTransformer. Isolated for testing."""
    # pylint: disable=import-outside-toplevel,import-error,line-too-long
    from sentence_transformers import (  # type: ignore[import-untyped,import-not-found,unused-ignore]
        SentenceTransformer,
    )

    return SentenceTransformer(
        bare,
        device=device,
        trust_remote_code=False,
        local_files_only=local_only,
    )


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
        if not any(m.get("name") == bare for m in models):
            models.append({"name": bare, "trust_remote_code": False})
            write_models(models)
        # The plugin lazily builds its underlying SentenceTransformer with no
        # device argument (auto-selecting MPS on Apple Silicon), which we must
        # not do — see `_embed_device`. Construct the wrapper (cheap) and
        # pre-seed its `_model` on the chosen device.
        model = SentenceTransformerModel(f"{_ST_PREFIX}{bare}", bare, False)
        device = _embed_device(verbose)
        # Load from the HuggingFace cache alone first. Left to itself the hub
        # client revalidates every file's ETag against huggingface.co on each
        # load, even when the model is fully cached: ~4.7s of round-trips per
        # process for bge-small, and worse on a machine that is offline rather
        # than merely slow (it falls back to the cache only once the requests
        # time out). Reading a search index is meant to work offline, so a hit
        # here must not touch the network. A miss raises, and only then do we
        # fall through and let the hub download. Weights are revision-pinned
        # once cached, which we want anyway: a silent upstream model update
        # would not match the vectors already in the index.
        st_model: Any = None
        try:
            st_model = _construct_sentence_transformer(bare, device, local_only=True)
        except Exception:  # pylint: disable=broad-except
            # Nothing usable in the cache (or it is partial). Fall back to a
            # networked load below, which reports the real error if it fails.
            pass
        if st_model is None:
            # Emit on stderr so it clusters with HuggingFace's tqdm bars
            # and survives stdout redirection. Always shown (even with
            # --quiet) because a multi-minute network operation deserves
            # a heads-up.
            print(
                f"\nFetching '{bare}' from HuggingFace: it is not in the local "
                f"cache (typically 100-500 MB; one-time, then cached in "
                f"~/.cache/huggingface/ and loaded offline thereafter).\n",
                file=sys.stderr,
                flush=True,
            )
            st_model = _construct_sentence_transformer(bare, device, local_only=False)
        model._model = st_model  # pylint: disable=protected-access
        log(f"Loaded {bare} on device={device}.", verbose, level=LogLevel.PROGRESS)
        return model
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
        pool_maxsize: int = _DEFAULT_POOL_MAXSIZE,
    ) -> None:
        self._model_id = model_id
        self._url = base_url.rstrip("/") + "/embeddings"
        self._headers = {"Content-Type": "application/json", **headers}
        self._batch_size = max(1, batch_size)
        self._timeout = timeout
        self._max_retries = max(0, max_retries)
        # One keep-alive session for the model's lifetime (it is process-cached
        # in _MODEL_CACHE): a bulk index fires many batches back-to-back at one
        # host, so reusing the connection drops a TCP + TLS handshake per batch.
        # A gather embeds up to `embed_concurrency()` files at once, so size the
        # per-host pool to match: with the stock pool (10) a higher concurrency
        # would exhaust it and urllib3 would discard the surplus connections
        # after each use, quietly undoing the keep-alive saving on most batches.
        self._session = requests.Session()
        adapter = HTTPAdapter(pool_maxsize=max(1, pool_maxsize))
        self._session.mount("https://", adapter)
        self._session.mount("http://", adapter)

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
        # Embed-backend RED for the /metrics scrape (issue #40): this is
        # the paid, metered upstream the read path depends on. Time the
        # whole retrying call and record success/failure either way; a
        # raised error still counts (in the `finally`) before re-raising.
        start = time.monotonic()
        errored = True
        try:
            body = oai_compat.post_json_with_retry(
                self._url,
                payload,
                self._headers,
                timeout=self._timeout,
                max_retries=self._max_retries,
                auth_hint="IETF_LLM_EMBED_TOKEN / IETF_LLM_EMBED_HEADERS",
                session=self._session,
            )
            errored = False
        finally:
            serve_metrics.record_embed(time.monotonic() - start, error=errored)
        # OpenAI returns one object per input carrying an explicit `index`.
        # Place each at its index so the output lines up with the input
        # positionally, and fail loudly on a count / index mismatch: silently
        # accepting a short or misindexed response would misalign every
        # chunk<->vector pair the caller zips together.
        data = body.get("data")
        if not isinstance(data, list):
            raise ValueError(
                f"embed backend returned no 'data' list: {str(body)[:200]}"
            )
        if len(data) != len(batch):
            raise ValueError(
                f"embed backend returned {len(data)} vectors for "
                f"{len(batch)} inputs"
            )
        result: list[list[float]] = [[] for _ in batch]
        filled = [False] * len(batch)
        for row in data:
            idx = int(row.get("index", 0))
            if not 0 <= idx < len(batch) or filled[idx]:
                raise ValueError(
                    f"embed backend returned a bad or duplicate index {idx} "
                    f"for a batch of {len(batch)}"
                )
            result[idx] = [float(x) for x in row["embedding"]]
            filled[idx] = True
        return result


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
        # Never below requests' stock pool, so the query path keeps its
        # headroom; raised to the fan-out when a gather embeds concurrently.
        pool_maxsize=max(_DEFAULT_POOL_MAXSIZE, embed_concurrency()),
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
