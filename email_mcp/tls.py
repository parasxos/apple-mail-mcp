"""One client-side TLS context for every lane that speaks TLS itself.

python.org framework builds of CPython ship without a root-CA bundle
wired: ``ssl.get_default_verify_paths()`` names a cafile and a capath
that do not exist, and ``ssl.create_default_context()`` silently holds
ZERO trust anchors. Every verified handshake then fails with
``CERTIFICATE_VERIFY_FAILED: unable to get local issuer certificate``,
and it fails the same way on every retry — the environment, not the
peer, is broken. Graph learned this on 2026-07-29 (docs/graph-
calibration-2026-07-29.md §2); the smtp lane hit it against Gmail on
2026-09-14 once its own peer verification was switched on.

The context is built bare and its trust store loaded EXPLICITLY, in
this order, so what it trusts never depends on what OpenSSL would have
picked up behind our back:

1. an explicit ``cafile`` argument wins, verbatim;
2. a trust store the operator configured through ``SSL_CERT_FILE`` /
   ``SSL_CERT_DIR`` is honoured as is, with OpenSSL's own semantics:
   each variable replaces ITS component only (``SSL_CERT_DIR`` alone
   keeps the default cafile, and vice versa), presence counts rather
   than content (an empty value empties that component), and a missing
   file or a store that does not trust the peer is the operator's
   decision, never patched over with certifi;
3. otherwise the interpreter's own default store, when it is usable: a
   non-empty cafile, or a capath directory with entries (a hashed
   directory loads lazily and legitimately reads zero anchors until a
   handshake touches it). A cafile that exists but yields no anchor is
   NOT usable — that is the empty-default install, and it falls through;
4. otherwise certifi's bundle;
5. otherwise nothing: the context is returned empty and
   :func:`describe` says so, and the caller decides whether to refuse
   the handshake and name the remedy before any secret is spoken.

``describe()['roots']`` counts anchors LOADED so far, not anchors
available: a directory store reads 0 and still verifies.

Verification itself is never relaxed: every context carries the
running interpreter's own ``create_default_context`` settings —
``CERT_REQUIRED``, ``check_hostname``, and its ``verify_flags`` (3.13+
adds ``VERIFY_X509_STRICT`` and ``VERIFY_X509_PARTIAL_CHAIN``; older
interpreters keep theirs) — only the trust loading is ours. Nothing
global is touched (no env writes, no monkeypatching ``ssl``).
"""
from __future__ import annotations

import functools
import os
import ssl

_ENV_FILE = "SSL_CERT_FILE"
_ENV_DIR = "SSL_CERT_DIR"

# Source labels, stable for callers that render them.
SOURCE_EXPLICIT = "cafile"     # the caller passed a bundle
SOURCE_ENV = "env"             # SSL_CERT_FILE / SSL_CERT_DIR
SOURCE_SYSTEM = "system"       # the interpreter's own default store
SOURCE_CERTIFI = "certifi"     # certifi's bundle, the fallback
SOURCE_NONE = "none"           # nothing loaded anywhere

_ATTR = "_email_mcp_trust_source"


# --------------------------------------------------------------------- #
# seams — each one is a fact about the host, and tests replace them      #
# --------------------------------------------------------------------- #

def _env_store() -> tuple[str | None, str | None] | None:
    """The operator's configuration, if either variable is PRESENT; each
    member is the variable's value or None when that variable is absent
    (so the caller can keep the default for that component). An empty
    value is a configuration too: it empties its component."""
    if _ENV_FILE not in os.environ and _ENV_DIR not in os.environ:
        return None
    return os.environ.get(_ENV_FILE), os.environ.get(_ENV_DIR)


def _system_store() -> tuple[str | None, str | None]:
    """The interpreter's compiled-in default store, reduced to what is
    actually usable on disk: a non-empty cafile, a capath with entries.
    The framework build reports paths that do not exist → (None, None).
    Compiled paths on purpose, not the env-aware ``cafile``/``capath``
    members: the environment is rule 2 and is handled before this."""
    paths = ssl.get_default_verify_paths()
    cafile = paths.openssl_cafile
    capath = paths.openssl_capath
    try:
        if not (cafile and os.path.isfile(cafile) and os.path.getsize(cafile) > 0):
            cafile = None
    except OSError:
        cafile = None
    try:
        if not (capath and os.path.isdir(capath) and any(os.scandir(capath))):
            capath = None
    except OSError:
        capath = None
    return cafile, capath


def _certifi_bundle() -> str | None:
    try:
        import certifi
    except ImportError:  # a direct dependency, but stay honest if absent
        return None
    return certifi.where()


# --------------------------------------------------------------------- #
# the rule                                                               #
# --------------------------------------------------------------------- #

@functools.lru_cache(maxsize=1)
def _interpreter_defaults() -> tuple[int, int, int, int]:
    """(verify_flags, options, minimum_version, maximum_version) of the
    running interpreter's ``create_default_context()`` — read once from
    a throwaway reference so parity is by construction on every
    version rather than a hard-coded flag list."""
    ref = ssl.create_default_context()
    return (int(ref.verify_flags), int(ref.options),
            int(ref.minimum_version), int(ref.maximum_version))


def _bare() -> ssl.SSLContext:
    """A verifying client context with NO trust anchors and everything
    else exactly as ``create_default_context`` would have set it."""
    verify_flags, options, lo, hi = _interpreter_defaults()
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.verify_mode = ssl.CERT_REQUIRED
    ctx.check_hostname = True
    ctx.options = ssl.Options(options)
    ctx.verify_flags = ssl.VerifyFlags(verify_flags)
    ctx.minimum_version = ssl.TLSVersion(lo)
    ctx.maximum_version = ssl.TLSVersion(hi)
    return ctx


def _ca_count(ctx: ssl.SSLContext) -> int:
    return int(ctx.cert_store_stats().get("x509_ca", 0))


def _load(ctx: ssl.SSLContext, cafile: str | None, capath: str | None) -> bool:
    """Load what is named, each component on its own so a broken file
    does not drop a good directory; False when nothing was loaded."""
    loaded = False
    for kw in ({"cafile": cafile}, {"capath": capath}):
        if not next(iter(kw.values())):
            continue
        try:
            ctx.load_verify_locations(**kw)
        except (OSError, ssl.SSLError):
            continue
        loaded = True
    return loaded


def client_context(cafile: str | None = None) -> ssl.SSLContext:
    """A verifying client context whose trust store is populated from the
    first usable source in the module's order. The chosen source is
    recorded on the context for :func:`describe`."""
    ctx = _bare()
    env = None if cafile else _env_store()
    if cafile:
        _load(ctx, cafile, None)
        source = SOURCE_EXPLICIT
    elif env is not None:
        # OpenSSL semantics: each variable replaces its own component;
        # an absent one keeps the interpreter's default for it.
        sys_file, sys_dir = _system_store()
        env_file = sys_file if env[0] is None else env[0]
        env_dir = sys_dir if env[1] is None else env[1]
        _load(ctx, env_file, env_dir)       # may load nothing: on purpose
        source = SOURCE_ENV
    else:
        sys_file, sys_dir = _system_store()
        loaded = _load(ctx, sys_file, sys_dir)
        # A cafile alone must yield an anchor; a directory is trusted to
        # load lazily. Anything else is the empty-default install.
        if loaded and (sys_dir or _ca_count(ctx) > 0):
            source = SOURCE_SYSTEM
        else:
            ctx = _bare()                   # drop a useless partial load
            source = SOURCE_NONE
            bundle = _certifi_bundle()
            if bundle and _load(ctx, bundle, None) and _ca_count(ctx) > 0:
                source = SOURCE_CERTIFI
    setattr(ctx, _ATTR, source)
    return ctx


def describe(ctx: ssl.SSLContext) -> dict:
    """``{"roots": <CA certificates loaded so far>, "source": <label>}`` —
    what a healthcheck prints and what a certificate failure's remedy is
    computed from. A directory store reads 0 until a handshake loads
    from it; ``source`` still says where trust comes from. A context not
    built here is described by its count alone."""
    source = getattr(ctx, _ATTR, None)
    if source is None:
        source = SOURCE_SYSTEM if _ca_count(ctx) else SOURCE_NONE
    return {"roots": _ca_count(ctx), "source": source}


def empty(ctx: ssl.SSLContext) -> bool:
    """True when a handshake through ``ctx`` cannot possibly verify any
    peer: no anchors loaded and none configured for lazy loading. The
    one case where connecting is pointless and the remedy is local."""
    return describe(ctx)["source"] == SOURCE_NONE


def failure(ctx: ssl.SSLContext, exc: BaseException | None = None) -> str:
    """One line for a verification failure through ``ctx`` — the context
    that actually judged the peer, so a lazily-loaded directory reports
    what it loaded: OpenSSL's own verdict (``verify_message``,
    ``verify_code``), the trust source, and the remedy that follows from
    THAT verdict — a missing issuer, an expired leaf and a wrong name are
    three different fixes, and none of them is "check the VPN"."""
    info = describe(ctx)
    store = f"{info['roots']} roots loaded from {info['source']}"
    if exc is None or info["source"] == SOURCE_NONE:
        return remedy(ctx)
    msg = getattr(exc, "verify_message", None) or str(exc)
    code = getattr(exc, "verify_code", None)
    verdict = f"{msg} (verify code {code})" if code is not None else msg
    if code in (9, 10):                     # not yet valid / expired
        hint = ("the server's certificate is outside its validity "
                "period, or this machine's clock is wrong")
    elif code in (62, 64):                  # host name / IP mismatch
        hint = ("the certificate is trusted but not issued for this "
                "host name — check the configured host")
    elif code in (2, 18, 19, 20, 21):       # issuer not found / self-signed
        if info["source"] == SOURCE_ENV:
            hint = ("the store configured by SSL_CERT_FILE/SSL_CERT_DIR "
                    "has no issuer for this chain — check that bundle, "
                    "or unset the variables to use the defaults")
        else:
            hint = ("no issuer for this chain in the trust store: the "
                    "server sent an incomplete chain, its CA is not "
                    "public, or something on the path re-signs TLS")
    else:
        hint = "verification failed; the message was not sent"
    return f"{verdict}; trust store: {store}; {hint}"


def remedy(ctx: ssl.SSLContext) -> str:
    """The fix-it line when no handshake verdict is available (or when
    the store is empty, where the verdict is a foregone conclusion)."""
    info = describe(ctx)
    if info["source"] == SOURCE_NONE:
        return ("this Python has no root-CA bundle: install certifi into "
                "its environment (pip install certifi), or run "
                "\"Install Certificates.command\" from the python.org "
                "install, or point SSL_CERT_FILE at a PEM bundle")
    if info["source"] == SOURCE_ENV:
        return ("the trust store configured by SSL_CERT_FILE/SSL_CERT_DIR "
                "does not trust the server's chain — check that bundle, "
                "or unset the variables to use the defaults")
    return (f"the trust store ({info['roots']} roots loaded from "
            f"{info['source']}) does not trust the server's chain; the "
            "message was not sent")
