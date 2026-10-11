Write the tests for one task of the OpenSpec change `{change}`, before anyone implements it:

> {task_id} {task_text}

Read the project's CLAUDE.md, its test strategy if it has one, and the change's documents in openspec/changes/{change}/.
Write tests that check the task's behavior as the change's specs and design define it, following the project's test
conventions. Run them and show that they fail for the right reason: the behavior is missing. A compile failure on names
the task will add counts, if the design names them. Commit only test files, with a message starting
`guard {task_id}: `. The implementer can't change your tests, so don't pin details the documents leave open.

If the task has no behavior a test can check (documentation, Maestro flows, configuration, a manual step), commit
nothing and say so.

{environment}
