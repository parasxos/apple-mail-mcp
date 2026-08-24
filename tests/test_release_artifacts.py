"""Build the artifacts users download and verify their release contract."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from email_mcp import __version__


ROOT = Path(__file__).resolve().parents[1]
VERIFY = ROOT / "tools" / "verify_dist.py"


@pytest.fixture(scope="module")
def built_dist(tmp_path_factory) -> Path:
    dist = tmp_path_factory.mktemp("release-dist")
    subprocess.run(
        [sys.executable, "-m", "build", "--outdir", str(dist)],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return dist


def test_built_wheel_and_sdist_pass_release_verification(built_dist):
    result = subprocess.run(
        [sys.executable, str(VERIFY), str(built_dist), "--tag", f"v{__version__}"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.count("verified ") == 2
    assert "sha256:" in result.stdout


def test_release_verification_rejects_a_tag_version_mismatch(built_dist):
    wrong_tag = f"v{__version__}-wrong"
    result = subprocess.run(
        [sys.executable, str(VERIFY), str(built_dist), "--tag", wrong_tag],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
    assert f"tag {wrong_tag!r} != source version 'v{__version__}'" in result.stderr
