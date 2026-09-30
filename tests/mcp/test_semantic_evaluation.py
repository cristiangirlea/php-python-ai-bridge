"""evaluation-semantic.xml asks questions that need meaning rather than shared words.

Each answer must be reachable with the pinned models, which tests/mcp_model_probe.py proves over stdio in the model
workflow, and with none of the demo worker's word matching, which this proves offline with the calls in
semantic_reach.SEMANTIC_REACH. So a model that scores on this file used what the models understand, and a run on the
demo worker shows what word matching misses.
"""

import json
import unittest
import xml.etree.ElementTree as ElementTree

from mcp import Client

from semantic_reach import SEMANTIC_REACH
from test_evaluation import DOCS, HERE, start_demo, stop_demo

SEMANTIC = HERE / "evaluation-semantic.xml"


class SemanticEvaluationTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.pairs = [(pair.findtext("question"), pair.findtext("answer"))
                     for pair in ElementTree.parse(SEMANTIC).getroot().iter("qa_pair")]
        start_demo(cls)

    @classmethod
    def tearDownClass(cls):
        stop_demo(cls)

    def test_the_file_holds_ten_distinct_well_formed_questions(self):
        self.assertEqual(len(self.pairs), 10)
        self.assertEqual(len(SEMANTIC_REACH), len(self.pairs))
        self.assertEqual(len({question for question, _ in self.pairs}), 10)
        for question, answer in self.pairs:
            with self.subTest(question=question[:50]):
                self.assertTrue(question and question.strip().endswith((".", "?")))
                self.assertTrue(answer and answer == answer.strip() and "\n" not in answer)

    def test_no_answer_is_given_away_by_its_question(self):
        for (question, answer), (_, _, may_contain) in zip(self.pairs, SEMANTIC_REACH):
            if not may_contain:
                with self.subTest(question=question[:60]):
                    self.assertNotIn(answer.casefold(), question.casefold())

    async def test_the_demo_worker_reaches_none_of_the_answers(self):
        async with Client(self.mcp, raise_exceptions=True) as client:
            for (question, answer), (calls, read, _) in zip(self.pairs, SEMANTIC_REACH):
                with self.subTest(question=question[:60]):
                    for tool, arguments in calls:
                        result = await client.call_tool(tool, arguments)
                        self.assertFalse(result.is_error, (tool, result.content))
                    self.assertNotEqual(read(result.structured_content), answer,
                                        json.dumps(result.structured_content)[:400])

    def test_the_guide_says_how_to_run_it_on_the_models(self):
        self.assertIn("--backend onnx --evaluation tests/mcp/evaluation-semantic.xml",
                      (DOCS / "mcp.md").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
