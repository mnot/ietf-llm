"""The seed store: a static, JSON-described public mirror of prebuilt corpora a
local client can seed from and then freshen. Producer-facing publishing lives in
`scripts/publish_seeds.py`; the consumer fetch/install path is `seed.fetch`. Both
share the on-disk format in `seed.format`. See `docs/seed-store.md`.
"""

from __future__ import annotations
