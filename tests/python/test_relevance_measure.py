"""scripts/relevance_measure.py scores the rerank task against BM25 on a public dataset.

These check the metrics on small cases worked by hand, the BM25 scorer, the dataset reader, the fit to the worker's
limits and the table, offline: the measurement itself needs the dataset and a worker, and never gates on a score.
"""

import importlib.util
import io
import json
import math
import unittest
import zipfile
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("relevance_measure", REPO / "scripts" / "relevance_measure.py")
measure = importlib.util.module_from_spec(spec)
spec.loader.exec_module(measure)


class MetricTests(unittest.TestCase):
    def test_ndcg_of_one_relevant_document_by_its_rank(self):
        relevant = {"d": 1}
        self.assertEqual(measure.ndcg(["d", "x", "y"], relevant), 1.0)
        self.assertAlmostEqual(measure.ndcg(["x", "y", "d"], relevant), 0.5)  # 1 / log2(4)
        self.assertAlmostEqual(measure.ndcg(["x", "d"], relevant), 1 / math.log2(3))
        self.assertEqual(measure.ndcg(["x"] * 10 + ["d"], relevant), 0.0)  # rank 11 is past the cut
        self.assertEqual(measure.ndcg([], relevant), 0.0)

    def test_ndcg_uses_the_grade_as_a_linear_gain_against_the_ideal_order(self):
        relevant = {"a": 2, "b": 1, "c": 0}
        self.assertEqual(measure.ndcg(["a", "b", "c"], relevant), 1.0)
        ideal = 2 + 1 / math.log2(3)
        self.assertAlmostEqual(measure.ndcg(["b", "a"], relevant), (1 + 2 / math.log2(3)) / ideal)
        self.assertAlmostEqual(measure.ndcg(["c", "a"], relevant), (2 / math.log2(3)) / ideal)

    def test_ndcg_of_a_query_with_nothing_relevant_is_zero(self):
        self.assertEqual(measure.ndcg(["a"], {"a": 0}), 0.0)

    def test_mrr_is_the_reciprocal_rank_of_the_first_relevant_document_within_ten(self):
        relevant = {"d": 1, "e": 1}
        self.assertEqual(measure.mrr(["d"], relevant), 1.0)
        self.assertEqual(measure.mrr(["x", "y", "e", "d"], relevant), 1 / 3)
        self.assertEqual(measure.mrr(["x"] * 10 + ["d"], relevant), 0.0)
        self.assertEqual(measure.mrr(["x", "c"], {"c": 0}), 0.0)

    def test_a_summary_averages_over_the_queries(self):
        queries = [("q1", "one"), ("q2", "two")]
        qrels = {"q1": {"a": 1}, "q2": {"b": 1}}
        ndcg, mrr, found = measure.summary([["a", "x"], ["x", "y"]], queries, qrels)
        self.assertEqual((ndcg, mrr, found), (0.5, 0.5, 0.5))
        _, mrr, found = measure.summary([["x", "a"], ["x"] * 10 + ["b"]], queries, qrels)
        self.assertEqual((mrr, found), (0.25, 1.0))

    def test_the_sample_is_evenly_spaced_and_stable(self):
        items = list(range(1406))
        chosen = measure.sample(items, 200)
        self.assertEqual(len(chosen), 200)
        self.assertEqual(chosen[:3], [0, 7, 14])
        self.assertEqual(chosen[-1], 1398)
        self.assertEqual(len(set(chosen)), 200)
        self.assertEqual(measure.sample(items[:5], 200), items[:5])

    def test_the_settings_fit_the_worker(self):
        self.assertEqual((measure.QUERIES, measure.CANDIDATES, measure.K), (200, 100, 10))
        self.assertLessEqual(measure.CANDIDATES, measure.MAX_DOCUMENTS)


class Bm25Tests(unittest.TestCase):
    def test_words_are_lower_cased_runs_of_letters_and_digits(self):
        self.assertEqual(measure.tokens("Vegetarian diets, CO2 & the Planet's 2nd-best"),
                         ["vegetarian", "diets", "co2", "the", "planet", "s", "2nd", "best"])

    def test_the_score_follows_the_formula(self):
        index = measure.BM25(["apple apple pie", "pie crust", "banana"], k1=1.2, b=0.75)
        average = 6 / 3
        idf_apple = math.log(1 + (3 - 1 + 0.5) / (1 + 0.5))
        idf_pie = math.log(1 + (3 - 2 + 0.5) / (2 + 0.5))

        def part(idf, tf, length):
            return idf * tf * 2.2 / (tf + 1.2 * (1 - 0.75 + 0.75 * length / average))

        scores = index.scores("apple pie")
        self.assertEqual(set(scores), {0, 1})
        self.assertAlmostEqual(scores[0], part(idf_apple, 2, 3) + part(idf_pie, 1, 3))
        self.assertAlmostEqual(scores[1], part(idf_pie, 1, 2))

    def test_a_word_repeated_in_the_query_counts_each_time(self):
        index = measure.BM25(["apple", "pie"])
        self.assertAlmostEqual(index.scores("apple apple")[0], 2 * index.scores("apple")[0])

    def test_the_idf_of_a_word_in_every_document_stays_positive(self):
        index = measure.BM25(["the cat", "the dog"])
        self.assertGreater(index.scores("the")[0], 0)

    def test_the_top_documents_best_first_with_ties_by_position_and_skips(self):
        index = measure.BM25(["red fish", "blue fish", "red red fish", "green"])
        self.assertEqual(index.top("red fish", 10), [2, 0, 1])
        self.assertEqual(index.top("fish", 10), [0, 1, 2])
        self.assertEqual(index.top("red fish", 2), [2, 0])
        self.assertEqual(index.top("red fish", 10, skip={2}), [0, 1])
        self.assertEqual(index.top("purple", 10), [])


def archive(corpus, queries, qrels, root="arguana") -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as z:
        z.writestr(f"{root}/corpus.jsonl", "".join(json.dumps(record) + "\n" for record in corpus))
        z.writestr(f"{root}/queries.jsonl", "".join(json.dumps(record) + "\n" for record in queries))
        z.writestr(f"{root}/qrels/test.tsv", "query-id\tcorpus-id\tscore\n" + "".join(f"{q}\t{d}\t{s}\n" for q, d, s in qrels))
    return buffer.getvalue()


class CandidateTests(unittest.TestCase):
    def test_a_query_never_finds_its_own_argument(self):
        ids = ["q1", "d1", "d2"]
        index = measure.BM25(["solar power is cheap", "solar power is cheap now", "solar panels"])
        self.assertEqual(measure.candidates(index, ids, "q1", "solar power is cheap", 10), ["d1", "d2"])

    def test_a_query_outside_the_corpus_skips_nothing(self):
        ids = ["d1", "d2"]
        index = measure.BM25(["solar power", "solar"])
        self.assertEqual(measure.candidates(index, ids, "q9", "solar power", 10), ["d1", "d2"])
        self.assertEqual(measure.candidates(index, ids, "q9", "solar power", 1), ["d1"])

    def test_only_the_model_worker_is_measured(self):
        with mock.patch.object(measure, "wait_ready", return_value={"backend": "lexical"}),                 self.assertRaisesRegex(SystemExit, "onnx"):
            measure.main()


class DatasetTests(unittest.TestCase):
    def test_the_beir_layout_is_read(self):
        data = archive(
            [{"_id": "d1", "title": "diet", "text": "Meat costs land."}, {"_id": "d2", "title": "", "text": "Bees."}],
            [{"_id": "q2", "text": "Second?"}, {"_id": "q1", "text": "First?"}, {"_id": "q9", "text": "No judgement."}],
            [("q1", "d1", 1), ("q2", "d2", 1), ("q2", "d1", 0)])
        corpus, queries, qrels = measure.load(data)
        self.assertEqual(corpus, {"d1": "diet Meat costs land.", "d2": "Bees."})
        # The queries file's order, only those the test qrels judge.
        self.assertEqual(queries, [("q2", "Second?"), ("q1", "First?")])
        self.assertEqual(qrels, {"q1": {"d1": 1}, "q2": {"d2": 1, "d1": 0}})

    def test_the_pinned_dataset_is_arguana(self):
        self.assertEqual(measure.DATASET, "arguana")
        self.assertIn(measure.DATASET, measure.public_data.DATASETS)


class FitTests(unittest.TestCase):
    def test_documents_within_the_limits_are_sent_whole(self):
        documents = ["short one", "short two"]
        self.assertEqual(measure.fit("query", documents), (documents, 0))

    def test_long_documents_are_cut_to_the_character_budget(self):
        documents = ["a" * 5000] * 100
        cut, shortened = measure.fit("q" * 1000, documents)
        self.assertEqual(shortened, 100)
        self.assertEqual({len(document) for document in cut}, {(measure.MAX_TOTAL_CHARACTERS - 1000) // 100})

    def test_escaped_text_is_cut_until_the_body_fits_in_bytes(self):
        # A curly quote is one character and six bytes once json.dumps escapes it.
        documents = ["’" * 1990] * 100
        cut, shortened = measure.fit("query", documents)
        self.assertEqual(shortened, 100)
        self.assertLessEqual(len(json.dumps(measure.job_body("query", cut)).encode()), measure.MAX_BODY)
        self.assertLessEqual(sum(map(len, cut)) + len("query"), measure.MAX_TOTAL_CHARACTERS)
        self.assertEqual(len({len(document) for document in cut}), 1)

    def test_a_document_longer_than_a_field_is_cut_to_the_field_limit(self):
        cut, shortened = measure.fit("query", ["a" * 10000, "short"])
        self.assertEqual([len(document) for document in cut], [measure.MAX_FIELD_CHARACTERS, 5])
        self.assertEqual(shortened, 1)

    def test_a_query_longer_than_a_field_ends_the_run_before_any_job(self):
        with self.assertRaisesRegex(SystemExit, "query"):
            measure.fit("q" * (measure.MAX_FIELD_CHARACTERS + 1), ["document"])

    def test_a_query_that_leaves_no_room_ends_the_run(self):
        with self.assertRaises(SystemExit):
            measure.fit("q" * (measure.MAX_TOTAL_CHARACTERS - 10), ["document"] * 100)

    def test_a_reranked_order_must_hold_every_candidate_once(self):
        good = {"model": "m", "rankings": [{"index": 1, "score": 2.0}, {"index": 0, "score": 1.0}]}
        with mock.patch.object(measure, "finish", return_value=good):
            self.assertEqual(measure.rerank("http://worker", "q", ["a", "b"]), ([1, 0], "m"))
        short = {"model": "m", "rankings": [{"index": 1, "score": 2.0}]}
        with mock.patch.object(measure, "finish", return_value=short), self.assertRaises(SystemExit):
            measure.rerank("http://worker", "q", ["a", "b"])


class TableTests(unittest.TestCase):
    def test_the_table_has_one_row_per_order(self):
        table = measure.render([("BM25", 200, 0.41234, 0.3456, 0.97, 12.345),
                                ("BM25, reranked", 200, 0.5, 0.40001, 0.97, 61.04)])
        self.assertEqual(table.splitlines(), [
            "| Order | Queries | NDCG@10 | MRR@10 | Relevant document among the candidates | Wall time s |",
            "| --- | --- | --- | --- | --- | --- |",
            "| BM25 | 200 | 0.412 | 0.346 | 0.97 | 12.3 |",
            "| BM25, reranked | 200 | 0.500 | 0.400 | 0.97 | 61.0 |",
        ])


if __name__ == "__main__":
    unittest.main()
