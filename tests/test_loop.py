import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TMP = tempfile.mkdtemp(prefix="qa-loop-test-")
os.environ["QA_LOOP_HOME"] = os.path.join(TMP, "runs")

from qa_loop import __main__ as cli  # noqa: E402
from qa_loop import engine, runner, stubs  # noqa: E402


def passing(loop_id, evidence="the screenshot shows the change on the page"):
    spec = engine.load(loop_id)
    return {"gates": {g["id"]: {"verdict": "PASS", "evidence": evidence, "why": "ok"} for g in spec["gates"]}}


def failing(loop_id, which, why="broken", evidence="output shows the failure clearly"):
    spec = engine.load(loop_id)
    gates = {g["id"]: {"verdict": "PASS", "evidence": "evidence is a long enough quoted observation", "why": "ok"}
             for g in spec["gates"]}
    for w in which:
        gates[w] = {"verdict": "FAIL", "evidence": evidence, "why": why}
    return {"gates": gates, "fixes": ["fix " + ",".join(which)]}


class EngineTests(unittest.TestCase):
    def new(self, **kw):
        return engine.init("t", "task", kw.pop("profile", "code"), **kw)["id"]

    def test_profile_gates_and_complaint(self):
        sid = self.new(complaint="the button does nothing")
        spec = engine.load(sid)
        self.assertEqual(spec["gates"][0]["id"], "G1")
        self.assertTrue(spec["gates"][0]["hard"])
        self.assertEqual(len(spec["gates"]), 5)
        with self.assertRaises(engine.LoopError):
            engine.drop(sid, "G1", "not needed")

    def test_unknown_profile(self):
        with self.assertRaises(engine.LoopError):
            engine.init("t", "task", "nope")

    def test_pass_in_one_round(self):
        sid = self.new()
        r = engine.record(sid, passing(sid))
        self.assertEqual(r["state"], "PASS")
        self.assertEqual(engine.close(sid, "PASS")["state"], "PASS")

    def test_pass_without_evidence_is_fail(self):
        sid = self.new()
        r = engine.record(sid, passing(sid, evidence="ok"))
        self.assertEqual(r["verdict"], "FAIL")
        self.assertEqual(r["passed"], 0)

    def test_pass_with_missing_path_is_fail(self):
        sid = self.new()
        r = engine.record(sid, passing(sid, evidence=os.path.join(TMP, "does-not-exist.png")))
        self.assertEqual(r["verdict"], "FAIL")

    def test_pass_with_existing_path_counts(self):
        sid = self.new()
        f = Path(TMP) / "shot.txt"
        f.write_text("x", encoding="utf-8")
        self.assertEqual(engine.record(sid, passing(sid, evidence=str(f)))["verdict"], "PASS")

    def test_unreviewed_gate_fails(self):
        sid = self.new()
        data = passing(sid)
        del data["gates"]["G2"]
        r = engine.record(sid, data)
        self.assertEqual(r["verdict"], "FAIL")
        self.assertIn("G2 not reviewed", r["warnings"])

    def test_max_rounds_stops(self):
        sid = self.new()
        for i in range(3):
            r = engine.record(sid, failing(sid, ["G2"], why="reason %d" % i))
        self.assertEqual(r["state"], "STOPPED")
        self.assertIn("max 3 rounds", engine.load(sid)["note"])
        with self.assertRaises(engine.LoopError):
            engine.record(sid, passing(sid))

    def test_same_reason_twice_stops_early(self):
        sid = self.new()
        engine.record(sid, failing(sid, ["G2"], why="same"))
        r = engine.record(sid, failing(sid, ["G2"], why="same"))
        self.assertEqual(r["state"], "STOPPED")
        self.assertIn("stagnation", engine.load(sid)["note"])

    def test_progress_keeps_loop_open(self):
        sid = self.new()
        engine.record(sid, failing(sid, ["G2", "G3"], why="a"))
        r = engine.record(sid, failing(sid, ["G3"], why="b"))
        self.assertEqual(r["state"], "OPEN")

    def test_cannot_close_pass_without_pass_round(self):
        sid = self.new()
        engine.record(sid, failing(sid, ["G2"]))
        with self.assertRaises(engine.LoopError):
            engine.close(sid, "PASS")
        self.assertEqual(engine.close(sid, "BLOCKED", "waiting for access")["state"], "BLOCKED")

    def test_gate_ids_never_reused(self):
        sid = self.new()
        engine.drop(sid, "G4", "does not apply here")
        self.assertEqual(engine.add(sid, "gate", "new check", evidence="a log"), "G5")
        with self.assertRaises(engine.LoopError):
            engine.add(sid, "gate", "no proof kind")

    def test_status_page_is_written(self):
        sid = self.new()
        engine.record(sid, failing(sid, ["G3"], why="edge case broke"))
        text = (engine.loop_dir(sid) / "status.md").read_text(encoding="utf-8")
        self.assertIn("round 1/3", text)
        self.assertIn("edge case broke", text)

    def test_list_open(self):
        sid = self.new()
        self.assertIn(sid, [s["id"] for s in engine.list_open(200)])
        engine.record(sid, passing(sid))
        self.assertNotIn(sid, [s["id"] for s in engine.list_open(200)])


class RunnerTests(unittest.TestCase):
    def go(self, builder, adversary):
        return runner.run_loop("slugify", builder, adversary, workdir=tempfile.mkdtemp(dir=TMP), profile="code", slug="s")

    def test_fix_then_pass_in_two_rounds(self):
        spec = self.go(stubs.ScriptedBuilder(), stubs.ProbingAdversary())
        self.assertEqual(spec["state"], "PASS")
        self.assertEqual(len(spec["rounds"]), 2)
        self.assertEqual(sorted(k for k, v in spec["rounds"][0]["gates"].items() if v["verdict"] == "FAIL"), ["G3", "G4"])

    def test_stubborn_builder_is_stopped(self):
        spec = self.go(stubs.ScriptedBuilder(stubborn=True), stubs.ProbingAdversary())
        self.assertEqual(spec["state"], "STOPPED")
        self.assertLessEqual(len(spec["rounds"]), 3)

    def test_lazy_adversary_cannot_pass_anything(self):
        spec = self.go(stubs.ScriptedBuilder(), stubs.LazyAdversary())
        self.assertEqual(spec["state"], "STOPPED")
        self.assertTrue(all(r["verdict"] == "FAIL" for r in spec["rounds"]))

    def test_evidence_files_exist(self):
        spec = self.go(stubs.ScriptedBuilder(), stubs.ProbingAdversary())
        ev = engine.loop_dir(spec["id"]) / "evidence"
        self.assertTrue((ev / "adversary-r2-G3.txt").exists())
        self.assertTrue((ev / "builder-r1-happy-path.txt").exists())

    def test_command_adversary_parses_json(self):
        script = Path(TMP) / "adv.py"
        script.write_text(
            "import json,sys\np=json.load(sys.stdin)\n"
            "print('noise before')\n"
            "print(json.dumps({'gates':{g['id']:{'verdict':'PASS','evidence':'cmd: pytest -q gave 4 passed','why':'ran it'} "
            "for g in p['spec']['gates']}}))\n", encoding="utf-8")
        adv = runner.CommandAdversary('"%s" "%s"' % (sys.executable, script))
        build = Path(TMP) / "b.py"
        build.write_text("import sys\nsys.stdin.read()\n", encoding="utf-8")
        bld = runner.CommandBuilder('"%s" "%s"' % (sys.executable, build), '"%s" "%s"' % (sys.executable, build))
        spec = self.go(bld, adv)
        self.assertEqual(spec["state"], "PASS")
        self.assertEqual(spec["rounds"][0]["adversary"], "command-adversary")

    def test_brief_names_the_rules(self):
        spec = engine.init("b", "t", "code")
        ctx = runner.Context(spec, 1, [], TMP)
        brief = runner.adversary_brief(ctx)
        self.assertIn("Do not edit files", brief)
        self.assertIn("did not open is a FAIL", brief)


class CliTests(unittest.TestCase):
    def test_init_record_close(self):
        import contextlib
        import io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(cli.main(["init", "cli-test", "--task", "x", "--profile", "other"]), 0)
            sid = buf.getvalue().splitlines()[0]
            spec = engine.load(sid)
            rf = Path(TMP) / "round.json"
            rf.write_text(json.dumps(passing(sid)), encoding="utf-8")
            self.assertEqual(cli.main(["record", sid, str(rf)]), 0)
            self.assertEqual(cli.main(["close", sid, "PASS"]), 0)
        self.assertEqual(engine.load(sid)["state"], "PASS")
        self.assertEqual(len(spec["gates"]), 2)

    def test_bad_round_file(self):
        sid = engine.init("cli-bad", "x", "other")["id"]
        bad = Path(TMP) / "bad.json"
        bad.write_text("{not json", encoding="utf-8")
        self.assertEqual(cli.main(["record", sid, str(bad)]), 1)


if __name__ == "__main__":
    unittest.main()
