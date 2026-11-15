#!/usr/bin/env python3
"""A claude review call appends one line to the worker's spend journal, when the worker names one.

The worker paces its Claude rounds on the account's usage, and the usage endpoint answers most polls
with 429. Every claude call's stream-json carries `rate_limit_event`s with the same utilization, so the
worker keeps a per-account journal of them (EpsilonEridaniWorker `spend.jsonl`) and its agent runs
already write to it. Review rounds spend on the same account; this pins that they report too.

Pinned here:

  1. Unset EPSILONERIDANI_CLAUDE_SPEND_JOURNAL: nothing is written (CI, a local run).
  2. Set: one line per call, with cost, tokens, and the latest reading per window, merged from the
     headline window and `unifiedWindows`.
  3. Readings are dated to the call's START, never later (the conservative side for the pacer).
  4. Malformed values are dropped, a call with no result still gets a line, and an unwritable path
     never fails the review.

Exit 0 = all assertions hold; 1 = a mismatch.
"""

import json
import os
import pathlib
import sys
import tempfile
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "runner"))
import reviewers  # noqa: E402

fails = 0


def check(name, cond):
    global fails
    fails += not cond
    print(f"[{'OK ' if cond else 'BAD'}] {name}")


RESULT = {"type": "result", "subtype": "success", "is_error": False,
          "result": "EPSILONERIDANI-VERDICT-abc\n{\"verdict\": \"approve\"}",
          "total_cost_usd": 1.25,
          "usage": {"input_tokens": 7, "output_tokens": 300, "cache_read_input_tokens": 9000,
                    "cache_creation_input_tokens": 50},
          "session_id": "s-1"}


def rate_event(kind, used, resets, windows=None):
    info = {"status": "allowed", "rateLimitType": kind, "utilization": used, "resetsAt": resets}
    if windows is not None:
        info["unifiedWindows"] = windows
    return {"type": "rate_limit_event", "rate_limit_info": info}


def run(events, rc=0):
    stream = "\n".join(json.dumps(e) if isinstance(e, dict) else e for e in events)

    class R:
        returncode, stdout, stderr = rc, stream, ""

    orig = reviewers.sh
    reviewers.sh = lambda *a, **k: R
    try:
        return reviewers.run_claude("prompt", ".", "claude-opus-5", {})
    finally:
        reviewers.sh = orig


def lines(path):
    if not os.path.exists(path):
        return []
    return [json.loads(x) for x in open(path).read().splitlines()]


def main():
    os.environ.pop(reviewers.SPEND_JOURNAL_ENV, None)
    with tempfile.TemporaryDirectory() as tmp:
        journal = os.path.join(tmp, "acct", "spend.jsonl")

        # --- 1) unset: nothing written ---
        run([rate_event("five_hour", 0.4, 2000000000), RESULT])
        check("unset: no journal written", not os.path.exists(journal))

        # --- 2/3) set: one line, merged windows, dated to the call's start ---
        os.environ[reviewers.SPEND_JOURNAL_ENV] = journal
        os.environ["EPSILONERIDANI_WORKER_ID"] = "worker99"
        before = time.time()
        out = run([
            rate_event("five_hour", 0.30, 2000000000,
                       {"seven_day": {"status": "allowed", "utilization": 0.10, "resetsAt": 2000500000}}),
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "hm"}]}},
            rate_event("five_hour", 0.42, 2000000000,
                       {"seven_day": {"utilization": 0.11, "resetsAt": 2000500000}}),
            RESULT,
        ])
        after = time.time()
        got = lines(journal)
        check("set: exactly one line", len(got) == 1)
        e = got[0] if got else {}
        rl = e.get("rate_limits") or {}
        check("label names the reviewer model", e.get("label") == "review-claude:claude-opus-5")
        check("worker id recorded", e.get("worker") == "worker99")
        check("rc recorded", e.get("rc") == 0)
        check("cost recorded", (e.get("spend") or {}).get("cost_usd") == 1.25)
        check("tokens mapped", (e.get("spend") or {}).get("tokens")
              == {"input": 7, "output": 300, "cache_read": 9000, "cache_write": 50})
        check("latest five_hour wins", rl.get("five_hour", {}).get("utilization") == 0.42)
        check("seven_day from unifiedWindows", rl.get("seven_day", {}).get("utilization") == 0.11)
        check("unifiedWindows merge keeps the earlier status", rl.get("seven_day", {}).get("status") is None
              or rl["seven_day"]["status"] == "allowed")
        seen = {w.get("seen") for w in rl.values()}
        check("every reading dated to the call's start",
              len(seen) == 1 and before - 0.01 <= next(iter(seen)) <= after + 0.01 and next(iter(seen)) == e.get("started"))
        check("review result unaffected", out.get("cost_usd") == 1.25 and out.get("text", "").startswith("EPSILON"))

        # --- 4) malformed values dropped; no result still journaled; unwritable path harmless ---
        os.remove(journal)
        run([{"type": "rate_limit_event", "rate_limit_info": {
                "rateLimitType": "five_hour", "utilization": "lots", "resetsAt": float("nan")}},
             "not json", {"type": "rate_limit_event", "rate_limit_info": "nope"}], rc=1)
        run([{"type": "rate_limit_event", "rate_limit_info": {
                "rateLimitType": "seven_day", "utilization": 10 ** 1000, "resetsAt": 10 ** 1000}}],
            rc=1)
        got = lines(journal)
        check("an int too large for a float is dropped, not raised", len(got) == 2
              and got[1].get("rate_limits") is None)
        got = got[:1]
        check("no-result call still journaled", len(got) == 1)
        check("no-result call: spend null", got and got[0].get("spend") is None)
        check("malformed readings dropped", got and got[0].get("rate_limits") is None)

        blocker = os.path.join(tmp, "file")
        open(blocker, "w").write("x")
        os.environ[reviewers.SPEND_JOURNAL_ENV] = os.path.join(blocker, "spend.jsonl")
        try:
            out = run([rate_event("five_hour", 0.5, 2000000000), RESULT])
            check("unwritable journal does not fail the review", out.get("cost_usd") == 1.25)
        except Exception as exc:  # noqa: BLE001
            check(f"unwritable journal does not fail the review ({exc!r})", False)
        os.environ.pop(reviewers.SPEND_JOURNAL_ENV, None)
        os.environ.pop("EPSILONERIDANI_WORKER_ID", None)

    print("\nall OK" if not fails else f"\n{fails} FAILED")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
