"""Every workflow job uses GitHub's hosted runners, with no self-hosted runner or repository-selected runner.

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
IMAGE_MATRIX = [
    "strategy:", "fail-fast: false", "matrix:", "include:",
    "- runner: ubuntu-latest", "platform: linux/amd64", "arch: amd64",
    "- runner: ubuntu-24.04-arm", "platform: linux/arm64", "arch: arm64",
]
# A step's `uses:` may follow its `name:` (so the dash is optional) and may quote its value.
USES = re.compile(r"^\s*(?:-\s+)?uses:\s*(.*?)\s*$")
# Every spelling of the key YAML accepts (quoted, a space before the colon, a flow-style step), so that a line
# USES cannot read is counted, not passed over.
ANY_USES = re.compile(r"""^\s*(?:-\s+)?(?:\{.*)?['"]?uses['"]?\s*:""")
# `owner/repo[/path]@<commit>`, and the version that commit is, as a comment, for the human reading the workflow.
PINNED = re.compile(
    r"""^['"]?[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*@[0-9a-f]{40}['"]?(?: +# v\d+\.\d+\.\d+)?$"""
)


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


def check_hosted_runners(text: str, workflow: str) -> None:
    """Only the image matrix and native publisher may select the hosted ARM runner."""
    found = jobs(text, workflow)
    if len(found) != runs_on_lines(text):
        raise AssertionError(f"{workflow}: unread job or runner")
    for name, lines in found.items():
        expected = RUNS_ON
        if (workflow, name) == ("tests.yml", "images"):
            expected = "runs-on: ${{ matrix.runner }}"
            if "strategy:" not in lines or "steps:" not in lines:
                raise AssertionError("images: missing native platform matrix")
            start, end = lines.index("strategy:"), lines.index("steps:")
            if lines[start:end] != IMAGE_MATRIX:
                raise AssertionError("images: only the two native hosted platforms are allowed")
        elif (workflow, name) == ("publish.yml", "build-arm64"):
            expected = "runs-on: ubuntu-24.04-arm"
        if [line for line in lines if line.startswith("runs-on:")] != [expected]:
            raise AssertionError(f"{workflow}/{name}: expected {expected}")


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


def job_steps(text: str, name: str) -> list:
    """Keep step indentation when selecting one job for exact comparisons."""
    block = re.search(rf"^  {name}:\n.*?(?=^  [\w-]+:|\Z)", text, re.MULTILINE | re.DOTALL)
    if block is None:
        raise AssertionError(f"missing job: {name}")
    return steps(block.group())


def run_scripts(text: str) -> list[str]:
    """Read inline and indented block scripts without including adjacent step fields."""
    found = []
    lines = text.splitlines()
    position = 0
    while position < len(lines):
        line = lines[position]
        position += 1
        key = re.match(r"""^( *)(?:- +)?['\"]?run['\"]? *: *(.*)$""", line)
        if key is None:
            continue
        value = key.group(2)
        if re.fullmatch(r"[|>][+-]?[1-9]? *(?:#.*)?", value):
            indent = line.index("run")
            body = []
            while position < len(lines):
                below = lines[position]
                if below.strip() and len(below) - len(below.lstrip()) <= indent:
                    break
                body.append(below)
                position += 1
            found.append("\n".join(body))
        else:
            found.append(value)
    return found


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
    def test_no_workflow_interpolates_expressions_into_run_scripts(self):
        for path in WORKFLOWS:
            for script in run_scripts(path.read_text(encoding="utf-8")):
                with self.subTest(workflow=path.name, script=script.splitlines()[0]):
                    self.assertNotIn("${{", script)

    def test_run_reader_separates_scripts_from_step_fields(self):
        text = """steps:
  - run: echo inline
  - name: Block
    env:
      VALUE: ${{ steps.previous.outputs.value }}
    run: |-
      echo "$VALUE"

      echo done
    timeout-minutes: 1
  - run: >
      echo folded
  - run: echo consecutive
  - run: |
      echo block
    env:
      VALUE: ${{ github.sha }}
  - run: echo last
"""
        self.assertEqual(run_scripts(text), [
            "echo inline", '      echo "$VALUE"\n\n      echo done', "      echo folded", "echo consecutive",
            "      echo block", "echo last",
        ])

    def test_publish_passes_each_comma_separated_tag_separately(self):
        text = (REPO / ".github/workflows/publish.yml").read_text(encoding="utf-8")
        tag_steps = [step for step in job_steps(text, "publish") if any("imagetools create" in line for line in step)]
        self.assertEqual(len(tag_steps), 2)
        for step in tag_steps:
            script = run_scripts("\n".join(step))[0]
            with self.subTest(step=step[0].strip()):
                self.assertIn('IFS=\',\' read -ra names <<< "$TAGS"', script)
                self.assertIn('for tag in "${names[@]}"; do tags+=(-t "$tag"); done', script)
                self.assertIn('imagetools create "${tags[@]}"', script)
                self.assertNotIn('-t "$TAGS"', script)

    def test_publish_validates_input_and_index_digests_before_output(self):
        text = (REPO / ".github/workflows/publish.yml").read_text(encoding="utf-8")
        tag_steps = [step for step in job_steps(text, "publish") if any("imagetools create" in line for line in step)]
        self.assertEqual(len(tag_steps), 2)
        for step in tag_steps:
            script = run_scripts("\n".join(step))[0]
            with self.subTest(step=step[0].strip()):
                self.assertIn("set -o pipefail", script)
                for variable, label in (("AMD64_DIGEST", "AMD64"), ("ARM64_DIGEST", "ARM64"), ("digest", "index")):
                    guard = f'[[ "${variable}" =~ ^sha256:[0-9a-f]{{64}}$ ]] || {{ echo "Invalid {label} digest" >&2; exit 1; }}'
                    self.assertIn(guard, script)
                    target = "imagetools create" if variable != "digest" else 'echo "digest=$digest" >> "$GITHUB_OUTPUT"'
                    self.assertLess(script.index(guard), script.index(target))
                self.assertIn("| jq -r '.digest')", script)
                self.assertLess(script.index("set -o pipefail"), script.index("digest=$("))
                self.assertLess(script.index("digest=$("), script.index('[[ "$digest"'))

    def test_publishing_requires_a_read_only_quality_gate(self):
        found = jobs((REPO / ".github/workflows/publish.yml").read_text(encoding="utf-8"))
        self.assertIn("needs: [quality, build-amd64, build-arm64]", found["publish"])
        gate = "\n".join(found["quality"])
        self.assertIn("if: startsWith(github.ref, 'refs/tags/v')", found["quality"])
        self.assertNotIn("continue-on-error", gate)
        self.assertNotIn("secrets.", gate)
        self.assertNotRegex(gate, r"python scripts/relevance_measure\.py[^\n]*\|\|\s*true")
        self.assertIn("contents: read", gate)
        self.assertNotIn(": write", gate)
        self.assertIn("RELEVANCE_DATASET: squad", gate)
        self.assertIn('RELEVANCE_GATE: "1"', gate)
        self.assertIn("python scripts/relevance_measure.py", gate)
        self.assertNotIn("if:", "\n".join(found["publish"]))
        for name, lines in found.items():
            if name.startswith("build-"):
                self.assertIn("needs: [quality]", lines, name)

    def test_native_image_checks_cover_both_platforms_and_gate_publication(self):
        tests = jobs((REPO / ".github/workflows/tests.yml").read_text(encoding="utf-8"))
        publish_text = (REPO / ".github/workflows/publish.yml").read_text(encoding="utf-8")
        publish = jobs(publish_text)
        image = tests["images"]
        self.assertIn("name: Worker images (${{ matrix.arch }})", image)
        self.assertEqual(image[image.index("strategy:"):image.index("steps:")], IMAGE_MATRIX)
        self.assertFalse(any(line.startswith("if:") for line in image[:image.index("steps:")]))
        self.assertEqual(set(publish), {"quality", "build-amd64", "build-arm64", "publish"})
        expected_checks = job_steps((REPO / ".github/workflows/tests.yml").read_text(encoding="utf-8"), "images")[-3:]
        expected_checks = [[line.replace("bridge-worker-onnx:ci", '"$IMAGE-onnx@$ONNX_DIGEST"')
                            .replace("bridge-worker:ci", '"$IMAGE@$WORKER_DIGEST"')
                            for line in step] for step in expected_checks]
        for step in expected_checks[:2]:
            if "        env:" not in step:
                step.insert(step.index("        run: |"), "        env:")
            start = step.index("        env:") + 1
            step[start:start] = [
                "          WORKER_DIGEST: ${{ steps.worker.outputs.digest }}",
                "          ONNX_DIGEST: ${{ steps.onnx.outputs.digest }}",
            ]
        for arch, runner in (("amd64", "ubuntu-latest"), ("arm64", "ubuntu-24.04-arm")):
            with self.subTest(arch=arch):
                lines = publish[f"build-{arch}"]
                self.assertIn(f"runs-on: {runner}", lines)
                self.assertIn("needs: [quality]", lines)
                header = lines[:lines.index("steps:")]
                self.assertFalse(any(line.startswith(("if:", "strategy:")) for line in header))
                self.assertIn("worker: ${{ steps.worker.outputs.digest }}", header)
                self.assertIn("onnx: ${{ steps.onnx.outputs.digest }}", header)
                self.assertIn('DOCKER_BUILD_RECORD_UPLOAD: "false"', header)
                block = "\n".join(lines)
                for forbidden in ("continue-on-error", "tags:", "load:", "push: false"):
                    self.assertNotIn(forbidden, block)
                build_steps = job_steps(publish_text, f"build-{arch}")
                builds = [step for step in build_steps if action(step).startswith("docker/build-push-action@")]
                self.assertEqual(len(builds), 2)
                for step, suffix in zip(builds, ("", "-onnx")):
                    self.assertIn(f"platforms: linux/{arch}", inputs(step))
                    self.assertIn(f"file: docker/Dockerfile.worker{suffix}", inputs(step))
                    self.assertIn(f"outputs: type=image,name=${{{{ env.IMAGE }}}}{suffix},push-by-digest=true,name-canonical=true,push=true", inputs(step))
                    self.assertIn("provenance: true", inputs(step))
                    self.assertIn("sbom: true", inputs(step))
                pull = next(i for i, step in enumerate(build_steps) if step[0].strip() == "- name: Pull the pushed images")
                self.assertGreater(pull, max(build_steps.index(step) for step in builds))
                for step in build_steps[pull:pull + 3]:
                    self.assertIn("WORKER_DIGEST: ${{ steps.worker.outputs.digest }}", [line.strip() for line in step])
                    self.assertIn("ONNX_DIGEST: ${{ steps.onnx.outputs.digest }}", [line.strip() for line in step])
                self.assertEqual([line.strip() for line in run_scripts("\n".join(build_steps[pull]))[0].splitlines()], [
                    'docker pull "$IMAGE@$WORKER_DIGEST"',
                    'docker pull "$IMAGE-onnx@$ONNX_DIGEST"',
                ])
                self.assertEqual(build_steps[pull + 1:], expected_checks)

    def test_no_workflow_uses_qemu_or_publish_artifacts(self):
        for path in WORKFLOWS:
            self.assertNotIn("setup-qemu-action", path.read_text(encoding="utf-8"))
        text = (REPO / ".github/workflows/publish.yml").read_text(encoding="utf-8")
        self.assertNotIn("upload-artifact", text)
        self.assertNotIn("download-artifact", text)

    def test_publish_combines_and_attests_the_tested_digests(self):
        text = (REPO / ".github/workflows/publish.yml").read_text(encoding="utf-8")
        publish = "\n".join(jobs(text)["publish"])
        self.assertNotIn("docker/build-push-action", publish)
        for name, suffix in (("worker", ""), ("onnx", "-onnx")):
            for arch in ("amd64", "arm64"):
                self.assertIn(f"{arch.upper()}_DIGEST: ${{{{ needs.build-{arch}.outputs.{name} }}}}", publish)
            self.assertIn(f'TAGS: ${{{{ steps.version.outputs.{name} }}}}', publish)
            self.assertIn(f'docker buildx imagetools create "${{tags[@]}}" "$IMAGE{suffix}@$AMD64_DIGEST" "$IMAGE{suffix}@$ARM64_DIGEST"', publish)
            self.assertIn(f'''docker buildx imagetools inspect "$IMAGE{suffix}:$VERSION" --format '{{{{json .Manifest}}}}' ''', publish)
        attestations = [step for step in job_steps(text, "publish") if action(step).startswith("actions/attest-build-provenance@")]
        self.assertEqual(len(attestations), 2)
        for step, name, suffix in zip(attestations, ("worker", "onnx"), ("", "-onnx")):
            self.assertIn(f"subject-name: ${{{{ env.IMAGE }}}}{suffix}", inputs(step))
            self.assertIn(f"subject-digest: ${{{{ steps.{name}.outputs.digest }}}}", inputs(step))
            self.assertIn("push-to-registry: true", inputs(step))

    def test_publishing_permissions_are_scoped_to_each_job(self):
        text = (REPO / ".github/workflows/publish.yml").read_text(encoding="utf-8")
        self.assertIn("\npermissions:\n  contents: read\n", text)
        for name, lines in jobs(text).items():
            start = lines.index("permissions:") + 1
            permissions = list(itertools.takewhile(lambda line: line.startswith(("contents:", "packages:", "id-token:", "attestations:")), lines[start:]))
            expected = ["contents: read"]
            if name.startswith("build-"):
                expected += ["packages: write"]
            elif name == "publish":
                expected = ["contents: write", "packages: write", "id-token: write", "attestations: write"]
            self.assertEqual(permissions, expected, name)

    def test_model_workers_disable_swap_and_release_passes_selection(self):
        text = (REPO / "docker/compose.yaml").read_text(encoding="utf-8")
        for name in ("model-worker", "model-worker-image"):
            block = re.search(rf"^  {name}:.*?(?=^  [\w-]+:|\Z)", text, re.MULTILINE | re.DOTALL).group()
            self.assertIn("mem_limit: 1536m", block)
            self.assertIn("memswap_limit: 1536m", block)
            self.assertIn("BRIDGE_RERANK_MODEL: ${BRIDGE_RERANK_MODEL:-tinybert}", block)

    def test_quantized_models_run_score_and_order_parity_in_model_job(self):
        text = (REPO / ".github/workflows/model-smoke.yml").read_text(encoding="utf-8")
        model = "\n".join(jobs(text)["model"])
        self.assertIn("for choice in minilm-l6 bge-base", model)
        self.assertIn("model-test sh -ec 'php tests/php/model_selected.php; php tests/php/model_parity.php'", model)

    def test_mcp_model_client_receives_the_workers_selection(self):
        text = (REPO / "docker/compose.yaml").read_text(encoding="utf-8")
        block = re.search(r"^  mcp-model:.*?(?=^  [\w-]+:|\Z)", text, re.MULTILINE | re.DOTALL).group()
        self.assertIn("BRIDGE_RERANK_MODEL: ${BRIDGE_RERANK_MODEL:-tinybert}", block)

    def test_the_workflows_are_found(self):
        self.assertEqual([path.name for path in WORKFLOWS], ["codeql.yml", "model-smoke.yml", "publish.yml", "tests.yml"])
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
            with self.subTest(workflow=path.name):
                check_hosted_runners(path.read_text(encoding="utf-8"), path.name)

    def test_runner_exceptions_are_scoped_and_reject_other_labels(self):
        for workflow in ("tests.yml", "publish.yml", "model-smoke.yml", "codeql.yml"):
            for name in ("images", "arm64", "build-amd64", "build-arm64", "php", "quality"):
                for label in ("ubuntu-latest", "ubuntu-24.04-arm", "self-hosted", "windows-latest",
                              "${{ vars.CI_RUNNER }}", "${{ matrix.runner }}", "[ubuntu-latest]", ""):
                    text = f"jobs:\n  {name}:\n    runs-on: {label}\n"
                    if (workflow, name) == ("tests.yml", "images"):
                        text += "\n".join(f"    {line}" for line in IMAGE_MATRIX) + "\n    steps:\n"
                        allowed = label == "${{ matrix.runner }}"
                    elif (workflow, name) == ("publish.yml", "build-arm64"):
                        allowed = label == "ubuntu-24.04-arm"
                    else:
                        allowed = label == "ubuntu-latest"
                    with self.subTest(workflow=workflow, job=name, label=label):
                        if allowed:
                            check_hosted_runners(text, workflow)
                        else:
                            with self.assertRaises(AssertionError):
                                check_hosted_runners(text, workflow)
        text = (REPO / ".github/workflows/tests.yml").read_text(encoding="utf-8")
        for label in ("self-hosted", "windows-latest", "${{ vars.CI_RUNNER }}", "ubuntu-latest"):
            with self.subTest(matrix_label=label), self.assertRaises(AssertionError):
                check_hosted_runners(text.replace("runner: ubuntu-24.04-arm", f"runner: {label}"), "tests.yml")

    def test_no_workflow_names_a_self_hosted_runner_or_a_runner_variable(self):
        # A `runs-on` expression is how a self-hosted runner or a fallback to one would come back. Only the
        # `runs-on` values are read for the label, so a comment may say "self-hosted".
        for path in WORKFLOWS:
            text = path.read_text(encoding="utf-8")
            with self.subTest(workflow=path.name):
                self.assertNotIn("vars.CI_RUNNER", text)
                self.assertNotIn("runner.environment", text)
                self.assertNotRegex(text, r"runs-on: *\[")
                check_hosted_runners(text, path.name)

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
                      f"owner-1/re.po_1@{sha} # v10.2.33", f"github/codeql-action/init@{sha} # v4.39.1"):
            self.assertRegex(value, PINNED)
        for value in ("actions/checkout@v7.0.1", "actions/checkout@3d3c42e # v7", f"actions/checkout@{sha} # v7",
                      f"actions/checkout@{sha} # v7.0.1 node24", f"./local@{sha}", f"docker://python@{sha}",
                      f"github/codeql-action/@{sha}", f"github/codeql-action//init@{sha}"):
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

    def test_the_gate_requires_the_image_builds(self):
        found = jobs((REPO / ".github" / "workflows" / "tests.yml").read_text(encoding="utf-8"), "tests.yml")
        self.assertIn("needs: [php, mcp, images, drills]", found["deterministic"])
        self.assertIn("if: always()", found["deterministic"])
        self.assertIn("IMAGES_RESULT: ${{ needs.images.result }}", found["deterministic"])
        self.assertTrue(any('"$IMAGES_RESULT" = success' in line for line in found["deterministic"]))

    def test_the_required_mcp_job_runs_instance_routing_and_hard_crash_recovery(self):
        found = jobs((REPO / ".github" / "workflows" / "tests.yml").read_text(encoding="utf-8"))
        self.assertIn("needs: [php, mcp, images, drills]", found["deterministic"])
        self.assertIn("run: sh scripts/ha_check.sh", found["mcp"])
        self.assertTrue(any("--profile ha config -q" in line for line in found["mcp"]))

    def test_the_gate_requires_failure_drills_and_promtool(self):
        found = jobs((REPO / ".github/workflows/tests.yml").read_text(encoding="utf-8"), "tests.yml")
        gate = "\n".join(found["deterministic"])
        self.assertIn("DRILLS_RESULT: ${{ needs.drills.result }}", gate)
        self.assertIn('"$DRILLS_RESULT" = success', gate)
        drill = "\n".join(found["drills"])
        self.assertIn("run: sh scripts/failure_drills.sh", drill)
        self.assertIn("run --rm --no-deps promtool", drill)
        self.assertIn("if: always()", drill)
        self.assertIn("--profile drills down -v", drill)
        compose = (REPO / "docker/compose.yaml").read_text(encoding="utf-8")
        self.assertRegex(compose, r"image: prom/prometheus:v[0-9.]+@sha256:[a-f0-9]{64}\n")
        self.assertIn("promtool check rules deploy/prometheus/alerts.yml", compose)
        self.assertIn("promtool test rules deploy/prometheus/alerts.test.yml", compose)

    def test_images_are_published_from_tags_with_the_job_token_only(self):
        # A pull request or a branch push must never reach a step that can write packages or releases.
        text = (REPO / ".github" / "workflows" / "publish.yml").read_text(encoding="utf-8")
        self.assertIn("on:\n  push:\n    tags: ['v*']\n  workflow_dispatch:\n", text)
        self.assertNotIn("pull_request", text)
        self.assertEqual(set(re.findall(r"secrets\.(\w+)", text)), {"GITHUB_TOKEN"})

    def test_publishing_starts_only_from_a_version_tag(self):
        # Every job that can publish refuses a manual branch run before login or push.
        text = (REPO / ".github" / "workflows" / "publish.yml").read_text(encoding="utf-8")
        triggers = re.search(r"^on:\n(.*?)^\S", text, re.MULTILINE | re.DOTALL)
        self.assertEqual(triggers.group(1), "  push:\n    tags: ['v*']\n  workflow_dispatch:\n\n")
        for name, lines in jobs(text).items():
            if not any("docker/login-action@" in line or "push=true" in line or "imagetools create" in line for line in lines):
                continue
            first = job_steps(text, name)[0]
            self.assertEqual(first[0].strip(), "- name: Require a version tag", name)
            guard = "\n".join(first)
            self.assertIn('case "$GITHUB_REF" in refs/tags/v*) ;; *)', guard)
            self.assertRegex(guard, r"\*\) echo .*>&2; exit 1 ;; esac")
        guard = "\n".join(job_steps(text, "publish")[0])
        self.assertIn('version="${TAG#v}"', guard)
        self.assertIn('"$IMAGE:$version" "$IMAGE-onnx:$version"', guard)
        self.assertIn('"$IMAGE:$version,$IMAGE:latest" "$IMAGE-onnx:$version,$IMAGE-onnx:latest"', guard)
        self.assertIn('*-*) echo "prerelease=true"', guard)

    def test_measurements_name_the_runner_they_ran_on(self):
        text = (REPO / ".github" / "workflows" / "model-smoke.yml").read_text(encoding="utf-8")
        self.assertEqual(text.count("BENCH_RUNNER: GitHub-hosted ubuntu-latest\n"), 4)
        self.assertEqual(text.count("BENCH_RUNNER:"), 4)

    def test_the_quality_job_runs_on_demand_and_for_release_tags(self):
        lines = jobs((REPO / ".github" / "workflows" / "model-smoke.yml").read_text(encoding="utf-8"))["quality"]
        self.assertEqual(lines[0], "if: github.event_name == 'workflow_dispatch' || github.event_name == 'push'")
        self.assertTrue(any("run --rm --no-deps quality-fetcher" in line for line in lines))
        self.assertTrue(any("run --rm --no-deps -T model-quality" in line for line in lines))
        text = (REPO / ".github" / "workflows" / "model-smoke.yml").read_text(encoding="utf-8")
        self.assertIn("  push:\n    tags: ['v*']\n", text)
        self.assertTrue(any("RELEVANCE_DATASET: squad" in line for line in lines))
        self.assertTrue(any('RELEVANCE_GATE: "1"' in line for line in lines))

    def test_the_load_measurement_runs_on_demand_only(self):
        # It takes about 45 minutes and measures, never gates: no pull request, push or schedule may start it.
        text = (REPO / ".github" / "workflows" / "model-smoke.yml").read_text(encoding="utf-8")
        self.assertEqual(jobs(text)["load"][0], "if: github.event_name == 'workflow_dispatch'")
        # The weekly schedule runs on main too: a group without the event would cancel a dispatched run there.
        self.assertIn("  group: model-smoke-${{ github.event_name }}-${{ github.ref }}\n", text)

    def test_dependabot_updates_actions_and_both_package_ecosystems_weekly(self):
        text = (REPO / ".github" / "dependabot.yml").read_text(encoding="utf-8")
        for ecosystem, prefix in (("github-actions", "ci"), ("pip", "deps"), ("composer", "deps")):
            with self.subTest(ecosystem=ecosystem):
                self.assertIn(
                    f"  - package-ecosystem: {ecosystem}\n    directory: /\n    schedule:\n      interval: weekly\n"
                    f"    commit-message:\n      prefix: {prefix}\n",
                    text,
                )
        self.assertEqual(text.count("package-ecosystem:"), 3)

    def test_packagist_install_job_and_triggers(self):
        text = (REPO / ".github" / "workflows" / "model-smoke.yml").read_text(encoding="utf-8")
        triggers = re.search(r"^on:\n(.*?)^\S", text, re.M | re.S)
        self.assertIsNotNone(triggers)
        self.assertEqual(triggers.group(1), """  workflow_dispatch:
  push:
    tags: ['v*']
  # Weekly, so a change upstream (a pinned file removed, a wheel yanked) is noticed without a PR.
  # GitHub runs schedules on the default branch only and pauses them after 60 days without commits.
  schedule:
    - cron: "17 6 * * 1"
  # Any change that can alter model acquisition or inference. Not a required check: it downloads
  # model files and needs the network, which the deterministic gate deliberately does not.
  pull_request:
    paths:
      - scripts/fetch_model.py
      - requirements-model.txt
      - requirements-model.lock
      - worker/ai_bridge/tasks.py
      # The worker pool that runs every model job.
      - worker/ai_bridge/jobs.py
      # The worker's HTTP handling, which every model test and the MCP workers' health check go through.
      - worker/ai_bridge/server.py
      # Imported by both: the counters and the log lines every model job passes through.
      - worker/ai_bridge/metrics.py
      - worker/ai_bridge/log.py
      - scripts/benchmark.py
      - scripts/ner_measure.py
      - scripts/relevance_measure.py
      - scripts/rerank_reference.py
      - tests/fixtures/rerank-fp32.json
      - tests/php/model_parity.php
      - scripts/public_data.py
      - tests/fixtures/ner-sample.json
      - tests/php/model.php
      - tests/php/model_selected.php
      - tests/model_integration.py
      # What those smoke tests run through: the shared HTTP helpers, the PHP client and the demo route.
      - tests/integration.py
      - src/**
      - examples/bootstrap.php
      - examples/frankenphp/**
      - scripts/run_frankenphp.sh
      # The MCP server with the models behind it, and the probe that drives it over stdio.
      - mcp_server/**
      - requirements-mcp.lock
      - scripts/mcp_stdio_onnx.sh
      - tests/mcp_model_probe.py
      - tests/mcp/fixtures/eval/**
      - tests/mcp/semantic_reach.py
      - tests/mcp/evaluation-semantic.xml
      - tests/model_cases.py
      - docker/compose.yaml
      - .github/workflows/model-smoke.yml
      # Audited by the audit job below, with the two Python locks above.
      - composer.lock
      - .github/audit-ignore.txt
      - .github/scripts/audit.sh

""")
        found = jobs(text)
        self.assertIn("if: github.event_name == 'workflow_dispatch' || github.event_name == 'push'", found["packagist"])
        for name in ("model", "audit"):
            self.assertIn("if: github.event_name != 'push'", found[name])
        for name in ("load",):
            self.assertIn("if: github.event_name == 'workflow_dispatch'", found[name])
        job = "\n".join(found["packagist"])
        for forbidden in ("uses:", "container:", "services:", "env:", "secrets.", "github.token"):
            self.assertNotIn(forbidden, job)
        self.assertIn("timeout-minutes: 10", job)
        self.assertIn("composer require cristiangirlea/php-python-ai-bridge:^0.4 --no-scripts --no-plugins", job)
        self.assertIn("class_exists(PhpAiBridge\\Client::class)", job)
        self.assertIn("PhpAiBridge\\Client::MAX_DOCUMENTS === 512", job)
        self.assertIn("docker run --rm --network bridge composer:2@sha256:af98f42dfff7c68ba8d53c2164fd9fde1087b7d449514baa38c418b1f6bc4bac sh -ec '", job)

    def test_the_audit_job_runs_the_audit_script(self):
        text = (REPO / ".github" / "workflows" / "model-smoke.yml").read_text(encoding="utf-8")
        # What the script does is test_audit_script.py's; here, that the job runs it and nothing else audits.
        audit = jobs(text, "model-smoke.yml")["audit"]
        self.assertIn("run: sh .github/scripts/audit.sh", audit)
        self.assertNotIn("pip-audit", "\n".join(audit))
        # A pull request that changes any lock file runs the workflow, and with it the audit.
        for lock in ("requirements-model.lock", "requirements-mcp.lock", "composer.lock", ".github/scripts/audit.sh"):
            self.assertIn(f"      - {lock}\n", text)

    def test_every_accepted_advisory_has_a_reason(self):
        for line in (REPO / ".github" / "audit-ignore.txt").read_text(encoding="utf-8").splitlines():
            if line.strip() and not line.lstrip().startswith("#"):
                with self.subTest(line=line):
                    self.assertRegex(line, r"^[A-Z]+-[A-Za-z0-9-]+\s+\S")

    def test_codeql_runs_on_main_pull_requests_and_weekly_with_least_privilege(self):
        text = (REPO / ".github" / "workflows" / "codeql.yml").read_text(encoding="utf-8")
        self.assertIn("on:\n  push:\n    branches: [main]\n  pull_request:\n  schedule:\n", text)
        self.assertIn("permissions:\n  contents: read\n  security-events: write\n", text)
        self.assertIn("languages: python\n", text)


if __name__ == "__main__":
    unittest.main()
