import json
import os
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
TMP = tempfile.mkdtemp(prefix="qa-loop-hook-test-")
os.environ["QA_LOOP_HOME"] = os.path.join(TMP, "runs")

from qa_loop import engine, gate, hook  # noqa: E402


def user(text):
    return {"type": "user", "message": {"content": text}}


def say(text):
    return {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}


def bash(cmd):
    return {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Bash", "input": {"command": cmd}}]}}


def agent(kind):
    return {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Agent", "input": {"subagent_type": kind}}]}}


def result(text="ok"):
    return {"type": "user", "message": {"content": [{"type": "tool_result", "content": text}]}}


def write_transcript(entries):
    path = os.path.join(TMP, "t-%d.jsonl" % len(os.listdir(TMP)))
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(json.dumps(e) for e in entries))
    return path


class ClassifyTests(unittest.TestCase):
    def test_explicit_requests(self):
        for p in ("qa loop this", "redo it until it works", "the page is still broken", "this is not working"):
            self.assertEqual(hook.classify(p)[0], "required", p)

    def test_quiet_suggestion_only_for_outbound_work(self):
        self.assertEqual(hook.classify("write the quarterly report as a pdf")[:2], ("expected", "doc"))
        self.assertEqual(hook.classify("deploy the new version to staging")[:2], ("expected", "deploy"))
        self.assertIsNone(hook.classify("fix the typo in the script")[0])

    def test_questions_and_talk_about_the_loop_do_nothing(self):
        for p in ("what does the report say", "the qa loop hook is too noisy", "/help", "",
                  "This session is being continued from a previous conversation"):
            self.assertIsNone(hook.classify(p)[0], p)

    def test_order_wins_over_talk(self):
        self.assertEqual(hook.classify("the qa loop hook is noisy, qa loop it now")[0], "required")


class HookTests(unittest.TestCase):
    def test_prompt_injects_context(self):
        out = hook.on_prompt({"session_id": "s1", "prompt": "qa loop the dashboard page"})
        ctx = out["hookSpecificOutput"]["additionalContext"]
        self.assertIn("QA LOOP REQUIRED", ctx)
        self.assertIn("profile guess: ui", ctx)

    def test_stop_blocks_when_required_loop_never_ran(self):
        hook.on_prompt({"session_id": "s2", "prompt": "qa loop this"})
        t = write_transcript([user("qa loop this"), say("All good now.")])
        out = hook.on_stop({"session_id": "s2", "transcript_path": t})
        self.assertEqual(out["decision"], "block")
        self.assertIsNone(hook.on_stop({"session_id": "s2", "transcript_path": t}))  # once per reason

    def test_skip_phrase_passes(self):
        hook.on_prompt({"session_id": "s3", "prompt": "qa loop this"})
        t = write_transcript([user("qa loop this"), say("qa-loop skipped: one-line typo")])
        self.assertIsNone(hook.on_stop({"session_id": "s3", "transcript_path": t}))

    def test_stop_blocks_while_loop_is_open(self):
        sid = engine.init("hk", "task", "code")["id"]
        hook.on_prompt({"session_id": "s4", "prompt": "qa loop this"})
        t = write_transcript([user("qa loop this"), bash("python -m qa_loop show " + sid), result(), say("Looks fine.")])
        out = hook.on_stop({"session_id": "s4", "transcript_path": t})
        self.assertIn(sid, out["reason"])
        self.assertIn("still OPEN", out["reason"])

    def test_closed_loop_does_not_block(self):
        sid = engine.init("hk2", "task", "other")["id"]
        engine.close(sid, "BLOCKED", "waiting on access")
        hook.on_prompt({"session_id": "s5", "prompt": "qa loop this"})
        t = write_transcript([user("qa loop this"), bash("python -m qa_loop close %s BLOCKED x" % sid), result(), say("Closed.")])
        self.assertIsNone(hook.on_stop({"session_id": "s5", "transcript_path": t}))

    def test_turn_waiting_on_the_user_is_never_blocked(self):
        hook.on_prompt({"session_id": "s6", "prompt": "qa loop this"})
        t = write_transcript([user("qa loop this"), say("I need you to log in first. Can you paste the code?")])
        self.assertIsNone(hook.on_stop({"session_id": "s6", "transcript_path": t}))

    def test_subagent_and_garbage_input(self):
        for stdin in ('{"agent_id": "x", "hook_event_name": "Stop"}', "not json", ""):
            p = subprocess.run([sys.executable, os.path.join(ROOT, "qa_loop", "hook.py")], input=stdin,
                               capture_output=True, text=True, env=dict(os.environ))
            self.assertEqual(p.stdout, "")
            self.assertEqual(p.returncode, 0)


class GateTests(unittest.TestCase):
    CFG = None

    def decide(self, entries, sid, mode="enforce"):
        return gate.decide({"session_id": sid, "transcript_path": write_transcript(entries)}, mode)

    def test_done_after_write_without_evidence_is_blocked(self):
        out = self.decide([user("ship it"), bash("kubectl apply -f app.yaml"), result(), say("Done, it is live.")], "g1")
        self.assertEqual(out["decision"], "block")

    def test_read_back_after_write_passes(self):
        out = self.decide([user("ship it"), bash("kubectl apply -f app.yaml"), result(),
                           bash("kubectl rollout status deploy/app"), result(), say("Done, it is live.")], "g2")
        self.assertIsNone(out)

    def test_check_before_write_does_not_count(self):
        out = self.decide([user("ship it"), bash("pytest -q"), result(), bash("git push origin main"), result(),
                           say("Pushed and done.")], "g3")
        self.assertEqual(out["decision"], "block")

    def test_unverified_or_not_done_replies_pass(self):
        for text in ("Pushed, but unverified: I could not run the tests.", "It is not done yet, the deploy is pending."):
            out = self.decide([user("ship it"), bash("git push origin main"), result(), say(text)], "g4")
            self.assertIsNone(out, text)

    def test_no_write_no_gate(self):
        self.assertIsNone(self.decide([user("fix it"), say("Done.")], "g5"))

    def test_dry_run_is_not_a_write(self):
        self.assertIsNone(self.decide([user("x"), bash("terraform apply --dry-run"), result(), say("Done.")], "g6"))

    def test_production_needs_a_reviewer(self):
        e = [user("ship"), bash("kubectl apply -f prod/app.yaml"), result(), bash("kubectl get pods"), result(), say("Deployed.")]
        self.assertEqual(self.decide(e, "g7")["decision"], "block")
        e = e[:-1] + [agent("code-reviewer"), result(), say("Deployed.")]
        self.assertIsNone(self.decide(e, "g8"))

    def test_after_max_blocks_the_turn_ends_with_a_notice(self):
        e = [user("ship"), bash("git push"), result(), say("Done.")]
        self.assertEqual(self.decide(e, "g9")["decision"], "block")
        self.assertEqual(self.decide(e, "g9")["decision"], "block")
        third = self.decide(e, "g9")
        self.assertNotIn("decision", third)
        self.assertIn("Unverified", third["systemMessage"])

    def test_new_user_turn_resets_the_count(self):
        e = [user("ship"), bash("git push"), result(), say("Done.")]
        self.decide(e, "g10")
        self.decide(e, "g10")
        e2 = e + [user("again"), bash("git push"), result(), say("Done.")]
        self.assertEqual(self.decide(e2, "g10")["decision"], "block")

    def test_shadow_mode_never_blocks(self):
        e = [user("ship"), bash("git push"), result(), say("Done.")]
        self.assertIsNone(self.decide(e, "g11", mode="shadow"))

    def test_garbage_stdin_exits_clean(self):
        p = subprocess.run([sys.executable, os.path.join(ROOT, "qa_loop", "gate.py")], input="{{", capture_output=True, text=True)
        self.assertEqual((p.stdout, p.returncode), ("", 0))


if __name__ == "__main__":
    unittest.main()
