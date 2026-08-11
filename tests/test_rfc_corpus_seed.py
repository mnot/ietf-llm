"""Installing and refreshing the RFC full-text corpus (#230).

The ordinary seed path is keyed to the corpus being gathered, which never
reaches this one — there is no `ietf-llm rfcs` to run. So it rides the
once-per-invocation trigger instead, and its refresh keys on the upstream
build rather than on a gather time it does not have.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Any, List, Optional

import pytest

from ietf_llm.gather.sources import rfc_corpus
from ietf_llm.log import Verbosity
from ietf_llm.paths import cached_wg_names, get_index_dir

MARGIN = 60.0


def _install_local(build: Optional[str]) -> None:
    """A local corpus at `build` — index only, as the bundle ships it."""
    d = os.path.join(get_index_dir(), rfc_corpus.RFC_CORPUS)
    os.makedirs(d, exist_ok=True)
    conn = sqlite3.connect(os.path.join(d, "embeddings.db"))
    try:
        conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        if build is not None:
            conn.execute("INSERT INTO meta VALUES(?, ?)", ("rfc_index_build", build))
        conn.commit()
    finally:
        conn.close()


# --- the refresh decision ---------------------------------------------------


def test_no_local_copy_installs() -> None:
    assert rfc_corpus._should_install(None, "20260811T003915Z", MARGIN)


def test_same_build_does_nothing() -> None:
    b = "20260811T003915Z"
    assert not rfc_corpus._should_install(b, b, MARGIN)


def test_one_month_newer_is_not_worth_the_download() -> None:
    """~270 MiB for a month of new RFCs is not a trade worth making; the
    provenance line discloses that the corpus is a little behind."""
    assert not rfc_corpus._should_install(
        "20260711T000000Z", "20260811T000000Z", MARGIN
    )


def test_three_months_newer_is() -> None:
    assert rfc_corpus._should_install("20260511T000000Z", "20260811T000000Z", MARGIN)


def test_a_store_behind_us_is_ignored() -> None:
    """Seeding never goes backwards."""
    assert not rfc_corpus._should_install(
        "20260811T000000Z", "20260511T000000Z", MARGIN
    )


def test_an_unreadable_build_id_prefers_the_fresher_artifact() -> None:
    """We produce these ids; a shape we cannot parse means something changed."""
    assert rfc_corpus._should_install("nonsense", "20260811T000000Z", MARGIN)
    assert rfc_corpus._should_install("20260811T000000Z", "nonsense", MARGIN)


# --- local state ------------------------------------------------------------


def test_local_build_reads_the_installed_corpus(isolated_home: Path) -> None:
    assert rfc_corpus.local_build() is None
    _install_local("20260811T003915Z")
    assert rfc_corpus.local_build() == "20260811T003915Z"


def test_local_build_survives_a_corpus_without_the_key(isolated_home: Path) -> None:
    _install_local(None)
    assert rfc_corpus.local_build() is None


# --- the trigger ------------------------------------------------------------


def test_disabled_seeding_is_a_no_op(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--no-seed` covers this too — it is the same switch."""
    monkeypatch.setenv("IETF_LLM_SEED_ENABLED", "off")
    called: List[str] = []
    monkeypatch.setattr(
        rfc_corpus.service_config, "seed_url", lambda: called.append("url") or None
    )
    rfc_corpus.ensure_rfc_corpus(Verbosity.QUIET)
    assert called == []


def test_no_seed_url_is_a_no_op(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("IETF_LLM_SEED_ENABLED", "on")
    monkeypatch.setattr(rfc_corpus.service_config, "seed_url", lambda: None)
    rfc_corpus.ensure_rfc_corpus(Verbosity.QUIET)  # must not raise


# --- enumeration ------------------------------------------------------------


def test_the_rfc_corpus_is_not_listed_as_a_gathered_corpus(
    isolated_home: Path,
) -> None:
    """It has no `files/` tree — it is an index and nothing else — so it stays
    out of `list_corpora` and out of `search_corpora`'s fan-out, where 457k
    chunks would swamp per-corpus ranking. Asserted rather than assumed,
    because giving it a `files/` tree later would silently reverse it.
    """
    _install_local("20260811T003915Z")
    assert rfc_corpus.RFC_CORPUS not in cached_wg_names()


def test_the_store_index_is_not_fetched_on_every_invocation(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """This runs on *every* `ietf-llm` run, and the store's index moves about
    six times a year, so an unthrottled fetch would be a request per run for a
    document that almost never changes."""
    monkeypatch.setenv("IETF_LLM_SEED_ENABLED", "on")
    monkeypatch.setattr(rfc_corpus.service_config, "seed_url", lambda: "https://x/")
    fetched: List[str] = []
    import ietf_llm.seed.fetch as sf

    monkeypatch.setattr(sf, "load_index", lambda url, **kw: fetched.append(url) or None)

    rfc_corpus.ensure_rfc_corpus(Verbosity.QUIET)
    rfc_corpus.ensure_rfc_corpus(Verbosity.QUIET)
    assert len(fetched) == 1, "the second call should have been throttled"

    # A zero interval is the escape hatch the tests and a forced check use.
    rfc_corpus.ensure_rfc_corpus(Verbosity.QUIET, interval=0.0)
    assert len(fetched) == 2
