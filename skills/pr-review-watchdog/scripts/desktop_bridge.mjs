// Use the installed desktop host; never open another app-server writer.
import net from 'node:net';
import fs from 'node:fs';
import { randomUUID } from 'node:crypto';

const MAX = 8 * 1024 * 1024;
const allowed = new Set(['read_thread', 'list_artifacts', 'send_message_to_thread']);
const clients = new Map();

function client(pipe) {
  if (clients.has(pipe)) return clients.get(pipe);
  const socket = net.createConnection(pipe);
  let nextId = 0;
  let closed = false;
  const pending = new Map();
  const ready = new Promise((resolve, reject) => {
    socket.once('connect', resolve);
    socket.once('error', reject);
  });
  const value = {
    async request(method, params, timeout) {
      await ready;
      const id = ++nextId;
      return new Promise((resolve, reject) => {
        const timer = setTimeout(() => fail(new Error('desktop timeout')), timeout);
        pending.set(id, { resolve, reject, timer });
        const body = Buffer.from(JSON.stringify({ jsonrpc: '2.0', id, method, params }));
        if (body.length > MAX) return fail(new Error('desktop request too large'));
        const header = Buffer.alloc(4);
        header.writeUInt32LE(body.length);
        socket.write(Buffer.concat([header, body]));
      });
    },
    close() { fail(new Error('desktop client closed')); },
  };
  function fail(error) {
    if (closed) return;
    closed = true;
    if (clients.get(pipe) === value) clients.delete(pipe);
    for (const p of pending.values()) { clearTimeout(p.timer); p.reject(error); }
    pending.clear();
    socket.destroy();
  }
  clients.set(pipe, value);
  socket.on('error', fail);
  socket.on('end', () => fail(new Error('desktop disconnected')));
  socket.on('close', () => fail(new Error('desktop closed')));
    let buffer = Buffer.alloc(0);
    socket.on('data', chunk => {
      buffer = Buffer.concat([buffer, chunk]);
      while (buffer.length >= 4) {
        const length = buffer.readUInt32LE();
        if (length > MAX) return fail(new Error('desktop frame too large'));
        if (buffer.length < length + 4) return;
        try {
          const reply = JSON.parse(buffer.subarray(4, length + 4));
          buffer = buffer.subarray(length + 4);
          const p = pending.get(reply.id);
          if (!p || reply.jsonrpc !== '2.0') return fail(new Error('invalid desktop response'));
          pending.delete(reply.id);
          clearTimeout(p.timer);
          if (reply.error || !('result' in reply)) p.reject(new Error('desktop rejected request'));
          else p.resolve(reply.result);
        } catch {
          return fail(new Error('invalid desktop response'));
        }
      }
    });
  return value;
}

export function request(pipe, method, params, timeout = 15000) {
  return client(pipe).request(method, params, timeout);
}
export function closeDesktop() { for (const c of [...clients.values()]) c.close(); }

export async function callDesktop(input) {
  if (!allowed.has(input.tool) || !/^[a-f0-9-]{36}$/.test(input.threadId)) {
    throw new Error('invalid desktop operation');
  }
  const info = fs.lstatSync(input.pipe);
  if (!info.isSocket() || info.uid !== process.getuid() || (info.mode & 0o077)) {
    throw new Error('desktop socket must be private and owned by this user');
  }
  // Discover the namespace and validate availability against this installed build.
  const catalog = await request(input.pipe, 'tools/list', { threadStartKind: 'all' });
  const tool = catalog.tools?.find(t => t.name === input.tool);
  if (!tool || tool.namespace !== 'codex_app') throw new Error('desktop tool unavailable');
  const result = await request(input.pipe, 'tools/call', {
    arguments: input.tool === 'list_artifacts' ? {} : { ...input.args, threadId: input.threadId, hostId: 'local' },
    callerSource: 'codex', namespace: tool.namespace, threadId: input.threadId,
    tool: input.tool, callId: `mcp-call-${randomUUID()}`, turnId: `mcp-turn-${randomUUID()}`,
  });
  if (result.success !== true || !Array.isArray(result.contentItems)) {
    throw new Error('desktop tool failed');
  }
  const texts = result.contentItems.filter(i => i.type === 'inputText');
  if (texts.length !== 1) throw new Error('unexpected desktop response');
  return JSON.parse(texts[0].text);
}

if (process.argv[1] && import.meta.url === new URL(`file://${process.argv[1]}`).href) {
  callDesktop(JSON.parse(fs.readFileSync(0, 'utf8')))
    .then(value => console.log(JSON.stringify(value)))
    .catch(() => { console.error('Desktop request failed; delivery may be uncertain.'); process.exitCode = 1; })
    .finally(closeDesktop);
}
