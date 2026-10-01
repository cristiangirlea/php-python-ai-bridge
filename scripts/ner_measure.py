"""Informational precision and recall of the redact task's NER pass on an invented, labelled sample.

Runs every sentence in tests/fixtures/ner-sample.json through the ONNX worker's redact task with PER, ORG and LOC,
once per threshold, and compares the model's spans (rule spans are not NER) with the sample's labels. Each threshold
is its own job: a lower one lets weaker spans into the worker's merge, where they can displace stronger ones. It asserts
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

REPO = Path(__file__).resolve().parents[1]
SAMPLE = REPO / "tests" / "fixtures" / "ner-sample.json"
LABELS = ("PER", "ORG", "LOC")
THRESHOLDS = (0.85, 0.5)  # the task's default, and a lower one to show what it trades


def sample_path(environ) -> Path:
    """The published sample, or NER_SAMPLE, such as the development set, relative to the repository."""
    return REPO / environ["NER_SAMPLE"] if environ.get("NER_SAMPLE") else SAMPLE


def gold_spans(item: dict) -> list:
    return [(item["text"].index(text), item["text"].index(text) + len(text), label) for label, text in item["entities"]]


def predictions(spans: list) -> list:
    return [(span["start"], span["end"], span["label"]) for span in spans if span["source"].startswith("model:")]


def matched(gold: list, predicted: list, same_label: bool) -> int:
    """Gold spans overlapped by a predicted span, each predicted span matching one gold span at most."""
    unused, count = list(predicted), 0
    for start, end, label in gold:
        match = next((p for p in unused if (p[2] == label or not same_label) and p[0] < end and start < p[1]), None)
        if match is not None:
            unused.remove(match)
            count += 1
    return count


def score(gold: list, predicted: list) -> dict:
    """Exact: same label and boundaries. Overlapping: same label and a shared character, one gold span per predicted
    span, so one span over two names finds one of them. Masked: a shared character under any label, which is what a
    redaction hides."""
    return {"gold": len(gold), "predicted": len(predicted), "exact": len(set(gold) & set(predicted)),
            "overlapping": matched(gold, predicted, True), "masked": matched(gold, predicted, False)}


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


def tally(items: list, spans: dict) -> list:
    """Rows of (threshold, register or label, counts) from each threshold's spans per item: each register in the
    sample's order, then each label, whose rows count only that label's gold and predicted spans."""
    registers = list(dict.fromkeys(item["register"] for item in items))
    rows = []
    for threshold, found_per_item in spans.items():
        totals = {name: {} for name in registers + list(LABELS)}
        for item, found in zip(items, found_per_item):
            gold, predicted = gold_spans(item), predictions(found)
            add(totals[item["register"]], score(gold, predicted))
            for label in LABELS:
                add(totals[label], score([g for g in gold if g[2] == label], [p for p in predicted if p[2] == label]))
        rows += [(threshold, name, counts) for name, counts in totals.items()]
    return rows


def main() -> None:
    base = os.environ.get("NER_TARGET", "http://model-worker:8090")
    health = wait_ready(base)
    if health["backend"] != "onnx":
        raise SystemExit(f"{base} reports backend {health['backend']!r}; NER needs the onnx worker")
    sample = sample_path(os.environ)
    items = json.loads(sample.read_text(encoding="utf-8"))["items"]
    spans, models = {threshold: [] for threshold in THRESHOLDS}, set()
    for threshold in THRESHOLDS:
        for number, item in enumerate(items, start=1):
            result = redact(base, item["text"], threshold)
            models.add(result["model"])
            spans[threshold].append(result["spans"])
            print(f"threshold {threshold} sentence {number}/{len(items)}", file=sys.stderr)
    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
    entities = sum(len(item["entities"]) for item in items)
    print(f"""Measured {now} UTC, commit `{os.environ.get("BENCH_COMMIT", "unknown")}`, runner: {os.environ.get("BENCH_RUNNER", "unspecified")}, CPU: {cpu_model()}.
Model {", ".join(f"`{model}`" for model in sorted(models))}; `{sample.relative_to(REPO).as_posix()}`, {len(items)} invented sentences with {entities} labelled entities; one redact job per sentence and threshold, entities PER, ORG and LOC. Exact: same label and boundaries. Overlapping: same label and a shared character, one gold span per predicted span. Masked: a shared character under any label, the share of entities a redaction hides.

| Threshold | Register or label | Gold | Predicted | Precision, exact | Recall, exact | Precision, overlapping | Recall, overlapping | Masked |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |""")
    for threshold, name, counts in tally(items, spans):
        print(f"| {threshold} | {name} | {counts['gold']} | {counts['predicted']} | "
              f"{rate(counts['exact'], counts['predicted'])} | {rate(counts['exact'], counts['gold'])} | "
              f"{rate(counts['overlapping'], counts['predicted'])} | {rate(counts['overlapping'], counts['gold'])} | "
              f"{rate(counts['masked'], counts['gold'])} |")


if __name__ == "__main__":
    main()
