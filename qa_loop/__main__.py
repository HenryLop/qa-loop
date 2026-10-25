"""Command line for the loop state.

    python -m qa_loop init <slug> --task "..." --profile code [--audience "..."] [--complaint "..."]
    python -m qa_loop add <id> req|gate|test "text" [--evidence "kind"] [--req R1] [--hard]
    python -m qa_loop drop <id> G3 "why it does not apply"
    python -m qa_loop param <id> key=value ...
    python -m qa_loop record <id> round.json
    python -m qa_loop close <id> PASS|STOPPED|BLOCKED ["note"]
    python -m qa_loop show <id>
    python -m qa_loop list
    python -m qa_loop profiles
"""
import argparse
import json
import sys

from . import engine


def build_parser():
    ap = argparse.ArgumentParser(prog="python -m qa_loop", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("init")
    p.add_argument("slug")
    p.add_argument("--task", default="")
    p.add_argument("--profile", default="other")
    p.add_argument("--audience", default="")
    p.add_argument("--complaint", default="")
    p.add_argument("--max-rounds", type=int, default=None)
    p = sub.add_parser("add")
    p.add_argument("loop")
    p.add_argument("kind", choices=["req", "gate", "test"])
    p.add_argument("text", nargs="+")
    p.add_argument("--evidence", default="")
    p.add_argument("--req", default="")
    p.add_argument("--hard", action="store_true")
    p = sub.add_parser("drop")
    p.add_argument("loop")
    p.add_argument("gate")
    p.add_argument("why", nargs="+")
    p = sub.add_parser("param")
    p.add_argument("loop")
    p.add_argument("pairs", nargs="+")
    p = sub.add_parser("record")
    p.add_argument("loop")
    p.add_argument("file")
    p = sub.add_parser("close")
    p.add_argument("loop")
    p.add_argument("state")
    p.add_argument("note", nargs="*")
    p = sub.add_parser("show")
    p.add_argument("loop")
    sub.add_parser("list")
    sub.add_parser("profiles")
    return ap


def main(argv=None):
    ap = build_parser()
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 2
    try:
        if a.cmd == "init":
            s = engine.init(a.slug, a.task or a.slug, a.profile, a.audience, a.complaint, a.max_rounds)
            d = engine.loop_dir(s["id"])
            print("%s\n%s\nevidence: %s" % (s["id"], d / "status.md", d / "evidence"))
        elif a.cmd == "add":
            print(engine.add(a.loop, a.kind, " ".join(a.text), a.evidence, a.req, a.hard))
        elif a.cmd == "drop":
            engine.drop(a.loop, a.gate, " ".join(a.why))
            print("dropped " + a.gate)
        elif a.cmd == "param":
            engine.param(a.loop, a.pairs)
        elif a.cmd == "record":
            try:
                with open(a.file, encoding="utf-8-sig") as fh:
                    data = json.load(fh)
            except (OSError, ValueError) as exc:
                raise engine.LoopError("round file unreadable (%s); save only the adversary JSON and record again" % exc)
            r = engine.record(a.loop, data)
            print("round %d: %s (%d/%d gates) -> state %s" % (r["round"], r["verdict"], r["passed"], r["total"], r["state"]))
            for w in r["warnings"]:
                print("  ! " + w)
            print(engine.loop_dir(a.loop) / "status.md")
        elif a.cmd == "close":
            s = engine.close(a.loop, a.state, " ".join(a.note))
            print("%s: %s\n%s" % (s["id"], s["state"], engine.loop_dir(a.loop) / "status.md"))
        elif a.cmd == "show":
            s = engine.load(a.loop)
            print("%s: %s round %d/%d\n%s" % (s["id"], s["state"], len(s["rounds"]), s["max_rounds"],
                                            engine.loop_dir(a.loop) / "status.md"))
        elif a.cmd == "list":
            for s in engine.list_open():
                print("%s  round %d/%d  %s" % (s["id"], len(s["rounds"]), s["max_rounds"], s["task"][:60]))
        elif a.cmd == "profiles":
            for k, v in engine.profiles().items():
                print("%-10s %s" % (k, v["adversary"]["agent"]))
    except engine.LoopError as exc:
        print("qa-loop: %s" % exc, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
