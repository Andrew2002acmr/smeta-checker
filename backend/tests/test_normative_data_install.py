"""A fresh install composes pinned release assets without replacing local data."""

import hashlib
import importlib.util
import io
from pathlib import Path
import tarfile

import pytest


def installer():
    path = Path(__file__).resolve().parents[2] / "scripts/install-normative-data.py"
    spec = importlib.util.spec_from_file_location("normative_data_install", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def bundle(path, files):
    with tarfile.open(path, "w:gz") as archive:
        for name, content in files.items():
            entry = tarfile.TarInfo(name)
            entry.size = len(content)
            archive.addfile(entry, io.BytesIO(content))
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.mark.parametrize("corrupt_road", [False, True])
def test_install_checks_new_road_asset_before_publishing_any_roots(
    tmp_path, monkeypatch, corrupt_road
):
    module = installer()
    downloads = tmp_path / "downloads"
    downloads.mkdir()
    assets = {
        "base.tar.gz": {"base/catalog.txt": b"catalog"},
        "handoff.tar.gz": {
            "normative-handoff-20261030-092929/knowledge-base/electrical-norms/CURRENT.json": b"electrical",
            "normative-handoff-20261030-092929/knowledge-base/road-norms/CURRENT.json": b"old-road",
        },
        "road.tar.gz": {"road-norms/CURRENT.json": b"new-road"},
    }
    hashes = {name: bundle(downloads / name, files) for name, files in assets.items()}
    module.ARCHIVES = {
        "base.tar.gz": (hashes["base.tar.gz"], "base"),
        "handoff.tar.gz": (
            hashes["handoff.tar.gz"],
            "normative-handoff-20261030-092929",
        ),
    }
    module.ROAD_ARCHIVE = "road.tar.gz"
    module.ROAD_SHA256 = "0" * 64 if corrupt_road else hashes["road.tar.gz"]
    calls = []

    def download(command, **kwargs):
        calls.append(command[3])
        destination = Path(command[command.index("--dir") + 1])
        for index, argument in enumerate(command):
            if argument == "--pattern":
                name = command[index + 1]
                (destination / name).write_bytes((downloads / name).read_bytes())

    monkeypatch.setattr(module.subprocess, "run", download)
    target = tmp_path / "installed"
    monkeypatch.setattr("sys.argv", ["install", "--dest", str(target)])
    if corrupt_road:
        with pytest.raises(ValueError, match="SHA-256 mismatch"):
            module.main()
        assert not (target / "base").exists()
        assert not (target / "normative-handoff-20261030-092929").exists()
    else:
        module.main()
        knowledge = target / "normative-handoff-20261030-092929/knowledge-base"
        assert (knowledge / "road-norms/CURRENT.json").read_bytes() == b"new-road"
        assert (
            knowledge / "electrical-norms/CURRENT.json"
        ).read_bytes() == b"electrical"
        assert (target / "base/catalog.txt").read_bytes() == b"catalog"
        assert calls == [module.RELEASE, module.ROAD_RELEASE]
        # A second install may not overwrite an existing corpus or fetch data.
        with pytest.raises(SystemExit):
            module.main()
        assert len(calls) == 2
