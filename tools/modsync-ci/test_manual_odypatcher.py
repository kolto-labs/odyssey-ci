import copy
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest

HERE = Path(__file__).resolve().parent


def load(name):
    spec = importlib.util.spec_from_file_location(name, HERE / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


manual = load("manual")

GUIDE = """# Example Build

## Mod List

### Example Mod A

Prose.

### Example Mod B

Prose.

### Example Mod C

Prose.
"""

HEADINGS = ["### Example Mod A", "### Example Mod B", "### Example Mod C"]
SELECT = manual.SELECTION_WINDOW
PAYLOAD = b"installed by odypatcher\n"

# Stands in for OdyPatcher: records its argv, writes one game file, then exits with FAKE_ODY_EXIT
# after printing FAKE_ODY_ERROR (if set) the way OdyPatcher prints errors.
FAKE_ODY = """#!%s
import json, os, sys
from pathlib import Path
argv = sys.argv[1:]
with open(os.environ["FAKE_ODY_RECORD"], "a", encoding="utf-8") as record:
    record.write(json.dumps({"argv": argv, "cwd": os.getcwd()}) + "\\n")
game = Path(argv[argv.index("--game-dir") + 1])
(game / "override").mkdir(exist_ok=True)
(game / "override" / "a.txt").write_bytes(%r)
print("[Info] [00:00:00] Patching a.txt")
if os.environ.get("FAKE_ODY_ERROR"):
    print("[Error] [00:00:01] " + os.environ["FAKE_ODY_ERROR"], file=sys.stderr)
sys.exit(int(os.environ.get("FAKE_ODY_EXIT", "0")))
""" % (sys.executable, PAYLOAD)


def delete_step(name, heading, path=None, missing_ok=True):
    operation = {"type": "delete", "path": path or "{game}/" + name + ".txt"}
    if missing_ok:
        operation["missing_ok"] = True
    return {"id": name, "source_heading": heading, "operations": [operation]}


def omit_step(name, heading):
    return {"id": name, "source_heading": heading, "omit": {"reason": "no archive", "reviewed_by": "Reviewer"}}


class Fixture(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.guide = self.root / "guide.md"
        self.guide.write_text(GUIDE, encoding="utf-8")
        self.archives = self.root / "archives"
        self.game = self.root / "game"
        (self.archives / "mod" / "tslpatchdata").mkdir(parents=True)
        (self.archives / "mod" / "tslpatchdata" / "changes.ini").write_text("[Settings]\n", encoding="utf-8")
        (self.archives / "mod" / "HoloPatcher.exe").write_bytes(b"original holopatcher")
        (self.archives / "mod" / "TSLPatcher.exe").write_bytes(b"original tslpatcher")
        self.game.mkdir()
        (self.game / "vanilla.txt").write_text("x", encoding="utf-8")
        self.tools = self.root / "tools"
        self.tools.mkdir()
        self.odypatcher = self.tools / "odypatcher"
        self.odypatcher.write_text(FAKE_ODY, encoding="utf-8")
        self.odypatcher.chmod(0o755)
        self.record = self.root / "ody-calls.jsonl"

    def roots(self):
        return {"game": self.game, "archives": self.archives, "extract": self.root / "work" / "extract"}

    def plan(self, steps):
        bound = {"{archives}/mod/" + name: manual.sha256(self.archives / "mod" / name)
                 for name in ("HoloPatcher.exe", "TSLPatcher.exe")}
        return {"guide_sha256": manual.sha256(self.guide), "archive_sha256": bound,
                "steps": steps, "supplemental_sections": []}

    def cli_patcher(self, index="1"):
        argv = ["{archives}/mod/HoloPatcher.exe", "--game-dir", "{game}", "--tslpatchdata", "{archives}/mod"]
        if index is not None:
            argv += ["--namespace-option-index", index]
        return {"type": "patcher", "patcher": "{archives}/mod/HoloPatcher.exe", "sha256": "0" * 64,
                "argv": argv, "cwd": "{archives}/mod", "success_log": "{extract}/installlog.txt",
                "success_markers": ["done"], "failure_regex": ["Error"],
                "expected_files": {"{game}/override/a.txt": manual.hashlib.sha256(PAYLOAD).hexdigest()}}

    def gui_patcher(self, keys=("Down", "Down")):
        actions = [{"wait_window": SELECT, "timeout": 5}]
        if keys:
            actions.append({"key": list(keys), "window": SELECT})
        actions += [{"click": [248, 181], "window": SELECT}, {"wait_log": 5}]
        return {"type": "gui_patcher", "patcher": "{archives}/mod/TSLPatcher.exe", "sha256": "0" * 64,
                "argv": ["/usr/bin/wine", "{archives}/mod/TSLPatcher.exe"], "cwd": "{archives}/mod",
                "success_log": "{extract}/installlog.rtf", "success_markers": ["done"],
                "failure_regex": ["Error"], "expected_changes": "changed_manifest", "actions": actions}

    def patch_plan(self, operation, log_delete=True):
        """Mod A runs the patcher, Mod B is omitted, Mod C deletes the original's log (no missing_ok)."""
        third = (delete_step("step-003", HEADINGS[2], operation["success_log"], missing_ok=False) if log_delete
                 else delete_step("step-003", HEADINGS[2]))
        return self.plan([{"id": "step-001", "source_heading": HEADINGS[0], "operations": [operation]},
                          omit_step("step-002", HEADINGS[1]), third])

    def run_executor(self, plan, odypatcher=True, barrier=None, extra=(), environment=None):
        plan_path = self.root / "plan.json"
        plan_path.write_text(json.dumps(plan), encoding="utf-8")
        work, report = self.root / "work", self.root / "out" / "report.json"
        command = [sys.executable, str(HERE / "manual.py"), "--plan", str(plan_path), "--guide", str(self.guide),
                   "--game-dir", str(self.game), "--archives", str(self.archives),
                   "--work-dir", str(work), "--report", str(report)] + list(extra)
        if odypatcher:
            command += ["--odypatcher", str(self.odypatcher)]
        env = {key: value for key, value in os.environ.items() if key != "LOCKSTEP_BARRIER_DIR"}
        env.update({"FAKE_ODY_RECORD": str(self.record)}, **(environment or {}))
        if barrier is not None:
            env["LOCKSTEP_BARRIER_DIR"] = str(barrier)
        result = subprocess.run(command, capture_output=True, text=True, timeout=120, env=env)
        data = json.loads(report.read_text(encoding="utf-8")) if report.exists() else None
        return result, data, work

    def calls(self):
        if not self.record.exists():
            return []
        return [json.loads(line) for line in self.record.read_text(encoding="utf-8").splitlines()]

    def validate(self, plan):
        manual.validate_plan(plan, self.guide, self.roots(), 1800)

    def assertBlocked(self, operation, message):
        with self.assertRaises(manual.Blocked) as caught:
            manual.validate_operation(operation, self.roots())
        self.assertIn(message, str(caught.exception))


class ReviewedFields(Fixture):
    def test_plan_fields_are_accepted_in_every_mode(self):
        gui = dict(self.gui_patcher(), namespace_index=2, namespace_index_evidence="two Down presses")
        cli = dict(self.cli_patcher(), namespace_index=1)
        none = dict(self.gui_patcher(), namespace_index=None)
        none["actions"] = [{"wait_log": 5}]
        for operation in (gui, cli, none):
            manual.validate_operation(operation, self.roots())

    def test_bad_values_are_rejected(self):
        for value in (-1, True, "1", 1.0):
            self.assertBlocked(dict(self.gui_patcher(), namespace_index=value),
                               "namespace_index must be null or a non-negative integer")
        self.assertBlocked(dict(self.gui_patcher(), namespace_index_evidence="x"),
                           "namespace_index_evidence requires namespace_index")
        self.assertBlocked(dict(self.gui_patcher(), namespace_index=2, namespace_index_evidence=" "),
                           "namespace_index_evidence must be a nonempty printable string")
        self.assertBlocked(dict(self.cli_patcher(), odypatcher_exempt="x", namespace_index=1),
                           "odypatcher_exempt operation must not declare OdyPatcher inputs")
        self.assertBlocked(dict(self.cli_patcher(), tslpatchdata="{game}/mod"), "path uses wrong root")
        self.assertBlocked(dict(self.cli_patcher(), unknown=1), "invalid object fields")

    def test_index_must_agree_with_keys_or_argv(self):
        self.assertBlocked(dict(self.gui_patcher(), namespace_index=1),
                           "namespace_index disagrees with the reviewed selection keys")
        self.assertBlocked(dict(self.cli_patcher("1"), namespace_index=0),
                           "namespace_index disagrees with the reviewed --namespace-option-index")
        self.assertBlocked(dict(self.cli_patcher(None), namespace_index=0),
                           "namespace_index disagrees with the reviewed --namespace-option-index")

    def test_undetermined_keys_need_evidence(self):
        operation = dict(self.gui_patcher(keys=("Down", "Up")), namespace_index=0)
        self.assertBlocked(operation, "needs namespace_index_evidence")
        operation["namespace_index_evidence"] = "namespaces.ini lists Main first"
        manual.validate_operation(operation, self.roots())


class DerivedInputs(Fixture):
    def test_selection_index_counts_down_presses(self):
        self.assertEqual(manual.selection_index(self.gui_patcher(keys=("Down", "Down"))), 2)
        self.assertEqual(manual.selection_index(self.gui_patcher(keys=())), 0)
        self.assertIsNone(manual.selection_index(dict(self.gui_patcher(), actions=[{"wait_log": 5}])))
        self.assertEqual(manual.selection_index(self.gui_patcher(keys=("Up",))), manual.UNRESOLVED)

    def test_inputs_come_from_argv_patcher_folder_or_fields(self):
        roots = self.roots()
        self.assertEqual(manual.ody_inputs(self.cli_patcher("3"), roots), (self.archives / "mod", 3))
        self.assertEqual(manual.ody_inputs(self.cli_patcher(None), roots), (self.archives / "mod", None))
        self.assertEqual(manual.ody_inputs(self.gui_patcher(), roots), (self.archives / "mod", 2))
        explicit = dict(self.gui_patcher(keys=("Up",)), namespace_index=4, namespace_index_evidence="x",
                        tslpatchdata="{archives}/mod/tslpatchdata")
        self.assertEqual(manual.ody_inputs(explicit, roots), (self.archives / "mod" / "tslpatchdata", 4))

    def test_missing_inputs_are_named(self):
        operation = self.cli_patcher()
        operation["argv"] = operation["argv"][:3]
        with self.assertRaises(manual.Blocked) as caught:
            manual.ody_inputs(operation, self.roots())
        self.assertIn("no reviewed tslpatchdata", str(caught.exception))
        with self.assertRaises(manual.Blocked) as caught:
            manual.ody_inputs(self.gui_patcher(keys=("Up",)), self.roots())
        self.assertIn("no reviewed namespace_index", str(caught.exception))

    def test_preflight_lists_every_operation(self):
        plan = self.plan([
            {"id": "step-001", "source_heading": HEADINGS[0],
             "operations": [self.cli_patcher(), self.gui_patcher(keys=("Up",))]},
            {"id": "step-002", "source_heading": HEADINGS[1],
             "operations": [dict(self.cli_patcher(None), odypatcher_exempt="Inno Setup installer")]},
            delete_step("step-003", HEADINGS[2])])
        self.validate(plan)
        summary = manual.ody_preflight(plan, self.roots())
        self.assertEqual(summary["patcher_operations"], 3)
        self.assertEqual(summary["resolved"], 1)
        self.assertEqual(summary["exempt"], [{"step": "step-002", "operation": 1, "reason": "Inno Setup installer"}])
        self.assertEqual(len(summary["unresolved"]), 1)
        self.assertEqual(summary["unresolved"][0]["step"], "step-001")
        self.assertEqual(summary["unresolved"][0]["operation"], 2)
        self.assertIn("no reviewed namespace_index", summary["unresolved"][0]["error"])


class OdyPatcherRun(Fixture):
    def test_cli_patcher_runs_odypatcher_natively(self):
        result, report, _ = self.run_executor(self.patch_plan(self.cli_patcher("1")))
        self.assertEqual(result.returncode, 0, result.stderr + json.dumps(report))
        self.assertEqual(report["status"], "partial")
        self.assertEqual(report["odypatcher"]["sha256"], manual.sha256(self.odypatcher))
        self.assertEqual(report["odypatcher"]["unresolved"], [])
        calls = self.calls()
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["argv"], ["--install", "--game-dir", str(self.game.resolve()),
                                            "--tslpatchdata", str(self.archives.resolve() / "mod"),
                                            "--namespace-option-index", "1", "--console"])
        self.assertEqual(calls[0]["cwd"], str(self.archives.resolve() / "mod"))
        self.assertEqual((self.game / "override" / "a.txt").read_bytes(), PAYLOAD)

    def test_gui_patcher_runs_without_wine_or_display(self):
        result, report, _ = self.run_executor(self.patch_plan(self.gui_patcher(keys=("Down", "Down"))),
                                              environment={"DISPLAY": "", "PATH": os.path.dirname(sys.executable)})
        self.assertEqual(result.returncode, 0, result.stderr + json.dumps(report))
        argv = self.calls()[0]["argv"]
        self.assertEqual(argv[argv.index("--tslpatchdata") + 1], str(self.archives.resolve() / "mod"))
        self.assertEqual(argv[argv.index("--namespace-option-index") + 1], "2")

    def test_gui_patcher_without_selection_passes_no_index(self):
        operation = dict(self.gui_patcher(), actions=[{"wait_log": 5}], namespace_index=None)
        result, report, _ = self.run_executor(self.patch_plan(operation))
        self.assertEqual(result.returncode, 0, result.stderr + json.dumps(report))
        self.assertNotIn("--namespace-option-index", self.calls()[0]["argv"])

    def test_nonzero_exit_fails_with_meaning_and_error_lines(self):
        result, report, _ = self.run_executor(self.patch_plan(self.cli_patcher()),
                                              environment={"FAKE_ODY_EXIT": "8", "FAKE_ODY_ERROR": "boom"})
        self.assertEqual(result.returncode, 1)
        self.assertEqual(report["status"], "failed")
        self.assertIn("OdyPatcher exit 8 (install completed with errors); [Error] [00:00:01] boom", report["error"])
        self.assertEqual(report["steps"][0]["status"], "rolled_back")
        self.assertFalse((self.game / "override").exists())

    def test_error_line_fails_even_with_exit_zero(self):
        result, report, _ = self.run_executor(self.patch_plan(self.cli_patcher()),
                                              environment={"FAKE_ODY_ERROR": "quiet failure"})
        self.assertEqual(result.returncode, 1)
        self.assertIn("OdyPatcher exit 0 but reported errors", report["error"])

    def test_refusal_codes_are_named(self):
        result, report, _ = self.run_executor(self.patch_plan(self.cli_patcher()), environment={"FAKE_ODY_EXIT": "4"})
        self.assertEqual(result.returncode, 1)
        self.assertIn("OdyPatcher exit 4 (refused: namespace index out of range)", report["error"])

    def test_game_checks_still_apply(self):
        operation = self.cli_patcher()
        operation["expected_files"] = {"{game}/override/a.txt": "f" * 64}
        result, report, _ = self.run_executor(self.patch_plan(operation))
        self.assertEqual(result.returncode, 1)
        self.assertIn("patcher expected file SHA256 mismatch", report["error"])

    def test_unresolved_operation_is_listed_then_fails_when_reached(self):
        result, report, _ = self.run_executor(self.patch_plan(self.gui_patcher(keys=("Up",))))
        self.assertEqual(result.returncode, 1)
        self.assertEqual([entry["step"] for entry in report["odypatcher"]["unresolved"]], ["step-001"])
        self.assertIn("no reviewed namespace_index", report["error"])
        self.assertIn("::warning title=OdyPatcher input missing::", result.stdout)
        self.assertEqual(self.calls(), [])

    def test_exempt_operation_runs_the_original(self):
        operation = dict(self.cli_patcher(), odypatcher_exempt="Inno Setup installer")
        result, report, _ = self.run_executor(self.patch_plan(operation))
        self.assertEqual(result.returncode, 1)
        self.assertIn("patcher SHA256 mismatch", report["error"])  # the original's identity check ran
        self.assertEqual(self.calls(), [])

    def test_original_log_delete_is_missing_ok_only_in_odypatcher_mode(self):
        roots = self.roots()
        roots["extract"].mkdir(parents=True)
        log_delete = {"type": "delete", "path": "{extract}/installlog.txt"}
        other = {"type": "delete", "path": "{extract}/other.txt"}
        mode = {"path": self.odypatcher, "logs": manual.ody_logs(self.patch_plan(self.cli_patcher()))}
        manual.execute(log_delete, roots, None, None, odypatcher=mode)
        for operation, odypatcher in ((log_delete, None), (other, mode)):
            with self.assertRaises(manual.Blocked) as caught:
                manual.execute(operation, roots, None, None, odypatcher=odypatcher)
            self.assertIn("delete target missing", str(caught.exception))

    def test_odypatcher_must_be_executable(self):
        self.odypatcher.chmod(0o644)
        result, report, work = self.run_executor(self.patch_plan(self.cli_patcher()))
        self.assertEqual(result.returncode, 1)
        self.assertIn("--odypatcher is not an executable file", result.stderr)
        self.assertFalse(work.exists())


class Coordinator:
    """Answers each NNNNNN.ready in a barrier dir like lockstep's pair: go, or stop at `stop_at`."""

    def __init__(self, directory, stop_at=None):
        self.directory, self.stop_at, self.seen = directory, stop_at, []
        self.done = threading.Event()
        self.thread = threading.Thread(target=self.serve, daemon=True)
        self.thread.start()

    def serve(self):
        number = 1
        while not self.done.is_set():
            ready = self.directory / ("%06d.ready" % number)
            if not ready.exists():
                time.sleep(0.05)
                continue
            self.seen.append(json.loads(ready.read_text(encoding="utf-8")))
            answer = "stop" if self.stop_at is not None and number >= self.stop_at else "go"
            (self.directory / ("%06d.%s" % (number, answer))).write_text(answer + "\n", encoding="utf-8")
            number += 1

    def close(self):
        self.done.set()
        self.thread.join(5)


class PairBarrier(Fixture):
    def barrier(self, stop_at=None):
        directory = self.root / "barrier"
        directory.mkdir()
        coordinator = Coordinator(directory, stop_at)
        self.addCleanup(coordinator.close)
        return directory, coordinator

    def test_every_step_reaches_the_barrier_in_order(self):
        directory, coordinator = self.barrier()
        result, report, _ = self.run_executor(self.patch_plan(self.cli_patcher()), barrier=directory)
        self.assertEqual(result.returncode, 0, result.stderr + json.dumps(report))
        self.assertEqual(coordinator.seen, [{"id": "step-%03d" % (index + 1), "label": heading, "exit": 0}
                                            for index, heading in enumerate(HEADINGS)])

    def test_both_modes_reach_the_same_ids(self):
        plan = self.patch_plan(self.cli_patcher(), log_delete=False)
        directory, coordinator = self.barrier()
        self.run_executor(plan, barrier=directory)
        ody = [entry["id"] for entry in coordinator.seen]
        self.assertEqual(ody, [step["id"] for step in plan["steps"]])

    def test_stop_after_a_step_reports_stopped_by_pair(self):
        directory, coordinator = self.barrier(stop_at=2)
        result, report, _ = self.run_executor(self.patch_plan(self.cli_patcher()), barrier=directory)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(report["status"], "stopped_by_pair")
        self.assertEqual([step["status"] for step in report["steps"]], ["complete", "omitted"])
        self.assertEqual(len(coordinator.seen), 2)

    def test_failed_step_reaches_barrier_before_rollback(self):
        directory, coordinator = self.barrier(stop_at=1)
        result, report, _ = self.run_executor(self.patch_plan(self.cli_patcher()), barrier=directory,
                                              environment={"FAKE_ODY_EXIT": "7"})
        self.assertEqual(result.returncode, 1)
        self.assertEqual(report["status"], "stopped_by_pair")
        self.assertEqual(coordinator.seen[0]["exit"], 1)
        self.assertIn("OdyPatcher exit 7", coordinator.seen[0]["error"])
        self.assertEqual(report["steps"][0]["status"], "failed_not_restored")
        # Left as the failure left it, for the coordinator's diff.
        self.assertEqual((self.game / "override" / "a.txt").read_bytes(), PAYLOAD)

    def test_failed_step_still_rolls_back_when_told_to_go(self):
        directory, coordinator = self.barrier()
        result, report, _ = self.run_executor(self.patch_plan(self.cli_patcher()), barrier=directory,
                                              environment={"FAKE_ODY_EXIT": "7"})
        self.assertEqual(result.returncode, 1)
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["steps"][0]["status"], "rolled_back")
        self.assertEqual([entry["exit"] for entry in coordinator.seen], [1])
        self.assertFalse((self.game / "override").exists())

    def test_no_barrier_dir_is_a_no_op(self):
        result, report, _ = self.run_executor(self.patch_plan(self.cli_patcher()))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.root / "barrier").exists())


class NoSnapshot(PairBarrier):
    def test_requires_a_pair(self):
        result, _, work = self.run_executor(self.patch_plan(self.cli_patcher()), extra=["--no-snapshot"])
        self.assertEqual(result.returncode, 1)
        self.assertIn("--no-snapshot is only for a lockstep pair", result.stderr)
        self.assertFalse(work.exists())

    def test_steps_run_without_snapshot(self):
        directory, coordinator = self.barrier()
        result, report, work = self.run_executor(self.patch_plan(self.cli_patcher()), barrier=directory,
                                                 extra=["--no-snapshot"])
        self.assertEqual(result.returncode, 0, result.stderr + json.dumps(report))
        self.assertFalse((work / "step-0001" / "before").exists())
        self.assertTrue((work / "step-0001" / "before.json").is_file())
        self.assertTrue((work / "step-0001" / "after.json").is_file())
        self.assertEqual(len(coordinator.seen), 3)

    def test_failure_is_left_for_the_pair(self):
        directory, coordinator = self.barrier(stop_at=1)
        result, report, _ = self.run_executor(self.patch_plan(self.cli_patcher()), barrier=directory,
                                              extra=["--no-snapshot"], environment={"FAKE_ODY_EXIT": "8"})
        self.assertEqual(result.returncode, 1)
        self.assertEqual(report["status"], "stopped_by_pair")
        self.assertEqual(report["steps"][0]["status"], "failed_not_restored")
        self.assertEqual(coordinator.seen[0]["exit"], 1)


class RealPlans(unittest.TestCase):
    def test_every_reviewed_patcher_has_odypatcher_inputs(self):
        directory = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: manual.shutil.rmtree(directory))
        roots = {"game": directory / "game", "archives": directory / "archives",
                 "extract": directory / "work" / "extract"}
        for game in ("k1", "k2"):
            plan = json.loads((HERE.parent.parent / "ci" / (game + "-manual-plan.json")).read_text(encoding="utf-8"))
            for step in plan["steps"]:
                for operation in step.get("operations", []):
                    manual.validate_operation(copy.deepcopy(operation), roots)
            summary = manual.ody_preflight(plan, roots)
            self.assertEqual(summary["unresolved"], [], game)


if __name__ == "__main__":
    unittest.main()
