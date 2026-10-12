# Evaluation plan: replaying Driving Log's archived changes

The herd rests on one bet: a change done by cheap models working task by task behind strong-model guardrails costs less
than one capable agent applying the whole change, with the same quality. Nothing is built yet, so this plan tests the
bet offline, before the orchestrator exists. It replays changes Driving Log has already archived, under five setups, and
compares cost, time and quality. Once the herd runs, a narrower comparison continues on live changes (see Measuring what
the herd saves in `docs/design.md`): the configured pipeline against one single-agent baseline, which corresponds to the
Baseline setup here. The Thin spec setup and the fixed per-task herd setups exist only in this replay. The metrics (cost
per accepted change, wall time, gate runs) are defined the same way, so those numbers line up.

The comparison is between independent, unattended implementations. No person touches a run between its start and its
judging.

## Questions

1. **Does the herd's split pay?** Per-task implementing with a strong reviewer, compared with one strong agent
   applying the whole change.
2. **How cheap can the implementer be?** A cheap implementer with a strong reviewer, with and without strong-model
   tests written ahead of each task (the `guard` idea).
3. **Does the spec pay?** One strong agent working from the full proposal, compared with the same agent given only the
   proposal's *Why* and *What Changes*.

## The corpus

Driving Log has 40 archived changes. Their history comes in three shapes:

- **Changes proposed and implemented directly on `main`** (most of September). The base is the commit that added
  `openspec/changes/<name>/`, and the reference result is the commit that archived it. Archiving only moves the
  proposal and syncs specs under `openspec/`, which every comparison leaves out, so the archive commit also serves for
  `clean-up-lint-baselines`, which was applied and archived in one commit.
- **Proposed and implemented in one commit**, or with other changes' work interleaved. These have no clean base, so
  they're left out unless the interleaved commits only add other proposals.
- **Squash-merged PRs** (from October). The base is the merge commit's first parent, and the proposal comes from the
  PR's own commits (`refs/pull/<n>/head`).

Changes whose result can't be judged in a container are left out:
- camera and OCR behavior (`add-live-scanner`, `add-odometer-ocr-capture`, `add-seven-segment-ocr`,
  `add-fuel-amount-ocr`, `scan-initial-odometer`, `fix-refueling-scan-capture`);
- Firebase (`add-firebase-auth`);
- GitHub-side work (`add-ci-workflows`, `add-app-distribution`);
- the license;
- Maestro-only changes.

The first corpus, smallest first:

| Change | Size (lines added, excluding `openspec/`) | Kind |
|---|---|---|
| `change-picture-badge-icon` | ~70 | UI detail |
| `confirm-lower-odometer` | ~260 | form rule plus dialog |
| `odometer-leading-zero-prefix` | ~570 | input model edge case |
| `add-landing-screen` | ~640 | new screen |
| `petroleum-color-theme` | ~820 | theme, checked by `ThemeColorsTest` |
| `add-direct-logging` | ~700 | feature across layers |
| `add-vehicle-color` | ~2,800 | feature plus migration |
| `add-distance-logging` | ~4,400 | large feature |
| `clean-up-lint-baselines` | ~1,900 (mostly deletions) | chore, checked by lint |

The sizes come from `git diff --shortstat <base> <reference>` and include tests. The pilot uses the first two changes.
The corpus script (below) checks each base and reference again before use, and drops a change whose span holds another
change's implementation. `add-direct-logging` was proposed in the same commit as `add-landing-screen`, which was
implemented first, so its base is `add-landing-screen`'s archive commit and its proposal still comes from the commit
that added it. Commits in a span that touch code but belong to no change stay in the reference and are listed in the
record: a test for `odometer-leading-zero-prefix`, a Maestro rework with a color test during `add-vehicle-color`, and a
moved document during `add-distance-logging`.

**Which proposal.** Every run gets the proposal as it was first written, before any implementation: the version in the
base commit, or in a squash-merged PR's first commit. The archived version also carries what was learned while
implementing (for the corpus, anywhere from a one-line edit in `petroleum-color-theme` to 189 lines of spec and design
in `add-distance-logging`). Handing that over would flatter every setup, and it would skew the spec question in
particular: the thin-spec setup gets only *Why* and *What Changes*, so a gap between it and the baseline would partly
measure the hindsight. The original is also what an unattended run faces live: it can't refine the spec as it goes. So
success rates come out lower and closer to the truth, and a run that fails on a gap the archived version later filled
shows up as such. The corpus script records each change's proposal edits for that check.

**No peeking.** Each run starts from a fresh repository holding only the base tree, in a single commit with no history.
Without it, an agent could read the answer from `git log`. Driving Log is public, so a model may have seen parts of it
in training. That's a small, shared bias, noted in the results.

## The setups

The plan compares five setups ("arms" in experiment terms). Phase 1 is cloud models only, and every unit runs through
`claude -p`, so the harness is the same in every setup:

| Setup | What it does | Answers |
|---|---|---|
| **Baseline** | Opus 5.5 applies the whole change in one session from the full proposal, like `/opsx:apply` | the reference point |
| **Herd** | per task: Sonnet 5.5 implements in a fresh session, and Opus 5.5 reviews the commit (accept or revise, up to 3 rounds). Opus finishes with a holistic review | question 1 |
| **Cheap herd** | as Herd, with Haiku 4.5 implementing. The last round of each task goes to Sonnet (the stronger-attempt rule) | question 2 |
| **Guarded herd** | as Cheap herd, but Opus first writes each task's tests and shows them failing. Those tests are read-only to every implementer, Sonnet's last round included: one it can't turn green is a test dispute, never an edit | question 2 |
| **Thin spec** | as Baseline, with only the proposal's *Why* and *What Changes* | question 3 |

Phase 2 adds a local implementer on the B70 in Cheap herd and Guarded herd, through the harness Build plan step 3 picks.
Phase 2 also doubles as the local backend's reality check (Build plan step 2).

Each run gets the change's gate as of its base: the final regression run in that base's `CLAUDE.md`. Early bases have no
`codeQuality`, and the five earliest don't name a final regression run at all: their gate is the two Gradle tasks their `CLAUDE.md` lists
for building and testing, plus `openspec validate --all --strict`. Two adjustments keep the gate fair at old bases:

- **Known OpenSpec failures.** Another in-flight proposal can fail today's strict validation at a base (at
  `confirm-lower-odometer`'s, `add-event-pictures` does). The corpus records these, and the gate passes when the
  validation fails only on them. The change's own proposal failing at its base drops the change.
- **Supplied files.** `clean-up-lint-baselines`' base applies the Google Services plugin, whose `google-services.json` is
  gitignored. The run gets the placeholder CI uses today, from outside the snapshot, as CI does. The three herd setups also run the gate before each commit, as the herd would. Every run gets the same
timeout: 4 hours for Baseline and Thin spec, and per-task limits for the herd setups matching the design's unit
timeouts.

## Judging

Judging is blind: the judge doesn't know which setup produced a result. Results are shuffled and their commit trailers
stripped.

1. **The gate**, rerun on a fresh clone of the result. Failing it fails the run.
2. **The tamper check.** Deleted or emptied tests, added skip markers, or changed lint baselines and build files are
   compared with the base. Any of these fails the run unless the proposal asks for it, which the check reads as: the
   reference change did the same.
3. **The reference tests.** The tests the original change added are copied in and run against the result. Many depend
   on internal names and won't compile, so this is recorded for information only: how many compiled, how many passed.
4. **Holistic review** by Opus 5.5, from the proposal, the specs and the result's diff. It gives an accept or revise
   verdict and lists findings by severity. For Baseline and Thin spec, Opus is judging its own model's work, so treat
   their review results with that bias in mind and lean on steps 1–3 and 5.
5. **The person's spot check**, a sample that checks the automated judging. For each change, you review two results
   blind, picked at random from those that passed steps 1–4, in about 15 minutes each. For each setup's best result on a
   change, you also run the area's Maestro flows on an emulator. This is the only human time the evaluation needs.

A run is **accepted** when it passes the gate and the tamper check and the holistic review gives a readable verdict
with no High findings. A run you reject in the spot check, or whose Maestro flows fail, becomes not accepted. If you reject
more than one in five of the runs you check, the holistic judging isn't trusted: the spot check widens to every
accepted run of the setups the decision depends on before anything is decided.

## Metrics

Per run:
- **Cost:** from `claude -p --output-format json` (`total_cost_usd`, plus tokens), summed over the run's sessions.
  Local models in phase 2 are priced by GPU time at a rate set before the runs.
- **Wall time** from start to judged result.
- **Gate runs:** the gate is wrapped in a script that logs each call.
- **Outcome:** accepted or not, with the reason.
- **For the herd setups:** per-task first-review acceptance rate, review rounds, and how often the stronger attempt was
  needed.
- **For Guarded herd:** test disputes, meaning tasks where the implementer couldn't turn the guard's tests green.

Per setup, counting every run (each setup runs each change twice in phase 1, and the runs aren't merged per change):
- the **success rate**: accepted runs divided by all runs;
- the **cost per accepted change**: the total cost of all runs, failed ones included, divided by the accepted runs;
- the median wall time.

## Running it

0. **The toolchain image.** Write Driving Log's `.herd/toolchain.Dockerfile` (JDK, Android SDK). The manifest already
   names it, so this is onboarding work done early. Check that the gate passes inside it at the oldest and newest base
   in the corpus.
1. **The corpus script** (`eval/corpus.py`). For each change in the table, it records the base, the reference, the
   original proposal, the edits made to it before archiving (ticked task boxes left out), the gate commands, the known
   OpenSpec failures, the supplied files, the size and the reference tests, all in `eval/corpus.json`. It also writes the
   base snapshots, as bare repositories under `eval/work/` (not committed, since they hold Driving Log's code).
2. **The runner** (`eval/run.py <setup> <change> [--rep n]`). It runs one setup on one change in a fresh container,
   records every session's JSON output and the gate log, and leaves the result as a branch in a local results
   repository (`eval/work/results.git`). The container keeps most of the worker contract: read-only root, per-run
   scratch volumes, the caches as overlays that no run writes back to, the memory, CPU and pid limits, its own internal
   network holding only it and the proxy, and egress only through that proxy: TLS to port 443 of Driving Log's hosts
   and the model API, with the TLS server name matching the host. Two parts are missing, since the evaluation's agents are the models under test, not untrusted code:
   - **No storage limits.** The host keeps containers on btrfs, and Podman's volume and overlay size limits need XFS
     project quotas. A run that fills its disk fills the host's.
   - **No metering gateway.** The worker gets the API key itself, so an agent's shell could call the API directly,
     past both the recorded cost and `--max-cost`. `--real` therefore needs `--max-cost` and a key made for the
     evaluation with its own spend limit at the provider, which is the hard cap. `--warm` fills the caches by running the gate at every base,
   which also checks each base passes it. Without `--real`, a stub stands in for `claude`, so the harness can be tested
   without spending. Where the herd's behavior isn't fixed by the setups' table, the runner does what the design does:
   - a commit that fails the gate goes back to the implementer with the gate's output, without a review, and uses up
     a round;
   - a task still not accepted after its last round stops the run, as the herd would park the change;
   - a holistic review asking for changes gets one fix session from the last round's implementer;
   - anything a reviewer commits, or leaves changed or added among the repository's tracked and untracked (not
     ignored) files, is discarded and counted, and the supplied files are copied in again; a guard test an implementer
     changes is restored and counted;
   - sessions have the design's `wall` timeouts; the `quiet` timeout isn't enforced.
3. **The judge** (`eval/judge.py [run ...]`): steps 1–4 above for every run not judged yet, in random order, written
   to `eval/results/<run>.json` (judgments made with the stub, or of stub runs, go to `eval/work/judgments/`). A run
   that ended early (a parked task, the cost limit, an error) made no change to merge and isn't accepted. For the
   others:
   - **The gate** runs in a fresh worker on a clone of the result branch, with the run's known OpenSpec failures.
   - **The tamper check** compares the result with its base snapshot (not with anything in the result's own history,
     which the agent controls), outside `openspec/`, and fails on any of these the
     reference change didn't also do: a test file or Maestro flow deleted, moved out of the tests and flows, or
     emptied; fewer `@Test` functions in a package; a changed file that configures the checks (`gradle.properties`,
     `.editorconfig`, `lint.xml`, `config/`, a lint baseline, the Gradle wrapper, `buildSrc/`, an init script); an added skip or suppression marker (`@Ignore`, `@Disabled`, `assume…(`,
     `@Suppress(…)`, `ktlint-disable`, `noinspection`) in source; or an added or removed build-script line that touches
     a check (a baseline, lint, detekt, warnings as errors, test filters, a disabled task) or tasks and the task graph
     (`tasks`, `dependsOn`, `doFirst`, `afterEvaluate`…), which could turn a gate task into one that does nothing. Other build-script changes,
     such as a new dependency, are listed but pass. Each flag carries its file and line, for the spot check.
   - **The reference tests** are copied in over the result and `:shared:allTests` runs. A copied file that doesn't
     compile is dropped and the tests run again; when the errors are only in the result's own files, the copied files
     that replaced one of them (a shared fake, say) are dropped instead. The record lists what compiled, what was
     dropped, and the tests that ran, passed and failed in the copied files.
   - **The holistic review** runs only when the gate and the tamper check pass, since the run fails otherwise. It
     happens in another fresh worker, under a random name, holding two commits: the base snapshot with the original
     proposal, and the result squashed into one commit with its `openspec/` reset to the base's, which hides ticked
     tasks and Thin spec's cut-down proposal. The files Claude Code loads as instructions or settings (`CLAUDE.md`,
     `.claude/`, `.mcp.json`) are reset to the base's too, so a result can't instruct its judge; the tamper record
     lists the ones the result changed. The worker has no gate: the review reads the code. The run passes this
     step when the verdict is readable and lists no High finding; a revise without one contradicts the prompt, which
     accepts it, so it counts as an accept.
4. **Pilot.** Run all five setups once each on the two pilot changes. Fix the harness, then estimate the full run's cost
   from the pilot before going on.
5. **Phase 1.** Run every setup twice on every change in the corpus. The two runs show how much results vary between
   attempts.
6. **The spot check**, then the report: `eval/results/phase1.md`, with the tables and the decision below.
7. **Phase 2.** Run Cheap herd and Guarded herd with the local implementer, once the B70 passes the reality check's
   hardware items.

Set a spending cap before each of steps 4, 5 and 7, and stop at it. After each of those steps, compare the runs'
recorded cost with the key's usage at the provider: a gap means calls the records missed, and the cost per accepted
change can't be trusted until it's explained.

## Deciding

The thresholds are set here, before any runs, so the results can't move them.

Rates are compared in percentage points. With 9 changes, one change is about 11 points.

- **Cheap implementers pay** if Cheap herd or Guarded herd has a success rate at most 10 points below Baseline's, at no
  more than half of Baseline's cost per accepted change. Build the herd as designed, with tiers.
- **Only the split pays** if Herd meets that bar but Cheap herd and Guarded herd don't. Build the herd as a strong-model
  pipeline, and drop the guard and tier work.
- **Neither pays** if no setup meets it. The herd's value is then only that it runs unattended, saving person-hours
  rather than money. Weigh that against its build cost before going further.
- **The spec pays** if Thin spec's success rate is 20 or more points below Baseline's. If it comes close to Baseline's,
  then for changes this size the detailed spec buys less than it costs, and proposals can stay thin.

The results go into the design: Open questions, and the Build plan's reality check.
