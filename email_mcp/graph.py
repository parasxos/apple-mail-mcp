"""Microsoft Graph deferred-send executor ("durable schedules").

For identities with `executor = "graph"`, schedule_email hands the SAME
frozen .eml bytes to Exchange as a draft carrying PidTagDeferredSendTime
(`singleValueExtendedProperties` id "SystemTime 0x3FEF") and /send-s it:
Exchange transmits at the deferred time — lid closed, Mac off, irrelevant.
The launchd spool remains the universal executor and the silent fallback
whenever Graph refuses. See docs/graph-probe.md for the tenant gate that
proved this path (probe: tools/graph_probe.py).

Wire discipline: EVERY HTTP request in this module goes through the single
module-level seam `_http(method, url, ident, body=None, headers=None)` —
tests monkeypatch exactly that function; nothing else touches urllib.
Throttling (HTTP 429) is honored above the seam: one bounded wait of at
most _RETRY_MAX_WAIT seconds, then a GraphError defer-signal — never a
busy-loop.

Auth: delegated device-code flow (Mail.ReadWrite + Mail.Send +
offline_access), stdlib only — no msal. Token caches live at
`<state root>/graph/<identity>.token.json`, 0600 inside a 0700 dir,
rewritten atomically (tmp + rename). Access tokens refresh silently via
the cached refresh_token; when refresh fails the GraphError names the fix:

    python -m email_mcp.graph --login <name>

CLI:  python -m email_mcp.graph --login NAME    interactive device login
      python -m email_mcp.graph --status NAME   local cache report, no network
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import ssl
import urllib.request
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

from . import codes, config, state
from .graph_mail import GraphMailbox
from .log import get_logger
from .transports import SendError

_log = get_logger()

LOGIN = "https://login.microsoftonline.com"
GRAPH = "https://graph.microsoft.com/v1.0"
SCOPES = ("https://graph.microsoft.com/Mail.ReadWrite "
          "https://graph.microsoft.com/Mail.Send offline_access")
DEFERRED_PROP = "SystemTime 0x3FEF"  # PidTagDeferredSendTime (PT_SYSTIME)

_TIMEOUT = 30           # seconds, every request
_RETRY_DEFAULT = 5      # assumed Retry-After when the header is absent/odd
_RETRY_MAX_WAIT = 15    # longest single in-process wait on a 429
_TOKEN_SKEW = 60        # refresh this many seconds before expiry


class GraphError(SendError):
    """A Graph-side failure (auth, throttle, transport, API refusal).

    Subclasses SendError so every existing `except SendError` handler
    catches it for free; schedule-time callers treat it as the signal to
    fall back to the launchd executor, reconcile-time callers as "leave
    the entry alone and retry next pass".

    One code for the whole class: the Exchange lane was unavailable or
    refused. Graph errors never reach the send/reply/schedule wire (the
    fallback swallows them); they surface only through cancel_scheduled.
    """

    code = codes.TRANSPORT_UNAVAILABLE


class GraphTransportError(GraphError):
    """The wire itself failed (DNS, refused, timeout, reset) — the request
    MAY have been processed server-side before the connection died.
    Distinguished from clean HTTP refusals so `create_deferred_draft` can
    treat an ambiguous /send as possibly-armed instead of falling back to
    launchd into a double-send."""


def _name(ident) -> str:
    return getattr(ident, "name", str(ident))


# --------------------------------------------------------------------- #
# the wire seam                                                          #
# --------------------------------------------------------------------- #


def _ssl_context():
    """TLS context that works everywhere the venv runs (launchd included):
    python.org framework builds ship without a root-CA bundle wired, so
    prefer certifi's when importable; fall back to system defaults."""
    global _SSL_CTX
    if _SSL_CTX is None:
        cafile = None
        try:
            import certifi
            cafile = certifi.where()
        except ImportError:
            pass
        _SSL_CTX = ssl.create_default_context(cafile=cafile)
    return _SSL_CTX


_SSL_CTX = None


def _http(method: str, url: str, ident, body: bytes | None = None,
          headers: dict | None = None) -> tuple[int, dict]:
    """THE single HTTP seam — every request in this module lands here, and
    tests monkeypatch exactly this function.

    Returns (status, parsed-JSON dict); HTTP error statuses are returned,
    not raised. On 429 the dict gains a "retry_after" key (int seconds,
    parsed from the Retry-After header) for the throttle handling in
    `_request`. Transport-level failures (DNS, refused, timeout) raise
    GraphError.
    """
    req = urllib.request.Request(url, data=body, method=method)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT,
                                    context=_ssl_context()) as resp:
            return resp.status, _parse(resp.read())
    except urllib.error.HTTPError as e:
        data = _parse(e.read())
        if e.code == 429:
            data.setdefault(
                "retry_after", _retry_after_seconds(e.headers.get("Retry-After"))
            )
        return e.code, data
    except OSError as e:  # URLError, socket.timeout, connection reset …
        reason = getattr(e, "reason", e)
        raise GraphTransportError(
            f"[{_name(ident)}/graph] network error reaching "
            f"{urllib.parse.urlsplit(url).netloc}: {reason}"
        ) from e


def _parse(raw: bytes) -> dict:
    try:
        out = json.loads(raw) if raw else {}
        return out if isinstance(out, dict) else {"value_raw": out}
    except ValueError:
        return {"raw": raw.decode("utf-8", "replace")}


def _retry_after_seconds(value) -> int:
    """Retry-After header → seconds (delta-seconds or HTTP-date form)."""
    if value is None:
        return _RETRY_DEFAULT
    try:
        return max(0, int(str(value).strip()))
    except ValueError:
        pass
    try:
        dt = parsedate_to_datetime(str(value))
        return max(0, int(dt.timestamp() - time.time()))
    except (TypeError, ValueError):
        return _RETRY_DEFAULT


def _request(method: str, url: str, ident, body: bytes | None = None,
             headers: dict | None = None) -> tuple[int, dict]:
    """`_http` plus throttle policy: on 429 honor Retry-After with ONE
    bounded wait (≤ _RETRY_MAX_WAIT s) and a single retry; anything more
    (long Retry-After, second 429) raises the GraphError defer-signal so
    callers pick the work up on their next pass — never a busy-loop."""
    status, data = _http(method, url, ident, body=body, headers=headers)
    if status != 429:
        return status, data
    try:
        wait = int(data.get("retry_after", _RETRY_DEFAULT))
    except (TypeError, ValueError):
        wait = _RETRY_DEFAULT
    if 0 <= wait <= _RETRY_MAX_WAIT:
        _log.info(
            "graph: throttled, honoring Retry-After %ss (one retry) [%s]",
            wait, _name(ident),
        )
        time.sleep(wait)
        status, data = _http(method, url, ident, body=body, headers=headers)
        if status != 429:
            return status, data
    raise GraphError(
        f"[{_name(ident)}/graph] throttled by Graph (HTTP 429, "
        f"Retry-After {wait}s) — deferring to the next pass"
    )


def _form(url: str, fields: dict, ident) -> tuple[int, dict]:
    """POST application/x-www-form-urlencoded (AAD endpoints, no auth)."""
    return _request(
        "POST", url, ident,
        body=urllib.parse.urlencode(fields).encode(),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )


def _graph(method: str, path: str, ident, body=None,
           ctype: str = "application/json") -> tuple[int, dict]:
    """Authenticated Graph call: bytes bodies pass through verbatim with
    `ctype` (the MIME-create path), dicts are JSON-encoded."""
    headers = {"Authorization": "Bearer " + _token(ident)}
    data = None
    if body is not None:
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        headers["Content-Type"] = ctype
    return _request(method, GRAPH + path, ident, body=data, headers=headers)


def _aad_reason(body: dict) -> str:
    # error_description carries AADSTS codes; pass them through verbatim.
    return str(body.get("error_description") or body.get("error") or body)


def _graph_reason(body: dict) -> str:
    err = body.get("error")
    if isinstance(err, dict):
        return f"{err.get('code')}: {err.get('message')}"
    return str(err or body)


# --------------------------------------------------------------------- #
# token cache (<state root>/graph/<name>.token.json, 0600 in 0700)       #
# --------------------------------------------------------------------- #


def _token_path(ident) -> Path:
    try:
        d = state.State.resolve().adopt().graph
    except OSError as e:  # RefusedError included
        # An unadoptable state tree or broken graph leaf must surface as a
        # GraphError: schedule-time callers then fall back to launchd
        # instead of leaking a raw OSError traceback through the tool.
        raise GraphError(
            f"[{_name(ident)}/graph] cannot create/open the graph state "
            f"dir: {e}"
        ) from e
    return d / f"{_name(ident)}.token.json"


def _write_cache(path: Path, data: dict) -> None:
    """Atomic 0600 rewrite: create the temp file already-restrictive (no
    0644 window), then rename over the live cache."""
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    os.replace(tmp, path)
    path.chmod(0o600)


def _cache_dict(tok: dict, fallback_refresh: str = "",
                bound_to: str = "") -> dict:
    now = time.time()
    return {
        "access_token": tok.get("access_token", ""),
        # AAD may rotate the refresh token on every refresh; keep the old
        # one only when the response carries none.
        "refresh_token": tok.get("refresh_token") or fallback_refresh,
        "expires_at": now + float(tok.get("expires_in") or 0),
        "scope": tok.get("scope", ""),
        "obtained_at": now,
        # The mailbox this token was PROVEN to be (GET /me at login).
        # A cache without a binding is refused by _token — the wrong-
        # account draft/send is the failure this field exists to prevent.
        "bound_to": bound_to,
    }


def _bare_addr(addr: str) -> str:
    """lowercased bare address ("Name <a@b>" → "a@b")."""
    return str(addr or "").split("<")[-1].strip(" >").lower()


def _signed_in_addr(ident, access_token: str) -> str:
    """Who Graph says this token IS: GET /me, preferring `mail` (the
    mailbox's SMTP address) over userPrincipalName."""
    status, me = _request(
        "GET", f"{GRAPH}/me", ident,
        headers={"Authorization": f"Bearer {access_token}"},
    )
    if status != 200:
        raise GraphError(
            f"[{_name(ident)}/graph] cannot verify the signed-in mailbox "
            f"(GET /me → HTTP {status}): {_graph_reason(me)}"
        )
    return _bare_addr(str(me.get("mail") or me.get("userPrincipalName") or ""))


def _graph_cfg(ident) -> tuple[str, str]:
    g = getattr(ident, "graph", None) or {}
    tenant = str(g.get("tenant", "")).strip()
    client_id = str(g.get("client_id", "")).strip()
    if not tenant or not client_id:
        raise GraphError(
            f"[{_name(ident)}/graph] identity has no usable graph config — "
            f"set tenant and client_id in [{_name(ident)}.graph] in "
            f"{config.identities_file()} (see docs/graph-probe.md)."
        )
    return tenant, client_id


def _token(ident) -> str:
    """Access token for `ident`, from cache or via silent refresh.

    Raises GraphError naming the interactive remedy when there is no
    cache, the cache is unreadable, or the refresh is refused — schedule
    callers fall back to launchd, reconcile callers record last_error.
    """
    path = _token_path(ident)
    fix = f"run: python -m email_mcp.graph --login {_name(ident)}"
    try:
        cache = json.loads(path.read_bytes())
    except FileNotFoundError:
        raise GraphError(
            f"[{_name(ident)}/graph] no token cache at {path} — {fix}"
        ) from None
    except (ValueError, OSError) as e:
        raise GraphError(
            f"[{_name(ident)}/graph] unreadable token cache {path}: {e} — {fix}"
        ) from e

    # The binding gate, at the ONE place every operation gets its token:
    # a cache must carry the mailbox it was proven to be at login, and it
    # must be the mailbox this identity claims to send as. Pure (no
    # network): the proof happened at login; this only refuses drift —
    # a pre-binding cache, or an identities.toml edited to a different
    # from_addr over a token for the old mailbox.
    bound = _bare_addr(cache.get("bound_to", ""))
    expected = _bare_addr(ident.from_addr)
    if not bound:
        raise GraphError(
            f"[{_name(ident)}/graph] token cache carries no mailbox "
            f"binding (pre-2026-08 login) — {fix}"
        )
    if bound != expected:
        raise GraphError(
            f"[{_name(ident)}/graph] token cache is bound to {bound!r} "
            f"but this identity sends as {expected!r} — refusing. {fix}"
        )

    try:
        expires_at = float(cache.get("expires_at") or 0)
    except (TypeError, ValueError):
        expires_at = 0.0
    if cache.get("access_token") and expires_at > time.time() + _TOKEN_SKEW:
        return cache["access_token"]

    refresh = cache.get("refresh_token")
    if not refresh:
        raise GraphError(
            f"[{_name(ident)}/graph] token cache has no refresh_token — {fix}"
        )
    tenant, client_id = _graph_cfg(ident)
    status, tok = _form(
        f"{LOGIN}/{tenant}/oauth2/v2.0/token",
        {
            "grant_type": "refresh_token",
            "client_id": client_id,
            "refresh_token": refresh,
            "scope": SCOPES,
        },
        ident,
    )
    if status != 200 or "access_token" not in tok:
        raise GraphError(
            f"[{_name(ident)}/graph] token refresh failed: "
            f"{_aad_reason(tok)} — {fix}"
        )
    _write_cache(path, _cache_dict(tok, fallback_refresh=refresh,
                                   bound_to=bound))  # binding survives refresh
    _log.info("graph: refreshed access token for identity %r", _name(ident))
    return tok["access_token"]


# --------------------------------------------------------------------- #
# device-code login (interactive; the ONLY place a human is needed)      #
# --------------------------------------------------------------------- #


def device_login(ident) -> Path:
    """Interactive device-code sign-in for `ident`; returns the cache path.

    Prints the verification URL + user code (stderr — stdout stays clean
    for CLI JSON), then polls the token endpoint honoring the advertised
    interval and any slow_down until the browser sign-in completes.
    """
    tenant, client_id = _graph_cfg(ident)
    status, body = _form(
        f"{LOGIN}/{tenant}/oauth2/v2.0/devicecode",
        {"client_id": client_id, "scope": SCOPES},
        ident,
    )
    if status != 200 or "device_code" not in body:
        raise GraphError(
            f"[{_name(ident)}/graph] device authorization refused: "
            f"{_aad_reason(body)}"
        )
    print(
        body.get("message")
        or "Visit %s and enter code %s"
        % (body.get("verification_uri"), body.get("user_code")),
        file=sys.stderr,
    )
    interval = int(body.get("interval") or 5)
    deadline = time.time() + int(body.get("expires_in") or 900)
    token_url = f"{LOGIN}/{tenant}/oauth2/v2.0/token"
    while time.time() < deadline:
        time.sleep(interval)
        status, tok = _form(
            token_url,
            {
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "client_id": client_id,
                "device_code": body["device_code"],
            },
            ident,
        )
        if status == 200 and "access_token" in tok:
            # Bind BEFORE the cache exists: the human who can fix a
            # wrong-mailbox sign-in is present exactly now. A token for
            # somebody else's mailbox never becomes a cache at all —
            # the wrong-account draft/schedule is unrepresentable
            # downstream because _token refuses unbound caches.
            signed_in = _signed_in_addr(ident, tok["access_token"])
            expected = _bare_addr(ident.from_addr)
            if signed_in != expected:
                raise GraphError(
                    f"[{_name(ident)}/graph] you signed in as "
                    f"{signed_in!r} but this identity sends as "
                    f"{expected!r} — refusing to cache the token. Rerun "
                    f"the login with the right Microsoft account."
                )
            path = _token_path(ident)
            _write_cache(path, _cache_dict(tok, bound_to=signed_in))
            _log.info("graph: device login complete for identity %r "
                      "(bound to %s)", _name(ident), signed_in)
            return path
        err = tok.get("error", "")
        if err == "authorization_pending":
            continue
        if err == "slow_down":
            interval += 5
            continue
        raise GraphError(
            f"[{_name(ident)}/graph] device login failed: {_aad_reason(tok)}"
        )
    raise GraphError(
        f"[{_name(ident)}/graph] device code expired before sign-in "
        f"completed — rerun: python -m email_mcp.graph --login {_name(ident)}"
    )


# --------------------------------------------------------------------- #
# executor operations                                                    #
# --------------------------------------------------------------------- #


def _mailbox() -> GraphMailbox:
    """Bind mailbox semantics to the current, monkeypatchable Graph client."""
    return GraphMailbox(
        _graph, GraphError, GraphTransportError, _log, DEFERRED_PROP,
    )


def _iso_utc(when: datetime) -> str:
    return _mailbox()._iso_utc(when)


def create_mime_draft(ident, raw: bytes) -> str:
    return _mailbox().create_mime_draft(ident, raw)


def draft_receipt(ident, draft_id: str) -> dict:
    return _mailbox().draft_receipt(ident, draft_id)


def create_deferred_draft(ident, raw: bytes, when: datetime) -> str:
    return _mailbox().create_deferred_draft(ident, raw, when)


def _body_payload(body: dict) -> dict:
    return _mailbox()._body_payload(body)


def fetch_body_by_message_id(ident, message_id: str) -> dict | None:
    return _mailbox().fetch_body_by_message_id(ident, message_id)


def translate_ews_ids(ident, ews_ids: list[str]) -> dict[str, str]:
    return _mailbox().translate_ews_ids(ident, ews_ids)


def fetch_body_by_graph_id(ident, rest_id: str) -> dict | None:
    return _mailbox().fetch_body_by_graph_id(ident, rest_id)


def _find_by_message_id(
    ident,
    folder: str,
    message_id: str,
) -> str | None:
    return _mailbox()._find_by_message_id(ident, folder, message_id)


def find_draft_by_message_id(ident, message_id: str) -> str | None:
    return _mailbox().find_draft_by_message_id(ident, message_id)


def sent_by_message_id(ident, message_id: str) -> bool:
    return _mailbox().sent_by_message_id(ident, message_id)


def draft_status(ident, draft_id: str, message_id: str) -> str:
    return _mailbox().draft_status(ident, draft_id, message_id)


def delete_draft(ident, draft_id: str) -> str:
    return _mailbox().delete_draft(ident, draft_id)


# --------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------- #


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m email_mcp.graph",
        description="Graph executor auth: interactive device-code login "
                    "and local token-cache status for one identity.",
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--login", metavar="NAME",
                       help="device-code sign-in for identity NAME; writes "
                            "the token cache (0600) under "
                            "~/.email-mcp/graph/")
    group.add_argument("--status", metavar="NAME",
                       help="report identity NAME's token cache — purely "
                            "local, no network")
    args = parser.parse_args(argv)
    name = args.login or args.status

    from . import identities  # late: CLI-only dependency
    try:
        ident = identities.get(name)
    except SendError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    if args.login:
        try:
            path = device_login(ident)
        except SendError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        print(f"logged in: token cache written to {path}")
        return 0

    # --status: report the cache without touching the network.
    path = _token_path(ident)
    info = {
        "identity": ident.name,
        "executor": getattr(ident, "executor", "launchd"),
        "token_cache": str(path),
        "has_refresh_token": False,
        "access_token_valid": False,
        "expires_at": None,
    }
    try:
        cache = json.loads(path.read_bytes())
    except (FileNotFoundError, ValueError, OSError):
        cache = None
    if cache:
        try:
            expires_at = float(cache.get("expires_at") or 0)
        except (TypeError, ValueError):
            expires_at = 0.0
        info["has_refresh_token"] = bool(cache.get("refresh_token"))
        info["access_token_valid"] = (
            bool(cache.get("access_token")) and expires_at > time.time()
        )
        if expires_at:
            info["expires_at"] = datetime.fromtimestamp(
                expires_at, tz=timezone.utc
            ).isoformat(timespec="seconds")
    print(json.dumps(info, indent=2))
    return 0 if info["has_refresh_token"] else 1


if __name__ == "__main__":
    sys.exit(main())
