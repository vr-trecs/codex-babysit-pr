# Watcher operations

## Requirements

Run from a local Codex desktop chat on macOS. Use a same-repository PR on github.com and exactly one checkout in the chat whose current branch and `origin` match the PR. The watcher needs Git, Python 3.9 or newer, an authenticated GitHub CLI with `gh api --paginate --slurp`, and the signed desktop Node runtime. It reads GitHub through the developer's existing `gh` login. Keep the desktop app open and the Mac awake. Fork PRs, cloud chats, GitHub Enterprise Server and unattended service hosts are outside this release's supported scope.

The desktop bridge uses an internal app-tools connection. Compatibility can change after a desktop update. Use `doctor` from the chat before starting. Try the runtime in `CODEX_MCP_NODE_PATH` first. If unavailable or incompatible, locate the installed app's `Contents/Resources/cua_node/bin/node` and use that signed binary. The default Python is `/usr/bin/python3`. Set `CODEX_PR_WATCHER_PYTHON` to an absolute interpreter path if needed.

## Commands

Set `watcher_script` to the absolute path of `scripts/pr_watcher.mjs` inside the installed skill. Use absolute paths in each executor call if your shell sessions do not share variables. `CODEX_THREAD_ID` defaults to the current chat ID in the desktop executor. If it is unset, resolve the exact local chat ID through desktop tools and pass `--thread`.

```bash
"$CODEX_MCP_NODE_PATH" "$watcher_script" doctor --thread "$CODEX_THREAD_ID"
"$CODEX_MCP_NODE_PATH" "$watcher_script" status
"$CODEX_MCP_NODE_PATH" "$watcher_script" watch --pr "$PR_URL" --thread "$CODEX_THREAD_ID"
"$CODEX_MCP_NODE_PATH" "$watcher_script" run
```

Launch `run` through the app-owned command executor with a short yield and retain the returned command session. Start it only when a service is absent. Verify `status` shows `running: true` and the expected binding is active with no error. `doctor` verifies that the transport can read the local chat. It does not send a message. A detached service such as `launchd` cannot use the verified desktop connection.

```bash
"$CODEX_MCP_NODE_PATH" "$watcher_script" inspect --pr "$PR_URL"
"$CODEX_MCP_NODE_PATH" "$watcher_script" stop --pr "$PR_URL"
```

`inspect` is a read-only snapshot. `stop` stops one PR binding and retains its delivery history. A merged or closed PR becomes terminal when scanned. If the app or executor exits, resume from the owning chat with `doctor`, `status`, and `run`. The state survives a service restart.

## Detection and delivery

Every scan checks eligible new or edited review text, current-head failures, and confirmed conflicts. Existing eligible feedback can trigger the first wakeup. Passing checks and approvals without new text do not trigger a wakeup. Reads are paginated and use your GitHub rate limit. The default pause between scans is 30 seconds. Events are coalesced while the chat is busy.

Private state in `~/.codex/pr-watcher/` contains PR and chat bindings, checkout paths, and event IDs and hashes. The state stores no GitHub token or review text. A local service lock prevents two copies from scanning the same state. Keep a single owner for each watched PR across teammates and machines. If replacing an earlier local watcher, wind down its active repair pass and stop its service before launching this copy with the same ledger. An existing healthy identical binding can be reused.

The watcher persists the batch before sending and confirms receipt from the chat transcript. If status reports `delivery-uncertain`, check the exact marker with `doctor --thread CHAT_ID --receipt '[local-pr-watcher:BATCH_ID]'`. Let the service reconcile a confirmed receipt on its next scan. If the receipt cannot be confirmed, report it and preserve the ledger. A later scan does not resend an ambiguous delivery automatically.
