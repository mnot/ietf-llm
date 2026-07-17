"""A cached on-device model must load without touching HuggingFace.

Left to itself the hub client revalidates every file's ETag against
huggingface.co on each load, even when the model is fully cached (~4.7s of
round-trips for bge-small, and worse when the machine is offline rather than
merely slow). The read path is meant to work offline, so `_load_sentence_
transformer` tries a cache-only load first and only falls back to the network
on a genuine miss. These tests pin both halves of that.
"""

from __future__ import annotations

import sys
import types
from typing import Any, List

import pytest

from ietf_llm.embeddings import models
from ietf_llm.log import Verbosity

_MODEL = "sentence-transformers/BAAI/bge-small-en-v1.5"
_BARE = "BAAI/bge-small-en-v1.5"


class _StubST:
    """Stands in for the plugin's SentenceTransformerModel wrapper."""

    def __init__(self, model_id: str, name: str, trust_remote_code: bool):
        self.model_id, self.name, self._model = model_id, name, None


class _StubPlugin:
    """Stub of llm_sentence_transformers, keeping the tests torch-free.

    Starts with an empty model list, so the registration write-through runs
    for real rather than being skipped by a pre-seeded entry.
    """

    def __init__(self) -> None:
        self.registered: List[dict] = []

    def read_models(self) -> List[dict]:
        return list(self.registered)

    def write_models(self, models_: List[dict]) -> None:
        self.registered = list(models_)


@pytest.fixture(name="plugin")
def _plugin(monkeypatch):
    stub = _StubPlugin()
    mod = types.ModuleType("llm_sentence_transformers")
    mod.SentenceTransformerModel = _StubST  # type: ignore[attr-defined]
    mod.read_models = stub.read_models  # type: ignore[attr-defined]
    mod.write_models = stub.write_models  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "llm_sentence_transformers", mod)
    monkeypatch.setattr(models, "_embed_device", lambda *_a, **_k: "cpu")
    return stub


def _record(monkeypatch, *, miss: bool) -> List[bool]:
    """Patch the constructor; return the list of local_only values seen.

    `miss=True` makes the cache-only attempt raise, as the hub does when the
    model is absent from ~/.cache/huggingface.
    """
    calls: List[bool] = []

    def fake(_bare: str, _device: str, *, local_only: bool) -> Any:
        calls.append(local_only)
        if local_only and miss:
            # What the hub actually raises on a miss: LocalEntryNotFoundError,
            # a FileNotFoundError and so an OSError.
            raise OSError("not in the local cache")
        return object()

    monkeypatch.setattr(models, "_construct_sentence_transformer", fake)
    return calls


def test_local_files_only_reaches_sentence_transformers(monkeypatch):
    """The kwarg this whole change rests on is really passed upstream.

    Every other test here patches `_construct_sentence_transformer` out, so
    without this one a misspelt or upstream-renamed kwarg would keep the suite
    green while offline search silently regressed to revalidating against
    huggingface.co on every load. `local-embeddings` is an unpinned extra, so
    that drift is live rather than theoretical.
    """
    seen: List[dict] = []

    class _Recorder:
        def __init__(self, name: str, **kwargs: Any) -> None:
            seen.append({"name": name, **kwargs})

    mod = types.ModuleType("sentence_transformers")
    mod.SentenceTransformer = _Recorder  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "sentence_transformers", mod)

    models._construct_sentence_transformer(_BARE, "cpu", local_only=True)
    assert seen == [
        {
            "name": _BARE,
            "device": "cpu",
            "trust_remote_code": False,
            "local_files_only": True,
        }
    ]
    # ...and the networked fallback really does re-enable the hub.
    models._construct_sentence_transformer(_BARE, "cpu", local_only=False)
    assert seen[1]["local_files_only"] is False


def test_cached_model_loads_without_network(plugin, monkeypatch):
    calls = _record(monkeypatch, miss=False)
    model = models._load_sentence_transformer(_MODEL, Verbosity.QUIET)
    assert model is not None and model._model is not None
    # Exactly one attempt, cache-only: the hub was never given the chance
    # to revalidate against huggingface.co.
    assert calls == [True]
    # The registration write-through still happened (empty list -> registered).
    assert plugin.registered == [{"name": _BARE, "trust_remote_code": False}]


def test_load_failure_after_cache_hit_is_not_retried_as_a_miss(plugin, monkeypatch):
    """A non-OSError failure means the weights loaded and something *else*
    broke (e.g. torch rejecting an IETF_LLM_EMBED_DEVICE override). It must
    surface as-is, not be misread as a cache miss and reloaded over the
    network."""
    calls: List[bool] = []

    def fake(_bare: str, _device: str, *, local_only: bool) -> Any:
        calls.append(local_only)
        raise AssertionError("Torch not compiled with CUDA enabled")

    monkeypatch.setattr(models, "_construct_sentence_transformer", fake)
    assert models._load_sentence_transformer(_MODEL, Verbosity.QUIET) is None
    # Tried once, cache-only -- no pointless networked reload.
    assert calls == [True]


def test_uncached_model_falls_back_to_download(plugin, monkeypatch, capsys):
    calls = _record(monkeypatch, miss=True)
    model = models._load_sentence_transformer(_MODEL, Verbosity.QUIET)
    assert model is not None and model._model is not None
    # Cache-only miss, then a networked load -- a first run must still work.
    assert calls == [True, False]
    # And the user is told why they are about to wait, even when quiet.
    assert "Fetching" in capsys.readouterr().err


def test_download_failure_is_reported(plugin, monkeypatch):
    """A miss whose network load also fails reports the upstream error,
    rather than the swallowed cache-only one."""

    def fake(_bare: str, _device: str, *, local_only: bool) -> Any:
        raise OSError("cache miss" if local_only else "upstream is down")

    monkeypatch.setattr(models, "_construct_sentence_transformer", fake)
    assert models._load_sentence_transformer(_MODEL, Verbosity.QUIET) is None
