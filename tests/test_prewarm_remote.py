"""R10: the prewarm must be lightweight for the remote backend -- no
weight load and, crucially, no network round-trip. _prewarm_one forces a
warmup embed for on-device models (to load weights) but skips it for a
remote OpenAI-compatible backend, which has nothing to warm.
"""

from __future__ import annotations
from ietf_llm import mcp

from typing import Iterable, List

from ietf_llm import embeddings
from ietf_llm.embeddings.models import is_remote_embed_model


def test_is_remote_embed_model():
    assert is_remote_embed_model("openai-embed/@cf/baai/bge-small-en-v1.5")
    assert not is_remote_embed_model("sentence-transformers/BAAI/bge-small-en-v1.5")
    assert not is_remote_embed_model("text-embedding-3-small")


class _RecordingModel:
    def __init__(self):
        self.embed_calls = 0

    def embed(self, _text: str) -> Iterable[float]:
        self.embed_calls += 1
        return [1.0, 0.0]

    def embed_multi(self, texts: List[str]) -> Iterable[List[float]]:
        return [list(self.embed(t)) for t in texts]


def _seed(model_name: str) -> _RecordingModel:
    m = _RecordingModel()
    embeddings._MODEL_CACHE[model_name] = m  # noqa
    return m


def test_prewarm_skips_warmup_embed_for_remote():
    m = _seed("openai-embed/probe-model")
    mcp.server._prewarm_one("openai-embed/probe-model")
    # Constructed the client, but did NOT embed (no network round-trip).
    assert m.embed_calls == 0


def test_prewarm_warms_on_device_model():
    m = _seed("sentence-transformers/x")
    mcp.server._prewarm_one("sentence-transformers/x")
    # On-device: the warmup embed runs to force the weight load.
    assert m.embed_calls == 1
