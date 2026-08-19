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
offers `inject_into_ssl()`, which rebinds `ssl.SSLContext` process-wide. An
application should not do that to every other library in its process on
principle — but it is also concretely broken here. stdlib
`ssl.SSLContext.options` resolves `SSLContext` through the module global its
setter closes over, so rebinding makes the setter recurse into itself, and
botocore sets `options` while building a client.

Observed on macOS 26.5.2 / CPython 3.13.14 / truststore 0.10.4, against both
botocore 1.43.67 and 1.43.70, in a clean venv::

    python -c "import truststore, boto3; truststore.inject_into_ssl(); \\
               boto3.client('s3', region_name='us-east-1')"
    RecursionError: maximum recursion depth exceeded

`botocore.httpsession.create_urllib3_context()` is the call that recurses. A
review of this change did not reproduce it at those same versions, so treat it
as environment-sensitive rather than universal — re-run the line above before
concluding it has been fixed upstream, and note the design does not rest on it.

So this module hands a context to the transports we own and leaves every other
TLS client on the machine exactly as it was.

**Not injecting is not enough — someone else's injection poisons ours.** The
trap belongs to the rebound global, not to whoever rebound it. An interpreter
that starts with `pip._vendor.truststore.inject_into_ssl()` — a corporate macOS
build's way of teaching every Python the MDM root — leaves `ssl.SSLContext`
naming a *subclass*, which is then what `truststore` captures and wraps. On
CPython 3.14, where `verify_mode` grew the same global-resolving setter that
`options` has, the assignment urllib3 makes for every connection recurses
~980 frames deep and `--init` dies in the RFC index fetch. Any rebinding will
do it — this is the report's traceback, frame for frame::

    python -c "import ssl; ssl.SSLContext = type('S', (ssl.SSLContext,), {}); \\
               import truststore; \\
               truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT).verify_mode = 2"

`_usable` below is the guard: make those assignments here, where declining is
still possible, rather than at request time where it is a traceback. On the
machine that motivated it the fallback costs nothing — its injected
`ssl.SSLContext` *is* a platform-verifying context, so default verification
goes through the platform anyway. That is what
`_platform_verified_elsewhere` tests, and why declining there is quiet: a
`[WARN]` on every run of a tool that is verifying exactly as intended would be
a false alarm nobody can act on.

The cost of that choice is reach: `dulwich` (the transcripts repo sync), `llm`
(summariser plugins) and `google.auth`'s own session build their own contexts
and still verify against their own defaults. `SSL_CERT_FILE` and
`REQUESTS_CA_BUNDLE` cover much of that, which is why `CERTIFICATE_HINT` names
them rather than claiming the problem is solved — and why
`system_trust_context` below is careful to keep `SSL_CERT_FILE` working.

`IETF_LLM_SYSTEM_TRUST_STORE=off` opts out, for the deployment whose platform
store is the *less* complete of the two (a container without `ca-certificates`
installed, say, where certifi is doing the real work).
"""

from __future__ import annotations

import os
import ssl
from typing import Any, List, Optional

from .log import LogLevel, log

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

#: The same remedy for the process that is *not* verifying against the platform
#: — opted out, or declined by `_usable`. The sentence above would send someone
#: to audit a keychain we never consulted, and its root being installed there is
#: exactly what did not help.
CERTIFICATE_HINT_NO_PLATFORM = (
    "the server's TLS certificate could not be verified, and your OS trust "
    "store was not consulted — platform verification is off in this process "
    "(see IETF_LLM_SYSTEM_TRUST_STORE, and any earlier warning about "
    "ssl.SSLContext). Point SSL_CERT_FILE and REQUESTS_CA_BUNDLE at the PEM of "
    "the root that signs your traffic, typically a TLS-inspecting proxy"
)


#: Set once a platform context has proven unusable in this process (see
#: `_usable`). Nothing un-poisons an interpreter mid-run, and the proof costs a
#: near-1000-frame recursion, so we ask once.
_UNUSABLE = False


def _opted_out() -> bool:
    return os.environ.get(_ENV, "").strip().lower() in _OFF


def _platform_verified_elsewhere() -> bool:
    """Is something else in this process already verifying through the platform?

    An injected `truststore` — the very thing that makes our own context
    unusable — leaves every plain `ssl.SSLContext()` verifying against the OS
    store, so declining costs that process nothing. Sniffing the class is
    inexact by nature; it decides only how loudly we say what happened.
    """
    return "truststore" in getattr(ssl.SSLContext, "__module__", "").split(".")


def _usable(context: ssl.SSLContext) -> bool:
    """Does `context` survive what our transports are about to do to it?

    urllib3 assigns `verify_mode` on every connection it builds, and `options`
    while building a context. Both stdlib setters resolve the class through the
    `ssl` module global (`super(SSLContext, SSLContext).verify_mode.__set__`),
    so in a process where anything has rebound `ssl.SSLContext` to a subclass
    they recurse into themselves — see the module docstring for the enterprise
    build where that happens. Making the assignments here, to the values they
    already hold, moves the discovery to the one place that can still decline.
    """
    try:
        context.verify_mode = context.verify_mode
        context.options = context.options
    except Exception:  # pylint: disable=broad-except
        return False
    return True


def system_trust_context() -> Optional[ssl.SSLContext]:
    """An `SSLContext` verifying through the platform trust store, or None.

    None means "carry on with the default verification" — opted out,
    `truststore` unavailable or unsupported here, or a context this process
    cannot configure (`_usable`). This only ever widens what we can reach, so
    failing to widen it is not worth an error on a machine where nothing was
    broken to begin with.

    A fresh context per call: `SSLContext` is not documented as safe to share
    across urllib3 pools and urllib calls, and building one is cheap next to
    the request it is about to serve.
    """
    global _UNUSABLE  # pylint: disable=global-statement

    if _UNUSABLE or _opted_out():
        return None
    try:
        import truststore  # pylint: disable=import-outside-toplevel

        context = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        # Load the *default* CA material as extra anchors. Without this the
        # context carries none, and on macOS truststore does not read them
        # itself — so passing this to `urlopen` would silently stop honouring
        # `SSL_CERT_FILE`, which is the remedy `CERTIFICATE_HINT` names and the
        # only one that reaches the urllib path at all. Widening only: the
        # platform store still applies, so this can add trust, never remove it.
        try:
            context.load_default_certs()
        except (OSError, ssl.SSLError):
            pass  # a malformed SSL_CERT_FILE must not cost us the platform store
        if not _usable(context):
            _UNUSABLE = True
            # Said once, because declining is otherwise invisible and the
            # failure it defers — a certificate error on an intercepted network
            # — then reads as though the platform store had been consulted and
            # come up short. Naming `ssl.SSLContext` gives whoever runs the
            # machine the one string to search their startup hooks for.
            # Nothing at all where an injected truststore is still verifying
            # against the platform: there is nothing to act on, and no
            # verbosity reaches here to hang a --verbose line on.
            if not _platform_verified_elsewhere():
                log(
                    "TLS: platform trust-store verification is unavailable in "
                    "this process — something has replaced ssl.SSLContext, "
                    "which makes the stdlib setters recurse. Falling back to "
                    "certifi / OpenSSL defaults.",
                    level=LogLevel.WARN,
                )
            return None
        return context
    except Exception:  # pylint: disable=broad-except
        return None


def trust_store_adapter(**pool_kwargs: Any) -> Any:
    """A `requests` `HTTPAdapter` whose pools verify through the OS trust store,
    or a plain one when platform verification is unavailable or opted out.

    The factory rather than the class, so every `requests` transport we own is
    built the same way and none of them has to know whether a context was
    available. `requests` is imported inside, keeping this module stdlib-only at
    import time — it is reached from the serve path, which must stay light.
    """
    # pylint: disable-next=import-outside-toplevel
    from requests.adapters import HTTPAdapter

    context = system_trust_context()
    if context is None:
        return HTTPAdapter(**pool_kwargs)

    class _TrustStoreAdapter(HTTPAdapter):
        """Passes the context to urllib3 as a pool kwarg. Note `proxy_manager_for`
        as well as `init_poolmanager`: the machine that needs the OS trust store
        is usually the one reaching us through a proxy, so the proxied pool is
        exactly the one that must carry it."""

        def init_poolmanager(self, *args: Any, **kwargs: Any) -> None:
            kwargs["ssl_context"] = context
            super().init_poolmanager(*args, **kwargs)

        def proxy_manager_for(self, *args: Any, **kwargs: Any) -> Any:
            kwargs["ssl_context"] = context
            return super().proxy_manager_for(*args, **kwargs)

    return _TrustStoreAdapter(**pool_kwargs)


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
    """The hint for `err`, or empty when `err` is not a verification failure.

    Which hint depends on what verified the certificate: `CERTIFICATE_HINT`
    claims the platform, so it is wrong for the process that opted out or had to
    decline — unless something else injected a platform-verifying context, in
    which case the OS store was consulted after all.
    """
    if not certificate_error(err):
        return ""
    if (_UNUSABLE or _opted_out()) and not _platform_verified_elsewhere():
        return f" — {CERTIFICATE_HINT_NO_PLATFORM}"
    return f" — {CERTIFICATE_HINT}"
