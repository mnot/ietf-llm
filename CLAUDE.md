# ietf-llm — working notes

`docs/architecture.md` has the design; `README.md` has usage. This file is the
working context that isn't in either — conventions and gotchas learned
the hard way.

## The gate (run before every commit)

- `make test lint typecheck` must pass. **pylint must be 10.00/10 with
  zero warnings** — any message fails the gate. (`.pylintrc`:
  max-branches=25; C0114/C0115/C0116/W0613/R0903 disabled.)
- **black is not in CI**, but keep `ietf_llm/` black-clean:
  `.venv/bin/black --check --target-version py313 <changed files>` before
  committing, or a later `make tidy` will churn them in an unrelated
  commit. `make tidy` formats **only `ietf_llm/`, not `tests/`** — do not
  run black over the test files; they use a compact style black fights
  and it isn't enforced there.
- Do **not** edit managed files: `.pylintrc`, `Makefile`,
  `Makefile.venv`, `Makefile.pyproject`.

## Commit conventions

- Subject: `Added:` / `Changed:` / `Fixed:` / `Removed:` + lowercase
  summary. A pure chore (e.g. applying black) gets **no prefix** — it is
  not a changelog entry (just `Apply black formatting`).
- End the body with a `Co-Authored-By: <model> <noreply@anthropic.com>`
  trailer naming the model you are *actually* running as — use your real
  current version, not a string copied from here or an old commit.
- **Gotcha:** `git commit -m "$(cat <<'EOF' … EOF)"` breaks on an
  apostrophe in the body (`corpus's`, `typo'd`, `WG's`) — shell reports
  "unexpected EOF". Reword to avoid apostrophes, or commit another way.
- One feature per commit; commit/branch only when asked.

## Reader-side vs write-side (matters for "does this need a re-gather?")

- Changes to how tools **render** (overview, `digest/query`,
  search, read_topic) are reader-side — existing caches benefit
  immediately.
- Changes to what gather **writes** (per-thread files / `elide_quotes`,
  `group.md`, `documents.json`, charter text, digests) are write-side —
  existing caches keep the old content until the next `ietf-llm <wg>`.
  Flag this when proposing such a change.

## Verifying

- Unit tests stub the network and embedding model. For real behaviour,
  call the `tool_*` functions directly against the gathered caches in
  `~/.cache/ietf-llm/` (`httpbis`, `tls`, `aipref`, …) — that is how most
  of the MCP work here was validated, and it catches things fixtures miss.
- **Test against real writer output, not only hand-built fixtures.** A
  digest-rendering bug slipped because `query_digest` was tested against
  synthetic tables that didn't match what the writer emits. The durable
  guard is a writer→reader round-trip: drive `generate_digests`, then
  read its bytes back through the tool.
- `_build_with_stub(wg, isolated_home)` (two args) builds an embedding
  index for a seeded cache. The stub model scores every chunk equally
  (search returns all of them — use `k`/limits to shape output).

## Conventions that are load-bearing

- `data/skill/SKILL.md` is served to clients as the MCP `instructions`
  field AND installed as the Claude skill — it's the routing brain.
  Update it (and the tool docstrings) whenever behaviour changes.
- The MCP server is read-only and never touches the network; gather is
  the only writer. Keep that boundary. (`get_wg_file_cache_dir` *creates*
  the dir — use a read-only existence check at the tool boundary so a
  mistyped corpus name doesn't materialise a junk cache.)
  - **The one exception:** the opt-in gather tools (`start_gather` /
    `gather_status`, gated behind `IETF_LLM_ENABLE_GATHER=1`) let MCP
    initiate a gather, so they *do* write and reach the network. They are
    the only sanctioned write/network path on the server — everything
    else stays read-only and offline. When the gate is unset the tools
    aren't registered, so the default surface keeps the boundary intact.
- **The cache is reached through a `CorpusStore` seam** (`corpus_store.py`):
  read tools resolve a corpus's files dir via `get_corpus_store().local_cache_dir`
  (the `_files_dir` / `_corpus_exists` boundary in `mcp_server`, which also keeps
  the read-only existence check above), and a gather publishes via
  `store.publish`. The default `local` backend is today's filesystem — no
  behaviour change. The opt-in `cloud` backend (`IETF_LLM_STORE_BACKEND=cloud`)
  puts the control plane behind a pluggable `SqlExecutor` seam (`query` + atomic
  `batch`, SQLite dialect): a local SQLite file (single-host/dev) or a
  SQLite-compatible cloud database over HTTP (e.g. a Cloudflare D1 adapter) for
  multi-host; the blob plane is `file://`. It makes publish the explicit
  write→read handoff (atomic version pointer flip) and the gather lease
  cross-host. So on the cloud backend the
  reader-side vs write-side line above is mediated by *publish*: a re-gather is
  not visible to readers until it publishes a new version. See
  `docs/architecture.md` ("The storage seam").
