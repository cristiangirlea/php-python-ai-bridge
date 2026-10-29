"""scripts/fetch_model.py pins every model file the worker loads, by revision and SHA-256.

The download itself needs the network and runs in the model workflow; these check the pins offline.
"""

import importlib.util
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from ai_bridge import tasks

REPO = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("fetch_model", REPO / "scripts" / "fetch_model.py")
fetch_model = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fetch_model)


class PinTests(unittest.TestCase):
    def test_only_bge_needs_the_larger_per_file_cap(self):
        self.assertEqual(fetch_model.MODELS["rerank-bge-base"]["limit"], 300 * 1024 * 1024)
        self.assertEqual(fetch_model.LIMIT, 128 * 1024 * 1024)
        self.assertTrue(all(spec.get("limit", fetch_model.LIMIT) == fetch_model.LIMIT
                            for name, spec in fetch_model.MODELS.items() if name != "rerank-bge-base"))

    def test_every_model_is_pinned_by_revision_licence_and_hash(self):
        for directory, pin in fetch_model.MODELS.items():
            with self.subTest(directory=directory):
                self.assertRegex(pin["revision"], r"^[0-9a-f]{40}$")
                self.assertTrue(pin["license"])
                self.assertEqual(sorted(pin["files"].values()), ["model.onnx", "tokenizer.json"])
                self.assertEqual(set(pin["hashes"]), set(pin["files"].values()))
                for digest in pin["hashes"].values():
                    self.assertRegex(digest, r"^[0-9a-f]{64}$")

    def test_the_uncased_ner_model_is_the_int8_conversion_of_the_same_author_s_uncased_weights(self):
        pin = fetch_model.MODELS["redact-uncased"]
        self.assertEqual(pin["repository"], "Xenova/bert-base-NER-uncased")
        self.assertEqual(pin["files"]["onnx/model_int8.onnx"], "model.onnx")
        self.assertEqual(pin["license"], fetch_model.MODELS["redact"]["license"])

    def test_reranker_candidates_are_quantized_and_pinned(self):
        for directory in ("rerank-minilm-l6", "rerank-bge-base"):
            with self.subTest(directory=directory):
                self.assertEqual(fetch_model.MODELS[directory]["files"]["onnx/model_quantized.onnx"], "model.onnx")

    def test_fp32_references_use_the_quantized_models_exact_revisions(self):
        fixture = json.loads((REPO / "tests/fixtures/rerank-fp32.json").read_text())
        self.assertEqual(fixture["reference_file"], "onnx/model.onnx")
        self.assertEqual(fixture["precision"], "fp32")
        self.assertEqual(sum(len(case["documents"]) for case in fixture["cases"]), 8)
        for choice in ("minilm-l6", "bge-base"):
            reference = fixture["models"][choice]
            pin = fetch_model.MODELS[tasks.RERANK_MODELS[choice][0]]
            self.assertEqual((reference["repository"], reference["revision"]), (pin["repository"], pin["revision"]))
            self.assertRegex(reference["sha256"], r"^[0-9a-f]{64}$")
            self.assertEqual([len(scores) for scores in reference["scores"]],
                             [len(case["documents"]) for case in fixture["cases"]])

    def test_every_directory_the_ner_pass_loads_is_one_the_fetcher_fills(self):
        loaded = []

        class Encoding:
            word_ids, offsets, overflowing = [], [], []

        class Tokenizer:
            @staticmethod
            def encode(text):
                return Encoding()

        def load(directory, max_length, stride=0):
            loaded.append(Path(directory).name)
            return Tokenizer(), None, set()

        with patch.object(tasks, "_onnx", side_effect=load), patch.object(tasks, "_probabilities", return_value=[]):
            tasks._ner("ask priya", ["PER"], 0.85, "/models", lambda *_: None)
        self.assertEqual(sorted(loaded), ["redact", "redact-uncased"])
        self.assertLessEqual(set(loaded), set(fetch_model.MODELS))

    def test_the_fetcher_fills_every_selectable_directory(self):
        self.assertEqual(set(tasks.MODEL_DIRECTORIES) | {"rerank-minilm-l6", "rerank-bge-base"}, set(fetch_model.MODELS))


if __name__ == "__main__":
    unittest.main()
