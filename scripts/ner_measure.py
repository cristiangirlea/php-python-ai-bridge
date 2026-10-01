"""Informational precision and recall of the redact task's NER pass on an invented, labelled sample.

Runs every sentence in tests/fixtures/ner-sample.json through the ONNX worker's redact task, with PER, ORG and LOC
and two thresholds, and compares the model's spans (rule spans are not NER) with the sample's labels. It asserts
only that every job succeeds with spans inside its text, never a score: this is a small invented sample, so the
figures describe this model on these registers, not a quality guarantee. The output is a Markdown block for
docs/testing.md; progress goes to stderr.

    NER_TARGET=http://model-worker:8090 BRIDGE_TOKEN=... python scripts/ner_measure.py
"""

import datetime
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from benchmark import POLL_S, TERMINAL, call, cpu_model, wait_ready  # noqa: E402  (the same protocol helpers)

SAMPLE = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "ner-sample.json"
LABELS = ("PER", "ORG", "LOC")
THRESHOLDS = (0.85, 0.5)  # the task's default, and a lower one to show what it trades


def gold_spans(item: dict) -> list:
    return [(item["text"].index(text), item["text"].index(text) + len(text), label) for label, text in item["entities"]]


def predictions(spans: list) -> list:
    return [(span["start"], span["end"], span["label"]) for span in spans if span["source"].startswith("model:")]


def score(gold: list, predicted: list) -> dict:
    """Exact: same label and boundaries. Overlapping: same label and any shared character, counted for the gold
    spans found (recall) and the predicted spans that are right (precision)."""
    def overlaps(a, b):
        return a[2] == b[2] and a[0] < b[1] and b[0] < a[1]

    return {"gold": len(gold), "predicted": len(predicted), "exact": len(set(gold) & set(predicted)),
            "found": sum(any(overlaps(g, p) for p in predicted) for g in gold),
            "right": sum(any(overlaps(p, g) for g in gold) for p in predicted)}


def rate(part: int, whole: int) -> str:
    return f"{part / whole:.2f}" if whole else "n/a"


def redact(base: str, text: str, threshold: float) -> dict:
    job = call(base, "POST", "/v1/jobs", {"task": "redact", "timeout_ms": 120000,
                                          "input": {"text": text, "entities": list(LABELS), "min_score": threshold}})
    while job["status"] not in TERMINAL:
        time.sleep(POLL_S)
        job = call(base, "GET", "/v1/jobs/" + job["id"])
    if job["status"] != "succeeded":
        raise SystemExit(f"redact did not succeed: {job['status']} {job.get('error')}")
    result = job["result"]
    if any(not 0 <= span["start"] < span["end"] <= len(text) for span in result["spans"]):
        raise SystemExit(f"redact returned a span outside its text: {result['spans']}")
    return result


def add(total: dict, counts: dict) -> None:
    for key, value in counts.items():
        total[key] = total.get(key, 0) + value


def main() -> None:
    base = os.environ.get("NER_TARGET", "http://model-worker:8090")
    health = wait_ready(base)
    if health["backend"] != "onnx":
        raise SystemExit(f"{base} reports backend {health['backend']!r}; NER needs the onnx worker")
    items = json.loads(SAMPLE.read_text(encoding="utf-8"))["items"]
    registers = sorted({item["register"] for item in items}, key=["news", "records", "informal"].index)
    by_register, by_label, models = {}, {}, set()
    for threshold in THRESHOLDS:
        for number, item in enumerate(items, start=1):
            result = redact(base, item["text"], threshold)
            models.add(result["model"])
            gold, predicted = gold_spans(item), predictions(result["spans"])
            add(by_register.setdefault((threshold, item["register"]), {}), score(gold, predicted))
            for label in LABELS:
                add(by_label.setdefault((threshold, label), {}),
                    score([g for g in gold if g[2] == label], [p for p in predicted if p[2] == label]))
            print(f"threshold {threshold} sentence {number}/{len(items)}", file=sys.stderr)
    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
    entities = sum(len(item["entities"]) for item in items)
    print(f"""Measured {now} UTC, commit `{os.environ.get("BENCH_COMMIT", "unknown")}`, runner: {os.environ.get("BENCH_RUNNER", "unspecified")}, CPU: {cpu_model()}.
Model {", ".join(f"`{model}`" for model in sorted(models))}; {len(items)} invented sentences with {entities} labelled entities; one redact job per sentence and threshold, entities PER, ORG and LOC. Exact: same label and boundaries. Overlapping: same label and any shared character.

| Threshold | Register or label | Gold | Predicted | Precision, exact | Recall, exact | Precision, overlapping | Recall, overlapping |
| --- | --- | --- | --- | --- | --- | --- | --- |""")
    for threshold in THRESHOLDS:
        rows = [(register, by_register[(threshold, register)]) for register in registers]
        rows += [(label, by_label[(threshold, label)]) for label in LABELS]
        for name, counts in rows:
            print(f"| {threshold} | {name} | {counts['gold']} | {counts['predicted']} | "
                  f"{rate(counts['exact'], counts['predicted'])} | {rate(counts['exact'], counts['gold'])} | "
                  f"{rate(counts['right'], counts['predicted'])} | {rate(counts['found'], counts['gold'])} |")


if __name__ == "__main__":
    main()
