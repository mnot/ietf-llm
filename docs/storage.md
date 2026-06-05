# Storage & locations

**This document is for:** relocating where ietf-llm keeps its files — the corpus cache, the config,
and the per-corpus search index — for example onto a faster volume or a mounted directory in a
container. — Back to the [docs index](README.md).

By default everything lives under the usual home directories:

```
~/.cache/ietf-llm/<name>/         the gathered corpus + its embeddings.db
~/.config/ietf-llm/             config (global config.json, per-corpus dirs)
```

The cache is the single source of truth; the corpus tree is self-contained and relocatable (chunk
paths are relative to the cache root). Each location can be pointed elsewhere with an environment
variable:

| Variable | What | Default |
|---|---|---|
| `IETF_LLM_CACHE_DIR` | corpus root (the gathered files) | `~/.cache/ietf-llm` |
| `IETF_LLM_CONFIG_DIR` | config root | `~/.config/ietf-llm` |
| `IETF_LLM_INDEX_DIR` | per-corpus `embeddings.db` files | the cache root |
| `IETF_LLM_INDEX_IMMUTABLE` | read the index in SQLite immutable mode (read-only mounts) | off |

An unset (or blank) variable falls back to the default, so the local CLI is unaffected.

## Separating the index from the corpus

`IETF_LLM_INDEX_DIR` exists because the index has a different access profile from the rest of the
corpus: search reads the whole of a corpus's `embeddings.db` on every query, where the corpus
files are read occasionally. Pointing the index at fast or RAM-backed storage (tmpfs) while the
corpus lives elsewhere can be worthwhile for a busy server:

```bash
export IETF_LLM_CACHE_DIR=/data/ietf-llm        # corpus, on a normal volume
export IETF_LLM_INDEX_DIR=/dev/shm/ietf-llm      # embeddings.db, on tmpfs
```

The layout under each root is the same as the defaults (`<root>/<name>/…`), so a directory can be
moved by setting the variable and relocating its contents — no re-gather required.

## The index must be on a local filesystem

Wherever `IETF_LLM_INDEX_DIR` points, it has to be a **local POSIX filesystem** — a real disk or
tmpfs, never a network filesystem (NFS, SMB) or an object-store FUSE mount. The index is a SQLite
database, and SQLite's WAL and file locking are unreliable over those, risking corruption or
"database is locked" errors. Serving an object-stored index means **materialising it to local disk
first**, not pointing `IETF_LLM_INDEX_DIR` at a remote mount.

## Serving from a read-only mount

The index is a SQLite database in WAL mode. A plain read opens fine when the index dir is
**writable** — the local CLI, or a tmpfs index — because SQLite can create the small `-wal` / `-shm`
sidecars it needs there. On a **read-only** mount those sidecars can't be created and the open
fails. For a published, immutable index that nothing rewrites in place, set:

```bash
export IETF_LLM_INDEX_IMMUTABLE=1
```

SQLite then reads the database file directly, skipping the WAL and locking. Use it **only** when the
index is genuinely immutable (a read replica you publish and swap atomically), never while a gather
rewrites it in place.

## Corpus store backend (local vs cloud)

By default ietf-llm reads and writes the cache directly — the **local** corpus store, where the
live `<cache>/<corpus>` tree is the one and only version. The CLI and a single-box server use this
and need none of the rest of this section.

For a replicated, ephemeral deployment (many serving containers, gather driven by cron *and* by the
in-session MCP tools), set `IETF_LLM_STORE_BACKEND=cloud`. The cloud store keeps durable state in
two places — a transactional **control plane** (per-corpus version pointer, manifests, gather
leases) and an immutable **blob store** (the versioned `files/` + `embeddings.db`) — and
materialises the current version onto local scratch to serve reads. A gather publishes a new
immutable version and flips the pointer in one transaction, so every replica sees the old version or
the new (never a torn one), and a cross-host lease keeps a cron gather and an in-session gather from
clobbering each other.

| Variable | What | Required |
|---|---|---|
| `IETF_LLM_STORE_BACKEND` | `local` (default) or `cloud` | — |
| `IETF_LLM_CONTROL_DB` | control-plane locator: a filesystem path → local SQLite. A cloud database-API adapter (e.g. Cloudflare D1) plugs into the same SQL seam | cloud |
| `IETF_LLM_BLOB_DIR` | blob-store locator: a directory path → `file://` (bundled), or `s3://bucket/prefix` → S3-compatible (needs `[s3]`) | cloud |
| `IETF_LLM_BLOB_ENDPOINT_URL` | S3 endpoint URL for a non-AWS service (R2, MinIO); unset = AWS | s3 only |
| `IETF_LLM_SCRATCH_DIR` | local dir to materialise versions into | cloud |
| `IETF_LLM_RESOLVE_TTL` | seconds a replica caches a corpus's current-version lookup; `0` disables (default `10`) | — |

These non-secret knobs may instead be set in the global `config.json` (`store_backend`,
`control_db`, `blob_dir`, `scratch_dir`, `resolve_ttl`); the environment wins. Any secret (an
object-store key, a database password) comes from the environment only and is never read from the
config file.

**Resolving the current version.** Every read first resolves the corpus's current version through
the control plane, so a replica caches that lookup for `IETF_LLM_RESOLVE_TTL` seconds (default 10) to
coalesce a burst of reads into one control-plane call — which also keeps the D1 HTTP path comfortably
under the account-wide Cloudflare API rate limit. Versions are immutable and the pointer moves only
on publish, so a stale hit just serves a valid older version for at most the TTL; the replica that
publishes refreshes its own cache immediately, and others pick up a new version within the TTL. Set
`0` to resolve on every read (immediate cross-replica visibility, more control-plane calls).

**Choosing a control-plane backend.** `IETF_LLM_CONTROL_DB` selects the transactional control plane
(version pointer, manifests, gather leases), reached through a pluggable **SQL executor** seam — the
control-plane logic is identical across backends (the lease is a single conditional `RETURNING`
upsert, and publish is one atomic two-statement batch — one round trip, no interactive transaction),
so a stateless cloud database over HTTP behaves exactly like a local file.

- A **filesystem path** (e.g. `/var/lib/ietf-llm/control.db`) selects the bundled **SQLite** backend.
  You provide the path; the directory, the database file, and the schema are all created on first use
  — nothing to migrate by hand. Being SQLite it must sit on a **local POSIX filesystem** (never
  NFS/SMB or an object-store FUSE mount, where SQLite's locking is unreliable), and it coordinates any
  number of *processes on one host* but **not writers across hosts**. So the SQLite backend fits a
  **single host** (the serve process(es) and the cron gather sharing one local file) or development.
- For **multiple hosts**, point it at a **SQLite-compatible cloud database reached over its HTTP
  API**, via that backend's adapter. That is the configuration in which the cross-host behaviour
  described above (every replica resolving the same pointer; one lease shared between a cron gather
  and the serve fleet) actually holds. **Cloudflare D1** ships today: set
  `IETF_LLM_CONTROL_DB=d1://<account_id>/<database_id>` and the API token in
  `IETF_LLM_CONTROL_DB_TOKEN` (a **secret** — environment only, never `config.json`). The D1 adapter
  uses only D1's HTTP API (no Workers binding), runs the lease as one `/raw` call and publish as one
  atomic D1 `batch`, and pulls no extra dependency. Other SQLite-compatible cloud databases (e.g.
  libSQL/Turso) plug in as additional adapters behind the same seam.

**Choosing a blob backend.** `IETF_LLM_BLOB_DIR` selects the immutable blob store (the versioned
`files/` + `embeddings.db`):

- A **directory path** selects the bundled `file://` backend — fine for development or a shared
  volume (whole-object writes + atomic rename), but not for a multi-host fleet.
- An **`s3://bucket/prefix`** locator selects the S3-compatible backend (`pip install ietf-llm[s3]`),
  which works against AWS S3, Cloudflare R2, or MinIO. For a non-AWS endpoint set
  `IETF_LLM_BLOB_ENDPOINT_URL` (e.g. the R2 `https://<account>.r2.cloudflarestorage.com`).
  Credentials come from the standard AWS environment / instance-role chain
  (`AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY`) — **secret**, environment only. It uses only
  whole-object GET/PUT/HEAD/LIST.

A real cloud fleet needs the object-store blob backend *and* a cloud control plane — `file://` blobs
or a local SQLite control DB each pin you to a single host.

In every case the program is the storage *client* (no FUSE mounts), and the object store needs no
special features because all atomicity lives in the control-plane pointer. The HTTP serve path
validates these knobs at boot and refuses to start if `cloud` is selected but under-configured (see
[mcp-server.md](mcp-server.md)).

## Scratch sizing, reaping, and the tmpfs trap

A cloud replica materialises the **whole** current version of each corpus it serves — the `files/`
tree plus `embeddings.db` — into `IETF_LLM_SCRATCH_DIR`, and keeps it to serve later reads without
re-fetching. Size scratch for roughly `Σ(actively-read corpora) × version-size × ~2`, where the `~2`
is the switch window: just after a gather publishes, a replica briefly holds both the version its
in-flight requests pinned and the new one it is materialising.

The replica reaps superseded versions on its own: when it materialises a new version of a corpus it
deletes that corpus's older version dirs, keeping the current version, any version an in-flight
request is still reading, and the one cached for imminent reads (`IETF_LLM_RESOLVE_TTL`). Reaping is
per-replica and non-destructive — blobs are immutable and retained, so a reaped version
re-materialises on demand if read again — so steady-state scratch stays bounded at the in-use
versions per corpus, not one copy per gather.

**tmpfs is RAM.** Putting `IETF_LLM_SCRATCH_DIR` on tmpfs (`/dev/shm`) makes the materialised index
fast, but every byte of scratch is then **memory** — size the pod for it, including the `~2×` switch
window. The publish/materialise paths also move blobs whole-object, so peak heap during a publish or
a cold first read is about the single largest file (usually `embeddings.db`), on top of scratch.

Orphaned **blobs** are not reaped: a killed publish, and every superseded version, leaves bytes in
the blob store. That is durable-cost growth, not a crash — drive it with a bucket lifecycle rule
(retain the current version plus a grace window) on the object store.

## Notes

- `IETF_LLM_CACHE_DIR` only needs to *exist* and be readable for a read-only consumer (the MCP
  server, `ietf-llm-search`); gather needs it writable.
- These pair naturally with [running the MCP server over HTTP](mcp-server.md), where the
  corpus is read-only and the index can sit on tmpfs for speed.
