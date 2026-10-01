"""scripts/fetch_model.py pins every model file the worker loads, by revision and SHA-256.

The download itself needs the network and runs in the model workflow; these check the pins offline.
"""

import importlib.util
import unittest
from pathlib import Path
from unittest.mock import patch

from ai_bridge import tasks

REPO = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("fetch_model", REPO / "scripts" / "fetch_model.py")
fetch_model = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fetch_model)


class PinTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
