"""The MCP SDK requirement, and the diagnostic for when it isn't honoured.

The server is built on `MCPServer`, which exists from `mcp` 2.0. Two guards:
the declared range stays inside the era that has it, and the resolved
environment actually provides it — the latter fails in CI if the resolve
drifts, which is how an upstream removal reached a user once (#203).
"""

from __future__ import annotations

from importlib import metadata
from pathlib import Path

import pytest
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from packaging.version import Version

from ietf_llm.mcp import server as mcp_server
from ietf_llm.mcp.server import _mcpserver_import_error


def test_mcpserver_is_importable() -> None:
    # Not a tautology: `mcp.types` imports fine on 1.x, so only this
    # submodule distinguishes a usable SDK from an older one.
    from mcp.server.mcpserver import MCPServer

    assert MCPServer is not None


def test_mcp_requirement_covers_exactly_the_2x_series() -> None:
    # Read the declaration, not `importlib.metadata`: installed metadata is
    # frozen at install time, so an editable checkout reports whatever the
    # pyproject said when it was last installed.
    tomllib = pytest.importorskip("tomllib")  # stdlib from 3.11
    path = Path(__file__).resolve().parents[1] / "pyproject.toml"
    declared = tomllib.loads(path.read_text(encoding="utf-8"))["project"]["dependencies"]
    # Match the distribution name exactly — `startswith("mcp")` would also
    # collect an unrelated future `mcp-something` dependency.
    reqs = [Requirement(r) for r in declared]
    mcp_reqs = [r for r in reqs if canonicalize_name(r.name) == "mcp"]
    assert len(mcp_reqs) == 1, f"expected exactly one mcp requirement, got {mcp_reqs}"
    spec = mcp_reqs[0].specifier
    # Assert on the versions themselves, not on the specifier's text.
    for excluded in ("1.10.0", "1.30.0", "3.0.0"):
        assert Version(excluded) not in spec, (
            f"mcp {excluded} is outside the 2.x series but satisfies {spec}"
        )
    for allowed in ("2.0.0", "2.3.0"):
        assert Version(allowed) in spec, f"mcp {allowed} should satisfy {spec}"


def test_import_error_names_the_installed_version_not_a_missing_package() -> None:
    message = _mcpserver_import_error(ImportError("No module named 'mcp.server.mcpserver'"))
    installed = metadata.version("mcp")
    # The SDK *is* installed whenever this path is reached, so the message
    # must not claim otherwise — that was the misdiagnosis in the bug report.
    assert "not installed" not in message
    assert installed in message
    assert "No module named 'mcp.server.mcpserver'" in message


def test_import_error_names_the_requirement_and_how_to_repair() -> None:
    message = _mcpserver_import_error(ImportError("boom"))
    assert "mcp>=2,<3" in message
    assert "pipx install --force" in message
    assert "pins `mcp<2`" in message


def test_import_error_handles_an_sdk_that_is_importable_but_not_installed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Reachable despite the docstring's argument: a source tree on sys.path or
    # a vendored copy imports without a distribution to report a version for.
    monkeypatch.setattr(mcp_server, "_installed_mcp_version", lambda: None)
    message = _mcpserver_import_error(ImportError("boom"))
    assert "is not installed" in message
    assert "boom" in message


def test_reinstall_command_preserves_installed_extras(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # `--force` drops extras, so a hint omitting them downgrades the very
    # install it is meant to repair. Pin the markers rather than depending on
    # which extras this venv happens to have.
    monkeypatch.setattr(
        mcp_server,
        "_EXTRA_MARKERS",
        (("present-extra", "mcp"), ("absent-extra", "no-such-dist-xyzzy")),
    )
    command = mcp_server._reinstall_command()
    assert command.strip() == "pipx install --force 'ietf-llm[present-extra]'"


def test_real_extra_markers_name_declared_extras() -> None:
    tomllib = pytest.importorskip("tomllib")
    path = Path(__file__).resolve().parents[1] / "pyproject.toml"
    declared = tomllib.loads(path.read_text(encoding="utf-8"))["project"][
        "optional-dependencies"
    ]
    for extra, _dist in mcp_server._EXTRA_MARKERS:
        assert extra in declared, f"{extra} is not a declared extra"


def test_reinstall_command_falls_back_when_no_extras_are_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(mcp_server, "_EXTRA_MARKERS", ())
    assert mcp_server._reinstall_command().strip() == "pipx install --force ietf-llm"
