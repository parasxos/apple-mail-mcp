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
    monkeypatch.setenv("EMAIL_MCP_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("EMAIL_MCP_IDENTITIES", str(tmp_path / "no-identities.toml"))


def _write_toml(tmp_path, monkeypatch, text: str):
    p = tmp_path / "identities.toml"
    p.write_text(textwrap.dedent(text))
    monkeypatch.setenv("EMAIL_MCP_IDENTITIES", str(p))
    return p


# --------------------------------------------------------------------- #
# absent file → env synthesis                                           #
# --------------------------------------------------------------------- #


def test_absent_file_without_from_addr_names_the_remedy():
    """The v0.8 guard: neither an identities file nor EMAIL_MCP_FROM_ADDR
    (defaults are empty post-flip) → ONE clear IdentityError with the
    remedy, instead of a cascade of empty-From failures downstream."""
    with pytest.raises(IdentityError) as ei:
        identities.load()
    s = str(ei.value)
    assert "no sending identity configured" in s
    assert "identities.toml" in s
    assert "EMAIL_MCP_FROM_ADDR" in s


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
    # Open since the 2026-08-01 flip: the fallback allowlist is not a
    # DECLARED restriction, so the synthesized identity is unrestricted.
    assert ident.allow_all is True
    assert ident.bcc_self is False


def test_env_declared_allowlist_engages_the_guard(monkeypatch):
    monkeypatch.setenv("EMAIL_MCP_FROM_ADDR", "someone@example.org")
    monkeypatch.setenv("EMAIL_MCP_SEND_ALLOWLIST", "a@b.org")
    ident = identities.get()
    assert ident.allow_all is False
    assert ident.allowlist == ["a@b.org"]


def test_toml_declared_allowlist_engages_the_guard(tmp_path, monkeypatch):
    _write_toml(tmp_path, monkeypatch, """\
        default = "main"

        [main]
        from_addr = "x@example.org"
        driver = "pipe"
        command = "/usr/sbin/sendmail -t -i"
        allowlist = ["a@b.org"]
    """)
    assert identities.get().allow_all is False


def test_toml_undeclared_identity_is_open(tmp_path, monkeypatch):
    _write_toml(tmp_path, monkeypatch, """\
        default = "main"

        [main]
        from_addr = "x@example.org"
        driver = "pipe"
        command = "/usr/sbin/sendmail -t -i"
    """)
    assert identities.get().allow_all is True


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


# --------------------------------------------------------------------- #
# executor capability (graph deferred send) — additive, inert by default #
# --------------------------------------------------------------------- #


def test_executor_defaults_to_launchd_and_stays_out_of_params(
    tmp_path, monkeypatch,
):
    """F13: an identity that never mentions `executor` is byte-identical
    to pre-graph behavior — launchd executor, empty graph config, and
    neither key anywhere near the driver params."""
    _write_toml(tmp_path, monkeypatch, """\
        default = "a"

        [a]
        from_addr = "a@example.org"
        driver = "pipe"
        command = "/usr/bin/true"
    """)
    ident = identities.get("a")
    assert ident.executor == "launchd"
    assert ident.graph == {}
    assert ident.params == {"command": "/usr/bin/true"}


def test_synthesized_identity_uses_launchd_executor(monkeypatch):
    monkeypatch.setenv("EMAIL_MCP_FROM_ADDR", "someone@example.org")
    ident = identities.get()
    assert ident.executor == "launchd"
    assert ident.graph == {}


def test_unknown_executor_errors(tmp_path, monkeypatch):
    p = _write_toml(tmp_path, monkeypatch, """\
        default = "a"

        [a]
        from_addr = "a@example.org"
        driver = "pipe"
        executor = "cron"
    """)
    with pytest.raises(IdentityError) as ei:
        identities.load()
    s = str(ei.value)
    assert str(p) in s and "cron" in s and "launchd" in s and "graph" in s


def test_executor_graph_without_graph_table_errors(tmp_path, monkeypatch):
    p = _write_toml(tmp_path, monkeypatch, """\
        default = "a"

        [a]
        from_addr = "a@example.org"
        driver = "pipe"
        executor = "graph"
    """)
    with pytest.raises(IdentityError) as ei:
        identities.load()
    s = str(ei.value)
    assert str(p) in s and "tenant" in s and "client_id" in s


def test_executor_graph_missing_client_id_errors(tmp_path, monkeypatch):
    _write_toml(tmp_path, monkeypatch, """\
        default = "a"

        [a]
        from_addr = "a@example.org"
        driver = "pipe"
        executor = "graph"

        [a.graph]
        tenant = "example.org"
    """)
    with pytest.raises(IdentityError) as ei:
        identities.load()
    assert "client_id" in str(ei.value)


def test_graph_must_be_a_table_errors(tmp_path, monkeypatch):
    _write_toml(tmp_path, monkeypatch, """\
        default = "a"

        [a]
        from_addr = "a@example.org"
        driver = "pipe"
        graph = "example.org"
    """)
    with pytest.raises(IdentityError) as ei:
        identities.load()
    assert "[a.graph]" in str(ei.value)


def test_graph_keys_never_reach_params_and_ssh_transport_constructs(
    tmp_path, monkeypatch,
):
    """The capability lives on the Identity, not in the driver's kwargs:
    a graph-executor identity must still construct its ssh transport
    (drivers have no **kwargs — a leak would TypeError)."""
    from email_mcp.transports import get_transport

    _write_toml(tmp_path, monkeypatch, """\
        default = "cern"

        [cern]
        from_addr = "someone@example.org"
        driver = "ssh_sendmail"
        executor = "graph"
        host = "mailhost.example.org"
        user = "someone"
        socket = "/tmp/sock-x"

        [cern.graph]
        tenant = "example.org"
        client_id = "app-123"
    """)
    ident = identities.get("cern")
    assert ident.executor == "graph"
    assert ident.graph == {"tenant": "example.org", "client_id": "app-123"}
    assert "executor" not in ident.params and "graph" not in ident.params
    transport = get_transport(ident)  # would raise SendError on a leak
    assert transport.host == "mailhost.example.org"


def test_imap_table_parses_and_never_reaches_params(tmp_path, monkeypatch):
    """[name.imap] opts the identity's mailbox into the IMAP backfill
    lane; like graph, the capability lives on the Identity — a leak
    into the driver's kwargs would TypeError the transport."""
    from email_mcp.transports import get_transport

    _write_toml(tmp_path, monkeypatch, """\
        default = "gmail"

        [gmail]
        from_addr = "someone@gmail.com"
        driver = "smtp"
        host = "smtp.gmail.com"
        keychain = "k-item"

        [gmail.imap]
        host = "imap.gmail.com"
        op = "op://Vault/item/password"
    """)
    ident = identities.get("gmail")
    assert ident.imap == {"host": "imap.gmail.com",
                          "op": "op://Vault/item/password"}
    assert "imap" not in ident.params
    transport = get_transport(ident)  # would raise SendError on a leak
    assert transport.host == "smtp.gmail.com"


def test_imap_table_missing_host_errors(tmp_path, monkeypatch):
    _write_toml(tmp_path, monkeypatch, """\
        default = "a"

        [a]
        from_addr = "a@example.org"
        driver = "pipe"

        [a.imap]
        op = "op://Vault/item/password"
    """)
    with pytest.raises(IdentityError) as ei:
        identities.load()
    assert "host" in str(ei.value)


def test_imap_table_missing_secret_source_errors(tmp_path, monkeypatch):
    _write_toml(tmp_path, monkeypatch, """\
        default = "a"

        [a]
        from_addr = "a@example.org"
        driver = "pipe"

        [a.imap]
        host = "imap.example.org"
    """)
    with pytest.raises(IdentityError) as ei:
        identities.load()
    s = str(ei.value)
    assert "op" in s and "keychain" in s


def test_imap_must_be_a_table_errors(tmp_path, monkeypatch):
    _write_toml(tmp_path, monkeypatch, """\
        default = "a"

        [a]
        from_addr = "a@example.org"
        driver = "pipe"
        imap = "imap.example.org"
    """)
    with pytest.raises(IdentityError) as ei:
        identities.load()
    assert "[a.imap]" in str(ei.value)


# --------------------------------------------------------------------- #
# path policy — the identities file is part of the managed tree          #
# --------------------------------------------------------------------- #
# One spelling owner: the default is <resolved root>/identities.toml, so
# the one root override moves the whole tree, this file included. Before
# this table, setup wrote under the override while sending read the
# default spelling — two files, one name.


def test_identities_default_lives_in_the_default_root(monkeypatch, tmp_path):
    h = tmp_path / "h"
    h.mkdir()
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.delenv("EMAIL_MCP_IDENTITIES", raising=False)
    monkeypatch.delenv("EMAIL_MCP_STATE_DIR", raising=False)
    from email_mcp import config, state
    assert config.identities_file() == state.default_root() / "identities.toml"


def test_identities_follows_the_root_override_and_agrees_with_checks(
    monkeypatch, tmp_path,
):
    """Under EMAIL_MCP_STATE_DIR the identities default moves WITH the
    tree, and it is the same path the secret-file mode checks probe —
    agreement by construction, not by care."""
    monkeypatch.delenv("EMAIL_MCP_IDENTITIES", raising=False)
    from email_mcp import checks, config, state
    r = state.State.resolve()
    assert isinstance(r, state.Resolved)  # _clean_env pinned the override
    assert config.identities_file() == r.root / "identities.toml"
    assert config.identities_file() in checks._secret_files(r.reader())


def test_identities_env_override_wins_over_the_root(tmp_path):
    from email_mcp import config
    # _clean_env sets both EMAIL_MCP_IDENTITIES and EMAIL_MCP_STATE_DIR.
    assert config.identities_file() == tmp_path / "no-identities.toml"


def test_identities_answers_under_a_refused_root(monkeypatch, tmp_path):
    """A refused root must not break identity routing: the path question
    stays total and falls back to the default spelling."""
    h = tmp_path / "h"
    h.mkdir()
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.delenv("EMAIL_MCP_IDENTITIES", raising=False)
    monkeypatch.setenv("EMAIL_MCP_SPOOL_DIR", str(tmp_path))  # retired var
    from email_mcp import config, state
    assert isinstance(state.State.resolve(), state.Refused)
    assert config.identities_file() == state.default_root() / "identities.toml"
