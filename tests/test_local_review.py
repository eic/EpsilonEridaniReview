#!/usr/bin/env python3
"""`epsiloneridani-review --local DIR` reviews an unpushed commit as a pre-push check.

A fixing agent can run the rubrics it was asked to clear against its own commit before pushing,
instead of pushing, waiting for a full review round, and finding out then. These tests drive
cli.main() against a throwaway git repository, with GitHub and the engine stubbed, and pin:

  1. the diff and the staged code are the LOCAL head against its merge base with the PR's base,
     not anything fetched from GitHub;
  2. nothing shared is touched: scratch store, no archive, no sync, no review-in-progress marker,
     no post, and manual mode so every requested rubric runs;
  3. an unpushed commit claims no CI result;
  4. the final line is `LOCAL-REVIEW: PASS (...)` only when every requested rubric is green, and
     names the others otherwise;
  5. --local refuses --post.

Run: python3 tests/test_local_review.py   (exit 0 = pass, 1 = fail)
"""
import contextlib
import io
import json
import pathlib
import subprocess
import sys
import tempfile

RUNNER = pathlib.Path(__file__).resolve().parent.parent / "runner"
sys.path.insert(0, str(RUNNER))
import cli  # noqa: E402

fails = 0


def check(name, ok, detail=""):
    global fails
    print(f"  [{'OK ' if ok else 'FAIL'}] {name}" + ("" if ok else f"  {detail}"))
    fails += not ok


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


def make_repo(root):
    repo = root / "checkout"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "t@example.com")
    git(repo, "config", "user.name", "t")
    (repo / "EpsilonEridani").mkdir()
    (repo / "EpsilonEridani" / "A.lean").write_text("theorem a : True := trivial\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "base")
    base = git(repo, "rev-parse", "HEAD")
    (repo / "EpsilonEridani" / "A.lean").write_text("theorem a : True := trivial\ntheorem b : True := trivial\n")
    git(repo, "commit", "-q", "-am", "the unpushed fix")
    return repo, base, git(repo, "rev-parse", "HEAD")


def drive(argv, states):
    """Run cli.main() with GitHub and the engine stubbed; return (engine_cmd, stdout, work)."""
    seen = {"engine": None, "coordinated": False}
    real_run = cli.run

    def fake_run(cmd, **kw):
        if any(str(c).endswith("review.py") for c in cmd):
            seen["engine"] = cmd
            store = pathlib.Path(cmd[cmd.index("--store") + 1])
            work = pathlib.Path(cmd[cmd.index("--tool-cwd") + 1])
            seen["work"] = work
            seen["diff"] = (work / "diff.txt").read_text()
            seen["code"] = (work / "code" / "EpsilonEridani" / "A.lean").read_text()
            (store / "ledger.json").write_text(json.dumps(
                {"days": {}, "prs": {"7": {"rounds": [{"states": states}]}}}))
            (work / "scoreboard.md").write_text("scoreboard\n")
            return subprocess.CompletedProcess(cmd, 0, "", "")
        if "fetch" in cmd and any("github.com" in str(c) for c in cmd):
            return subprocess.CompletedProcess(cmd, 0, "", "")   # the base is already local here
        if cmd[:2] == ["gh", "api"] and "user" in cmd:
            return subprocess.CompletedProcess(cmd, 0, "tester\n", "")
        return real_run(cmd, **kw)

    def no_coordinate(*a, **k):
        seen["coordinated"] = True
        return ["claude"]

    real_which = cli.shutil.which

    def fake_which(name, *a, **k):
        # The CLI only offers a reviewer whose binary is on PATH; a CI runner has no `claude`, and
        # the engine is stubbed anyway, so report one rather than depend on the machine.
        return f"/usr/bin/{name}" if name == "claude" else real_which(name, *a, **k)

    patches = {"run": fake_run, "pr_ref_oids": lambda repo, pr: ("remote-head-not-used", BASE),
               "gh_json": lambda repo, pr, fields: {"title": "t", "body": "b"},
               "fetch_thread_replies": lambda repo, pr: {}, "coordinate": no_coordinate,
               "need": lambda *a: None}
    saved = {k: getattr(cli, k) for k in patches}
    for k, v in patches.items():
        setattr(cli, k, v)
    cli.shutil.which = fake_which
    old_argv, out, code = sys.argv, io.StringIO(), None
    sys.argv = ["epsiloneridani-review", *argv]
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            cli.main()
    except SystemExit as e:
        code = e.code
    finally:
        sys.argv = old_argv
        for k, v in saved.items():
            setattr(cli, k, v)
        cli.shutil.which = real_which
    return seen, out.getvalue(), code


def main():
    global BASE
    root = pathlib.Path(tempfile.mkdtemp(prefix="epsiloneridani-local-review-"))
    repo, BASE, head = make_repo(root)
    roadmap = root / "roadmap"; roadmap.mkdir()
    common = ["7", "--repo", "owner/repo", "--local", str(repo), "--roadmap-dir", str(roadmap),
              "--repo-dir", str(RUNNER.parent), "--no-upstream", "--reviewer", "claude",
              "--rubrics", "correctness,reuse"]

    print("1-3) local head, nothing shared touched")
    seen, out, code = drive(common, {"correctness": "green", "reuse": "green", "scope": "blocking_request"})
    cmd = seen["engine"] or []
    check("the engine ran", bool(cmd), code)
    check("reviews the local head", cmd[cmd.index("--head-sha") + 1] == head if cmd else False)
    check("diff is the local change", "+theorem b" in seen.get("diff", ""), seen.get("diff", "")[:200])
    check("code staged at the local head", "theorem b" in seen.get("code", ""))
    check("manual mode", cmd[cmd.index("--mode") + 1] == "manual" if cmd else False)
    check("never posts", "--no-post" in cmd and "--post" not in cmd)
    check("no archive", "--archive-dir" not in cmd)
    check("scratch store inside the workspace",
          pathlib.Path(cmd[cmd.index("--store") + 1]).parent == seen.get("work") if cmd else False)
    check("no review-in-progress marker", not seen["coordinated"])
    check("no CI result claimed", cmd[cmd.index("--ci-build") + 1] == "" if cmd else False)
    check("only the requested rubrics", cmd[cmd.index("--rubrics") + 1] == "correctness,reuse" if cmd else False)

    print("4) the verdict line")
    check("PASS when the requested rubrics are green", out.strip().endswith("LOCAL-REVIEW: PASS (correctness, reuse)"),
          out.strip().splitlines()[-1:])
    _, out, _ = drive(common, {"correctness": "green", "reuse": "blocking_request"})
    check("FAIL names the rest", out.strip().endswith("LOCAL-REVIEW: FAIL (reuse: blocking_request)"),
          out.strip().splitlines()[-1:])

    print("5) never posts")
    _, _, code = drive([*common, "--post"], {})
    check("--local --post is refused", code == 1, code)

    print(f"\n{'PASS' if not fails else 'FAIL'}: {fails} failure(s)")
    return 1 if fails else 0


BASE = ""

if __name__ == "__main__":
    sys.exit(main())
