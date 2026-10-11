#!/usr/bin/env python3
"""Run one evaluation setup on one corpus change (docs/evaluation-plan.md, step 2).

The run happens in a fresh worker container from the change's base snapshot, under the worker contract: read-only root,
per-run scratch volumes, the caches as overlays, the memory, CPU and pid limits, and egress only through a proxy that
allows the project's hosts. Every session's JSON output and the gate log land in eval/work/runs/<run>/, and the result
becomes branch <run> in the results repository, eval/work/results.git.

Usage: eval/run.py <setup> <change> [--rep N] [--real] [--fake-gate] [--max-cost USD]
       eval/run.py --warm

Without --real, the Claude Code CLI is replaced by eval/container/stub/claude, which makes no model calls. --warm fills
the cache volumes by running the gate at every base, which also checks that each base passes it.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

EVAL = Path(__file__).resolve().parent
WORK = EVAL / "work"

OPUS, SONNET, HAIKU = "claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-4-5-20251001"

# The setups (the plan's table). `rounds` lists the implementer of each review round of a task: Cheap herd's last round
# is the stronger attempt.
SETUPS = {
    "baseline": {"kind": "single", "model": OPUS, "prompt": "apply"},
    "thin-spec": {"kind": "single", "model": OPUS, "prompt": "thin"},
    "herd": {"kind": "herd", "rounds": [SONNET, SONNET, SONNET], "guard": False},
    "cheap-herd": {"kind": "herd", "rounds": [HAIKU, HAIKU, SONNET], "guard": False},
    "guarded-herd": {"kind": "herd", "rounds": [HAIKU, HAIKU, SONNET], "guard": True},
}
REVIEWER = OPUS

# The design's unit timeouts (docs/design.md, unit kinds), in seconds. The guard writes code like an implementer.
TIMEOUT = {"apply": 4 * 3600, "thin": 4 * 3600, "implement": 3600, "guard": 3600, "fix": 3600, "review": 1200,
           "holistic": 1800}
GATE_TIMEOUT = 3600

# Driving Log's egress (.herd/project.yaml) plus the model API.
EGRESS = ["repo.maven.apache.org", "maven.google.com", "dl.google.com", "plugins.gradle.org",
          "plugins-artifacts.gradle.org", "repo.gradle.org", "services.gradle.org", "github.com",
          "release-assets.githubusercontent.com", "api.anthropic.com"]
LIMITS = ["--memory", "8g", "--cpus", "4", "--pids-limit", "4096"]
CACHES = {"eval-gradle": "/home/agent/.gradle", "eval-konan": "/home/agent/.konan"}
OUTSIDE, PROXY = "eval-ext", "eval-proxy"
PROXY_URL = f"http://{PROXY}:3128"

TEST_PATH = re.compile(r"/src/\w*[Tt]est\w*/")  # test source sets: commonTest, androidHostTest, androidTest...
TASK = re.compile(r"^(\s*)- \[( |x)\] (\d+(?:\.\d+)*)\.?\s+(.*)$")


def sh(*args, check=True, capture=True, input=None, timeout=None):
    result = subprocess.run(args, capture_output=capture, text=True, input=input, timeout=timeout)
    if check and result.returncode != 0:
        raise RuntimeError(f"{' '.join(args[:4])}…: {(result.stderr or '').strip()[-2000:]}")
    return result


def fill(template, **values):
    """Substitute {name} placeholders. The templates also hold literal JSON braces, so str.format won't do."""
    for name, value in values.items():
        template = template.replace("{" + name + "}", value)
    return template


def prompt(name, **values):
    environment = (EVAL / "prompts" / "_environment.md").read_text().strip()
    return fill((EVAL / "prompts" / f"{name}.md").read_text(), environment=environment, **values)


def verdict(text):
    """The last `VERDICT: {...}` line of a review. A review without one counts as a revise."""
    for line in reversed(text.splitlines()):
        if line.strip().startswith("VERDICT:"):
            try:
                found = json.loads(line.split("VERDICT:", 1)[1])
                findings = found.get("findings", [])
                if found.get("verdict") in ("accept", "revise") and isinstance(findings, list) \
                        and all(isinstance(f, dict) for f in findings):
                    # An accept that lists blocking findings contradicts itself: the prompt makes them a revise.
                    blocking = any(str(f.get("severity", "")).lower() in ("high", "medium") for f in findings)
                    if found["verdict"] == "accept" and blocking:
                        return {"verdict": "revise", "findings": findings, "contradictory_accept": True}
                    return {"verdict": found["verdict"], "findings": findings}
            except (json.JSONDecodeError, AttributeError):
                pass
            break
    return {"verdict": "revise", "findings": [{"severity": "High", "text": "The review gave no readable verdict."}],
            "unparsed": True}


def parse_tasks(text):
    """The unticked tasks of a tasks.md, with their wrapped lines joined."""
    tasks, current = [], None
    for line in text.splitlines():
        match = TASK.match(line)
        if match:
            current = {"id": match[3], "text": match[4].strip(), "indent": len(match[1]), "done": match[2] == "x"}
            tasks.append(current)
        elif current and line.strip() and len(line) - len(line.lstrip()) > current["indent"] \
                and not line.lstrip().startswith("- ["):
            current["text"] += " " + line.strip()
        else:
            current = None
    return [{"id": t["id"], "text": t["text"]} for t in tasks if not t["done"]]


def thin_proposal(text):
    """The proposal's title, Why and What Changes sections."""
    keep, out = True, []
    for line in text.splitlines():
        if line.startswith("## "):
            keep = line[3:].strip().lower() in ("why", "what changes")
        if keep:
            out.append(line)
    return "\n".join(out).rstrip() + "\n"


class Run:
    def __init__(self, args, entry):
        self.args, self.entry = args, entry
        self.setup = SETUPS[args.setup]
        self.change = entry["change"]
        self.id = f"{args.setup}-{self.change}-{args.rep}"
        self.dir = WORK / "runs" / self.id
        self.container = f"eval-{self.id}"
        self.network = f"{self.container}-net"
        self.gate_calls = 0
        self.volumes = {f"{self.container}-{name}": path
                        for name, path in (("work", "/work"), ("home", "/home/agent"), ("tmp", "/tmp"))}
        self.sessions, self.cost, self.started = [], 0.0, time.time()
        self.record = {"run": self.id, "setup": args.setup, "change": self.change, "rep": args.rep,
                       "claude": "real" if args.real else "stub", "fake_gate": args.fake_gate,
                       "base": entry["base"], "image": args.image}

    # --- the source repository and the container ---

    def source(self):
        """A bare repository with the run's single starting commit: the snapshot, or for Thin spec, the snapshot with
        the change cut down to its proposal's Why and What Changes. Built from objects, so nothing of the full
        proposal reaches the run's repository."""
        snapshot = WORK / "snapshots" / f"{self.change}.git"
        if not snapshot.exists():
            raise RuntimeError(f"no snapshot at {snapshot}: run eval/corpus.py first")
        source = self.dir / "source.git"
        sh("git", "clone", "-q", "--bare", "--no-local", str(snapshot), str(source))
        if self.setup.get("prompt") != "thin":
            return source
        git = ["git", "-C", str(source)]
        target = f"openspec/changes/{self.change}"
        proposal = sh(*git, "show", f"main:{target}/proposal.md").stdout
        with tempfile.TemporaryDirectory() as tmp:
            env = {**os.environ, "GIT_INDEX_FILE": f"{tmp}/index"}
            run = lambda *a, **k: subprocess.run([*git, *a], env=env, check=True, capture_output=True, text=True, **k)
            run("read-tree", "main")
            run("rm", "-r", "--cached", "--quiet", target)
            blob = run("hash-object", "-w", "--stdin", input=thin_proposal(proposal)).stdout.strip()
            run("update-index", "--add", "--cacheinfo", f"100644,{blob},{target}/proposal.md")
            meta = f"{target}/.openspec.yaml"
            if sh(*git, "cat-file", "-e", f"main:{meta}", check=False).returncode == 0:
                run("update-index", "--add", "--cacheinfo",
                    f"100644,{sh(*git, 'rev-parse', f'main:{meta}').stdout.strip()},{meta}")
            tree = run("write-tree").stdout.strip()
        ident = {k: v for k, v in os.environ.items()}
        ident.update({"GIT_AUTHOR_NAME": "herd eval", "GIT_AUTHOR_EMAIL": "eval@herd.invalid",
                      "GIT_COMMITTER_NAME": "herd eval", "GIT_COMMITTER_EMAIL": "eval@herd.invalid",
                      "GIT_AUTHOR_DATE": "2026-01-01T00:00:00Z", "GIT_COMMITTER_DATE": "2026-01-01T00:00:00Z"})
        commit = subprocess.run([*git, "commit-tree", tree, "-m", "Base snapshot"], env=ident, check=True,
                                capture_output=True, text=True).stdout.strip()
        sh(*git, "update-ref", "refs/heads/main", commit)
        sh(*git, "reflog", "expire", "--expire=now", "--all")
        sh(*git, "gc", "-q", "--prune=now")
        return source

    def config(self):
        """The read-only files the gate reads: its commands, the supplied files, and (after setup) known failures."""
        config = self.dir / "config"
        (config / "supply").mkdir(parents=True)
        (config / "gate-commands").write_text("\n".join(self.entry["gate"]) + "\n")
        (config / "known-failures").write_text("")
        if self.args.fake_gate:
            (config / "fake-gate").write_text("")
        for item in self.entry["supply"]:
            commit, path = item["from"].split(":", 1)
            content = sh("git", "-C", self.args.repo, "show", f"{commit}:{path}").stdout
            target = config / "supply" / item["path"]
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
        return config

    def proxy(self):
        """The shared egress proxy, plus this run's own internal network holding only the worker and the proxy, so
        runs can't reach each other. Restarting the proxy (a changed allow-list) cuts off runs in progress."""
        allow = ",".join(EGRESS)
        if sh("podman", "network", "exists", OUTSIDE, check=False).returncode != 0:
            sh("podman", "network", "create", OUTSIDE)
        sh("podman", "network", "create", "--internal", self.network)
        running = sh("podman", "inspect", PROXY, "--format", "{{.State.Running}} {{range .Config.Env}}{{.}} {{end}}",
                     check=False)
        if not (running.returncode == 0 and running.stdout.startswith("true") and f"ALLOW={allow} " in running.stdout):
            sh("podman", "rm", "-f", PROXY, check=False)
            sh("podman", "run", "-d", "--name", PROXY, "--network", OUTSIDE, "-e", f"ALLOW={allow}",
               "-v", f"{EVAL / 'proxy.py'}:/proxy.py:ro,Z", "docker.io/library/python:3.13-slim", "python", "-u",
               "/proxy.py")
        sh("podman", "network", "connect", self.network, PROXY)

    def start(self):
        self.dir.mkdir(parents=True)
        (self.dir / "log").mkdir()
        (self.dir / "log").chmod(0o777)  # the container's agent is a sub-UID on the host
        (self.dir / "sessions").mkdir()
        source, config = self.source(), self.config()
        self.proxy()
        for volume in self.volumes:
            sh("podman", "volume", "create", volume)
        mounts = [arg for volume, path in self.volumes.items() for arg in ("-v", f"{volume}:{path}")]
        sh("podman", "run", "--rm", "--user", "root", *mounts, self.args.image, "chown", "1000:1000",
           *self.volumes.values())
        caches = []
        for volume, path in CACHES.items():
            if sh("podman", "volume", "exists", volume, check=False).returncode != 0:
                raise RuntimeError(f"no cache volume {volume}: run eval/run.py --warm first")
            if self.args.warm:
                caches += ["-v", f"{volume}:{path}"]
            else:
                mountpoint = sh("podman", "volume", "inspect", volume, "--format", "{{.Mountpoint}}").stdout.strip()
                caches += ["-v", f"{mountpoint}:{path}:O"]
        env = {"JAVA_TOOL_OPTIONS": f"-Dhttps.proxyHost={PROXY} -Dhttps.proxyPort=3128 -Dhttp.proxyHost={PROXY} "
                                    f"-Dhttp.proxyPort=3128",
               "HTTPS_PROXY": PROXY_URL, "https_proxy": PROXY_URL, "HTTP_PROXY": PROXY_URL, "http_proxy": PROXY_URL,
               "NO_PROXY": "localhost,127.0.0.1", "GRADLE_OPTS": "-Dorg.gradle.daemon=false",
               "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1", "DISABLE_AUTOUPDATER": "1", "DO_NOT_TRACK": "1",
               "OPENSPEC_TELEMETRY": "0"}
        stub = []
        if not self.args.real:
            stub = ["-v", f"{EVAL / 'container' / 'stub'}:/eval-stub:ro,z"]
            env["PATH"] = "/eval-stub:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:" \
                          "/opt/java/openjdk/bin"
        env_args = [arg for key, value in env.items() for arg in ("-e", f"{key}={value}")]
        if self.args.real:
            env_args += ["-e", "ANTHROPIC_API_KEY"]  # passed from the runner's environment, never on the command line
        sh("podman", "run", "-d", "--name", self.container, "--network", self.network, "--read-only",
           "--read-only-tmpfs=false", *LIMITS, "--cap-drop=all", *mounts, *caches, *stub,
           "-v", f"{EVAL / 'container' / 'gate'}:/eval/gate:ro,z", "-v", f"{config}:/eval-run:ro,z",
           "-v", f"{self.dir / 'log'}:/eval-log:z", "-v", f"{source}:/source:ro,z", *env_args, "-w", "/work",
           self.args.image, "sleep", "infinity")
        self.exec("git config --global user.name agent && git config --global user.email agent@herd.invalid"
                  " && git config --global --add safe.directory '*' && git clone -q /source /work/repo"
                  " && cd /work/repo && git remote remove origin"
                  )
        self.supply()
        self.base = self.git("rev-parse", "HEAD")
        # Known failures: what the strict validation fails on before any work. For every setup but Thin spec that's
        # the corpus record; Thin spec's cut-down change fails it too.
        output = self.exec("cd /work/repo && openspec validate --all --strict --no-interactive 2>&1", check=False).stdout
        known = re.findall(r"^✗ (\S+)$", output, re.MULTILINE)
        if self.setup.get("prompt") != "thin":
            if f"change/{self.change}" in known:
                raise RuntimeError(f"the change's own proposal fails validation at the start")
            if set(known) != set(self.entry["openspec_known_failures"]):
                print(f"  warning: validation at the start fails on {known}, the corpus records "
                      f"{self.entry['openspec_known_failures']}")
        (config / "known-failures").write_text("".join(f"{item}\n" for item in known))
        self.record["openspec_known_failures"] = known

    def exec(self, script, env=None, input=None, timeout=None, check=True):
        env_args = [arg for key, value in (env or {}).items() for arg in ("-e", f"{key}={value}")]
        return sh("podman", "exec", "-i", *env_args, "-w", "/work", self.container, "bash", "-c", script,
                  input=input, timeout=timeout, check=check)

    def git(self, *args):
        quoted = " ".join("'" + a.replace("'", "'\\''") + "'" for a in args)
        return self.exec(f"cd /work/repo && git {quoted}").stdout.strip()

    def stop(self):
        """Remove the container and the run's volumes. Safe to call on a run that only partly started."""
        sh("podman", "rm", "-f", "-t", "0", self.container, check=False)
        sh("podman", "network", "disconnect", "-f", self.network, PROXY, check=False)
        sh("podman", "network", "rm", "-f", self.network, check=False)
        for volume in self.volumes:
            sh("podman", "volume", "rm", "-f", volume, check=False)

    # --- sessions, the gate and commits ---

    def session(self, role, model, text, **env):
        """One `claude -p` session, its JSON output saved. Raises Budget when the run's cost passes --max-cost."""
        number = len(self.sessions) + 1
        name = f"{number:02d}-{role}" + (f"-{env['EVAL_TASK']}" if env.get("EVAL_TASK") else "") + \
               (f"-r{env['EVAL_ROUND']}" if env.get("EVAL_ROUND") else "")
        (self.dir / "sessions" / f"{name}.prompt.md").write_text(text)
        started = time.time()
        env = {"EVAL_ROLE": role, "EVAL_CHANGE": self.change, **{k: str(v) for k, v in env.items()}}
        for key in ("STUB_REVISE", "STUB_GATE", "STUB_TAMPER"):
            if os.environ.get(key):
                env[key] = os.environ[key]
        # TERM at the wall timeout, KILL 30 s later if the CLI ignores it: inside the outer 60 s margin, so nothing of
        # the session outlives it. timeout exits 124 after TERM, 137 after KILL.
        command = f"cd /work/repo && timeout --kill-after=30 {TIMEOUT[role]} claude -p --model {model} --output-format json " \
                  "--dangerously-skip-permissions"
        try:
            result = self.exec(command, env=env, input=text, timeout=TIMEOUT[role] + 60, check=False)
            stdout, code = result.stdout, result.returncode
            (self.dir / "sessions" / f"{name}.stderr").write_text(result.stderr)
        except subprocess.TimeoutExpired:  # past the kill deadline too: make sure nothing of the session survives
            self.exec("pkill -KILL -f 'claude -p' || true", check=False)
            stdout, code = "", 137
        (self.dir / "sessions" / f"{name}.json").write_text(stdout)
        try:
            output = json.loads(stdout)
        except json.JSONDecodeError:
            output = {}
        summary = {"session": name, "role": role, "model": model, "exit": code,
                   "timed_out": code in (124, 137), "seconds": round(time.time() - started),
                   "cost_usd": output.get("total_cost_usd", 0.0), "usage": output.get("usage", {}),
                   "is_error": output.get("is_error", not output), "num_turns": output.get("num_turns")}
        self.sessions.append(summary)
        self.cost += summary["cost_usd"] or 0.0
        print(f"  {name}: {model}, {summary['seconds']} s, ${summary['cost_usd']:.4f}"
              + (" TIMED OUT" + (" (killed)" if code == 137 else "") if summary["timed_out"] else "") + (" ERROR" if summary["is_error"] else ""))
        if self.args.max_cost and self.cost > self.args.max_cost:
            raise Budget()
        return output.get("result", "")

    def gate(self):
        self.gate_calls += 1
        result = self.exec("/eval/gate", env={"GATE_CALLER": "runner"}, timeout=GATE_TIMEOUT + 60, check=False)
        print(f"  gate: {'passed' if result.returncode == 0 else 'failed'}")
        return result.returncode == 0, (result.stdout + result.stderr)[-6000:]

    def review(self, text, **env):
        """A review session. Reviewers don't change files: anything one commits or leaves changed in the repository is
        discarded. Their home, /tmp and cache overlay persist into later sessions, like every session's."""
        head = self.git("rev-parse", "HEAD")
        try:
            return verdict(self.session(env.pop("role", "review"), REVIEWER, text, **env))
        finally:  # also when the session crosses --max-cost
            if self.git("rev-parse", "HEAD") != head or self.git("status", "--porcelain"):
                self.exec(f"cd /work/repo && git reset -q --hard {head} && git clean -qfd")
                self.record["reviewer_edits_discarded"] = self.record.get("reviewer_edits_discarded", 0) + 1
            self.supply()  # gitignored, so the checks above don't see an edit to a supplied file

    def supply(self):
        """Copy the supplied files (gitignored inputs such as google-services.json) into the repository."""
        self.exec("cd /eval-run/supply && find . -type f -exec cp --parents {} /work/repo/ \\;")

    def commit_leftovers(self, message):
        """Commit what a session left uncommitted, so every change is in the result."""
        if self.git("status", "--porcelain"):
            self.exec(f"cd /work/repo && git add -A && git commit -qm '{message}'")

    def tasks(self):
        text = self.exec(f"cat /work/repo/openspec/changes/{self.change}/tasks.md").stdout
        return parse_tasks(text)

    # --- the setups ---

    def single(self):
        self.session(self.setup["prompt"], self.setup["model"], prompt(self.setup["prompt"], change=self.change))
        self.commit_leftovers("Uncommitted work at the end of the session")
        return "completed"

    def herd(self):
        tasks = self.tasks()
        self.record["tasks"] = []
        for task in tasks:
            outcome = self.herd_task(task)
            self.record["tasks"].append(outcome)
            if not outcome["accepted"]:
                return "task_failed"  # the herd would park the change here
        review = self.review(prompt("holistic", change=self.change, base=self.base), role="holistic")
        self.record["holistic"] = review
        if review["verdict"] == "revise":
            findings = "\n".join(f"- {f.get('severity', '?')}: {f.get('text', '')}" for f in review["findings"])
            self.session("fix", self.setup["rounds"][-1], prompt("fix", change=self.change, findings=findings))
            self.commit_leftovers("fix: uncommitted work at the end of the session")
        return "completed"

    def herd_task(self, task):
        task_id, text = task["id"], task["text"]
        outcome = {"task": task_id, "rounds": 0, "accepted": False, "first_review_accept": None,
                   "stronger_attempt": False, "guard_files": [], "guard_edits_reverted": 0, "gate_failures": 0}
        guard_note, guard_commit = "", None
        if self.setup["guard"]:
            before = self.git("rev-parse", "HEAD")
            self.session("guard", REVIEWER, prompt("guard", change=self.change, task_id=task_id, task_text=text),
                         EVAL_TASK=task_id)
            self.commit_leftovers(f"guard {task_id}: uncommitted tests")
            guard_commit = self.git("rev-parse", "HEAD")
            changed = self.git("diff", "--name-only", before, guard_commit).splitlines()
            other = [path for path in changed if not TEST_PATH.search("/" + path)]
            if other:  # the guard writes tests only: anything else would be implementation nobody reviews
                paths = " ".join(f"'{p}'" for p in other)
                self.exec(f"cd /work/repo && git restore --source={before} --staged --worktree -- {paths}"
                          f" && git commit -qm 'guard {task_id}: revert changes outside test sources'")
                outcome["guard_non_test_reverted"] = other
                guard_commit = self.git("rev-parse", "HEAD")
            if guard_commit != before:
                outcome["guard_files"] = self.git("diff", "--name-only", before, guard_commit).splitlines()
                guard_note = (f"\nTests for this task were written ahead of you in commit {guard_commit[:12]}: "
                              f"{', '.join(outcome['guard_files'])}. Make them pass. They are read-only: a change "
                              "you make to them is reverted. If you think one is wrong, say so in your final message "
                              "(a test dispute) instead of editing it.\n")
        task_base = self.git("rev-parse", "HEAD")
        feedback = ""
        for round_number, model in enumerate(self.setup["rounds"], start=1):
            outcome["rounds"] = round_number
            outcome["stronger_attempt"] = model != self.setup["rounds"][0]
            started_at = self.git("rev-parse", "HEAD")
            self.session("implement", model,
                         prompt("implement", change=self.change, task_id=task_id, task_text=text, guard=guard_note,
                                feedback=feedback),
                         EVAL_TASK=task_id, EVAL_ROUND=round_number)
            if outcome["guard_files"]:
                changed = [f for f in outcome["guard_files"]
                           if self.exec(f"cd /work/repo && git diff --quiet {guard_commit} -- '{f}'",
                                        check=False).returncode != 0]
                if changed:
                    outcome["guard_edits_reverted"] += 1
                    self.exec(f"cd /work/repo && git checkout {guard_commit} -- " + " ".join(f"'{f}'" for f in changed))
            self.commit_leftovers(f"{task_id}: uncommitted work at the end of round {round_number}")
            if self.git("rev-parse", "HEAD") == started_at:
                feedback = "\nYour previous attempt changed nothing. Do the task.\n"
                continue
            passed, log = self.gate()
            if not passed:
                outcome["gate_failures"] += 1
                feedback = f"\nYour previous attempt failed the gate. Its output ends:\n\n```\n{log[-3000:]}\n```\n"
                continue
            review = self.review(prompt("review", change=self.change, task_id=task_id, task_text=text,
                                        task_base=task_base),
                                 EVAL_TASK=task_id, EVAL_ROUND=round_number)
            if outcome["first_review_accept"] is None:
                outcome["first_review_accept"] = review["verdict"] == "accept"
            if review["verdict"] == "accept":
                outcome["accepted"] = True
                break
            findings = "\n".join(f"- {f.get('severity', '?')}: {f.get('text', '')}" for f in review["findings"])
            feedback = f"\nA reviewer asked for changes to your previous attempt:\n\n{findings}\n"
        print(f"  task {task_id}: {'accepted' if outcome['accepted'] else 'failed'} in {outcome['rounds']} round(s)")
        return outcome

    # --- the whole run ---

    def execute(self):
        print(f"{self.id}: starting ({self.record['claude']} claude"
              + (", fake gate" if self.args.fake_gate else "") + ")")
        try:
            self.start()
            try:
                outcome = self.herd() if self.setup["kind"] == "herd" else self.single()
            except Budget:
                outcome = "budget"
                self.commit_leftovers("Uncommitted work when the run hit its cost limit")
            passed, _ = self.gate()
            self.record["final_gate"] = passed
            self.record["result"] = self.git("rev-parse", "HEAD")
            self.exec("cd /work/repo && git bundle create -q /eval-log/result.bundle HEAD")
            results = WORK / "results.git"
            if not results.exists():
                sh("git", "init", "-q", "--bare", str(results))
            sh("git", "-C", str(results), "fetch", "-q", str(self.dir / "log" / "result.bundle"),
               f"+HEAD:refs/heads/{self.id}")
        except Exception as error:
            outcome = "error"
            self.record["error"] = str(error)
            raise
        finally:
            self.finish(locals().get("outcome", "error"))

    def finish(self, outcome):
        gate_log = self.dir / "log" / "gate.jsonl"
        # The agent can write this log, so only its own calls are read from it, and a malformed line is skipped. The
        # runner counts its calls itself.
        agent_calls = 0
        for line in (gate_log.read_text(errors="replace").splitlines() if gate_log.exists() else []):
            try:
                agent_calls += json.loads(line).get("caller") == "agent"
            except (json.JSONDecodeError, AttributeError):
                pass
        proxy = sh("podman", "logs", "--since", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.started)),
                   PROXY, check=False)
        (self.dir / "proxy.log").write_text(proxy.stdout + proxy.stderr)
        tokens = {}
        for session in self.sessions:
            for key, value in (session["usage"] or {}).items():
                if isinstance(value, int):
                    tokens[key] = tokens.get(key, 0) + value
        self.record.update({
            "outcome": outcome, "wall_seconds": round(time.time() - self.started),
            "cost_usd": round(self.cost, 6), "tokens": tokens, "sessions": self.sessions,
            "gate_runs": {"total": self.gate_calls + agent_calls, "by_runner": self.gate_calls,
                          "by_agent_reported": agent_calls},
            "denied_hosts": sorted(set(re.findall(r"^DENY (\S+)", proxy.stdout, re.MULTILINE))),
        })
        (self.dir / "run.json").write_text(json.dumps(self.record, indent=2) + "\n")
        if not self.args.keep:
            self.stop()
        print(f"{self.id}: {outcome}, ${self.cost:.4f}, {self.record['wall_seconds']} s -> {self.dir}/run.json")


class Budget(Exception):
    pass


def warm(args):
    """Fill the cache volumes by running each base's Gradle gate once, writing to them directly. Runs mount them as
    overlays afterwards, so no run's writes reach another."""
    corpus = json.loads((EVAL / "corpus.json").read_text())
    for volume, path in CACHES.items():
        sh("podman", "volume", "create", "--ignore", volume)
        sh("podman", "run", "--rm", "--user", "root", "-v", f"{volume}:/cache", args.image, "chown", "1000:1000",
           "/cache")
    args.setup, args.rep, args.fake_gate, args.real, args.keep = "baseline", "warm", False, False, False
    for entry in corpus["changes"]:
        run = Run(args, entry)
        if run.dir.exists():
            shutil.rmtree(run.dir)
        print(f"warming the caches at {entry['change']}'s base")
        started = time.time()
        try:
            run.start()
            passed, log = run.gate()
            if not passed:
                print(log)
                raise RuntimeError(f"the gate fails at {entry['change']}'s base")
            print(f"  {round(time.time() - started)} s")
        finally:
            run.stop()
            shutil.rmtree(run.dir)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("setup", nargs="?", choices=SETUPS)
    parser.add_argument("change", nargs="?")
    parser.add_argument("--rep", default="1", help="repetition number, part of the run's name")
    parser.add_argument("--real", action="store_true", help="call the real Claude Code CLI (spends money)")
    parser.add_argument("--fake-gate", action="store_true", help="the gate passes at once: tests the plumbing")
    parser.add_argument("--max-cost", type=float, help="stop the run when its sessions have cost more (USD)")
    parser.add_argument("--keep", action="store_true", help="keep the container and volumes after the run")
    parser.add_argument("--force", action="store_true", help="replace an earlier run of the same name")
    parser.add_argument("--warm", action="store_true", help="fill the cache volumes at every base, then exit")
    parser.add_argument("--image", default="localhost/herd-eval-driving-log")
    parser.add_argument("--repo", default=os.path.expanduser("~/depot/driving-log"),
                        help="a Driving Log clone, for supplied files")
    args = parser.parse_args()

    if args.warm:
        warm(args)
        return 0
    if not args.setup or not args.change:
        parser.error("give a setup and a change, or --warm")
    if args.real and not os.environ.get("ANTHROPIC_API_KEY"):
        parser.error("--real needs ANTHROPIC_API_KEY in the environment")
    if args.real and not args.max_cost:
        # Agent shells can see the key, so calls they make themselves escape both the record and this limit. The hard
        # cap is the key's own spend limit at the provider (docs/evaluation-plan.md, Running it).
        parser.error("--real needs --max-cost, and a key with its own spend limit")
    corpus = json.loads((EVAL / "corpus.json").read_text())
    entry = next((e for e in corpus["changes"] if e["change"] == args.change), None)
    if not entry:
        parser.error(f"{args.change} isn't in eval/corpus.json's changes")
    run = Run(args, entry)
    if run.dir.exists():
        if not args.force:
            parser.error(f"{run.dir} exists: use --force to replace it")
        shutil.rmtree(run.dir)
        sh("git", "-C", str(WORK / "results.git"), "branch", "-D", run.id, check=False)
    run.execute()
    return 0


if __name__ == "__main__":
    sys.exit(main())
