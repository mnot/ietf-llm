"""Global (non-WG) service config: precedence env > CLI > global-persisted
> default, with CLI write-through and the environment overriding even an
explicit CLI flag (container authority).
"""

from __future__ import annotations

import argparse

from ietf_llm import config

# (arg_name, env_var, default)
_SPEC = [
    ("embed_model", "IETF_LLM_EMBED_MODEL", None),
    ("no_embed", "IETF_LLM_NO_EMBED", False),
]


def _args(**kw):
    # None is the unset sentinel (what argparse stores when no flag is
    # given); a non-None value means the flag was supplied this run.
    ns = argparse.Namespace(embed_model=None, no_embed=None)
    for key, val in kw.items():
        setattr(ns, key, val)
    return ns


def test_default_when_nothing_set(isolated_home):
    args = _args()
    config.merge_global(args, _SPEC)
    assert args.embed_model is None
    assert args.no_embed is False


def test_cli_value_persists_globally(isolated_home):
    args = _args(embed_model="openai-embed/m")
    config.merge_global(args, _SPEC)
    assert args.embed_model == "openai-embed/m"
    # A later run with no CLI value picks up the persisted global value.
    args2 = _args()
    config.merge_global(args2, _SPEC)
    assert args2.embed_model == "openai-embed/m"


def test_env_overrides_global_and_default(monkeypatch, isolated_home):
    monkeypatch.setenv("IETF_LLM_EMBED_MODEL", "openai-embed/env")
    args = _args()
    config.merge_global(args, _SPEC)
    assert args.embed_model == "openai-embed/env"


def test_env_overrides_explicit_cli(monkeypatch, isolated_home):
    monkeypatch.setenv("IETF_LLM_EMBED_MODEL", "openai-embed/env")
    args = _args(embed_model="openai-embed/cli")
    config.merge_global(args, _SPEC)
    assert args.embed_model == "openai-embed/env"  # env wins this run


def test_cli_remembered_even_when_env_present(monkeypatch, isolated_home):
    # Env wins for the run, but the flag is still written through so it is
    # remembered once the env var is gone.
    monkeypatch.setenv("IETF_LLM_EMBED_MODEL", "openai-embed/env")
    args = _args(embed_model="openai-embed/cli")
    config.merge_global(args, _SPEC)
    assert config.load_global()["embed_model"] == "openai-embed/cli"
    monkeypatch.delenv("IETF_LLM_EMBED_MODEL")
    args2 = _args()
    config.merge_global(args2, _SPEC)
    assert args2.embed_model == "openai-embed/cli"


def test_bool_env_coercion(monkeypatch, isolated_home):
    monkeypatch.setenv("IETF_LLM_NO_EMBED", "yes")
    args = _args()
    config.merge_global(args, _SPEC)
    assert args.no_embed is True

    monkeypatch.setenv("IETF_LLM_NO_EMBED", "0")
    args = _args()
    config.merge_global(args, _SPEC)
    assert args.no_embed is False


def test_explicit_false_overrides_and_persists_globally(isolated_home):
    # --no-embed persists True...
    config.merge_global(_args(no_embed=True), _SPEC)
    assert config.load_global()["no_embed"] is True
    # ...and --embed (an explicit False, distinct from the unset None) both
    # overrides it this run AND clears it for later runs.
    args = _args(no_embed=False)
    config.merge_global(args, _SPEC)
    assert args.no_embed is False
    assert config.load_global()["no_embed"] is False
    later = _args()
    config.merge_global(later, _SPEC)
    assert later.no_embed is False


def test_records_resolution_source(monkeypatch, isolated_home):
    # default → config → cli → env, surfaced on args._global_sources.
    args = _args()
    config.merge_global(args, _SPEC)
    assert args._global_sources["no_embed"] == "default"

    config.merge_global(_args(no_embed=True), _SPEC)  # persist it
    from_config = _args()
    config.merge_global(from_config, _SPEC)
    assert from_config._global_sources["no_embed"] == "config"

    cli = _args(embed_model="openai-embed/m")
    config.merge_global(cli, _SPEC)
    assert cli._global_sources["embed_model"] == "cli"

    monkeypatch.setenv("IETF_LLM_NO_EMBED", "yes")
    env = _args()
    config.merge_global(env, _SPEC)
    assert env._global_sources["no_embed"] == "env"
