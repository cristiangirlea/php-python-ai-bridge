"""The pre-call hook in .claude/hooks/push-guard.sh refuses, before a Bash or PowerShell command runs, a push that
would land on main or a tag, a force push, git tag, gh release, gh pr merge --auto or --admin, and a write through
gh api to releases or refs, in every spelling it can read; what it cannot read and mentions one of those, it refuses
too. Its cases live beside it in push-guard.test.sh, as in the owner's other repositories; this runs them, and holds
the hook's wiring in settings.json in place.
"""

import io
import json
import os
import re
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
HOOK = REPO / ".claude" / "hooks" / "push-guard.sh"
CASES = REPO / ".claude" / "hooks" / "push-guard.test.sh"
SETTINGS = REPO / ".claude" / "settings.json"
SKIP_LINE = re.compile(r"^push-guard\.test: skipped the killed-git case: .+ is not executable$", re.MULTILINE)


def _tmp_is_noexec() -> bool:
    if not hasattr(os, "statvfs") or not hasattr(os, "ST_NOEXEC"):
        return False
    return bool(os.statvfs(os.environ.get("TMPDIR") or "/tmp").f_flag & os.ST_NOEXEC)


class PushGuardHookTests(unittest.TestCase):
    def _check_skip_output(self, stdout: str, *, noexec: bool, stream: io.TextIOBase) -> None:
        skips = SKIP_LINE.findall(stdout)
        self.assertEqual(len(skips), 1 if noexec else 0, stdout)
        for line in skips:
            print(f"\ntest_push_guard_hook: {line}", file=stream, flush=True)

    def test_the_hook_and_its_cases_exist_and_are_lf(self):
        for path in (HOOK, CASES):
            with self.subTest(file=path.name):
                self.assertTrue(path.is_file())
                self.assertNotIn(b"\r", path.read_bytes())

    def test_settings_run_the_hook_before_every_bash_and_powershell_call(self):
        hooks = json.loads(SETTINGS.read_text(encoding="utf-8"))["hooks"]["PreToolUse"]
        commands = [hook["command"] for entry in hooks if entry["matcher"] == "Bash|PowerShell" for hook in entry["hooks"]]
        self.assertEqual(commands, ['sh "$CLAUDE_PROJECT_DIR"/.claude/hooks/push-guard.sh'])

    def test_the_skip_line_is_printed_only_when_tmp_is_noexec(self):
        skip = "push-guard.test: skipped the killed-git case: /tmp/tmp.abc is not executable\n"
        ok = "push-guard.test: ok (306 cases)\n"
        err = io.StringIO()
        self._check_skip_output(skip + ok, noexec=True, stream=err)
        self.assertEqual(err.getvalue(), f"\ntest_push_guard_hook: {skip}")
        err = io.StringIO()
        self._check_skip_output(ok, noexec=False, stream=err)
        self.assertEqual(err.getvalue(), "")
        with self.assertRaises(AssertionError):
            self._check_skip_output(skip + ok, noexec=False, stream=io.StringIO())
        with self.assertRaises(AssertionError):
            self._check_skip_output(ok, noexec=True, stream=io.StringIO())

    def test_the_hooks_own_cases_pass(self):
        # Several hundred cases, each a sh and a few git processes: seconds on Linux, minutes under Git Bash.
        result = subprocess.run([shutil.which("sh") or "sh", str(CASES)], capture_output=True, text=True, timeout=900)
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        self.assertRegex(result.stdout, r"push-guard\.test: ok \(\d+ cases\)")
        self._check_skip_output(result.stdout, noexec=_tmp_is_noexec(), stream=sys.stderr)


if __name__ == "__main__":
    unittest.main()
