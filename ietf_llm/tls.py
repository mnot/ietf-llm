"""Verify TLS against the operating system's trust store, and recognise when
verification is what failed.

**Why this exists.** `requests` verifies against certifi's bundle and `urllib`
against OpenSSL's default paths, and on macOS neither of those is the system
keychain. So a machine behind a TLS-inspecting corporate proxy fails every
fetch we make — including the seed store, so `--init` cannot install the RFC
corpus — against a root certificate the machine itself already trusts. That is
the failure that motivated this: the enterprise root was installed and trusted,
and we were the only thing on the box that could not see it.

`truststore` builds an `SSLContext` that verifies through the platform (macOS
Security framework, Windows CryptoAPI, OpenSSL's configured store on Linux),
which is where an MDM-deployed root actually lives. It is pure Python with no
transitive dependencies, so the torch-free serve path is unaffected.

**Scoped to our own transports, never injected globally.** `truststore` also
offers `inject_into_ssl()`, which rebinds `ssl.SSLContext` process-wide. That
is a trap here: stdlib `ssl.SSLContext.options` resolves `SSLContext` through
the module global its setter closes over, so rebinding it makes the setter
recurse into itself — and `boto3`, which the cloud storage backend is built on,
sets `options` while constructing a client. Verified: one `inject_into_ssl()`
call is enough to turn every `boto3.client(...)` into a `RecursionError`. So
this module hands a context to the two transports we own (`net.transport`'s
session and `seed.fetch`'s urllib calls) and leaves every other TLS client on
the machine exactly as it was.

The cost of that choice is reach: `dulwich` (the transcripts repo sync) and
`llm` (a remote embedding endpoint) build their own contexts and still verify
against their own defaults. `SSL_CERT_FILE` covers them, which is why
`CERTIFICATE_HINT` names it rather than claiming the problem is solved.

`IETF_LLM_SYSTEM_TRUST_STORE=off` opts out, for the deployment whose platform
store is the *less* complete of the two (a container without `ca-certificates`
installed, say, where certifi is doing the real work).
"""

from __future__ import annotations

import os
import ssl
from typing import List, Optional

#: Opt out of platform verification, falling back to certifi / OpenSSL defaults.
_ENV = "IETF_LLM_SYSTEM_TRUST_STORE"

_OFF = ("0", "false", "no", "off")

#: Appended to whatever error we are already reporting. Deliberately does not
#: assume the proxy case is the user's to fix: naming the two environment
#: variables gives them a remedy that needs no reinstall and no admin.
CERTIFICATE_HINT = (
    "the server's TLS certificate could not be verified. We already verify "
    "against your OS trust store, so if you are behind a TLS-inspecting proxy "
    "whose root is not installed there, point SSL_CERT_FILE and "
    "REQUESTS_CA_BUNDLE at its PEM"
)


def system_trust_context() -> Optional[ssl.SSLContext]:
    """An `SSLContext` verifying through the platform trust store, or None.

    None means "carry on with the default verification" — opted out, or
    `truststore` unavailable or unsupported here. This only ever widens what we
    can reach, so failing to widen it is not worth an error on a machine where
    nothing was broken to begin with.

    A fresh context per call: `SSLContext` is not documented as safe to share
    across urllib3 pools and urllib calls, and building one is cheap next to
    the request it is about to serve.
    """
    if os.environ.get(_ENV, "").strip().lower() in _OFF:
        return None
    try:
        import truststore  # pylint: disable=import-outside-toplevel

        return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    except Exception:  # pylint: disable=broad-except
        return None


def certificate_error(err: BaseException) -> Optional[ssl.SSLCertVerificationError]:
    """The certificate-verification failure inside `err`, if there is one.

    Both transports bury it, differently: `urllib` raises `URLError` whose
    `reason` is the `SSLCertVerificationError`, while `requests` wraps it as
    `requests.SSLError` -> `MaxRetryError` -> `urllib3.SSLError` and links the
    original through `__context__`. Walking all three links catches both, and
    keys on the exception type rather than on the message text — which is
    OpenSSL's to reword.
    """
    seen: set[int] = set()
    stack: List[BaseException] = [err]
    while stack:
        cur = stack.pop()
        if id(cur) in seen:
            continue
        seen.add(id(cur))
        if isinstance(cur, ssl.SSLCertVerificationError):
            return cur
        for link in (getattr(cur, "reason", None), cur.__cause__, cur.__context__):
            if isinstance(link, BaseException):
                stack.append(link)
    return None


def certificate_hint(err: BaseException) -> str:
    """`CERTIFICATE_HINT` when `err` is a verification failure, else empty."""
    return f" — {CERTIFICATE_HINT}" if certificate_error(err) else ""
