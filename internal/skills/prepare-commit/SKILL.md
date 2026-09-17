---
name: prepare-commit
description: >
  Prepares a commit: works out what changed, writes the commit message, and
  stages and commits only with approval. Use when asked to commit, to write a
  commit message, or to get changes ready to push.
tier: cheap
escalation: never
tools: [git_status, git_diff, git_branch_info, read_file, git_stage, git_commit]
---

# Goal

Produce a commit message that describes the change accurately, and stage exactly
the files that belong in it.

# Workflow

1. `git_status` and `git_diff` to establish what actually changed. Never write a
   message from the task description alone.
2. Group the changes. If the diff contains more than one logical change, say so
   and propose splitting it rather than writing a message with "and" in it.
3. Draft the message:
   - subject: imperative mood, under 72 characters, no trailing full stop
   - blank line
   - body: what changed and why, wrapped at 72 columns. The diff already says
     how.
4. Show the message and the exact file list, then ask.
5. `git_stage` with explicit paths, then `git_commit`. Both require approval and
   will be refused if repository policy disables commits.

# Why this skill never escalates

This is the finalise phase. Staging and committing are refused inside any
attempt that could still be discarded and retried on another tier, so a skill
that stages or commits must be terminal by construction. If the cheap tier
cannot write a decent commit message, that is a result to report, not a reason
to redo the commit on a bigger model.

# Guardrails

- Never stage `.`, `-A` or `--all`. Name the files.
- Never include build output, run logs, editor files or anything under the
  configured run directory.
- Never commit while the build or tests are known to be failing without saying
  so in the report.
- Never push. There is no push tool, and there will not be one.
