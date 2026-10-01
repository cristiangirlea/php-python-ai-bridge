"""scripts/ner_measure.py scores the redact task's NER pass on tests/fixtures/ner-sample.json.

These check the sample and the scoring offline; the measurement itself needs the model and runs in the model
workflow, where it reports and never gates on a score.
"""

import importlib.util
import json
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SAMPLE = REPO / "tests" / "fixtures" / "ner-sample.json"
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


class ScoringTests(unittest.TestCase):
    def test_gold_spans_are_the_offsets_of_their_texts(self):
        item = {"text": "Ada met Bo in Rome.", "entities": [["PER", "Ada"], ["PER", "Bo"], ["LOC", "Rome"]]}
        self.assertEqual(measure.gold_spans(item), [(0, 3, "PER"), (8, 10, "PER"), (14, 18, "LOC")])

    def test_exact_and_overlapping_matches_are_counted_separately(self):
        gold = [(0, 11, "PER"), (20, 27, "LOC")]
        predicted = [(0, 4, "PER"), (20, 27, "LOC"), (30, 35, "ORG")]
        self.assertEqual(measure.score(gold, predicted),
                         {"gold": 2, "predicted": 3, "exact": 1, "found": 2, "right": 2})

    def test_a_wrong_label_is_neither_exact_nor_overlapping(self):
        self.assertEqual(measure.score([(0, 5, "PER")], [(0, 5, "ORG")]),
                         {"gold": 1, "predicted": 1, "exact": 0, "found": 0, "right": 0})

    def test_only_the_model_s_spans_are_predictions(self):
        spans = [{"start": 0, "end": 3, "label": "PER", "source": "model:PER", "score": 0.9},
                 {"start": 5, "end": 9, "label": "EMAIL", "source": "rule:email", "score": 1.0}]
        self.assertEqual(measure.predictions(spans), [(0, 3, "PER")])

    def test_rates_have_two_decimals_and_an_empty_denominator_is_not_a_number(self):
        self.assertEqual(measure.rate(1, 4), "0.25")
        self.assertEqual(measure.rate(2, 3), "0.67")
        self.assertEqual(measure.rate(0, 0), "n/a")


if __name__ == "__main__":
    unittest.main()
