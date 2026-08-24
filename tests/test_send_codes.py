"""§3.4 send codes on the wire (docs/v1-contract.md — frozen on paper at
v0.10, wired at v0.11): every send/reply/schedule failure carries its
assigned code; cancel_scheduled's prose-only failure sites gain §3 codes;
refresh_mail gains the §3.3 mapped string code."""
from __future__ import annotations

import subprocess
from unittest.mock import patch

import pytest

from email_mcp import bootstrap, codes, sender, server, spool
from email_mcp.adapters import refresh as refresh_adapter


@pytest.fixture
def send_env(monkeypatch, tmp_path):
    """A synthesized default identity: paris@example.org, allowlist=self.
    EMAIL_MCP_IDENTITIES points at a non-existent file so the developer's
    real ~/.email-mcp/identities.toml can never leak into the suite."""
    monkeypatch.setenv("EMAIL_MCP_IDENTITIES", str(tmp_path / "no.toml"))
    monkeypatch.setenv("EMAIL_MCP_FROM_ADDR", "paris@example.org")
    monkeypatch.delenv("EMAIL_MCP_SEND_ALLOW_ALL", raising=False)


def _send(**kw) -> dict:
    args = dict(to="paris@example.org", subject="s", body="b")
    args.update(kw)
    return server.tool_send_email(**args)


# --------------------------------------------------------------------- #
# send_email / schedule_email — validation and compose sites            #
# --------------------------------------------------------------------- #


def test_header_injection(send_env):
    out = _send(subject="hi\r\nBcc: mole@example.org")
    assert out["ok"] is False
    assert out["code"] == "header_injection"


def test_invalid_recipient(send_env):
    assert _send(to="not-an-address")["code"] == "invalid_recipient"
    assert _send(cc="a@b\nc@d")["code"] == "invalid_recipient"


def test_required_fields_are_invalid_input(send_env):
    assert _send(to="")["code"] == "invalid_input"
    assert _send(subject="")["code"] == "invalid_input"
    assert _send(body=" ")["code"] == "invalid_input"


def test_recipient_not_allowed(send_env, monkeypatch):
    monkeypatch.setenv("EMAIL_MCP_SEND_ALLOW_ALL", "0")  # guard DECLARED
    out = _send(to="stranger@example.org")
    assert out["code"] == "recipient_not_allowed"
    assert "allowlist" in out["error"]


def test_attachment_codes(send_env, tmp_path):
    missing = tmp_path / "nope.txt"
    assert _send(attachments=[str(missing)])["code"] == "attachment_not_found"
    assert _send(attachments=[str(tmp_path)])["code"] == "attachment_unreadable"


def test_attachments_too_large(send_env, tmp_path, monkeypatch):
    monkeypatch.setenv("EMAIL_MCP_MAX_ATTACH_MB", "0")
    f = tmp_path / "big.bin"
    f.write_bytes(b"x" * 1024)
    assert _send(attachments=[str(f)])["code"] == "attachments_too_large"


def test_schedule_send_at_codes(send_env):
    out = server.tool_schedule_email(to="paris@example.org", subject="s",
                                     body="b", send_at="whenever")
    assert out["code"] == "invalid_send_at"
    out = server.tool_schedule_email(to="paris@example.org", subject="s",
                                     body="b",
                                     send_at="2020-01-01T00:00:00+00:00")
    assert out["code"] == "send_at_in_past"


def test_unknown_identity(send_env):
    out = _send(from_identity="ghost")
    assert out["code"] == "unknown_identity"


def test_no_identity_at_all_is_identity_misconfigured(monkeypatch, tmp_path):
    monkeypatch.setenv("EMAIL_MCP_IDENTITIES", str(tmp_path / "no.toml"))
    monkeypatch.delenv("EMAIL_MCP_FROM_ADDR", raising=False)
    assert _send()["code"] == "identity_misconfigured"


# --------------------------------------------------------------------- #
# transport sites                                                       #
# --------------------------------------------------------------------- #


def test_transport_unavailable_on_failed_preflight(send_env, monkeypatch):
    monkeypatch.setattr(sender, "_socket_alive", lambda: False)
    monkeypatch.setattr(sender, "_bootstrap_master", lambda: False)
    out = _send()
    assert out["code"] == "transport_unavailable"
    assert "transport unavailable" in out["error"]


def test_smtp_default_reports_dns_truth_not_ssh_session(monkeypatch, tmp_path):
    """A DEFAULT smtp identity whose host does not resolve must fail in
    smtp's own vocabulary. The seam flow used to be gated on being the
    default rather than on being session-shaped, so this exact case
    reported "session dead ... (2FA)" — ssh prose for a DNS failure —
    and sent the first user's debugging down the wrong path (2026-08-01:
    smtp.cern.ch is split-horizon DNS, unresolvable off-site)."""
    import socket as socket_mod

    from email_mcp.transports import smtp as smtp_mod

    ident = tmp_path / "identities.toml"
    ident.write_text(
        'default = "main"\n\n[main]\n'
        'from_addr = "camilla@example.org"\n'
        'driver = "smtp"\nhost = "smtp.cern.example"\nport = 587\n'
        'username = "cv"\nkeychain = "item"\n'
    )
    monkeypatch.setenv("EMAIL_MCP_IDENTITIES", str(ident))
    monkeypatch.setattr(smtp_mod, "_read_keychain",
                        lambda item, account: "pw")

    def _no_dns(addr, timeout=None):
        raise socket_mod.gaierror(
            8, "nodename nor servname provided, or not known")

    monkeypatch.setattr(smtp_mod.socket, "create_connection", _no_dns)
    out = server.tool_send_email(
        to="camilla@example.org", subject="s", body="b")
    assert out["ok"] is False
    assert out["code"] == "transport_unavailable"
    assert "smtp.cern.example:587" in out["error"]
    assert "nodename" in out["error"]          # the DNS truth, verbatim
    assert "2FA" not in out["error"]           # ssh vocabulary stays home
    assert "session" not in out["error"]


def test_delivery_failure_default_code(send_env, monkeypatch):
    monkeypatch.setattr(sender, "_socket_alive", lambda: True)

    def _boom(raw):
        raise sender.SendError("smtp exploded")

    monkeypatch.setattr(sender, "_deliver_bytes", _boom)
    assert _send()["code"] == "delivery_failed"


def test_smtp_secret_errors_carry_code_and_lane_prefix(monkeypatch):
    """The known §3.4 gap, closed: smtp's formerly UNPREFIXED secret
    errors are credentials_unavailable AND name their [identity/smtp] lane."""
    from email_mcp.transports import smtp as smtp_mod

    t = smtp_mod.SmtpTransport(host="mail.example.org", keychain="item",
                               identity="work", from_addr="w@example.org")

    def _no_cli(*a, **kw):
        raise FileNotFoundError("security")

    monkeypatch.setattr(smtp_mod.subprocess, "run", _no_cli)
    with pytest.raises(smtp_mod.SendError) as ei:
        t._secret()
    assert ei.value.code == codes.CREDENTIALS_UNAVAILABLE
    assert str(ei.value).startswith("[work/smtp]")


def test_smtp_auth_and_delivery_codes(monkeypatch):
    import smtplib

    from email_mcp.transports import smtp as smtp_mod

    t = smtp_mod.SmtpTransport(host="mail.example.org", keychain="item",
                               identity="work", from_addr="w@example.org")
    monkeypatch.setattr(smtp_mod, "_read_keychain", lambda item, account: "pw")

    class _Auth:
        def login(self, u, p):
            raise smtplib.SMTPAuthenticationError(535, b"bad password")

        def quit(self):
            pass

    monkeypatch.setattr(t, "_connect", lambda timeout: _Auth())
    with pytest.raises(smtp_mod.SendError) as ei:
        t.deliver(b"From: w@example.org\r\n\r\nhi", "w@example.org",
                  ["a@example.org"])
    assert ei.value.code == codes.AUTH_FAILED

    class _Refuse:
        def login(self, u, p):
            pass

        def sendmail(self, f, r, raw):
            raise smtplib.SMTPException("relay denied")

        def quit(self):
            pass

    monkeypatch.setattr(t, "_connect", lambda timeout: _Refuse())
    with pytest.raises(smtp_mod.SendError) as ei:
        t.deliver(b"From: w@example.org\r\n\r\nhi", "w@example.org",
                  ["a@example.org"])
    assert ei.value.code == codes.DELIVERY_FAILED


def test_misconfigured_transport_params(send_env):
    from email_mcp import identities, transports

    ident = identities.Identity(name="bad", from_addr="b@example.org",
                                driver="teleport")
    with pytest.raises(transports.SendError) as ei:
        transports.get_transport(ident)
    assert ei.value.code == codes.IDENTITY_MISCONFIGURED


# --------------------------------------------------------------------- #
# cancel_scheduled — the seven prose-only sites gain §3 codes           #
# --------------------------------------------------------------------- #


def test_cancel_unknown_id_is_not_found(send_env):
    out = server.tool_cancel_scheduled("nope-123")
    assert out["ok"] is False and out["code"] == "not_found"
    assert "operation_id" not in out  # nothing was ever minted for it


def test_cancel_not_pending_is_invalid_input_with_minted_id(send_env):
    entry = sender.schedule_email(to="paris@example.org", subject="s",
                                  body="b", send_at="2999-01-01T00:00:00+00:00")
    assert spool.claim(entry.id)  # a dispatcher grabbed it → sending/
    out = server.tool_cancel_scheduled(entry.id)
    assert out["ok"] is False and out["code"] == "invalid_input"
    assert out["operation_id"] == entry.id  # the spool id was minted


# --------------------------------------------------------------------- #
# refresh_mail — §3.3 string code beside the numeric one                #
# --------------------------------------------------------------------- #


def _osa(returncode: int, stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=["osascript"],
                                       returncode=returncode,
                                       stdout="", stderr=stderr)


def test_refresh_mail_maps_error_code_to_string_code(monkeypatch):
    monkeypatch.setattr(
        bootstrap, "_application", bootstrap.build_application(source=object())
    )  # no freshness snapshot
    stderr = "execution error: Not authorized to send Apple events. (-1743)"
    with patch.object(refresh_adapter.subprocess, "run",
                      return_value=_osa(1, stderr)):
        out = server.tool_refresh_mail(wait_seconds=0, timeout_seconds=1)
    assert out["ok"] is False
    assert out["error_code"] == -1743
    assert out["code"] == "automation_denied"

    with patch.object(refresh_adapter.subprocess, "run", return_value=_osa(0)):
        out = server.tool_refresh_mail(wait_seconds=0, timeout_seconds=1)
    assert out["ok"] is True
    assert out["error_code"] is None and out["code"] is None


def test_list_scheduled_unknown_state_is_coded(send_env):
    out = server.tool_list_scheduled(state="bogus")
    assert out["ok"] is False and out["code"] == "invalid_input"
