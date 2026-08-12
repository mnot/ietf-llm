"""Platform trust-store verification, and recognising a verification failure.

The motivating bug: a machine behind a TLS-inspecting corporate proxy trusted
the proxy's root, but `requests` verifies against certifi and `urllib` against
OpenSSL's defaults — neither of which is the macOS keychain — so every fetch
failed, `--init` could not install the RFC corpus, and the reported error was
raw OpenSSL text that read as though the seed store were at fault.
"""

from __future__ import annotations

import ssl
from typing import Any

import pytest

from ietf_llm import tls


def _verify_error() -> ssl.SSLCertVerificationError:
    return ssl.SSLCertVerificationError(
        1, "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed"
    )


# --- finding the verification failure ---------------------------------------


def test_a_bare_verification_error_is_found() -> None:
    err = _verify_error()
    assert tls.certificate_error(err) is err


def test_the_urllib_shape_is_found() -> None:
    """`urllib` raises URLError whose `reason` *is* the SSL error — reachable
    through neither `__cause__` nor `__context__`."""
    import urllib.error

    inner = _verify_error()
    assert tls.certificate_error(urllib.error.URLError(inner)) is inner


def test_the_requests_shape_is_found() -> None:
    """`requests` buries it two wrappers down and links it via `__context__`:
    requests.SSLError -> MaxRetryError -> urllib3.SSLError -> the real one."""
    inner = _verify_error()
    middle = OSError("HTTPSConnectionPool: Max retries exceeded")
    middle.__context__ = inner
    outer = OSError("SSLError")
    outer.__context__ = middle
    assert tls.certificate_error(outer) is inner


def test_an_unrelated_error_is_not_a_certificate_error() -> None:
    """The hint has to stay silent on the 404s and timeouts, or it becomes
    noise attached to every failure."""
    assert tls.certificate_error(OSError("HTTP Error 404: Not Found")) is None
    assert tls.certificate_hint(OSError("timed out")) == ""


def test_a_reason_that_is_not_an_exception_does_not_break_the_walk() -> None:
    """`.reason` is a string on plenty of exception types."""

    class Weird(OSError):
        reason = "just a string"

    err = Weird("nope")
    err.__cause__ = _verify_error()
    assert tls.certificate_error(err) is not None


def test_a_reference_cycle_terminates() -> None:
    """Exception chains can loop; the walk must not."""
    a, b = OSError("a"), OSError("b")
    a.__context__ = b
    b.__context__ = a
    assert tls.certificate_error(a) is None


def test_the_hint_names_a_remedy_that_needs_no_reinstall() -> None:
    hint = tls.certificate_hint(_verify_error())
    assert "SSL_CERT_FILE" in hint and "REQUESTS_CA_BUNDLE" in hint


# --- the context ------------------------------------------------------------


def test_a_platform_context_is_built_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(tls._ENV, raising=False)
    import truststore

    ctx = tls.system_trust_context()
    assert isinstance(ctx, truststore.SSLContext)


@pytest.mark.parametrize("value", ["off", "0", "false", "no", "OFF"])
def test_the_opt_out_is_honoured(value: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """For the deployment whose platform store is the less complete of the two
    — a container without `ca-certificates`, where certifi does the real work."""
    monkeypatch.setenv(tls._ENV, value)
    assert tls.system_trust_context() is None


def test_an_unavailable_truststore_is_not_fatal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """This only ever widens what we can reach, so failing to widen it must not
    break a machine where nothing was wrong. None means "default verification"."""
    monkeypatch.delenv(tls._ENV, raising=False)
    import truststore

    def _boom(*_a: Any, **_kw: Any) -> Any:
        raise RuntimeError("unsupported platform")

    monkeypatch.setattr(truststore, "SSLContext", _boom)
    assert tls.system_trust_context() is None


# --- scoped, never global ---------------------------------------------------


def test_we_never_inject_into_ssl_globally() -> None:
    """`truststore.inject_into_ssl()` rebinds `ssl.SSLContext` process-wide,
    which makes stdlib's `options` setter recurse into itself — and boto3 sets
    `options` while building a client, so the cloud storage backend dies with a
    RecursionError. Verified before this test was written: a single
    `inject_into_ssl()` call is enough to break `boto3.client(...)`. The guard
    is a source check because the damage is global and irreversible in-process,
    so there is nothing left to assert on once it has happened. Over the AST
    rather than the text, so the module docstring explaining the trap does not
    trip the guard against it."""
    import ast
    import pathlib

    offenders = []
    for path in pathlib.Path(tls.__file__).parent.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Call) and (
                getattr(node.func, "attr", None) == "inject_into_ssl"
                or getattr(node.func, "id", None) == "inject_into_ssl"
            ):
                offenders.append(f"{path}:{node.lineno}")
    assert not offenders, offenders


def test_boto3_still_works_after_we_build_our_context() -> None:
    """The regression this design exists to avoid, asserted end to end rather
    than by proxy: build our context the way the transports do, then construct
    the client the cloud backend constructs."""
    pytest.importorskip("boto3")
    import boto3

    assert tls.system_trust_context() is not None
    boto3.client(
        "s3",
        region_name="us-east-1",
        aws_access_key_id="x",
        aws_secret_access_key="y",
    )


def test_the_requests_session_carries_the_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ietf_llm.net import transport

    monkeypatch.delenv(tls._ENV, raising=False)
    adapter = transport._build_session(None).get_adapter("https://example.org/")
    assert isinstance(adapter, transport._TrustStoreAdapter)
    # Both pools, not just the direct one: the machine that needs the OS trust
    # store is usually the one reaching us through a proxy.
    assert adapter.poolmanager.connection_pool_kw["ssl_context"] is not None


def test_the_session_falls_back_when_opted_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ietf_llm.net import transport

    monkeypatch.setenv(tls._ENV, "off")
    adapter = transport._build_session(None).get_adapter("https://example.org/")
    assert not isinstance(adapter, transport._TrustStoreAdapter)


def test_the_seed_fetcher_passes_a_context(monkeypatch: pytest.MonkeyPatch) -> None:
    """`seed.fetch` is on urllib, not requests, and it is the transport the
    reported failure actually hit (`--init` reads the store index)."""
    import ietf_llm.seed.fetch as sf

    monkeypatch.delenv(tls._ENV, raising=False)
    seen: dict[str, Any] = {}

    class _Resp:
        def read(self) -> bytes:
            return b"{}"

        def __enter__(self) -> "_Resp":
            return self

        def __exit__(self, *_a: Any) -> None:
            return None

    def _urlopen(_req: Any, **kwargs: Any) -> Any:
        seen.update(kwargs)
        return _Resp()

    monkeypatch.setattr(sf.urllib.request, "urlopen", _urlopen)
    sf._read_bytes("https://example.org/index.json")
    assert seen["context"] is not None


def test_the_seed_reason_carries_the_hint(
    isolated_home: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End of the wire: a verification failure reaching `--init` has to say so,
    not just relay OpenSSL."""
    import ietf_llm.seed.fetch as sf
    from ietf_llm.gather.sources import rfc_corpus
    from ietf_llm.log import Verbosity

    monkeypatch.setenv("IETF_LLM_SEED_ENABLED", "on")
    monkeypatch.setattr(rfc_corpus.service_config, "seed_url", lambda: "https://x/")

    wrapped = sf.SeedFetchError("cannot read https://x/index.json")
    wrapped.__cause__ = _verify_error()

    def _read(_url: str, **_kw: Any) -> Any:
        raise wrapped

    monkeypatch.setattr(sf, "read_index", _read)
    reason = rfc_corpus.ensure_rfc_corpus(Verbosity.QUIET, interval=0.0)
    assert reason and "SSL_CERT_FILE" in reason
