"""Plain Python runner: builder -> evidence -> adversary -> record, up to max_rounds.

Builder and Adversary are small interfaces. The stubs in stubs.py are deterministic and are
what the demo and the tests use. CommandBuilder and CommandAdversary wrap any shell command,
so a real model can be plugged in without this package ever calling one.
"""
import json
import subprocess
from pathlib import Path

from . import engine


class Context:
    """What an adapter gets for one round."""

    def __init__(self, spec, round_no, fixes, workdir):
        self.spec = spec
        self.loop_id = spec["id"]
        self.round = round_no
        self.fixes = list(fixes)
        self.workdir = Path(workdir)
        self.evidence_dir = engine.loop_dir(spec["id"]) / "evidence"


class Builder:
    def build(self, ctx):
        """Make or fix the deliverable inside ctx.workdir. ctx.fixes holds what the last round asked for."""
        raise NotImplementedError

    def gather_evidence(self, ctx):
        """Write one evidence file per claim into ctx.evidence_dir."""
        raise NotImplementedError


class Adversary:
    def review(self, ctx):
        """Return {"adversary": name, "gates": {gate_id: {"verdict", "evidence", "why"}}, "fixes": [...]}.

        Must not edit the deliverable. Must not read the builder's reasoning, only the spec,
        the evidence folder and the deliverable."""
        raise NotImplementedError


def adversary_brief(ctx):
    """The text a fresh model agent gets in place of the builder's context."""
    return (
        "You are the adversary for QA loop %s, round %d.\n"
        "Read %s. Assume the work is broken until the evidence proves otherwise.\n"
        "For every gate: open the evidence yourself, run read-only checks, make your own captures when you can.\n"
        "PASS only with the evidence path, URL or command that proves it. FAIL with the concrete reason.\n"
        "A gate whose evidence you did not open is a FAIL. Also report anything wrong outside the gates as a fix.\n"
        "Do not edit files, deploy or send anything.\n"
        "Evidence folder: %s\nDeliverable folder: %s\n"
        "Reply with only this JSON: "
        '{"adversary":"<you>","gates":{"G1":{"verdict":"PASS|FAIL","evidence":"<path|url|command>","why":"..."}},"fixes":["..."]}\n'
        % (ctx.loop_id, ctx.round, engine.loop_dir(ctx.loop_id) / "spec.json", ctx.evidence_dir, ctx.workdir))


def _run(cmd, payload, cwd):
    p = subprocess.run(cmd, input=payload, capture_output=True, text=True, shell=True, cwd=str(cwd), timeout=600)
    return p.returncode, p.stdout, p.stderr


def _payload(ctx):
    return json.dumps({"spec": ctx.spec, "round": ctx.round, "fixes": ctx.fixes,
                       "evidence_dir": str(ctx.evidence_dir), "workdir": str(ctx.workdir),
                       "brief": adversary_brief(ctx)})


class CommandBuilder(Builder):
    """build_cmd and evidence_cmd get the round as JSON on stdin and run inside workdir."""

    def __init__(self, build_cmd, evidence_cmd):
        self.build_cmd, self.evidence_cmd = build_cmd, evidence_cmd

    def build(self, ctx):
        code, _, err = _run(self.build_cmd, _payload(ctx), ctx.workdir)
        if code:
            raise RuntimeError("build command failed: " + err.strip()[:300])

    def gather_evidence(self, ctx):
        code, _, err = _run(self.evidence_cmd, _payload(ctx), ctx.workdir)
        if code:
            raise RuntimeError("evidence command failed: " + err.strip()[:300])


class CommandAdversary(Adversary):
    """cmd gets the round as JSON on stdin and must print the round JSON on stdout.

    This is where a real model goes, for example a script that sends ctx['brief'] to a fresh
    agent and prints its JSON answer. This package never makes that call itself."""

    def __init__(self, cmd, name="command-adversary"):
        self.cmd, self.name = cmd, name

    def review(self, ctx):
        code, out, err = _run(self.cmd, _payload(ctx), ctx.workdir)
        if code:
            raise RuntimeError("adversary command failed: " + err.strip()[:300])
        start, end = out.find("{"), out.rfind("}")
        try:
            data = json.loads(out[start:end + 1])
        except ValueError:
            data = {"gates": {}, "fixes": ["adversary output was not JSON"]}
        data.setdefault("adversary", self.name)
        return data


def run_loop(task, builder, adversary, workdir, profile="other", slug="run", audience="",
             complaint="", extra_gates=(), max_rounds=None, on_event=None):
    """Run the loop to a final state and return the final spec."""
    say = on_event or (lambda *a: None)
    spec = engine.init(slug, task, profile, audience, complaint, max_rounds)
    for text, evidence in extra_gates:
        engine.add(spec["id"], "gate", text, evidence=evidence)
    Path(workdir).mkdir(parents=True, exist_ok=True)
    fixes = []
    while True:
        spec = engine.load(spec["id"])
        n = len(spec["rounds"]) + 1
        ctx = Context(spec, n, fixes, workdir)
        say("build", n, ctx)
        builder.build(ctx)
        builder.gather_evidence(ctx)
        say("review", n, ctx)
        result = adversary.review(ctx)
        outcome = engine.record(spec["id"], result)
        say("record", n, outcome)
        if outcome["state"] != "OPEN":
            break
        fixes = result.get("fixes", [])
    return engine.load(spec["id"])
