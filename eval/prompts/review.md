Review one task's implementation in the OpenSpec change `{change}`:

> {task_id} {task_text}

The change's documents are in openspec/changes/{change}/; the project's conventions are in CLAUDE.md. See the work with
`git diff {task_base}..HEAD` and `git log {task_base}..HEAD`. The gate has passed on it. Check that:
- the task is done as the documents specify, and ticked;
- nothing outside the task changed;
- the tests check the behavior, and none were deleted, emptied, skipped or weakened, and no lint baseline grew.

Don't change any files. Accept when there are no High or Medium findings. End your answer with one line, exactly:
VERDICT: {"verdict": "accept" or "revise", "findings": [{"severity": "High", "Medium" or "Low", "text": "..."}]}

{environment}
