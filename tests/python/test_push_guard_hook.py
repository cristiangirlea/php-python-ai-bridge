"""The pre-call hook in .claude/hooks/push-guard.sh refuses, before a Bash or PowerShell command runs, a push that
would land on main or a tag, a force push, git tag, gh release, gh pr merge --auto or --admin, and a write through
gh api to releases or refs, in every spelling it can read; what it cannot read and mentions one of those, it refuses
too. Its cases live beside it in push-guard.test.sh, as in the owner's other repositories; this runs them, and holds
the hook's wiring in settings.json in place.
"""

import json
import shutil
import subprocess
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
HOOK = REPO / ".claude" / "hooks" / "push-guard.sh"
CASES = REPO / ".claude" / "hooks" / "push-guard.test.sh"
SETTINGS = REPO / ".claude" / "settings.json"


class PushGuardHookTests(unittest.TestCase):
    def test_the_hook_and_its_cases_exist_and_are_lf(self):
        for path in (HOOK, CASES):
            with self.subTest(file=path.name):
                self.assertTrue(path.is_file())
                self.assertNotIn(b"\r", path.read_bytes())

    def test_settings_run_the_hook_before_every_bash_and_powershell_call(self):
        hooks = json.loads(SETTINGS.read_text(encoding="utf-8"))["hooks"]["PreToolUse"]
        commands = [hook["command"] for entry in hooks if entry["matcher"] == "Bash|PowerShell" for hook in entry["hooks"]]
        self.assertEqual(commands, ['sh "$CLAUDE_PROJECT_DIR"/.claude/hooks/push-guard.sh'])

    def test_the_hooks_own_cases_pass(self):
        # Several hundred cases, each a sh and a few git processes: seconds on Linux, minutes under Git Bash.
        result = subprocess.run([shutil.which("sh") or "sh", str(CASES)], capture_output=True, text=True, timeout=900)
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        self.assertRegex(result.stdout, r"push-guard\.test: ok \(\d+ cases\)")


if __name__ == "__main__":
    unittest.main()
