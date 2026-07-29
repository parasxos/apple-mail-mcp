#!/usr/bin/env python3
"""Graph deferred-send feasibility probe (v0.8 M2 gate G0).

Answers ONE question against a real tenant: can a delegated Graph token
create a draft from raw MIME and attach PidTagDeferredSendTime
(singleValueExtendedProperties id "SystemTime 0x3FEF")?  YES means the
v0.8 Graph executor (email_mcp/graph.py) is buildable; NO means S7
closes as documentation.  See docs/graph-probe.md.

Deliberately self-contained: stdlib only, zero imports from email_mcp,
so it can be copied to any machine and run against any tenant.
"""

import argparse
import base64
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

LOGIN = "https://login.microsoftonline.com"
GRAPH = "https://graph.microsoft.com/v1.0"
SCOPES = ("https://graph.microsoft.com/Mail.ReadWrite "
          "https://graph.microsoft.com/Mail.Send offline_access")
DEFERRED_PROP = "SystemTime 0x3FEF"  # PidTagDeferredSendTime (PT_SYSTIME)

_EPILOG = """\
finding a --client-id:
  This probe ships with NO baked-in application id.  It needs a public-
  client app registration whose tenant policy allows the device-code
  flow with delegated Mail.ReadWrite + Mail.Send.  Options, best first:
    1. Register your own: portal.azure.com > App registrations > New,
       "Accounts in this organizational directory", add the two Mail
       delegated permissions, set "Allow public client flows" to Yes,
       and pass its Application (client) ID here.
    2. Ask the tenant admins which client id is approved for device-code
       sign-in.  CERN (like many managed tenants) may only permit
       admin-approved application ids; an arbitrary registration or a
       borrowed well-known id can be refused by Conditional Access.
    3. Well-known Microsoft first-party ids (e.g. the Azure CLI's
       04b07795-8ddb-461a-bbee-02f9e1bf7b46) are pre-consented on many
       tenants and sometimes work — but using them outside their product
       is indistinguishable from token abuse to a security team, and a
       refusal (AADSTS error, passed through verbatim in the verdict)
       does NOT prove the capability is absent.  Last resort only.
"""


class ProbeError(Exception):
    """Fatal probe failure; str(exc) becomes the verdict reason."""


def info(msg):
    print(msg, file=sys.stderr)


def _http(method, url, headers, data=None):
    req = urllib.request.Request(url, data=data, method=method)
    for k, v in headers.items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def _parse(raw):
    try:
        return json.loads(raw) if raw else {}
    except ValueError:
        return {"raw": raw.decode("utf-8", "replace")}


def _form(url, fields):
    data = urllib.parse.urlencode(fields).encode()
    status, raw = _http("POST", url,
                        {"Content-Type": "application/x-www-form-urlencoded"},
                        data)
    return status, _parse(raw)


def _aad_reason(body):
    # error_description carries AADSTS codes; pass them through verbatim.
    return body.get("error_description") or body.get("error") or str(body)


def _graph(method, path, token, body=None, ctype="application/json"):
    headers = {"Authorization": "Bearer " + token}
    data = None
    if body is not None:
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        headers["Content-Type"] = ctype
    status, raw = _http(method, GRAPH + path, headers, data)
    return status, _parse(raw)


def _graph_reason(body):
    err = body.get("error")
    if isinstance(err, dict):
        return "%s: %s" % (err.get("code"), err.get("message"))
    return str(err or body)


def device_login(tenant, client_id):
    status, body = _form("%s/%s/oauth2/v2.0/devicecode" % (LOGIN, tenant),
                         {"client_id": client_id, "scope": SCOPES})
    if status != 200 or "device_code" not in body:
        raise ProbeError("device authorization refused: " + _aad_reason(body))
    info(body.get("message") or "Visit %s and enter code %s"
         % (body.get("verification_uri"), body.get("user_code")))
    interval = int(body.get("interval", 5))
    deadline = time.time() + int(body.get("expires_in", 900))
    while time.time() < deadline:
        time.sleep(interval)
        status, tok = _form("%s/%s/oauth2/v2.0/token" % (LOGIN, tenant),
                            {"grant_type":
                             "urn:ietf:params:oauth:grant-type:device_code",
                             "client_id": client_id,
                             "device_code": body["device_code"]})
        if status == 200:
            return tok
        err = tok.get("error", "")
        if err == "authorization_pending":
            continue
        if err == "slow_down":
            interval += 5
            continue
        raise ProbeError("token request failed: " + _aad_reason(tok))
    raise ProbeError("device code expired before sign-in completed")


def _token_upn(access_token):
    try:
        payload = access_token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(
            payload + "=" * (-len(payload) % 4)))
        return (claims.get("upn") or claims.get("preferred_username")
                or claims.get("unique_name") or "")
    except Exception:
        return ""


def _rfc822(to_addr):
    lines = ["Subject: email-mcp graph probe (deferred-send feasibility)"]
    if to_addr:
        lines.append("To: " + to_addr)
    lines += ["MIME-Version: 1.0",
              "Content-Type: text/plain; charset=utf-8", "",
              "Draft created by tools/graph_probe.py; safe to delete.", ""]
    return "\r\n".join(lines).encode()


def _norm(stamp):
    return stamp.strip().rstrip("Z").split(".")[0]


def _probe(args, verdict):
    tok = device_login(args.tenant, args.client_id)
    token = tok["access_token"]
    verdict["scopes_granted"] = tok.get("scope", "")
    to_addr = _token_upn(token)
    if args.live and not to_addr:
        status, me = _graph("GET", "/me", token)
        to_addr = me.get("mail") or me.get("userPrincipalName") or ""
        if not to_addr:
            raise ProbeError("--live needs your own address for To:, but the "
                             "token carries no upn and GET /me failed "
                             "(HTTP %s): %s" % (status, _graph_reason(me)))
    # MIME-create path: raw RFC-822, base64, Content-Type text/plain.
    status, draft = _graph("POST", "/me/messages", token,
                           base64.b64encode(_rfc822(to_addr)), "text/plain")
    if status != 201 or "id" not in draft:
        verdict["reason"] = ("MIME draft create failed (HTTP %s): %s"
                             % (status, _graph_reason(draft)))
        return
    verdict["mime_create"] = True
    draft_id = draft["id"]

    def cleanup():
        st, body = _graph("DELETE", "/me/messages/" + draft_id, token)
        if st != 204:
            info("warning: probe draft not deleted (HTTP %s): %s — remove it "
                 "from Drafts by hand" % (st, _graph_reason(body)))

    when = time.strftime("%Y-%m-%dT%H:%M:%SZ",
                         time.gmtime(time.time() + (180 if args.live else 3600)))
    status, body = _graph("PATCH", "/me/messages/" + draft_id, token,
                          {"singleValueExtendedProperties":
                           [{"id": DEFERRED_PROP, "value": when}]})
    if status != 200:
        verdict["reason"] = ("PATCH %s failed (HTTP %s): %s"
                             % (DEFERRED_PROP, status, _graph_reason(body)))
        cleanup()
        return
    expand = urllib.parse.quote("singleValueExtendedProperties($filter=id eq "
                                "'%s')" % DEFERRED_PROP, safe="()'$=")
    status, got = _graph("GET", "/me/messages/%s?$expand=%s"
                         % (draft_id, expand), token)
    echoed = next((p.get("value", "") for p in
                   got.get("singleValueExtendedProperties") or []
                   if p.get("id", "").lower() == DEFERRED_PROP.lower()), "")
    if status != 200 or not echoed:
        verdict["reason"] = ("extended property did not round-trip "
                             "(HTTP %s): %s" % (status, _graph_reason(got)))
        cleanup()
        return
    if _norm(echoed) != _norm(when):
        verdict["reason"] = ("round-trip mismatch: sent %s, read back %s"
                             % (when, echoed))
        cleanup()
        return
    verdict["extended_prop_roundtrip"] = True

    if not args.live:
        cleanup()
        return
    status, body = _graph("POST", "/me/messages/%s/send" % draft_id, token)
    if status != 202:
        verdict["reason"] = ("deferred /send rejected (HTTP %s): %s"
                             % (status, _graph_reason(body)))
        cleanup()
        return
    verdict["deferred_send_honored"] = "awaiting human confirmation"
    info("Deferred send accepted. Exchange should deliver to %s at %s "
         "(about 3 minutes from now)." % (to_addr, when))
    info("You may close the laptop lid NOW — delivery is Exchange's job, "
         "not this machine's.")
    info("If the message arrives on time with the lid closed, deferred send "
         "is honored: record the probe as fully green.")


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="graph_probe.py",
        description="Probe a Microsoft 365 tenant for Graph deferred-send "
                    "support (device-code login, MIME draft, "
                    "PidTagDeferredSendTime round-trip). Prints a JSON "
                    "verdict; exit 0 = YES, 1 = NO.",
        epilog=_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--tenant", default="organizations",
                        help="tenant domain or GUID, e.g. cern.ch "
                             "(default: organizations)")
    parser.add_argument("--client-id", required=True,
                        help="public-client application id for the "
                             "device-code flow — see epilog below on how to "
                             "obtain one (none is baked in)")
    parser.add_argument("--live", action="store_true",
                        help="Phase B: really schedule a self-addressed "
                             "message ~3 min out and /send it, so a human "
                             "can confirm Exchange delivers with the lid "
                             "closed (default Phase A never sends)")
    parser.add_argument("--json", action="store_true",
                        help="compact machine-readable verdict on stdout")
    args = parser.parse_args(argv)

    verdict = {"probe": "NO", "tenant": args.tenant,
               "client_id": args.client_id, "scopes_granted": "",
               "mime_create": False, "extended_prop_roundtrip": False,
               "deferred_send_honored": "untested", "reason": ""}
    try:
        _probe(args, verdict)
    except ProbeError as e:
        verdict["reason"] = str(e)
    except urllib.error.URLError as e:
        verdict["reason"] = "network error: %s" % getattr(e, "reason", e)
    if verdict["mime_create"] and verdict["extended_prop_roundtrip"]:
        verdict["probe"] = "YES"
    print(json.dumps(verdict) if args.json else json.dumps(verdict, indent=2))
    return 0 if verdict["probe"] == "YES" else 1


if __name__ == "__main__":
    sys.exit(main())
