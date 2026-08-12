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


def test_a_bumped_assembly_is_taken_even_though_upstream_is_unchanged() -> None:
    """A fix to our own assembly of the same upstream index. Without this the
    republished bundle carries the version the client already has and reaches
    nobody — which is what would have happened to the fix that stopped chunk
    boundaries splitting figure rows."""
    b = "20260811T003915Z"
    assert rfc_corpus._should_install(f"{b}+1", f"{b}+2", MARGIN)
    assert not rfc_corpus._should_install(f"{b}+2", f"{b}+2", MARGIN)


def test_the_margin_still_applies_across_upstream_builds() -> None:
    """The assembly suffix must not disturb the staleness arithmetic."""
    assert not rfc_corpus._should_install(
        "20260711T000000Z+2", "20260811T000000Z+2", MARGIN
    )
    assert rfc_corpus._should_install(
        "20260511T000000Z+2", "20260811T000000Z+2", MARGIN
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
    reason = rfc_corpus.ensure_rfc_corpus(Verbosity.QUIET)
    assert called == []
    assert reason and "--seed" in reason


def test_no_seed_url_is_a_no_op(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("IETF_LLM_SEED_ENABLED", "on")
    monkeypatch.setattr(rfc_corpus.service_config, "seed_url", lambda: None)
    reason = rfc_corpus.ensure_rfc_corpus(Verbosity.QUIET)  # must not raise
    assert reason and "IETF_LLM_SEED_URL" in reason


# --- why it declined --------------------------------------------------------
#
# Every path out of `ensure_rfc_corpus` is silent by design — it is
# housekeeping after somebody else's gather. `--init` is the one caller that
# has to explain itself, so each way of declining has to name itself.


def _stub_store(monkeypatch: pytest.MonkeyPatch, index: Any) -> None:
    """Point the corpus at a store whose index is `index` — an `fmt.Index`, or
    an exception instance to raise instead."""
    import ietf_llm.seed.fetch as sf

    monkeypatch.setenv("IETF_LLM_SEED_ENABLED", "on")
    monkeypatch.setattr(rfc_corpus.service_config, "seed_url", lambda: "https://x/")

    def _read(_url: str, **_kw: Any) -> Any:
        if isinstance(index, Exception):
            raise index
        return index

    monkeypatch.setattr(sf, "read_index", _read)


def _index(*names: str) -> Any:
    """A store index carrying an entry per name."""
    from ietf_llm.seed import format as fmt

    return fmt.Index(
        generated="2026-08-11T20:24:52Z",
        compat=fmt.CompatTuple(11, "m", "rfcfyi-1", 384),
        corpora=[
            fmt.IndexEntry(
                name=n,
                kind="custom",
                subject="",
                window_months=12,
                gathered="2026-08-11T20:24:52Z",
                version="20260811T003915Z+2",
                manifest=f"{n}/manifest.json",
                bytes=1,
            )
            for n in names
        ],
    )


def test_an_unreachable_store_names_the_error(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reason a fresh `--init` most often reports nothing useful: the
    index read soft-failed and the caller could only guess at why."""
    import ietf_llm.seed.fetch as sf

    _stub_store(monkeypatch, sf.SeedFetchError("HTTP Error 404: Not Found"))
    reason = rfc_corpus.ensure_rfc_corpus(Verbosity.QUIET, interval=0.0)
    assert reason and "404" in reason


def test_a_store_without_the_entry_says_so(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_store(monkeypatch, _index("httpbis"))
    reason = rfc_corpus.ensure_rfc_corpus(Verbosity.QUIET, interval=0.0)
    assert reason and rfc_corpus.RFC_CORPUS in reason


def test_a_current_corpus_declines_with_no_reason(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing to install is not a failure, and `--init` must not report one."""
    _install_local("20260811T003915Z+2")
    _stub_store(monkeypatch, _index(rfc_corpus.RFC_CORPUS))
    assert rfc_corpus.ensure_rfc_corpus(Verbosity.QUIET, interval=0.0) is None


def test_the_throttle_says_it_is_the_throttle(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_store(monkeypatch, _index("httpbis"))
    rfc_corpus.ensure_rfc_corpus(Verbosity.QUIET, interval=0.0)
    reason = rfc_corpus.ensure_rfc_corpus(Verbosity.QUIET)
    assert reason and "checked within" in reason
    # And names the stamp, which is the whole remedy when the mtime is in the
    # future (clock skew, a restored backup) and even interval=0.0 throttles.
    assert rfc_corpus._STAMP in reason


def test_the_throttle_is_not_a_reason_when_the_corpus_is_current(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A reason is about the corpus, not about the call. Throttling a machine
    that already has a current corpus is an ordinary success, and reporting it
    as a cause would send `--init` looking for a problem that is not there."""
    _install_local("20260811T003915Z+2")
    _stub_store(monkeypatch, _index(rfc_corpus.RFC_CORPUS))
    rfc_corpus.ensure_rfc_corpus(Verbosity.QUIET, interval=0.0)
    assert rfc_corpus.ensure_rfc_corpus(Verbosity.QUIET) is None


def test_the_disabled_seeding_remedy_is_a_command_that_runs(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`ietf-llm --seed` alone exits 2 — a corpus name is required — so naming
    it would hand the user of a machine that has no corpus a dead end."""
    monkeypatch.setenv("IETF_LLM_SEED_ENABLED", "off")
    reason = rfc_corpus.ensure_rfc_corpus(Verbosity.QUIET)
    assert reason and "--init --seed" in reason


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

    def _read(url: str, **_kw: Any) -> Any:
        fetched.append(url)
        raise sf.SeedFetchError("stub")

    monkeypatch.setattr(sf, "read_index", _read)

    rfc_corpus.ensure_rfc_corpus(Verbosity.QUIET)
    rfc_corpus.ensure_rfc_corpus(Verbosity.QUIET)
    assert len(fetched) == 1, "the second call should have been throttled"

    # A zero interval is the escape hatch the tests and a forced check use.
    rfc_corpus.ensure_rfc_corpus(Verbosity.QUIET, interval=0.0)
    assert len(fetched) == 2


def test_init_runs_the_housekeeping_without_a_corpus_name(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--init` is the only route to the RFC corpus for a machine that never
    gathers, so it must not require the corpus name a gather does."""
    import sys as _sys

    from ietf_llm.cli import main as cli_main

    ran: List[bool] = []
    monkeypatch.setattr(
        cli_main, "_housekeeping", lambda v, forced=False: ran.append(forced)
    )
    monkeypatch.setattr(_sys, "argv", ["ietf-llm", "--init"])
    with pytest.raises(SystemExit) as exc:
        cli_main.main()
    assert exc.value.code == 0
    # Forced: asking explicitly should not be silently throttled away.
    assert ran == [True]


def test_init_reports_the_state_it_leaves(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    """A setup command that prints nothing on success is indistinguishable
    from one that silently failed — which is what `--init` did when the
    machine was already current, since the housekeeping only logs changes."""
    import sys as _sys

    from ietf_llm.cli import main as cli_main

    monkeypatch.setattr(cli_main, "_housekeeping", lambda v, forced=False: None)
    monkeypatch.setattr(_sys, "argv", ["ietf-llm", "--init"])
    _install_local("20260811T003915Z+2")
    with pytest.raises(SystemExit):
        cli_main.main()
    out = capsys.readouterr().out
    assert "20260811T003915Z+2" in out


def test_init_says_so_when_the_corpus_is_missing(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    """The case worth being loud about: the RFC tools will not work."""
    import sys as _sys

    from ietf_llm.cli import main as cli_main

    monkeypatch.setattr(cli_main, "_housekeeping", lambda v, forced=False: None)
    monkeypatch.setattr(_sys, "argv", ["ietf-llm", "--init"])
    with pytest.raises(SystemExit):
        cli_main.main()
    out = capsys.readouterr().out
    assert "NOT installed" in out
    # Nothing named a cause, so the report falls back rather than inventing one.
    assert "writable" in out


def test_init_reports_why_the_corpus_is_missing(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    """The whole point of the plumbing: seeding off, an empty seed URL, an
    unreachable store and a store missing the entry all end in the same "not
    installed", and only the step that declined knows which it was."""
    import sys as _sys

    from ietf_llm.cli import main as cli_main

    monkeypatch.setattr(
        cli_main, "_housekeeping", lambda v, forced=False: "seeding is disabled"
    )
    monkeypatch.setattr(_sys, "argv", ["ietf-llm", "--init"])
    with pytest.raises(SystemExit):
        cli_main.main()
    out = capsys.readouterr().out
    assert "seeding is disabled" in out
    assert "writable" not in out  # the guess is not printed alongside the answer


def test_init_seed_re_enables_seeding_before_installing(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--init --seed` is the remedy the disabled-seeding reason names, so it
    has to work on a machine with no corpus to gather. The toggle must land
    before the housekeeping, or the run it is meant to unblock still sees
    seeding off."""
    import sys as _sys

    from ietf_llm.cli import main as cli_main
    from ietf_llm.config import service as service_config

    service_config.set_seeding_enabled(False)
    seen: List[bool] = []
    monkeypatch.setattr(
        cli_main,
        "_housekeeping",
        lambda v, forced=False: seen.append(service_config.seeding_enabled()),
    )
    monkeypatch.setattr(_sys, "argv", ["ietf-llm", "--init", "--seed"])
    with pytest.raises(SystemExit) as exc:
        cli_main.main()
    assert exc.value.code == 0
    assert seen == [True]
    assert service_config.seeding_enabled()  # and it persisted


def test_init_without_the_flag_leaves_the_toggle_alone(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sys as _sys

    from ietf_llm.cli import main as cli_main
    from ietf_llm.config import service as service_config

    service_config.set_seeding_enabled(False)
    monkeypatch.setattr(cli_main, "_housekeeping", lambda v, forced=False: None)
    monkeypatch.setattr(_sys, "argv", ["ietf-llm", "--init"])
    with pytest.raises(SystemExit):
        cli_main.main()
    assert not service_config.seeding_enabled()


def test_housekeeping_hands_back_the_corpus_reason(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--init` can only report what `_housekeeping` passes up."""
    from ietf_llm.cli import main as cli_main

    monkeypatch.setattr(cli_main, "ensure_rfc_index", lambda v: None)
    monkeypatch.setattr(cli_main, "ensure_catalog_index", lambda v: None)
    monkeypatch.setattr(cli_main, "sync_if_pristine", lambda v: None)
    monkeypatch.setattr(
        cli_main, "ensure_rfc_corpus", lambda v, interval=None: "no store"
    )
    assert cli_main._housekeeping(Verbosity.QUIET, forced=True) == "no store"
