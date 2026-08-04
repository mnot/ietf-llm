"""The background prewarm must be silent, and diagnosable on demand.

The prewarm is unsolicited and best-effort: it runs in a daemon thread and
the first search does a lazy load if it fails. It was not silent. `log()`
makes ERROR bypass `Verbosity.QUIET`, so a transient first-run miss printed
[ERROR] to stderr and into the client's logs for something that then worked
fine on retry -- reported by a user whose server was in fact healthy.

The durable guard is `test_prewarm_emits_nothing_on_stderr`: it drives the
real `_prewarm_one` and asserts on the stream, not on `log()` calls. An
earlier fix satisfied a log-level assertion while still printing a
HuggingFace fetch notice from the same path.

The suite is torch-free, so `llm_sentence_transformers` is stubbed -- without
it these silently exercise the missing-extra branch instead of the
load-failure branch they are about.
"""

from __future__ import annotations

import sys
import types
from typing import Any, List, Tuple

import pytest

from ietf_llm.embeddings import models
from ietf_llm.log import LogLevel, Verbosity
from ietf_llm.mcp import server as mcp_server

_BARE = models.DEFAULT_EMBED_MODEL.split("/", 1)[1]


def _boom(*_a: Any, **_k: Any) -> Any:
    # An OSError, so the loader treats it as a cache miss, emits the fetch
    # notice, retries over the network, and lands in the outer handler --
    # exactly the sequence the reporter hit.
    raise FileNotFoundError(2, "No such file or directory")


@pytest.fixture(name="stub_plugin")
def _stub_plugin(monkeypatch: pytest.MonkeyPatch) -> None:
    plugin = types.ModuleType("llm_sentence_transformers")
    plugin.SentenceTransformerModel = (  # type: ignore[attr-defined]
        lambda *a, **k: types.SimpleNamespace(_model=None)
    )
    plugin.read_models = lambda: [{"name": _BARE}]  # type: ignore[attr-defined]
    plugin.write_models = lambda _models: None  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "llm_sentence_transformers", plugin)
    monkeypatch.setattr(models, "_MODEL_CACHE", {})
    monkeypatch.setattr(models, "_construct_sentence_transformer", _boom)


@pytest.fixture(name="logged")
def _logged(monkeypatch: pytest.MonkeyPatch) -> List[Tuple[str, Any]]:
    calls: List[Tuple[str, Any]] = []

    def fake_log(
        message: Any, _verbose: Any = None, level: Any = LogLevel.PROGRESS, **_kw: Any
    ) -> None:
        calls.append((str(message), level))

    monkeypatch.setattr(models, "log", fake_log)
    return calls


@pytest.mark.usefixtures("stub_plugin")
def test_prewarm_emits_nothing_on_stderr(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("IETF_LLM_DEBUG_LOG", raising=False)
    mcp_server._prewarm_one(models.DEFAULT_EMBED_MODEL)
    captured = capsys.readouterr()
    assert captured.err == "", f"prewarm wrote to stderr: {captured.err!r}"
    assert captured.out == ""


@pytest.mark.usefixtures("stub_plugin")
def test_foreground_load_still_announces_the_download(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # The notice is wanted when a user is waiting on the load: it is a
    # multi-minute, 100-500 MB operation.
    models._load_sentence_transformer(models.DEFAULT_EMBED_MODEL, Verbosity.QUIET)
    assert "Fetching" in capsys.readouterr().err


@pytest.mark.usefixtures("stub_plugin")
def test_background_load_failure_does_not_log_at_error(
    logged: List[Tuple[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("IETF_LLM_DEBUG_LOG", raising=False)
    models._get_embed_model(
        models.DEFAULT_EMBED_MODEL, Verbosity.QUIET, background=True
    )
    levels = [level for _msg, level in logged]
    assert levels, "the failure should still be logged, just not at ERROR"
    assert LogLevel.ERROR not in levels
    # Guard the trap this test already fell into once: without the plugin
    # stub it would pass by way of the missing-extra branch instead.
    assert any("Could not load sentence-transformers" in m for m, _l in logged)


@pytest.mark.usefixtures("stub_plugin")
def test_search_path_load_failure_still_logs_at_error(
    logged: List[Tuple[str, Any]],
) -> None:
    # On the search path a load failure blocks a call someone is waiting on.
    models._get_embed_model(models.DEFAULT_EMBED_MODEL, Verbosity.QUIET)
    assert LogLevel.ERROR in [level for _msg, level in logged]


@pytest.mark.usefixtures("stub_plugin")
def test_debug_logging_reaches_the_background_path(
    logged: List[Tuple[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    # The whole point of the traceback is #205, which was reported from the
    # prewarm. WARN is invisible there (QUIET), so debug logging must lift
    # the failure back to ERROR or the diagnostic is unreachable.
    monkeypatch.setenv("IETF_LLM_DEBUG_LOG", "1")
    models._get_embed_model(
        models.DEFAULT_EMBED_MODEL, Verbosity.QUIET, background=True
    )
    assert any(
        "Traceback (most recent call last)" in msg and level is LogLevel.ERROR
        for msg, level in logged
    )


@pytest.mark.usefixtures("stub_plugin")
def test_traceback_is_absent_without_the_debug_flag(
    logged: List[Tuple[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("IETF_LLM_DEBUG_LOG", raising=False)
    models._load_sentence_transformer(models.DEFAULT_EMBED_MODEL, Verbosity.QUIET)
    assert logged
    assert not any("Traceback (most recent call last)" in msg for msg, _l in logged)


def test_missing_extra_stays_loud_even_in_background(
    logged: List[Tuple[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    # A broken install never resolves on its own, so quieting it would just
    # defer the report to the first search. No plugin stub here.
    monkeypatch.setitem(sys.modules, "llm_sentence_transformers", None)
    monkeypatch.setattr(models, "_MODEL_CACHE", {})
    models._get_embed_model(
        models.DEFAULT_EMBED_MODEL, Verbosity.QUIET, background=True
    )
    assert LogLevel.ERROR in [level for _msg, level in logged]


def test_unknown_device_override_is_a_warning_not_an_error(
    logged: List[Tuple[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    # It fired at ERROR from the prewarm even when the load then succeeded.
    monkeypatch.setenv("IETF_LLM_EMBED_DEVICE", "gpu")
    assert models._embed_device(Verbosity.QUIET) in ("cpu", "cuda")
    assert [level for _msg, level in logged] == [LogLevel.WARN]


def test_quiet_embedding_stack_silences_sentence_transformers_logger(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # FastMCP's configure_logging() puts the root logger at INFO, which turns
    # on both the INFO record and the `Batches` bar in sentence-transformers.
    import logging

    logger = logging.getLogger("sentence_transformers")
    monkeypatch.setattr(logger, "level", logging.INFO)
    mcp_server._quiet_embedding_stack_output()
    assert logger.level == logging.WARNING


def test_quiet_embedding_stack_tolerates_a_torch_free_install(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # huggingface_hub ships with the local-embeddings extra; the serve path
    # must not require it.
    monkeypatch.setitem(sys.modules, "huggingface_hub.utils", None)
    mcp_server._quiet_embedding_stack_output()  # must not raise
