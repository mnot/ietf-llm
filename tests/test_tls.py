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


def test_ssl_cert_file_still_reaches_the_context(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bare `truststore.SSLContext` carries no CA material, and on macOS
    truststore does not read the default paths itself — so handing one to
    `urlopen` silently stopped honouring `SSL_CERT_FILE`. That is the remedy
    `CERTIFICATE_HINT` names, and the only one that reaches the urllib path at
    all (`REQUESTS_CA_BUNDLE` is requests-only), so breaking it would leave the
    hint pointing at nothing."""
    import certifi

    monkeypatch.delenv(tls._ENV, raising=False)
    one = tmp_path / "one-ca.pem"
    first = certifi.contents().split("-----END CERTIFICATE-----")[0]
    one.write_text(f"{first}-----END CERTIFICATE-----\n")

    monkeypatch.setenv("SSL_CERT_FILE", str(one))
    ctx = tls.system_trust_context()
    assert ctx is not None
    # Reading the wrapped context: that is where truststore looks for the extra
    # anchors it passes to the platform verifier.
    assert len(ctx._ctx.get_ca_certs()) == 1


def test_a_malformed_ssl_cert_file_does_not_cost_us_the_platform_store(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(tls._ENV, raising=False)
    bad = tmp_path / "nonsense.pem"
    bad.write_text("this is not a certificate")
    monkeypatch.setenv("SSL_CERT_FILE", str(bad))
    assert tls.system_trust_context() is not None


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


def _poison(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Stand in for the reported interpreter: a platform context whose
    `verify_mode` setter recurses. Returns the real `truststore.SSLContext`, so
    a test can put it back and watch the verdict hold."""
    import truststore

    real = truststore.SSLContext

    class _Poisoned(real):  # type: ignore[valid-type,misc]
        @property
        def verify_mode(self) -> Any:
            return ssl.CERT_REQUIRED

        @verify_mode.setter
        def verify_mode(self, value: Any) -> None:
            raise RecursionError("maximum recursion depth exceeded")

    monkeypatch.delenv(tls._ENV, raising=False)
    monkeypatch.setattr(tls, "_UNUSABLE", False)
    monkeypatch.setattr(truststore, "SSLContext", _Poisoned)
    return real


def test_a_context_this_process_cannot_configure_is_declined(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The reported failure: an enterprise macOS interpreter starts with
    `pip._vendor.truststore.inject_into_ssl()`, so `ssl.SSLContext` names a
    subclass and CPython 3.14's `verify_mode` setter — which resolves the class
    through that global — recurses into itself. urllib3 sets `verify_mode` on
    every connection, so `--init` died ~980 frames deep in the RFC index fetch.
    We cannot un-poison their interpreter; we can decline to hand over a context
    that will explode at request time."""
    import truststore

    real = _poison(monkeypatch)
    assert tls.system_trust_context() is None

    # And the verdict sticks: nothing un-poisons an interpreter mid-run, so a
    # later call must not pay for the recursion again.
    monkeypatch.setattr(truststore, "SSLContext", real)
    assert tls.system_trust_context() is None


def test_declining_is_said_out_loud_once(
    monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    """Silent degradation would leave the certificate error it defers looking
    like an OS trust store that had been consulted and come up short. Once,
    because the verdict is cached and a gather builds contexts in a loop."""
    monkeypatch.delenv("IETF_LLM_LOG_FORMAT", raising=False)
    _poison(monkeypatch)

    assert tls.system_trust_context() is None
    first = capsys.readouterr().err
    assert first.startswith("[WARN] ")
    assert "ssl.SSLContext" in first

    assert tls.system_trust_context() is None
    assert capsys.readouterr().err == ""


def test_the_hint_stops_claiming_a_trust_store_we_did_not_consult(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`CERTIFICATE_HINT` opens by saying we already verify against the OS trust
    store. In the process that declined, that sends the reader to audit a
    keychain we never opened — where an installed root is exactly what did not
    help."""
    monkeypatch.delenv(tls._ENV, raising=False)
    monkeypatch.setattr(tls, "_UNUSABLE", True)
    hint = tls.certificate_hint(_verify_error())
    assert tls.CERTIFICATE_HINT_NO_PLATFORM in hint
    assert "SSL_CERT_FILE" in hint and "REQUESTS_CA_BUNDLE" in hint


def test_the_hint_follows_the_opt_out_too(monkeypatch: pytest.MonkeyPatch) -> None:
    """Same sentence, same wrongness, for the deployment that turned platform
    verification off on purpose."""
    monkeypatch.setattr(tls, "_UNUSABLE", False)
    monkeypatch.setenv(tls._ENV, "off")
    assert tls.CERTIFICATE_HINT_NO_PLATFORM in tls.certificate_hint(_verify_error())


def test_an_injected_truststore_keeps_the_decline_quiet(
    monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    """The reported box again: its `ssl.SSLContext` is pip's vendored
    `truststore`, so every default context there verifies against the keychain
    and our declining changes nothing anyone can act on. A `[WARN]` on every run
    would be a false alarm — and the certificate hint must keep crediting the OS
    trust store, because that is what checked the certificate."""
    monkeypatch.delenv("IETF_LLM_LOG_FORMAT", raising=False)
    _poison(monkeypatch)

    class _Injected:
        pass

    _Injected.__module__ = "truststore._api"
    monkeypatch.setattr(ssl, "SSLContext", _Injected)

    assert tls.system_trust_context() is None
    assert capsys.readouterr().err == ""
    assert tls.CERTIFICATE_HINT in tls.certificate_hint(_verify_error())


def test_an_injected_interpreter_declines_rather_than_crashing() -> None:
    """The reported environment itself, not a stand-in for it: an interpreter
    whose `ssl.SSLContext` is pip's vendored `truststore` (a corporate startup
    hook's way of teaching every Python the MDM root). In a subprocess because
    `inject_into_ssl()` is global and irreversible — the same reason the guard
    above it is a source check. Without the `_usable` probe this process gets a
    context that raises RecursionError on the first request."""
    import subprocess
    import sys

    pytest.importorskip("pip._vendor.truststore")
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            "from pip._vendor import truststore; truststore.inject_into_ssl();"
            " from ietf_llm import tls;"
            " assert tls.system_trust_context() is None; print('declined')",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert "declined" in proc.stdout


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


def test_the_adapter_carries_the_context_into_both_pools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(tls._ENV, raising=False)
    adapter = tls.trust_store_adapter(pool_maxsize=2)
    assert adapter.poolmanager.connection_pool_kw["ssl_context"] is not None
    # Not just the direct pool: the machine that needs the OS trust store is
    # usually the one reaching us through a proxy.
    proxied = adapter.proxy_manager_for("http://proxy.example.org:3128")
    assert proxied.connection_pool_kw["ssl_context"] is not None


def test_the_adapter_falls_back_when_opted_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(tls._ENV, "off")
    adapter = tls.trust_store_adapter(pool_maxsize=2)
    assert "ssl_context" not in adapter.poolmanager.connection_pool_kw


def test_no_requests_call_bypasses_the_trust_store() -> None:
    """Scanned across the whole package rather than a list of known transports.

    "We missed one" happened twice — the remote embedding client, then the
    remote summariser and the NotebookLM push — and both times the guard named
    the modules it already knew about, so it could not have caught either. A
    module-level `requests.get`/`post` or a bare `HTTPAdapter` is the shape of
    the mistake, so that is what this looks for.
    """
    import pathlib

    root = pathlib.Path(tls.__file__).parent
    offenders = []
    for path in sorted(root.rglob("*.py")):
        offenders += _bypasses(path.read_text(encoding="utf-8"), path.name)
    assert not offenders, offenders


def _bypasses(source: str, name: str = "x.py") -> "list[str]":
    """Calls in `source` that would reach the network without the trust store."""
    import ast

    tree = ast.parse(source)
    # `requests` is a perfectly ordinary variable name — `live_lookup/reviews.py`
    # keeps a dict of review requests and calls `.get()` on it, which matching the
    # name alone flagged. Gate on the module actually importing the library.
    imports_requests = any(
        isinstance(n, ast.Import) and any(a.name == "requests" for a in n.names)
        for n in ast.walk(tree)
    )
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        # A module-level `requests.get(...)` builds a throwaway session, which
        # verifies against certifi rather than the OS trust store.
        if (
            imports_requests
            and isinstance(func, ast.Attribute)
            and getattr(func.value, "id", None) == "requests"
            and func.attr in ("get", "post", "put", "delete", "request", "head")
        ):
            found.append(f"{name}:{node.lineno} requests.{func.attr}()")
        # A bare adapter carries no context; `tls.trust_store_adapter` is the
        # only sanctioned way to build one (and defines the subclass).
        if getattr(func, "id", None) == "HTTPAdapter" and name != "tls.py":
            found.append(f"{name}:{node.lineno} HTTPAdapter()")
    return found


def test_the_bypass_scan_catches_what_it_should_and_nothing_else() -> None:
    """The scan above is only worth its noise if it still fires. Narrowing it to
    dodge the `reviews.py` false positive could have neutered it silently."""
    assert _bypasses("import requests\nrequests.post('u')\n")
    assert _bypasses("import requests\nrequests.get('u')\n")
    assert _bypasses("from requests.adapters import HTTPAdapter\nHTTPAdapter()\n")
    # A local dict named `requests` is not the library.
    assert not _bypasses("requests = {}\nrequests.get('k')\n")
    # Nor is one in a module that never imports it, however it is spelled.
    assert not _bypasses("def f(requests):\n    return requests.get('k')\n")


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
