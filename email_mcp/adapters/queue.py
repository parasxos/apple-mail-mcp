"""Typed translation of filesystem-spool integrity scans."""
from __future__ import annotations

from ..application.models import QueueIntegrity
from ..domain.models import ScheduledScan


def queue_integrity(scans: list[ScheduledScan]) -> QueueIntegrity:
    return QueueIntegrity(
        ok=all(scan.ok for scan in scans),
        counts={scan.state: scan.manifest_files for scan in scans},
        readable_counts={
            scan.state: scan.readable_manifests for scan in scans
        },
        message_files={scan.state: scan.eml_files for scan in scans},
        issues=[issue for scan in scans for issue in scan.issues],
    )
