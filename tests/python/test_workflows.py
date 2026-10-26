"""The workflows run on GitHub's hosted runners, every job of them, with no self-hosted runner and no runner variable.

The repository is public: GitHub does not meter Actions minutes for it, and a fork's pull request runs its own code
on a disposable runner of GitHub's, never on a machine of the maintainer's. Nothing here installs Docker Compose,
since GitHub's images ship the plugin. Read as text, since the standard library has no YAML parser; the workflows
keep their jobs two spaces in under `jobs:`. Every action a step uses is pinned to a commit, and no checkout
leaves the job's token behind: `docs/security.md` promises both.
"""

import itertools
import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
WORKFLOWS = sorted(path for ext in ("*.yml", "*.yaml") for path in (REPO / ".github" / "workflows").glob(ext))
RUNS_ON = "runs-on: ubuntu-latest"
# A step's `uses:` may follow its `name:` (so the dash is optional) and may quote its value.
USES = re.compile(r"^\s*(?:-\s+)?uses:\s*(.*?)\s*$")
# Every spelling of the key YAML accepts (quoted, a space before the colon, a flow-style step), so that a line
# USES cannot read is counted, not passed over.
ANY_USES = re.compile(r"""^\s*(?:-\s+)?(?:\{.*)?['"]?uses['"]?\s*:""")
# `owner/repo@<commit>`, and the version that commit is, as a comment, for the human reading the workflow.
PINNED = re.compile(r"""^['"]?[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9_.-]+@[0-9a-f]{40}['"]?(?: +# v\d+\.\d+\.\d+)?$""")


def jobs(text: str, workflow: str = "<workflow>") -> dict:
    """Each job's name and its lines.

    A job's header is its key two spaces in, quoted or not, with nothing after the colon but a comment. A header
    this does not recognise drops the lines of a first job, or adds a later job's lines to the job before it,
    which is why the number of jobs read is checked against the `runs-on:` lines of the file.
    """
    parts = re.split(r"^jobs:[ \t]*(?:#.*)?$", text, maxsplit=1, flags=re.MULTILINE)
    if len(parts) < 2:
        raise AssertionError(f"{workflow}: missing jobs: header")
    found, name = {}, None
    for line in parts[1].splitlines():
        header = re.match(r"""^  (['"]?)([A-Za-z0-9_-]+)\1:\s*(?:#.*)?$""", line)
        if header:
            name = header.group(2)
            found[name] = []
        elif name and line.startswith("    "):
            found[name].append(line.strip())
        elif line.strip() and not line.startswith(" "):
            name = None
    return found


def runs_on_lines(text: str) -> int:
    """How many `runs-on:` lines the file has, wherever they are: the number of jobs the reader should have found."""
    return len(re.findall(r"^\s*runs-on:", text, re.MULTILINE))


def steps(text: str) -> list:
    """Every step of every job, in file order, each the list of its lines with their indentation kept.

    A step is an item of a `steps:` list: its `- ` line and the lines below it until the next item at the same
    indentation, or the end of the list.
    """
    found, key_indent, item_indent = [], None, None
    for line in text.splitlines():
        key = re.match(r"^(\s*)steps:\s*$", line)
        if key:
            key_indent, item_indent = len(key.group(1)), None
            continue
        if key_indent is None or not line.strip() or line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        if indent <= key_indent:
            key_indent = None
        elif line.lstrip().startswith("- ") and item_indent in (None, indent):
            item_indent = indent
            found.append([line])
        else:
            found[-1].append(line)
    return found


def action(step: list) -> str:
    """The value of a step's `uses:` line as written, or an empty string for a step that runs a command."""
    return next((USES.match(line).group(1) for line in step if USES.match(line)), "")


def is_checkout(step: list) -> bool:
    """Whether the step runs actions/checkout; GitHub reads owner and repository names case-insensitively."""
    return action(step).lstrip("'\"").lower().startswith("actions/checkout@")


def inputs(step: list) -> list:
    """The stripped lines of a step's `with:` block."""
    lines = iter(step)
    for line in lines:
        key = re.match(r"^(\s*)with:\s*$", line)
        if key:
            deeper = itertools.takewhile(lambda below: len(below) - len(below.lstrip()) > len(key.group(1)), lines)  # noqa: B023  (called only within this iteration)
            return [below.strip() for below in deeper]
    return []


class WorkflowTests(unittest.TestCase):
    def test_the_workflows_are_found(self):
        self.assertEqual([path.name for path in WORKFLOWS], ["model-smoke.yml", "tests.yml"])
        for path in WORKFLOWS:
            self.assertTrue(jobs(path.read_text(encoding="utf-8"), path.name), path.name)

    def test_the_job_reader_sees_every_job(self):
        # Every job of these workflows runs on a runner, so it has exactly one `runs-on:` line: a job whose header
        # the reader did not recognise shows up as a `runs-on:` line without a job, not as a job that was never
        # checked. (A job that calls a reusable workflow has none; add it to the count if one appears.)
        for path in WORKFLOWS:
            text = path.read_text(encoding="utf-8")
            with self.subTest(workflow=path.name):
                self.assertEqual(len(jobs(text, path.name)), runs_on_lines(text))

    def test_the_job_reader_on_the_headers_it_must_tell_apart(self):
        text = """name: x
on: push
jobs:
  php:  # the matrix
    runs-on: ubuntu-latest
    steps:
      - run: echo php
  "mcp":
    runs-on: ubuntu-latest
  'gate':
    needs: [php, mcp]
    runs-on: ubuntu-latest
  # a comment between jobs
  "odd':
    runs-on: ubuntu-latest
  smoke: &anchored
    runs-on: ubuntu-latest
env:
  runs-on: not-a-job
"""
        found = jobs(text)
        # Mismatched quotes and an anchor are shapes the reader does not know; the count is what tells, since a
        # job it misses leaves its `runs-on:` line in the file. The `env:` line after the jobs is counted too: a
        # false failure at worst, never a job passed over.
        self.assertEqual(list(found), ["php", "mcp", "gate"])
        self.assertEqual(found["php"], ["runs-on: ubuntu-latest", "steps:", "- run: echo php"])
        self.assertEqual(found["gate"][:2], ["needs: [php, mcp]", "runs-on: ubuntu-latest"])
        self.assertEqual(runs_on_lines(text), 6)
        self.assertNotEqual(len(found), runs_on_lines(text))
        commented = """name: x
on: push
jobs:  # the jobs
  one:
    runs-on: ubuntu-latest
"""
        self.assertEqual(jobs(commented), {"one": ["runs-on: ubuntu-latest"]})
        with self.assertRaisesRegex(AssertionError, r"missing\.yml.*jobs:"):
            jobs("name: x\non: push\n", "missing.yml")

    def test_every_job_runs_on_github_hosted_ubuntu(self):
        for path in WORKFLOWS:
            for name, lines in jobs(path.read_text(encoding="utf-8"), path.name).items():
                with self.subTest(workflow=path.name, job=name):
                    self.assertEqual([line for line in lines if line.startswith("runs-on:")], [RUNS_ON])

    def test_no_workflow_names_a_self_hosted_runner_or_a_runner_variable(self):
        # A `runs-on` expression is how a self-hosted runner or a fallback to one would come back. Only the
        # `runs-on` values are read for the label, so a comment may say "self-hosted".
        for path in WORKFLOWS:
            text = path.read_text(encoding="utf-8")
            with self.subTest(workflow=path.name):
                self.assertNotIn("vars.CI_RUNNER", text)
                self.assertNotIn("runner.environment", text)
                self.assertNotRegex(text, r"runs-on: *\$\{\{")
                self.assertNotRegex(text, r"runs-on: *\[")
                # Every runs-on line in the file, whatever jobs() made of the job around it: a block form
                # (`runs-on:` followed by `- self-hosted`) is a bare `runs-on:` line, which is not RUNS_ON.
                for line in re.findall(r"^\s*runs-on:.*$", text, re.MULTILINE):
                    self.assertEqual(line.strip(), RUNS_ON, line)

    def test_no_step_uses_a_local_action(self):
        # GitHub's runner images ship the Docker Compose plugin; the lab-only install action is gone with the lab.
        # What matters is that no step runs one, not whether a directory exists in a checkout.
        for path in WORKFLOWS:
            for name, lines in jobs(path.read_text(encoding="utf-8"), path.name).items():
                with self.subTest(workflow=path.name, job=name):
                    # A step's `uses:` may follow its `name:` (no dash once stripped) and may quote the path.
                    self.assertEqual([line for line in lines if re.match(r"""(?:-\s+)?uses:\s*['"]?\./""", line)], [])

    def test_every_job_that_runs_compose_checks_the_repository_out_first(self):
        for path in WORKFLOWS:
            for name, lines in jobs(path.read_text(encoding="utf-8"), path.name).items():
                commands = [i for i, line in enumerate(lines) if "docker compose" in line and not line.startswith("#")]
                if not commands:
                    continue
                with self.subTest(workflow=path.name, job=name):
                    checkout = next(i for i, line in enumerate(lines) if line.startswith("- uses: actions/checkout@"))
                    self.assertLess(checkout, commands[0])

    def test_every_action_is_pinned_to_a_commit(self):
        # A tag or a branch can be moved after the review that admitted it; a commit cannot. The version comment
        # beside the hash is what a human reads, and what the next bump is compared against.
        for path in WORKFLOWS:
            text = path.read_text(encoding="utf-8")
            lines = text.splitlines()
            used = [USES.match(line).group(1) for line in lines if USES.match(line)]
            self.assertTrue(used, path.name)
            self.assertEqual(len(used), len([line for line in lines if ANY_USES.match(line)]), path.name)
            for value in used:
                with self.subTest(workflow=path.name, uses=value):
                    self.assertRegex(value, PINNED)

    def test_every_checkout_step_leaves_no_credentials_behind(self):
        # By default the checkout writes the job's token into .git/config, where every later step can read it.
        for path in WORKFLOWS:
            text = path.read_text(encoding="utf-8")
            found = steps(text)
            with self.subTest(workflow=path.name):
                # The step parser saw every `uses:` line, so no checkout step is missing from the check below.
                seen = len([step for step in found if action(step)])
                self.assertEqual(seen, len([line for line in text.splitlines() if USES.match(line)]))
            checkouts = [step for step in found if is_checkout(step)]
            self.assertTrue(checkouts, path.name)
            for step in checkouts:
                with self.subTest(workflow=path.name, step=step[0].strip()):
                    self.assertIn("persist-credentials: false", inputs(step))

    def test_the_pin_and_step_readers_on_the_shapes_they_must_tell_apart(self):
        sha = "3d3c42e5aac5ba805825da76410c181273ba90b1"
        for value in (f"actions/checkout@{sha}", f"'actions/checkout@{sha}' # v7.0.1",
                      f"owner-1/re.po_1@{sha} # v10.2.33"):
            self.assertRegex(value, PINNED)
        for value in ("actions/checkout@v7.0.1", "actions/checkout@3d3c42e # v7", f"actions/checkout@{sha} # v7",
                      f"actions/checkout@{sha} # v7.0.1 node24", f"./local@{sha}", f"docker://python@{sha}"):
            self.assertNotRegex(value, PINNED)
        text = f"""jobs:
  one:
    steps:
      # a comment at the items' indentation
      - name: Check out
        uses: 'Actions/Checkout@{sha}' # v7.0.1
        with:
          fetch-depth: 1
          persist-credentials: false
      - run: |
          echo one

          echo two
      - uses: actions/checkout@{sha}
        with:
          persist-credentials: true
  two:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@{sha}
        env:
          persist-credentials: false
"""
        found = steps(text)
        checkout = f"- uses: actions/checkout@{sha}"
        self.assertEqual([step[0].strip() for step in found], ["- name: Check out", "- run: |", checkout, checkout])
        self.assertEqual([is_checkout(step) for step in found], [True, False, True, True])
        self.assertEqual(inputs(found[0]), ["fetch-depth: 1", "persist-credentials: false"])
        self.assertEqual(inputs(found[2]), ["persist-credentials: true"])
        self.assertEqual(inputs(found[3]), [])
        self.assertEqual(len([line for line in text.splitlines() if ANY_USES.match(line)]), 3)
        flow = """jobs:
  one:
    runs-on: ubuntu-latest
    steps:
      - { name: x, uses: actions/checkout@v4 }
"""
        self.assertEqual([line for line in flow.splitlines() if USES.match(line)], [])
        self.assertEqual(len([line for line in flow.splitlines() if ANY_USES.match(line)]), 1)
        for line in (
            "- { uses: actions/checkout@v4 }",
            "- { name: x, uses: actions/checkout@v4 }",
            '- "uses": actions/checkout@v4',
            "uses : actions/checkout@v4",
        ):
            self.assertTrue(ANY_USES.match(line), line)
            self.assertIsNone(USES.match(line), line)

    def test_tests_run_for_pull_requests_and_for_pushes_to_main_only(self):
        # A push to a pull request's branch would otherwise run every job twice.
        text = (REPO / ".github" / "workflows" / "tests.yml").read_text(encoding="utf-8")
        self.assertIn("on:\n  push:\n    branches: [main]\n  pull_request:\n", text)

    def test_measurements_name_the_runner_they_ran_on(self):
        text = (REPO / ".github" / "workflows" / "model-smoke.yml").read_text(encoding="utf-8")
        self.assertEqual(text.count("BENCH_RUNNER: GitHub-hosted ubuntu-latest\n"), 3)
        self.assertEqual(text.count("BENCH_RUNNER:"), 3)

    def test_the_load_measurement_runs_on_demand_only(self):
        # It takes about 45 minutes and measures, never gates: no pull request, push or schedule may start it.
        text = (REPO / ".github" / "workflows" / "model-smoke.yml").read_text(encoding="utf-8")
        self.assertEqual(jobs(text)["load"][0], "if: github.event_name == 'workflow_dispatch'")
        # The weekly schedule runs on main too: a group without the event would cancel a dispatched run there.
        self.assertIn("  group: model-smoke-${{ github.event_name }}-${{ github.ref }}\n", text)

    def test_dependabot_updates_github_actions_weekly(self):
        text = (REPO / ".github" / "dependabot.yml").read_text(encoding="utf-8")
        self.assertIn("package-ecosystem: github-actions\n", text)
        self.assertIn("directory: /\n", text)
        self.assertIn("schedule:\n      interval: weekly\n", text)
        self.assertIn("commit-message:\n      prefix: ci\n", text)


if __name__ == "__main__":
    unittest.main()
