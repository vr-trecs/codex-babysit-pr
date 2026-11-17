# Codex PR Watchdog

Watch a GitHub pull request from its owning local Codex desktop chat. A bundled watcher polls GitHub, groups new review feedback, current-head CI failures and merge conflicts, and wakes the chat for a single triage pass. Ask Codex to report findings or authorize it to repair and push verified fixes to the PR branch.

This is a local macOS integration for same-repository PRs on github.com. The desktop app must stay open and the Mac awake. The watcher uses an internal desktop connection, which may change after an app update. GitHub detection uses polling and your existing `gh` login.

## Publish this repository

Create an empty GitHub repository. Unzip this package and run these commands inside the extracted `codex-pr-watchdog` directory. Replace `OWNER` and the repository name as needed.

```bash
git init -b main
git add .gitignore README.md skills tests
git commit -m "Add Codex PR Watchdog skill"
git remote add origin https://github.com/OWNER/codex-pr-watchdog.git
git push -u origin main
```

## Install and use

Review the scripts in the checkout before installing. Clone into a stable location. Link the skill directory into Codex's user skill path. If a skill of the same name is already installed, inspect it before replacing it.

```bash
git clone https://github.com/vr-trecs/codex-babysit-pr.git
cd codex-babysit-pr
mkdir -p "$HOME/.agents/skills"
ln -s "$(pwd -P)/skills/pr-review-watchdog" "$HOME/.agents/skills/pr-review-watchdog"
gh auth status --hostname github.com
```

Open a **local** Codex desktop chat from the checkout that owns the feature branch. Start a new chat or restart Codex if the skill picker has not refreshed. Try one of these prompts.

> Use $pr-review-watchdog to watch https://github.com/OWNER/REPO/pull/123 in this chat and report actionable findings.

> Use $pr-review-watchdog to watch https://github.com/OWNER/REPO/pull/123. Fix verified in-scope review and CI issues, run the repository's required checks, and push to this PR's feature branch.

> Use $pr-review-watchdog to stop watching https://github.com/OWNER/REPO/pull/123.

The [skill](skills/pr-review-watchdog/SKILL.md) defines how Codex handles a wakeup. The [operations guide](skills/pr-review-watchdog/references/operations.md) covers runtime setup, supported PRs, recovery, and migration from another local watcher.

## Verify

Run the offline tests from this repository root with Python and Node installed. The bridge test uses a local mock socket.

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=skills/pr-review-watchdog/scripts CODEX_MCP_NODE_PATH="$(command -v node)" python3 -m unittest discover -s tests -v
```

For a desktop smoke test, use a disposable PR and start with a report-only watch. Confirm `doctor` and the active binding, then trigger a failing check or have an eligible reviewer leave feedback. Verify the owning chat wakes once. Restart the watcher service and verify unchanged feedback stays quiet. Stop the binding when finished.
