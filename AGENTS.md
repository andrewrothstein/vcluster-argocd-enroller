# Agent Instructions

This project uses **bd** (beads) for issue tracking. Run `bd onboard` to get started.

## Quick Reference

```bash
bd ready              # Find available work
bd show <id>          # View issue details
bd update <id> --status in_progress  # Claim work
bd close <id>         # Complete work
bd sync               # Sync with git
```

## Session Completion

When ending a work session, complete ALL steps below. Work is NOT complete until `git push` succeeds.

1. File issues for remaining work
2. Run quality gates if code changed (tests, linters, builds)
3. Update issue status — close finished work, update in-progress items
4. Push to remote:
   ```bash
   git pull --rebase
   bd sync
   git push
   git status  # Must show "up to date with origin"
   ```
5. Clean up — clear stashes, prune remote branches
