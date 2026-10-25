---
name: qa-loop
description: "Build-verify loop with an adversary agent: spec, build, gather evidence per claim, adversary tries to break it, fix, repeat (max 3 rounds). Use on 'qa loop', 'redo until done', 'still broken', when the qa-loop hook says QA LOOP REQUIRED, and before calling done any UI, render, PDF, reply, deploy or data load."
argument-hint: "[what to loop, or empty = the current task]"
---

# QA loop

The person says "qa loop" when the first result was not checked the way they would check it. The proof is
what they would look at (a screen, a page, a record), checked by someone who did not build it.

State and rules live in the engine; replace `$QA` with the path to this repo's `qa_loop` parent:

    QA="python -m qa_loop"        # run from the repo root, or set PYTHONPATH to it

State goes to `$QA_LOOP_HOME` (default `~/.qa-loop/runs/<id>/`): `spec.json`, `rounds/`, `evidence/`, `status.md`.

## 0. Spec (do not ask questions)

1. `$QA list`: an open loop on this same task means continue it, do not start over.
2. `$QA init <slug> --task "<one line>" --profile <p> --audience "<who judges it>" [--complaint "<their words>"]`.
   When the person reports a defect, `--complaint` is their sentence verbatim. It becomes the hard gate G1,
   proven the way they saw it (same screen, same page).
3. Before building, add what exists in the conversation:
   - every explicit ask is a requirement: `$QA add <id> req "..."` (split on "and" / "also");
   - concrete values: `$QA param <id> viewport=390x844 file=report.pdf`.
4. Each requirement gets at least one gate whose evidence is the same kind as the claim:
   `$QA add <id> gate "..." --evidence "<kind>" --req R2 [--hard]`.
   A visual claim needs a screenshot; a data claim needs a query; a text claim needs the rendered text; "works"
   needs the real entry point run. Profile gates that do not apply: `$QA drop <id> G4 "why"`.
5. Tests are the commands that produce the evidence: `$QA add <id> test "..."`.

## 1. Build or fix

The main thread does the work. Writes to shared systems and sends never run inside the adversary.

## 2. Evidence

Run every test and save the output into `<id>/evidence/` (PNG per viewport, query output as `.txt`, test logs).
Look at each file yourself before the adversary does. An exit code of 0, a file that exists, or "rows returned"
prove nothing about content.

## 3. Adversary round

Start a fresh agent every round: never the one that built it, never the previous round's agent. Use the agent in
`spec.adversary`. It gets only the spec path, the evidence folder, the deliverable and the rules that apply. It does
not get the builder's reasoning. Brief (also produced by `qa_loop.runner.adversary_brief`):

> You are the adversary for QA loop `<id>`. Read `<spec.json>`. Assume it is broken until the evidence proves
> otherwise. For every gate: open the evidence yourself, run read-only checks, take your own captures when you can.
> PASS only with the evidence path, URL or command that proves it; FAIL with the concrete reason. A gate whose
> evidence you did not open is a FAIL. Report anything wrong outside the gates as a fix. Do not edit files, deploy
> or send. Reply with only this JSON:
> `{"adversary":"<you>","gates":{"G1":{"verdict":"PASS|FAIL","evidence":"<path|url|command>","why":"..."}},"fixes":["..."]}`

Save the JSON to `<id>/rounds/next.json`, then `$QA record <id> <that file>`. The engine turns a PASS without valid
evidence into FAIL, stops at the round cap, and stops early when the same gates fail for the same reason twice.

## 4. Loop

FAIL: apply the fixes, produce new evidence, start a new adversary, record. Three rounds at most. If the engine says
STOPPED, do not push a fourth round; report the open gates.

## 5. Close and report

- `$QA close <id> PASS` (only after a PASS round), or `STOPPED|BLOCKED "<reason>"`.
- First line of the reply is the verdict: "QA loop PASS in 2 rounds, 6/6 gates" or "STOPPED: G3 still fails: <why>".
  Add the gate table only if something is open, with the evidence path per gate.

## Choosing the profile

| Task | Profile | Adversary |
|---|---|---|
| Page, app, dashboard, artifact | ui | picky user, screenshots at desktop and phone width |
| Video, render, image | render | reviewer with a frame-by-frame rubric |
| Guide, PDF, report, deck | doc | the named reader |
| Reply or email for a person | message | the recipient |
| Script, hook, skill, server, pipeline | code | hostile reviewer |
| Bulk load, migration | data | auditor |
| Deploy, release | deploy | release reviewer, reads back from the target |
| "Why does X happen" | diagnosis | devil's advocate |
| Anything else | other | the most demanding user |

Profiles are in `qa_loop/profiles.json`. Edit them, or point `QA_LOOP_PROFILES` at your own file.
