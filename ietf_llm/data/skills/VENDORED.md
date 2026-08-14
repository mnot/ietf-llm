# Vendored skill text

The skills in this directory are **vendored, not authored here.** Their
canonical source is:

    https://github.com/mnot/ietf-skill
    tag v0.5.0  (commit 322c0c3c3400e546e43a12af1565f5556c57ddfd)
    licensed CC-BY-4.0

**Not all of it is CC-BY-4.0.** The skill text is; the RFC `.txt` files under
`ietf-http/reference/` and `ietf-reviewing/reference/` are not — they are RFCs,
copyright the IETF Trust or the Internet Society and their authors, reproduced
under <https://trustee.ietf.org/license-info>. Each `reference/` directory
carries the upstream `NOTICE` saying so, vendored with it; read those before
reasoning about redistribution of the wheel, not this line.

Whatever that repo publishes is what ships here: the vendoring script discovers
the skill set rather than naming it, so a skill added upstream arrives on the
next re-vendor and one retired upstream is pruned. The MCP server serves two of
their bodies as tools — `read_ietf_participation_norms` (`ietf-contributing`)
and `read_ietf_interpretation_norms` (`ietf-interpreting`) — and its
instructions (`data/mcp-instructions.md`) point at them; the rest ship for
`--install-skills` only.

Do **not** edit these files here — edit them upstream and re-vendor with
`make vendor-skills`. Run with no argument to track the newest upstream tag, or
pass one to pin a specific version (`make vendor-skills REF=v0.2.2`); either way
it rewrites the tag+commit line above for you — review the diff and commit.

`make vendor-skills-check` verifies the on-disk files match that pin, **and CI
runs it on every push**, so drift from the pin fails the build rather than
sitting here unnoticed.

Routing is not vendored: it lives in the MCP server's own instructions, not a
skill.
