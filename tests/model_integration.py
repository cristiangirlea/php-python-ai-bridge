"""Small model smoke test, not a ranking-quality benchmark."""

import time

from integration import finished, request, wait_ready


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
    texts = ["A dog barks loudly.", "Puppies make barking noises.", "The stock market fell today."]
    status, job, _ = request("/embed", {"texts": texts})
    assert status == 202
    result = finished(job["id"])["result"]
    assert result["model"] == "sentence-transformers/all-MiniLM-L6-v2" and result["dimensions"] == 384
    dot = lambda a, b: sum(x * y for x, y in zip(a, b))
    assert dot(result["vectors"][0], result["vectors"][1]) > dot(result["vectors"][0], result["vectors"][2])
    print("PASS: 2 real ONNX ranking cases and 1 embedding case through HTTP -> FrankenPHP worker -> PHP client -> Python model")


if __name__ == "__main__":
    main()
