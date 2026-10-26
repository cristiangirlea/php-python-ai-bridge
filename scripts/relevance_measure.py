"""Informational ranking quality of the rerank task on a public dataset, against the BM25 order it reranks.

Reads BEIR's copy of ArguAna, which scripts/public_data.py pins and compose's quality-fetcher stores, takes 200 of its
test queries evenly spaced in file order, ranks the whole corpus for each with the standard-library BM25 below and
sends its top 100 to the worker's rerank task, one job per query. It reports NDCG@10 and MRR@10 of the BM25 order and
of the reranked order, and the wall time of each. It asserts only that every job succeeds and returns each candidate
once, never a score. The output is a Markdown block for docs/testing.md; progress goes to stderr.

    RELEVANCE_TARGET=http://model-worker:8090 BRIDGE_TOKEN=... python scripts/relevance_measure.py
"""

import datetime
import io
import json
import math
import os
import re
import statistics
import sys
import time
import zipfile
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "worker"))
import public_data  # noqa: E402

# The worker's own limits, so that no job is refused for its size.
from ai_bridge.server import MAX_BODY  # noqa: E402
from ai_bridge.tasks import MAX_DOCUMENTS, MAX_FIELD_CHARACTERS, MAX_TOTAL_CHARACTERS  # noqa: E402, F401  (the tests hold CANDIDATES to MAX_DOCUMENTS)
from benchmark import cpu_model, wait_ready  # noqa: E402
from ner_measure import finish  # noqa: E402  (submits a job and waits out a full worker)

DATASET = "arguana"
QUERIES, CANDIDATES, K = 200, 100, 10
K1, B = 1.2, 0.75  # Lucene's defaults
WORD = re.compile(r"\w+")


def tokens(text: str) -> list:
    return WORD.findall(text.casefold())


class BM25:
    """Okapi BM25 over lower-cased word tokens, with no stemming and no stop words, and Lucene's idf, which stays
    positive: idf = ln(1 + (N - df + 0.5) / (df + 0.5)). A document scores the sum over the query's words, each
    counted as often as the query repeats it, of idf * tf * (k1 + 1) / (tf + k1 * (1 - b + b * length / average))."""

    def __init__(self, documents: list, k1: float = K1, b: float = B):
        self.k1, self.b, self.lengths, self.postings = k1, b, [], {}
        for index, document in enumerate(documents):
            counts = Counter(tokens(document))
            self.lengths.append(sum(counts.values()))
            for word, tf in counts.items():
                self.postings.setdefault(word, []).append((index, tf))
        self.average = sum(self.lengths) / len(self.lengths) if self.lengths else 0.0
        self.idf = {word: math.log(1 + (len(documents) - len(found) + 0.5) / (len(found) + 0.5))
                    for word, found in self.postings.items()}

    def scores(self, query: str) -> dict:
        found = {}
        for word, count in Counter(tokens(query)).items():
            for index, tf in self.postings.get(word, ()):
                norm = tf + self.k1 * (1 - self.b + self.b * self.lengths[index] / self.average)
                found[index] = found.get(index, 0.0) + count * self.idf[word] * tf * (self.k1 + 1) / norm
        return found

    def top(self, query: str, k: int, skip=frozenset()) -> list:
        """The k best document positions, best first, ties by position; a document sharing no word never ranks."""
        ranked = sorted((-score, index) for index, score in self.scores(query).items() if index not in skip)
        return [index for _, index in ranked[:k]]


def candidates(index: BM25, ids: list, query_id: str, query: str, k: int) -> list:
    """The BM25 top k document ids. As BEIR scores ArguAna, a query that is itself an argument of the corpus never
    finds itself: that is no answer."""
    skip = {ids.index(query_id)} if query_id in ids else set()
    return [ids[i] for i in index.top(query, k, skip)]


def ndcg(ranked: list, relevant: dict, k: int = K) -> float:
    """NDCG@k with trec_eval's linear gain, as BEIR reports it: the grade at rank r counts grade / log2(r + 1)."""
    dcg = sum(relevant.get(doc, 0) / math.log2(rank + 2) for rank, doc in enumerate(ranked[:k]))
    grades = sorted((grade for grade in relevant.values() if grade > 0), reverse=True)[:k]
    ideal = sum(grade / math.log2(rank + 2) for rank, grade in enumerate(grades))
    return dcg / ideal if ideal else 0.0


def mrr(ranked: list, relevant: dict, k: int = K) -> float:
    return next((1 / (rank + 1) for rank, doc in enumerate(ranked[:k]) if relevant.get(doc, 0) > 0), 0.0)


def summary(rankings: list, queries: list, qrels: dict) -> tuple:
    """Mean NDCG@10, mean MRR@10, and the share of queries with a relevant document anywhere in their ranking."""
    judged = [(ranking, qrels[query_id]) for ranking, (query_id, _) in zip(rankings, queries)]
    return (statistics.fmean(ndcg(ranking, relevant) for ranking, relevant in judged),
            statistics.fmean(mrr(ranking, relevant) for ranking, relevant in judged),
            statistics.fmean(any(relevant.get(doc, 0) > 0 for doc in ranking) for ranking, relevant in judged))


def sample(items: list, count: int) -> list:
    """count items evenly spaced over the list, the first among them; the whole list if it is no longer."""
    return [items[i * len(items) // count] for i in range(count)] if len(items) > count else list(items)


def load(archive: bytes, root: str = DATASET) -> tuple:
    """A BEIR zip's corpus (id to title and text), its test queries in file order, and their judgements."""
    with zipfile.ZipFile(io.BytesIO(archive)) as z:
        corpus = {}
        for line in z.read(f"{root}/corpus.jsonl").decode("utf-8").splitlines():
            if line.strip():
                record = json.loads(line)
                corpus[record["_id"]] = " ".join(part for part in (record.get("title", ""), record["text"]) if part).strip()
        texts = [json.loads(line) for line in z.read(f"{root}/queries.jsonl").decode("utf-8").splitlines() if line.strip()]
        qrels = {}
        for row in z.read(f"{root}/qrels/test.tsv").decode("utf-8").splitlines()[1:]:
            if row.strip():
                query_id, doc_id, grade = row.split("\t")
                qrels.setdefault(query_id, {})[doc_id] = int(grade)
    return corpus, [(record["_id"], record["text"]) for record in texts if record["_id"] in qrels], qrels


def job_body(query: str, documents: list) -> dict:
    return {"task": "rerank", "timeout_ms": 120000, "input": {"query": query, "documents": documents}}


def fit(query: str, documents: list) -> tuple:
    """The documents cut to one length at which the job passes the worker's limits (characters per field and in
    all, and the body's bytes once json.dumps escapes non-ASCII text), and how many of them were cut."""
    if len(query) > MAX_FIELD_CHARACTERS:
        raise SystemExit(f"a query of {len(query)} characters is over the worker's {MAX_FIELD_CHARACTERS}")
    cap = min(MAX_FIELD_CHARACTERS, (MAX_TOTAL_CHARACTERS - len(query)) // len(documents))
    while cap > 0:
        cut = [document[:cap] for document in documents]
        if len(json.dumps(job_body(query, cut)).encode()) <= MAX_BODY:
            return cut, sum(len(document) > cap for document in documents)
        cap -= 100
    raise SystemExit("the query leaves no room for its documents within the worker's limits")


def rerank(base: str, query: str, documents: list) -> tuple:
    result = finish(base, job_body(query, documents))
    order = [item["index"] for item in result["rankings"]]
    if sorted(order) != list(range(len(documents))):
        raise SystemExit(f"rerank returned {len(order)} rankings that are not each of {len(documents)} candidates once")
    return order, result["model"]


def render(rows: list) -> str:
    lines = ["| Order | Queries | NDCG@10 | MRR@10 | Relevant document among the candidates | Wall time s |",
             "| --- | --- | --- | --- | --- | --- |"]
    for name, count, ndcg_mean, mrr_mean, found, seconds in rows:
        lines.append(f"| {name} | {count} | {ndcg_mean:.3f} | {mrr_mean:.3f} | {found:.2f} | {seconds:.1f} |")
    return "\n".join(lines)


def main() -> None:
    base = os.environ.get("RELEVANCE_TARGET", "http://model-worker:8090")
    backend = wait_ready(base)["backend"]
    if backend != "onnx":
        raise SystemExit(f"{base} reports backend {backend!r}; the measurement needs the onnx worker")
    pinned = public_data.DATASETS[DATASET]
    corpus, queries, qrels = load(public_data.read(DATASET, public_data.data_dir(os.environ)))
    chosen = sample(queries, QUERIES)
    ids = list(corpus)
    started = time.monotonic()
    index = BM25([corpus[doc_id] for doc_id in ids])
    lists = [candidates(index, ids, query_id, text, CANDIDATES) for query_id, text in chosen]
    bm25_seconds = time.monotonic() - started
    reranked, models, shortened, seconds = [], set(), 0, []
    for number, ((_query_id, text), found) in enumerate(zip(chosen, lists), start=1):
        if not found:
            reranked.append([])
            continue
        documents, cut = fit(text, [corpus[doc_id] for doc_id in found])
        began = time.monotonic()
        order, model = rerank(base, text, documents)
        seconds.append(time.monotonic() - began)
        reranked.append([found[i] for i in order])
        models.add(model)
        shortened += cut
        if number % 20 == 0:
            print(f"query {number}/{len(chosen)}", file=sys.stderr)
    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
    sent = sum(map(len, lists))
    print(f"""Measured {now} UTC, commit `{os.environ.get("BENCH_COMMIT", "unknown")}`, runner: {os.environ.get("BENCH_RUNNER", "unspecified")}, CPU: {cpu_model()}.
Model {", ".join(f"`{model}`" for model in sorted(models))} ({backend} backend); ArguAna from BEIR, `{pinned["file"]}` at SHA-256 `{pinned["sha256"][:12]}`, {pinned["licence"]}: {len(chosen)} of its {len(queries)} test queries, evenly spaced in file order, over its {len(corpus)} documents (title and text); judged documents per query, on average: {statistics.fmean(len(qrels[query_id]) for query_id, _ in chosen):g}. Candidates: the BM25 top {CANDIDATES} per query (k1 {K1}, b {B}, lower-cased word tokens, no stemming or stop words), the query's own argument left out as BEIR does; each list reranked by one job, {shortened} of its {sent} documents cut to fit the worker's limits. Wall time: BM25 indexing and ranking all {len(chosen)} queries in one process; reranking, the sum of the jobs from POST /v1/jobs to a terminal state, median {statistics.median(seconds):.2f} s per job.
""")
    print(render([("BM25", len(chosen), *summary(lists, chosen, qrels), bm25_seconds),
                  ("BM25, reranked", len(chosen), *summary(reranked, chosen, qrels), sum(seconds))]))


if __name__ == "__main__":
    main()
