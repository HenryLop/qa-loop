"""Loop state: spec, rounds, verdicts and a Markdown status page. Plain Python, no model calls.

A loop lives in one folder: <root>/<id>/spec.json, rounds/rN.json, evidence/, status.md.
The root is $QA_LOOP_HOME or ~/.qa-loop/runs.

Rules the engine enforces, so no agent has to remember them:
  - a PASS needs evidence: an existing local path, a URL, a query:/cmd: prefix or a quoted observation;
    a PASS without it is recorded as FAIL;
  - a gate the adversary did not review is a FAIL;
  - the loop stops at max_rounds, and stops early when the same gates fail for the same reason twice;
  - a loop cannot be closed PASS unless the last round was a PASS.
"""
import json
import os
import re
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
FINAL_STATES = ("PASS", "STOPPED", "BLOCKED")


class LoopError(Exception):
    pass


def profiles():
    path = os.environ.get("QA_LOOP_PROFILES") or str(HERE / "profiles.json")
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def root():
    return Path(os.environ.get("QA_LOOP_HOME") or Path.home() / ".qa-loop" / "runs")


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def loop_dir(loop_id):
    d = root() / loop_id
    if not (d / "spec.json").exists():
        raise LoopError("no loop %s (try: python -m qa_loop list)" % loop_id)
    return d


def load(loop_id):
    return json.loads((loop_dir(loop_id) / "spec.json").read_text(encoding="utf-8"))


def save(spec):
    d = root() / spec["id"]
    (d / "spec.json").write_text(json.dumps(spec, indent=1), encoding="utf-8")
    (d / "status.md").write_text(render(spec), encoding="utf-8")


def init(slug, task, profile="other", audience="", complaint="", max_rounds=None):
    prof = profiles()
    if profile not in prof:
        raise LoopError("unknown profile %r (%s)" % (profile, ", ".join(prof)))
    slug = re.sub(r"[^a-z0-9-]+", "-", slug.lower()).strip("-")[:40] or "loop"
    base = "%s-%s" % (datetime.now().strftime("%Y%m%d-%H%M%S"), slug)
    loop_id, n = base, 1
    while (root() / loop_id).exists():
        n += 1
        loop_id = "%s-%d" % (base, n)
    d = root() / loop_id
    (d / "rounds").mkdir(parents=True)
    (d / "evidence").mkdir()
    p = prof[profile]
    spec = {"id": loop_id, "task": task, "profile": profile, "audience": audience, "created": _now(),
            "max_rounds": max_rounds or p.get("max_rounds", 3), "adversary": p["adversary"],
            "params": dict(p.get("params", {})), "reqs": [], "gates": [], "tests": [], "rounds": [],
            "state": "OPEN", "note": ""}
    if complaint:  # the user's own words become requirement R1 and a hard gate
        spec["reqs"].append({"id": "R1", "text": complaint})
        spec["gates"].append({"id": "G1", "text": "Fixed as the user would see it: " + complaint,
                              "evidence": p["complaint_evidence"], "req": "R1", "hard": True})
    for g in p["gates"]:
        spec["gates"].append({"id": "G%d" % (len(spec["gates"]) + 1), "text": g["text"],
                              "evidence": g["evidence"], "req": "", "hard": g.get("hard", False)})
    for t in p.get("tests", []):
        spec["tests"].append({"id": "T%d" % (len(spec["tests"]) + 1), "text": t})
    save(spec)
    return spec


def add(loop_id, kind, text, evidence="", req="", hard=False):
    spec = load(loop_id)
    key = {"req": "reqs", "gate": "gates", "test": "tests"}[kind]
    prefix = {"req": "R", "gate": "G", "test": "T"}[kind]
    used = [x["id"] for x in spec[key] + (spec.get("dropped", []) if kind == "gate" else [])]
    nid = "%s%d" % (prefix, max([int(i[1:]) for i in used if i[1:].isdigit()] or [0]) + 1)  # ids are never reused
    item = {"id": nid, "text": text}
    if kind == "gate":
        if not evidence:
            raise LoopError("a gate needs evidence: say what kind of proof passes it")
        item.update(evidence=evidence, req=req, hard=hard)
    spec[key].append(item)
    save(spec)
    return nid


def drop(loop_id, gate_id, why):
    spec = load(loop_id)
    g = next((g for g in spec["gates"] if g["id"] == gate_id), None)
    if not g or not why:
        raise LoopError("drop needs an existing gate and a reason (%s)" % gate_id)
    if g.get("hard") and g.get("req") == "R1":
        raise LoopError("G1 is the user's complaint; it cannot be dropped")
    spec["gates"].remove(g)
    spec.setdefault("dropped", []).append({"id": gate_id, "text": g["text"], "why": why})
    save(spec)


def param(loop_id, pairs):
    spec = load(loop_id)
    for p in pairs:
        k, _, v = p.partition("=")
        spec["params"][k.strip()] = v.strip()
    save(spec)


def evidence_ok(ev):
    ev = (ev or "").strip()
    if not ev:
        return False
    if re.match(r"^(https?://|select\b|query:|cmd:)", ev, re.I):
        return True
    if re.match(r"^[A-Za-z]:[\\/]|^[/~]", ev):
        return Path(ev).expanduser().exists()
    return len(ev) > 15  # a quoted observation counts as evidence; a single word does not


def record(loop_id, round_data):
    """round_data = {"gates": {"G1": {"verdict": "PASS|FAIL", "evidence": ..., "why": ...}}, "fixes": [...], "adversary": ...}"""
    spec = load(loop_id)
    if spec["state"] in FINAL_STATES:
        raise LoopError("%s is closed (%s)" % (loop_id, spec["state"]))
    if not isinstance(round_data, dict):
        raise LoopError("round data must be a JSON object")
    n = len(spec["rounds"]) + 1
    results, warnings = {}, []
    for g in spec["gates"]:
        v = (round_data.get("gates") or {}).get(g["id"])
        if not isinstance(v, dict):
            results[g["id"]] = {"verdict": "FAIL", "evidence": "", "why": "not reviewed by the adversary"}
            warnings.append("%s not reviewed" % g["id"])
            continue
        verdict = str(v.get("verdict", "")).upper()
        why = v.get("why", "")
        if verdict == "PASS" and not evidence_ok(v.get("evidence")):
            verdict, why = "FAIL", "PASS without valid evidence (%s)" % (v.get("evidence") or "none")
            warnings.append("%s PASS rejected: no evidence" % g["id"])
        results[g["id"]] = {"verdict": "PASS" if verdict == "PASS" else "FAIL",
                            "evidence": v.get("evidence", ""), "why": why}
    failing = [k for k, v in results.items() if v["verdict"] == "FAIL"]
    rnd = {"n": n, "at": _now(), "adversary": round_data.get("adversary") or spec["adversary"]["agent"],
           "gates": results, "fixes": round_data.get("fixes", []), "verdict": "FAIL" if failing else "PASS"}
    spec["rounds"].append(rnd)
    (loop_dir(loop_id) / "rounds" / ("r%d.json" % n)).write_text(json.dumps(rnd, indent=1), encoding="utf-8")
    if not failing:
        spec["state"], spec["note"] = "PASS", "all %d gates PASS in round %d" % (len(results), n)
    elif n >= spec["max_rounds"]:
        spec["state"], spec["note"] = "STOPPED", "max %d rounds; still failing: %s" % (spec["max_rounds"], ", ".join(failing))
    elif n >= 2:
        prev = spec["rounds"][-2]["gates"]
        same = [k for k in failing if prev.get(k, {}).get("verdict") == "FAIL" and prev[k].get("why") == results[k]["why"]]
        if same and len(same) == len(failing):
            spec["state"], spec["note"] = "STOPPED", "stagnation: %s failed twice for the same reason" % ", ".join(same)
    save(spec)
    return {"round": n, "verdict": rnd["verdict"], "passed": len(results) - len(failing), "total": len(results),
            "state": spec["state"], "warnings": warnings}


def close(loop_id, state, note=""):
    state = state.upper()
    if state not in FINAL_STATES:
        raise LoopError("close needs one of: " + ", ".join(FINAL_STATES))
    spec = load(loop_id)
    last = spec["rounds"][-1]["verdict"] if spec["rounds"] else None
    if state == "PASS" and last != "PASS":
        raise LoopError("cannot close PASS without a PASS round from the adversary")
    spec["state"], spec["note"] = state, note or spec["note"]
    save(spec)
    return spec


def list_open(limit=15):
    out = []
    for f in sorted(root().glob("*/spec.json"), reverse=True)[:limit]:
        s = json.loads(f.read_text(encoding="utf-8"))
        if s["state"] == "OPEN":
            out.append(s)
    return out


def _cell(t, n=70):
    t = " ".join(str(t or "").split()).replace("|", "/")
    return t if len(t) <= n else t[:n - 3] + "..."


def render(spec):
    rs = spec["rounds"]
    passing = sum(1 for v in (rs[-1]["gates"].values() if rs else []) if v["verdict"] == "PASS")
    L = ["# QA loop: %s" % spec["task"], "",
         "**%s** | round %d/%d | gates %d/%d PASS%s" % (spec["state"], len(rs), spec["max_rounds"], passing,
                                                        len(spec["gates"]), (" | " + spec["note"]) if spec["note"] else ""),
         "", "| Parameter | Value |", "|---|---|",
         "| Profile | %s |" % spec["profile"], "| Audience | %s |" % _cell(spec["audience"] or "-"),
         "| Adversary | %s: %s |" % (spec["adversary"]["agent"], _cell(spec["adversary"]["role"], 60)),
         "| Started | %s |" % spec["created"]]
    L += ["| %s | %s |" % (_cell(k, 30), _cell(v)) for k, v in spec["params"].items()]
    if spec["reqs"]:
        L += ["", "## Requirements", ""] + ["- **%s** %s" % (r["id"], r["text"]) for r in spec["reqs"]]
    L += ["", "## Gates", "", "| Gate | Check | Evidence needed | " + " | ".join("#%d" % r["n"] for r in rs) + " |",
          "|---|---|---|" + "---|" * len(rs)]
    for g in spec["gates"]:
        cells = []
        for r in rs:
            v = r["gates"].get(g["id"])
            cells.append(("PASS" if v["verdict"] == "PASS" else "FAIL") if v else "-")
        L.append("| %s%s | %s | %s | %s |" % (g["id"], " *" if g.get("hard") else "", _cell(g["text"]),
                                              _cell(g["evidence"], 40), " | ".join(cells)))
    if spec.get("dropped"):
        L += [""] + ["_dropped %s: %s (%s)_" % (d["id"], _cell(d["text"], 50), _cell(d["why"], 80)) for d in spec["dropped"]]
    if spec["tests"]:
        L += ["", "## Tests", ""] + ["- **%s** %s" % (t["id"], t["text"]) for t in spec["tests"]]
    if rs:
        u = rs[-1]
        bad = [(k, v) for k, v in u["gates"].items() if v["verdict"] == "FAIL"]
        L += ["", "## Round %d: %s (%s, %s)" % (u["n"], u["verdict"], u["at"], u["adversary"]), ""]
        L += ["- FAIL **%s** %s" % (k, _cell(v["why"], 160)) for k, v in bad] or ["- all gates PASS"]
        L += ["- fix: " + _cell(f, 160) for f in u["fixes"]]
        L += ["", "| Gate | Evidence |", "|---|---|"] + ["| %s | %s |" % (k, _cell(v["evidence"], 90))
                                                         for k, v in u["gates"].items() if v["evidence"]]
    L += ["", "_updated %s_" % _now(), ""]
    return "\n".join(L)
