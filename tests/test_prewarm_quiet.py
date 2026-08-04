"""A failed background prewarm must not shout.

The prewarm is best-effort: it runs in a daemon thread and falls back to a
lazy load on the first search. But `log()` makes ERROR bypass
`Verbosity.QUIET`, so a transient first-run model-load miss printed an
[ERROR] line to stderr and into the client's logs for something that then
worked fine on retry. Reported by a user whose server was in fact healthy.

The suite is torch-free, so `llm_sentence_transformers` is stubbed — without
it these would silently exercise the missing-extra branch instead of the
load-failure branch they are about.
"""

from __future__ import annotations

import sys
import types
from typing import Any, List

import pytest

from ietf_llm.embeddings import models
from ietf_llm.log import LogLevel, Verbosity
from ietf_llm.mcp import server as mcp_server

_BARE = models.DEFAULT_EMBED_MODEL.split("/", 1)[1]


def _boom(*_a: Any, **_k: Any) -> Any:
    raise FileNotFoundError(2, "No such file or directory")


@pytest.fixture(name="failing_load")
def _failing_load(monkeypatch: pytest.MonkeyPatch) -> List[Any]:
    """Reach the load-failure path with the on-device stack absent, and
    capture every `log()` call as (message, level)."""
    plugin = types.ModuleType("llm_sentence_transformers")
    plugin.SentenceTransformerModel = (  # type: ignore[attr-defined]
        lambda *a, **k: types.SimpleNamespace(_model=None)
    )
    plugin.read_models = lambda: [{"name": _BARE}]  # type: ignore[attr-defined]
    plugin.write_models = lambda _models: None  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "llm_sentence_transformers", plugin)
    monkeypatch.setattr(models, "_MODEL_CACHE", {})
    monkeypatch.setattr(models, "_construct_sentence_transformer", _boom)

    calls: List[Any] = []

    def fake_log(
        message: Any, _verbose: Any = None, level: Any = LogLevel.PROGRESS, **_kw: Any
    ) -> None:
        calls.append((str(message), level))

    monkeypatch.setattr(models, "log", fake_log)
    return calls


def test_prewarm_load_failure_does_not_log_at_error(failing_load: List[Any]) -> None:
    models._get_embed_model(
        models.DEFAULT_EMBED_MODEL, Verbosity.QUIET, on_error_level=LogLevel.WARN
    )
    levels = [level for _msg, level in failing_load]
    assert levels, "the failure should still be logged, just not at ERROR"
    assert LogLevel.ERROR not in levels
    # Guard the trap this test already fell into once: without the plugin
    # stub it would pass by way of the missing-extra branch instead.
    assert any("Could not load sentence-transformers" in m for m, _l in failing_load)


def test_search_path_load_failure_still_logs_at_error(failing_load: List[Any]) -> None:
    # The default must stay loud: on the search path a load failure blocks
    # the call the user is waiting on.
    models._get_embed_model(models.DEFAULT_EMBED_MODEL, Verbosity.QUIET)
    assert LogLevel.ERROR in [level for _msg, level in failing_load]


def test_debug_logging_adds_a_traceback(
    failing_load: List[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("IETF_LLM_DEBUG_LOG", "1")
    models._load_sentence_transformer(models.DEFAULT_EMBED_MODEL, Verbosity.QUIET)
    assert any("Traceback (most recent call last)" in msg for msg, _l in failing_load)


def test_traceback_is_absent_without_the_debug_flag(
    failing_load: List[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("IETF_LLM_DEBUG_LOG", raising=False)
    models._load_sentence_transformer(models.DEFAULT_EMBED_MODEL, Verbosity.QUIET)
    assert failing_load
    assert not any(
        "Traceback (most recent call last)" in msg for msg, _l in failing_load
    )


def test_prewarm_passes_the_downgraded_level(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = {}

    def fake_get(
        _model_name: str, _verbose: Any, *, on_error_level: Any = LogLevel.ERROR
    ) -> Any:
        captured["level"] = on_error_level
        return None

    monkeypatch.setattr(mcp_server, "_get_embed_model", fake_get)
    mcp_server._prewarm_one(models.DEFAULT_EMBED_MODEL)
    assert captured["level"] is LogLevel.WARN
