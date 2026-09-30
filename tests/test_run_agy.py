import json
import os
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "runner"))
import reviewers  # noqa: E402
from pricing import PRICES  # noqa: E402

fails = 0

def check(name, cond):
    global fails
    fails += not cond
    print(f"[{'OK ' if cond else 'BAD'}] {name}")

def run(events, ws, rc=0):
    stream = "\n".join(json.dumps(e) if isinstance(e, dict) else e for e in events)

    class R:
        returncode, stdout, stderr = rc, stream, "stderr text"

    orig = reviewers.sh
    reviewers.sh = lambda *a, **k: R
    try:
        return reviewers.run_agy("prompt", ws, "gemini-3.1-pro", {})
    finally:
        reviewers.sh = orig

def test_agy_parsing():
    with tempfile.TemporaryDirectory() as ws:
        # 1. Success event
        ev_success = {
            "event": "result",
            "result": {
                "conversation_id": "sid-123",
                "status": "SUCCESS",
                "response": "EPSILONERIDANI-VERDICT-abc\n{\"verdict\": \"approve\"}",
                "usage": {
                    "input_tokens": 100,
                    "cache_read_tokens": 50,
                    "output_tokens": 20
                }
            }
        }
        
        out = run([ev_success], ws)
        check("cost_estimated is True", out.get("cost_estimated") is True)
        check("session_id is extracted", out.get("session_id") == "sid-123")
        check("text is extracted", "EPSILONERIDANI-VERDICT-abc" in out.get("text", ""))
        check("is_error is not set for success", not out.get("is_error"))
        check("returncode is propagated", out.get("returncode") == 0)

        # 2. Error event
        ev_error = {
            "event": "result",
            "result": {
                "conversation_id": "sid-err",
                "status": "ERROR",
                "error": "some internal error",
                "response": ""
            }
        }
        out_err = run([ev_error], ws, rc=1)
        check("is_error is set", out_err.get("is_error") is True)
        check("error_message is set", out_err.get("error_message") == "some internal error")
        check("raw_stdout is captured on error", "some internal error" in out_err.get("raw_stdout", ""))

        # 3. Tolerant parsing
        out_tol = run(["invalid json", "{ broken json", ev_success], ws)
        check("tolerates invalid json lines", out_tol.get("session_id") == "sid-123")

def main():
    test_agy_parsing()
    print("FAIL" if fails else "PASS")
    return 1 if fails else 0

if __name__ == "__main__":
    sys.exit(main())
