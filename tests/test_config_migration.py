"""The 0.8.0 move of embed / summarise settings from per-WG gather.json to
the global config: a gather strips any legacy per-WG values (with a
notice) so they stop shadowing the global ones. Non-global per-WG keys are
left untouched.
"""

from __future__ import annotations

from pathlib import Path

from ietf_llm.gather import sequencer as main
from ietf_llm import config
from ietf_llm.log import Verbosity

_LEGACY = ("embed_model", "summarize", "no_embed", "summarize_model")


def test_migrate_strips_legacy_global_keys(isolated_home: Path):
    config.save(
        "wg",
        "gather",
        {
            "embed_model": "sentence-transformers/x",
            "summarize": True,
            "no_embed": True,
            "summarize_model": "gpt-4o-mini",
            "months": 6,  # a genuine per-WG key -- must be kept
        },
    )
    persisted = config.load("wg", "gather")
    main._migrate_global_keys("wg", persisted, Verbosity.QUIET)

    for key in _LEGACY:
        assert key not in persisted
    assert persisted["months"] == 6

    on_disk = config.load("wg", "gather")
    for key in _LEGACY:
        assert key not in on_disk
    assert on_disk["months"] == 6


def test_migrate_is_noop_without_legacy_keys(isolated_home: Path):
    config.save("wg", "gather", {"months": 12})
    persisted = config.load("wg", "gather")
    main._migrate_global_keys("wg", persisted, Verbosity.QUIET)
    assert persisted == {"months": 12}
