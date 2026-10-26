"""The public datasets the quality measurements read, each pinned by URL, size and SHA-256, with its licence.

`python scripts/public_data.py` is compose's quality-fetcher, the only quality step with network: it downloads each
dataset into QUALITY_DATA_DIR (default .cache/datasets) and refuses bytes that differ from the pin. The measurements
read the files offline and check them again, so a cache that changed is refused rather than measured.
"""

import hashlib
import os
import sys
from pathlib import Path
from urllib.request import urlopen

REPO = Path(__file__).resolve().parents[1]
# Licences under which anyone may fetch the data, measure on it and publish the figures, commercial use included.
FREE_LICENCES = {"CC-BY-4.0", "CC-BY-SA-4.0"}
DATASETS = {
    # BEIR's copy of ArguAna: its corpus, queries and test qrels in one zip, whose MD5 BEIR publishes too.
    "arguana": {
        "file": "arguana.zip",
        "url": "https://public.ukp.informatik.tu-darmstadt.de/thakur/BEIR/datasets/arguana.zip",
        "bytes": 3773617,
        "sha256": "cfdf79adce27a401b3cd3ea267903134dbfab2c6afeb95d7fe5724a00bf7557b",
        "licence": "CC-BY-4.0",
        "licence_source": "BEIR, arXiv:2104.08663, appendix E (the zip carries no licence file)",
    },
    # The WNUT-17 shared task's annotated test split, as its organisers publish it.
    "wnut17": {
        "file": "wnut17-test.conll",
        "url": "https://raw.githubusercontent.com/leondz/emerging_entities_17/"
               "e52a2d2a71ac1a051ca2d532eef28653c5c02603/emerging.test.annotated",
        "bytes": 192425,
        "sha256": "2aa79b764e56ec9264a1b30fdd9b70195bd00ff400b62edd8f399d5f13c178f0",
        "licence": "CC-BY-4.0",
        "licence_source": "the README of github.com/leondz/emerging_entities_17",
    },
}


def data_dir(environ) -> Path:
    return Path(environ["QUALITY_DATA_DIR"]) if environ.get("QUALITY_DATA_DIR") else REPO / ".cache" / "datasets"


def pin(name: str) -> dict:
    entry = DATASETS[name]
    if entry["licence"] not in FREE_LICENCES:
        raise SystemExit(f"{name}: {entry['licence']} is not a licence the measurements may publish under")
    return entry


def matches(entry: dict, data: bytes) -> bool:
    return len(data) == entry["bytes"] and hashlib.sha256(data).hexdigest() == entry["sha256"]


def fetch(name: str, directory: Path) -> Path:
    entry = pin(name)
    target = directory / entry["file"]
    if target.is_file() and matches(entry, target.read_bytes()):
        return target
    with urlopen(entry["url"], timeout=60) as response:
        # One byte past the pin tells a longer file apart without reading all of it.
        data = response.read(entry["bytes"] + 1)
    if not matches(entry, data):
        raise SystemExit(f"{name}: {entry['url']} served bytes that differ from the pinned size and SHA-256")
    directory.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(target.name + ".part")
    partial.write_bytes(data)
    os.replace(partial, target)
    return target


def read(name: str, directory: Path) -> bytes:
    entry = pin(name)
    path = directory / entry["file"]
    if not path.is_file():
        raise SystemExit(f"{path} is missing: run the quality-fetcher service first")
    data = path.read_bytes()
    if not matches(entry, data):
        raise SystemExit(f"{path} differs from the pinned SHA-256: run the quality-fetcher service again")
    return data


def main() -> None:
    directory = data_dir(os.environ)
    for name, entry in DATASETS.items():
        path = fetch(name, directory)
        print(f"{name}: {path.name}, {entry['bytes']} bytes, SHA-256 verified, {entry['licence']}", file=sys.stderr)


if __name__ == "__main__":
    main()
