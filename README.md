# herd

herd turns an [OpenSpec](https://github.com/Fission-AI/OpenSpec) proposal into a reviewed, merge-ready pull request
without anyone attending it. You write the proposal and mark it ready. A herd of coding agents implements it task by
task, reviews each task, tests the change end to end, answers the PR's reviewers and archives the spec delta. You
review and merge the result.

> **Status: design.** This repository holds the design and the user guide. Nothing is installable yet: the steps below
> describe the herd as designed, and the [build plan](docs/design.md#build-plan) is the order it gets built in.
> [Driving Log](https://github.com/mliikanen/driving-log), a Kotlin Multiplatform app, is the first project it will run.

## Goals

- **Your time goes where judgment is needed.** You spend it proposing a change, reviewing its PR (including the
  project's own check, if it has one, such as a test on a real device) and merging it. Implementing, reviewing each
  task, fixing review feedback, testing and archiving run unattended.
- **Stuck work stops instead of looping.** A change the herd can't finish on its own stops at `needs-human`, with the
  reason and a way to resolve it.
- **Agents are untrusted.** Workers run in disposable containers with no credentials. They can't push, can't reach the
  default branch, and can't change the herd's configuration or the files that decide what's tested. A proxy holds every
  API key and meters every paid call against a monthly budget.
- **A red/green loop around the agents.** For projects with end-to-end tests that can run on an emulator, and once the
  host has emulator capacity, the herd runs the tests relevant to each task while implementing it, and a test the task
  adds or changes has to fail before the change and pass after it. CI runs the full suite as a second net.
- **Simple, recoverable state.** The orchestrator is plain code with no model. It works out every change's state from
  git and GitHub on each pass, so a crash or a reboot loses at most a task's unpushed work.
- **Any project, any model.** Project specifics live in the project's `.herd/` manifest. Each worker slot can run a
  cloud model, a local one or one on a rented GPU, mixed on one host.

## Requirements

**The host**
- A Linux machine you control, with [Podman](https://podman.io/) for rootless containers and systemd user services.
- [herdr](https://herdr.dev), the terminal workspace you watch and steer the herd from.
- A GitHub App for the herd. It needs read and write on *Contents*, *Pull requests* and *Commit statuses*, and read
  only on *Checks*, *Actions* and *Administration*. It's installed on the repositories the herd works on.
- At least one model backend. The default is a cloud API key, such as Anthropic's for Claude. A local model server on
  a GPU and rented GPU machines are optional.
- Optional: `/dev/kvm`, for running a project's end-to-end tests on an emulator.

**Each project**
- Uses OpenSpec, with changes in `openspec/changes/`.
- Is hosted on GitHub, with a **PR-only default branch** for everyone (no bypass), up-to-date branches before merging,
  and required CI checks that run the project's gate on every push to a PR.
- Has a gate (build, lint, unit tests) that runs headless in a Linux container. Anything that can't run there is
  either the herd's own emulator run, a person's check in PR review, or a capability the project declares as missing.

## Getting started

Once the herd is built, setting it up on a host is:

1. **Run the install script** (as root). It creates the herd's own system user and directories, installs the `herd`
   CLI and the herd's systemd units, and checks that herdr is installed.
2. **Create the GitHub App** with the permissions above, and put its private key and your model API keys in the herd
   user's secrets directory (`~herd/secrets/`, readable by that user only).
3. **Write the host config** in `/etc/herd/config.yaml`: your model backends, the worker slots that use them, the
   monthly budget and where alerts go (desktop, phone push). See [Models](docs/design.md#models) and
   [Monitoring](docs/design.md#monitoring).
4. **Run `herd`.** It starts herdr, or attaches to it, with a status pane listing every change in flight, and opens a
   workspace per project with your planner agent in it.

## Enrolling a project

1. **`herd init`** in your checkout of the project. It detects the project's build tool, writes a `.herd/` manifest
   with defaults (the gate, a toolchain image, guarded paths, review settings), installs the planner skills
   (`/herd-propose`, `/herd-ready`, `/herd-resolve`) and registers the project with the herd. Review the files and land
   them by PR.
2. **Set up CI and branch protection.** CI runs the gate on every PR as a required check, and the default branch is
   PR-only with no bypass.
3. **Install the herd's GitHub App** on the repository. Until then the project shows as inactive with
   "App not installed".
4. **Move existing proposals onto change branches.** Each change lives on its own `change/<name>` branch from its
   first commit, so the default branch's `openspec/changes/` holds only `archive/`.
5. **Write the project's workflow doc**: what a ready change may need, and the project's final checks. Point your
   agent's instructions (`CLAUDE.md` or `AGENTS.md`) at it.
6. **Run `herd doctor <project>`** until it passes. It runs the gate in a real worker, checks branch protection and the
   CI triggers, and checks that the model backends answer.
7. **Smoke-test it** with one small, low-risk proposal before trusting it with a real queue.

To register a project that already has `.herd/`, on a second host say, run `herd add <repo-url>`.
`herd pause|resume <project>` stops and restarts new work, and `herd remove <project>` unregisters it. Its branches and
PRs stay on GitHub, so registering it again picks up where it stopped.

After that, the everyday loop is short: `/herd-propose` drafts a change on its branch, `/herd-ready` hands it to the
herd, and `/herd-resolve` answers a stop. See [Using the herd](docs/using-the-herd.md).

## Documentation

- [Using the herd](docs/using-the-herd.md): the life of a change, working in herdr, when the herd asks for you, and
  merging.
- [Design](docs/design.md): how the herd works and why. It's the reference for the state machine, containers, models,
  security and monitoring.
- [Implementation notes](docs/implementation-notes.md): detail worked out during design review, kept as a starting
  point for the code.

## License

[MIT](LICENSE)
