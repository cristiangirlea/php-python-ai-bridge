"""Small model smoke test, not a ranking-quality benchmark."""

from integration import finished, request, wait_ready
from model_cases import REDACT_TEXT, RERANK_CASES, SIMILAR_TEXTS, UNCASED_MASKED, UNCASED_TEXT


FRANCE = ["Bananas are yellow fruit.", "Paris is the capital of France.", "A car has four wheels."]


def batch_set():
    """70 documents, so three batches of 32, 32 and 6, with the three FRANCE documents at 5, 40 and 66.

    Distractors vary from a few words to several hundred, so batches are padded to very different widths.
    """
    documents = []
    for i in range(70):
        words = ("gardening", "tomatoes", "rainfall", "recipes", "compost", "seedlings")
        documents.append(" ".join(words[j % 6] for j in range(3 + (i * 37) % 290)) + f" note {i}.")
    documents[5], documents[40], documents[66] = FRANCE[0], FRANCE[1], FRANCE[2]
    return documents


def scores_by_index(rankings):
    return {item["index"]: item["score"] for item in rankings}


def main():
    wait_ready()
    for query, documents, expected in RERANK_CASES:
        status, job, _ = request("/rerank", {"query": query, "documents": documents})
        assert status == 202
        done = finished(job["id"])
        assert done["result"]["model"] == "cross-encoder/ms-marco-TinyBERT-L2-v2"
        assert done["result"]["rankings"][0]["index"] == expected
    # Batched scoring: the relevant document sits in the second batch, and padding must not move any score.
    query = "What is the capital of France?"
    status, job, _ = request("/rerank", {"query": query, "documents": FRANCE})
    assert status == 202
    alone = scores_by_index(finished(job["id"])["result"]["rankings"])
    status, job, _ = request("/rerank", {"query": query, "documents": batch_set()})
    assert status == 202
    done = finished(job["id"], 120)
    assert done["result"]["rankings"][0]["index"] == 40, done["result"]["rankings"][:3]
    assert done["progress"] == {"completed": 70, "total": 70}, done["progress"]
    together = scores_by_index(done["result"]["rankings"])
    for position, index in enumerate((5, 40, 66)):
        assert abs(alone[position] - together[index]) < 1e-3, (position, alone[position], together[index])
    assert sorted(range(3), key=lambda i: -alone[i]) == sorted(range(3), key=lambda i: -together[(5, 40, 66)[i]])
    texts = SIMILAR_TEXTS
    status, job, _ = request("/embed", {"texts": texts})
    assert status == 202
    result = finished(job["id"])["result"]
    assert result["model"] == "sentence-transformers/all-MiniLM-L6-v2" and result["dimensions"] == 384
    dot = lambda a, b: sum(x * y for x, y in zip(a, b))
    assert dot(result["vectors"][0], result["vectors"][1]) > dot(result["vectors"][0], result["vectors"][2])
    status, job, _ = request("/redact", {"text": REDACT_TEXT, "entities": ["PER", "LOC"]})
    assert status == 202
    result = finished(job["id"])["result"]
    assert result["model"] == "Xenova/bert-base-NER:int8+Xenova/bert-base-NER-uncased:int8", result["model"]
    assert result["text"] == "\u2014 [PER] wrote to [EMAIL] about [LOC].", result["text"]
    assert [s["source"] for s in result["spans"]] == ["model:PER", "rule:email", "model:LOC"], result["spans"]
    assert all(0 < s["score"] <= 1 for s in result["spans"])
    status, job, _ = request("/redact", {"text": UNCASED_TEXT, "entities": ["PER"], "min_score": 0.5})
    assert status == 202
    result = finished(job["id"])["result"]
    assert result["text"] == UNCASED_MASKED, result
    assert [s["source"] for s in result["spans"]] == ["model:PER"], result["spans"]
    # Precision guard: ordinary text must not grow spans.
    status, job, _ = request("/redact", {"text": "The weather is nice today and the meeting starts at noon."})
    assert finished(job["id"])["result"]["spans"] == []
    print("PASS: 2 real ONNX ranking cases, a 70-document batched ranking with score parity, 1 embedding case and 3 redaction cases through HTTP -> FrankenPHP worker -> PHP client -> Python model")


if __name__ == "__main__":
    main()
