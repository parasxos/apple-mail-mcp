"""Identity loader tests — env synthesis (absent file), TOML parsing rules,
every validation error, and the get() aliasing contract. No file outside
tmp_path is ever read; nothing leaves the machine."""
from __future__ import annotations

import os
import textwrap

import pytest

from email_mcp import identities
from email_mcp.identities import Identity, IdentityError
from email_mcp.transports import SendError


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch, tmp_path):
    """Documented defaults, and the identities file pointed at a nonexistent
    path so a production ~/.email-mcp/identities.toml can never leak in."""
    for k in list(os.environ):
        if k.startswith("EMAIL_MCP_"):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("EMAIL_MCP_IDENTITIES", str(tmp_path / "no-identities.toml"))


def _write_toml(tmp_path, monkeypatch, text: str):
    p = tmp_path / "identities.toml"
    p.write_text(textwrap.dedent(text))
    monkeypatch.setenv("EMAIL_MCP_IDENTITIES", str(p))
    return p


# --------------------------------------------------------------------- #
# absent file → env synthesis                                           #
# --------------------------------------------------------------------- #


def test_absent_file_synthesizes_default_mirroring_env(monkeypatch):
    monkeypatch.setenv("EMAIL_MCP_FROM_ADDR", "someone@example.org")
    monkeypatch.setenv("EMAIL_MCP_FROM_NAME", "Someone Else")
    monkeypatch.setenv("EMAIL_MCP_SEND_HOST", "mailhost.example.org")
    monkeypatch.setenv("EMAIL_MCP_SEND_USER", "someone")
    monkeypatch.setenv("EMAIL_MCP_SSH_SOCKET", "/tmp/sock-x")
    monkeypatch.setenv("EMAIL_MCP_SSH_BOOTSTRAP", "/tmp/boot.sh")
    monkeypatch.setenv("EMAIL_MCP_DELIVERY_CMD", "/usr/bin/sendmail")
    monkeypatch.setenv("EMAIL_MCP_BCC_SELF", "0")
    ident = identities.get()
    assert ident.name == "default"
    assert ident.from_addr == "someone@example.org"
    assert ident.from_name == "Someone Else"
    assert ident.driver == "ssh_sendmail"
    assert ident.params == {
        "host": "mailhost.example.org",
        "user": "someone",
        "socket": "/tmp/sock-x",
        "bootstrap": "/tmp/boot.sh",
        "delivery_cmd": "/usr/bin/sendmail",
    }
    assert ident.allowlist == ["someone@example.org"]
    assert ident.allow_all is False
    assert ident.bcc_self is False


def test_identities_env_var_overrides_file_path(tmp_path, monkeypatch):
    p = tmp_path / "elsewhere" / "ids.toml"
    p.parent.mkdir()
    p.write_text(
        'default = "work"\n\n[work]\n'
        'from_addr = "w@example.org"\ndriver = "pipe"\n'
    )
    monkeypatch.setenv("EMAIL_MCP_IDENTITIES", str(p))
    ident = identities.get()
    assert ident.name == "work"
    assert ident.from_addr == "w@example.org"
    assert ident.driver == "pipe"


# --------------------------------------------------------------------- #
# TOML rule: known fields → Identity, everything else → params          #
# --------------------------------------------------------------------- #


def test_known_fields_vs_driver_params_split(tmp_path, monkeypatch):
    _write_toml(tmp_path, monkeypatch, """\
        default = "gmail"

        [gmail]
        from_addr = "g@example.org"
        from_name = "G"
        driver = "smtp"
        allow_all = true
        bcc_self = false
        allowlist = ["g@example.org", "Other@Example.org"]
        host = "smtp.example.org"
        port = 465
        keychain = "email-mcp-g"
    """)
    ident = identities.get("gmail")
    assert ident.from_addr == "g@example.org"
    assert ident.from_name == "G"
    assert ident.driver == "smtp"
    assert ident.allow_all is True
    assert ident.bcc_self is False
    assert ident.allowlist == ["g@example.org", "other@example.org"]
    # everything the loader doesn't know is the driver's business
    assert ident.params == {
        "host": "smtp.example.org", "port": 465, "keychain": "email-mcp-g",
    }


def test_allowlist_defaults_to_the_identitys_own_address(tmp_path, monkeypatch):
    _write_toml(tmp_path, monkeypatch, """\
        default = "a"

        [a]
        from_addr = "Mixed@Example.Org"
        driver = "pipe"
    """)
    assert identities.get("a").allowlist == ["mixed@example.org"]


# --------------------------------------------------------------------- #
# validation errors (each names the file)                               #
# --------------------------------------------------------------------- #


def test_missing_default_key_errors(tmp_path, monkeypatch):
    p = _write_toml(tmp_path, monkeypatch, """\
        [a]
        from_addr = "a@example.org"
        driver = "pipe"
    """)
    with pytest.raises(IdentityError) as ei:
        identities.load()
    s = str(ei.value)
    assert str(p) in s and "default" in s


def test_unknown_default_errors(tmp_path, monkeypatch):
    p = _write_toml(tmp_path, monkeypatch, """\
        default = "nope"

        [a]
        from_addr = "a@example.org"
        driver = "pipe"
    """)
    with pytest.raises(IdentityError) as ei:
        identities.load()
    s = str(ei.value)
    assert str(p) in s and "'nope'" in s and "'a'" in s


def test_unknown_driver_errors(tmp_path, monkeypatch):
    p = _write_toml(tmp_path, monkeypatch, """\
        default = "a"

        [a]
        from_addr = "a@example.org"
        driver = "carrier-pigeon"
    """)
    with pytest.raises(IdentityError) as ei:
        identities.load()
    s = str(ei.value)
    assert str(p) in s and "carrier-pigeon" in s and "ssh_sendmail" in s


def test_missing_from_addr_errors(tmp_path, monkeypatch):
    p = _write_toml(tmp_path, monkeypatch, """\
        default = "a"

        [a]
        driver = "pipe"
    """)
    with pytest.raises(IdentityError) as ei:
        identities.load()
    s = str(ei.value)
    assert str(p) in s and "[a]" in s and "from_addr" in s


def test_duplicate_from_addr_errors(tmp_path, monkeypatch):
    p = _write_toml(tmp_path, monkeypatch, """\
        default = "a"

        [a]
        from_addr = "Same@Example.org"
        driver = "pipe"

        [b]
        from_addr = "same@example.org"
        driver = "pipe"
    """)
    with pytest.raises(IdentityError) as ei:
        identities.load()
    s = str(ei.value)  # case-insensitive match; names both identities
    assert str(p) in s and "[a]" in s and "[b]" in s


def test_malformed_toml_errors(tmp_path, monkeypatch):
    p = tmp_path / "identities.toml"
    p.write_text('default = "a\n[a]\nbroken')
    monkeypatch.setenv("EMAIL_MCP_IDENTITIES", str(p))
    with pytest.raises(IdentityError) as ei:
        identities.load()
    assert str(p) in str(ei.value)


# --------------------------------------------------------------------- #
# get() aliasing contract                                               #
# --------------------------------------------------------------------- #


def test_get_none_and_literal_default_alias_file_default(tmp_path, monkeypatch):
    _write_toml(tmp_path, monkeypatch, """\
        default = "a"

        [a]
        from_addr = "a@example.org"
        driver = "pipe"

        [b]
        from_addr = "b@example.org"
        driver = "pipe"
    """)
    assert identities.get(None).name == "a"
    assert identities.get("default").name == "a"  # old spool entries say this
    assert identities.get("b").name == "b"
    with pytest.raises(IdentityError) as ei:
        identities.get("zzz")
    s = str(ei.value)
    assert "'zzz'" in s and "'a'" in s and "'b'" in s  # lists what exists


def test_identity_error_is_a_send_error():
    assert issubclass(IdentityError, SendError)
    with pytest.raises(SendError):  # existing handlers catch it for free
        raise IdentityError("boom")
