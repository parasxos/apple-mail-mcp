"""Path-resolution unit tests — no DB or .emlx required."""
from __future__ import annotations

from email_mcp.sources.apple_mail_paths import (
    account_label,
    emlx_relpath_for_rowid,
    mailbox_name,
    parse_mailbox_url,
)


def test_parse_local_url():
    scheme, host, segs = parse_mailbox_url(
        "local://A5A1FE68-7C89-40C8-B6FC-A63FC787294A/Elogs"
    )
    assert scheme == "local"
    assert host == "A5A1FE68-7C89-40C8-B6FC-A63FC787294A"
    assert segs == ["Elogs"]


def test_parse_imap_nested_url():
    scheme, host, segs = parse_mailbox_url(
        "imap://C7B1EC2E-C8EC-4F68-B03B-E337D4794194/%5BGmail%5D/All%20Mail"
    )
    assert scheme == "imap"
    assert segs == ["[Gmail]", "All Mail"]
    assert mailbox_name(
        "imap://C7B1EC2E-C8EC-4F68-B03B-E337D4794194/%5BGmail%5D/All%20Mail"
    ) == "[Gmail]/All Mail"


def test_account_label_returns_uuid():
    assert account_label("local://AAAA-BBBB/Folder") == "AAAA-BBBB"


def test_emlx_relpath_shard_long():
    # Verified empirically: ROWID 1277162 → Data/7/7/2/1/Messages/1277162.emlx
    p = emlx_relpath_for_rowid(1277162)
    assert p.as_posix() == "7/7/2/1/Messages/1277162.emlx"


def test_emlx_relpath_shard_medium():
    # ROWID 425506 → drop "506", reverse "425" → 5/2/4
    p = emlx_relpath_for_rowid(425506)
    assert p.as_posix() == "5/2/4/Messages/425506.emlx"


def test_emlx_relpath_shard_short():
    # 4-digit ROWID 9336 → drop "336", reverse "9" → just 9
    p = emlx_relpath_for_rowid(9336)
    assert p.as_posix() == "9/Messages/9336.emlx"


def test_emlx_relpath_no_shard():
    # ≤3 digits → no subdir
    p = emlx_relpath_for_rowid(500)
    assert p.as_posix() == "Messages/500.emlx"
