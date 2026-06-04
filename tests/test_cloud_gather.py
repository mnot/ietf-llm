"""Feature 6 capstone: an in-session gather publishes to the cloud backend, and
the published version is then served by the read path — gather to read, end to
end, through the sqlite + file:// stand-in."""

from __future__ import annotations

from pathlib import Path

import pytest

from ietf_llm import __main__ as main_mod
from ietf_llm import gather_runner
from ietf_llm.corpus_store import get_corpus_store
from ietf_llm.utils import get_cache_dir

_STORE_ENV = (
    "IETF_LLM_STORE_BACKEND",
    "IETF_LLM_CONTROL_DB",
    "IETF_LLM_BLOB_DIR",
    "IETF_LLM_SCRATCH_DIR",
)


@pytest.fixture(autouse=True)
def _clear_store_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in _STORE_ENV:
        monkeypatch.delenv(var, raising=False)


def test_cloud_gather_publishes_and_serves(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = isolated_home / "store"
    monkeypatch.setenv("IETF_LLM_STORE_BACKEND", "cloud")
    monkeypatch.setenv("IETF_LLM_CONTROL_DB", str(base / "control.db"))
    monkeypatch.setenv("IETF_LLM_BLOB_DIR", str(base / "bucket"))
    monkeypatch.setenv("IETF_LLM_SCRATCH_DIR", str(base / "scratch"))

    # Stub the pipeline: write a files/ tree into the local cache (the gather's
    # natural output, which on a cloud node is ephemeral scratch), report
    # success. _execute then publishes that tree to the cloud backend.
    def _fake_run_gather(argv: list[str], _verbosity: object, progress: object = None) -> bool:
        corpus = argv[0]
        digests = Path(get_cache_dir()) / corpus / "files" / "digests"
        digests.mkdir(parents=True, exist_ok=True)
        (digests / "index.md").write_text("# Overview\nbody\n")
        return True

    monkeypatch.setattr(main_mod, "run_gather", _fake_run_gather)

    gather_runner._execute(gather_runner.GatherSpec(corpus="tls"))

    # A fresh store (same config) resolves the published version and
    # materialises its files for the read path.
    store = get_corpus_store()
    assert store.resolve_current("tls") is not None
    cache = store.local_cache_dir("tls")
    assert cache is not None
    assert (Path(cache) / "digests" / "index.md").read_text() == "# Overview\nbody\n"
