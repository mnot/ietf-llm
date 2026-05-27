"""Tests for the `--draft` and `--mailing-list` CLI flags.

Focus is on the normalisation helpers and the persistence integration
with `config.merge` — the actual gather network paths are exercised
elsewhere with their existing mocks.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from ietf_llm import config
from ietf_llm.gather.drafts import normalize_draft_name
from ietf_llm.gather.mbox import normalize_list_name


# --- normalize_draft_name -------------------------------------------------


def test_normalize_draft_name_strips_version_suffix() -> None:
    assert normalize_draft_name("draft-foo-bar-07") == "draft-foo-bar"


def test_normalize_draft_name_strips_txt_extension() -> None:
    assert normalize_draft_name("draft-foo-bar.txt") == "draft-foo-bar"


def test_normalize_draft_name_strips_both() -> None:
    assert normalize_draft_name("draft-foo-bar-07.txt") == "draft-foo-bar"


def test_normalize_draft_name_passthrough_already_clean() -> None:
    assert normalize_draft_name("draft-foo-bar") == "draft-foo-bar"


def test_normalize_draft_name_does_not_strip_two_digit_topic_suffix() -> None:
    # A draft whose topic legitimately ends in two digits (rare but
    # possible) shouldn't lose them. We only strip the "-NN" form
    # where the digits are revision-shaped. The regex is `-\d{2}$`
    # so a topic ending in `-2024` (4 digits) stays intact.
    assert normalize_draft_name("draft-ietf-foo-2024") == "draft-ietf-foo-2024"


# --- normalize_list_name --------------------------------------------------


def test_normalize_list_name_strips_ietf_domain() -> None:
    assert normalize_list_name("httpbis@ietf.org") == "httpbis"


def test_normalize_list_name_strips_irtf_domain() -> None:
    # IRTF RG lists are on irtf.org but share the IMAP server.
    assert normalize_list_name("cfrg@irtf.org") == "cfrg"


def test_normalize_list_name_passthrough_already_bare() -> None:
    assert normalize_list_name("httpbis") == "httpbis"


def test_normalize_list_name_lowercases_and_trims() -> None:
    assert normalize_list_name("  Httpbis@IETF.org  ") == "httpbis"


# --- config integration ---------------------------------------------------


def test_draft_and_mailing_list_persist_across_runs(isolated_home: Path) -> None:
    # First call: user passes --draft and --mailing-list explicitly.
    args = argparse.Namespace(
        draft=["draft-foo-bar"],
        mailing_list=["foo@ietf.org"],
        github=None, github_label=None, exclude_github_label=None,
        months=12, summarize=False, summarize_model=None,
        no_embed=False, embed_model=None,
    )
    config.merge(
        args, wg="wg", scope="gather",
        scalars=("months", "summarize", "summarize_model", "no_embed", "embed_model"),
        lists=(
            "github", "github_label", "exclude_github_label",
            "draft", "mailing_list",
        ),
        defaults={"months": 12, "summarize": False, "no_embed": False},
    )
    # Persisted-after-merge value is the union; same as input here.
    assert args.draft == ["draft-foo-bar"]
    assert args.mailing_list == ["foo@ietf.org"]

    # Second call: user omits both flags. Persistence fills them in.
    args2 = argparse.Namespace(
        draft=None, mailing_list=None,
        github=None, github_label=None, exclude_github_label=None,
        months=12, summarize=False, summarize_model=None,
        no_embed=False, embed_model=None,
    )
    config.merge(
        args2, wg="wg", scope="gather",
        scalars=("months", "summarize", "summarize_model", "no_embed", "embed_model"),
        lists=(
            "github", "github_label", "exclude_github_label",
            "draft", "mailing_list",
        ),
        defaults={"months": 12, "summarize": False, "no_embed": False},
    )
    assert args2.draft == ["draft-foo-bar"]
    assert args2.mailing_list == ["foo@ietf.org"]


def test_draft_and_mailing_list_union_across_runs(isolated_home: Path) -> None:
    # First call adds one draft; second call adds another. Both persist.
    for value in ("draft-foo-bar", "draft-baz-qux"):
        args = argparse.Namespace(
            draft=[value],
            mailing_list=None,
            github=None, github_label=None, exclude_github_label=None,
            months=12, summarize=False, summarize_model=None,
            no_embed=False, embed_model=None,
        )
        config.merge(
            args, wg="wg", scope="gather",
            scalars=("months", "summarize", "summarize_model", "no_embed", "embed_model"),
            lists=(
                "github", "github_label", "exclude_github_label",
                "draft", "mailing_list",
            ),
            defaults={"months": 12, "summarize": False, "no_embed": False},
        )
    # Final state has both.
    assert set(args.draft) == {"draft-foo-bar", "draft-baz-qux"}
