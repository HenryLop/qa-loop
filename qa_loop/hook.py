"""Claude Code hook for the loop. Register it on UserPromptSubmit and Stop. Plain Python, no tokens.

UserPromptSubmit
  - the user asks for a loop ("qa loop", "redo it until it works", "still broken") -> context says
    QA LOOP REQUIRED and names the profile it guessed;
  - a build or fix verb on a task whose result leaves the session or is hard to undo (a document, a
    render, a message, a deploy, a data load) -> a quiet suggestion that never blocks.
Stop
  - the user asked for a loop and none ran this turn -> block once;
  - a loop touched this turn is still OPEN -> block once per loop, naming the failing gates.
It never blocks a turn that ends waiting on the user, never blocks twice for the same reason, and
stays silent inside subagents. Kill switch: QA_LOOP_HOOKS_OFF=1.
"""
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from qa_loop import engine, transcript  # noqa: E402

EXPLICIT = re.compile(
    r"\bqa[\s-]?loop|\bloop (it|this) until|re-?do (it )?until|until (it'?s |it is )?(done|right|ready|perfect)"
    r"|\b(still|again)\b.{0,40}\b(broke|broken|wrong|not working|failing)"
    r"|\b(is|are|it'?s) (now |still )?broken\b|not up to (the )?standard|this is not working", re.I)
BUILD = re.compile(r"\b(make|build|create|fix|redo|re-?do|render|implement|deploy|update|change|write|generate|prepare|set ?up|repair|refactor|add)\b", re.I)
KINDS = [  # first match wins, most specific first
    ("deploy", re.compile(r"\b(deploy|release|to prod(uction)?|to staging|migration)\b", re.I)),
    ("render", re.compile(r"\b(video|render|animation|frames?|image|clip)\b", re.I)),
    ("doc", re.compile(r"\b(guide|pdf|manual|report|deck|slides|document|docx|one-?pager)\b", re.I)),
    ("code", re.compile(r"\b(hook|script|skill|agent|plugin|pipeline|bot|cli|server|test)\b", re.I)),
    ("ui", re.compile(r"\b(ui|ux|panel|sidebar|dashboard|page|html|screen|app|site|artifact)\b", re.I)),
    ("message", re.compile(r"\b(reply|respond|response|email|mail|message|chat) (to|for)\b|\bdraft\b", re.I)),
    ("data", re.compile(r"\b(bulk|import|data load|records|backfill)\b", re.I)),
    ("diagnosis", re.compile(r"\b(why|diagnos\w+|root cause)\b", re.I)),
]
SUGGEST = {"deploy", "render", "doc", "message", "data"}
TRIVIAL = re.compile(r"^\s*(what|which|how many|show me|open|list|read|check)\b", re.I)
META = re.compile(r"\bqa[\s-]?loop\b.{0,80}\b(hook|noise|noisy|annoying|filter|too (much|many|often)|spam|turn (it )?off|disable|tune)\b"
                  r"|\b(hook|noise|annoying|filter|disable|turn off)\b.{0,80}\bqa[\s-]?loop\b", re.I)
ORDER = re.compile(r"\bqa[\s-]?loop(ed)?\s*(\b(this|it|that|now|please|again|on)\b|[.!,;]|$)", re.I)
SKIPPED = re.compile(r"qa-?loop skipped", re.I)
WAITING = re.compile(r"\b(2fa|verification code|your (yes|ok|approval)|need you to|waiting (on|for) you|log ?in|sign in|paste)\b[^.\n]{0,80}(\?|\.?\s*$)", re.I | re.M)
LOOP_ID = re.compile(r"\b(\d{8}-\d{4,6}(?:-[a-z0-9-]{1,40}))\b")


def classify(prompt):
    """-> (level, profile, reason); level is 'required', 'expected' or None."""
    p = (prompt or "").strip()
    if not p or p.startswith("<") or p.startswith("This session is being continued"):
        return None, None, ""
    m = EXPLICIT.search(p)
    if (p.startswith("/") and not m) or (META.search(p) and not ORDER.search(p)):
        return None, None, ""
    profile = next((n for n, rx in KINDS if rx.search(p)), "other")
    if m:
        return "required", profile, 'user said "%s"' % m.group(0).strip()
    if profile in SUGGEST and BUILD.search(p) and not TRIVIAL.search(p):
        return "expected", profile, "%s task with a build or fix verb" % profile
    return None, None, ""


def _session_file(sid):
    return engine.root() / "_sessions" / ("%s.json" % re.sub(r"[^A-Za-z0-9_-]", "", sid or "x"))


def read_session(sid):
    try:
        return json.loads(_session_file(sid).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def write_session(sid, data):
    f = _session_file(sid)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(data), encoding="utf-8")


def ran_cli(events):
    return any(k == "use" and "qa_loop" in ((x.get("command") or "") if isinstance(x, dict) else "") for k, _, x in events)


def loops_touched(events):
    """Loop ids named in qa_loop commands, or printed by an `init` of this turn."""
    ids, after_init = [], False
    for kind, _, x in events:
        if kind == "use":
            cmd = (x.get("command") or "") if isinstance(x, dict) else ""
            after_init = "qa_loop" in cmd and bool(re.search(r"qa_loop\S*\s+init\b", cmd))
            if "qa_loop" in cmd and not re.search(r"qa_loop\S*\s+list\b", cmd):
                ids += LOOP_ID.findall(cmd)
        elif kind == "result" and after_init:
            ids += LOOP_ID.findall(x[:600])
            after_init = False
    out = []
    for i in ids:
        if i not in out and (engine.root() / i / "spec.json").exists():
            out.append(i)
    return out


def on_prompt(payload):
    level, profile, why = classify(payload.get("prompt") or "")
    sid = payload.get("session_id", "")
    write_session(sid, {"level": level, "profile": profile, "blocked": []})
    if not level:
        return None
    if level == "required":
        msg = ("QA LOOP REQUIRED (%s; profile guess: %s). Run the qa-loop skill now. If a loop on this task is already "
               "open (python -m qa_loop list), continue it. What the user says is broken becomes the hard gate G1 "
               "(--complaint), proven the way the user saw it." % (why, profile))
    else:
        msg = ("QA loop suggested (%s). Run the qa-loop skill only if the result goes to someone else or is hard to "
               "undo; otherwise verify once yourself." % why)
    return {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": msg}}


def on_stop(payload):
    if payload.get("stop_hook_active"):
        return None
    sid = payload.get("session_id", "")
    ses = read_session(sid)
    entries = transcript.read(payload.get("transcript_path", ""))
    if not entries:
        return None
    ev = list(transcript.events(transcript.current_turn(entries)))
    texts = [x for k, _, x in ev if k == "text"]
    if WAITING.search((texts[-1] if texts else "")[-400:]):
        return None
    done = set(ses.get("blocked") or [])
    reasons = []
    if (ses.get("level") == "required" and not ran_cli(ev) and not SKIPPED.search(" ".join(texts))
            and "start" not in done):
        reasons.append(("start", "The user asked for a QA loop (profile %s) but none ran this turn. Run the qa-loop skill "
                                 "(python -m qa_loop init ...), or if it is really trivial say \"qa-loop skipped: <why>\"."
                        % ses.get("profile")))
    for lid in loops_touched(ev):
        try:
            s = engine.load(lid)
        except engine.LoopError:
            continue
        key = "open:" + lid
        if s["state"] == "OPEN" and key not in done:
            rs = s["rounds"]
            what = "no adversary round yet" if not rs else "round %d FAIL (%s)" % (
                len(rs), ", ".join(k for k, v in rs[-1]["gates"].items() if v["verdict"] == "FAIL"))
            reasons.append((key, "QA loop %s is still OPEN: %s, %d/%d rounds. Fix it and run the next adversary round, or "
                                 "close it with `python -m qa_loop close %s STOPPED \"<reason>\"` and tell the user which "
                                 "gates are open." % (lid, what, len(rs), s["max_rounds"], lid)))
    if not reasons:
        write_session(sid, ses)
        return None
    ses["blocked"] = sorted(done | {k for k, _ in reasons})
    write_session(sid, ses)
    return {"decision": "block", "reason": "\n".join(r for _, r in reasons)}


def main():
    if os.environ.get("QA_LOOP_HOOKS_OFF"):
        return
    try:
        payload = json.loads(sys.stdin.buffer.read().decode("utf-8", errors="replace") or "{}")
        if payload.get("agent_id"):
            return
        name = payload.get("hook_event_name")
        out = on_prompt(payload) if name == "UserPromptSubmit" else on_stop(payload) if name == "Stop" else None
        if out:
            sys.stdout.write(json.dumps(out))
    except Exception as exc:  # a broken hook must never block the session
        sys.stderr.write("qa_loop.hook: %s\n" % exc)


if __name__ == "__main__":
    main()
