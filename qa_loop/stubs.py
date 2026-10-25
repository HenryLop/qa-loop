"""Deterministic stand-ins for a builder and an adversary. No model, no network.

The task is a tiny one on purpose: write slugify(text). The scripted builder ships a version that
passes its own happy-path check but breaks on awkward input. The stub adversary does what a real
one should: it runs the deliverable on inputs the builder never tried and writes its own evidence.
"""
import subprocess
import sys
from pathlib import Path

from .runner import Adversary, Builder

V1 = '''def slugify(text):
    return text.lower().replace(" ", "-")
'''

V2 = '''import re
import unicodedata


def slugify(text):
    if text is None:
        return ""
    plain = unicodedata.normalize("NFKD", str(text)).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", plain.lower()).strip("-")
'''

TESTS = '''import unittest
from slugify import slugify


class T(unittest.TestCase):
    def test_basic(self):
        self.assertEqual(slugify("Hello World"), "hello-world")


if __name__ == "__main__":
    unittest.main()
'''

NORMAL = [("Hello World", "hello-world"), ("one two three", "one-two-three"), ("Plain", "plain")]
EDGE = [("Crème  brûlée!!", "creme-brulee"), ("  --a--b  ", "a-b"), ("", ""), ("100% Sure", "100-sure")]


def _load(workdir):
    ns = {}
    src = (Path(workdir) / "slugify.py").read_text(encoding="utf-8")
    exec(compile(src, "slugify.py", "exec"), ns)
    return ns["slugify"]


class ScriptedBuilder(Builder):
    """Ships V1 first; applies the fix (V2) on the next round if it was asked for one.

    stubborn=True never applies a fix, to show the stop conditions."""

    def __init__(self, stubborn=False):
        self.stubborn = stubborn

    def build(self, ctx):
        ctx.workdir.mkdir(parents=True, exist_ok=True)
        (ctx.workdir / "test_slugify.py").write_text(TESTS, encoding="utf-8")
        code = V2 if (ctx.fixes and not self.stubborn) else V1
        (ctx.workdir / "slugify.py").write_text(code, encoding="utf-8")

    def gather_evidence(self, ctx):
        fn = _load(ctx.workdir)
        lines = ["%r -> %r" % (a, fn(a)) for a, _ in NORMAL]
        (ctx.evidence_dir / ("builder-r%d-happy-path.txt" % ctx.round)).write_text("\n".join(lines) + "\n", encoding="utf-8")


class ProbingAdversary(Adversary):
    """Maps the four 'code' profile gates to real probes. Each probe writes its own evidence file."""

    name = "stub-hostile-reviewer"

    def _write(self, ctx, gate, text):
        path = ctx.evidence_dir / ("adversary-r%d-%s.txt" % (ctx.round, gate))
        path.write_text(text, encoding="utf-8")
        return str(path)

    def _first_gate(self, ctx, word):
        for g in ctx.spec["gates"]:
            if word.lower() in g["text"].lower():
                return g["id"]
        return None

    def review(self, ctx):
        fn = _load(ctx.workdir)
        gates, fixes = {}, []

        gid = self._first_gate(ctx, "end to end")
        bad = [(a, fn(a), want) for a, want in NORMAL if fn(a) != want]
        ev = self._write(ctx, gid, "\n".join("%r -> %r (want %r)" % (a, fn(a), w) for a, w in NORMAL))
        gates[gid] = {"verdict": "FAIL" if bad else "PASS", "evidence": ev,
                      "why": "ordinary input is wrong: %r" % (bad[0],) if bad else "3 ordinary inputs correct"}

        gid = self._first_gate(ctx, "tests exist")
        p = subprocess.run([sys.executable, "-m", "unittest", "-q"], cwd=str(ctx.workdir), capture_output=True, text=True)
        ev = self._write(ctx, gid, p.stdout + p.stderr)
        gates[gid] = {"verdict": "PASS" if p.returncode == 0 else "FAIL", "evidence": ev,
                      "why": "test run exit code %d" % p.returncode}

        gid = self._first_gate(ctx, "edge cases")
        wrong = []
        for a, want in EDGE:
            try:
                got = fn(a)
            except Exception as exc:  # noqa: BLE001 - the adversary reports any failure
                got = "raised " + type(exc).__name__
            if got != want:
                wrong.append((a, got, want))
        ev = self._write(ctx, gid, "\n".join("%r want %r" % (a, w) for a, w in EDGE) + "\nwrong: %d\n" % len(wrong))
        if wrong:
            gates[gid] = {"verdict": "FAIL", "evidence": ev,
                          "why": "%d of %d edge inputs wrong, first: %r gives %r, expected %r" % ((len(wrong), len(EDGE)) + wrong[0])}
            fixes.append("handle accents, repeated separators and trailing punctuation")
        else:
            gates[gid] = {"verdict": "PASS", "evidence": ev, "why": "all %d edge inputs correct" % len(EDGE)}

        gid = self._first_gate(ctx, "fails safe")
        crashes = []
        for bad_input in (None, 12345):
            try:
                fn(bad_input)
            except Exception as exc:  # noqa: BLE001
                crashes.append("%r raised %s" % (bad_input, type(exc).__name__))
        ev = self._write(ctx, gid, "\n".join(crashes) or "no exception on None or 12345")
        if crashes:
            gates[gid] = {"verdict": "FAIL", "evidence": ev, "why": "crashes on odd input: " + "; ".join(crashes)}
            fixes.append("never raise on None or non-string input")
        else:
            gates[gid] = {"verdict": "PASS", "evidence": ev, "why": "None and an int do not raise"}

        return {"adversary": self.name, "gates": gates, "fixes": fixes}


class LazyAdversary(Adversary):
    """Says PASS everywhere with a one-word 'evidence'. The engine must not believe it."""

    def review(self, ctx):
        return {"adversary": "stub-lazy-reviewer",
                "gates": {g["id"]: {"verdict": "PASS", "evidence": "fine", "why": "looks good"} for g in ctx.spec["gates"]},
                "fixes": []}
