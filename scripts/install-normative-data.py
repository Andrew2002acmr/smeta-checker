#!/usr/bin/env python3
"""Install the versioned normative data release without overwriting existing data."""

import argparse
import hashlib
from pathlib import Path
import subprocess
import tarfile
import tempfile

REPOSITORY = "KDFP-IT/smeta-checker"
RELEASE = "normative-data-20261030"
ARCHIVES = {
    "normative-base-20261030.tar.gz": (
        "28244557cc92b124aab188eb33d4fdbf5d5e40c6ca43222e59915681a9d06c6e",
        "base",
    ),
    "normative-handoff-20261030.tar.gz": (
        "f665cdc72bcd4ba77aa6922a13f182fc241895bc5bb84f868e0bef947e014cda",
        "normative-handoff-20261030-092929",
    ),
}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dest", type=Path, default=Path(__file__).resolve().parents[1],
        help="installation directory (default: repository root)",
    )
    args = parser.parse_args()
    destination = args.dest.resolve()
    destination.mkdir(parents=True, exist_ok=True)
    existing = [destination / name for _, name in ARCHIVES.values() if (destination / name).exists()]
    if existing:
        parser.error("refusing to overwrite existing paths: " + ", ".join(map(str, existing)))

    with tempfile.TemporaryDirectory(prefix=".normative-install-", dir=destination) as tmp:
        staging = Path(tmp)
        command = ["gh", "release", "download", RELEASE, "--repo", REPOSITORY, "--dir", str(staging)]
        for archive in ARCHIVES:
            command.extend(["--pattern", archive])
        subprocess.run(command, check=True)
        for archive, (expected, name) in ARCHIVES.items():
            path = staging / archive
            with path.open("rb") as stream:
                actual = hashlib.file_digest(stream, "sha256").hexdigest()
            if actual != expected:
                raise ValueError(f"SHA-256 mismatch: {archive}")
            with tarfile.open(path, "r:gz") as bundle:
                for member in bundle:
                    parts = Path(member.name).parts
                    if not parts or parts[0] != name or member.issym() or member.islnk():
                        raise ValueError(f"Unexpected archive member: {member.name}")
                bundle.extractall(staging, filter="data")
        for _, name in ARCHIVES.values():
            (staging / name).rename(destination / name)
    print(f"Normative data installed in {destination}")


if __name__ == "__main__":
    main()
