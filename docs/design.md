# Herd: unattended proposal → implementation → review → PR pipeline

This is the **project-agnostic** design. Nothing in it names a project's language, build tool, test runner or
release process; everything that does lives in each project's `.herd/` manifest (below) and in that project's
workflow doc for its people (see What a project knows about the herd).

## Goal and division of labor

Human time is spent at three points only: writing/refining a proposal (interactive, as today), the project's final
approval step if it has one (e.g. running end-to-end tests by hand, see Final approval), and merging the resulting
PR. Everything else — implementing `tasks.md` item by item, reviewing each task, iterating on review feedback,
fixing final-approval failures, archiving the spec delta, and opening the PR — runs unattended. A proposal that
the pipeline can't finish on its own stops in a `needs-human` state (see Escalation) instead of looping.

- **Proposer**: a human with an interactive cloud SOTA agent. Unchanged, plus one step: marking the proposal ready
  (see The hand-off).
- **Implementer**: an LLM run non-interactively, one task at a time. Which model is host config per worker slot,
  local (Ollama or similar) or a cloud API, and one host can mix them (see Models).
- **Reviewer**: an LLM run non-interactively, by default cloud SOTA (Claude Code, `claude -p`), configured per worker
  slot like the implementer and never on the backend that wrote what it reviews (see Models). It reviews each task's
  commit and, once
  every task is accepted, the whole change holistically. Also triages the PR's review feedback into tasks (see
  Following up on PR review), and runs the archive step once final approval passes and review is done, since syncing
  spec deltas can need judgment.
- **Orchestrator**: plain code (no LLM, no LLM API key), owns the queue and git/GitHub plumbing — assigns work,
  creates/tears down working copies, starts worker containers, derives task/review state, pushes, opens and
  updates PRs. The orchestrator opens the PR and marks it ready, so it also watches the PR for review feedback until
  merge, and delegates each round of fixes to the workers (see Following up on PR review).

## What is generic and what is per project

One herd instance runs per host and serves every **registered project**. The split:

| Generic (the herd repo) | Per project (`.herd/` in the project's repo) |
|---|---|
| Orchestrator, state machine, queue, crash recovery | Toolchain image: what a worker needs to build and test (`toolchain.Dockerfile`) |
| Role layers: implementer harness, `claude`, `git`, `openspec` | Gate: the commands that must pass before a commit is accepted, and what the tamper guard protects |
| Role system prompts | Optional prompt additions per role (appended, never replacing) |
| Planner skills (`herd-propose`, `herd-ready`, `herd-resolve`), installed by `herd init` | A short workflow doc for the project's people: what's specific to them (see What a project knows) |
| Commit validation, push, PR lifecycle, update-branch | Network egress beyond the model endpoint (package registries) |
| Escalation markers, inputs request/provide cycle | Cache volumes (e.g. a build tool's dependency cache) |
| herdr bridge, event log, `herd` CLI | Final approval: `none`, or `human` with instructions |
| Model backends and secrets (host config) | Capabilities the toolchain lacks (tasks needing them escalate) |
| | Cap overrides; whether the reviewer writes release notes into the PR body |

The project's CI workflows, release automation and branch-protection choices are **not** part of the herd. The
herd only *requires* some of them to exist (see Requirements on a project) and `herd doctor` checks that they do.

**Workflow: OpenSpec only, for now.** The state machine reads OpenSpec's files (`.openspec.yaml`, `tasks.md`) and
runs its archive. "Project-agnostic" means any project that uses OpenSpec, regardless of language or platform. Keep
the OpenSpec-specific reads behind one module in the orchestrator (list changes, read the ready flag, read/write
task state, archive) so a second workflow is a new module, but don't build that abstraction further until a
project needs it.

## The project manifest

`.herd/project.yaml`, committed to the project's default branch. Example shape (a real one, with comments
explaining its values, is Driving Log's `.herd/project.yaml`):

```yaml
workflow: openspec
branch_prefix: change/
toolchain:
  dockerfile: .herd/toolchain.Dockerfile     # Debian/Ubuntu-based; the herd adds its role layer on top
  caches: [/home/agent/.gradle]              # per-project, per-role volumes
  egress: [repo.maven.apache.org, maven.google.com, dl.google.com, plugins.gradle.org, services.gradle.org]
gate:                                         # implementer runs it before committing; reviewer re-runs it
  - ./gradlew check
  - openspec validate --all --strict
guarded:                                      # the tamper guard (see Who commits, who pushes)
  tests: ["**/src/*Test/**"]                  # deleting or emptying one needs a declared reason
  skip_markers: ["@Ignore", "@Disabled"]      # adding one needs a declared reason
  paths: [config/detekt/baseline.xml]         # e.g. lint baselines: any change needs a declared reason
final_approval:
  kind: human                                 # or: none
  instructions: |                             # shown in the herd's status pane and in the draft PR body
    Run the end-to-end suite for the areas this change touches; record pass/fail in review-notes.md.
missing_capabilities:                         # a task that needs one of these escalates instead of being attempted
  - macOS / Xcode
caps: { review_rounds: 3, added_tasks: 3, gate_fixes: 3, pr_review_rounds: 5 }
prompts:                                      # optional, appended to the generic role prompts
  implementer: .herd/implementer.md
  reviewer: .herd/reviewer.md
release_notes: true                           # reviewer writes a "Release notes" section into the PR body
pr_review:                                    # see Following up on PR review
  wait_for: [copilot-pull-request-reviewer]   # automated reviewers whose review of the current tip is awaited
  timeout: 15m                                # after this, a missing review is treated as none, shown in the status pane
```

**Defaults** for what a manifest leaves out: `caps` as in the example (3, 3, 3 and 5) and `pr_review.timeout: 15m`.
They come from the first project's PRs before the herd existed: one PR took five Copilot rounds to come clean,
which sets `pr_review_rounds`, and Copilot re-reviewed about 2 to 4 minutes after each push, which a 15-minute
timeout covers with room for a slow round. The other caps are still guesses (see Open questions).

Project knowledge the agents need (conventions, where things live, test strategy) comes from the project's own
`CLAUDE.md`/`AGENTS.md` and `openspec/config.yaml`, which the harness reads like any interactive session would — the
manifest doesn't duplicate it.

**The manifest and toolchain are read from the default branch, never from a change branch.** Otherwise an agent
could widen its own egress or weaken its own gate. Commit validation also rejects any worker commit that touches
`.herd/`. Changing the manifest is an ordinary human PR.

## Requirements on a project

- Uses OpenSpec, with changes in `openspec/changes/`.
- Hosted on GitHub, with branch protection on the default branch requiring PRs, required CI status checks, and
  up-to-date branches (see Keeping up with the default branch). The CI checks should run the same gate as the
  manifest, as an independent second net.
- The gate runs headless in a Linux container. Anything that can't (a Mac, a device, a GUI emulator) is either
  the project's human final-approval step or a `missing_capabilities` entry.
- The default branch is **PR-only for everyone**, with no bypass. Nothing needs one: a proposal lives on its own
  branch from its first commit and reaches the default branch only as the one merge of its PR (see The hand-off).

## What a project knows about the herd

A product project knows as little as possible about how the herd works. All of the design (states, review loop,
recovery, containers) lives only here. A project carries:

- **`.herd/project.yaml` and `.herd/toolchain.Dockerfile`**: configuration, with the reasoning for its values
  in comments. It isn't documentation of the herd.
- **The planner skills**, installed and updated by `herd init` into the project's agent config (for Claude Code,
  `.claude/skills/`), the same way `openspec init` installs its skills. They are vendored and herd-managed, so a
  project doesn't edit them; project-specific rules go in the project's workflow doc, which the skills read.
  Herd is MIT-licensed so the vendored skills fit any project; `herd init` writes the license next to them
  (`.claude/HERD-LICENSE`), as OpenSpec does with its own.
  - `herd-propose <change>`: starts a proposal the herd's way. It creates `<branch_prefix><change>` from the default
    branch (in a worktree, so the person's main checkout is untouched), runs the project's propose workflow there,
    commits, pushes, and opens a **draft PR**, where the proposal is reviewed. Before proposing, it fetches the other
    change branches and lists what's in flight, so the planner can spot overlaps and record dependencies, and it
    removes the worktrees of merged changes (Cleaning up after merge).
  - `herd-ready <change>`: checks a proposal against Writing proposals for the herd (below) and the manifest's
    `missing_capabilities`, then sets `ready: true` in the change's `.openspec.yaml` **on its branch**, commits and
    pushes.
  - `herd-resolve <change>`: shows why a change is waiting on a human, helps fix it on the change branch, and
    commits the resolution: a resolved `needs-human` marker, or a final-approval pass/fail.
- **A short workflow doc for the project's people and planner agents.** It covers only what's specific to that
  project: its extra rules for herd-ready tasks, how to run its final approval, and what not to touch once a change
  is ready. For everything else it points to `using-the-herd.md` (the generic guide for people), never repeating
  it. The project's `CLAUDE.md`/`AGENTS.md` points to it.

## Writing proposals for the herd

These are the generic rules `herd-ready` checks. A project's workflow doc adds its own.

- **The proposal validates** (`openspec validate <change> --strict`) and is complete. The herd implements what's
  written and can't ask clarifying questions, apart from stopping at `needs-human`.
- **Each `tasks.md` item is one reviewable commit**: one coherent step that leaves the gate green, small enough for
  one review. Split items that aren't; merge items that can't pass the gate on their own.
- **Sections are in dependency order.** Tasks run strictly in sequence (see Concurrency model).
- **Nothing needs a `missing_capabilities` entry**, and no task *runs* the final-approval checks. Tasks may write or
  update those tests; running them is the human's final approval.
- **Outside content is committed with the proposal** (test fixtures, sample files) where it can be, so the herd
  doesn't stop and ask for it (see Outside content).
- **No task needs a secret.**

## Onboarding a project

1. `herd init` in the project's checkout. It writes `.herd/` with detected defaults and installs the planner
   skills. Review the files and land them by PR.
2. Add a CI workflow that runs the gate on every PR. Make it a required status check.
3. **(manual, repo admin)** Branch protection on the default branch: PRs required for everyone (no bypass), the CI
   check required, branches up to date before merging.
4. **Move proposals already on the default branch onto change branches**: one commit removes them from the
   default branch, and each gets `<branch_prefix><change>`, branched from that commit, holding only its own
   proposal. After that, the default branch's `openspec/changes/` holds only `archive/`.
5. Write the project's workflow doc (see What a project knows), and point `CLAUDE.md`/`AGENTS.md` to it.
6. `herd doctor <project>` until it passes.
7. **(manual)** Smoke test: run one small, low-risk proposal through the whole pipeline while watching the `herd`
   session, including one deliberate review rejection and one final-approval failure fed back, before trusting it
   with a real queue.

## Registering projects

The herd can start observing a new project at any time, without restarting anything.

- **Registering.** `herd init` registers the project it onboards. `herd add <repo-url>` registers a project that
  already has `.herd/` (for example, on a second host). Both write `~/.config/herd/config.yaml` and poke the
  orchestrator. The orchestrator gets that file **read-only**, so registering stays a host-side, human action that
  no agent or orchestrator bug can widen.
- **Active is derived, not remembered.** On each scan, a registered project is *active* when its default branch has
  a `.herd/project.yaml` that parses, the orchestrator's GitHub token can push to the repo, and the project's
  toolchain image builds. Otherwise it's *inactive*, and the status pane says which check failed. Nothing records
  that `herd doctor` passed. `doctor` is the human's deeper check (gate in a real worker, branch protection, model
  backend) to run before trusting a project, not a switch the orchestrator reads.
- **First scan of a new project.** The orchestrator creates the project's bare mirror and builds its images. It
  then treats the project like any other: it queues change branches already marked `ready: true`, and leaves the
  others alone. The herdr bridge adds the project's workspace on its next pass.
- **GitHub access is the manual step.** The orchestrator's token is a fine-grained token limited to specific
  repos, so a new project must be added to the token's repository list (or a new token issued). `herd add`/`init`
  check this and say so when it's missing. Until it's done, the project stays inactive with "token can't push".
- **Pausing and removing.** `herd pause <project>` / `herd resume <project>` set `paused` in host config. A paused
  project gets no new work; a unit already running finishes and pushes. `herd remove <project>` unregisters it:
  running units for it are stopped (their work is discarded like any crashed attempt), and its mirror, caches and
  images are deleted. Its branches, PRs and `review-notes.md` stay on GitHub, so registering it again resumes
  exactly where it stopped.
- **Why not discover projects automatically** (e.g. every repo the token can see that has `.herd/`)? Because the
  token's repo list already has to be edited by hand. Discovery would save one command and turn "the token can
  see this repo" into "the herd will push to this repo", which should stay an explicit choice.

## The hand-off: when a proposal enters the queue

**A change lives on its own branch for its whole life**, `<branch_prefix><name>` (`change/<name>`): proposal,
refinement, implementation, review, final approval and archive. The default branch never holds a change in progress.
It gets the change, code and archive together, as the one merge of the change's PR. So the default branch's
`openspec/changes/` holds only `archive/`, and its specs describe only what is built.

- **Proposing.** `herd-propose` (or a person by hand) creates the branch, commits the proposal there, pushes it, and
  opens a draft PR. Refining the proposal is more commits on the branch; the draft PR is where it's discussed.
- **The hand-off** is an explicit `ready: true` field in the change's `.openspec.yaml`, **committed on the change
  branch** (by `herd-ready`). A pushed branch without it is still being written, and the herd leaves it alone. The
  intake scan lists the remote's `<branch_prefix>*` branches and reads that one field on each. Like everything else
  here, it's read from git, not remembered.
- **Dependencies.** `depends_on: [<change>, ...]` in `.openspec.yaml` names changes that must be merged first. A
  ready change whose dependencies aren't all archived on the default branch waits (state *waiting-on-dependency*).
  When they are, its first unit of work merges the default branch in (see Keeping up with the default branch).
  Change branches never stack on each other.
- **Pausing.** Setting `ready: false` (or removing the field) on the branch stops the herd after the unit it's
  running, so a person can revise the change. Setting it back resumes from whatever the files then say.
- **A person pushing while the herd works.** The orchestrator's next push to that branch is rejected as
  non-fast-forward. It discards that unit, as it would a crashed one, and the next scan derives the change's state
  from the new tip. Git enforces this, so nothing a person pushes can be silently overwritten.

## Concurrency model

- A proposal's tasks run **strictly sequentially** — `tasks.md`'s sections are a dependency chain in practice
  (storage → UI → cross-cutting logic → end-to-end tests → verification). Building scheduling for intra-proposal
  parallelism isn't worth it for v1.
- **Multiple proposals run concurrently**, across and within projects, each on its own branch
  (`<branch_prefix><name>`), each bound to at most one active worker at a time. The host's worker slots (see
  Models) cap the total; projects share them round-robin, so one project's backlog can't starve another.
- Each unit of work (one task's implementation, one round of addressing review feedback, or one review) gets a
  **fresh, ephemeral clone** of the proposal branch's current tip, taken from that project's local bare mirror (see
  Containers), handed to a new worker container, and deleted when that unit of work ends. The branch is the
  persistent state; the clone is disposable — this is what lets review feedback be picked up by a different
  worker than the one who wrote the original code.
- **Restarting a task always starts clean, never resumes.** If a task needs to be restarted — orchestrator crash,
  worker crash, a hung worker getting killed on timeout — whatever partial work existed in that attempt's clone
  is discarded outright: not diffed, not merged, not used as a starting point. The next attempt gets a brand-new
  clone of the branch's current (last-pushed) tip, exactly as if the task were starting for the first time. This
  is a deliberate rule, not just a side effect of clones being ephemeral — the temptation to "resume the
  half-finished diff instead of wasting the compute already spent" needs to be ruled out explicitly, since a
  resumed partial edit from a crashed/killed worker is exactly the kind of unverified state this design otherwise
  avoids by treating every attempt as a clean, reproducible function of (branch tip, task, review notes).
- **Commits are additive, never amended, rebased or force-pushed**, so the commit history is an honest review
  trail (implement → review → revise → review → accept) worth surfacing in the final PR body.
- The reviewer pins its verdict to the exact commit SHA it evaluated (recorded in a `review-notes.md` sibling to
  `tasks.md`), so a verdict never becomes ambiguous if the branch moves under it. Because history is never
  rewritten, these pins stay valid for the life of the branch.

## Who commits, who pushes

**Workers commit; only the orchestrator pushes.** A worker (implementer or reviewer) makes exactly one commit in
its clone per unit of work — so commit messages are written by the agent that knows what changed — and writes a
status file on exit. The orchestrator then validates the commit before pushing it:
- exactly one new commit on top of the tip the unit started from;
- the commit contains the expected state transition (see below) and nothing outside the change's scope (e.g. an
  implementer commit must not touch `review-notes.md` or flip a checkbox to `[x]`; no worker commit may touch
  `.herd/`);
- the status file agrees with the commit;
- **the tamper guard**: the commit doesn't weaken the safety net silently. Deleting or emptying a file matching
  the manifest's `guarded.tests`, adding one of its `guarded.skip_markers`, or changing a `guarded.paths` file (a
  lint baseline, say) must each be declared, with a reason, under a `Guarded:` section of the commit message. The
  task review must then accept or reject each declared item by name, and validation of the verdict commit checks
  that it does. An undeclared one fails validation before any review is spent. Legitimate cases (removing a
  feature removes its tests) still pass, but never silently. Assertions weakened into tautologies need judgment,
  so catching them stays with the reviewer. (The idea comes from no_human, see Prior art.)

A commit that fails validation is discarded like any crashed attempt. Worker containers never hold GitHub
credentials; the orchestrator's GitHub token is the only push-capable credential in the system.

## Communication and the work queue: git + files, no message bus, no separate durable store

No queue service, no database. The queue is **derived from git on every orchestrator start**, not persisted
independently:
- `.openspec.yaml`'s `ready: true` on each change branch is the intake queue, and its `depends_on` the ordering
  (see The hand-off).
- `tasks.md` checkbox state (`[ ]` / `[r]` awaiting review / `[x]` accepted) is the task queue itself. `[r]` is
  OpenSpec-compatible: its parser counts only `x` as done, so `openspec list` and `openspec archive` treat `[r]` as
  still open, which is correct.
- `review-notes.md` (new, per change) carries reviewer feedback back to whichever worker picks up the task next,
  the final-approval result, and any `needs-human` marker. The implement→reject→revise sequence visible in the
  branch's commit history is how many review rounds a task has had — not a separate counter.
- Whether a proposal's PR exists, and whether it's still a draft, is answered by asking GitHub
  (`gh pr list --head <branch> --json number,isDraft`), not remembered.
- The set of registered projects is host **config** (`~/.config/herd/config.yaml`), not state: it's written by
  the `herd` CLI, never by the orchestrator, and re-read on every scan (see Registering projects).

The only state that lives purely in the orchestrator process's memory — and is allowed to vanish on crash — is the
*current assignment lock* (which worker container currently holds which proposal), whose only job is to stop two
workers racing on the same branch while the process is alive.

**The scan loop.** The orchestrator doesn't keep a work list between passes. It runs the same full scan on start
and then repeatedly: every few minutes (`scan_interval` in host config), and right away when the `herd` CLI pokes
it after changing the config or recording a human action. Each scan re-reads the host config, fetches every
registered project's bare mirror, derives each proposal's state, and dispatches work to free worker slots. A
project, a proposal or a human fix that appeared since the last pass is picked up by the next one with no restart,
because nothing is remembered between passes to go stale. Crash recovery (below) is just the first scan.

**Crash recovery** (orchestrator container restart or a full host reboot look identical from here, given
its systemd unit's `Restart=always` and lingering, see Containers): on start, the orchestrator, for every
registered project, (1) lists every change branch without a merged PR, (2) reads each one's `.openspec.yaml` and
`tasks.md`/`review-notes.md` to compute its exact next action from scratch — no assumption carried over from
before the crash, (3) reconciles against running worker containers (kill any orphaned worker rather than adopt
it — its state is suspect) and `gh pr list` (don't open a second PR for a branch that already has one), (4)
re-enqueues and resumes. The cost of a crash is bounded to whatever unpushed work was sitting in a worker's
ephemeral clone — redone from the last pushed commit (and, per the rule above, that redo never reuses the discarded
partial work) — which is cheap specifically because workers are stateless and task granularity is small (one
`tasks.md` item at a time).

**The guarantee that a started-but-unfinished task can never be missed on restart** — not just redone, but never
silently dropped — comes from two rules together, not from the scan alone:
- Every state transition is a **single commit, pushed as a unit**. An implementer's "done" flips `tasks.md`'s
  checkbox to `[r]` *in the same commit* as the code change; a reviewer's verdict flips it to `[x]` (or back to
  `[ ]` with `review-notes.md` updated) *in the same commit* as writing its notes. A commit that never got pushed
  doesn't exist as far as recovery is concerned — it was in an ephemeral clone and is discarded. This rules out a
  4th, invisible in-between state — e.g. code pushed but still marked `[ ]`, which would make a restart redispatch
  it as unstarted and duplicate work on top of what's already there, instead of cleanly treating it as awaiting
  review.
- The state space is **exhaustive and structurally derived**, both per-task and per-proposal, so recovery is a
  total function of current facts, never a remembered checkpoint. Per task: `[ ]` / `[r]` / `[x]`, nothing else
  possible. Per proposal, checked in this order (first match wins):
  1. *needs-human* — `review-notes.md` on the branch carries an unresolved `needs-human` marker (see Escalation).
     Next action: none; shown in the herd's status pane until a human resolves it.
  2. *drafting* — no `ready: true` in the change's `.openspec.yaml` on its branch. Not queued; shown in the status
     pane as in flight, so planners and people can see it.
  3. *waiting-on-dependency* — ready, but a change in its `depends_on` isn't archived on the default branch yet.
     Next action: none until it is.
  4. *implementing* — ready, dependencies merged, ≥1 task not `[x]`. This includes tasks appended after a final-approval
     failure, even when a draft PR already exists.
  5. *holistic-review-pending* — every task `[x]`, no holistic-accept recorded in `review-notes.md` for the
     current tip, change not archived.
  6. *awaiting-approval* — holistic review accepted, `final_approval.kind: human`, no pass recorded, change not
     archived. Next action: ensure a **draft** PR exists; the human runs the project's final approval. Skipped
     entirely when `final_approval.kind: none`.
  7. *in-review* — holistic review accepted and (if required) a final-approval pass recorded, change not archived, and
     review isn't done: the PR has unresolved review threads, a review requesting changes, a review finding not yet
     triaged (including ones in a review's summary, which have no thread), or an awaited reviewer (`pr_review.wait_for`)
     hasn't reviewed the current tip yet. Next action: mark the PR ready for review if it's still a draft, then follow
     up as Following up on PR review describes. A triaged finding becomes a task under "(added during review)", which
     sends the change back to *implementing*. Review comes before archiving, because a fix after the archive would
     mean editing the synced main specs by hand.
  8. *archiving* — holistic review accepted, (if required) a final-approval pass recorded in `review-notes.md`, review
     done (no open thread or untriaged finding, and every awaited reviewer has reviewed the tip), change not yet
     archived on the branch. Next action: the reviewer runs the archive and
     commits. A crash mid-archive never gets pushed, so it's discarded with the clone and redone, same as any
     other unit of work.
  9. *ready-to-merge* — archive commit pushed and its checks passing. Next action: none; a human merges.

  Because every branch is always in exactly one of these states and each has a defined next action, a full scan
  over all open branches cannot skip anything — there's nothing outside the enum for a task or proposal to
  silently fall into.

This forces every multi-step git/GitHub operation (push, open-PR, mark-ready, update-branch) to be written as
check-before-act/idempotent rather than blindly re-run — needed anyway for ordinary network failures, so crash
recovery exercises the same code path rather than a separate one. It also means recovery must always be a **full**
re-scan of every open branch, never a delta/"resume from last known position" scheme — a remembered checkpoint is
itself exactly the kind of state that could be lost in the same crash, which would reintroduce the missed-task risk
this design is meant to rule out. Deliberately not reaching for Redis/SQLite/a job queue here: a second source of
truth that can drift from git's would undercut the property that git is the only infrastructure dependency this
whole design has. Revisit only if rescanning branches on startup becomes slow with many projects and concurrent
proposals.

## Adding tasks mid-apply

Changes sometimes grow tasks during apply, so the pipeline allows it, narrowly:
- **Only the reviewer appends tasks**, as part of a verdict commit, under a section marked "(added during apply)".
  The implementer can only *request* one, via its status file; the reviewer decides on its next pass.
- Tasks fixing final-approval failures are appended the same way, under "(added during final approval)", and so are
  tasks for GitHub review comments, under "(added during review)" (see *in-review*).
- Added tasks count toward the per-proposal cap (`caps.added_tasks`); appending past it escalates to
  `needs-human` instead, since that much scope drift means the proposal itself needs revisiting.
- Tasks added during review are bounded by `caps.pr_review_rounds` instead of `caps.added_tasks`: one review round
  can raise several findings, and a finding fixed by class is still one task, so the count of rounds is what shows
  a change isn't converging.

## Escalation: the `needs-human` state

A proposal stops and waits for a human when any of these happens:
- a single task reaches the review-round cap (`caps.review_rounds`);
- the added-tasks cap is exceeded (above);
- PR review runs past `caps.pr_review_rounds` rounds without coming clean, or raises a finding the reviewer can't
  map to a task (see Following up on PR review);
- merging the default branch into the change branch conflicts (see Keeping up with the default branch);
- the gate keeps failing after `caps.gate_fixes` fix attempts;
- the holistic review rejects with feedback that can't be mapped to a specific task;
- a task needs something in the manifest's `missing_capabilities`, or anything else the pipeline doesn't have (a
  device, a credential), or content from outside the project (see Outside content).

The marker is a committed line in `review-notes.md` (`needs-human: <reason>`, pinned to a SHA like any verdict),
so the state is derived from git like everything else. The human resolves it by fixing whatever's wrong (editing
`tasks.md`, resolving the conflict, revising the proposal) and committing the marker as resolved on the branch
(`herd-resolve` does the bookkeeping); the next scan picks the proposal back up from whatever state its files now
describe.

## Final approval

The gate runs inside the workers. Some checks don't fit there — typically end-to-end tests that need an emulator,
a device or a GUI — and suit a human final check better. A project opts in with `final_approval.kind: human`.
Tasks that *write* those tests are still implemented and reviewed like any other task; only *running* them is
deferred.

When a proposal reaches *awaiting-approval*, the orchestrator makes sure its **draft** PR exists (normally opened by
`herd-propose` at proposal time) and puts in its body the
manifest's `final_approval.instructions`. The human checks the branch out, follows them, and records the result in
`review-notes.md`, pinned to the SHA tested (`herd-resolve` writes the entry):
- **pass** → the proposal moves to *in-review* (GitHub's review of the PR), and from there to *archiving* once no
  review thread is open;
- **fail** → the human writes the failure as the note; the reviewer turns it into appended task(s) under "(added
  during final approval)" and the proposal goes back to *implementing*. The draft PR stays open throughout and
  simply gets more commits.

Archiving happens only after the pass and the review, deliberately: `openspec archive` syncs the spec deltas and
moves the change directory, so feeding failures or review feedback back as new tasks after an archive would mean
un-archiving.

## Following up on PR review

The orchestrator opens the change's PR and marks it ready for review, so it is also the one that watches the PR until
it is merged and follows up on what reviewers say. It runs no model, so it doesn't judge feedback. It collects
feedback, hands it to the reviewer to triage, has implementers fix it, and posts the answers. Every step is a unit of
work like any other, derived from git and GitHub on each scan, never remembered.

1. **Watch.** Every scan, for each PR in *in-review* or later and not yet merged, the orchestrator reads:
   - the review threads (`reviewThreads` over GraphQL, with `isResolved`), every page of them: a check that reads only
     the first page can miss an open thread;
   - the reviews, bodies included: an automated reviewer often puts findings in its summary, with no thread (for
     Copilot, "Previously missed" and "In code that hasn't changed since last review");
   - the PR's conversation comments;
   - which commit each review covers (`commit_id`).

   Automated reviewers review again after every push, a few minutes later. So review is done only when every
   reviewer in `pr_review.wait_for` has reviewed the **current tip**, and that review leaves nothing open. Replies the
   herd itself posted don't count as reviews; filter by author and commit, not by the number of reviews.
2. **Triage (reviewer).** New findings go to a reviewer unit together with the change, so the reviewer can check each
   claim against the code and the upstream sources it names. For each finding it decides one of:
   - *fix*: it appends a task under "(added during review)". Where the finding is one instance of a class (a missed
     file in an inventory, a wrong copyright line), the task covers the whole class, checked across the change, so
     the next review round doesn't find the next instance;
   - *no change*: it writes the reason.

   The decision goes in `review-notes.md`, keyed by the finding's id (thread id, or review id plus index for a
   finding in a summary), so a finding is triaged exactly once.
3. **Fix (implementers).** The added tasks go through *implementing* like any other task, task review included. A
   fix that changes behavior also updates the change's spec deltas, design and tasks.
4. **Answer (orchestrator).** Once a finding's task is accepted, or its no-change reason recorded, the orchestrator
   posts the reply the reviewer wrote, naming the fixing commit, and resolves the thread. A finding with no thread is
   answered in one PR comment per review round. Workers hold no GitHub credentials, so replies are always posted by
   the orchestrator, from text in `review-notes.md`.
5. **Repeat.** The push of the fixes triggers the next round. A change past `caps.pr_review_rounds` rounds without coming
   clean, or a finding the reviewer can't map to a task, escalates to `needs-human`.

Feedback from a person is handled the same way. A request to change the proposal's scope rather than its
implementation is escalated, not triaged: scope is the proposer's call.

## Keeping up with the default branch

No rebasing (that would rewrite SHAs and break the reviewer's pins and the additive-history rule). Instead,
branch protection requires PR branches to be **up to date** before merging, and the orchestrator uses GitHub's
update-branch (`gh api -X PUT repos/<owner>/<repo>/pulls/<n>/update-branch`, a merge commit) when the PR is
behind. That merge triggers the CI checks again, which is where two concurrent proposals that both touched a shared
file (a dependency catalog, `CLAUDE.md`, a shared spec) actually collide — per-task testing alone won't catch that.
A conflicting update-branch escalates to `needs-human`; a CI failure after the update is treated like a failed
gate (fix task, then escalate past the cap). A final-approval pass recorded before an update-branch merge is not
invalidated by it — CI re-runs the gate, and the human re-runs final approval at their discretion.

## Cleaning up after merge

A worktree or branch that no longer has a use is removed, so in-flight work stays easy to read: the change branches
are the queue, and a stale one looks like work. Within the herd, the per-unit clones are already deleted after each
unit (Containers), so what lingers is a change's branch and a planner's worktree.

- **Once a change's PR is merged**, the orchestrator deletes its change branch on GitHub (unless the repository
  already deletes head branches on merge) and prunes it from the bare mirror, on the scan after the merge. A merged
  branch has no state in the enum, so nothing would ever pick it up again.
- **A PR closed without merging** keeps its branch: it may be reopened or reworked. The branch is removed only when
  a person says the change is abandoned (`herd-resolve`), never because of the close alone.
- **Planner worktrees** live on the person's machine, which the orchestrator can't reach (it has no host mounts).
  `herd-propose` made them, so the planner skills clean them up. Each run of `herd-propose` and `herd-resolve`
  first lists the project's worktrees whose branch has a merged PR whose head commit (`headRefOid`) is exactly the
  local branch's tip, and removes them with their local branches. A remote branch being gone isn't enough (it's
  deleted on every merge), and neither is `git branch --merged` (it doesn't recognize squash merges); a local tip
  that differs from the merged head has a commit the PR never got. It skips a worktree with uncommitted or untracked changes
  (gitignored local files such as SDK paths and config are fine to drop) and asks about it. The branch is deleted
  with `git branch -D`, not `-d`: a squash merge leaves the branch's own commits off the default branch, so `-d`
  refuses even after the check above passed. Then `git fetch --prune` drops the deleted remote branch.
- **Nothing with unmerged commits is deleted** without a person's say-so: a branch is removed only when it is merged
  or a person abandoned it, and a worktree only when its branch is.

## Models

Which model runs a unit is the operator's cost and privacy decision, so it's host config
(`~/.config/herd/config.yaml`), never the project manifest. Named **backends** say what a model is; **worker slots**
say which backend runs which kind of unit:

```yaml
backends:
  opus:        { kind: anthropic, model: claude-opus-5-5,   secret: ANTHROPIC_API_KEY }
  sonnet:      { kind: anthropic, model: claude-sonnet-5-5, secret: ANTHROPIC_API_KEY }
  local-coder: { kind: ollama, model: <coder model>, endpoint: http://ollama:11434 }
slots:                                 # each slot runs one unit at a time
  - name: gpu
    implementer: local-coder
    reviewer: { archive: local-coder }  # only the unit kinds listed run here
  - name: cloud-1
    implementer: sonnet
    reviewer: { task: sonnet, holistic: opus, triage: opus, archive: sonnet }
  - name: cloud-2
    implementer: sonnet
    reviewer: opus                      # one backend for every reviewer unit kind
projects:
  some-project: { slots: [gpu] }        # optional: e.g. code that must not leave the host
```

A role maps to one backend, or to one per **unit kind**. The implementer has one kind (`implement`). The reviewer
has four that need very different judgment: `task` (one task's commit), `holistic` (the whole change, plus release
notes), `triage` (PR review findings and final-approval failures into tasks) and `archive` (mostly running
`openspec archive`). A slot that doesn't list a role or kind never runs it.

- **The slots are the capacity.** The scheduler gives each unit to a free slot that can run its kind for its project,
  round-robin across projects. A local slot is one unit at a time on the host's GPU; cloud slots bound spend.
- **Any capable slot can take any unit.** Every unit starts from a fresh clone, so a task implemented on one slot can
  be revised or reviewed on another.
- **A task review never runs on the backend that wrote the commit.** The same model shares its own blind spots.
  When no slot qualifies, the status pane shows it as a configuration problem; it isn't a `needs-human` stop.
- **A stronger attempt before a human.** A task's last allowed round under `caps.review_rounds` goes to a slot with
  a different implementer backend, when one exists, before the task escalates.
- **Each worker commit records its backend and unit kind** in trailers (`Herd-Backend: local-coder`,
  `Herd-Unit: implement`), and commit validation checks them against the slot. How often each backend's work is
  accepted comes straight from git history, which is how to judge a local model against a cloud one: replay tasks
  the herd has already accepted on the candidate and compare. There's no separate metrics store.
- **A container gets only its backend's settings.** Endpoint and model name as env, and the secret only if the
  backend names one; a local slot's workers never see an API key. Egress is that backend's endpoint plus the
  manifest's list.
- **The harness follows the backend kind.** The implementer harness serves every kind. The reviewer runs `claude -p`
  on `anthropic` backends and the implementer harness with the review prompt otherwise; both produce the same
  structured verdict.
- `herd doctor` checks that every backend answers, and warns when `holistic` or `triage` runs on a local backend.

Switching a slot between local and cloud, or adding a slot, is a config change: the scan re-reads host config, and a
running unit finishes on the backend it started with.

## Containers

**Runtime: rootless Podman.** Everything runs as an ordinary user's containers, with no root daemon. The
orchestrator is a container defined by a Quadlet unit (`herd-orchestrator.container`), run by the user's systemd
with `Restart=always`; lingering (`loginctl enable-linger`) starts it at boot without anyone logged in. The herdr
session that displays it runs on the host (see Launching and watching the herd). Workers are **not** units: the
orchestrator starts one container per unit of work (it has to, to mount that unit's clone), through the Podman API,
from a per-project, per-role image:

- **Toolchain image** — built from the project's `.herd/toolchain.Dockerfile` (read from the default branch),
  tagged with the hash of that file, rebuilt when it changes. Contains the language runtimes and SDKs the gate
  needs. Must be Debian/Ubuntu-based so the role layer can install onto it.
- **Role layer** — generic, from the herd repo, applied with `FROM <toolchain image>`:
  - *implementer*: the implementer harness, `git`, the `openspec` CLI (gates typically run `openspec validate`),
    and the generic `SYSTEM_PROMPT.md` plus the project's optional addition. Gets its clone as a volume; reads its
    task from an env var/arg; runs the gate; commits; writes a status file on exit. The harness must serve a local
    (Ollama-served) model and cloud APIs behind one config and run headless; the specific tool is secondary. Two
    candidates, compared on the same tasks at Build plan step 3:
    - [Aider](https://aider.chat): a thin edit loop with a `--model` config.
    - [OpenHands](https://docs.openhands.dev) headless: a fuller agent loop (tool use, running tests, correcting
      itself), any model through LiteLLM. It normally starts its own sandbox container; in a herd worker it must
      run in-process instead, since the worker is the sandbox and has no Podman socket. With Ollama it needs a
      context of at least 22k tokens, which narrows the local models a small GPU can serve.
  - *reviewer*: `claude -p` (and the implementer harness, for non-Anthropic backends) with the review system
    prompt (structured accept/revise output), `git`, and the `openspec` CLI for the archive. Re-runs the gate
    itself rather than trusting the implementer's claim.
- **Local model server** — when a backend is local: Ollama (or similar) as its own container on the workers'
  internal network, with no egress of its own (the operator pulls models). It's the only container given the GPU,
  however the vendor exposes it to rootless Podman: an Intel or AMD card as `--device /dev/dri` (the user in the
  `render` group), an NVIDIA card through CDI (`nvidia-ctk cdi generate`, then `--device nvidia.com/gpu=all`).
- **Orchestrator** — generic image: bare-mirror and clone lifecycle, queue, image builds, worker container
  lifecycle, commit validation, push, `gh pr create`/update-branch/mark-ready. Needs a GitHub token (scoped to the
  registered repos), the user's rootless Podman API socket, and the host config read-only; no LLM key. It's the one
  privileged component, which is acceptable because it runs no model and no project code. Rootless, the socket is
  worth the user's account, not root: it could still start a container that mounts the user's home (see Open
  questions).

**Repo access: a local bare mirror per project, one fresh clone per unit of work.** The orchestrator keeps a bare
mirror of each registered repo in a volume (fetched before each assignment). For each unit of work it clones
the proposal branch from the mirror into a per-unit volume, mounts that into the worker, and after the worker exits
fetches the worker's commit back from the clone, validates it, pushes it to GitHub, and deletes the clone. This is
used instead of `git worktree` on a bind-mounted host checkout because worktrees record absolute `gitdir` paths
(which break across container mount points) and share one `.git` whose locks concurrent workers would contend on.
It also means nothing changes if workers ever run on more than one machine — they'd clone from the mirror over the
network instead.

**Least privilege for worker containers** (implementer and reviewer):
- Each image contains **exactly** what the role needs: the project's toolchain plus the role layer, nothing else.
  No `gh`, no `podman`, no `ssh`/`curl`-style network tools, no package managers at runtime, no general-purpose
  extras "just in case". Adding a tool is a reviewed change to the toolchain Dockerfile (project) or the role
  layer (herd), not something an agent can do itself — and since `.herd/` is read from the default branch and
  off-limits to worker commits, an agent can't do it through its own branch either.
- **No filesystem access outside the project.** The only mounts are the unit's own clone, the project's declared
  cache volumes, and (when provided) the read-only inputs volume. No host bind mounts (not the host checkout, not
  `$HOME`, not the Podman socket), no access to other units' clones, other projects' volumes or the bare mirrors.
  The container's root filesystem is read-only apart from those mounts and a scratch `tmpfs`. Secrets reach a
  container only as the env vars its role needs.
- **Caches** are per project *and* per role, so one project's worker can never read or poison another's. A project
  that declares none gets the strict per-clone behavior (slower, nothing shared).
- Network egress is the model endpoint plus the manifest's `egress` list — not open internet. Enforced by putting
  workers on an internal Podman network (`--internal`) behind an allow-listing proxy.

## Outside content

When an agent needs content from outside the project (a reference photo, a sample file, a vendor doc, a model
file), it never fetches it itself. The human provides it through a request/provide cycle that is recorded in git
like every other state:

1. **Request.** The implementer names what it needs in its status file (what, why, which task); the reviewer turns
   that into a `needs-human: input <name> — <why>` marker in `review-notes.md`. The proposal stops there (see
   Escalation); the status pane lists the request.
2. **Provide, preferred: commit it.** If the content is fine to live in the repo, the human commits it to the
   change branch at a path inside the project, with a line in the change's `inputs.md` saying where it came from.
   From then on it's ordinary project content.
3. **Provide, when it can't be committed** (too large, licensed, or not to be published): the human runs
   `herd provide <project> <change> <file>...`, which copies the files into a per-change **inputs volume**
   (never a host bind mount) and records each file's name, SHA-256 and origin in `inputs.md`, committed to the
   branch. The orchestrator mounts that volume **read-only** at `.agent-inputs/` inside the unit's clone (the
   herd adds it to the clone's `.git/info/exclude`, so the project needn't gitignore it), and only for units of
   that change. A file whose hash doesn't match `inputs.md` is not mounted.
4. **Resume.** The human marks the `needs-human` request resolved; the next scan picks the proposal back up.

This path is for content, never credentials. A task that needs a secret escalates and stays with the human; it
isn't solved by handing the secret to an agent.

## Launching and watching the herd: herdr

The herd's terminal front end is [herdr](https://herdr.dev), the terminal workspace manager for AI coding agents
(workspaces, tabs and panes in a persistent session you can detach from and reattach to). **The herd is launched
and watched through herdr; there is no herd-specific dashboard.** "The herd" is the *running ecosystem instance*:
the orchestrator, the active worker containers, and the `herd` session in herdr that shows them.

**Launch: attach-or-create.**
- `herd` (no arguments) first runs `systemctl --user start herd-orchestrator`. This is idempotent: it does nothing
  when the orchestrator is already running, and the unit name is the singleton key, so no lock file is needed.
  Then it hands over to `herdr --session herd`, which launches the named herdr session or attaches to it if it
  already exists. One command either way, and detaching (closing the terminal) leaves everything running.
- The orchestrator does not depend on herdr. It runs as a systemd user unit whether or not anyone is
  attached, and lingering brings it back after a host reboot without herdr.

**The bridge: what the session shows.** A small display-only process, `herd watch`, runs in the session's first
pane. Because it runs *inside* herdr, it drives the session through the `herdr` CLI with the session context it
inherits, so the orchestrator container never needs herdr's socket. Every few seconds it reconciles the session
layout against the orchestrator's event log and the running worker containers:
- **one workspace per registered project**, with workspace metadata showing that project's in-flight count;
- **one pane per active unit of work**, running `podman logs -f <worker>` (read-only; the workers are
  non-interactive), named `<change> · <role> · task <n>`, with pane metadata for the proposal's state and review
  round. The bridge closes the pane when the unit ends, and only closes panes it created itself;
- **a status pane** (`herd status --follow`) listing every proposal in flight with its state (from the enum
  above), its current task and its review-round count. Proposals in `needs-human` or `awaiting-approval` are
  listed first, with the reason or the final-approval instructions, because those are waiting on the human;
- **a herdr notification** when a proposal enters `needs-human` or `awaiting-approval`.

The bridge is stateless, like the orchestrator: a fresh session (for example after a reboot) or a restarted bridge
just rebuilds the layout on its next pass. How the bridge's pane gets started in a new session (herdr session
config, or `herd` starting it through the `herdr` CLI right after creating the session) is a build-time detail to
check against herdr's documentation.

**The event log.** The orchestrator writes one structured JSON event per state transition (task assigned, commit
pushed, review verdict, PR opened, escalation) to an append-only log on a volume the bridge can read. **Both the
event log and the herdr layout are display-only. The orchestrator never reads them back**, so git stays the only
source of truth. Losing the log, the bridge or the herdr session loses only what's on screen.

**The `herd` CLI.** The herd repo installs `herd` on the host:
- `herd`: launch or attach (above).
- `herd init`: run in a project's checkout. **The one command that onboards a project.** It detects what it can
  (OpenSpec present; build tool from wrapper/lock files; gate candidates from `CLAUDE.md`/CI workflows), writes
  `.herd/project.yaml` and `.herd/toolchain.Dockerfile` with those defaults for the human to review and land
  via PR, installs or updates the planner skills, and registers the repo (see Registering projects). It's idempotent:
  re-running it on an onboarded project updates the skills and only reports where `.herd/` differs from the
  detected defaults.
- `herd doctor [<project>]`: proves a project is ready. It builds the project's worker images, runs the gate on
  the default branch inside a worker container with the real mount/egress limits (proving the toolchain is
  sufficient and the egress list complete), checks branch protection and required checks via `gh`, and checks
  that every model backend answers (see Models). It also checks that `herdr` is installed and lingering is on.
  Run it before trusting a project; it doesn't switch anything on (see Registering projects).
- `herd add <repo-url>`, `herd pause|resume|remove <project>`: see Registering projects.
- `herd provide <project> <change> <file>...`: see Outside content.
- `herd status [--follow]` and `herd watch`: the status view and the bridge (above). Both also work outside herdr
  (`status` in any terminal; `watch` refuses to run outside a herdr pane).

## PR body

The orchestrator keeps the change's PR body to a generic template (opening the PR first if nobody did): summary, test coverage, review-round count per task,
flagged human-review-worth items from the holistic review, final-approval instructions (while a draft), and — when
`release_notes: true` — a `## Release notes` section the reviewer writes in its holistic pass, for the project's
own release automation to lift if it wants to.

## Prior art

Checked 2026-10-04 for a free, open-source tool that does this end to end; none does. What exists, and what the
herd takes from it:

- **OpenHands** (MIT): a sandboxed coding agent with a headless mode and a GitHub issue resolver, one agent per
  issue. No task loop, separate reviewer or state machine. Taken: a candidate implementer harness (Containers).
- **no_human**: ticket to reviewed PR on your own machine, with an adversarial review by a different model and a
  guard against tampering with tests. Taken: the tamper guard (Who commits, who pushes), and the same rule as the
  herd's that a review never runs on the backend that wrote the code.
- **Hydra** (Conduction): the closest workflow, an OpenSpec pipeline from `tasks.md` through containerized quality
  checks, code and security review and `needs-input` escalation to a human merge. But it's Conduction's internal
  pipeline in a private repository, PHP/Nextcloud-only and Claude-only; its agents are GitHub users that push and
  open PRs; its state lives in labels and GitHub Projects; and it archives after merge. Taken as data points: iptables
  egress allow-lists per agent (Open questions), and a separate security review.
- **Symphony** (OpenAI, Apache-2.0): a spec and reference implementation that gives each ticket an agent workspace
  until its PR lands. Tied to Codex and Linear.
- **CrewAI** (MIT) and similar agent frameworks: they put an LLM in charge of coordination, the opposite of an
  orchestrator that runs no model, and don't cover git and GitHub plumbing, state or isolation, which are the hard
  parts here.
- Worktree-based session runners (Orbi, Contrabass, Composio's orchestrator and others): agents work in host
  worktrees and hold credentials, the security model the herd avoids.

## Build plan

Steps marked **(manual)** need a human.

1. ~~Create the herd repository~~: done (`herd`, starting with this file).
2. ~~Local vs. cloud for the implementer~~: decided 2026-10-04. Backends are per worker slot and can be mixed (see
   Models). The smoke test (Onboarding a project, step 7) runs cloud only, so a model's weakness isn't mistaken for
   a pipeline bug: Sonnet 5.5 implements, Opus 5.5 reviews. A local implementer slot joins right after, on an Intel
   Arc Pro B70 (32 GB, 608 GB/s): enough for a 30B-class coder model at 4 to 8 bits with an agent's long context.
   It's bounded by the rule that a task's last review round goes to a different backend. The B70 runs under
   official Ollama's Vulkan backend (Intel archived IPEX-LLM in January 2026), passed to the Ollama container as
   `/dev/dri`. The runtime is rootless Podman, already on the host.

   **(manual) Reality check before any local backend or slot goes into host config.** The B70 figures above are
   assumptions from published specs and benchmarks; the host had an RTX 3080 when this was written. Each of these
   must hold, measured on the host itself:
   - **The card is there and usable:** `lspci` shows it, the kernel's `xe` driver binds it, `/dev/dri/renderD*`
     exists, and the herd's user is in the `render` group.
   - **The container sees it:** a rootless Ollama container given `/dev/dri` reports the Vulkan device and loads
     a model onto it, not onto the CPU.
   - **The model fits with real context:** the candidate loads fully into VRAM at the context the harness needs
     (OpenHands: at least 22k tokens; a real task's prompt plus files is more), with no CPU offload.
   - **It's fast enough:** time per task, measured on the replayed tasks, is acceptable next to the cloud
     backend's. A task that takes hours locally holds up its change for hours.
   - **It's good enough:** replaying the smoke test's accepted tasks on the candidate (see Models), its first-review
     acceptance rate is close enough to the cloud backend's that the extra rounds cost less than they save.
   - **The host copes:** the gate (a Gradle build, say) and the model running at once don't run the host out of
     RAM or throttle it.

   If any fails, the herd stays cloud only, and the result goes in Open questions.
3. Role layers and generic `SYSTEM_PROMPT.md` per role; the toolchain-image + role-layer build.
4. The orchestrator: per-project bare mirror and per-unit clone lifecycle, worker container lifecycle, intake from
   `ready: true`, round-robin assignment to worker slots, the state derivation above, commit validation and
   push, escalation markers, draft PR, update-branch, mark-ready, PR body template, watching PRs for review and
   posting the reviewer's replies, and deleting merged change branches.
5. The event log and the herdr bridge (`herd watch`, `herd status`).
6. The orchestrator's Quadlet unit and the `herd` CLI: launch (start the unit, then `herdr --session herd`),
   `init`, `doctor`, `provide`; an install script that puts `herd` on `PATH`, creates `~/.config/herd/`, installs
   the Quadlet unit, enables lingering and the Podman API socket, and checks that `herdr` is installed.
7. **(manual)** Host secrets: `ANTHROPIC_API_KEY` (for every `anthropic` backend); a fine-grained GitHub
   token limited to the registered repos with contents + pull-request scopes, for the orchestrator only.
8. The planner skills (`herd-propose`, `herd-ready`, `herd-resolve`), including their worktree clean-up, and their
   installation by `herd init`.
9. Onboard the first project (Onboarding a project, above). Onboard a second project on a different stack before
   calling the herd project-agnostic; the first one alone will hide assumptions.

## Open questions deferred, not forgotten

- Implementer harness: Aider or OpenHands headless (see Containers), compared on the same tasks; something custom
  only if neither fits.
- Which local coder model earns the B70 slot, and whether the reviewer's `archive` (or `task`) units can run there
  too: decide by replaying accepted tasks (see Models).
- Whether the herd runs as a dedicated `herd` user instead of the operator's account. Rootless Podman keeps the
  orchestrator's socket off root, but under the operator's account it could still mount their home. A dedicated
  user closes that, at the cost of the `herd` command and `herd watch` having to reach another user's Podman.
- The `review_rounds`, `added_tasks` and `gate_fixes` defaults (3 each): tune from the first smoke tests. Projects
  can override them. `pr_review_rounds` and the review timeout already rest on observed Copilot behavior (see The
  project manifest).
- Egress enforcement mechanism (allow-listing proxy vs. per-host firewall rules) — decide at Build plan step 3. Hydra
  enforces per-agent allow-lists with iptables, giving its security reviewer less egress than its builder: a tested
  data point for the firewall option (it needs checking under rootless Podman's networking).
- A separate security-review unit kind (static analysis such as Semgrep plus a security-focused prompt), run beside
  task or holistic review, as Hydra does.
- A second workflow besides OpenSpec — only when a project needs it.
- Whether to automate final approval for projects whose end-to-end tests can run in a container (e.g. an
  emulator with KVM passthrough), as a `final_approval.kind: container` with its own image.
- How the bridge pane starts in a fresh herdr session, and whether herdr's agent-state integration is worth
  feeding from the workers' status files (they aren't interactive agents herdr can detect itself).
