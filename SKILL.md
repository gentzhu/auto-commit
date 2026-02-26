---
name: auto-commit
description: Automatically compare git file diffs, generate Chinese commit description fields (type, scope, theme, intro), map to conventional types (feat/fix/refactor/docs/style/test/chore/perf/ci/build/revert), and commit changes. Use when users ask for one-command auto commit, Chinese commit summaries, or automatic commit message generation from changed files.
---

# Diff to Chinese Auto Commit

## Overview

Read repository changes, analyze staged diffs, generate Chinese commit descriptors, and commit automatically.
Generated fields always include:
- `type` (one of `feat|fix|refactor|docs|style|test|chore|perf|ci|build|revert`)
- `scope`
- `theme`
- `intro`

Default behavior runs `git add -A` before generating and committing.

## Workflow

1. Run `scripts/auto_commit_from_diff.py` in the target repository.
2. Stage changes automatically (unless `--no-stage`).
3. Infer commit type/scope/theme/intro from staged diff.
4. Print the generated Chinese description.
5. Commit with Conventional Commit header and Chinese body (unless `--dry-run`).

## Commands

```bash
# Stage all + generate Chinese descriptors + commit
python scripts/auto_commit_from_diff.py

# Preview only, no commit
python scripts/auto_commit_from_diff.py --dry-run

# Override any generated field
python scripts/auto_commit_from_diff.py --type fix --scope api --theme "Fix API validation" --intro "Repair null-input validation gap."

# Run on a specific repository
python scripts/auto_commit_from_diff.py --repo /path/to/repo
```

## Arguments

- Default behavior: run `git add -A`
- `--no-stage`: do not stage automatically
- `--dry-run`: print generated description only
- `--no-verify`: append `--no-verify` to `git commit`
- `--type`: force one supported type
- `--scope`, `--theme`, `--intro`: override inferred values

## Failure Handling

- No repository changes: exit without commit
- No staged changes: exit with error
- Git failure: print git error and exit non-zero
