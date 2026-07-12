# Vendored skill text

The norm skills in this directory — `ietf-contributing/` and `ietf-interpreting/`
— are **vendored, not authored here.** Their canonical source is:

    https://github.com/mnot/ietf-skill
    tag v0.2.1  (commit c76a3b11b1e3d910b21dd8319e78cb6f90639db6)
    licensed CC-BY-4.0

The MCP server serves their bodies via `read_ietf_participation_norms` /
`read_ietf_interpretation_norms`, and its instructions (`data/mcp-instructions.md`)
point at them.

Do **not** edit these two files here — edit them upstream and re-vendor with
`scripts/vendor-norms.sh`, which pins the tag above (bump it deliberately when
the norm text changes). Only the norms are vendored; routing lives in the MCP
server's own instructions, not a skill.
