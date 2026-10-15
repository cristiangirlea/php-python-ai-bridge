"""The post-edit hook in .claude/hooks/repo-rules.sh reports a PHP file under src/, examples/ or tests/php that lacks
declare(strict_types=1) or indents with tabs, and a worker module that imports a model dependency at module level.
It must stay quiet for everything else: files outside its scope, odd or empty input, and a file that does not exist.
"""

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
HOOK = REPO / ".claude" / "hooks" / "repo-rules.sh"

CLEAN_PHP = "<?php\n\ndeclare(strict_types=1);\n\nnamespace PhpAiBridge;\n\nfinal class X\n{\n    public int $n = 1;\n}\n"
CLEAN_WORKER = "import json\nimport math\n\n\ndef score(x):\n    import numpy as np\n    return np.array(x)\n"


def run_hook(root: Path, payload) -> subprocess.CompletedProcess:
    text = payload if isinstance(payload, str) else json.dumps({"tool_name": "Edit", "tool_input": payload})
    return subprocess.run([shutil.which("sh") or "sh", str(HOOK)], input=text, capture_output=True, text=True,
                          env={**os.environ, "CLAUDE_PROJECT_DIR": str(root)})


class RepoRulesHookTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, True)
        for name in ("src", "examples", "tests/php", "worker/ai_bridge", "docs"):
            (self.root / name).mkdir(parents=True)

    def write(self, relative: str, text: str) -> Path:
        path = self.root / relative
        path.write_text(text, encoding="utf-8")
        return path

    def test_the_hook_exists_and_is_lf(self):
        self.assertTrue(HOOK.is_file())
        self.assertNotIn(b"\r", HOOK.read_bytes())

    def test_clean_files_pass(self):
        for relative, text in (("src/X.php", CLEAN_PHP), ("examples/x.php", CLEAN_PHP), ("tests/php/x.php", CLEAN_PHP),
                               ("worker/ai_bridge/tasks.py", CLEAN_WORKER)):
            with self.subTest(file=relative):
                path = self.write(relative, text)
                result = run_hook(self.root, {"file_path": str(path)})
                self.assertEqual((result.returncode, result.stderr), (0, ""))

    def test_a_php_file_without_strict_types_and_with_tabs_is_reported_twice(self):
        path = self.write("src/X.php", "<?php\n\nnamespace PhpAiBridge;\n\nclass X\n{\n\tpublic int $n = 1;\n}\n")
        result = run_hook(self.root, {"file_path": str(path)})
        self.assertEqual(result.returncode, 2)
        self.assertIn("src/X.php: add declare(strict_types=1);", result.stderr)
        self.assertIn("src/X.php: indent with four spaces, not tabs", result.stderr)

    def test_a_module_level_model_import_is_reported_in_every_form(self):
        for text in ("import numpy as np\n", "from onnxruntime import InferenceSession\n", "import os, tokenizers\n",
                     "try:\n    import numpy\nexcept ImportError:\n    numpy = None\n",
                     "def f():\n    pass\n\n\nimport torch\n"):
            with self.subTest(text=text):
                path = self.write("worker/ai_bridge/tasks.py", "import json\n" + text)
                result = run_hook(self.root, {"file_path": str(path)})
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn("worker/ai_bridge/tasks.py: a model dependency is imported at module level", result.stderr)

    def test_an_import_inside_a_function_or_in_a_name_is_not_reported(self):
        for text in (CLEAN_WORKER, "import json\n\n\nclass A:\n    def f(self):\n        from tokenizers import Tokenizer\n",
                     "import numpy_like_helper\n", "from .numpy_free import x\n", "# import numpy\n"):
            with self.subTest(text=text):
                path = self.write("worker/ai_bridge/server.py", text)
                result = run_hook(self.root, {"file_path": str(path)})
                self.assertEqual((result.returncode, result.stderr), (0, ""))

    def test_files_outside_the_scope_are_ignored(self):
        for relative, text in (("docs/x.php", "<?php\n\tnamespace X;\n"), ("worker/setup.py", "import numpy\n"),
                               ("tests/python/x.py", "import numpy\n")):
            with self.subTest(file=relative):
                path = self.root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text, encoding="utf-8")
                result = run_hook(self.root, {"file_path": str(path)})
                self.assertEqual((result.returncode, result.stderr), (0, ""))

    def test_odd_input_is_ignored(self):
        for payload in ("", "not json", json.dumps({"tool_input": {}}), {"file_path": str(self.root / "src" / "Missing.php")},
                        {"file_path": ""}):
            with self.subTest(payload=payload):
                result = run_hook(self.root, payload)
                self.assertEqual((result.returncode, result.stderr), (0, ""))

    def test_a_json_escaped_backslash_path_is_made_repository_relative(self):
        # Claude Code on Windows sends E:\\repo\\src\\X.php; the same escaping with this root must resolve the file.
        self.write("src/X.php", "<?php\n\tnamespace X;\n")
        escaped = str(self.root).replace("\\", "/") + "\\\\src\\\\X.php"
        result = run_hook(self.root, '{"tool_input": {"file_path": "%s"}}' % escaped)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("src/X.php: add declare(strict_types=1);", result.stderr)

    def test_a_relative_path_resolves_under_the_project_directory(self):
        self.write("tests/php/x.php", "<?php\n\tnamespace X;\n")
        result = run_hook(self.root, {"file_path": "tests/php/x.php"})
        self.assertEqual(result.returncode, 2, result.stderr)


if __name__ == "__main__":
    unittest.main()
