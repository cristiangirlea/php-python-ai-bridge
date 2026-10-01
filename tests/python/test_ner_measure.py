"""scripts/ner_measure.py scores the redact task's NER pass on tests/fixtures/ner-sample.json.

These check the sample and the scoring offline; the measurement itself needs the model and runs in the model
workflow, where it reports and never gates on a score.
"""

import importlib.util
import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[2]
SAMPLE = REPO / "tests" / "fixtures" / "ner-sample.json"
DEV = REPO / "tests" / "fixtures" / "ner-dev.json"
spec = importlib.util.spec_from_file_location("ner_measure", REPO / "scripts" / "ner_measure.py")
measure = importlib.util.module_from_spec(spec)
spec.loader.exec_module(measure)


class SampleTests(unittest.TestCase):
    def setUp(self):
        self.items = json.loads(SAMPLE.read_text(encoding="utf-8"))["items"]

    def test_the_sample_is_well_formed(self):
        self.assertGreaterEqual(len(self.items), 45)
        self.assertEqual(len({item["text"] for item in self.items}), len(self.items))
        for item in self.items:
            with self.subTest(text=item["text"][:40]):
                self.assertTrue(1 <= len(item["text"]) <= 8192)
                self.assertTrue(item["entities"])
                for label, text in item["entities"]:
                    self.assertIn(label, {"PER", "ORG", "LOC"})
                    self.assertEqual(item["text"].count(text), 1, "each entity's text occurs exactly once")

    def test_every_register_holds_every_label(self):
        registers = {item["register"] for item in self.items}
        self.assertEqual(registers, {"news", "informal", "records"})
        for register in registers:
            labels = {label for item in self.items if item["register"] == register for label, _ in item["entities"]}
            with self.subTest(register=register):
                self.assertEqual(labels, {"PER", "ORG", "LOC"})

    def test_the_script_reads_this_sample(self):
        self.assertEqual(measure.SAMPLE, SAMPLE)

    def test_another_sample_can_be_measured(self):
        self.assertEqual(measure.sample_path({}), SAMPLE)
        self.assertEqual(measure.sample_path({"NER_SAMPLE": "tests/fixtures/ner-dev.json"}), DEV)

    def test_the_development_set_is_well_formed_and_apart_from_the_sample(self):
        dev = json.loads(DEV.read_text(encoding="utf-8"))["items"]
        self.assertEqual({item["text"] for item in dev} & {item["text"] for item in self.items}, set())
        self.assertTrue(any(not item["entities"] for item in dev), "it must show false positives too")
        for item in dev:
            with self.subTest(text=item["text"][:40]):
                for label, text in item["entities"]:
                    self.assertIn(label, {"PER", "ORG", "LOC"})
                    self.assertEqual(item["text"].count(text), 1)

    def test_importing_the_script_ignores_the_benchmark_s_settings(self):
        # It borrows the benchmark's protocol helpers; a malformed BENCH_TARGETS is the benchmark's business.
        sys.modules.pop("benchmark", None)
        fresh = importlib.util.spec_from_file_location("ner_measure_fresh", REPO / "scripts" / "ner_measure.py")
        with mock.patch.dict(os.environ, {"BENCH_TARGETS": "not-a-target"}):
            fresh.loader.exec_module(importlib.util.module_from_spec(fresh))


class ScoringTests(unittest.TestCase):
    def test_gold_spans_are_the_offsets_of_their_texts(self):
        item = {"text": "Ada met Bo in Rome.", "entities": [["PER", "Ada"], ["PER", "Bo"], ["LOC", "Rome"]]}
        self.assertEqual(measure.gold_spans(item), [(0, 3, "PER"), (8, 10, "PER"), (14, 18, "LOC")])

    def test_exact_and_overlapping_matches_are_counted_separately(self):
        gold = [(0, 11, "PER"), (20, 27, "LOC")]
        predicted = [(0, 4, "PER"), (20, 27, "LOC"), (30, 35, "ORG")]
        self.assertEqual(measure.score(gold, predicted),
                         {"gold": 2, "predicted": 3, "exact": 1, "overlapping": 2, "masked": 2})

    def test_one_predicted_span_matches_one_gold_span_at_most(self):
        # One PER span over "ines and pedro" found one name, not both.
        self.assertEqual(measure.score([(0, 4, "PER"), (9, 14, "PER")], [(0, 14, "PER")]),
                         {"gold": 2, "predicted": 1, "exact": 0, "overlapping": 1, "masked": 1})

    def test_a_wrong_label_is_neither_exact_nor_overlapping_but_still_masks(self):
        # For redaction what matters is that the name is hidden; the label it is hidden under is secondary.
        self.assertEqual(measure.score([(0, 5, "PER")], [(0, 5, "ORG")]),
                         {"gold": 1, "predicted": 1, "exact": 0, "overlapping": 0, "masked": 1})

    def test_only_the_model_s_spans_are_predictions(self):
        spans = [{"start": 0, "end": 3, "label": "PER", "source": "model:PER", "score": 0.9},
                 {"start": 4, "end": 7, "label": "LOC", "source": "model:LOC", "score": 0.6},
                 {"start": 9, "end": 13, "label": "EMAIL", "source": "rule:email", "score": 1.0}]
        self.assertEqual(measure.predictions(spans), [(0, 3, "PER"), (4, 7, "LOC")])

    def test_rows_add_up_by_register_and_by_label_for_each_threshold(self):
        items = [{"register": "informal", "text": "ada in rome", "entities": [["PER", "ada"], ["LOC", "rome"]]},
                 {"register": "news", "text": "Bo joined Acme.", "entities": [["PER", "Bo"], ["ORG", "Acme"]]}]
        # Each threshold is its own job: a lower one lets weaker spans into the worker's merge, where they can
        # displace stronger ones, so filtering one low-threshold job's spans is not the same as asking at 0.85.
        news = [{"start": 0, "end": 2, "label": "PER", "source": "model:PER", "score": 0.99},
                {"start": 10, "end": 14, "label": "LOC", "source": "model:LOC", "score": 0.9}]
        spans = {0.85: [[], news], 0.5: [[{"start": 7, "end": 11, "label": "LOC", "source": "model:LOC", "score": 0.6}], news]}
        rows = measure.tally(items, spans)
        # Registers keep the sample's order, then the labels follow.
        self.assertEqual([name for threshold, name, _ in rows if threshold == 0.85],
                         ["informal", "news", "PER", "ORG", "LOC"])
        counts = {(threshold, name): value for threshold, name, value in rows}
        self.assertEqual(counts[(0.85, "informal")],
                         {"gold": 2, "predicted": 0, "exact": 0, "overlapping": 0, "masked": 0})
        self.assertEqual(counts[(0.5, "informal")], {"gold": 2, "predicted": 1, "exact": 1, "overlapping": 1, "masked": 1})
        # Acme predicted as a place is an ORG missed and a LOC that is wrong, in each label's own row; in the
        # register's row it is still a name masked.
        self.assertEqual(counts[(0.85, "news")], {"gold": 2, "predicted": 2, "exact": 1, "overlapping": 1, "masked": 2})
        self.assertEqual(counts[(0.85, "ORG")], {"gold": 1, "predicted": 0, "exact": 0, "overlapping": 0, "masked": 0})
        self.assertEqual(counts[(0.85, "LOC")], {"gold": 1, "predicted": 1, "exact": 0, "overlapping": 0, "masked": 0})
        self.assertEqual(counts[(0.85, "PER")], {"gold": 2, "predicted": 1, "exact": 1, "overlapping": 1, "masked": 1})

    def test_rates_have_two_decimals_and_an_empty_denominator_is_not_a_number(self):
        self.assertEqual(measure.rate(1, 4), "0.25")
        self.assertEqual(measure.rate(2, 3), "0.67")
        self.assertEqual(measure.rate(0, 0), "n/a")


if __name__ == "__main__":
    unittest.main()
