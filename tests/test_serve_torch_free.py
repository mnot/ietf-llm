"""R15: the serving / remote-embedding path must not import torch.

The dev venv has torch installed, so an in-process check is meaningless.
Instead run a fresh subprocess that imports the MCP server module and
resolves a remote `openai-embed/` model, then asserts torch and
sentence-transformers were never imported. Guards against anyone adding a
top-level torch import to the serve path, which would break the
torch-free container image (the local model is the optional
`local-embeddings` extra; the serve path must not depend on it).
"""

from __future__ import annotations

import subprocess
import sys

_PROBE = r"""
import os, sys
os.environ["IETF_LLM_EMBED_BASE_URL"] = "https://example.invalid/v1"
import ietf_llm.mcp_server  # imports the whole serve path
from ietf_llm.embeddings.models import _get_embed_model
from ietf_llm.utils import Verbosity

# Resolving the remote backend must construct without touching torch.
model = _get_embed_model("openai-embed/probe-model", Verbosity.QUIET)
assert model is not None, "remote backend should construct with a base URL set"

leaked = sorted(
    name
    for name in ("torch", "sentence_transformers", "llm_sentence_transformers")
    if name in sys.modules
)
assert not leaked, "serve path imported: %s" % leaked
print("OK")
"""


def test_serve_path_is_torch_free():
    proc = subprocess.run(
        [sys.executable, "-c", _PROBE],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert "OK" in proc.stdout
