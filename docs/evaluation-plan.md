# Evaluation plan: replaying Driving Log's archived changes

The herd rests on one bet: a change done by cheap models working task by task behind strong-model guardrails costs less
than one capable agent applying the whole change, with the same quality. Nothing is built yet, so this plan tests the
bet offline, before the orchestrator exists. It replays changes Driving Log has already archived, under several setups
("arms"), and compares cost, time and quality. Once the herd runs, a narrower comparison continues on live changes (see
Measuring what the herd saves in `docs/design.md`): the configured pipeline against one single-agent baseline, which
corresponds to arm A here. The thin-spec arm and the fixed per-task setups exist only in this replay. The metrics (cost
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
  `openspec/changes/<name>/`, and the reference result is the parent of the commit that archived it.
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
| `add-direct-logging` | ~1,300 | feature across layers |
| `add-vehicle-color` | ~2,800 | feature plus migration |
| `add-distance-logging` | ~4,400 | large feature |
| `clean-up-lint-baselines` | ~1,900 (mostly deletions) | chore, checked by lint |

The sizes come from `git diff --shortstat <base> <reference>` and include tests. The pilot uses the first two changes.
The corpus script (below) checks each base and reference again before use, and drops a change whose span holds another
change's implementation.

**Which proposal.** Each run gets the proposal as it was when the change was archived, with every task box unticked.
Earlier versions were often refined during implementation, and handing those over would fail every arm for reasons
that have nothing to do with the arms. The final proposal does carry what was learned while implementing, so absolute
success rates will look better than they'd be live. Every arm gets the same advantage, so the comparison between arms
still holds.

**No peeking.** Each run starts from a fresh repository holding only the base tree, in a single commit with no history.
Without it, an agent could read the answer from `git log`. Driving Log is public, so a model may have seen parts of it
in training. That's a small, shared bias, noted in the results.

## The arms

Phase 1 is cloud models only, and every unit runs through `claude -p`, so the harness is the same in every arm:

| Arm | Setup | Answers |
|---|---|---|
| **A** (baseline) | Opus 5.5 applies the whole change in one session from the full proposal, like `/opsx:apply` | the reference point |
| **B** (herd) | per task: Sonnet 5.5 implements in a fresh session, and Opus 5.5 reviews the commit (accept or revise, up to 3 rounds). Opus finishes with a holistic review | question 1 |
| **C** (cheap) | as B, with Haiku 4.5 implementing. The last round of each task goes to Sonnet (the stronger-attempt rule) | question 2 |
| **D** (guard) | as C, but Opus first writes each task's tests and shows them failing. Haiku can't edit those tests | question 2 |
| **T** (thin spec) | as A, with only the proposal's *Why* and *What Changes* | question 3 |

Phase 2 adds a local implementer on the B70 in arms C and D, through the harness Build plan step 3 picks. Phase 2
also doubles as the local backend's reality check (Build plan step 2).

Each run gets the change's gate as of its base: the final regression run in that base's `CLAUDE.md`. Early bases have
no `codeQuality`. Arms B, C and D also run the gate before each commit, as the herd would. Every run gets the same
timeout: 4 hours for A and T, and per-task limits for B, C and D matching the design's unit timeouts.

## Judging

Judging is blind: the judge doesn't know which arm produced a result. Results are shuffled and their commit trailers
stripped.

1. **The gate**, rerun on a fresh clone of the result. Failing it fails the run.
2. **The tamper check.** Deleted or emptied tests, added skip markers, or changed lint baselines and build files are
   compared with the base. Any of these fails the run unless the proposal asks for it.
3. **The reference tests.** The tests the original change added are copied in and run against the result. Many depend
   on internal names and won't compile, so this is recorded for information only: how many compiled, how many passed.
4. **Holistic review** by Opus 5.5, from the proposal, the specs and the result's diff. It gives an accept or revise
   verdict and lists findings by severity. For arm A, Opus is judging its own model's work, so treat A's review results
   with that bias in mind and lean on steps 1–3 and 5.
5. **The person's spot check**, a sample that checks the automated judging. For each change, you review two results
   blind, picked at random from those that passed steps 1–4, in about 15 minutes each. For each arm's best result on a
   change, you also run the area's Maestro flows on an emulator. This is the only human time the evaluation needs.

A run is **accepted** when it passes the gate and the tamper check and gets an accept with no High findings in the
holistic review. A run you reject in the spot check, or whose Maestro flows fail, becomes not accepted. If you reject
more than one in five of the runs you check, the holistic judging isn't trusted: the spot check widens to every
accepted run of the arms the decision depends on before anything is decided.

## Metrics

Per run:
- **Cost:** from `claude -p --output-format json` (`total_cost_usd`, plus tokens), summed over the run's sessions.
  Local models in phase 2 are priced by GPU time at a rate set before the runs.
- **Wall time** from start to judged result.
- **Gate runs:** the gate is wrapped in a script that logs each call.
- **Outcome:** accepted or not, with the reason.
- **For B, C and D:** per-task first-review acceptance rate, review rounds, and how often the stronger attempt was
  needed.
- **For D:** test disputes, meaning tasks where the implementer couldn't turn the guard's tests green.

Per arm: the success rate, the cost per accepted change (total cost divided by accepted changes), and the median wall
time.

## Running it

0. **The toolchain image.** Write Driving Log's `.herd/toolchain.Dockerfile` (JDK, Android SDK). The manifest already
   names it, so this is onboarding work done early. Check that the gate passes inside it at the oldest and newest base
   in the corpus.
1. **The corpus script** (`eval/corpus.py`). For each change in the table, it records the base, the reference, the
   proposal with its boxes unticked, the gate command and the size, all in `eval/corpus.json`. It also writes the base
   snapshots.
2. **The arm runner** (`eval/run.py <arm> <change> [--rep n]`). It runs one arm on one change in a fresh container,
   records every session's JSON output and the gate log, and leaves the result as a branch in a local results
   repository.
3. **The judge** (`eval/judge.py`): steps 1–4 above, written to `eval/results/<run>.json`.
4. **Pilot.** Run all five arms once each on the two pilot changes. Fix the harness, then estimate the full run's cost
   from the pilot before going on.
5. **Phase 1.** Run every arm twice on every change in the corpus. The two runs show how much results vary between
   attempts.
6. **The spot check**, then the report: `eval/results/phase1.md`, with the tables and the decision below.
7. **Phase 2.** Run C and D with the local implementer, once the B70 passes the reality check's hardware items.

Set a spending cap before each of steps 4, 5 and 7, and stop at it.

## Deciding

The thresholds are set here, before any runs, so the results can't move them.

- **Cheap implementers pay** if C or D accepts at most one change fewer than A does, at no more than half of A's cost
  per accepted change. Build the herd as designed, with tiers.
- **Only the split pays** if B meets that bar but C and D don't. Build the herd as a strong-model pipeline, and drop
  the guard and tier work.
- **Neither pays** if no arm meets it. The herd's value is then only that it runs unattended, saving person-hours rather
  than money. Weigh that against its build cost before going further.
- **The spec pays** if T accepts at least two changes fewer than A. If T comes close to A, then for changes this size
  the detailed spec buys less than it costs, and proposals can stay thin.

The results go into the design: Open questions, and the Build plan's reality check.
