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
- **The MCP surface budget** (`tests/test_mcp_surface_budget.py`) ratchets the
  bytes every client pays before asking anything: the `instructions` field plus
  the serialized tool list, for both the stdio and read-only-HTTP shapes.
  Ceilings live in `tests/mcp_surface_baseline.json`; growth fails until you
  regenerate them in the same commit, so the diff shows what a docstring cost.
  Shrink freely — but tighten the ratchet after a real trim (the test asks).
  Regenerate with:
  `IETF_LLM_UPDATE_MCP_BASELINE=1 .venv/bin/python -m pytest tests/test_mcp_surface_budget.py`
  To decide *what* to cut (none of these are gates):
  `scripts/mcp_surface_report.py` (weight per tool in tokens, repeated
  phrasing), `scripts/lint-prose.sh` (vale over the instructions + extracted
  docstrings), `scripts/mcp_tool_similarity.py` (which descriptions a model
  can't tell apart; needs the embedding model, so not in CI). All three read
  `ietf_llm/mcp/surface.py`, which is also what the gate measures.

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
- One feature per commit; PR only when asked.

## PR checklist (for reviews + before opening PRs)

Most items point to a section below for the detail — this is the scan, not
the spec. Skip an item only when you can say *why* it doesn't apply.

- **Reader-side vs write-side?** Does this change what gather *writes*? If so
  existing caches are stale until re-gathered (cloud: until *published*) —
  flag it in the PR. See the section below.
- **Local backend.** Default `local` (filesystem) path still works and is
  unchanged in behaviour? This is the path almost everyone runs.
- **Cloud backend.** Touch `store/` / S3 / the control plane? Re-check
  the CAS pointer flip, the gather lease, the `fleet/slots` semaphore, and
  the accelerator caches. See "Conventions that are load-bearing".
- **Concurrency.** Cloud is multi-host: any new shared state needs a
  CAS/lease story (no SQL, no transaction — `get` + conditional `put`). Local
  is single-process — don't assume that on cloud.
- **Efficiency.** Too much time waiting for a result (or a gather) will
  lose us users.
- **Source burden.** We need to be friendly to our back-end services: datatracker,
  GitHub, etc.
- **Read-only boundary.** Read tools stay read-only and offline. New
  network/write code belongs only on the gather path (or the gather tools
  `start_gather` / `gather_status`, registered only when gather is enabled).
  Don't materialise a cache from a read tool.
- **NotebookLM / export use case.** `ietf_llm/export.py` mirrors the `.txt`/
  `.md` files under a corpus's `files/` (bundled by year/repo) for NotebookLM.
  Changing what gather writes there changes the export — it's write-side. And
  the serve path must stay **torch-free** (`tests/test_serve_torch_free.py`):
  remote `openai-embed/` embeddings (`embeddings/models.py`) and HTTP transport
  must keep working without importing torch.
- **Operability.** New failure mode that needs to be diagnosable in the cloud
  deployment? Wire it into what exists — no external logging/metrics libs, all
  hand-rolled: structured logs (`IETF_LLM_LOG_FORMAT=json`, `log.log`),
  Prometheus RED metrics at `GET /metrics` (`serve_metrics.py`), `GET /health`
  readiness, per-gather egress in `gather-metrics.json` (`net/http_metrics.py`),
  and opt-in request telemetry (`IETF_LLM_DEBUG_LOG`, `get_session_log`).
- **Dead code sweep.** Remove anything this change orphans — old branches,
  now-unused helpers, superseded config.
- **Docs.** `README.md` (usage) and `docs/architecture.md` (design) still
  accurate? Update them in the same PR.
- **Routing brain + tool docstrings.** `ietf_llm/data/mcp-instructions.md` is
  the routing brain, served to clients as the MCP `instructions` field. Update
  it and the affected tool docstrings whenever behaviour changes. (The vendored
  norms skills under `ietf_llm/data/skills/<name>/SKILL.md` are separate
  installed Agent Skills, not the routing brain.)
- **The gate.** `make test lint typecheck` clean (pylint 10.00/10), and
  `ietf_llm/` is black-clean. See "The gate" above.

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

- `ietf_llm/data/mcp-instructions.md` is served to clients as the MCP
  `instructions` field — it's the routing brain. Update it (and the tool
  docstrings) whenever behaviour changes. (The installed Agent Skills live
  separately under `ietf_llm/data/skills/<name>/SKILL.md`.)
- The MCP server is read-only and never touches the network; gather is
  the only writer. Keep that boundary. (`get_wg_file_cache_dir` *creates*
  the dir — use a read-only existence check at the tool boundary so a
  mistyped corpus name doesn't materialise a junk cache.)
  - **The exceptions, all behind one gate.** `freshness.gather_enabled` is
    the project's sanctioned networked-read gate: a networked *read* belongs
    on the same side of the line as the networked *writer*, so both default on
    for stdio and off for the HTTP replica, which keeps that deployment
    read-only. Behind it today: the **gather tools** (`start_gather` /
    `gather_status`), which let MCP initiate a gather, so they write *and*
    reach the network; the **live Datatracker lookups** (`meeting_schedule`,
    `draft_status`, `overview(live=True)`); the **seed catalog** refresh in
    `list_corpora`; and **`get_rfc`**, which revalidates the `_rfc/` mirror on
    a stale miss (`docs/architecture.md`, "The networked read exception").
    Adding a *new* one is a real design decision — argue it against that
    section — but it is a sanctioned pattern, not a boundary breach.
    Everything else stays read-only and offline. The gather tools default **on**
    for a local stdio server (that user can already run `ietf-llm` against the
    same cache) and **off** for the shared HTTP deployment (keeping it
    read-only); `IETF_LLM_ENABLE_GATHER` overrides either way, and an
    immutable index mount also forces the default off. When gather is
    disabled the tools aren't registered, so the HTTP surface keeps the
    boundary intact.
- **The cache is reached through a `CorpusStore` seam** (`ietf_llm/store/`):
  read tools resolve a corpus's files dir via `get_corpus_store().local_cache_dir`
  (the `_files_dir` / `_corpus_exists` boundary in `mcp`, which also keeps
  the read-only existence check above), and a gather publishes via
  `store.publish`. The default `local` backend is today's filesystem — no
  behaviour change. The opt-in `cloud` backend (`IETF_LLM_STORE_BACKEND=cloud`,
  `IETF_LLM_STORE_URL=s3://…`) is **object-store only**: one S3-compatible bucket
  holds both the immutable version content (the blob plane) and the control plane
  — a `KvControlPlane` over the `KvStore` compare-and-swap seam (`get` + a
  conditional `put`; no SQL, no transaction). `S3KvStore` and `S3BlobStore` share
  one `S3Bucket`. Per-corpus control lives under `corpora/<name>/{pointer,lease,
  status}`, content (and its manifest) under `corpora/<name>/versions/<version>/`,
  and the one cross-corpora key — the gather-slot semaphore — at `fleet/slots`.
  The same bucket also holds the **gather accelerator caches** (`gather/sources/cache_sync.py`),
  hydrated before a gather and persisted after so an ephemeral host doesn't re-hit
  rate-limited upstreams: `.http-cache.json` sharded per corpus at
  `corpora/<name>/gather-cache/` (lease-serialised, plain RMW), the shared
  GitHub/datatracker identity maps at `fleet/gather-cache/` (CAS-merge), and the
  effort catalog (`_catalog/`) at `fleet/catalog/` (a shared fleet singleton, one
  key per file, last-writer-wins). These are gather-only (the read path never
  touches them) and best-effort. `_rfc/` is deferred (non-rate-limited CDN
  mirror). See issue #82.
  publish is the explicit write→read handoff (a compare-and-swap pointer flip),
  and the gather lease is cross-host. So on the cloud backend the reader-side vs
  write-side line above is mediated by *publish*: a re-gather is not visible to
  readers until it publishes a new version. See `docs/architecture.md` ("The
  storage seam").
