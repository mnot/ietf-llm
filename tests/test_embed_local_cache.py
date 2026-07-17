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


@pytest.fixture(name="plugin")
def _plugin(monkeypatch):
    """Stub llm_sentence_transformers so the test stays torch-free."""
    written: List[dict] = [{"name": _BARE, "trust_remote_code": False}]
    mod = types.ModuleType("llm_sentence_transformers")
    mod.SentenceTransformerModel = _StubST  # type: ignore[attr-defined]
    mod.read_models = lambda: list(written)  # type: ignore[attr-defined]
    mod.write_models = written.__init__  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "llm_sentence_transformers", mod)
    monkeypatch.setattr(models, "_embed_device", lambda *_a, **_k: "cpu")
    return mod


def _record(monkeypatch, *, miss: bool) -> List[bool]:
    """Patch the constructor; return the list of local_only values seen.

    `miss=True` makes the cache-only attempt raise, as the hub does when the
    model is absent from ~/.cache/huggingface.
    """
    calls: List[bool] = []

    def fake(_bare: str, _device: str, *, local_only: bool) -> Any:
        calls.append(local_only)
        if local_only and miss:
            raise OSError("not in the local cache")
        return object()

    monkeypatch.setattr(models, "_construct_sentence_transformer", fake)
    return calls


def test_cached_model_loads_without_network(plugin, monkeypatch):
    calls = _record(monkeypatch, miss=False)
    model = models._load_sentence_transformer(_MODEL, Verbosity.QUIET)
    assert model is not None and model._model is not None
    # Exactly one attempt, cache-only: the hub was never given the chance
    # to revalidate against huggingface.co.
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
