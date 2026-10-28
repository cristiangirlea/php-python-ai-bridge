"""Regression checks for selectable rerankers and the release boundary, without model dependencies."""

import os
import subprocess
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from ai_bridge import tasks

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts"))
import relevance_measure as measure  # noqa: E402


class RerankerReviewTests(unittest.TestCase):
    def test_gate_refuses_another_dataset_before_contacting_worker(self):
        self.assertEqual(measure.GATE_DATASET, "squad")
        with patch.dict(os.environ, {"RELEVANCE_GATE": "1", "RELEVANCE_DATASET": "arguana"}), \
                patch.object(measure, "wait_ready") as ready:
            with self.assertRaisesRegex(SystemExit, "RELEVANCE_GATE.*squad"):
                measure.main()
        ready.assert_not_called()

    def test_each_selection_controls_the_batch_size(self):
        for choice, size in (("tinybert", 32), ("minilm-l6", 8), ("bge-base", 4)):
            with self.subTest(choice=choice), patch.dict(os.environ, {"BRIDGE_RERANK_MODEL": choice}), \
                    patch.object(tasks, "_onnx", return_value=(None, None, set())), \
                    patch.object(tasks, "_score_pairs", return_value=[1.0]) as score:
                tasks.execute("rerank", {"query": "q", "documents": ["d"]}, "onnx", "/models", lambda *_: None)
                self.assertEqual(score.call_args.kwargs["batch_size"], size)

    def test_small_batches_cannot_flood_the_progress_pipe(self):
        events = []

        def score(*args, batch_size):
            for completed in range(batch_size, 513, batch_size):
                args[5](completed)
            return [1.0] * 512

        with patch.dict(os.environ, {"BRIDGE_RERANK_MODEL": "bge-base"}), \
                patch.object(tasks, "_onnx", return_value=(None, None, set())), \
                patch.object(tasks, "_score_pairs", side_effect=score):
            tasks.execute("rerank", {"query": "q", "documents": ["d"] * 512}, "onnx", "/models",
                          lambda *event: events.append(event))
        self.assertLessEqual(len(events), 66)
        self.assertEqual(events[0], (0, 512))
        self.assertEqual(events[-1], (512, 512))

    def test_padding_uses_the_tokenizers_pad_token(self):
        for vocabulary, expected in (({"<pad>": 1, "<s>": 0}, ("<pad>", 1)),
                                     ({"[PAD]": 0}, ("[PAD]", 0))):
            tokenizer = Mock()
            tokenizer.token_to_id.side_effect = vocabulary.get
            ort = Mock()
            ort.InferenceSession.return_value.get_inputs.return_value = [SimpleNamespace(name="input_ids")]
            with patch.dict(sys.modules, {"onnxruntime": ort,
                                          "tokenizers": SimpleNamespace(Tokenizer=Mock(from_file=Mock(return_value=tokenizer)))}):
                tasks._load_onnx(Path("/models"), 512, 0)
            tokenizer.enable_padding.assert_called_once_with(pad_token=expected[0], pad_id=expected[1])

    def test_missing_pad_token_is_refused(self):
        tokenizer = Mock()
        tokenizer.token_to_id.return_value = None
        with patch.dict(sys.modules, {"onnxruntime": Mock(),
                                      "tokenizers": SimpleNamespace(Tokenizer=Mock(from_file=Mock(return_value=tokenizer)))}):
            with self.assertRaisesRegex(ValueError, "no pad token"):
                tasks._load_onnx(Path("/models"), 512, 0)

    def test_reported_table_uses_the_selected_model(self):
        for choice in ("tinybert", "minilm-l6", "bge-base"):
            with self.subTest(choice=choice):
                subprocess.run([sys.executable, "-c", "from ai_bridge import tasks; "
                                "assert tasks.MODEL_NAMES['onnx']['rerank'] == tasks.rerank_model()[1]"],
                               env={**os.environ, "BRIDGE_RERANK_MODEL": choice}, check=True, timeout=10)
