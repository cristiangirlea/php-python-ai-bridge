"""Small model smoke test, not a ranking-quality benchmark."""

import time

from integration import request, wait_ready


def main():
    wait_ready()
    cases = [
        ("What is the capital of France?", ["Bananas are yellow.", "Paris is France's capital.", "Cars have wheels."], 1),
        ("Which animal barks?", ["Dogs bark to communicate.", "Whales live in water.", "Paris is a city."], 0),
    ]
    for query, documents, expected in cases:
        status, job, _ = request("/rerank", {"query": query, "documents": documents})
        assert status == 202
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            _, done, _ = request("/jobs/" + job["id"])
            if done["status"] == "succeeded":
                assert done["result"]["model"] == "cross-encoder/ms-marco-TinyBERT-L2-v2"
                assert done["result"]["rankings"][0]["index"] == expected
                break
            assert done["status"] in {"queued", "running"}, done
            time.sleep(0.05)
        else:
            raise AssertionError("Model task timed out")
    print("PASS: 2 real ONNX cases through HTTP -> FrankenPHP worker -> PHP client -> Python model")


if __name__ == "__main__":
    main()
