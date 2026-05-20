"""On-disk path resolution for Apple Mail.

The Envelope Index tells us *which* message exists but not *where* on disk it
lives. This module is the single place that knows how to translate
(account_uuid, mailbox_url, message_rowid) into a `.emlx` path.

The mapping verified empirically on macOS Mail V10 (May 2026):

    file = <mail_dir>/<account_uuid>/<seg1>.mbox/<seg2>.mbox/.../<inner_uuid>/
           Data/<reversed-prefix-chars>/Messages/<rowid>.emlx

where the URL path `[Gmail]/All Mail` decodes to two nested .mbox segments,
and the sharding `<reversed-prefix-chars>` is computed as:

    s = str(rowid)
    prefix = s[:-3]             # drop the last 3 digits
    dirs = list(reversed(prefix))  # each remaining char is a directory level

The `<inner_uuid>` is constant across all mailboxes in a given install but is
discovered by glob (a UUID-shaped subdir) so we never hard-code it.
"""
from __future__ import annotations

import re
import urllib.parse
from pathlib import Path

_URL_RE = re.compile(r"^(?P<scheme>[a-z0-9+.-]+)://(?P<host>[^/]+)(?P<path>/.*)?$")
_UUID_RE = re.compile(
    r"^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}$"
)


def parse_mailbox_url(url: str) -> tuple[str, str, list[str]]:
    """Split a mailbox URL into (scheme, account_uuid, path_segments).

    Examples:
        ``local://A5A1FE68-…/Elogs`` → (``local``, ``A5A1FE68-…``, [``Elogs``])
        ``imap://C7B1…/%5BGmail%5D/All%20Mail`` →
            (``imap``, ``C7B1…``, [``[Gmail]``, ``All Mail``])
    """
    m = _URL_RE.match(url)
    if not m:
        raise ValueError(f"unrecognised mailbox url: {url!r}")
    scheme = m.group("scheme")
    host = m.group("host")
    path = m.group("path") or ""
    segments = [
        urllib.parse.unquote(seg)
        for seg in path.split("/")
        if seg  # skip leading empty from "/foo"
    ]
    return scheme, host, segments


def _find_inner_uuid_dir(mailbox_dir: Path) -> Path:
    """Pick the UUID-shaped subdir that holds Data/. Errors out cleanly."""
    if not mailbox_dir.is_dir():
        raise FileNotFoundError(f"mailbox dir missing: {mailbox_dir}")
    candidates = [
        p for p in mailbox_dir.iterdir()
        if p.is_dir() and _UUID_RE.match(p.name)
    ]
    if not candidates:
        raise FileNotFoundError(
            f"no UUID subdir under {mailbox_dir} — mailbox has no local store"
        )
    # If Mail.app ever ships >1 (it shouldn't), prefer the one that has Data/.
    candidates.sort(key=lambda p: (p / "Data").exists(), reverse=True)
    return candidates[0]


def emlx_relpath_for_rowid(rowid: int) -> Path:
    """The relative path under <inner_uuid>/Data for a given message ROWID.

    rowid=1277162  →  Path("7/7/2/1/Messages/1277162.emlx")
    rowid=425506   →  Path("5/2/4/Messages/425506.emlx")
    rowid=9336     →  Path("9/Messages/9336.emlx")
    rowid=500      →  Path("Messages/500.emlx")  # <=3 digits → no shard dirs
    """
    s = str(rowid)
    if len(s) <= 3:
        return Path("Messages") / f"{rowid}.emlx"
    prefix = s[:-3]
    parts = list(reversed(prefix))
    return Path(*parts) / "Messages" / f"{rowid}.emlx"


def build_emlx_path(
    mail_dir: Path,
    mailbox_url: str,
    rowid: int,
) -> Path:
    """Full path on disk for an .emlx given the Mail base dir + url + rowid."""
    _, account_uuid, segments = parse_mailbox_url(mailbox_url)
    if not segments:
        raise ValueError(f"mailbox url has no path: {mailbox_url!r}")
    mailbox_dir = mail_dir / account_uuid
    for seg in segments:
        mailbox_dir = mailbox_dir / f"{seg}.mbox"
    inner = _find_inner_uuid_dir(mailbox_dir)
    return inner / "Data" / emlx_relpath_for_rowid(rowid)


def account_label(mailbox_url: str) -> str:
    """Best-effort human-friendly account name. For now: the UUID itself.

    Phase 2 can plug in AccountsInfo plist parsing to resolve display names.
    """
    _, host, _ = parse_mailbox_url(mailbox_url)
    return host


def mailbox_name(mailbox_url: str) -> str:
    """Folder name including any nesting, e.g. '[Gmail]/All Mail'."""
    _, _, segments = parse_mailbox_url(mailbox_url)
    return "/".join(segments)
