"""The MCP SDK pin, and the diagnostic for when it isn't honoured.

ietf-llm's server is built on the FastMCP bundled *inside* the `mcp` SDK
(`mcp.server.fastmcp`), which exists only in a window: 1.2.0 first shipped
it and 2.0.0 removed it again. While the dependency was unpinned a fresh
install resolved `mcp==2.0.0` and `ietf-llm-mcp` died at startup — and the
error blamed a missing `mcp`, which sent the reporter off installing the
unrelated `fastmcp` distribution (it "works" only because it pins `mcp<2`).

Two guards: the ceiling stays declared, and the resolved environment
actually provides the module. The second is what fails in CI the day the
resolve drifts back out of the window.
"""

from __future__ import annotations

from importlib import metadata
from pathlib import Path

import pytest

from ietf_llm.mcp.server import _fastmcp_import_error


def test_bundled_fastmcp_is_importable() -> None:
    # Not a tautology: `mcp.types` imports fine on 2.x, so only this
    # submodule distinguishes an in-window SDK from an out-of-window one.
    from mcp.server.fastmcp import FastMCP  # pylint: disable=import-outside-toplevel

    assert FastMCP is not None


def test_mcp_requirement_keeps_the_upper_bound() -> None:
    # Read the declaration, not `importlib.metadata`: installed metadata is
    # frozen at install time, so an editable checkout reports whatever the
    # pyproject said when it was last installed.
    tomllib = pytest.importorskip("tomllib")  # stdlib from 3.11
    path = Path(__file__).resolve().parents[1] / "pyproject.toml"
    declared = tomllib.loads(path.read_text(encoding="utf-8"))["project"]["dependencies"]
    mcp_reqs = [r for r in declared if r.split(";")[0].strip().startswith("mcp")]
    assert mcp_reqs, "ietf-llm must declare a dependency on mcp"
    assert any("<2" in r for r in mcp_reqs), (
        f"the mcp<2 ceiling is load-bearing (2.0 removed mcp.server.fastmcp); got {mcp_reqs}"
    )


def test_import_error_names_the_installed_version_not_a_missing_package() -> None:
    message = _fastmcp_import_error(ImportError("No module named 'mcp.server.fastmcp'"))
    installed = metadata.version("mcp")
    # The SDK *is* installed whenever this path is reached, so the message
    # must not claim otherwise — that was the misdiagnosis in the bug report.
    assert "not installed" not in message
    assert installed in message
    assert "No module named 'mcp.server.fastmcp'" in message


def test_import_error_steers_away_from_the_fastmcp_distribution() -> None:
    message = _fastmcp_import_error(ImportError("boom"))
    assert "pipx install --force ietf-llm" in message
    assert "Do not install the separate `fastmcp` package" in message
