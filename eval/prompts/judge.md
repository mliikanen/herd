Judge an implementation of the OpenSpec change `{change}`.

The change's documents are in openspec/changes/{change}/, as they were proposed before the work; the project's
conventions are in CLAUDE.md. The implementation is `git diff {base}..HEAD`. Decide whether it is ready to merge:
- does it do everything the proposal, design and specs require, and nothing they don't;
- do the tests check the behavior the specs' scenarios describe;
- does it fit the code base and its conventions, without anything left over or duplicated?

The build, the tests and the lint checks are run separately, so don't run them: read the code. You work unattended at
/work/repo, and nobody will answer questions.

Don't change any files. Severity: High for behavior the specs require that is missing or wrong, a bug a user would
hit, lost or corrupted data, or tests that don't check what they claim; Medium for problems a reviewer would ask to fix
before merging; Low for the rest. Accept when there are no High findings. End your answer with one line, exactly:
VERDICT: {"verdict": "accept" or "revise", "findings": [{"severity": "High", "Medium" or "Low", "text": "..."}]}
