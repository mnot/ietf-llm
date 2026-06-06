"""Tests for the `--draft` and `--mailing-list` CLI flags.

Focus is on the normalisation helpers and the persistence integration
with `config.merge` — the actual gather network paths are exercised
elsewhere with their existing mocks.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import requests

from ietf_llm import config
from ietf_llm.gather import github
from ietf_llm.gather.drafts import normalize_draft_name
from ietf_llm.gather.github import normalize_repo_short, validate_github_repos
from ietf_llm.gather.mbox import normalize_list_name
from ietf_llm.utils import Verbosity


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


def test_validate_draft_names_drops_unresolved(
    isolated_home: Path, monkeypatch: object,
) -> None:
    # validate_draft_names should drop names that Datatracker doesn't
    # know, returning only the valid subset.
    from ietf_llm.gather import drafts  # pylint: disable=import-outside-toplevel
    from ietf_llm.utils import Verbosity  # pylint: disable=import-outside-toplevel

    def fake_fetch_current_rev(name: str, _verbose: object) -> object:
        return 7 if name == "draft-real-thing" else None

    monkeypatch.setattr(  # type: ignore[attr-defined]
        drafts, "fetch_current_rev", fake_fetch_current_rev,
    )
    valid = drafts.validate_draft_names(
        ["draft-real-thing", "draft-typo-name", "not-a-draft"],
        verbose=Verbosity.QUIET,
    )
    assert valid == ["draft-real-thing"]


def test_validate_list_names_drops_unknown(
    isolated_home: Path, monkeypatch: object,
) -> None:
    from ietf_llm.gather import mbox  # pylint: disable=import-outside-toplevel
    from ietf_llm.utils import Verbosity  # pylint: disable=import-outside-toplevel

    def fake_fetch(url: str, headers: object = None) -> object:
        # Simulate mailarchive: only `httpbis` exists.
        return object() if "/arch/browse/httpbis/" in url else None

    # mbox binds `fetch_resource` at import time (from ..utils import
    # fetch_resource), so patch the name where it's looked up.
    monkeypatch.setattr(  # type: ignore[attr-defined]
        "ietf_llm.gather.mbox.fetch_resource", fake_fetch,
    )
    valid = mbox.validate_list_names(
        ["httpbis@ietf.org", "ghost@ietf.org"],
        verbose=Verbosity.QUIET,
    )
    assert valid == ["httpbis@ietf.org"]


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


# --- normalize_repo_short -------------------------------------------------


def test_normalize_repo_short_passthrough_owner_repo() -> None:
    # An owner that starts with "http" must NOT be mistaken for a URL.
    assert normalize_repo_short("httpwg/drafts") == "httpwg/drafts"


def test_normalize_repo_short_strips_github_url() -> None:
    assert normalize_repo_short("https://github.com/httpwg/drafts") == "httpwg/drafts"


def test_normalize_repo_short_strips_trailing_slash_and_git() -> None:
    assert normalize_repo_short("https://github.com/httpwg/drafts/") == "httpwg/drafts"
    assert normalize_repo_short("httpwg/drafts.git") == "httpwg/drafts"


# --- validate_github_repos ------------------------------------------------


class _Resp:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


def test_validate_github_repos_keeps_real_drops_missing(
    monkeypatch: object,
) -> None:
    # 200 for the real repo, 404 for the typo'd one.
    def fake_get(url: str, **_kwargs: object) -> _Resp:
        return _Resp(200 if "httpwg/drafts" in url else 404)

    monkeypatch.setattr(github, "governed_get", fake_get)  # type: ignore[attr-defined]
    valid = validate_github_repos(
        ["httpwg/drafts", "httpwg/typodrafts"], verbose=Verbosity.QUIET,
    )
    assert valid == ["httpwg/drafts"]


def test_validate_github_repos_drops_non_owner_repo(monkeypatch: object) -> None:
    # A value with no "owner/repo" shape is dropped without a network call.
    def boom(*_a: object, **_k: object) -> object:
        raise AssertionError("should not probe a malformed value")

    monkeypatch.setattr(github, "governed_get", boom)  # type: ignore[attr-defined]
    assert validate_github_repos(["just-a-name"], verbose=Verbosity.QUIET) == []


def test_validate_github_repos_keeps_on_network_error(monkeypatch: object) -> None:
    # An ambiguous failure (rate limit, outage) must keep the value rather
    # than discard working config.
    def fake_get(*_a: object, **_k: object) -> object:
        raise requests.RequestException("boom")

    monkeypatch.setattr(github, "governed_get", fake_get)  # type: ignore[attr-defined]
    assert validate_github_repos(["httpwg/drafts"], verbose=Verbosity.QUIET) == [
        "httpwg/drafts"
    ]


def test_download_github_issues_owner_starting_with_http(
    monkeypatch: object, tmp_path: Path,
) -> None:
    # Regression: an 'owner/repo' whose owner starts with "http"
    # (e.g. httpwg) must be treated as a short name, not a raw URL.
    seen: list = []

    def fake_get(url: str, **_kwargs: object) -> _Resp:
        seen.append(url)
        return _Resp(404)  # no gh-pages archive; falls through to the API

    monkeypatch.setattr(github, "governed_get", fake_get)  # type: ignore[attr-defined]
    monkeypatch.setattr(  # type: ignore[attr-defined]
        github, "_fetch_all_issues", lambda *a, **k: [],
    )
    dest = tmp_path / "drafts.json"
    ok = github.download_github_issues(
        "httpwg/drafts", str(dest), verbose=Verbosity.QUIET,
    )
    assert ok
    # The bug sent the bare 'httpwg/drafts' to requests.get; the fix resolves
    # it to the gh-pages archive URL instead.
    assert seen == ["https://httpwg.github.io/drafts/archive.json"]
