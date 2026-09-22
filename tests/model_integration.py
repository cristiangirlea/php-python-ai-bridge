"""Small model smoke test, not a ranking-quality benchmark."""

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
        done = finished(job["id"])
        assert done["result"]["model"] == "cross-encoder/ms-marco-TinyBERT-L2-v2"
        assert done["result"]["rankings"][0]["index"] == expected
    texts = ["A dog barks loudly.", "Puppies make barking noises.", "The stock market fell today."]
    status, job, _ = request("/embed", {"texts": texts})
    assert status == 202
    result = finished(job["id"])["result"]
    assert result["model"] == "sentence-transformers/all-MiniLM-L6-v2" and result["dimensions"] == 384
    dot = lambda a, b: sum(x * y for x, y in zip(a, b))
    assert dot(result["vectors"][0], result["vectors"][1]) > dot(result["vectors"][0], result["vectors"][2])
    # The leading dash is one code point of three bytes: byte-based model offsets would misplace every mask.
    status, job, _ = request("/redact", {"text": "\u2014 John Smith wrote to john@example.com about Berlin.", "entities": ["PER", "LOC"]})
    assert status == 202
    result = finished(job["id"])["result"]
    assert result["model"] == "Xenova/bert-base-NER:int8", result["model"]
    assert result["text"] == "\u2014 [PER] wrote to [EMAIL] about [LOC].", result["text"]
    assert [s["source"] for s in result["spans"]] == ["model:PER", "rule:email", "model:LOC"], result["spans"]
    assert all(0 < s["score"] <= 1 for s in result["spans"])
    # Precision guard: ordinary text must not grow spans.
    status, job, _ = request("/redact", {"text": "The weather is nice today and the meeting starts at noon."})
    assert finished(job["id"])["result"]["spans"] == []
    print("PASS: 2 real ONNX ranking cases, 1 embedding case and 2 redaction cases through HTTP -> FrankenPHP worker -> PHP client -> Python model")


if __name__ == "__main__":
    main()
