"""Stop hook: a turn that wrote to the outside world cannot say "done" without evidence read back.

Per turn it looks at three things, all from the transcript:
  1. the last write: a shell command that changes something outside the session (deploy, push,
     apply, a SQL write, a POST). Patterns live in gate.json; edit them for your stack.
  2. a done claim in the final reply ("done", "deployed", "works").
  3. evidence after that last write: a test run, a read-back, a GET, a screenshot, or a reviewer agent.
A write to production also needs a reviewer agent.

No evidence -> the turn is blocked with a short reason. After max_blocks blocks (default 2) the turn
is allowed to end with a visible "unverified" notice instead of looping. A reply that says it is
unverified, or not done, passes. Modes: --mode enforce (default) or --mode shadow (log only).
Config: QA_GATE_CONFIG=<path to your own gate.json>. Kill switch: QA_LOOP_HOOKS_OFF=1.
"""
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from qa_loop import engine, transcript  # noqa: E402

SHELL = ("Bash", "PowerShell")


def config():
    path = os.environ.get("QA_GATE_CONFIG") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "gate.json")
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _any(patterns, text):
    return any(re.search(p, text, re.I | re.S) for p in patterns)


def analyse(entries, cfg=None):
    """-> dict(claim, wrote, evidence, reviewed, prod, problem) for the current turn."""
    cfg = cfg or config()
    turn = transcript.current_turn(entries)
    events = list(transcript.events(turn))
    last_write, last_write_cmd = -1, ""
    for i, (kind, name, x) in enumerate(events):
        if kind == "use" and name in SHELL:
            cmd = (x.get("command") or "") if isinstance(x, dict) else ""
            if _any(cfg["write_commands"], cmd) and not re.search(cfg["check_only"], cmd, re.I):
                last_write, last_write_cmd = i, cmd
    texts = [x for k, _, x in events if k == "text"]
    final = texts[-1] if texts else ""
    claim = None
    if final and not re.search(cfg["not_done"], final, re.I):
        m = re.search(cfg["done_claim"], final, re.I)
        claim = m.group(0) if m else None
    evidence = reviewed = False
    for kind, name, x in events[last_write + 1:] if last_write >= 0 else []:
        if kind != "use":
            continue
        cmd = (x.get("command") or "") if isinstance(x, dict) else ""
        if name in SHELL and _any(cfg["evidence_commands"], cmd) and not _any(cfg["write_commands"], cmd):
            evidence = True
        elif re.search(cfg["evidence_tools"], name, re.I):
            evidence = True
        elif name in ("Agent", "Task"):
            kind_of_agent = str((x or {}).get("subagent_type", "")) if isinstance(x, dict) else ""
            if re.search(cfg["reviewer_agents"], kind_of_agent, re.I):
                reviewed = evidence = True
    prod = bool(last_write_cmd and re.search(cfg["production"], last_write_cmd, re.I))
    problem = None
    if last_write >= 0 and claim:
        if not evidence:
            problem = "evidence"
        elif prod and not reviewed:
            problem = "reviewer"
    return {"claim": claim, "wrote": last_write >= 0, "evidence": evidence, "reviewed": reviewed,
            "prod": prod, "problem": problem}


def _state_file(sid):
    return engine.root() / "_gate" / ("%s.json" % re.sub(r"[^A-Za-z0-9_-]", "", sid or "x"))


def _log(entry):
    f = engine.root() / "_gate" / "log.jsonl"
    f.parent.mkdir(parents=True, exist_ok=True)
    with open(f, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry) + "\n")


MESSAGES = {
    "evidence": ("This turn changed something outside the session and the reply says it is done, but nothing read the result "
                 "back after the last change. Run a check now (a test, a read-back, a GET, a screenshot), or say plainly "
                 "that the change is unverified."),
    "reviewer": ("The last change went to production. A read-back is not enough there: have a reviewer agent look at the "
                 "result before calling it done, or say plainly that it is unverified."),
}


def decide(payload, mode="enforce", cfg=None):
    cfg = cfg or config()
    entries = transcript.read(payload.get("transcript_path", ""))
    if not entries:
        return None
    res = analyse(entries, cfg)
    sid = payload.get("session_id", "")
    turn_key = sum(1 for e in entries if transcript.is_real_user(e))
    state_file = _state_file(sid)
    try:
        state = json.loads(state_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = {}
    if state.get("turn") != turn_key:
        state = {"turn": turn_key, "blocks": 0}
    blocked = False
    out = None
    if res["problem"]:
        if mode == "shadow":
            pass
        elif state["blocks"] < cfg.get("max_blocks", 2):
            state["blocks"] += 1
            blocked = True
            out = {"decision": "block", "reason": MESSAGES[res["problem"]]}
        else:
            out = {"systemMessage": "Unverified: the change was not read back after it was made."}
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text(json.dumps(state), encoding="utf-8")
    _log({"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "session": sid[:8], "mode": mode, "wrote": res["wrote"],
          "claim": bool(res["claim"]), "evidence": res["evidence"], "problem": res["problem"], "blocked": blocked})
    return out


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    mode = argv[argv.index("--mode") + 1] if "--mode" in argv and argv.index("--mode") + 1 < len(argv) else "enforce"
    if os.environ.get("QA_LOOP_HOOKS_OFF"):
        return
    try:
        payload = json.loads(sys.stdin.buffer.read().decode("utf-8", errors="replace") or "{}")
        if payload.get("agent_id"):
            return
        out = decide(payload, mode)
        if out:
            sys.stdout.write(json.dumps(out))
    except Exception as exc:  # a broken hook must never block the session
        sys.stderr.write("qa_loop.gate: %s\n" % exc)


if __name__ == "__main__":
    main()
