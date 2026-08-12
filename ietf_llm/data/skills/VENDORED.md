# Vendored skill text

The skills in this directory are **vendored, not authored here.** Their
canonical source is:

    https://github.com/mnot/ietf-skill
    tag v0.4.1  (commit e127f7267c2a0abb6e0302652c99b5f838310c6e)
    licensed CC-BY-4.0

Whatever that repo publishes is what ships here: the vendoring script discovers
the skill set rather than naming it, so a skill added upstream arrives on the
next re-vendor and one retired upstream is pruned. The MCP server serves two of
their bodies as tools — `read_ietf_participation_norms` (`ietf-contributing`)
and `read_ietf_interpretation_norms` (`ietf-interpreting`) — and its
instructions (`data/mcp-instructions.md`) point at them; the rest ship for
`--install-skills` only.

Do **not** edit these files here — edit them upstream and re-vendor with
`scripts/vendor-skills.sh`. Run with no argument to track the newest upstream
tag, or pass one to pin a specific version (`scripts/vendor-skills.sh v0.2.2`);
either way the script rewrites the tag+commit line above for you — review the
diff and commit. `scripts/vendor-skills.sh --check` verifies the on-disk files
match that pin. Routing is not vendored: it lives in the MCP server's own
instructions, not a skill.
