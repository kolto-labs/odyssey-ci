import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
import unittest.mock

HERE = Path(__file__).resolve().parent


def load(name):
    spec = importlib.util.spec_from_file_location(name, HERE / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


lockstep = load("lockstep")


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def python(code):
    return {"argv": [sys.executable, "-c", code]}


class Fixture(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.a = self.root / "a"
        write(self.a / "override" / "base.2da", "base")
        write(self.a / "chitin.key", "key")
        self.b = self.root / "b"
        shutil.copytree(self.a, self.b)

    def run_steps(self, steps, evidence=False):
        path = self.root / "steps.json"
        path.write_text(json.dumps({"steps": steps}), encoding="utf-8")
        report = self.root / "report.json"
        argv = ["run", "--steps", str(path), "--tree", "holo=%s" % self.a, "--tree", "ody=%s" % self.b,
                "--work-dir", str(self.root / "work"), "--report", str(report)]
        if evidence:
            argv += ["--evidence-dir", str(self.root / "evidence")]
        code = lockstep.main(argv)
        return code, json.loads(report.read_text(encoding="utf-8"))

    def write_step(self, identifier, relative_a, text_a, relative_b=None, text_b=None):
        relative_b = relative_a if relative_b is None else relative_b
        text_b = text_a if text_b is None else text_b
        code = "import pathlib,sys; p=pathlib.Path(sys.argv[1]); p.parent.mkdir(parents=True, exist_ok=True); p.write_text(sys.argv[2])"
        return {"id": identifier, "commands": {
            "holo": {"argv": [sys.executable, "-c", code, str(self.a / relative_a), text_a]},
            "ody": {"argv": [sys.executable, "-c", code, str(self.b / relative_b), text_b]}}}


class Run(Fixture):
    def test_equal_steps_pass_and_hash_only_changed_files(self):
        code, report = self.run_steps([self.write_step("one", "override/new.2da", "x"),
                                       self.write_step("two", "override/base.2da", "changed")])
        self.assertEqual(code, 0)
        self.assertEqual(report["status"], "equivalent")
        self.assertEqual([step["hashed"] for step in report["steps"]], [1, 1])

    def test_content_difference_stops_at_that_step(self):
        code, report = self.run_steps([self.write_step("one", "override/a.2da", "same"),
                                       self.write_step("two", "override/b.2da", "abc", text_b="abd"),
                                       self.write_step("three", "override/c.2da", "never")], evidence=True)
        self.assertEqual(code, 1)
        self.assertEqual(report["status"], "discrepancy")
        self.assertEqual(report["stopped_at"]["id"], "two")
        self.assertEqual(len(report["steps"]), 2)
        [item] = report["stopped_at"]["discrepancies"]
        self.assertEqual(item["kind"], "content")
        self.assertEqual(item["first_difference"], 2)
        self.assertNotEqual(item["holo"]["sha256"], item["ody"]["sha256"])
        self.assertTrue((self.root / "evidence" / "step-0002" / "ody" / "override" / "b.2da").is_file())
        self.assertFalse((self.a / "override" / "c.2da").exists())

    def test_same_size_rewrite_is_seen(self):
        os.utime(self.b / "override" / "base.2da", ns=(1, 1))
        os.utime(self.a / "override" / "base.2da", ns=(1, 1))
        restore_mtime = ("import os,pathlib,sys; p=pathlib.Path(sys.argv[1]); p.write_text(sys.argv[2]);"
                         " os.utime(p, ns=(1, 1))")
        step = {"id": "same-size", "commands": {
            "holo": {"argv": [sys.executable, "-c", restore_mtime, str(self.a / "override/base.2da"), "base"]},
            "ody": {"argv": [sys.executable, "-c", restore_mtime, str(self.b / "override/base.2da"), "bAse"]}}}
        code, report = self.run_steps([step])
        self.assertEqual(code, 1)
        self.assertEqual(report["stopped_at"]["discrepancies"][0]["kind"], "content")

    def test_case_only_difference_is_its_own_kind(self):
        code, report = self.run_steps([self.write_step("one", "Movies/X.bik", "x", relative_b="movies/x.bik")])
        self.assertEqual(code, 1)
        kinds = [item["kind"] for item in report["stopped_at"]["discrepancies"]]
        self.assertEqual(kinds, ["case", "case"])

    def test_ignored_folder_is_left_out(self):
        path = self.root / "steps.json"
        path.write_text(json.dumps({"steps": [self.write_step("one", ".ModSync/checkpoint", "a", text_b="b")]}),
                        encoding="utf-8")
        report = self.root / "report.json"
        code = lockstep.main(["run", "--steps", str(path), "--tree", "holo=%s" % self.a, "--tree", "ody=%s" % self.b,
                              "--work-dir", str(self.root / "work"), "--report", str(report), "--ignore", ".modsync"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(report.read_text(encoding="utf-8"))["ignored"], [".modsync"])

    def test_missing_file_on_one_side(self):
        code_a = "import pathlib,sys; pathlib.Path(sys.argv[1]).unlink()"
        step = {"id": "delete", "commands": {
            "holo": {"argv": [sys.executable, "-c", code_a, str(self.a / "chitin.key")]},
            "ody": python("pass")}}
        code, report = self.run_steps([step])
        self.assertEqual(code, 1)
        self.assertEqual(report["stopped_at"]["discrepancies"][0]["kind"], "only_in_ody")

    def test_failure_on_one_side_waits_for_the_other_and_stops(self):
        step = {"id": "boom", "commands": {"holo": python("import sys; sys.exit(3)"),
                                           "ody": python("import time; time.sleep(0.2)")}}
        code, report = self.run_steps([step, self.write_step("after", "x.txt", "x")])
        self.assertEqual(code, 1)
        self.assertEqual(report["status"], "failed")
        results = report["steps"][0]["results"]
        self.assertEqual((results["holo"]["exit"], results["ody"]["exit"]), (3, 0))
        self.assertEqual(len(report["steps"]), 1)

    def test_steps_run_both_sides_at_the_same_time(self):
        barrier = self.root / "barrier"
        # Each side waits for the other's marker; run one after the other, this would time out.
        wait = ("import pathlib,sys,time; pathlib.Path(sys.argv[1]).touch(); o=pathlib.Path(sys.argv[2]);"
                " [time.sleep(0.05) for _ in range(100) if not o.exists()]; sys.exit(0 if o.exists() else 9)")
        step = {"id": "together", "commands": {
            "holo": {"argv": [sys.executable, "-c", wait, str(barrier) + ".holo", str(barrier) + ".ody"]},
            "ody": {"argv": [sys.executable, "-c", wait, str(barrier) + ".ody", str(barrier) + ".holo"]}}}
        code, report = self.run_steps([step])
        self.assertEqual(code, 0, report)

    def test_baseline_difference_runs_nothing(self):
        write(self.b / "extra.txt", "x")
        code, report = self.run_steps([self.write_step("one", "x.txt", "x")])
        self.assertEqual(code, 1)
        self.assertEqual(report["stopped_at"]["id"], "baseline")
        self.assertEqual(report["steps"], [])

    def test_steps_need_a_command_per_tree(self):
        with self.assertRaises(SystemExit):
            self.run_steps([{"id": "one", "commands": {"holo": python("pass")}}])

    def test_new_case_collision_fails_even_on_both_sides(self):
        # Windows would have overwritten override/base.2da; two files on Linux mean the patcher missed it.
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code, report = self.run_steps([self.write_step("dup", "Override/BASE.2da", "base")])
        self.assertEqual(code, 1)
        self.assertIn("ody has Override/BASE.2da and override/base.2da", output.getvalue())
        folder, item = report["stopped_at"]["discrepancies"]
        self.assertEqual((folder["kind"], item["kind"]), ("case_collision", "case_collision"))
        self.assertEqual(folder["spellings"]["ody"], ["Override", "override"])
        self.assertEqual(item["spellings"]["holo"], ["Override/BASE.2da", "override/base.2da"])

    def test_baseline_collision_is_not_a_patcher_bug_but_still_compared(self):
        for tree in (self.a, self.b):
            write(tree / "Override" / "Base.2da", "game")
        code, report = self.run_steps([self.write_step("same", "x.txt", "x")])
        self.assertEqual(code, 0, report)
        self.assertEqual(report["baseline"]["case_collisions"], ["override", "override/base.2da"])
        code, report = self.run_steps([self.write_step("diverge", "Override/Base.2da", "holo", text_b="odyy")])
        self.assertEqual(code, 1)
        self.assertEqual(report["stopped_at"]["discrepancies"][0]["kind"], "collision_content")

    def test_folder_in_other_case_still_compares_its_files(self):
        os.rename(self.b / "override", self.b / "Override")
        code, report = self.run_steps([self.write_step("one", "override/base.2da", "holo",
                                                       relative_b="Override/base.2da", text_b="odyy")])
        self.assertEqual(code, 1)
        self.assertEqual(report["stopped_at"]["id"], "baseline")
        kinds = sorted(item["kind"] for item in report["stopped_at"]["discrepancies"])
        self.assertEqual(kinds, ["case"])


BARRIER_CLIENT = """
import json, pathlib, sys
sys.path.insert(0, sys.argv[1])
import lockstep
barrier = lockstep.Barrier()
for step in json.loads(sys.argv[2]):
    path = pathlib.Path(step["path"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(step["text"])
    if not barrier.reach(step["id"], exit=step.get("exit", 0)):
        sys.exit(0)
sys.exit(int(sys.argv[3]))
"""


class Pair(Fixture):
    def run_pair(self, holo_steps, ody_steps, holo_exit=0, ody_exit=0):
        def side(root, steps, code):
            for step in steps:
                step["path"] = str(root / step.pop("relative"))
            return {"argv": [sys.executable, "-c", BARRIER_CLIENT, str(HERE), json.dumps(steps), str(code)]}
        path = self.root / "commands.json"
        path.write_text(json.dumps({"commands": {"holo": side(self.a, holo_steps, holo_exit),
                                                 "ody": side(self.b, ody_steps, ody_exit)}}), encoding="utf-8")
        report = self.root / "report.json"
        code = lockstep.main(["pair", "--commands", str(path), "--tree", "holo=%s" % self.a,
                              "--tree", "ody=%s" % self.b, "--work-dir", str(self.root / "work"),
                              "--report", str(report), "--step-timeout", "30"])
        return code, json.loads(report.read_text(encoding="utf-8"))

    @staticmethod
    def steps(*items):
        return [{"id": identifier, "relative": relative, "text": text} for identifier, relative, text in items]

    def test_equal_installs_pass(self):
        steps = (("a", "override/a.2da", "a"), ("b", "override/b.2da", "b"))
        code, report = self.run_pair(self.steps(*steps), self.steps(*steps))
        self.assertEqual(code, 0, report)
        self.assertEqual([step["id"] for step in report["steps"]], ["a", "b", "end"])
        self.assertEqual(report["exits"], {"holo": 0, "ody": 0})

    def test_difference_stops_both_at_that_mod(self):
        code, report = self.run_pair(self.steps(("a", "x.2da", "1"), ("b", "y.2da", "y")),
                                     self.steps(("a", "x.2da", "2"), ("b", "y.2da", "y")))
        self.assertEqual(code, 1)
        self.assertEqual(report["stopped_at"]["id"], "a")
        self.assertFalse((self.a / "y.2da").exists())
        self.assertFalse((self.b / "y.2da").exists())

    def test_a_side_reporting_its_mod_failed_stops_the_pair(self):
        holo = self.steps(("a", "x.2da", "x"))
        holo[0]["exit"] = 2
        code, report = self.run_pair(holo, self.steps(("a", "x.2da", "x")))
        self.assertEqual(code, 1)
        self.assertEqual(report["status"], "failed")
        self.assertIn("failed on holo (exit 2)", report["stopped_at"]["reason"])

    def test_a_mod_failing_the_same_on_both_sides_goes_on(self):
        holo, ody = self.steps(("a", "x.2da", "x"), ("b", "y.2da", "y")), self.steps(("a", "x.2da", "x"), ("b", "y.2da", "y"))
        holo[0]["exit"], ody[0]["exit"] = 2, 8
        code, report = self.run_pair(holo, ody)
        self.assertEqual(code, 0, report)
        self.assertEqual([step["id"] for step in report["steps"]], ["a", "b", "end"])
        self.assertEqual(report["steps"][0]["failed_on_both"], {"holo": "exit 2", "ody": "exit 8"})

    def test_installs_that_never_reach_a_barrier_fail(self):
        code, report = self.run_pair(self.steps(), self.steps())
        self.assertEqual(code, 1)
        self.assertIn("no step reached a barrier", report["stopped_at"]["reason"])

    def test_one_side_exiting_early_stops_the_other(self):
        code, report = self.run_pair(self.steps(), self.steps(("a", "x.2da", "x")), holo_exit=5)
        self.assertEqual(code, 1)
        self.assertIn("holo exited 5 before this barrier", report["stopped_at"]["reason"])

    def test_barriers_after_a_stop_answer_at_once(self):
        directory = self.root / "barrier"
        directory.mkdir()
        (directory / lockstep.STOP_ALL).touch()
        self.assertFalse(lockstep.Barrier(directory).reach("retry", timeout=5))

    def test_barrier_without_a_pair_is_a_no_op(self):
        with unittest.mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("LOCKSTEP_BARRIER_DIR", None)
            self.assertTrue(lockstep.Barrier().reach("anything"))


FAKE_PATCHER = """
import pathlib, sys
args = sys.argv[1:]
if "--install" not in args:
    print("probe"); sys.exit(0)
game = pathlib.Path(args[args.index("--game-dir") + 1])
(game / "override").mkdir(exist_ok=True)
(game / "override" / "patched.2da").write_text(sys.argv[0].rsplit("-", 1)[-1][:-3])
sys.exit(0)
"""

# Stands in for ModSync: runs the patcher it was given for two mods, the way ModSync runs an external patcher.
FAKE_INSTALLER = """
import pathlib, subprocess, sys
patcher, game, mods = sys.argv[1], sys.argv[2], sys.argv[3:]
for mod in mods:
    code = subprocess.call([patcher, "--install", "--game-dir", game, "--tslpatchdata", mod, "--console"])
    if code:
        sys.exit(code)
"""


class Wrapper(Fixture):
    def make_patcher(self, name, content):
        path = self.root / ("patcher-%s.py" % name)
        path.write_text("#!%s\n%s" % (sys.executable, content), encoding="utf-8")
        path.chmod(0o755)
        return path

    def mod(self, name, ini):
        folder = self.root / "extract" / name / "tslpatchdata"
        write(folder / "changes.ini", ini)
        return folder

    def run_wrapped(self, holo_patcher, ody_patcher, mods):
        work = self.root / "work"
        commands = {}
        for name, patcher, game in (("holo", holo_patcher, self.a), ("ody", ody_patcher, self.b)):
            wrapper = self.root / ("wrap-%s" % name)
            lockstep.main(["wrapper", "--real", str(patcher), "--barrier-dir", str(work / name / "barrier"),
                           "--out", str(wrapper)])
            commands[name] = {"argv": [sys.executable, "-c", FAKE_INSTALLER, str(wrapper), str(game)]
                              + [str(mod) for mod in mods]}
        path = self.root / "commands.json"
        path.write_text(json.dumps({"commands": commands}), encoding="utf-8")
        report = self.root / "report.json"
        code = lockstep.main(["pair", "--commands", str(path), "--tree", "holo=%s" % self.a,
                              "--tree", "ody=%s" % self.b, "--work-dir", str(work), "--report", str(report),
                              "--step-timeout", "30"])
        return code, json.loads(report.read_text(encoding="utf-8"))

    def test_equal_patchers_pass_with_one_barrier_per_patcher_run(self):
        same = self.make_patcher("same", FAKE_PATCHER.replace('sys.argv[0].rsplit("-", 1)[-1][:-3]', '"ok"'))
        mods = [self.mod("first", "[a]"), self.mod("second", "[b]")]
        code, report = self.run_wrapped(same, same, mods)
        self.assertEqual(code, 0, report)
        ids = [step["id"] for step in report["steps"]]
        self.assertEqual(len(ids), 3)
        self.assertEqual(ids[-1], "end")
        self.assertNotEqual(ids[0], ids[1])
        self.assertIn("first/tslpatchdata", report["steps"][0]["label"])

    def test_patch_after_a_stop_does_not_run_the_patcher(self):
        marker = self.root / "ran"
        patcher = self.make_patcher("marker", "open(%r, 'w').close()\n" % str(marker))
        barrier = self.root / "barrier"
        barrier.mkdir()
        (barrier / lockstep.STOP_ALL).touch()
        code = lockstep.main(["patch", "--real", str(patcher), "--barrier-dir", str(barrier), "--",
                              "--install", "--tslpatchdata", str(self.mod("first", "[a]"))])
        self.assertEqual(code, lockstep.STOPPED_EXIT)
        self.assertFalse(marker.exists())

    def test_patchers_that_write_different_bytes_stop_at_that_patcher_run(self):
        # Each fake patcher writes its own file name stem, so the two sides differ after the first mod.
        holo = self.make_patcher("holo", FAKE_PATCHER)
        ody = self.make_patcher("ody", FAKE_PATCHER)
        code, report = self.run_wrapped(holo, ody, [self.mod("first", "[a]"), self.mod("second", "[b]")])
        self.assertEqual(code, 1)
        self.assertEqual(report["status"], "discrepancy")
        self.assertEqual(len(report["steps"]), 1)
        self.assertEqual(report["stopped_at"]["discrepancies"][0]["holo"]["path"], "override/patched.2da")

    def test_id_ignores_where_each_side_extracted(self):
        one, two = self.root / "x" / "tslpatchdata", self.root / "y" / "deeper" / "tslpatchdata"
        write(one / "Changes.ini", "[x]")
        write(two / "changes.INI", "[x]")
        first = lockstep.patcher_step(["--tslpatchdata", str(one), "--namespace-option-index", "1"])
        second = lockstep.patcher_step(["--tslpatchdata", str(two), "--namespace-option-index", "1"])
        other = lockstep.patcher_step(["--tslpatchdata", str(two), "--namespace-option-index", "2"])
        self.assertEqual(first[0], second[0])
        self.assertNotEqual(second[0], other[0])

    def test_id_of_a_mod_folder_is_the_id_of_its_tslpatchdata(self):
        inner = self.root / "payload" / "tslpatchdata"
        write(inner / "changes.ini", "[x]")
        write(self.root / "other" / "changes.ini", "[y]")
        write(self.root / "other" / "tslpatchdata" / "changes.ini", "[z]")
        self.assertEqual(lockstep.patcher_step(["--tslpatchdata", str(inner.parent)])[0],
                         lockstep.patcher_step(["--tslpatchdata", str(inner)])[0])
        self.assertNotEqual(lockstep.patcher_step(["--tslpatchdata", str(self.root / "other")])[0],
                            lockstep.patcher_step(["--tslpatchdata", str(inner)])[0])


if __name__ == "__main__":
    unittest.main()
