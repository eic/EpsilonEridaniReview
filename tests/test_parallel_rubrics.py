#!/usr/bin/env python3
"""The parallel rubric loop (review.run_rubrics) keeps the sequential loop's guarantees.

After the lead rubric, which runs alone, the rubrics of a round run up to `--jobs` at a time. These
scenarios drive review.main() end to end with the reviewer subprocess stubbed (no spend, no
network) and pin what must not change: the lead runs alone, concurrency stays at most `--jobs`, a
block stops new launches, the daily budget is reserved for every rubric in flight, and a provider
outage aborts the round only after the rubrics in flight have folded their spend into the ledger.

Run: python3 tests/test_parallel_rubrics.py   (exit 0 = pass, 1 = fail)
"""
import contextlib
import io
import json
import pathlib
import re
import sys
import tempfile
import threading
import time

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "runner"))
import review  # noqa: E402

HEAD = "b" * 40
COST = 0.01
RUBRICS = ["correctness", "reuse", "scope", "attribution", "api-design", "generality"]

# Per scenario: rubric -> (verdict, seconds); a verdict of "quota" fails like an exhausted plan.
_PLAN = {}
_lock = threading.Lock()
_live = {"now": 0, "max": 0, "order": []}


def _fake_runner(prompt, cwd, model, env):
    marker = re.search(r"EPSILONERIDANI-VERDICT-\w+", prompt).group(0)
    rubric = re.search(r"ANGLE:(\S+)", prompt).group(1)
    verdict, secs = _PLAN.get(rubric, ("approve", 0.05))
    with _lock:
        _live["now"] += 1
        _live["max"] = max(_live["max"], _live["now"])
        _live["order"].append(rubric)
    try:
        time.sleep(secs)
    finally:
        with _lock:
            _live["now"] -= 1
    if verdict == "quota":
        return {"returncode": 1, "is_error": True, "cost_usd": 0.0,
                "text": "You've hit your session limit · resets 9:30pm (UTC)"}
    vo = {"verdict": verdict, "summary": f"{rubric} says {verdict}",
          "findings": ([{"file": f"code/{rubric}.lean", "what": "x"}] if verdict != "approve" else [])}
    return {"returncode": 0, "text": f"analysis\n{marker}\n{json.dumps(vo)}", "cost_usd": COST,
            "usage": {"input_tokens": 100, "cached_input_tokens": 0, "output_tokens": 50}}


def _install_stubs():
    review.run_claude = _fake_runner
    review.reviewer_env = lambda provider, keys, subscription=False: ({}, None)
    review.cleanup_rev_home = lambda home: None
    review.sweep_rev_homes = lambda *a, **k: None


def _workspace():
    d = pathlib.Path(tempfile.mkdtemp(prefix="epsiloneridani-parallel-"))
    rd = d / "rubrics"; rd.mkdir()
    (rd / "_common.md").write_text("common rubric preamble\n")
    for r in RUBRICS:
        (rd / f"{r}.md").write_text(f"ANGLE:{r}\nreview angle for {r}\n")
    (d / "diff.txt").write_text("diff --git a/code/x.lean b/code/x.lean\n+x\n")
    store = d / "store"; store.mkdir()
    return d, rd, store


def _run(plan, *, jobs, mode="commit", daily_budget=5.0, max_call_cost=1.0):
    _PLAN.clear(); _PLAN.update(plan)
    _live.update(now=0, max=0, order=[])
    d, rd, store = _workspace()
    argv = ["review", "--pr", "1", "--repo", "Owner/Repo", "--rubrics", ",".join(RUBRICS),
            "--rubrics-dir", str(rd), "--tool-cwd", str(d / "code"), "--diff-file", str(d / "diff.txt"),
            "--store", str(store), "--head-sha", HEAD, "--mode", mode, "--auth", "subscription",
            "--no-post", "--providers", "claude", "--jobs", str(jobs),
            "--daily-budget", str(daily_budget), "--max-call-cost", str(max_call_cost)]
    old, code = sys.argv, None
    sys.argv = argv
    try:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            review.main()
    except SystemExit as e:
        code = e.code
    finally:
        sys.argv = old
    led = json.loads((store / "ledger.json").read_text())
    return led, store, code


fails = 0


def check(name, got, want):
    global fails
    ok = got == want
    print(f"  [{'OK ' if ok else 'FAIL'}] {name}" + ("" if ok else f"  got={got!r} want={want!r}"))
    fails += not ok


def main():
    _install_stubs()
    today = review.today()

    print("A. the lead runs alone, then at most --jobs at a time")
    plan = {r: ("approve", 0.2) for r in RUBRICS}
    t = time.monotonic()
    led, _, code = _run(plan, jobs=3)
    wall = time.monotonic() - t
    rnd = led["prs"]["1"]["rounds"][0]
    check("no abort", code, None)
    check("all rubrics ran", sorted(rnd["ran"]), sorted(RUBRICS))
    check("the lead ran first", rnd["ran"][0], "correctness")
    check("concurrency reached --jobs", _live["max"], 3)
    check("spend = 6*COST", round(led["days"][today], 6), round(6 * COST, 6))
    check("faster than sequential", wall < 6 * 0.2, True)

    print("B. --jobs 1 is the sequential loop")
    led, _, _ = _run({r: ("approve", 0.02) for r in RUBRICS}, jobs=1)
    check("one at a time", _live["max"], 1)
    check("queue order kept", led["prs"]["1"]["rounds"][0]["ran"], RUBRICS)

    print("C. a block from the lead halts before the fan-out")
    led, _, _ = _run({"correctness": ("block", 0.01)}, jobs=5)
    rnd = led["prs"]["1"]["rounds"][0]
    check("only the lead ran", rnd["ran"], ["correctness"])
    check("halted at the lead", rnd["halted_at"], "correctness")

    print("D. a block in the fan-out stops new launches; the rubric in flight still folds")
    led, _, _ = _run({"reuse": ("block", 0.01), "scope": ("approve", 0.3)}, jobs=2)
    rnd = led["prs"]["1"]["rounds"][0]
    check("lead, the blocker and the one in flight ran", rnd["ran"], ["correctness", "reuse", "scope"])
    check("halted at reuse", rnd["halted_at"], "reuse")
    check("nothing launched after the block", "attribution" in _live["order"], False)
    check("spend = 3*COST", round(led["days"][today], 6), round(3 * COST, 6))

    print("E. the daily budget is reserved for every rubric in flight")
    led, _, _ = _run({r: ("approve", 0.05) for r in RUBRICS}, jobs=3,
                     daily_budget=2.5 * COST, max_call_cost=COST)
    rnd = led["prs"]["1"]["rounds"][0]
    check("lead plus one fit", rnd["ran"], ["correctness", "reuse"])
    check("never two in flight past the reservation", _live["max"], 1)
    check("spend = 2*COST", round(led["days"][today], 6), round(2 * COST, 6))

    print("F. a provider outage aborts only after the rubrics in flight fold")
    led, store, code = _run({"reuse": ("quota", 0.01), "scope": ("approve", 0.3)}, jobs=2)
    check("abort status", code, review.PROVIDER_DOWN_EXIT)
    check("nothing launched after the outage", "attribution" in _live["order"], False)
    check("the in-flight rubric's spend reached the ledger",
          round(led["days"][today], 6), round(2 * COST, 6))
    check("the in-flight rubric's record was written",
          (store / "reviews/1/1/scope.json").exists(), True)
    check("no scoreboard rendered", (store / "reviews/1/1/scoreboard.md").exists(), False)

    print(f"\n{'PASS' if not fails else 'FAIL'}: {fails} failure(s)")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
