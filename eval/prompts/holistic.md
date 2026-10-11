Review the whole implementation of the OpenSpec change `{change}`.

The change's documents are in openspec/changes/{change}/; the project's conventions are in CLAUDE.md. See the work
with `git diff {base}..HEAD` and `git log {base}..HEAD`. It was done task by task, and each task was reviewed; look at
the change as a whole: is everything the specs require there, does it fit together, is anything left over or
duplicated, and do the tests cover the requirements' scenarios?

Don't change any files. Accept when there are no High or Medium findings. End your answer with one line, exactly:
VERDICT: {"verdict": "accept" or "revise", "findings": [{"severity": "High", "Medium" or "Low", "text": "..."}]}

{environment}
