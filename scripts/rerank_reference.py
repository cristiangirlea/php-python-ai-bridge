"""Regenerate fp32 parity references in a disposable, network-enabled model container."""

import hashlib
import json
import sys
import tempfile
from pathlib import Path
from urllib.request import urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "worker"))
from ai_bridge import tasks  # noqa: E402
from fetch_model import MODELS  # noqa: E402

CASES = [
    {"query": "What is the capital of France?", "documents": [
        "Paris is the capital and most populous city of France.",
        "France is a country in Western Europe, bordering Germany and Spain.",
        "Berlin is the capital of Germany.",
        "Bananas are yellow fruits that grow in tropical climates.",
    ]},
    {"query": "How do antibiotics treat bacterial infections?", "documents": [
        "Antibiotics treat bacterial infections by killing bacteria or preventing their growth.",
        "Bacterial infections can cause fever, swelling and pain.",
        "Viruses reproduce inside living cells and are not killed by antibiotics.",
        "The Moon orbits Earth approximately once every twenty-seven days.",
    ]},
]


def main() -> None:
    result = {"reference_file": "onnx/model.onnx", "precision": "fp32",
              "absolute_logit_tolerance": {"minilm-l6": 0.75, "bge-base": 1.25}, "cases": CASES, "models": {}}
    for choice in ("minilm-l6", "bge-base"):
        directory, _, batch = tasks.RERANK_MODELS[choice]
        spec = MODELS[directory]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for source, destination in (("tokenizer.json", "tokenizer.json"), ("onnx/model.onnx", "model.onnx")):
                url = f'https://huggingface.co/{spec["repository"]}/resolve/{spec["revision"]}/{source}'
                digest, size = hashlib.sha256(), 0
                with urlopen(url, timeout=120) as response, (root / destination).open("wb") as output:
                    while chunk := response.read(1024 * 1024):
                        size += len(chunk)
                        if size > 2 * 1024**3:
                            raise ValueError("reference file exceeds 2 GiB")
                        digest.update(chunk)
                        output.write(chunk)
                print(f"{choice} {source}: {size} bytes SHA-256 {digest.hexdigest()}", file=sys.stderr)
            tokenizer, session, names = tasks._load_onnx(root, 512, 0)
            scores = [tasks._score_pairs(tokenizer, session, names, case["query"], case["documents"],
                                        lambda _: None, batch_size=batch) for case in CASES]
            result["models"][choice] = {"repository": spec["repository"], "revision": spec["revision"],
                                       "sha256": digest.hexdigest(), "scores": scores}
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
