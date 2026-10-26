"""Informational precision and recall of the redact task's NER pass on an invented, labelled sample or WNUT-17.

Runs every sentence in tests/fixtures/ner-sample.json through the ONNX worker's redact task with PER, ORG and LOC,
once per threshold, and compares the model's spans (rule spans are not NER) with the sample's labels. Each threshold
is its own job: a lower one lets weaker spans into the worker's merge, where they can displace stronger ones. It asserts
only that every job succeeds with spans inside its text, never a score: this is a small invented sample, so the
figures describe this model on these registers, not a quality guarantee. The output is a Markdown block for
docs/testing.md; progress goes to stderr.

NER_DATASET=wnut17 measures the WNUT-17 test split instead, which scripts/public_data.py pins and compose's
quality-fetcher stores: its person, corporation and group, and location entities as PER, ORG and LOC, with precision,
recall and F1 per label and over all three.

    NER_TARGET=http://model-worker:8090 BRIDGE_TOKEN=... python scripts/ner_measure.py
"""

import datetime
import json
import os
import sys
import time
from pathlib import Path
from urllib.error import HTTPError

sys.path.insert(0, str(Path(__file__).resolve().parent))
import public_data  # noqa: E402
from benchmark import POLL_S, TERMINAL, call, cpu_model, wait_ready  # noqa: E402  (the same protocol helpers)

REPO = Path(__file__).resolve().parents[1]
SAMPLE = REPO / "tests" / "fixtures" / "ner-sample.json"
LABELS = ("PER", "ORG", "LOC")
# The worker counts finished jobs against its capacity for 300 s by default; wait that out rather than fail.
CAPACITY_WAIT_S, CAPACITY_RETRIES = 10, 40
THRESHOLDS = (0.85, 0.5)  # the task's default, and a lower one to show what it trades
# WNUT-17's product and creative-work types have no counterpart among the task's labels.
WNUT_LABELS = {"person": "PER", "corporation": "ORG", "group": "ORG", "location": "LOC"}


def sample_path(environ) -> Path:
    """The published sample, or NER_SAMPLE, such as the development set, relative to the repository."""
    return REPO / environ["NER_SAMPLE"] if environ.get("NER_SAMPLE") else SAMPLE


def dataset(environ) -> str:
    name = environ.get("NER_DATASET", "")
    if name not in ("", "wnut17"):
        raise SystemExit("NER_DATASET must be empty or wnut17")
    if name and environ.get("NER_SAMPLE"):
        raise SystemExit("NER_SAMPLE names an invented sample; leave it empty with NER_DATASET")
    return name


def parse_conll(text: str) -> list:
    """Sentences of a two-column CoNLL file (a token, a tab and a BIO tag per line, a blank line between sentences),
    each as an item whose text is its tokens joined by single spaces and whose gold spans are the offsets of the
    entities WNUT_LABELS maps. An I- tag that does not continue an entity of its own type starts one."""
    items, sentence = [], []
    for number, line in enumerate(text.splitlines() + [""], start=1):
        if not line.strip():
            if sentence:
                items.append(conll_item(sentence))
            sentence = []
            continue
        token, tab, tag = line.rpartition("\t")
        if not tab or not token or not (tag == "O" or tag[:2] in ("B-", "I-") and len(tag) > 2):
            raise ValueError(f"line {number} is not a token, a tab and a BIO tag: {line!r}")
        sentence.append((token, tag))
    return items


def conll_item(sentence: list) -> dict:
    text, entities, current = "", [], None
    for token, tag in sentence:
        start = len(text) + 1 if text else 0
        text = f"{text} {token}" if text else token
        kind = tag[2:]
        if tag.startswith("I-") and current and current[2] == kind:
            current[1] = len(text)
        else:
            current = [start, len(text), kind] if tag != "O" else None
            if current:
                entities.append(current)
    return {"register": "all labels", "text": text,
            "gold": [(start, end, WNUT_LABELS[kind]) for start, end, kind in entities if kind in WNUT_LABELS]}


def gold_spans(item: dict) -> list:
    """A parsed dataset's offsets, or the invented sample's, found from each entity's text."""
    if "gold" in item:
        return list(item["gold"])
    return [(item["text"].index(text), item["text"].index(text) + len(text), label) for label, text in item["entities"]]


def predictions(spans: list) -> list:
    return [(span["start"], span["end"], span["label"]) for span in spans if span["source"].startswith("model:")]


def matched(gold: list, predicted: list, same_label: bool) -> list:
    """Gold spans overlapped by a predicted span, each predicted span matching one gold span at most."""
    unused, found = list(predicted), []
    for start, end, label in gold:
        match = next((p for p in unused if (p[2] == label or not same_label) and p[0] < end and start < p[1]), None)
        if match is not None:
            unused.remove(match)
            found.append((start, end, label))
    return found


def score(gold: list, predicted: list, masked: list | None = None) -> dict:
    """Exact: same label and boundaries. Overlapping: same label and a shared character, one gold span per predicted
    span, so one span over two names finds one of them. Masked: a shared character under any label, which is what a
    redaction hides; masked, when given, is that match already made over the whole sentence."""
    return {"gold": len(gold), "predicted": len(predicted), "exact": len(set(gold) & set(predicted)),
            "overlapping": len(matched(gold, predicted, True)),
            "masked": len(matched(gold, predicted, False) if masked is None else masked)}


def rate(part: int, whole: int) -> str:
    return f"{part / whole:.2f}" if whole else "n/a"


def f1(part: int, gold: int, predicted: int) -> str:
    """The harmonic mean of part / predicted and part / gold."""
    return f"{2 * part / (gold + predicted):.2f}" if gold + predicted else "n/a"


def f1_table(rows: list) -> str:
    lines = ["| Threshold | Label | Gold | Predicted | Precision, exact | Recall, exact | F1, exact "
             "| Precision, overlapping | Recall, overlapping | F1, overlapping | Masked |",
             "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for threshold, name, c in rows:
        lines.append(f"| {threshold} | {name} | {c['gold']} | {c['predicted']} | "
                     f"{rate(c['exact'], c['predicted'])} | {rate(c['exact'], c['gold'])} | "
                     f"{f1(c['exact'], c['gold'], c['predicted'])} | "
                     f"{rate(c['overlapping'], c['predicted'])} | {rate(c['overlapping'], c['gold'])} | "
                     f"{f1(c['overlapping'], c['gold'], c['predicted'])} | {rate(c['masked'], c['gold'])} |")
    return "\n".join(lines)


def finish(base: str, body: dict) -> dict:
    """Submits a job, waiting out a full worker, and returns its result; a job that does not succeed ends the run."""
    for attempt in range(CAPACITY_RETRIES + 1):
        try:
            job = call(base, "POST", "/v1/jobs", body)
            break
        except HTTPError as error:
            if error.code != 429:
                raise
            if attempt == CAPACITY_RETRIES:
                raise SystemExit(f"the worker stayed at capacity for {CAPACITY_RETRIES * CAPACITY_WAIT_S} s") from None
            time.sleep(CAPACITY_WAIT_S)
    while job["status"] not in TERMINAL:
        time.sleep(POLL_S)
        job = call(base, "GET", "/v1/jobs/" + job["id"])
    if job["status"] != "succeeded":
        raise SystemExit(f"{body['task']} did not succeed: {job['status']} {job.get('error')}")
    return job["result"]


def redact(base: str, text: str, threshold: float) -> dict:
    result = finish(base, {"task": "redact", "timeout_ms": 120000,
                           "input": {"text": text, "entities": list(LABELS), "min_score": threshold}})
    if any(not 0 <= span["start"] < span["end"] <= len(text) for span in result["spans"]):
        raise SystemExit(f"redact returned a span outside its text: {result['spans']}")
    return result


def add(total: dict, counts: dict) -> None:
    for key, value in counts.items():
        total[key] = total.get(key, 0) + value


def tally(items: list, spans: dict) -> list:
    """Rows of (threshold, register or label, counts) from each threshold's spans per item: each register in the
    sample's order, then each label, whose rows count only that label's gold and predicted spans, except that the
    sentence's masked gold spans, matched once under any label, are each counted in their own label's row."""
    registers = list(dict.fromkeys(item["register"] for item in items))
    rows = []
    for threshold, found_per_item in spans.items():
        totals = {name: {} for name in registers + list(LABELS)}
        for item, found in zip(items, found_per_item):
            gold, predicted = gold_spans(item), predictions(found)
            hidden = matched(gold, predicted, False)
            add(totals[item["register"]], score(gold, predicted, hidden))
            for label in LABELS:
                add(totals[label], score([g for g in gold if g[2] == label], [p for p in predicted if p[2] == label],
                                         [g for g in hidden if g[2] == label]))
        rows += [(threshold, name, counts) for name, counts in totals.items()]
    return rows


def main() -> None:
    base = os.environ.get("NER_TARGET", "http://model-worker:8090")
    health = wait_ready(base)
    if health["backend"] != "onnx":
        raise SystemExit(f"{base} reports backend {health['backend']!r}; NER needs the onnx worker")
    wnut = dataset(os.environ) == "wnut17"
    if wnut:
        items = parse_conll(public_data.read("wnut17", public_data.data_dir(os.environ)).decode("utf-8"))
    else:
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
    if wnut:
        pinned = public_data.DATASETS["wnut17"]
        print(f"""Measured {now} UTC, commit `{os.environ.get("BENCH_COMMIT", "unknown")}`, runner: {os.environ.get("BENCH_RUNNER", "unspecified")}, CPU: {cpu_model()}.
Model {", ".join(f"`{model}`" for model in sorted(models))}; WNUT-17 test split, `{pinned["url"].rsplit("/", 1)[1]}` at SHA-256 `{pinned["sha256"][:12]}`, {pinned["licence"]}: {len(items)} sentences with {sum(len(item["gold"]) for item in items)} entities mapped to labels, person to PER, corporation and group to ORG, location to LOC; product and creative-work have no label here, so a span over one counts against precision. Each sentence is its tokens joined by single spaces; one redact job per sentence and threshold, entities PER, ORG and LOC. Exact: same label and boundaries. Overlapping: same label and a shared character, one gold span per predicted span. Masked: gold spans that share a character with a predicted span of any label, one gold span per predicted span, in a label's row too.
""")
        print(f1_table(tally(items, spans)))
        return
    entities = sum(len(item["entities"]) for item in items)
    print(f"""Measured {now} UTC, commit `{os.environ.get("BENCH_COMMIT", "unknown")}`, runner: {os.environ.get("BENCH_RUNNER", "unspecified")}, CPU: {cpu_model()}.
Model {", ".join(f"`{model}`" for model in sorted(models))}; `{sample.relative_to(REPO).as_posix()}`, {len(items)} invented sentences with {entities} labelled entities; one redact job per sentence and threshold, entities PER, ORG and LOC. Exact: same label and boundaries. Overlapping: same label and a shared character, one gold span per predicted span. Masked: a shared character with a predicted span of any label, in a label's row too, one gold span per predicted span: the share of entities a redaction hides.

| Threshold | Register or label | Gold | Predicted | Precision, exact | Recall, exact | Precision, overlapping | Recall, overlapping | Masked |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |""")
    for threshold, name, counts in tally(items, spans):
        print(f"| {threshold} | {name} | {counts['gold']} | {counts['predicted']} | "
              f"{rate(counts['exact'], counts['predicted'])} | {rate(counts['exact'], counts['gold'])} | "
              f"{rate(counts['overlapping'], counts['predicted'])} | {rate(counts['overlapping'], counts['gold'])} | "
              f"{rate(counts['masked'], counts['gold'])} |")


if __name__ == "__main__":
    main()
