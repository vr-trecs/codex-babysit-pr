#!/usr/bin/env node
// The desktop's signed Node runtime owns its connection; Python handles GitHub/state.
import { spawn } from 'node:child_process';
import { createInterface } from 'node:readline';
import { fileURLToPath } from 'node:url';
import { callDesktop, closeDesktop } from './desktop_bridge.mjs';

const worker = spawn(process.env.CODEX_PR_WATCHER_PYTHON || '/usr/bin/python3',
  [fileURLToPath(new URL('./pr_watcher.py', import.meta.url)), ...process.argv.slice(2)],
  { env: { ...process.env, CODEX_PR_WATCHER_BROKER: '1', CODEX_MCP_NODE_PATH: process.execPath },
    stdio: ['pipe', 'pipe', 'inherit'] });
const lines = createInterface({ input: worker.stdout });
lines.on('line', async line => {
  let message;
  try { message = JSON.parse(line); } catch { console.log(line); return; }
  if (!message.desktop_request) { console.log(line); return; }
  let reply;
  try { reply = { result: await callDesktop(message.desktop_request) }; }
  catch { reply = { error: 'Desktop unavailable or incompatible; delivery may be uncertain' }; }
  if (worker.stdin.writable) worker.stdin.write(JSON.stringify(reply) + '\n');
});
worker.on('error', () => { console.error('PR watcher worker unavailable'); process.exitCode = 1; });
worker.on('exit', code => { closeDesktop(); process.exitCode = code ?? 1; });
for (const signal of ['SIGTERM', 'SIGINT']) process.on(signal, () => worker.kill(signal));
