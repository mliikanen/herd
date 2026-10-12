#!/usr/bin/env python3
"""Judge evaluation runs (docs/evaluation-plan.md, Judging steps 1-4).

For each run in eval/work/runs/ (all those not yet judged, or the ones named), in random order:
1. the gate, rerun in a fresh worker on a clone of the run's result branch in eval/work/results.git;
2. the tamper check, comparing the result with its base and the reference change with its base;
3. the reference tests: the tests the original change added or changed, copied into the result and run, for
   information only;
4. the holistic review by Opus, in another fresh worker that sees only the original proposal and the result squashed
   into one commit, under a random name: it can't tell which setup made it. It is skipped when step 1 or 2 fails.

The judgment goes to eval/results/<run>.json when both the run and the judge used the real Claude Code CLI, and to
eval/work/judgments/<run>.json otherwise.

Usage: eval/judge.py [RUN ...] [--real --max-cost USD] [--fake-gate] [--force] [--keep]
"""

import argparse
import json
import os
import random
import re
import secrets
import subprocess
import sys
import time
import xml.etree.ElementTree as ElementTree
from pathlib import Path

from run import EVAL, GATE_TIMEOUT, OPUS, TEST_PATH, WORK, Budget, Worker, commit_tree, fill, sh, verdict, write_config

RESULTS = EVAL / "results"
STUB_JUDGMENTS = WORK / "judgments"
JUDGING = WORK / "judging"

# The tamper check. Files that configure the checks: any change to one is tampering unless the reference changed it.
CHECK_CONFIG = re.compile(r"(^|/)(gradle\.properties|\.editorconfig|lint\.xml|[^/]*baseline[^/]*)$|^config/|"
                          r"^gradle/wrapper/")
# Build scripts change for ordinary reasons (a new dependency), so only their lines that touch a check count.
BUILD_SCRIPT = re.compile(r"(\.gradle\.kts|\.gradle|libs\.versions\.toml)$")
CHECK_LINE = re.compile(r"baseline|abortOnError|warningsAsErrors|allWarnings|ignoreFailures|checkReleaseBuilds|disable|"
                        r"isEnabled|enabled\s*=|onlyIf|exclude|suppress|nowarn|lint|detekt|ktlint|codeQuality|"
                        r"warning\.mode|withType<Test>|named<Test>|filter\s*\{", re.IGNORECASE)
# Skipped tests and suppressed checks, in added source lines.
SKIP_MARKER = re.compile(r"@(?:file:)?(?:kotlin\.test\.|org\.junit\.(?:jupiter\.api\.)?)?(?:Ignore|Disabled)\b|"
                         r"\bassume(?:True|False|That)?\s*\(|@(?:file:)?Suppress(?:Warnings|Lint)?\s*\([^)]*\)|"
                         r"ktlint-disable[\w:,-]*|noinspection\s+\w+|detekt:disable[\w:,-]*")
SOURCE = re.compile(r"\.(kt|kts|java)$")
MAESTRO = re.compile(r"^maestro/")
KOTLIN_ROOT = re.compile(r"^.*?/src/\w+/kotlin/")  # what's left is the package directory

TESTS = "./gradlew :shared:allTests --continue --console=plain"
COMPILE_ERROR = re.compile(r"^e: (?:file://)?/work/repo/(\S+?\.kt):\d+", re.MULTILINE)
REFERENCE_ATTEMPTS = 6


# --- step 2: the tamper check (on the host, from the repositories) ---

def name_status(git_dir, old, new):
    """The files changed between two commits, outside openspec/, as (status letter, old path, new path)."""
    out = sh("git", "-C", str(git_dir), "diff", "--name-status", "-M", old, new, "--", ".", ":(exclude)openspec").stdout
    for line in out.splitlines():
        status, *paths = line.split("\t")
        yield status[0], paths[0], paths[-1]


def changed_lines(git_dir, old, new):
    """The added and removed lines between two commits, outside openspec/: {path: [(sign, line stripped)]}."""
    out = sh("git", "-C", str(git_dir), "diff", "--no-color", "-U0", "-M", old, new, "--", ".",
             ":(exclude)openspec").stdout
    lines, path, in_hunk = {}, None, False
    for line in out.splitlines():
        if line.startswith("diff --git "):
            path, in_hunk = None, False
        elif not in_hunk and line.startswith("--- a/"):
            path = line[6:]
        elif not in_hunk and line.startswith("+++ b/"):
            path = line[6:]
        elif line.startswith("@@"):
            in_hunk = True
        elif in_hunk and path and line[:1] in "+-" and line:
            lines.setdefault(path, []).append((line[0], line[1:].strip()))
    return lines


def test_counts(git_dir, rev):
    """The number of @Test functions per package directory in the test source sets."""
    out = sh("git", "-C", str(git_dir), "grep", "-c", "-w", "-F", "@Test", rev, "--", "*.kt", check=False).stdout
    counts = {}
    for line in out.splitlines():
        path, count = line.split(":", 1)[1].rsplit(":", 1)
        if TEST_PATH.search("/" + path):
            package = KOTLIN_ROOT.sub("", os.path.dirname(path) + "/")
            counts[package] = counts.get(package, 0) + int(count)
    return counts


def marker(text):
    return re.sub(r"\s+", "", text)


def tamper(entry, branch, repo):
    """Compare the result with its base, and allow what the reference change also did: deleted tests or flows,
    fewer tests in a package, changed check configuration, skip or suppression markers, and build-script lines that
    touch a check."""
    results = WORK / "results.git"
    root = sh("git", "-C", str(results), "rev-list", "--max-parents=0", branch).stdout.split()[0]
    base, reference = entry["base"], entry["reference"]
    flags = []
    ours, theirs = list(name_status(results, root, branch)), list(name_status(repo, base, reference))
    their_paths = {path for _, old, new in theirs for path in (old, new)}
    their_deleted = {old for status, old, _ in theirs if status == "D"}

    for status, old, new in ours:
        if status == "D" and (TEST_PATH.search("/" + old) or MAESTRO.match(old)) and old not in their_deleted:
            flags.append({"kind": "test_deleted", "path": old})
        for path in {old, new}:
            if CHECK_CONFIG.search(path) and path not in their_paths:
                flags.append({"kind": "check_config_changed", "path": path, "status": status})

    before, after = test_counts(results, root), test_counts(results, branch)
    their_before, their_after = test_counts(repo, base), test_counts(repo, reference)
    for package, count in sorted(before.items()):
        removed = count - after.get(package, 0)
        allowed = their_before.get(package, 0) - their_after.get(package, 0)
        if removed > max(allowed, 0):
            flags.append({"kind": "tests_removed", "package": package, "removed": removed,
                          "reference_removed": max(allowed, 0)})

    their_lines = changed_lines(repo, base, reference)
    their_markers = {marker(found) for path, lines in their_lines.items() if SOURCE.search(path)
                     for sign, text in lines if sign == "+" for found in SKIP_MARKER.findall(text)}
    their_build = {(path, sign, text) for path, lines in their_lines.items() if BUILD_SCRIPT.search(path)
                   for sign, text in lines}
    build_changed = set()
    for path, lines in changed_lines(results, root, branch).items():
        for sign, text in lines:
            if sign == "+" and SOURCE.search(path):
                for found in SKIP_MARKER.findall(text):
                    if marker(found) not in their_markers:
                        flags.append({"kind": "skip_marker", "path": path, "line": text})
            if BUILD_SCRIPT.search(path):
                build_changed.add(path)
                if CHECK_LINE.search(text) and (path, sign, text) not in their_build:
                    flags.append({"kind": "build_check_line", "path": path, "change": sign, "line": text})
    return {"passed": not flags, "flags": flags, "build_scripts_changed": sorted(build_changed)}


# --- steps 1 and 3: the gate and the reference tests, in a worker with the result ---

def reference_tests(worker, entry, reference_dir):
    """Copy the reference change's test files into the result and run the tests. A copied file that doesn't compile
    is dropped and the run repeated; when the errors are only in the result's own files, the copied files that
    replaced one of the result's (a shared fake, say) are dropped instead."""
    files = {item["path"]: item["status"] for item in entry["reference_tests"] if item["status"] != "deleted"}
    if not files:
        return {"files": 0}
    active, dropped, record = dict(files), [], {"files": len(files)}
    for _ in range(REFERENCE_ATTEMPTS):
        copy = (" && cd /eval-run/reference && cp --parents " + " ".join(f"'{p}'" for p in active) + " /work/repo/"
                if active else "")
        worker.exec("cd /work/repo && git reset -q --hard && git clean -qfd"
                    " && find . -path '*/build/test-results' -prune -exec rm -rf {} +" + copy)
        if not active:
            break
        try:
            result = worker.exec(f"cd /work/repo && {TESTS} 2>&1", timeout=GATE_TIMEOUT, check=False)
        except subprocess.TimeoutExpired:
            record["timed_out"] = True
            break
        errors = set(COMPILE_ERROR.findall(result.stdout))
        if not errors:
            record["gradle_exit"] = result.returncode
            break
        if errors & active.keys():
            for path in sorted(errors & active.keys()):
                dropped.append({"path": path, "reason": "doesn't compile against the result"})
                del active[path]
            continue
        replacing = [path for path, status in active.items() if status == "modified"]
        if replacing:
            for path in replacing:
                dropped.append({"path": path, "reason": "replacing the result's version breaks the result's tests"})
                del active[path]
            continue
        record["compile_errors_elsewhere"] = sorted(errors)
        break
    else:
        record["gave_up"] = True
    # Flat copies: the log directory is the host's, but directories the container made there wouldn't be.
    worker.exec("cd /work/repo && find . -path '*/build/test-results/*' -name 'TEST-*.xml'"
                " | while read -r f; do cp \"$f\" \"/eval-log/test-results$(echo \"${f#.}\" | tr / _)\"; done")
    suites = {}
    for path in (worker.dir / "log").glob("test-results*.xml"):
        try:
            suite = ElementTree.parse(path).getroot()
        except ElementTree.ParseError:
            continue
        counts = suites.setdefault(suite.get("name"), [0, 0, 0])
        counts[0] += int(suite.get("tests", 0))
        counts[1] += int(suite.get("failures", 0)) + int(suite.get("errors", 0))
        counts[2] += int(suite.get("skipped", 0))
    tests = failed = skipped = 0
    ran = []
    if not record.get("compile_errors_elsewhere") and not record.get("timed_out") and not record.get("gave_up"):
        for path in active:
            package = re.search(r"^package\s+([\w.]+)", (reference_dir / path).read_text(), re.MULTILINE)
            name = f"{package[1]}.{Path(path).stem}" if package else Path(path).stem
            if name in suites:
                ran.append(path)
                tests, failed, skipped = (tests + suites[name][0], failed + suites[name][1],
                                          skipped + suites[name][2])
        record["compiled"] = sorted(active)
    else:
        record["compiled"] = []
    record.update({"dropped": dropped, "ran": sorted(ran), "tests": tests, "passed": tests - failed - skipped,
                   "failed": failed, "skipped": skipped})
    return record


def check(args, entry, run, directory, name):
    """Steps 1 and 3 in a fresh worker holding a clone of the result branch."""
    worker = Worker(args, name, entry["change"], directory)
    worker.real = False  # no sessions here
    directory.mkdir(parents=True)
    config = write_config(directory / "config", entry, args.repo, known=run["openspec_known_failures"],
                          fake_gate=args.fake_gate)
    reference_dir = config / "reference"
    for item in entry["reference_tests"]:
        if item["status"] != "deleted":
            target = reference_dir / item["path"]
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(sh("git", "-C", args.repo, "show", f"{entry['reference']}:{item['path']}").stdout)
    try:
        worker.start(WORK / "results.git", config, branch=run["run"])
        started = time.time()
        passed, log = worker.gate()
        gate = {"passed": passed, "seconds": round(time.time() - started), "log_tail": log[-3000:]}
        print("  reference tests")
        return gate, reference_tests(worker, entry, reference_dir)
    finally:
        if not args.keep:
            worker.stop()


# --- step 4: the holistic review, blind ---

def blind_source(entry, run, directory):
    """A bare repository with two commits: the change's base snapshot, holding the original proposal, and the
    result's code changes squashed into one commit. The result's own openspec/ (ticked tasks, Thin spec's cut-down
    proposal) is replaced with the base's, so the judge sees every setup's work against the same documents."""
    source = directory / "judge.git"
    sh("git", "clone", "-q", "--bare", "--no-local", str(WORK / "snapshots" / f"{entry['change']}.git"), str(source))
    git = ["git", "-C", str(source)]
    sh(*git, "fetch", "-q", str(WORK / "results.git"), f"refs/heads/{run['run']}:refs/result")
    openspec = sh(*git, "rev-parse", "main:openspec").stdout.strip()
    entries = [f"040000 tree {openspec}\topenspec" if line.endswith("\topenspec") else line
               for line in sh(*git, "ls-tree", "refs/result").stdout.splitlines()]
    tree = sh(*git, "mktree", input="\n".join(entries) + "\n").stdout.strip()
    base = sh(*git, "rev-parse", "main").stdout.strip()
    sh(*git, "update-ref", "refs/heads/main", commit_tree(source, tree, "Implementation", base))
    sh(*git, "update-ref", "-d", "refs/result")
    sh(*git, "remote", "remove", "origin")
    (source / "FETCH_HEAD").unlink()  # it names the run
    sh(*git, "reflog", "expire", "--expire=now", "--all")
    sh(*git, "gc", "-q", "--prune=now")
    return source, base


def holistic(args, entry, run, directory, name, spent):
    worker = Worker(args, name, entry["change"], directory)
    worker.spent_before = spent
    directory.mkdir(parents=True)
    config = write_config(directory / "config", entry, args.repo)
    source, base = blind_source(entry, run, directory)
    try:
        worker.start(source, config, gate=False)
        text = fill((EVAL / "prompts" / "judge.md").read_text(), change=entry["change"], base=base)
        try:
            found = verdict(worker.session("judge", OPUS, text), blocking=("high",))
        except Budget:
            found = {"error": "cost limit"}
        found["sessions"] = worker.sessions
        found["cost_usd"] = round(worker.cost, 6)
        return found
    finally:
        if not args.keep:
            worker.stop()


# --- the judgment ---

def judge(args, entry, run, spent):
    name = secrets.token_hex(4)  # the only name the judge's worker and its repository carry
    directory = JUDGING / name
    print(f"{run['run']}: judging as {name}")
    judgment = {"run": run["run"], "judge_id": name, "setup": run["setup"], "change": run["change"],
                "rep": run["rep"], "run_outcome": run["outcome"], "judge": "real" if args.real else "stub",
                "fake_gate": args.fake_gate, "judged_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    started = time.time()
    if run["outcome"] != "completed":  # a parked or failed run produced no change to merge
        judgment.update({"accepted": False, "reason": f"the run ended {run['outcome']}", "cost_usd": 0.0})
        return judgment
    judgment["tamper"] = tamper(entry, run["run"], args.repo)
    judgment["gate"], judgment["reference_tests"] = check(args, entry, run, directory / "check", f"check-{name}")
    if not judgment["gate"]["passed"]:
        reason = "the gate failed"
    elif not judgment["tamper"]["passed"]:
        reason = "tampering: " + ", ".join(sorted({flag["kind"] for flag in judgment["tamper"]["flags"]}))
    else:
        reason = None
    if reason:
        judgment["holistic"] = {"skipped": reason}
    else:
        review = judgment["holistic"] = holistic(args, entry, run, directory / "judge", f"judge-{name}", spent)
        highs = [f for f in review.get("findings", []) if str(f.get("severity", "")).lower() == "high"]
        if review.get("error"):
            reason = f"the holistic review stopped: {review['error']}"
        elif review.get("unparsed"):
            reason = "the holistic review gave no readable verdict"
        elif highs:
            reason = f"the holistic review found {len(highs)} High finding(s)"
        # A revise without High findings contradicts the prompt, which accepts those: it counts as an accept.
    judgment.update({"accepted": reason is None, "reason": reason or "accepted",
                     "cost_usd": judgment.get("holistic", {}).get("cost_usd", 0.0),
                     "seconds": round(time.time() - started)})
    return judgment


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("runs", nargs="*", help="run names (default: every run not judged yet)")
    parser.add_argument("--real", action="store_true", help="call the real Claude Code CLI (spends money)")
    parser.add_argument("--max-cost", type=float, help="stop judging when the reviews have cost more (USD)")
    parser.add_argument("--fake-gate", action="store_true", help="the gate passes at once: tests the plumbing")
    parser.add_argument("--force", action="store_true", help="judge runs that already have a judgment again")
    parser.add_argument("--keep", action="store_true", help="keep the containers and volumes after judging")
    parser.add_argument("--image", default="localhost/herd-eval-driving-log")
    parser.add_argument("--repo", default=os.path.expanduser("~/depot/driving-log"),
                        help="a Driving Log clone, for the reference change and supplied files")
    args = parser.parse_args()
    args.warm = False
    if args.real and not os.environ.get("ANTHROPIC_API_KEY"):
        parser.error("--real needs ANTHROPIC_API_KEY in the environment")
    if args.real and not args.max_cost:
        parser.error("--real needs --max-cost, and a key with its own spend limit")

    corpus = {entry["change"]: entry for entry in json.loads((EVAL / "corpus.json").read_text())["changes"]}
    names = args.runs or sorted(path.parent.name for path in (WORK / "runs").glob("*/run.json"))
    runs = []
    for name in names:
        path = WORK / "runs" / name / "run.json"
        if not path.exists():
            parser.error(f"no run record at {path}")
        run = json.loads(path.read_text())
        if args.real and run["claude"] != "real":
            print(f"{name}: skipped, a stub run is judged with the stub")
            continue
        destination = (RESULTS if args.real and run["claude"] == "real" else STUB_JUDGMENTS) / f"{name}.json"
        if destination.exists() and not args.force:
            if args.runs:
                parser.error(f"{destination} exists: use --force to judge again")
            continue
        runs.append((run, destination))
    random.shuffle(runs)  # judged in random order, so no setup is always judged first

    spent = 0.0
    for run, destination in runs:
        judgment = judge(args, corpus[run["change"]], run, spent)
        spent += judgment["cost_usd"]
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(json.dumps(judgment, indent=2) + "\n")
        print(f"{run['run']}: {judgment['reason']} -> {destination}")
        if judgment.get("holistic", {}).get("error") == "cost limit":
            print(f"stopped at the cost limit, ${spent:.4f}")
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
