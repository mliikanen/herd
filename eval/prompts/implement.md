Implement one task of the OpenSpec change `{change}`:

> {task_id} {task_text}

Read the project's CLAUDE.md and the change's documents in openspec/changes/{change}/ for context. Earlier tasks are
done (see `git log`); later ones are someone else's. Do only this task, with tests where it has behavior to test, tick
it in tasks.md, and commit with a message starting `{task_id}: `. The orchestrator runs the gate after you, and a
reviewer checks your commit.
{guard}{feedback}
{environment}
