"""One-command demo of the build-verify loop. No model, no network, nothing written outside a temp folder.

    python demo.py

Part 1: a builder ships slugify(); the adversary tries inputs the builder never tried; fix; PASS.
Part 2: an adversary that says PASS without evidence is not believed.
Part 3: a builder that ignores the fixes is stopped, not looped forever.
Part 4: the "done needs evidence" Stop hook, fed a replayed transcript.
"""
import json
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.abspath(__file__))
TMP = tempfile.mkdtemp(prefix="qa-loop-demo-")
os.environ["QA_LOOP_HOME"] = os.path.join(TMP, "runs")
sys.path.insert(0, ROOT)

from qa_loop import engine, runner, stubs  # noqa: E402


def head(text):
    print("\n== " + text)


def show_events(event, n, payload):
    if event == "build":
        print("  round %d  builder ships a version and attaches its own evidence" % n)
    elif event == "record":
        print("  round %d  adversary verdict: %s (%d/%d gates PASS) -> loop %s" % (
            n, payload["verdict"], payload["passed"], payload["total"], payload["state"]))
        for w in payload["warnings"]:
            print("            ! " + w)


def gate_table(spec):
    print("  %-4s %-62s %s" % ("gate", "check", " ".join("r%d" % r["n"] for r in spec["rounds"])))
    for g in spec["gates"]:
        marks = " ".join("%-2s" % ("ok" if r["gates"][g["id"]]["verdict"] == "PASS" else "NO") for r in spec["rounds"])
        print("  %-4s %-62s %s" % (g["id"], g["text"][:62], marks))
    last = spec["rounds"][-1]
    for gid, v in last["gates"].items():
        if v["verdict"] == "FAIL":
            print("  last failure %s: %s" % (gid, v["why"][:100]))
    print("  final state: %s (%s)" % (spec["state"], spec["note"]))


def loop(title, builder, adversary):
    head(title)
    spec = runner.run_loop("slugify(text) for URL paths", builder, adversary,
                           workdir=os.path.join(TMP, "work-" + str(len(os.listdir(TMP)))),
                           profile="code", slug="slugify", audience="a maintainer who will merge it",
                           on_event=show_events)
    gate_table(spec)
    return spec


def part4():
    head("Part 4: done needs evidence (Stop hook on a replayed transcript)")
    def entry(kind, content):
        return {"type": kind, "message": {"content": content}}
    def use(cmd):
        return entry("assistant", [{"type": "tool_use", "name": "Bash", "input": {"command": cmd}}])
    base = [entry("user", "ship the fix"),
            use("kubectl apply -f deploy/app.yaml"),
            entry("assistant", [{"type": "text", "text": "Done, the fix is live."}])]
    with_check = base[:2] + [use("kubectl rollout status deploy/app"),
                             entry("assistant", [{"type": "text", "text": "Done, the fix is live."}])]
    for label, entries in (("write, then 'done', no read-back", base), ("write, read-back, then 'done'", with_check)):
        path = os.path.join(TMP, "t.jsonl")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(json.dumps(e) for e in entries))
        p = subprocess.run([sys.executable, os.path.join(ROOT, "qa_loop", "gate.py")], capture_output=True, text=True,
                           input=json.dumps({"session_id": "demo-" + label[:5], "transcript_path": path}))
        out = json.loads(p.stdout) if p.stdout.strip() else None
        print("  %-36s -> %s" % (label, ("BLOCKED: " + out["reason"][:60] + "...") if out else "allowed"))


def main():
    print("QA loop demo. State goes to %s" % TMP)
    loop("Part 1: build, adversary, fix, PASS", stubs.ScriptedBuilder(), stubs.ProbingAdversary())
    loop("Part 2: an adversary that says PASS with no evidence", stubs.ScriptedBuilder(), stubs.LazyAdversary())
    loop("Part 3: a builder that ignores every fix", stubs.ScriptedBuilder(stubborn=True), stubs.ProbingAdversary())
    part4()
    print("\nStatus pages and evidence files are in %s" % os.environ["QA_LOOP_HOME"])


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    main()
