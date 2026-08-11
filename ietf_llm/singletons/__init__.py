"""Cross-corpus singleton readers.

Not gathered Working Groups but fleet-wide indexes that span *every* effort,
each mirrored to a leading-underscore dir under the cache root (kept out of
`list_corpora` / `ietf-llm --list`) and read here on the offline read path:

  catalog.py  — the active-effort catalog (`_catalog/`), behind `find_efforts`
  rfcs.py     — the RFC-series index (`_rfc/`), behind `search_rfc_index` / `get_rfc`

Each is the read side of a `gather/sources/` writer twin (`sources/catalog.py`,
`sources/rfcs.py`) and owns the on-disk location its writer mirrors into — so
the writers import the path constants (`catalog_index_dir` / `rfc_index_dir`,
`CATALOG_FILE` / `RFC_FILES`) from here. The two readers share no code and are
independent peers, so they stay separate submodules with nothing re-exported at
the package level; import them as `singletons.catalog` / `singletons.rfcs`. See
`docs/architecture.md`, "Cross-corpus singletons".
"""
