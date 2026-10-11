#!/usr/bin/env python3
"""Build the evaluation corpus from Driving Log's archived changes (docs/evaluation-plan.md, step 1).

For each change it finds the base, the reference and the original proposal in Driving Log's history, checks that the
span between them holds no other change's implementation, and records the gate, the size, the reference tests and the
edits made to the proposal before archiving. The record goes to eval/corpus.json. It also writes each base snapshot: a
bare repository holding the base tree in one commit with no history, with the original proposal in place.

Usage: eval/corpus.py --repo ~/depot/driving-log [--out eval/work]
"""

import argparse
import difflib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# The corpus, smallest first. `base` overrides the base commit when the proposal's own commit can't be one: the span
# from it holds another change's implementation. The proposal still comes from the commit that added it.
CORPUS = [
    {"change": "change-picture-badge-icon"},
    {"change": "confirm-lower-odometer"},
    {"change": "odometer-leading-zero-prefix"},
    {"change": "add-landing-screen"},
    {"change": "petroleum-color-theme"},
    # Proposed together with add-landing-screen, which was implemented first: start after its archive.
    {"change": "add-direct-logging", "base": "49653ee"},
    {"change": "add-vehicle-color"},
    {"change": "add-distance-logging"},
    {"change": "clean-up-lint-baselines"},
]

# Before Driving Log's CLAUDE.md named a final regression run, it listed these two Gradle tasks as the build and test
# commands. The strict validation is what every later gate runs; the snapshot check below shows it passes at the base.
EARLY_GATE = ["./gradlew :shared:allTests :androidApp:assembleDebug", "openspec validate --all --strict"]
FINAL_RUN = re.compile(r"final regression run of a change is (`[^`]+`(?:,? and `[^`]+`)*)")

# The production flavor needs google-services.json, which is gitignored. Bases that apply the Google Services plugin
# get the placeholder CI uses, as content supplied from outside the snapshot.
GOOGLE_SERVICES = "androidApp/src/production/google-services.json"
PLACEHOLDER = ".github/ci/google-services.placeholder.json"

SNAPSHOT_IDENT = {
    "GIT_AUTHOR_NAME": "herd eval", "GIT_AUTHOR_EMAIL": "eval@herd.invalid",
    "GIT_COMMITTER_NAME": "herd eval", "GIT_COMMITTER_EMAIL": "eval@herd.invalid",
    "GIT_AUTHOR_DATE": "2026-01-01T00:00:00Z", "GIT_COMMITTER_DATE": "2026-01-01T00:00:00Z",
}

TEST_DIR = re.compile(r"/src/\w*[Tt]est\w*/")


class Repo:
    def __init__(self, path):
        self.path = path

    def git(self, *args, env=None, input=None):
        result = subprocess.run(["git", "-C", self.path, *args], capture_output=True, text=True, input=input,
                                env={**os.environ, **(env or {})})
        if result.returncode != 0:
            raise RuntimeError(f"git {' '.join(args)}: {result.stderr.strip()}")
        return result.stdout

    def lines(self, *args):
        return [line for line in self.git(*args).splitlines() if line]

    def sha(self, rev):
        return self.git("rev-parse", "--verify", f"{rev}^{{commit}}").strip()

    def exists(self, rev, path):
        return subprocess.run(["git", "-C", self.path, "cat-file", "-e", f"{rev}:{path}"],
                              capture_output=True).returncode == 0

    def show(self, rev, path):
        return self.git("show", f"{rev}:{path}")


def find_commits(repo, main, change):
    """The commit that added the proposal and the commit that archived the change."""
    proposal = repo.lines("log", "--reverse", "--diff-filter=A", "--format=%H", main, "--",
                          f"openspec/changes/{change}/proposal.md")
    archived = repo.lines("log", "--diff-filter=A", "--format=%H", main, "--",
                          f"openspec/changes/archive/*-{change}/proposal.md")
    if not proposal or not archived:
        raise RuntimeError(f"{change}: no proposal or archive commit on {main}")
    return proposal[0], archived[-1]


def archive_dir(repo, archive, change):
    for path in repo.lines("ls-tree", "-d", "--name-only", archive, "openspec/changes/archive/"):
        if re.fullmatch(rf"openspec/changes/archive/\d{{4}}-\d{{2}}-\d{{2}}-{re.escape(change)}", path):
            return path
    raise RuntimeError(f"{change}: no archive directory at {archive}")


def classify_span(repo, base, reference, change):
    """Sort the span's commits that touch code (anything outside openspec/).

    A commit that touches the change's own directory is the change's. One that touches another change's directory with
    anything but new files is that change's work, which makes the span impure. One that touches no change directory is
    unattributed: it stays in the reference, and the record lists it."""
    foreign, unattributed = [], []
    own = re.compile(rf"openspec/changes/(archive/[\d-]+-)?{re.escape(change)}/")
    for sha in repo.lines("rev-list", "--reverse", f"{base}..{reference}"):
        entries = [line.split("\t") for line in repo.lines("diff-tree", "--no-commit-id", "-r", "--name-status", sha)]
        code = [path for _, path, *_ in entries if not path.startswith("openspec/")]
        if not code or any(own.match(path) for _, path, *_ in entries):
            continue
        others = [path for status, path, *_ in entries
                  if path.startswith("openspec/changes/") and not status.startswith("A")]
        subject = repo.git("log", "-1", "--format=%s", sha).strip()
        entry = {"commit": sha[:12], "subject": subject, "files": len(code)}
        (foreign if others else unattributed).append(entry)
    return foreign, unattributed


def gate(repo, base):
    text = repo.show(base, "CLAUDE.md") if repo.exists(base, "CLAUDE.md") else ""
    match = FINAL_RUN.search(text)
    if match:
        return re.findall(r"`([^`]+)`", match.group(1)), "CLAUDE.md, final regression run"
    return list(EARLY_GATE), "CLAUDE.md's build and test commands (no final regression run yet)"


def shortstat(repo, *args):
    out = repo.git("diff", "--shortstat", *args).strip()
    numbers = {key: int(n) for n, key in re.findall(r"(\d+) (file|insertion|deletion)", out)}
    return {"files": numbers.get("file", 0), "added": numbers.get("insertion", 0),
            "deleted": numbers.get("deletion", 0)}


def reference_tests(repo, base, reference):
    tests = []
    for line in repo.lines("diff", "--name-status", "--no-renames", base, reference, "--", ".", ":(exclude)openspec"):
        status, path = line.split("\t", 1)
        if TEST_DIR.search(path) and status in ("A", "M"):
            tests.append({"path": path, "status": "added" if status == "A" else "modified"})
    return tests


def snapshot(repo, base, proposal, change, dest):
    """A bare repository with one commit: the base tree, with the original proposal in place of the base's version."""
    with tempfile.TemporaryDirectory() as tmp:
        env = {"GIT_INDEX_FILE": os.path.join(tmp, "index")}
        repo.git("read-tree", base, env=env)
        target = f"openspec/changes/{change}"
        if proposal != base:
            repo.git("rm", "-r", "--cached", "--quiet", "--ignore-unmatch", target, env=env)
            repo.git("read-tree", f"--prefix={target}/", f"{proposal}:{target}", env=env)
        tree = repo.git("write-tree", env=env).strip()
    commit = repo.git("commit-tree", tree, "-m", "Base snapshot", env=SNAPSHOT_IDENT).strip()
    if dest.exists():
        shutil.rmtree(dest)
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(dest)], check=True)
    repo.git("push", "-q", str(dest.resolve()), f"{commit}:refs/heads/main")
    return tree, commit


def validate_snapshot(dest):
    """The items the strict OpenSpec validation fails on the snapshot, before any work, and the full report.

    Other in-flight proposals at a base can fail it (a later CLI is stricter than the one they were written with). The
    gate counts those as known failures; only the change's own item failing at the base breaks the corpus entry."""
    with tempfile.TemporaryDirectory() as tmp:
        subprocess.run(["git", "clone", "-q", str(dest), tmp], check=True)
        result = subprocess.run(["openspec", "validate", "--all", "--strict", "--no-interactive"], cwd=tmp,
                                capture_output=True, text=True)
        report = (result.stdout + result.stderr).strip()
        return re.findall(r"^✗ (\S+)$", report, re.MULTILINE), report


def proposal_edits(repo, original, archived):
    """Lines changed in the proposal between its first version and its archived one. Ticked task boxes don't count:
    every change ticks them all."""
    def files(treeish):
        names = repo.lines("ls-tree", "-r", "--name-only", treeish)
        return {name: re.sub(r"^(\s*- )\[x\]", r"\1[ ]", repo.git("show", f"{treeish}/{name}"), flags=re.MULTILINE)
                for name in names}
    before, after = files(original), files(archived)
    added = deleted = 0
    for name in before.keys() | after.keys():
        for line in difflib.unified_diff(before.get(name, "").splitlines(), after.get(name, "").splitlines(),
                                         lineterm="", n=0):
            if line.startswith("+") and not line.startswith("+++"):
                added += 1
            elif line.startswith("-") and not line.startswith("---"):
                deleted += 1
    return {"files": sum(before.get(n) != after.get(n) for n in before.keys() | after.keys()),
            "added": added, "deleted": deleted}


def build_entry(repo, main, spec, out):
    change = spec["change"]
    proposal, archive = find_commits(repo, main, change)
    base = repo.sha(spec.get("base", proposal))
    reference = archive
    archived = archive_dir(repo, archive, change)
    try:
        repo.git("merge-base", "--is-ancestor", proposal, base)
    except RuntimeError:
        raise RuntimeError(f"{change}: the proposal commit is not an ancestor of the base") from None
    if not repo.exists(base, f"openspec/changes/{change}/proposal.md"):
        raise RuntimeError(f"{change}: the base doesn't hold the proposal")
    foreign, unattributed = classify_span(repo, base, reference, change)

    original = f"{proposal}:openspec/changes/{change}"
    edits = proposal_edits(repo, original, f"{archive}:{archived}")
    work = out / "corpus" / change
    work.mkdir(parents=True, exist_ok=True)
    (work / "proposal-edits.diff").write_text(repo.git("diff", original, f"{archive}:{archived}"))

    commands, source = gate(repo, base)
    supply = []
    gradle = repo.show(base, "androidApp/build.gradle.kts") if repo.exists(base, "androidApp/build.gradle.kts") else ""
    if "googleServices" in gradle and not repo.exists(base, GOOGLE_SERVICES):
        supply.append({"path": GOOGLE_SERVICES, "from": f"{repo.sha(main)[:12]}:{PLACEHOLDER}"})

    tree, commit = snapshot(repo, base, proposal, change, out / "snapshots" / f"{change}.git")
    failures, report = validate_snapshot(out / "snapshots" / f"{change}.git")
    (work / "validate.txt").write_text(report + "\n")

    return {
        "change": change,
        "proposal_commit": proposal[:12],
        "base": base[:12],
        "reference": reference[:12],
        "archive_dir": archived,
        "size": shortstat(repo, base, reference, "--", ".", ":(exclude)openspec"),
        "proposal_edits": edits,
        "gate": commands,
        "gate_source": source,
        "supply": supply,
        "reference_tests": reference_tests(repo, base, reference),
        "unattributed_commits": unattributed,
        "foreign_commits": foreign,
        "snapshot": {"commit": commit[:12], "tree": tree[:12]},
        "openspec_known_failures": failures,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo", required=True, help="a Driving Log clone")
    parser.add_argument("--main", default="origin/main", help="the branch the history is read from")
    parser.add_argument("--out", default=str(Path(__file__).parent / "work"),
                        help="where snapshots and per-change files go (default: eval/work, not committed)")
    parser.add_argument("--record", default=str(Path(__file__).parent / "corpus.json"))
    args = parser.parse_args()

    repo = Repo(os.path.expanduser(args.repo))
    out = Path(args.out)
    entries, dropped = [], []
    for spec in CORPUS:
        entry = build_entry(repo, args.main, spec, out)
        problems = []
        if entry["foreign_commits"]:
            problems.append("the span holds another change's work")
        if f"change/{entry['change']}" in entry["openspec_known_failures"]:
            problems.append(f"its proposal fails openspec validation (see {out}/corpus/{entry['change']}/validate.txt)")
        if problems:
            entry["dropped"] = problems
            dropped.append(entry["change"])
        entries.append(entry)
        size = entry["size"]
        print(f"{entry['change']:30} base {entry['base'][:7]} ref {entry['reference'][:7]} "
              f"+{size['added']} -{size['deleted']}  edits +{entry['proposal_edits']['added']} "
              f"-{entry['proposal_edits']['deleted']}  tests {len(entry['reference_tests'])}"
              + (f"  DROPPED: {'; '.join(problems)}" if problems else ""))

    record = {"repository": "mliikanen/driving-log", "main": repo.sha(args.main)[:12], "changes": entries}
    Path(args.record).write_text(json.dumps(record, indent=2) + "\n")
    print(f"wrote {args.record}; snapshots in {out}/snapshots")
    return 1 if dropped else 0


if __name__ == "__main__":
    sys.exit(main())
