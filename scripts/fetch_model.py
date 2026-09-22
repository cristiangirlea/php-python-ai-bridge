"""Fetch explicit data files from pinned public revisions. Never load remote code."""

import hashlib
import json
from pathlib import Path
from urllib.request import urlopen

# One directory per task under /cache/models. Hashes are SHA-256 of the exact files at the revision.
MODELS = {
    "rerank": {
        "repository": "cross-encoder/ms-marco-TinyBERT-L2-v2",
        "revision": "81d1926f67cb8eee2c2be17ca9f793c7c3bd20cc",
        "license": "Apache-2.0",
        "files": {"tokenizer.json": "tokenizer.json", "onnx/model.onnx": "model.onnx"},
        "hashes": {
            "tokenizer.json": "d241a60d5e8f04cc1b2b3e9ef7a4921b27bf526d9f6050ab90f9267a1f9e5c66",
            "model.onnx": "0eac39ee56a3edf98d0beee17fa1bb368a5a1d8c8671b5afd6dd71677f8d8496",
        },
    },
    "embed": {
        "repository": "sentence-transformers/all-MiniLM-L6-v2",
        "revision": "1110a243fdf4706b3f48f1d95db1a4f5529b4d41",
        "license": "Apache-2.0",
        "files": {"tokenizer.json": "tokenizer.json", "onnx/model.onnx": "model.onnx"},
        "hashes": {
            "tokenizer.json": "be50c3628f2bf5bb5e3a7f17b1f74611b2561a3a27eeab05e5aa30f411572037",
            "model.onnx": "6fd5d72fe4589f189f8ebc006442dbb529bb7ce38f8082112682524616046452",
        },
    },
}
LIMIT = 100 * 1024 * 1024


def fetch(task: str, spec: dict) -> dict:
    directory = Path("/cache/models") / task
    directory.mkdir(parents=True, exist_ok=True)
    repository, revision = spec["repository"], spec["revision"]
    manifest = {"repository": repository, "revision": revision, "license": spec["license"], "files": {}}
    for source, destination in spec["files"].items():
        url = f"https://huggingface.co/{repository}/resolve/{revision}/{source}"
        temporary = directory / (destination + ".partial")
        total = 0
        digest = hashlib.sha256()
        with urlopen(url, timeout=60) as response, temporary.open("wb") as output:
            while chunk := response.read(1024 * 1024):
                total += len(chunk)
                if total > LIMIT:
                    raise ValueError(f"Model artifact exceeds the {LIMIT // (1024 * 1024)} MiB limit")
                digest.update(chunk)
                output.write(chunk)
        if digest.hexdigest() != spec["hashes"][destination]:
            raise ValueError("Model artifact failed SHA-256 verification")
        temporary.replace(directory / destination)
        manifest["files"][destination] = {"sha256": digest.hexdigest(), "bytes": total}
    (directory / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest


def main():
    print(json.dumps({task: fetch(task, spec) for task, spec in MODELS.items()}, indent=2))


if __name__ == "__main__":
    main()
