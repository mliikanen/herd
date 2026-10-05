# Reviewing herd

This repository is a design (`docs/design.md`) and user guide (`docs/using-the-herd.md`) for **herd**, a pipeline that
takes an OpenSpec proposal and turns it into a reviewed pull request without anyone attending it. It is a design, not
a specification: review it for whether it would work and stay safe, not for completeness.

## Intent to review against

- **Scale.** One operator, one home Linux host, a handful of projects, one to three worker slots, at most one or two
  rented GPU machines, one monthly budget. Not multi-tenant, not a service, not a fleet.
- **Threat model.** Agents and the code they write are untrusted: they must not reach credentials, `main`, the
  herd's own configuration, or anything outside their unit. The operator is trusted, and so are their host config,
  the project's maintainers and the default branch. Cloud and rental providers are trusted with data the way any cloud
  API is. A project's CI may be buggy, but it isn't hostile.
- **Shape.** The orchestrator is plain code with no model. It derives every change's state from git and GitHub, with
  a few small operational stores. A person stays in the loop at proposing, PR review and merging, and at explicit
  `needs-human` stops.
- **Simplicity.** A simpler design that's good enough for one operator is better than a complete one. When in doubt,
  escalating to a person beats adding a new mechanism.

## What to flag

Flag only problems that break the intent above:

- **Security:** an agent reaching a credential, the default branch, guarded files or the herd's config; forged test
  results or verdicts; a sandbox escape.
- **Liveness:** a state the change can never leave, an endless loop, or work repeated forever.
- **Silent loss:** spend that goes uncounted with no alert, or a person's decision that gets lost.
- **Contradictions:** two sections that can't both be implemented.

Start each finding with its severity: **High** (one of the above), **Medium** (a real gap that a person would notice and
could work around), or **Low** (wording, consistency). Give at most a few Medium or Low findings per review, and only
when they're cheap to fix.

## What not to flag

- Edge cases that only matter at a scale or under a threat model the intent rules out (a hostile operator, many
  tenants, adversarial providers, exact billing).
- Implementation details that are cheaper to settle in code: file names, encodings, API endpoints, exact ordering.
  The design leaves these open on purpose.
- Missing mechanisms. Don't propose a new subsystem, record type or state to cover a gap. Say what goes wrong, and
  prefer fixes that remove or simplify text.
- Unchanged text, unless it's High. Medium and Low items in unchanged text are tracked in issues labeled
  `design-backlog`; don't re-raise them.

## Style

Be brief. One finding per problem, with a concrete failure scenario (inputs, then what goes wrong). If a change makes
the design simpler without breaking the intent, that's a good change: say so rather than asking for more.
