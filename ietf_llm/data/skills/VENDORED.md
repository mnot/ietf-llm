# Vendored skill text

The norm skills in this directory — `ietf-contributing/` and `ietf-interpreting/`
— are **vendored, not authored here.** Their canonical source is:

    https://github.com/mnot/ietf-skill
    tag v0.1.0  (commit 3aca3bfc1ab2c33ccf1640241c9656a3b371db9a)
    licensed CC-BY-4.0

The MCP server serves their bodies via `read_ietf_participation_norms` /
`read_ietf_interpretation_norms`, and its instructions floor points at them.

Do **not** edit these two files here — edit them upstream and re-vendor with
`scripts/vendor-norms.sh`, which pins the tag above (bump it deliberately when
the norm text changes). `ietf-corpus` (the query/routing skill) is **not**
vendored: it lives only in that repo and is installed by the user.
