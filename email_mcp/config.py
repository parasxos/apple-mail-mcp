"""Runtime configuration via environment variables. All optional."""
from __future__ import annotations

import os
import re
from pathlib import Path


def mail_dir() -> Path:
    """Return the active Mail.app data directory.

    Honors EMAIL_MCP_MAIL_DIR; otherwise picks the newest ~/Library/Mail/V*.
    """
    override = os.environ.get("EMAIL_MCP_MAIL_DIR", "").strip()
    if override:
        return Path(override).expanduser()

    base = Path.home() / "Library" / "Mail"
    if not base.exists():
        raise FileNotFoundError(
            f"{base} does not exist. Apple Mail is not configured on this Mac, "
            f"or grant Full Disk Access to the app running Claude Code."
        )
    versioned = [
        p for p in base.iterdir()
        if p.is_dir() and re.fullmatch(r"V\d+", p.name)
    ]
    if not versioned:
        raise FileNotFoundError(
            f"No V<N> directory found under {base}. "
            f"Grant Full Disk Access to the app running Claude Code."
        )
    # Highest version number wins (V10 > V9 > V8 …).
    return max(versioned, key=lambda p: int(p.name[1:]))


def source_name() -> str:
    return os.environ.get("EMAIL_MCP_SOURCE", "apple").strip() or "apple"


def max_body_bytes() -> int:
    return int(os.environ.get("EMAIL_MCP_MAX_BODY_BYTES", "2000000"))


def attach_dir() -> Path:
    raw = os.environ.get("EMAIL_MCP_ATTACH_DIR", "").strip()
    if raw:
        d = Path(raw).expanduser()
    else:
        tmp = os.environ.get("TMPDIR", "/tmp")
        d = Path(tmp) / "email-mcp"
    d.mkdir(parents=True, exist_ok=True)
    return d


# ---------------------------------------------------------------------- #
# Sending (send_email / reply_email). All optional; Paris-flavoured       #
# defaults, every value overridable via env.                             #
#                                                                        #
# Transport rationale: macOS Mail.app's scripted-compose path wraps the  #
# body in a collapsed blockquote (renders empty in Outlook), so we do    #
# NOT send through Mail.app. Instead we compose clean MIME here and pipe  #
# it to `sendmail` on an SSH host (lxplus), reusing a warm ControlMaster  #
# socket. smtp.cern.ch is GPN-internal and refuses tunneled STARTTLS, so  #
# lxplus sendmail is the sanctioned path.                                #
# ---------------------------------------------------------------------- #


def send_from_addr() -> str:
    """The From: address for outgoing mail."""
    return os.environ.get(
        "EMAIL_MCP_FROM_ADDR", "paris.moschovakos@cern.ch"
    ).strip()


def send_from_name() -> str:
    return os.environ.get("EMAIL_MCP_FROM_NAME", "Paris Moschovakos").strip()


def send_allow_all() -> bool:
    """When false (default), recipients are restricted to the allowlist —
    the trial-safety guard so a mistake can only reach Paris himself.

    Flip EMAIL_MCP_SEND_ALLOW_ALL=1 to send to anyone.
    """
    return os.environ.get("EMAIL_MCP_SEND_ALLOW_ALL", "0").strip() in {
        "1", "true", "True", "yes",
    }


def send_allowlist() -> set[str]:
    """Lower-cased set of addresses sending is permitted to reach when
    allow_all is off. Empty env → defaults to just the From: address.
    """
    raw = os.environ.get("EMAIL_MCP_SEND_ALLOWLIST", "").strip()
    if not raw:
        return {send_from_addr().lower()}
    return {a.strip().lower() for a in raw.split(",") if a.strip()}


def send_bcc_self() -> bool:
    """Bcc the From: address on every send, so there's a searchable record
    in Mail (SMTP-via-sendmail does not populate Exchange Sent Items).
    """
    return os.environ.get("EMAIL_MCP_BCC_SELF", "1").strip() in {
        "1", "true", "True", "yes",
    }


def send_host() -> str:
    return os.environ.get("EMAIL_MCP_SEND_HOST", "lxplus.cern.ch").strip()


def send_user() -> str:
    return os.environ.get("EMAIL_MCP_SEND_USER", "pmoschov").strip()


def send_ssh_socket() -> Path:
    raw = os.environ.get(
        "EMAIL_MCP_SSH_SOCKET", "~/.ssh/sock-lxplus-mail"
    ).strip()
    return Path(raw).expanduser()


def send_sendmail_path() -> str:
    return os.environ.get("EMAIL_MCP_SENDMAIL", "/usr/sbin/sendmail").strip()


def send_bootstrap_cmd() -> str:
    """Shell command that (re)establishes the ControlMaster socket headlessly.

    Empty (default) → resolve the bundled tools/lxplus_mail_master.sh relative
    to the installed package. The script sources ~/.secrets/cern_secrets.sh
    for CERN_PASSWORD and generates a TOTP, so it needs no interactive input.
    """
    raw = os.environ.get("EMAIL_MCP_SSH_BOOTSTRAP", "").strip()
    if raw:
        return raw
    # repo layout: email_mcp/config.py → ../tools/lxplus_mail_master.sh
    script = Path(__file__).resolve().parent.parent / "tools" / "lxplus_mail_master.sh"
    return str(script)
