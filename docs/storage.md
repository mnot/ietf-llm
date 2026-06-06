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

An unset (or blank) variable falls back to the default, so the local CLI is unaffected. A read-only
consumer (the MCP server, `ietf-llm-search`) only needs `IETF_LLM_CACHE_DIR` to *exist* and be
readable; gather needs it writable.

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

## The index is SQLite: where it can live

The index is a SQLite database in WAL mode, which constrains where `IETF_LLM_INDEX_DIR` can point:

- **A local POSIX filesystem only** — a real disk or tmpfs, never a network filesystem (NFS, SMB) or
  an object-store FUSE mount, where SQLite's WAL and locking are unreliable (corruption, or
  "database is locked"). To serve an object-stored index, materialise it to local disk first rather
  than point `IETF_LLM_INDEX_DIR` at a remote mount.
- **Writable, unless marked immutable** — a normal open creates the small `-wal` / `-shm` sidecars,
  so it fails on a read-only mount. For a published index that nothing rewrites in place, set
  `IETF_LLM_INDEX_IMMUTABLE=1`: SQLite then reads the file directly, skipping the WAL and locking.
  Use it *only* when the index really is immutable (a read replica you publish and swap atomically),
  never while a gather rewrites it.

## Corpus store backend (local vs cloud)

By default ietf-llm reads and writes the cache directly — the **local** store, where the live
`<cache>/<corpus>` tree is the only version. The CLI and a single-box server use this and can skip the
rest of this section.

Set `IETF_LLM_STORE_BACKEND=cloud` for a replicated, ephemeral deployment (many serving containers;
gather driven by cron *and* by the in-session MCP tools). The cloud store keeps durable state in a
**control plane** (a small SQL database of version pointers, manifests, and gather leases) and a
**blob store** (the immutable, versioned `files/` + `embeddings.db`), materialising the current
version onto local scratch to serve reads. How it works is in
[architecture.md](architecture.md); what to set is here.

| Variable | What | Required |
|---|---|---|
| `IETF_LLM_STORE_BACKEND` | `local` (default) or `cloud` | — |
| `IETF_LLM_CONTROL_DB` | control-plane locator — a filesystem path (SQLite) or `d1://<account>/<database>` (Cloudflare D1) | cloud |
| `IETF_LLM_CONTROL_DB_TOKEN` | API token for a `d1://` control DB (**secret**) | d1 |
| `IETF_LLM_BLOB_DIR` | blob-store locator — a directory path (`file://`) or `s3://bucket/prefix` (needs `[s3]`) | cloud |
| `IETF_LLM_BLOB_ENDPOINT_URL` | S3 endpoint for a non-AWS service (R2, MinIO); unset = AWS | s3 |
| `IETF_LLM_SCRATCH_DIR` | local dir to materialise versions into | cloud |
| `IETF_LLM_RESOLVE_TTL` | seconds to cache the current-version lookup; `0` disables (default `10`) | — |
| `IETF_LLM_GATHER_MAX_INFLIGHT` | max gathers running concurrently — per host and fleet-wide (default `3`) | — |
| `IETF_LLM_HTTP_MAX_PER_HOST` | max gather HTTP requests in flight per host — non-datatracker (default `6`) | — |
| `IETF_LLM_HTTP_MAX_DATATRACKER` | max gather HTTP requests in flight to datatracker (default `2`) | — |

The non-secret knobs may instead go in the global `config.json` (`store_backend`, `control_db`,
`blob_dir`, `scratch_dir`, `resolve_ttl`); the environment wins. The two
`IETF_LLM_HTTP_MAX_*` caps are environment-only (the governor that reads them
sits below `config` in the import graph; see `ietf_llm/http_governor.py`). Secrets
(`IETF_LLM_CONTROL_DB_TOKEN`, object-store keys) are environment-only. The HTTP serve path validates
all of this at boot and refuses to start if `cloud` is under-configured (see
[mcp-server.md](mcp-server.md)).

**Single host vs fleet.** A `file://` blob dir or a SQLite control DB each pin you to one host —
SQLite must be on a local POSIX filesystem (never NFS/SMB), coordinating processes on one box, not
across hosts. A real fleet needs both:

- **Control DB** — point `IETF_LLM_CONTROL_DB` at a SQLite-compatible cloud database over HTTP.
  **Cloudflare D1** ships today: `d1://<account_id>/<database_id>` plus the token in
  `IETF_LLM_CONTROL_DB_TOKEN`. (libSQL/Turso and others can plug in behind the same seam.)
  Both locator segments are **IDs from the Cloudflare dashboard, not names**:
  - `<account_id>` — the Account ID (a 32-char hex string), not the account name.
  - `<database_id>` — the **Database ID**, a UUID like `0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d`
    shown on the database's page. This is the common slip: it is **not** the database name you
    chose when creating it. A name here is rejected at startup with a message saying so.
  - `IETF_LLM_CONTROL_DB_TOKEN` — a Cloudflare **API token** (not the account's Global API Key)
    scoped with D1 edit permission.
- **Blob store** — `s3://bucket/prefix` works against AWS S3, Cloudflare R2, or MinIO. It needs the
  `s3` extra: `pipx install 'ietf-llm[s3]'` (quote it so the shell doesn't glob the brackets). For a
  non-AWS endpoint set `IETF_LLM_BLOB_ENDPOINT_URL`; credentials come from the standard AWS
  environment / instance-role chain.

**Current-version cache.** A new version is visible to a replica within `IETF_LLM_RESOLVE_TTL`
seconds of a publish (the publishing replica sees it at once). Raise it to cut control-plane calls,
or set `0` for instant cross-replica visibility.

**Scratch.** A replica materialises whole versions into `IETF_LLM_SCRATCH_DIR` and reaps superseded
ones automatically, so size it for roughly 2× the versions you actively read per corpus. **On tmpfs,
scratch is RAM** — size the pod accordingly. Orphaned *blobs* are not reaped; set a bucket lifecycle
rule (retain the current version plus a grace window) to cap object-store cost.
