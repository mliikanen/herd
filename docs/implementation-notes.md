# Implementation notes

Detail that `docs/design.md` deliberately leaves out: exact formats, sandbox mechanics, limits and edge-case ordering.
It was worked out during design review and is kept here as a starting point for the implementation, not as settled
design. When the code settles a point differently, the code wins, and this file should follow it. Each section names
the design section it belongs to.

## End-to-end tests (design: End-to-end tests: the red/green loop)

The gate proves a task compiles and its unit tests pass; it can't prove the feature works end to end. For projects whose
end-to-end tests can run on an emulator in a worker (Driving Log's Maestro flows, say), the herd closes a red/green loop
around the agents with them, in layers that get broader and more independent as the change matures. A project opts in
with an `e2e` block in the manifest: four commands of its own, which the herd treats as opaque, like the gate. The
block, together with emulator capacity on the host (see Emulators in workers), is what turns on the per-task layers (1
to 3 below) for every change, whatever the project's final-approval kind; `final_approval.kind` only chooses the
whole-change approval phase, and `kind: container` adds layer 4. `container` requires the block; manifest validation
rejects `container` without it, and the project is inactive with that reason until it's fixed. Likewise,
`pr_review.human_check` requires both `reviewers` and `instructions`, so the person always has a check to follow.

The commands' contract, so the orchestrator can handle ids and results deterministically:

- Every command runs in the repository root of the unit's clone, `boot` and `run` against the unit's own emulator
  (`prepare` and `select` get no access to it), and `run` with `$HERD_E2E_ARTIFACTS` naming a fresh, empty directory it
  may write to, created for that one `run` (no other command gets one; see below). It's a size-limited mount, capped in
  total bytes and file count (`e2e.max_artifacts`, from host config), so a broken or runaway `run` fills its own
  directory, not the host: hitting the cap fails that run, with the reason, like any other failed command. A run's
  directory is deleted once the herd has read its results, so retries don't pile up; what the unit's verdict cites (the
  failing tests' reports, screenshots and logs) is first copied to the change's evidence store under `/var/lib/herd/`,
  which the orchestrator mounts read-only into the `triage` unit that handles the failure. It holds evidence only for a
  failure not yet triaged, and each failure's copy is deleted as soon as its triage verdict is committed, or as soon as
  the failure's record stops being effective without one (a `rerun`, a newer content tip), so a change never holds more
  than its untriaged failures' evidence, each bounded by the per-run caps, however long it lives. A closed PR keeps its
  evidence for `e2e.closed_evidence` (host config, default 14 days), since the change may be reopened, and then it's
  deleted, as it is with the change branch; a change reopened after that takes the evidence-lost path below. If the
  evidence is missing when triage is due (lost in a restore, say), the failure isn't triaged blind: the orchestrator
  records `final-approval rerun: evidence lost` and the run happens again.
- **`boot`** takes no arguments: it starts the unit's emulator and exits 0 once the emulator accepts installs. It's
  part of the pinned harness, and the herd runs it once per unit, before the first `prepare`, in a cgroup of its own
  that `prepare`'s clean-up (below) never touches, and stops that cgroup, emulator and all, when the unit ends.
- **`prepare`** takes no arguments and is idempotent: it rebuilds the app from the working tree, leaving it at the path
  `e2e.app` names (relative to the repository root), and exits 0 once it's built; any other exit fails the unit. It has
  no access to the emulator: the herd copies the built app out of the tree, checking it's a regular file inside the
  repository, into the sandbox as `$HERD_E2E_APP`, and the pinned `run` installs it. The herd runs `prepare` before
  every `run`, so a rerun after an edit always tests the edited code, never the previously installed build.
- **`select`** takes no arguments. It reads, on stdin, the paths that differ between a base commit and the working tree,
  committed or not, one JSON string per line (a Git path may contain a newline, so a raw path per line would be
  ambiguous; JSON can't carry bytes that aren't UTF-8, so the herd requires a change's paths to be valid UTF-8 and
  commit validation rejects one that isn't, which also keeps the waiver digest's path framing exact), which the herd
  computes itself (so the implementer can run it before its commit exists, and a reviewer runs it for the commit under
  review); it can read the `e2e.tests` files, but nothing else of the change (see below). It prints the relevant tests
  on stdout as JSON lines, one per test: `{"id": ..., "files": [...]}`, where `files` lists the repository paths that
  define the test (all under `e2e.tests`). That's how the herd knows which selected tests a change adds or changes: a
  test is change-local if any of its files is among the changed paths, which decides both the required red run and
  whether step 2 below compares with the default branch. Every added or modified path under `e2e.tests` among the
  changed paths must appear in at least one selected test's `files` (a deleted one is the tamper guard's business), so a
  selector that doesn't recognize a new test or helper can't make its red/green proof disappear by selecting nothing.
  Ids are unique, and each is a safe single path component (only letters, digits, `.`, `_` and `-`, and never `.` or
  `..`), since it also names the test's evidence directory; output that breaks this is a failed attempt with reason
  `e2e-contract`. A deterministic script, so selection is reviewable and repeatable and an agent can't quietly skip a
  test. Any non-zero exit fails the unit.
- **`tests`** names the end-to-end test files. They're built-in guarded paths: changing one in any way, not only
  deleting it or adding a skip marker, needs a `change` declaration under `Guarded:` and the task review's acceptance
  (see Who commits, who pushes). And they're what a red run carries over (see layer 2).
- **`run`** reads test ids from stdin, one per line, so no id needs shell escaping. It first installs `$HERD_E2E_APP` on
  the unit's emulator, then runs the ids, writing `$HERD_E2E_ARTIFACTS/results.jsonl`, one line per id
  (`{"id": ..., "status": "pass" | "fail", "detail": ...}`), and per-test evidence (reports, screenshots, view
  hierarchies, device logs) under `$HERD_E2E_ARTIFACTS/<id>/`. Each id runs from clean app and device state (app data
  cleared, device settings and media as `boot` left them), so results don't depend on order or on what ran before; the
  herd relies on that when it reruns one failed id alone to tell a flake from a failure, and in red/green runs. It exits
  0 if every test passed, 1 if any failed, anything else on an error that isn't a test result. Any other exit is a
  command failure (an install that failed before any test ran, say): the unit fails with the reason, and whatever
  partial results it left are ignored. For exits 0 and 1, the herd validates the results before believing them: valid
  JSON, exactly one record for every requested id and none for any other, and statuses that agree with the exit code (0
  means all pass, 1 means at least one fail). A violation means the script is broken, not the test: it's a failed
  attempt with reason `e2e-contract`, so a script that keeps breaking escalates instead of passing a change by accident.

**The harness comes from the default branch, never the change branch.** It decides whether the loop can go red at all,
so an implementer that rewrote `select` to print nothing, or `run` (or any helper either loads) to report success, would
switch the safety net off. So:

- **A pinned, separate copy runs.** `e2e.harness` names a directory; the herd copies it whole from the default branch
  and mounts it read-only at `/herd/harness/` in the unit. All four commands (`boot`, `prepare`, `select` and `run`)
  must live in it, and their manifest paths are resolved against that copy (with `harness: e2e/`, `run: ./e2e/run.sh`
  runs `/herd/harness/run.sh`), still with the repository root as working directory. The working tree's own harness
  directory stays editable, since a task may need to change it, but it's never executed: everything under `e2e.harness`
  is a built-in guarded path, so an edit is declared, and it takes effect only once it's merged.

- **The boundary is enforced, not just checked.** `boot`, `select` and `run` run in a sandbox whose filesystem holds
  only the harness copy, the toolchain image (built from the default branch too), a read-only copy of the working tree's
  `e2e.tests` files, taken before `prepare` runs (so the build, which runs the change's own code, can't change what's
  tested), in which every entry must be a regular file whose path stays under an `e2e.tests` root (a symlink or a
  gitlink there fails the unit, since following it would test content the guard never sees), the test definitions under
  test, and, for `run`, `$HERD_E2E_ARTIFACTS` and the copied `$HERD_E2E_APP`; `boot` and `run` also get the unit's
  emulator. Nothing else of the change is readable to them, so they can't source a helper or load configuration from a
  branch-controlled path. `select` doesn't need the tree: the orchestrator computes the changed paths itself and passes
  them on stdin. `herd doctor` confirms the sandbox by having a probe in it fail to read outside those mounts. Test
  definitions are the change's too, and a test tool may run code from them (a Maestro flow's scripts, say), so inside
  `run` the tests themselves execute as yet another user, without access to `$HERD_E2E_ARTIFACTS`, writing their raw
  output to a scratch directory, a size-limited mount with the same per-run caps as the artifacts directory (hitting
  them fails the run); each test id runs in a cgroup of its own, and when it exits the herd kills everything left in
  that cgroup before `run` translates its output or starts the next id, so nothing a test started in the background
  outlives it; only then does the pinned `run` translate that output into `results.jsonl` and the evidence, so no test
  can write or replace the results. `prepare` is the exception by nature: building the app means running the working
  tree's own build (`./gradlew`, its wrapper and build scripts), which is the change's code, so it runs with the whole
  working tree, in the unit's container but outside that sandbox and with no access to the unit's emulator, so it can't
  tamper with the device the trusted `run` tests on; its build configuration (the wrapper and build scripts) is guarded
  (see Who commits, who pushes), while the app source it compiles isn't, and needn't be: whatever that builds is only
  the app under test, which never reaches the results. So it can't touch what's trusted later, it runs as its own user
  in its own cgroup, with `$HERD_E2E_ARTIFACTS` unset and no results directory in existence; when it exits, the herd
  kills everything left in that cgroup (a background process it started included; the emulator isn't among them, since
  `boot` started it in its own), and only then creates the artifacts directory for `run`, a new one for every run, so no
  earlier run's evidence is lying around either, mounted into the sandbox alone, which runs as a different user that the
  build's user can't write as. After `prepare`, the herd also compares the working tree's `e2e.tests` files with the
  copy it took before: a build that changed them fails the unit, so a test can't be weakened for one run without a
  commit that shows it. The change contributes the app being tested and its tests, nothing that decides selection or
  reads results.

- **`e2e.tests` and `e2e.harness` may not overlap**, or copying the harness would replace a changed test with its
  default-branch version; manifest validation rejects a manifest where they do.

- **The pin.** The copy is taken at the default-branch commit the change's branch is currently based on (its
  merge-base), not the moving tip, and the rest of the e2e environment is pinned with it: the manifest's `e2e` block and
  the toolchain image (by digest), both as they are at that same commit. So every unit of the change runs the same
  `select` and `run`, with the same configuration and the same image, and an implementer's `E2E:` section is checked
  against the same harness that produced it. The pin, all three together, moves only when update-branch merges a newer
  default branch into the change, and only before the archive, and only to a commit whose manifest can still serve the
  change's recorded `e2e-mode`: if the newer default branch has dropped the `e2e` block or the harness, or no longer
  builds the emulator image a `container` mode needs, the merge goes ahead, provided every test definition the kept
  environment can select still exists in the merged tree (otherwise the update escalates to `needs-human`, "the default
  branch dropped what this change's end-to-end tests need"), but the pin stays where it was, no longer the branch's
  merge-base (the scan derives it as the latest of the change's merge-bases whose manifest can serve the mode), so the
  change keeps the last environment that can run its red/green proofs and final e2e for the rest of its life, and the
  scan notes it on the status pane. And from the archive commit on, it's frozen, since an archived change can't go back
  to *awaiting-approval*, and CI's full suite covers anything a later merge brings in. When the merge-base moves before
  the archive, whether or not the pin moves with it, the change's `container`-phase pass is no longer current, even if
  the merge is otherwise bookkeeping: that pass was earned against the old baseline and possibly the old harness, so the
  final e2e runs again (which is also when a waiver that lapsed on the merge gets its test run). This is the one
  exception to clean merges keeping final-approval records; the person's approving review isn't affected, since a clean
  merge doesn't move the content tip. The `container` record names its merge-base for this
  (`final-approval container pass <tested sha> base <merge-base sha>: <detail>`).

The layers:

1. **Inner loop: the implementer.** When a task's diff selects any tests, they're part of the implementer's gate for
   that task: it runs them, reads the results, fixes what's red and reruns until they're green, before it commits. This
   is where the loop does its work: the agent gets the same feedback a person would, in the same unit, as often as it
   needs.
2. **Red/green proof.** A test the task adds or changes has to show that it actually tests the change: it must fail
   against the app as it was before the task, and pass on the task's own commit. "Before the task" is the task's
   **baseline**: the parent of the task's first implementation commit, fixed for the life of the task. A revision after
   a rejected attempt starts on top of the rejected commit, but keeps the same baseline, so the task's earlier changes
   stay in its selection and its red run stays against the app without any of them. Selection for the task compares that
   baseline with the working tree. The red run builds the baseline's tree untouched, so `prepare` builds exactly the old
   app even if a build input happens to match `e2e.tests`, and the task's versions of the `e2e.tests` files reach only
   `run`, through the test snapshot (the baseline has the old test or none): `run` runs the new test against the old
   app. The implementer runs both and records the results in an `E2E:` section of its commit message, in the fixed
   format of `Guarded:`: a `- <test id> green` line for **every** test in the effective selection (what `select`
   returned, minus tests with an effective `e2e-waive`, which aren't run; each of those gets a `- <test id> waived` line
   instead, so a waiver stays visible), since the green run is on the commit that carries the section (which a commit
   can't name by its own SHA, so it's implicit); an additional `- <test id> red <baseline sha>` line for each
   change-local test; and an additional `- <test id> flaky xN` line for each test that flaked along the way, N being how
   many times. Validation checks that every test in the effective selection has its green line (or, with a matching
   `escalate` request, its `escalated` line; see below), every waived one its waived line, every change-local test its
   red line, and that the SHA is the task's baseline. A test that's green on both is vacuous, and the task isn't done.
   The exception is a **test-maintenance task**, one whose own changes stay inside `e2e.tests` (fixing a flaky test,
   refactoring a helper), counting only what its implementation commits change from the baseline, so neither the
   checkbox every implementer commit flips in `tasks.md` nor a reviewer's verdict in `review-notes.md` after a rejection
   counts against it: it doesn't change the app, so there's no behavior for a red run to prove, and its change-local
   tests instead must pass on the baseline too, recorded as `- <test id> green <baseline sha>` in place of the red line.
   Validation accepts that line only when the task's own changes stay inside `e2e.tests`, and the task review checks
   that `tasks.md` describes the task as test maintenance and that the change doesn't weaken what the test checks.
3. **Task review.** The reviewer doesn't take the implementer's word for it: it runs the selected tests itself on the
   task's commit, and the added or changed ones against the task's baseline too, and a result that doesn't match the
   `E2E:` section is a revise verdict.
4. **Final e2e: `final_approval.kind: container`.** In *awaiting-approval*, an `e2e` unit (a reviewer unit kind) runs
   every test `select` picks for the whole change (from its current merge-base to its branch tip, so a clean
   update-branch merge's result is what's compared, not the pre-merge content tip) on one fresh emulator. A pass is
   recorded as a `container`-phase final-approval pass, a bookkeeping line in `review-notes.md` (see The effective
   final-approval record); a failure is recorded as a `container`-phase `fail` with the run's results, and a separate
   `triage` unit then applies the test-or-implementation rule below (see Final approval). The `e2e` unit itself only
   runs the tests and records the verdict. A person's check, for what only a real device can do, comes later, in PR
   review (`pr_review.human_check`).
5. **CI: the full suite.** The project's CI runs every end-to-end test as a required check, independent of the herd's
   selection and of its emulator setup. A failure goes through Failing checks before the archive like any other, with
   the job's artifacts handed to the triage unit (see below).

**Test or implementation?** When a test fails, whoever triages it (the implementer in its own loop, the reviewer, or a
`triage` unit after the final e2e, a failed check in the person's PR review, or CI) follows the same order, so the
answer comes from evidence rather than taste:

1. **Rerun it.** If it passes on a rerun, it's intermittent, but that alone doesn't say whose: for a test this change
   didn't add or change, the triager also runs it on the merge-base build (step 2's) `caps.flaky_retries` + 1 times. If
   it both passes and fails there, the flakiness predates the change: it's flaky, so record it, retry, change nothing.
   If it fails every time there, it was already broken, and step 2's pre-existing path applies (fix the default branch
   or waive). If it passes every time there, this change made it intermittent, which is a regression like any other
   failure the change caused: go on to 3. A change-local test that passes on a rerun is flaky and the change's own. Each
   flake leaves a fixed record, written by whoever saw it, since reruns happen inside disposable units: the implementer
   lists it in its `E2E:` section (`- <test id> flaky xN`), a task review or `triage` unit adds
   `e2e-flaky <test id> <sha> xN` to its verdict commit, N counting every flaky outcome in the unit, not just whether
   there was one, and CI triage records `ci-triage "<check>" <sha> <run key> "<test id>": flaky xN`. Flaky outcomes are
   counted from those records per test per change, summing the Ns (CI's keyed by check and test id together, so a
   non-test failure, `"-"`, in one check never shares a count with another check's), and when the count reaches
   `caps.flaky_retries` the change escalates to `needs-human` ("flaky test") instead of retrying again, so an
   intermittently failing test can't cycle forever. Units enforce the cap as they go, since reruns happen inside one
   unit (for a test the change didn't add or change, only once the merge-base runs have attributed the flake as
   pre-existing; until then nothing counts toward the cap): the change's recorded count plus the unit's own flakes so
   far must stay below the cap before another rerun, and when it doesn't, the unit stops retrying (an implementer
   commits what it has with an `escalate` request, "flaky test"; a reviewer or triage unit records its flakes and
   escalates); for a test the change didn't add or change, the person fixes the test or its environment in a separate
   change and resolves the stop once that's merged (update-branch comes first, as for a "fails on the default branch
   too" stop); for a change-local test, the flakiness is this change's own, so the person resolves the stop by adding a
   task to stabilize it to this change's `tasks.md` (`herd-resolve` helps), or fixes the environment if that's the
   cause. Either way the test's flake count starts over from that resolution. A flaky test can't be waived: a waiver
   needs a failure on the merge-base to point at, and a flake may not have one.
2. **Run it on the build of the change's merge-base** (the default-branch commit the change is based on, normally also
   the one the harness is pinned to), but only if the same test definition exists unchanged there. Not the default
   branch's current tip: it may have picked up an unrelated fix since, which would make a pre-existing failure look like
   this change's. A test this change adds or changes (under `e2e.tests`) is meant to fail on the old build, or may not
   exist there at all, so it skips this step and goes straight to 3. For an unchanged test: if it passes on the
   merge-base, this change caused the failure: go on to 3. If it fails there too, it's rerun there once more, since a
   single run can't tell a broken test from a flaky one: passing on that rerun makes it flaky (step 1's record and count
   apply), and failing again means the test was already broken, and fixing it isn't this change's job. So that can't
   loop, the triager escalates to `needs-human` ("fails on the default branch too"), and the person either fixes the
   default branch in a separate change and resolves the stop once it's merged, (resolving that stop makes update-branch
   the next action before any retry, whatever the change's state: see Failing checks before the archive), or waives the
   test for this change with `herd-resolve`, which records `e2e-waive <test id> <default sha> <files digest>: <reason>`,
   naming the merge-base commit the test was found failing on and a digest of the test's `files` (`sha256:` and the
   lowercase hex SHA-256 over the files sorted by path bytewise, each file framed as: its Git file mode in octal
   (`100644` or `100755`) and a newline; its path's length in bytes in decimal, a newline and the path's UTF-8 bytes
   (paths are UTF-8, see `select`); a newline; its content's length in bytes in decimal, a newline and the content's
   exact bytes, so `herd-resolve` and the scan compute the same value); the herd's own e2e runs for the change then skip
   it. The waiver covers exactly that failure and lapses on its own when either changes: once the test's files on the
   branch no longer match the digest (a later task touched the test, so it's change-local again and owes its red/green
   proof), or once the change merges a newer default branch (the baseline moved, so the comparison runs again). A lapsed
   waiver means the test runs, and if it still fails on the default branch, a fresh escalation. A waiver doesn't touch
   CI: the required check still fails until the default branch is fixed, which keeps the merge blocked on the real
   problem, but its failures in the waived test are triaged as `waived` rather than escalated again (see Failing checks
   before the archive), so the rest of the change can still progress while it waits.
3. **The spec decides.** If the change's spec deltas change the behavior the test asserts, the test is out of date and
   gets updated (ideally a task already said so). If they don't, the implementation broke existing behavior and the
   code is fixed. If the spec doesn't settle it, the change escalates to `needs-human`: intended behavior is the
   proposer's call.

The implementer can't write `review-notes.md`, so when its own triage ends in either stop (in step 2 or 3), it doesn't
escalate itself: it commits the task's work as it stands, since the failure may depend on it, with the checkbox flipped
to `[r]` as for a request and an `escalate` request for the test (see Who commits, who pushes). Its `E2E:` section lists
that test as `- <test id> escalated` in place of its green line, which validation accepts only with a matching
`escalate` request; everything else in the gate still has to pass. The task review reruns the test on that commit (and
on the merge-base, for a pre-existing claim) and answers `needs-human` when the evidence holds, or `refused` with what
it found; either way the task goes back to `[ ]`, and the next attempt starts on top of that commit, as after any
rejection. An escalation isn't a failed attempt, so this doesn't spend `caps.failed_attempts`.

Updating a test to make it pass is always explicit: a project's end-to-end test files are named by `e2e.tests` (for
Driving Log, `maestro/**`), which makes them built-in guarded paths, so any commit that changes one declares it under
`Guarded:` with what justifies it: the spec delta that changed the behavior it asserts, or, for a test-maintenance task,
that task and the evidence it leaves behavior alone (the green run on the baseline), and the task review has to accept
it (see Who commits, who pushes).

**CI artifacts for triage.** A triage unit for a failed CI check gets the job's logs and uploaded artifacts (the
end-to-end reports and screenshots, for instance): the orchestrator fetches them through the GitHub App (which therefore
has *Actions* read access) and mounts them read-only into the unit, like provided inputs; the triage unit itself has no
GitHub access. That output crosses into a model-backed worker, so only some of it does: what the manifest lists in
`e2e.ci_artifacts`, as pairs of a job and an artifact name. A job's logs are attributable to it, so a listed job's logs
can be passed on once the job is checked secret-free. That takes more than the absence of `secrets.*`: the job
references no secrets and runs in no deployment environment; its token has no write permission and no `id-token`
(read-only `contents` at most); every `actions/checkout` sets `persist-credentials: false`, so test code can't copy the
token into an artifact; it calls no reusable workflow; and the workflow isn't triggered by `pull_request_target`; and it
runs on a GitHub-hosted runner, which the workflow file can only suggest (a `runs-on` label from GitHub's own fixed set,
not `self-hosted` or a custom label, nor an expression that could resolve to one) and which the orchestrator verifies
for the very job it fetches from, through the Actions API's record of the job's runner group (GitHub's hosted pool, not
a self-hosted group, since a self-hosted runner can carry any label), withholding the output when that can't be
established, which is ephemeral and holds nothing of anyone's beyond the job, whereas test code on a self-hosted runner
could read the runner host's own files and credentials and copy them into its output. And it holds for every job in the
workflow, not just the listed one: any job in a run can download what its siblings uploaded, so test code in a
secret-free job could otherwise copy a secret-bearing sibling's artifact into its own output. Anything the check can't
prove from the workflow file disqualifies the job. Artifacts aren't: GitHub scopes them to the whole workflow run and
doesn't record which job uploaded one, so a listed artifact is passed on only if the run's workflow file shows that
exactly one job uploads an artifact by that name and it's the listed, secret-free job; an artifact that can't be
attributed that way is never mounted. `herd doctor` checks all of this against the default branch's workflows, and the
orchestrator re-checks it against the workflow file of the very run it fetches from. Downloads are capped in size, and
extraction is bounded while it streams, since a small, highly compressed artifact could otherwise fill the disk: total
extracted bytes and file count are capped, only regular files are written (no symlinks, hard links or devices), and no
entry's path may escape the mount. Hitting a limit drops that artifact and tells the triager it was too large. Any other
failed check reaches triage only as its name and conclusion; without the artifacts the triager reasons from far less,
which is why the project's end-to-end CI job should be one it can list.

**Emulators in workers.** The project's toolchain image includes what `boot` and `prepare` need (an emulator and a
system image, for Android), and units with e2e work get `/dev/kvm` (with the `herd` user in `kvm`, kept in the container
by `GroupAdd=keep-groups`, as for the GPU). Each such unit boots its own emulator (with `boot`) and throws it away with
the unit, like its clone: sharing one would carry app data and device state from one unit into the next. Emulators are
heavy (a few GB of memory and a few cores each, on the host that also serves the desktop and the local model server), so
host config caps how many run at once (`e2e.max_emulators`); a unit that needs one waits for capacity, and its wait
doesn't count against its timeouts. That capacity is also the switch: `e2e.max_emulators` defaults to **0**, and the
operator raises it only once the emulator probe in Build plan step 2's reality check passes on the host. While it's 0
(and on a host without KVM, where it must stay 0), no e2e layer runs at all, whatever a project's `e2e` block says: no
per-task loop, no red/green proof, no `e2e` units. Capacity is re-read every scan, but a change can't switch mode
halfway: whether its e2e layers apply is decided once, when its first unit is dispatched, from the manifest **at the
change's pinned merge-base** (see The pin), not the current default branch, so the mode never names an `e2e` block,
harness or image the pinned commit doesn't have (no block there means `off`), and recorded by the orchestrator as a
bookkeeping line in `review-notes.md`, which holds for the change's life. The line fixes the final-approval kind at the
same moment, since the two must agree (an "off" change can't take a container pass):
`e2e-mode <on|off> approval <none|container> human-check <on|off>`, where `human-check` pins whether the change owes a
person's check in PR review, and with it the check's reviewers and instructions from the same manifest. A later change
to the manifest's `final_approval` applies to new changes only, so a change in flight never finds itself owing a phase
it can't run. A change started with e2e on keeps owing its red/green proofs and its reviews' reruns: if capacity later
drops to 0, its units that need an emulator wait (the status pane says so, and it alerts once the wait passes
`alerts.infra_after`) rather than skip. A change started with e2e off stays off, with CI covering it, even if capacity
appears midway, so no accepted task is left without a proof it was never asked for. A project with an `e2e` block then
gets no emulator work, relies on CI, plus a person's check in PR review where it has one, and a change whose pinned
approval kind (from the manifest at its merge-base) includes `container` waits at its first dispatch, shown as "waiting
for emulator capacity" and alerted once the wait passes `alerts.infra_after`, rather than starting with a mode it can't
honor. Capacity is decided per change, not per project, because a change's pinned kind can differ from the manifest's
current one.

## Rented GPU backends (design: Rented GPU backends)

Between the local model server and a cloud API sits a third option, especially for coding agents: an open-weight model
on a GPU machine rented by the hour from a GPU cloud or marketplace. It can run models far larger than a desktop card
holds (a coder model that needs 80 GB or more), it's billed per hour rather than per token, and the harnesses already
speak to it, since the usual servers (vLLM, SGLang, Ollama) expose an OpenAI-compatible API.

The bill belongs to a **machine**, not to a model, so host config describes them separately. A `machines` entry is one
rented machine: its `instance`, which is what identifies the machine (the provider's ID for the rented instance,
qualified so it's unique across providers: `<provider>:<account>:<instance id>`, with a region or project added where
the provider's IDs are only unique within one; the operator copies it in), its endpoint, how it's protected, its secret,
its hourly `price` and its `idle_alert`. A backend of `kind: openai` that names a `machine` is a rented backend (the
`machine` field is what marks it; there's no separate flag), and gives its model, revision and context; several backends
may share a machine (two models served by one server), and the machine is still billed once. Validation rejects two
entries in the current config with the same `instance` or the same endpoint, since that would bill one machine twice,
and a machine's endpoint must reach that machine alone (the operator's assertion, like the model revision below). A
conflict with a retained definition isn't a config error but an operational block: a replacement instance that reuses
its predecessor's endpoint isn't activated (no units, but it is health-checked, and while the shared endpoint answers
both definitions accrue, since the herd can't tell which instance is answering and both may be billing; the status pane
says why, and a "replacement blocked, possibly billing" alert episode opens as soon as the block does, whether or not
the endpoint answers (a silent machine may still be billing), cleared only when the block lifts or the replacement is
confirmed stopped, with the daily reminder like any episode) until the retained snapshot holding that endpoint is
confirmed stopped and retired, since until then both would answer at the same URL. For the same reason an endpoint stays
bound to its instance until that definition is retired, or moves to another endpoint by an in-place update and its
in-flight calls on the old one have finished (see Removing or repointing a machine), whichever comes first: the operator
gives a replacement instance a new endpoint, or keeps the old URL leading to the old machine until it's confirmed
stopped and retired. The herd can't see where a URL leads, so this is the operator's assertion too, like an endpoint
reaching one machine alone; repointing a URL under a retained snapshot would charge the new machine's traffic to the old
one.

- **Model identity.** The backend names the model and its exact `revision` (the weights' commit, for a Hugging Face
  model). The OpenAI-compatible API reports only a served model ID, not a revision, so the revision is attested by
  convention: the server must serve the model under the ID `<model>@<revision>` (vLLM, for one, takes the weights'
  revision and a served-model name as separate options), `herd doctor` checks that the server's model list contains
  exactly that ID, and the gateway sets it as the request's model. That's the operator's attestation, since they set up
  the server, not a cryptographic proof of what weights are loaded; it's what keeps acceptance rates from silently
  mixing two versions. `Herd-Model` records the same ID (`openai/<model>@<revision>`).

- **Trust.** The machine's provider can see prompts and code, as a cloud API's can, often without the data commitments a
  model vendor gives. A rented backend counts as leaving the host, so a project with `locality: host` (see Models) never
  gets a rented slot, which config validation enforces; whether to use rented hardware at all is the operator's call,
  per project.

- **Network.** The model gateway is the only client, and the endpoint is never an open port. A machine declares how it's
  protected with `access`: `https-key`, HTTPS with a key (`secret`, kept in `~herd/secrets/machines/` and read by the
  proxy alone, like a provider key), for which `herd doctor` checks that a request without the key, and one with a wrong
  key, are both refused; or `wireguard`, reachable only through a WireGuard tunnel the proxy holds, where tunnel
  membership is the authentication (so `secret` belongs to `https-key` alone, and validation rejects it on a `wireguard`
  machine). The machine's `wireguard` block gives everything the proxy needs to bring the tunnel up itself: the
  machine's public address and port (`peer`), its public key, the proxy's own tunnel `address`, the `allowed_ips` it
  routes into the tunnel (the machine's tunnel address only), and the proxy's private key as a secret
  (`private_key_secret`, in `~herd/secrets/machines/`); the `endpoint` is then an `http://` URL at the machine's tunnel
  address, with no TLS inside the tunnel, since WireGuard already encrypts the traffic and authenticates the peer by its
  key. The operator sets up the other side on the machine. The proxy runs WireGuard in userspace, with the tunnel served
  by an in-process network stack rather than a kernel interface, so it needs no TUN device and no `NET_ADMIN`, and the
  orchestrator stays the only privileged component. `doctor` checks that the endpoint answers through the tunnel, and
  that the inference port is closed at the peer's public address. The endpoint's host is on the proxy's egress for that
  machine's backends only. The gateway applies the same rules as to any backend: it pins the attested model ID,
  allow-lists the inference route and token-only features, and checks each request against the backend's `context`,
  which a rented backend must declare (the OpenAI-compatible model list doesn't report it): the prompt's upper bound
  (counted as for a reservation, see Monitoring) plus the requested output must fit, and a request that can't is refused
  rather than sent to fail on the server. `doctor` checks the value with a request near that length.
- **Lifecycle.** At first the operator starts and stops the machine. The herd dispatches a unit to a rented backend only
  while that backend is ready: its machine isn't confirmed stopped, its endpoint answers health checks, **and** the
  machine's model list still contains the backend's own `<model>@<revision>` (one machine may serve several models, and
  one can disappear while the endpoint stays up), a backend that stays unready past `alerts.infra_after` while its
  machine is healthy (its model gone from the list, say) raises an "unready backend" alert episode, since its units
  would otherwise just wait without any other alert noticing; and a unit whose machine disappears mid-call (spot and
  marketplace machines can be reclaimed), or whose assigned model disappears from a machine that's still up (a
  model-not-found answer, which the gateway checks against the model list), ends as an `infra` failure and is retried,
  never charged as an attempt. **Endpoint health decides dispatch, never whether billing stopped**: an expired key, a
  broken tunnel, a crashed model server or a network blip look exactly like a stopped machine while the provider keeps
  billing. Only the operator can say a machine is stopped, with `herd machines stopped <machine or snapshot id>` (a
  request; see The herd's own account). Requests are consumed asynchronously and a name can be repointed in between, so
  the CLI resolves a machine name to its current definition id when it's run (from the status snapshot) and the request
  carries that id; a request made by name must still be that name's current definition when it's consumed, and is
  rejected and reported otherwise, never applied to whatever the name points at now or to the definition it just left
  behind (the snapshot might be published a scan late). A retained snapshot is confirmed only by its own snapshot id,
  given explicitly. The orchestrator handles the request in a crash-safe order: it validates it, durably records the
  confirmation in the persisted machine definitions against the exact current definition or snapshot id, and only then
  deletes the request file, so a crash in between at worst handles the same request twice, which changes nothing, and
  never loses the confirmation; it closes that machine's billing-related alerts, stops its accrual, and takes it out of
  dispatch: a confirmed-stopped machine gets no new units, the gateway refuses its calls, and units still running on it
  end as `infra` failures, retried elsewhere and never charged as attempts. For a current machine, the confirmation
  clears only explicitly, with `herd machines started <machine>` (a request, validated and persisted the same way), so
  nothing depends on the orchestrator having watched the machine go down and come back. Health checks keep running
  through a confirmation, and any successful one after it restores the machine's accrual for the time it was seen up, so
  a wrong confirmation never drops known-up time. An endpoint that keeps answering past `alerts.infra_after` after its
  confirmation means the machine wasn't stopped after all: that raises a "confirmed stopped but still answering" alert
  episode and restores the machine's accrual back to the confirmation, as continuous; for a retained snapshot, which
  never returns to dispatch, it also withdraws the confirmation, so retiring the snapshot needs a fresh
  `herd machines stopped <snapshot id>` once it really has stopped, since it may well have been billing all along
  (health checks keep running through the confirmation, so the herd knows the endpoint was up the whole time), while
  dispatch stays off until the operator runs `herd machines started` (stopping the machine for real leaves the
  confirmation in place, now simply true). Since unhealthy may still mean billing, an active machine whose health checks
  fail for longer than `alerts.infra_after` opens a "machine unhealthy, not confirmed stopped" alert episode, cleared
  when its health returns or the operator confirms it stopped.
- **Idle machines.** So that a machine left running for nothing doesn't burn money unnoticed, an idle machine raises an
  alert: up and healthy with no call in flight for its `idle_alert` (default 30 minutes), counted from the end of the
  last call, or, for a machine that hasn't served one since it became healthy, from the start of that healthy interval,
  so a long generation never looks idle and a machine never used can't escape the alert. It's an alert episode like an
  ongoing condition in Monitoring, opened when the threshold passes and cleared when the next call starts, when the
  machine stops being healthy (the unhealthy alert takes over), or by the operator confirming it stopped, so a machine
  idle all day alerts once plus the daily reminder.
- **Removing or repointing a machine.** Host config can change a machine entry at any scan. A machine's identity is its
  `instance`, so a change to anything else (`endpoint`, `access` and its `wireguard` block, `price`, `idle_alert`, a
  rotated `secret`) is an update in place: the same machine, reached the new way or billed at the new rate from that
  moment on, still accrued once. A unit's token is bound to the definition, not to a connection, so when the endpoint or
  access changes the gateway sends every new call, running units' included, over the new connection from that step on. A
  call already in flight can't move: it finishes on the old connection, which the gateway keeps open only for that, and
  once the last one has finished the old URL belongs to no definition and may be reused. A change of `instance`, or
  dropping the entry, means a different machine, or none, from the herd's point of view, but the old machine doesn't
  stop with it: the orchestrator keeps the old definition (tunnel, credentials, health checks, hourly accrual, alerts)
  as a **retained snapshot** under its definition id, the immutable id every definition gets when it's first persisted
  (the machine's name and the moment it was first loaded, say `h100-a@2026-10-04T15:02Z`), persisted in the herd's own
  files as an operational store and read back on start like the budget counter. A name can be reused or repointed many
  times, so the definition id, not the name, is what identifies it. Likewise the `instance`, not the name, decides which
  machine an entry is: an entry whose `instance` matches an existing definition, current or retained, takes that
  definition over rather than starting a second one, so a rename keeps the machine's id, accrual and credential copy,
  and a retained machine that comes back into config becomes current again. One physical machine never has two
  definitions, which keeps accrual once per machine (validation already rejects two entries with one endpoint). Noticing
  the change doesn't depend on a scan seeing the old config: the orchestrator persists every machine definition it puts
  into use (with the hash of its credential copy, below) in that same store before any health check, dispatch or accrual
  uses it, and every scan, the first after a restart included, compares host config with those persisted definitions,
  not with what the previous scan read. So a config change made while the orchestrator was down, or a crash before a
  scan finished, still finds the old machine to retain. The snapshot's credentials can't depend on files the operator
  may already have rotated or deleted as part of the very config change that retains it, so copying them at that point
  would be too late. Instead the proxy copies a machine's credentials into its own store as soon as it first loads the
  machine definition, keyed by the content's hash, and every definition in use (current or retained) runs on its own
  copy: editing or deleting the operator's file later only affects definitions loaded after the change, and a retained
  snapshot simply keeps the copy it already had, until it's retired. Since copies are keyed by content, definitions that
  share a credential share one copy, so a copy is deleted only once no persisted definition, current or retained, still
  references its hash. That store is a dedicated persistent volume mounted read-write into the proxy alone (directories
  `0700`, files `0600`), separate from the operator's files it copies from. Those live in a directory of their own,
  `~herd/secrets/machines/` (`0700`, files `0600`), holding machine keys only, which the proxy mounts whole and
  read-only, so a machine added or a secret renamed at any scan is readable without recreating the proxy; the rest of
  `~herd/secrets/` (provider keys, the orchestrator's App key) stays mounted one file at a time, and the App key never
  reaches the proxy. The snapshot takes no new units, its running units finish on it (as Models promises), and it keeps
  accruing cost and raises a "retained machine not confirmed stopped" alert episode, shown with its id. The two ends are
  separate: the operator's `herd machines stopped <snapshot id>` closes the alert and stops the accrual at once, like
  any confirmation, and the snapshot is retired (its record and, if nothing else references it, its credential copy
  deleted) once it's confirmed stopped, its units have finished, **and** its endpoint has stayed silent for
  `alerts.infra_after` since the confirmation; until then it stays health-checked, so a confirmation that turns out
  wrong still raises the "confirmed stopped but still answering" alert and resumes accrual, and a replacement waiting
  for its endpoint stays inactive.
- **Cost.** A machine's `price` is `per_hour`, in `budget.currency`, accrued **once per machine** however many backends
  use it. The budget counts its hours from the health checks: while the herd sees the endpoint up, the counter accrues
  the hourly rate, so the monthly budget covers rented hours alongside cloud tokens. That's an approximation of the
  provider's bill: health checks miss time (while the orchestrator is down, say), so the operator reconciles against the
  bill with `herd budget set --spent <amount> --as-of <time> --source <source>`, giving what one bill charged up to its
  cutoff. A source is what one bill covers: a cloud billing scope that carries the herd's traffic alone, named by the
  backend's `account` (`<provider>:<scope id>`: a dedicated account, or a workspace or project within one whose usage
  the provider reports separately, say `anthropic:<workspace id>`; a scope shared with other use would put that use's
  spend in the herd's budget and could never reconcile; backends billed to one account share it whatever keys they use,
  and a key's rotation or rename doesn't change it) or a rented machine, keyed by its qualified `instance`, never by its
  name, since names can be renamed and repointed (the command also accepts a current name, resolved when it's run, with
  the request carrying both the name and the definition it resolved to, and accepted only if the name still points at
  that definition when it's consumed, as for `machines stopped`; a retained machine is named only by its snapshot id or
  `instance`), and every ledger entry records its source, since bills from different providers arrive with different
  cutoffs. The orchestrator doesn't overwrite the counter with it, which could lose or double-count work in flight: in
  one atomic step, it adds an adjustment for that source, dated at the cutoff: the billed amount (the bill's running
  total for the billing month up to that cutoff) minus everything already counted for the source up to the cutoff, the
  herd's own settled accrual (from the ledger's timestamped entries) and any earlier adjustment alike, so the source's
  total up to the cutoff becomes the billed amount, and a later reconciliation corrects it rather than adding to it. A
  cutoff earlier than the source's last one, or in the future, is refused. Nothing is deleted: the detailed entries keep
  their project, change and idle attribution, so spend per proposal is still what the herd measured, and the adjustment
  is attributed to the source alone, shown separately as reconciliation. Every other source's spend is left alone, and
  every accrual and reservation after the cutoff stays, along with every reservation still unresolved, whatever its
  timestamp: a call in flight at the cutoff may or may not be on the bill, so its reservation stays in the counter until
  it settles and is replaced by the reported usage as usual, dated at the call's start. That can count such a call twice
  (once in the bill, once settled), never zero times, the same direction the counter errs in everywhere else, and the
  next reconciliation, with its later cutoff, replaces the settled entry along with the rest. The old total, the new
  one, the cutoff and the reason go to the event log. Restoring a lost counter works the same way, one source at a time,
  so a later reconciliation never double-counts an unscoped total: the operator gives each source's month-to-date bill
  with the cutoff at now, and paid dispatch, paused anyway, resumes once every source that may have spend this period
  has one: every source the config names (every cloud billing account and every rented machine, current or retained),
  plus every source in the period's source list, a small record kept with the persisted machine definitions, apart from
  the counter, of every source that could have spent anything this billing period: each cloud billing account that made
  a call, and every rented-machine definition persisted during the period, whether or not the herd ever saw it healthy,
  so an account removed or a machine retired earlier in the month is still asked for. Hours are attributed for the usage
  ledger by time, not tokens: while units are calling the machine, through any of its backends, its time is split evenly
  among them, and their share goes to their change; time with no call in flight goes to the machine's own idle bucket,
  never to a change. Hours are what the budget counts, but the ledger also keeps each rented call's token usage (the
  OpenAI-compatible response's `usage`), per unit and change like a cloud call's: the gateway reports it after the call,
  with no reservation to replace, so tokens per task stay comparable across backends.
- **The budget can't stop a rented machine yet**, since the herd doesn't control it. At the limit the herd stops
  dispatching to rented slots like any paid backend, and running rented units stop too: the orchestrator ends each one
  once any call it has in flight finishes, with a `budget` reason (not `infra`, and not a failed attempt), and since
  rented calls make no per-call reservation, the gateway also asks the orchestrator for a zero-cost authorization on
  every call and is refused while paid dispatch is paused, so none starts another meanwhile. But the machine keeps
  billing, so the herd raises an urgent alert asking the operator to stop it (and does the same for every rented machine
  not confirmed stopped when paid dispatch pauses because the counter was lost, since its hours are then being spent
  with nothing to count them against), the counter keeps accruing, and the status pane shows "over budget: rented
  machine not confirmed stopped" until the operator runs `herd machines stopped <machine>` or paid dispatch resumes,
  whether the operator raised `budget.monthly` or the billing month turned: once the pause is lifted, the budget no
  longer keeps work off the machine (its readiness still does) and the episode closes with it, rather than asking for a
  machine to be stopped that the herd is about to use (the idle and unhealthy alerts still cover it from there). For
  per-token backends the budget is a hard limit; for rented ones it's a hard stop on dispatch and an alert on spend,
  until the herd can stop the machine itself (see Open questions).

- **Evaluation.** A rented backend earns a slot the same way a local one does: replay tasks the herd has already
  accepted and compare first-review acceptance, time per task and cost per accepted task with the cloud backend (see
  Models). Cost per accepted task is measured in an exclusive window, with the machine serving only the replay: the cost
  is what the provider billed for that window, entered by the operator from the bill, since health-check accrual starts
  only at the first healthy probe and so misses boot and model loading; that figure includes startup and the gaps
  between calls (which the ledger would otherwise book to the idle bucket), divided by the tasks the replay got
  accepted, so other units' calls don't distort it and idle time isn't hidden. The bigger models it can serve are the
  reason to try it; the replay is what shows whether they pay off.

## Spending (design: Monitoring)

- **Spending.** The model gateway accounts for every cloud call **before** forwarding it. It asks the orchestrator, over
  the control socket, to reserve the call's maximum cost: an upper bound on its input tokens plus the requested output
  limit, priced from the backend's `price` in host config (per million input and output tokens). A rented backend is
  priced by the hour instead and counted from its health checks (see Rented GPU backends). The request body carries no
  authoritative input count, so the bound comes from the provider's token-counting endpoint where it has one
  (Anthropic's does), and otherwise from the request's byte length, which bounds text tokens from above; a call whose
  input neither can bound (an image, for a provider without counting) is refused. `price.input` is the backend's highest
  input rate (cache writes, say), so no billing category can exceed the reservation. If a provider ever reports more
  than was reserved anyway, the counter takes the reported amount and dispatch pauses once it crosses the budget. The
  orchestrator adds the reservation to the monthly counter, atomically, only if the result stays within `budget.monthly`
  (a reservation that would cross it is refused, not just those made after the limit), writes it durably, and only then
  acknowledges; the gateway forwards the call only after that acknowledgement, and refuses it (an `infra` failure for
  the unit) when the handshake can't complete. After the call, the gateway reports the actual usage the same way, and
  the orchestrator replaces the reservation with it and writes a usage event to the event log. A crash between the two
  leaves the reservation counted, so the counter can overcount but never undercount. The orchestrator stays the only
  writer of `/var/lib/herd/shared/` and of the counter, and the key-holding proxy gets no writable shared mount. The
  status pane shows spend this month against `budget.monthly`. At `warn_at` the operator gets an alert; "paid" means
  cloud APIs and rented GPUs alike, and only local backends are outside the budget; at the budget, the orchestrator
  stops dispatching units to paid backends (cloud and rented) and refuses new reservations, running units' in-flight
  calls finish, and local slots carry on. The status pane shows it as "paused: budget", not as `needs-human`: it's the
  operator's call to raise the budget or wait for the month to turn. The counter is keyed by billing period, the
  calendar month in `budget.timezone` (`2026-10`, say), and the orchestrator also records the last period it
  initialized: a missing current-month value counts as a rollover, starting from zero and lifting a budget pause on its
  own, only when that recorded period is the month before (the install script initializes the first one); any other
  missing value is a lost counter (below), and a restart near the boundary reads the right period's total. A reservation
  is charged to the period it was made in, and settled there even if the call finishes after the month turns, so a
  boundary can't move spend between months. Besides the counter, the orchestrator keeps a usage ledger, totals per
  project and change, so the status snapshot's spend per proposal survives a restart. Counter and ledger are the
  budget's control state, kept in the herd's own files and allowed as recovery input; with the alert queue (below) and
  the persisted rented-machine definitions, current and retained (see Rented GPU backends), they're the only state the
  orchestrator reads back besides git. They decide only whether calls to paid backends go out, never a change's state. A
  reservation refused because it would cross the budget pauses paid dispatch the same way, so units aren't dispatched
  only to have their first call refused; the refused unit ends with a `budget` reason, which counts neither as a failed
  attempt nor as an infrastructure failure, and is retried once dispatch resumes. If the counter is lost, paid dispatch
  pauses until the operator sets this month's spend per source with
  `herd budget set --spent <amount> --as-of now --source <source>` (read from the provider's billing), a request the
  orchestrator records in the event log before dispatch resumes.

Host config for the end-to-end limits above:

```yaml
e2e:
  max_artifacts: { bytes: 2GB, files: 10000 }   # per run's $HERD_E2E_ARTIFACTS; past it the run fails
  closed_evidence: 14d                 # how long a closed PR keeps its untriaged failures' evidence
```

## Failing checks before the archive (design: Communication and the work queue)

**Failing checks before the archive.** In states 6–8, a required check that failed on the current tip comes first: the
next action is a `triage` unit, which applies the test-or-implementation rule (see End-to-end tests) and records its
outcomes in `review-notes.md`, one line per failed test (a full-suite run can fail several, for different reasons):
`ci-triage "<check>" <sha> <run key> "<test id>": fix | flaky xN | preexisting | unsettled | waived`, with the check's
and the test's names as JSON strings since they may contain spaces (a failure that isn't a test's, a build step say,
takes the test id `"-"`). The run key names the exact failed run, since a re-run keeps the check's name and commit:
`check:<check run id>` for a check run, or `status:<status id>` for a legacy commit status, whose every update is a
new status with its own id. All of a run's lines are written in one verdict commit, keyed by it: the scan never
dispatches triage for a run that already has lines, and a re-run that fails again is a new run, triaged and counted
afresh. Each `fix` adds a fix task under "(added for CI)"; `preexisting` escalates to `needs-human` ("fails on the
default branch too") and `unsettled` to `needs-human` ("spec doesn't settle it"), and an escalation comes before the
fix tasks, which wait for the stop's resolution; with at least one `fix` and no escalation the change goes back to
*implementing* for its new tasks; `waived` marks a failing test with an effective `e2e-waive`: the triage unit records
it without judging it, and it adds no task and no escalation, since the person already decided. When the lines are
only `flaky` and `waived`, no task is added and the change stays in its state: if any flaky test's count has reached
`caps.flaky_retries`, the triage unit escalates ("flaky test") instead; otherwise, with at least one `flaky` line, the
commit that records them, itself a push, runs the check again on the new tip (required workflows run on every push;
see Requirements on a project). Flakes count per test, as everywhere. `flaky xN` counts every flaky outcome the triage
saw for that test (its reruns and merge-base runs included), like the other flake records. A run whose every failure
is in a waived test needs no triage at all, so nothing is committed for it and a waiver can't loop: the orchestrator
reads the run's failed ids itself from its `results.jsonl` (the CI end-to-end job uploads one in `run`'s format,
together with `requested.jsonl`, the ids it was asked to run as one JSON string per line, both at the top level of an
artifact listed in `e2e.ci_artifacts`; ids must be unique in each file, and a file that's missing, unparseable or has
duplicates means the run can't be verified) and treats the run as waived, without a unit or a record, only when it can
verify that's the whole story: the steps of the workflow job behind the check run, read through the Actions API (which
the App can read, and which, unlike the Checks API, lists a job's steps), show the end-to-end step as the only one
that failed, `results.jsonl` covers exactly the requested ids, and every failed id has an effective `e2e-waive`.
Whenever any of that can't be verified, the run goes to triage as usual. The required check stays red, so the change
carries on through its other work but can't become *ready-to-merge* (the status pane shows "waiting for a
default-branch fix"); the next update-branch merge that brings the fix in clears it. A triage unit that can't rerun
the test (the change's `e2e-mode` is `off`, so there's no emulator for it) classifies from the job's artifacts alone,
and records `unsettled` with the reason ("can't reproduce: no emulator") when they don't settle it, so a person
decides rather than the herd guessing. Those tasks count toward `caps.gate_fixes`; past it, the orchestrator
escalates. A "fails on the default branch too" or "flaky test" stop resolved as fixed on the default branch, that no
update-branch merge has followed yet, comes before everything else in every state before the archive (a stop resolved
by an effective `e2e-waive` doesn't: the waived test is skipped, and there may be nothing newer to merge): the next
action is update-branch, so the retry runs against a branch that contains the default branch's fix. This is the path
for a CI failure after an update-branch merge too (see Keeping up with the default branch). A required check that
still has no result `pr_review.checks_timeout` after the push it's for (queued, running or merely expected) doesn't
wait forever: the orchestrator commits a mechanical `needs-human` marker and alerts, before the archive or after it,
since a stuck CI is for a person to look at.

## Final approval (design: Final approval)

**The final e2e.** When a proposal reaches *awaiting-approval*, the orchestrator makes sure its **draft** PR exists
(normally opened by `herd-propose` at proposal time) and dispatches an `e2e` unit, which records its verdict in
`review-notes.md` as a `container` pass or fail, pinned to the SHA tested. Its result is also published as a commit
status, `herd/final-e2e`: on the tested commit, and, since statuses belong to one SHA, copied by the orchestrator onto
every later head of the branch it observes, whoever pushed it, while that record stays effective (and set to `pending`
on any head once a rerun is owed), so the PR's current head always shows it next to CI (the App's one write permission
outside *Contents* and *Pull requests*; see Build plan):
- **pass** → the proposal moves to *in-review*, and the PR is marked ready;
- **fail** → while that `fail` is the effective record and hasn't been triaged, *awaiting-approval*'s next action is a
  `triage` unit, which applies the Test or implementation rule (see End-to-end tests) with the run's evidence: a real
  failure becomes appended task(s) under "(added during final approval)", sending the proposal back to *implementing*; a
  flake below the cap is recorded and the final e2e runs again; a pre-existing failure, a flake past the cap or a
  failure the spec doesn't settle escalates to `needs-human`. Either way its verdict commit records
  `final-approval-triaged <sha of the fail record>`, so once a person resolves a stop (a waiver, say) the same fail
  isn't triaged and escalated again. Once fix tasks are accepted and the holistic review is current again, the change
  returns to *awaiting-approval* with the `fail` already triaged, and the next action is a new `e2e` unit.

**The person's check.** `pr_review.human_check` names who checks (`reviewers`, GitHub logins; one approval from any of
them is enough) and how (`instructions`). Both are pinned with the change's `e2e-mode` line, from the manifest at its
merge-base when it was first dispatched, so a change always shows the check it owes even if the manifest has since
changed or dropped it. When the PR is marked ready, the orchestrator puts the instructions in the PR body and requests a
review from the listed people. If none of them can be requested (a mistyped login, a removed collaborator: the request
fails for every one of them), the change escalates to `needs-human` ("can't request the person's check") instead of
waiting forever. The check passes with an **Approve** review from one of them on the current content tip, submitted
after the PR was marked ready and the review was requested for that tip (an approval given earlier, on a draft without
the instructions or before the final e2e passed, doesn't count): bookkeeping commits after it (a clean update-branch
merge, a verdict line) don't undo it, but a new content tip does, and the orchestrator requests the review again. Until
then the change can't leave *in-review*, and the status pane shows it as waiting on that person (see Monitoring).
There's no timeout: unlike an automated reviewer's, this review is required. It's judged before the archive only: the
archive commit is a new content tip for automated reviewers, but the person's approval of the change's content still
stands, and their merge is the last word anyway.

A **Request changes** or comment review is triaged like any other review (see Following up on PR review): a finding
becomes a task under "(added during review)". A Request changes review stops blocking once its findings are all triaged
and resolved, like any review's: the herd judges from the reviews and its own records, not from GitHub's "changes
requested" badge, and once the fixes land as a new content tip it requests the check again, so what's needed next is a
counting approval from any listed reviewer. A failed check in it goes through the same Test or implementation rule as
any failing test, with the person's review as its evidence, and that evidence comes with the review: a person reporting
a failed check runs `/herd-resolve`, which gathers it and submits the Request changes review with the evidence attached,
so triage never starts before it exists (a failed check reported in a plain review is triaged with whatever it says, and
the triage unit asks for the missing runs in a reply, which puts the change back to waiting on the person).
`/herd-resolve` gathers it like this: it asks the person to rerun the failing check once and, for an end-to-end test the
change didn't add or change, to run it on a build of the change's merge-base, which it checks out for them (never the
default branch's current tip, which may already carry an unrelated fix): `caps.flaky_retries` + 1 times when the rerun
passed, or a second time when the first try there failed. It includes the results in the review it submits. The
merge-base runs have one owner each: when the change's `e2e-mode` is `on` and the failing check is a test the pinned
harness can run, `/herd-resolve` gathers only the person's rerun, and the `triage` unit runs the merge-base repetitions
itself after the review is submitted; otherwise `/herd-resolve` gathers them from the person before submitting, and a
real-device check always stays with the person. The outcomes:
- passing on the rerun but stable on the merge-base: the change made it intermittent, a regression that becomes a fix
  task like any other;
- passing and failing on the merge-base: a pre-existing flake, recorded as `e2e-flaky <id> <sha> xN` (the harness's test
  id for an end-to-end test, or for a manual check `human:` plus a short slug `herd-resolve` assigns and the person
  confirms, offering the slugs this change already used, so one check keeps one id and its flakes add up), and the
  person is asked to review again until the count reaches `caps.flaky_retries`, when the change escalates to
  `needs-human` ("flaky test");
- failing on every merge-base run: pre-existing breakage, escalated to `needs-human` ("fails on the default branch
  too"). The person fixes the default branch in a separate change, or, for this change, waives an end-to-end test
  (`e2e-waive`, see End-to-end tests) or approves despite a manual check: `/herd-resolve` then resolves the stop and
  submits the approving review naming the known default-branch failure in one go, so the resolution and the approval
  land together, and the PR body repeats it;
- a failure the spec doesn't settle: `needs-human` ("spec doesn't settle it").
