"""Fixed-source hosted validation. This file never publishes or deploys."""
from collections import Counter
import ast
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import traceback
import xml.etree.ElementTree as ET


SERVER = {
    "test_server::test_initialize_and_list_tools",
    "test_server::test_stdio_startup_and_list_tools",
    "test_server::test_tool_call_uses_mocked_api",
}
UTILS = {"test_utils::" + name for name in (
    "test_is_file_writeable", "test_make_output_file", "test_make_output_path",
    "test_find_similar_filenames", "test_try_find_similar_files", "test_process_input_file",
)}
REGRESSION = {"test_list_voices::" + name for name in (
    "test_list_voices_includes_generated_voices[all]",
    "test_list_voices_includes_generated_voices[voice_generation]",
    "test_list_voices_keeps_all_categories",
    "test_list_voices_preserves_legacy_empty_response[response0]",
    "test_list_voices_preserves_legacy_empty_response[response1]",
    "test_list_voices_preserves_legacy_empty_response[response2]",
    "test_list_voices_explicit_empty_generated_category",
)}
REDS = {
    "test_list_voices::test_list_voices_includes_generated_voices[all]": "Voice Generation Voices:",
    "test_list_voices::test_list_voices_includes_generated_voices[voice_generation]": "Voice Generation Voices:",
    "test_list_voices::test_list_voices_keeps_all_categories": "designed-1",
    "test_list_voices::test_list_voices_explicit_empty_generated_category": "Voice Generation Voices:",
}
NEGATIVE = REGRESSION | (SERVER - {"test_server::test_stdio_startup_and_list_tools"})
REQUIRED = (
    "identity", "checkout-baseline", "checkout-candidate", "inputs",
    "sync-baseline", "sync-candidate", "environment",
    "baseline-full", "candidate-full", "baseline-negative",
    "restore-baseline", "baseline-build", "candidate-build", "source-integrity",
)

OLD_EMPTY = "Success. System Voices: [], Voice Cloning Voices: []"
OLD_MIXED = "Success. System Voices: ['Name: System, ID: system-1'], Voice Cloning Voices: ['Name: None, ID: clone-1']"
GENERATED_SUFFIX = "Voice Generation Voices: []"


def assertion_ast(source):
    try:
        statements = ast.parse(source.strip()).body
    except SyntaxError as error:
        raise AssertionError("Expected assertion syntax") from error
    assert len(statements) == 1 and isinstance(statements[0], ast.Assert), "Expected one assert statement"
    assert statements[0].msg is None, "Unexpected custom assertion message"
    return statements[0]


def expected_failure(key, failure):
    """Verify the actual old-output assertion, not a word appearing in a crash."""
    assert failure.attrib.get("type") in (None, "AssertionError", "builtins.AssertionError"), "Wrong failure type"
    message = failure.attrib.get("message", "")
    trace = failure.text or ""
    assert re.search(r"(?:^|[/\\])test_list_voices\.py:\d+: AssertionError$", trace.rstrip()), "Wrong traceback exception"
    first_line = message.splitlines()[0] if message else ""
    if first_line.startswith("AssertionError: "):
        first_line = first_line[len("AssertionError: "):]
    reported = assertion_ast(first_line)

    explicit_empty = key.endswith("::test_list_voices_explicit_empty_generated_category")
    mixed = key.endswith("::test_list_voices_keeps_all_categories")
    if explicit_empty:
        source_assert = 'assert call_list_voices(server, "voice_generation").text.endswith("Voice Generation Voices: []")'
        assert isinstance(reported.test, ast.Constant) and reported.test.value is False, "Expected failed endswith assertion"
        call = re.escape(repr(GENERATED_SUFFIX))
        assert re.search(r"where False = <built-in method endswith of str object at 0x[0-9a-fA-F]+>\(" + call + r"\)", message), "Wrong endswith argument/result"
        assert "= " + repr(OLD_EMPTY) + ".endswith" in message, "Wrong endswith receiver"
        assert not OLD_EMPTY.endswith(GENERATED_SUFFIX)
    else:
        needle = "designed-1" if mixed else "Voice Generation Voices:"
        old_output = OLD_MIXED if mixed else OLD_EMPTY
        source_assert = 'assert voice_id in text' if mixed else 'assert "Voice Generation Voices:" in result.text'
        comparison = reported.test
        assert isinstance(comparison, ast.Compare), "Expected membership assertion"
        assert len(comparison.ops) == 1 and isinstance(comparison.ops[0], ast.In), "Wrong assertion operator"
        assert len(comparison.comparators) == 1
        assert isinstance(comparison.left, ast.Constant) and comparison.left.value == needle, "Wrong missing value"
        actual = comparison.comparators[0]
        assert isinstance(actual, ast.Constant) and actual.value == old_output, "Wrong old successful output"
        assert needle not in old_output

    source_lines = [line.lstrip()[1:].strip() for line in trace.splitlines() if line.lstrip().startswith(">")]
    assert len(source_lines) == 1, "Ambiguous failed source statement"
    assert ast.dump(assertion_ast(source_lines[0])) == ast.dump(assertion_ast(source_assert)), "Wrong source assertion"
    error_assertions = []
    for line in trace.splitlines():
        match = re.fullmatch(r"E\s+(?:AssertionError:\s*)?(assert .+)", line)
        if match:
            error_assertions.append(ast.dump(assertion_ast(match.group(1))))
    assert ast.dump(reported) in error_assertions, "Message and traceback assertions disagree"


def parse_junit(path, expected, reds=None):
    expected = set(expected)
    reds = reds or {}
    cases = ET.parse(path).getroot().findall(".//testcase")
    actual = {}
    for case in cases:
        key = case.attrib["classname"].rsplit(".", 1)[-1] + "::" + case.attrib["name"]
        assert key not in actual, f"Duplicate testcase: {key}"
        assert case.find("error") is None, f"Test error: {key}"
        assert case.find("skipped") is None, f"Unexpected skip/xfail: {key}"
        failure = case.find("failure")
        actual[key] = failure
    assert set(actual) == expected, {"missing": sorted(expected - set(actual)), "extra": sorted(set(actual) - expected)}
    failed = {key for key, failure in actual.items() if failure is not None}
    assert failed == set(reds), {"failed": sorted(failed), "expected": sorted(reds)}
    for key in reds:
        expected_failure(key, actual[key])
    return {"cases": len(cases), "passed": len(cases) - len(failed), "failed": sorted(failed)}


def all_ok(stages):
    return all(stages.get(name, {}).get("ok") is True for name in REQUIRED)


class Validation:
    def __init__(self, config):
        self.config = config
        self.home = Path(__file__).resolve().parent
        self.output = self.home.parent / "artifacts"
        self.output.mkdir(exist_ok=True)
        self.work = Path(os.environ["RUNNER_TEMP"]) / "minimax-voice-listing-validation"
        self.work.mkdir(exist_ok=False)
        self.stages = {}
        self.env = dict(os.environ)
        for key in ("PIP_EXTRA_INDEX_URL", "UV_INDEX_URL", "UV_EXTRA_INDEX_URL", "PYTHONPATH", "PYTEST_ADDOPTS"):
            self.env.pop(key, None)
        self.env.update({
            "MINIMAX_API_KEY": "offline-test-placeholder",
            "MINIMAX_API_HOST": "https://api.example.invalid",
            "MINIMAX_API_RESOURCE_MODE": "url",
            "PYTHON_DOTENV_DISABLED": "1",
            "PIP_INDEX_URL": "https://pypi.org/simple",
            "UV_DEFAULT_INDEX": "https://pypi.org/simple",
            "UV_NO_CACHE": "1",
        })

    def record(self):
        report = {"config": self.config, "required_stages": REQUIRED,
                  "stages": self.stages, "passed": all_ok(self.stages)}
        (self.output / "result.json").write_text(json.dumps(report, indent=2) + "\n")

    def attempt(self, name, fn):
        try:
            detail = fn()
            self.stages[name] = {"ok": True, "detail": detail}
        except Exception as error:
            self.stages[name] = {"ok": False, "error": str(error)}
            (self.output / (name + ".error.txt")).write_text(traceback.format_exc())
        self.record()
        print(name, json.dumps(self.stages[name]), flush=True)
        return self.stages[name]["ok"]

    def run(self, name, args, cwd=None, expected_exit=0, timeout=900):
        result = subprocess.run(args, cwd=cwd, env=self.env, text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout)
        (self.output / (name + ".log")).write_text(result.stdout)
        assert result.returncode == expected_exit, f"{name}: exit {result.returncode}, expected {expected_exit}; see log"
        return result.stdout

    def identity(self):
        checks = {
            "GITHUB_REPOSITORY": self.config["repository"],
            "GITHUB_REPOSITORY_ID": str(self.config["repository_id"]),
            "GITHUB_REPOSITORY_OWNER_ID": str(self.config["owner_id"]),
            "GITHUB_REF": "refs/heads/" + self.config["validation_branch"],
        }
        for name, expected in checks.items():
            assert os.environ.get(name) == expected, f"Wrong execution target: {name}"
        assert os.environ.get("GITHUB_EVENT_NAME") in ("push", "workflow_dispatch")
        assert sys.version_info[:2] == (3, 12)
        for name in ("baseline_commit", "candidate_commit", "baseline_tree", "candidate_tree"):
            assert re.fullmatch(r"[0-9a-f]{40}", self.config[name]), f"Unfrozen {name}"
        assert self.config["candidate_commit"] != "0" * 40
        return checks

    def checkout(self, lane):
        path = self.work / lane
        path.mkdir()
        url = "https://github.com/" + self.config["repository"] + ".git"
        self.run(lane + "-git-init", ["git", "init", str(path)])
        self.run(lane + "-git-remote", ["git", "remote", "add", "origin", url], path)
        self.run(lane + "-git-fetch", ["git", "fetch", "--depth=1", "origin", self.config[lane + "_commit"]], path)
        self.run(lane + "-git-checkout", ["git", "checkout", "--detach", "FETCH_HEAD"], path)
        head = self.run(lane + "-head", ["git", "rev-parse", "HEAD"], path).strip()
        tree = self.run(lane + "-tree", ["git", "rev-parse", "HEAD^{tree}"], path).strip()
        assert head == self.config[lane + "_commit"]
        assert tree == self.config[lane + "_tree"]
        assert not (path / ".env").exists()
        return {"head": head, "tree": tree}

    def inputs(self):
        baseline, candidate = self.work / "baseline", self.work / "candidate"
        def files(root):
            raw = self.run(root.name + "-tracked-files", ["git", "ls-files", "-z"], root)
            return {name: (root / name).read_bytes() for name in raw.split("\0") if name}
        before, after = files(baseline), files(candidate)
        changed = {name for name in before.keys() | after.keys() if before.get(name) != after.get(name)}
        assert changed == {"minimax_mcp/server.py", "tests/test_list_voices.py"}, changed
        for name in ("uv.lock", "pyproject.toml", "setup.py", "tests/test_server.py", "tests/test_utils.py", "tests/conftest.py"):
            assert before[name] == after[name]
        assert b'addopts = "-v --cov=minimax_mcp --cov-report=term-missing"' in before["pyproject.toml"]
        return {"changed_files": sorted(changed), "lock_sha256": hashlib.sha256(before["uv.lock"]).hexdigest()}

    def sync(self, lane):
        path = self.work / lane
        self.run(lane + "-sync", ["uv", "sync", "--locked", "--extra", "dev", "--python", "3.12", "--no-cache"], path)
        self.run(lane + "-config-diff", ["git", "diff", "--exit-code", "--", "uv.lock", "pyproject.toml"], path)
        return "Complete original project plus dev extra installed with locked configuration"

    def environment(self):
        versions = {}
        script = (
            "import json,sys,re;from importlib.metadata import distributions;"
            "print(json.dumps({'python':sys.version,'packages':{re.sub(r'[-_.]+','-',d.metadata['Name']).lower():d.version for d in distributions()}}))"
        )
        for lane in ("baseline", "candidate"):
            path = self.work / lane
            versions[lane] = json.loads(self.run(lane + "-versions", [str(path / ".venv/bin/python"), "-c", script], path))
            assert versions[lane]["packages"]["mcp"] == "2.0.0"
            assert versions[lane]["packages"]["mcp-types"] == "2.0.0"
            for name in ("pytest", "pytest-cov", "pre-commit", "ruff", "build", "twine", "sounddevice", "soundfile", "python-levenshtein"):
                assert name in versions[lane]["packages"], name
        assert versions["baseline"] == versions["candidate"]
        return versions

    def guard(self, lane, name, allow_stdio):
        root = self.work / lane
        site = root / ".venv/lib/python3.12/site-packages"
        assert site.is_dir()
        targets = [site / filename for filename in ("sitecustomize.py", "minimax_ci_guard.py", "minimax_ci_guard_state.json")]
        assert not any(path.exists() for path in targets)
        shutil.copyfile(self.home / "guard.py", targets[1])
        log = self.output / (name + "-guard.jsonl")
        targets[2].write_text(json.dumps({"source": str(root), "log": str(log), "allow_stdio": allow_stdio}))
        targets[0].write_text(
            "import os\ntry:\n import minimax_ci_guard\n minimax_ci_guard.install()\nexcept BaseException:\n os._exit(97)\n"
        )
        return targets, log

    def tests(self, lane, name, negative=False):
        root = self.work / lane
        xml = self.output / (name + ".xml")
        targets, log = self.guard(lane, name, not negative)
        args = [str(root / ".venv/bin/python"), "-m", "pytest", "--junitxml=" + str(xml)]
        expected = SERVER | UTILS | (REGRESSION if lane == "candidate" else set())
        if negative:
            args += ["tests/test_list_voices.py", "tests/test_server.py::test_initialize_and_list_tools",
                     "tests/test_server.py::test_tool_call_uses_mocked_api"]
            expected = NEGATIVE
        try:
            self.run(name, args, root, expected_exit=1 if negative else 0)
            result = parse_junit(xml, expected, REDS if negative else {})
            events = [json.loads(line)["event"] for line in log.read_text().splitlines()]
            counts = Counter(events)
            assert counts["violation"] == 0
            assert counts["activated"] >= (1 if negative else 3)
            assert counts["approved_stdio_spawn"] == (0 if negative else 2)
            assert (root / ".coverage").is_file(), "Original coverage plugin did not produce coverage data"
            shutil.copyfile(root / ".coverage", self.output / (name + ".coverage"))
            result["guard_events"] = dict(counts)
            return result
        finally:
            for path in targets:
                path.unlink(missing_ok=True)

    def negative(self):
        target = self.work / "baseline/tests/test_list_voices.py"
        assert not target.exists()
        shutil.copyfile(self.work / "candidate/tests/test_list_voices.py", target)
        return self.tests("baseline", "baseline-negative", negative=True)

    def restore(self):
        (self.work / "baseline/tests/test_list_voices.py").unlink(missing_ok=True)
        return "Removed only the copied regression file from disposable baseline"

    def build(self, lane):
        root = self.work / lane
        self.run(lane + "-build", [str(root / ".venv/bin/python"), "-m", "build"], root)
        artifacts = list((root / "dist").iterdir())
        assert len(artifacts) == 2
        assert sum(path.name.endswith(".whl") for path in artifacts) == 1
        assert sum(path.name.endswith(".tar.gz") for path in artifacts) == 1
        return {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in artifacts}

    def integrity(self):
        for lane in ("baseline", "candidate"):
            self.run(lane + "-tracked-diff", ["git", "diff", "--exit-code", "HEAD", "--"], self.work / lane)
        assert not (self.work / "baseline/tests/test_list_voices.py").exists()
        return "No tracked source/test/config files mutated by validation"

    def execute(self):
        if not self.attempt("identity", self.identity):
            return 1
        ready = {lane: self.attempt("checkout-" + lane, lambda lane=lane: self.checkout(lane)) for lane in ("baseline", "candidate")}
        if not all(ready.values()) or not self.attempt("inputs", self.inputs):
            return 1
        ready = {lane: self.attempt("sync-" + lane, lambda lane=lane: self.sync(lane)) for lane in ("baseline", "candidate")}
        if not all(ready.values()) or not self.attempt("environment", self.environment):
            return 1
        self.attempt("baseline-full", lambda: self.tests("baseline", "baseline-full"))
        self.attempt("candidate-full", lambda: self.tests("candidate", "candidate-full"))
        self.attempt("baseline-negative", self.negative)
        self.attempt("restore-baseline", self.restore)
        self.attempt("baseline-build", lambda: self.build("baseline"))
        self.attempt("candidate-build", lambda: self.build("candidate"))
        self.attempt("source-integrity", self.integrity)
        self.record()
        return 0 if all_ok(self.stages) else 1


if __name__ == "__main__":
    config = json.loads(Path(__file__).with_name("config.json").read_text())
    validator = Validation(config)
    try:
        status = validator.execute()
    finally:
        validator.record()
    raise SystemExit(status)
