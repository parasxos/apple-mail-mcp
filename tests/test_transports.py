"""Transport driver tests — registry resolution, ssh argv/timeout/ensure,
smtp Bcc-strip/TLS/auth/Keychain, pipe stdin. Every subprocess and smtplib
call is monkeypatched; nothing leaves the machine."""
from __future__ import annotations

import os
import smtplib
import subprocess

import pytest

from email_mcp import transports
from email_mcp.identities import Identity
from email_mcp.transports import SendError
from email_mcp.transports import pipe as pipe_mod
from email_mcp.transports import smtp as smtp_mod
from email_mcp.transports import ssh_sendmail as ssh_mod
from email_mcp.transports.pipe import PipeTransport
from email_mcp.transports.smtp import SmtpTransport
from email_mcp.transports.ssh_sendmail import SshSendmailTransport


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch, tmp_path):
    """Documented defaults, and the identities file pointed at a nonexistent
    path so a production ~/.email-mcp/identities.toml can never leak in."""
    for k in list(os.environ):
        if k.startswith("EMAIL_MCP_"):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("EMAIL_MCP_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("EMAIL_MCP_IDENTITIES", str(tmp_path / "no-identities.toml"))


class _FakeProc:
    def __init__(self, returncode=0, stdout=b"", stderr=b""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _ssh(**kw) -> SshSendmailTransport:
    defaults = dict(
        identity="cern", from_addr="paris@cern.ch",
        host="lxplus.cern.ch", user="pmoschov", socket="/tmp/sock-test",
    )
    defaults.update(kw)
    return SshSendmailTransport(**defaults)


def _fake_smtp_cls(fail_login: Exception | None = None):
    """A fresh fake smtplib.SMTP/SMTP_SSL class per test (records calls)."""

    class FakeSMTP:
        instances: list = []

        def __init__(self, host, port, timeout=None):
            self.host, self.port, self.timeout = host, port, timeout
            self.calls: list = []
            FakeSMTP.instances.append(self)

        def starttls(self):
            self.calls.append(("starttls",))

        def ehlo(self):
            self.calls.append(("ehlo",))

        def login(self, user, password):
            self.calls.append(("login", user, password))
            if fail_login is not None:
                raise fail_login

        def sendmail(self, mail_from, rcpt_to, data):
            self.calls.append(("sendmail", mail_from, rcpt_to, data))

        def quit(self):
            self.calls.append(("quit",))

    return FakeSMTP


# --------------------------------------------------------------------- #
# registry                                                              #
# --------------------------------------------------------------------- #


def test_registry_resolves_all_three_drivers():
    assert set(transports.DRIVERS) == {"ssh_sendmail", "smtp", "pipe"}
    minimal = {
        "ssh_sendmail": {"host": "h", "user": "u", "socket": "/tmp/s"},
        "smtp": {"host": "smtp.example.org", "keychain": "item"},
        "pipe": {},
    }
    for driver, params in minimal.items():
        ident = Identity(
            name="t", from_addr="t@example.org", driver=driver, params=params,
        )
        transport = transports.get_transport(ident)
        assert transport.name == driver
        assert callable(transport.deliver)


def test_unknown_driver_raises_send_error():
    ident = Identity(name="x", from_addr="x@example.org", driver="carrier-pigeon")
    with pytest.raises(SendError) as ei:
        transports.get_transport(ident)
    s = str(ei.value)
    assert "carrier-pigeon" in s
    assert "ssh_sendmail" in s  # lists what IS available


def test_missing_driver_param_raises_send_error():
    ident = Identity(
        name="x", from_addr="x@example.org", driver="ssh_sendmail", params={},
    )
    with pytest.raises(SendError) as ei:
        transports.get_transport(ident)
    s = str(ei.value)
    assert "[x/ssh_sendmail]" in s
    assert "host" in s  # names the missing constructor param


# --------------------------------------------------------------------- #
# ssh_sendmail                                                          #
# --------------------------------------------------------------------- #


def test_ssh_argv_built_from_params_with_envelope_sender(monkeypatch):
    calls = []
    monkeypatch.setattr(
        ssh_mod.subprocess, "run",
        lambda cmd, **kw: calls.append((cmd, kw)) or _FakeProc(),
    )
    t = _ssh()
    raw = b"Message-ID: <x@y>\r\nTo: a@example.org\r\n\r\nhi\r\n"
    t.deliver(raw, "envelope@cern.ch", ["a@example.org"])
    cmd, kw = calls[-1]
    assert cmd == [
        "ssh",
        "-o", "ControlPath=/tmp/sock-test",
        "-o", "BatchMode=yes",
        "pmoschov@lxplus.cern.ch",
        "/usr/sbin/sendmail -t -i -f envelope@cern.ch",
    ]
    assert kw["input"] == raw  # stdin carries the frozen bytes


def test_ssh_failure_raises_prefixed_send_error(monkeypatch):
    monkeypatch.setattr(
        ssh_mod.subprocess, "run",
        lambda cmd, **kw: _FakeProc(returncode=1, stderr=b"relay denied"),
    )
    t = _ssh()
    with pytest.raises(SendError) as ei:
        t.deliver(b"To: a@b\r\n\r\nx\r\n", "p@cern.ch", ["a@b"])
    s = str(ei.value)
    assert s.startswith("[cern/ssh_sendmail]")
    assert "exit 1" in s and "relay denied" in s


def test_ssh_delivery_timeout_kills_master(monkeypatch):
    kills = []

    def fake_run(cmd, **kw):
        if "-O" in cmd and "exit" in cmd:
            kills.append(cmd)
            return _FakeProc()
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=60)

    monkeypatch.setattr(ssh_mod.subprocess, "run", fake_run)
    t = _ssh()
    with pytest.raises(SendError) as ei:
        t.deliver(b"To: a@b\r\n\r\nx\r\n", "p@cern.ch", ["a@b"])
    assert "timed out" in str(ei.value)
    assert len(kills) == 1  # the wedged master got torn down


def test_ssh_ensure_short_circuits_when_socket_alive(monkeypatch):
    t = _ssh()
    monkeypatch.setattr(t, "socket_alive", lambda: True)
    monkeypatch.setattr(
        t, "bootstrap_master",
        lambda: pytest.fail("bootstrap must not run when the socket is alive"),
    )
    assert t.ensure() is True
    assert t.last_ensure_error is None


def test_ssh_healthcheck_only_checks_never_mutates(monkeypatch):
    calls = []
    monkeypatch.setattr(
        ssh_mod.subprocess, "run",
        lambda cmd, **kw: calls.append(cmd) or _FakeProc(),
    )
    t = _ssh()
    hc = t.healthcheck()
    assert hc["ok"] is True and hc["socket_alive"] is True
    assert hc["driver"] == "ssh_sendmail" and hc["host"] == "lxplus.cern.ch"
    # exactly one read-only `ssh -O check`; no bootstrap, no -O exit
    assert len(calls) == 1
    assert calls[0][:3] == ["ssh", "-O", "check"]


# --------------------------------------------------------------------- #
# smtp                                                                  #
# --------------------------------------------------------------------- #

_RAW_WITH_BCC = (
    b"From: g@example.org\r\nTo: a@example.org\r\n"
    b"Bcc: g@example.org\r\nSubject: s\r\nMessage-ID: <m@x>\r\n\r\nbody\r\n"
)


def _smtp(**kw) -> SmtpTransport:
    defaults = dict(
        identity="gmail", from_addr="g@example.org",
        host="smtp.example.org", keychain="email-mcp-g",
    )
    defaults.update(kw)
    return SmtpTransport(**defaults)


def test_smtp_strips_bcc_header_but_keeps_envelope_rcpt(monkeypatch):
    FakeSMTP = _fake_smtp_cls()
    monkeypatch.setattr(smtp_mod.smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(smtp_mod, "_read_keychain", lambda item, account: "pw")
    t = _smtp()
    t.deliver(_RAW_WITH_BCC, "g@example.org", ["a@example.org", "g@example.org"])
    srv = FakeSMTP.instances[0]
    _, mail_from, rcpt_to, data = next(c for c in srv.calls if c[0] == "sendmail")
    assert mail_from == "g@example.org"
    assert rcpt_to == ["a@example.org", "g@example.org"]  # bcc in envelope
    assert b"bcc" not in data.lower()                     # …not in headers
    assert b"To: a@example.org" in data
    assert ("quit",) in srv.calls


def test_smtp_port_465_is_ssl_else_starttls(monkeypatch):
    FakeSSL, FakePlain = _fake_smtp_cls(), _fake_smtp_cls()
    monkeypatch.setattr(smtp_mod.smtplib, "SMTP_SSL", FakeSSL)
    monkeypatch.setattr(smtp_mod.smtplib, "SMTP", FakePlain)
    monkeypatch.setattr(smtp_mod, "_read_keychain", lambda item, account: "pw")

    _smtp(port=465).deliver(_RAW_WITH_BCC, "g@example.org", ["a@example.org"])
    ssl_calls = [c[0] for c in FakeSSL.instances[0].calls]
    assert "starttls" not in ssl_calls  # implicit TLS, no upgrade
    assert not FakePlain.instances

    _smtp(port=587).deliver(_RAW_WITH_BCC, "g@example.org", ["a@example.org"])
    plain_calls = [c[0] for c in FakePlain.instances[0].calls]
    assert plain_calls.index("starttls") < plain_calls.index("login")


def test_smtp_auth_failure_names_lane_and_keychain_item(monkeypatch):
    FakeSMTP = _fake_smtp_cls(
        fail_login=smtplib.SMTPAuthenticationError(535, b"5.7.8 bad creds"),
    )
    monkeypatch.setattr(smtp_mod.smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(smtp_mod, "_read_keychain", lambda item, account: "pw")
    t = _smtp()
    with pytest.raises(SendError) as ei:
        t.deliver(_RAW_WITH_BCC, "g@example.org", ["a@example.org"])
    s = str(ei.value)
    assert "[gmail/smtp]" in s          # the lane
    assert "email-mcp-g" in s           # the Keychain item to fix
    assert "g@example.org" in s         # the login that failed


def test_keychain_missing_item_fixit_and_timeout_always_allow(monkeypatch):
    # item absent (exit 44) → error carries the exact one-liner to store it
    monkeypatch.setattr(
        smtp_mod.subprocess, "run",
        lambda *a, **kw: _FakeProc(returncode=44, stdout=""),
    )
    with pytest.raises(SendError) as ei:
        smtp_mod._read_keychain("email-mcp-g", "g@example.org")
    assert (
        "security add-generic-password -s email-mcp-g -a g@example.org -w"
        in str(ei.value)
    )

    # hang (GUI permission prompt under launchd) → Always Allow guidance
    def raise_timeout(*a, **kw):
        raise subprocess.TimeoutExpired(cmd="security", timeout=30)

    monkeypatch.setattr(smtp_mod.subprocess, "run", raise_timeout)
    with pytest.raises(SendError) as ei:
        smtp_mod._read_keychain("email-mcp-g", "g@example.org")
    assert "Always Allow" in str(ei.value)


# --------------------------------------------------------------------- #
# pipe                                                                  #
# --------------------------------------------------------------------- #


def test_pipe_stdin_delivery_and_nonzero_exit(monkeypatch):
    calls = []
    monkeypatch.setattr(
        pipe_mod.subprocess, "run",
        lambda argv, **kw: calls.append((argv, kw)) or _FakeProc(),
    )
    t = PipeTransport(
        command="/usr/local/bin/msmtp -t --read-envelope-from",
        identity="local", from_addr="me@example.org",
    )
    raw = b"To: a@example.org\r\n\r\nhi\r\n"
    t.deliver(raw, "me@example.org", ["a@example.org"])
    argv, kw = calls[0]
    assert argv == ["/usr/local/bin/msmtp", "-t", "--read-envelope-from"]
    assert kw["input"] == raw  # the message travels over stdin

    monkeypatch.setattr(
        pipe_mod.subprocess, "run",
        lambda argv, **kw: _FakeProc(returncode=78, stderr=b"config error"),
    )
    with pytest.raises(SendError) as ei:
        t.deliver(raw, "me@example.org", ["a@example.org"])
    s = str(ei.value)
    assert s.startswith("[local/pipe]")
    assert "exit 78" in s and "config error" in s


def test_smtp_op_secret_source_wins_and_keychain_untouched(monkeypatch):
    """`op` (1Password secret reference) is a first-class secret source;
    when set it is used and the Keychain path is never consulted."""
    FakeSMTP = _fake_smtp_cls()
    monkeypatch.setattr(smtp_mod.smtplib, "SMTP", FakeSMTP)
    op_reads, kc_reads = [], []
    monkeypatch.setattr(smtp_mod, "_read_op",
                        lambda ref: (op_reads.append(ref), "op-pw")[1])
    monkeypatch.setattr(smtp_mod, "_read_keychain",
                        lambda item, account: kc_reads.append(item))
    t = _smtp(op="op://Personal/gmail app pw/password", keychain="ignored")
    t.deliver(_RAW_WITH_BCC, "g@example.org", ["a@example.org"])
    assert op_reads == ["op://Personal/gmail app pw/password"]
    assert kc_reads == []
    login = [c for c in FakeSMTP.instances[0].calls if c[0] == "login"][0]
    assert "op-pw" in login[1:]


def test_smtp_requires_a_secret_source():
    with pytest.raises(SendError) as ei:
        _smtp(keychain="")
    s = str(ei.value)
    assert "[gmail/smtp]" in s and "op" in s and "keychain" in s


def test_read_op_missing_cli_and_failure_messages(monkeypatch):
    def no_cli(*a, **k):
        raise FileNotFoundError("op")
    monkeypatch.setattr(smtp_mod.subprocess, "run", no_cli)
    with pytest.raises(SendError) as ei:
        smtp_mod._read_op("op://v/i/password")
    assert "1password-cli" in str(ei.value)

    def op_fails(*a, **k):
        import subprocess as sp
        return sp.CompletedProcess(a[0], 1, "", "no item found")
    monkeypatch.setattr(smtp_mod.subprocess, "run", op_fails)
    with pytest.raises(SendError) as ei:
        smtp_mod._read_op("op://v/i/password")
    s = str(ei.value)
    assert "no item found" in s and "op://v/i/password" in s
