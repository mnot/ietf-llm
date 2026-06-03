"""The NotebookLM (Google auth) dependency is an optional `notebooklm`
extra. Verify the export module imports without pulling Google in, and
that the push path degrades cleanly when the extra is absent so a plain
install can still do mirror-mode export.
"""

from __future__ import annotations

import subprocess
import sys

from ietf_llm import export
from ietf_llm.utils import Verbosity

_PROBE = r"""
import sys
import ietf_llm.export  # must not import google at module load
leaked = sorted(
    n for n in sys.modules
    if n == "google" or n.startswith("google.") or n.startswith("google_auth")
)
assert not leaked, "importing ietf_llm.export pulled in: %s" % leaked
print("OK")
"""


def test_importing_export_does_not_import_google():
    proc = subprocess.run(
        [sys.executable, "-c", _PROBE],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert "OK" in proc.stdout


def test_notebooklm_push_without_extra_is_graceful(monkeypatch):
    # Force the lazy `from .notebooklm import ...` to fail as if the extra
    # were not installed; the push must return 0, not raise ImportError.
    monkeypatch.setitem(sys.modules, "ietf_llm.notebooklm", None)
    n = export.notebooklm(
        "anywg", "proj", "client_secrets.json", "token.json",
        verbose=Verbosity.QUIET,
    )
    assert n == 0
