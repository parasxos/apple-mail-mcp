#!/usr/bin/env python3
"""Verify that a release directory contains one complete, installable build."""
from __future__ import annotations

import argparse
import ast
import hashlib
import sys
import tarfile
import zipfile
from email.parser import BytesParser
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "email_mcp"
PROJECT_NAME = "apple-mail-mcp"
CONSOLE_SCRIPT = "email-mcp = email_mcp.cli:main"


class VerificationError(RuntimeError):
    pass


def _source_version() -> str:
    tree = ast.parse((PACKAGE / "__init__.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign):
            if any(isinstance(target, ast.Name) and target.id == "__version__"
                   for target in node.targets):
                value = ast.literal_eval(node.value)
                if isinstance(value, str):
                    return value
    raise VerificationError("email_mcp.__version__ is not a string literal")


def _one(directory: Path, pattern: str, label: str) -> Path:
    matches = sorted(directory.glob(pattern))
    if len(matches) != 1:
        raise VerificationError(
            f"want exactly one {label} in {directory}, found {len(matches)}"
        )
    return matches[0]


def _wheel_metadata(wheel: Path):
    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())
        metadata_names = [name for name in names
                          if name.endswith(".dist-info/METADATA")]
        entry_names = [name for name in names
                       if name.endswith(".dist-info/entry_points.txt")]
        if len(metadata_names) != 1 or len(entry_names) != 1:
            raise VerificationError(
                "wheel must contain one METADATA and one entry_points.txt"
            )
        metadata = BytesParser().parsebytes(archive.read(metadata_names[0]))
        entry_points = archive.read(entry_names[0]).decode("utf-8")
    return metadata, entry_points, names


def _check_metadata(metadata, version: str) -> None:
    if metadata["Name"] != PROJECT_NAME:
        raise VerificationError(
            f"package name {metadata['Name']!r} != {PROJECT_NAME!r}"
        )
    if metadata["Version"] != version:
        raise VerificationError(
            f"package version {metadata['Version']!r} != source {version!r}"
        )
    if metadata["Requires-Python"] != ">=3.11":
        raise VerificationError(
            f"unexpected Requires-Python {metadata['Requires-Python']!r}"
        )
    if not metadata.get_payload().strip():
        raise VerificationError("package metadata has no README description")


def _check_wheel(wheel: Path, version: str) -> None:
    metadata, entry_points, names = _wheel_metadata(wheel)
    _check_metadata(metadata, version)
    if CONSOLE_SCRIPT not in entry_points:
        raise VerificationError(f"wheel is missing console script: {CONSOLE_SCRIPT}")

    expected = {
        path.relative_to(ROOT).as_posix()
        for path in PACKAGE.rglob("*.py")
        if "__pycache__" not in path.parts
    }
    missing = sorted(expected - names)
    if missing:
        raise VerificationError(
            "wheel is missing Python modules: " + ", ".join(missing)
        )


def _check_sdist(sdist: Path, version: str) -> None:
    with tarfile.open(sdist, "r:gz") as archive:
        names = set(archive.getnames())
        roots = {name.split("/", 1)[0] for name in names if "/" in name}
        if len(roots) != 1:
            raise VerificationError("sdist must contain exactly one root directory")
        root = next(iter(roots))
        required = {
            "README.md",
            "LICENSE",
            "MANIFEST.in",
            "docs/architecture.md",
            "pyproject.toml",
        }
        required.update(
            path.relative_to(ROOT).as_posix()
            for path in PACKAGE.rglob("*.py")
            if "__pycache__" not in path.parts
        )
        missing = sorted(
            relative for relative in required
            if f"{root}/{relative}" not in names
        )
        if missing:
            raise VerificationError(
                "sdist is missing release files: " + ", ".join(missing)
            )

        pkg_info = f"{root}/PKG-INFO"
        try:
            member = archive.extractfile(pkg_info)
            metadata = BytesParser().parsebytes(member.read()) if member else None
        except KeyError as exc:
            raise VerificationError("sdist is missing PKG-INFO") from exc
        if metadata is None:
            raise VerificationError("sdist PKG-INFO is unreadable")
        _check_metadata(metadata, version)


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify(directory: Path, tag: str | None = None) -> list[Path]:
    version = _source_version()
    if tag is not None and tag != f"v{version}":
        raise VerificationError(f"tag {tag!r} != source version 'v{version}'")
    wheel = _one(directory, "*.whl", "wheel")
    sdist = _one(directory, "*.tar.gz", "source distribution")
    _check_wheel(wheel, version)
    _check_sdist(sdist, version)
    return [wheel, sdist]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Verify email-mcp wheel and source release contents.",
    )
    parser.add_argument("directory", type=Path, help="directory containing dist files")
    parser.add_argument("--tag", help="optional Git tag, for example v1.3.0")
    args = parser.parse_args(argv)
    try:
        artifacts = verify(args.directory, args.tag)
    except VerificationError as exc:
        print(f"release verification failed: {exc}", file=sys.stderr)
        return 1
    for artifact in artifacts:
        print(f"verified {artifact.name} sha256:{_digest(artifact)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
