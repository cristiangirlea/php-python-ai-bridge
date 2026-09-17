"""Fetch explicit data files from a pinned public revision. Never load remote code."""

import hashlib
import json
from pathlib import Path
from urllib.request import urlopen

REPOSITORY = "cross-encoder/ms-marco-TinyBERT-L2-v2"
REVISION = "81d1926f67cb8eee2c2be17ca9f793c7c3bd20cc"
FILES = {"tokenizer.json": "tokenizer.json", "onnx/model.onnx": "model.onnx"}
HASHES = {
    "tokenizer.json": "d241a60d5e8f04cc1b2b3e9ef7a4921b27bf526d9f6050ab90f9267a1f9e5c66",
    "model.onnx": "0eac39ee56a3edf98d0beee17fa1bb368a5a1d8c8671b5afd6dd71677f8d8496",
}


def main():
    directory = Path("/cache/models")
    directory.mkdir(parents=True, exist_ok=True)
    manifest = {"repository": REPOSITORY, "revision": REVISION, "license": "Apache-2.0", "files": {}}
    for source, destination in FILES.items():
        url = f"https://huggingface.co/{REPOSITORY}/resolve/{REVISION}/{source}"
        temporary = directory / (destination + ".partial")
        total = 0
        digest = hashlib.sha256()
        with urlopen(url, timeout=60) as response, temporary.open("wb") as output:
            while chunk := response.read(1024 * 1024):
                total += len(chunk)
                if total > 100 * 1024 * 1024:
                    raise ValueError("Model artifact exceeds the 100 MiB limit")
                digest.update(chunk)
                output.write(chunk)
        if digest.hexdigest() != HASHES[destination]:
            raise ValueError("Model artifact failed SHA-256 verification")
        temporary.replace(directory / destination)
        manifest["files"][destination] = {"sha256": digest.hexdigest(), "bytes": total}
    (directory / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
