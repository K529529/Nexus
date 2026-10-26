// Sequential JSONL RPC. Only trusted host configuration reaches initialize().
// Exported serve(manager) permits protocol tests with a visibly separate fake manager.
import { spawn } from 'node:child_process';
import { createInterface } from 'node:readline';
import { resolve, relative, isAbsolute, join } from 'node:path';
import { realpath } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';

const MAX_FRAME = 5 * 1024 * 1024;
const inside = (path, root) => {
  const rel = relative(root, path);
  return rel === '' || (!rel.startsWith('..') && !isAbsolute(rel));
};

export async function serve(manager) {
  const send = value => new Promise((ok, fail) => {
    process.stdout.write(JSON.stringify(value) + '\n', error => error ? fail(error) : ok());
  });
  let config;
  let active;
  let initializing = false;
  let executing = false;
  let closing = false;
  const seen = new Set();
  const error = (id, code, state = 'not_executed') => send({id, type: 'error', code, state});

  async function stop(job) {
    job.cancelled = true;
    job.controller.abort();
    if (!job.child) return;
    if (job.patch && !job.child.stdin.destroyed) {
      job.child.stdin.write(JSON.stringify({id: job.id, method: 'cancel'}) + '\n');
      // Worker cancellation at its per-file persistence barrier preserves partial facts.
      await Promise.race([job.closed, new Promise(r => setTimeout(r, 200))]);
    }
    if (job.child.exitCode !== null) return;
    if (process.platform === 'win32') {
      await new Promise(resolveStop => {
        const killer = spawn(join(process.env.SystemRoot, 'System32', 'taskkill.exe'),
          ['/PID', String(job.child.pid), '/T', '/F'], {shell: false, windowsHide: true,
            stdio: 'ignore'});
        const timer = setTimeout(() => { killer.kill(); resolveStop(); }, 2000);
        killer.on('error', () => { clearTimeout(timer); resolveStop(); });
        killer.on('close', code => {
          clearTimeout(timer);
          if (code !== 0 && job.child.exitCode === null) job.cleanupIncomplete = true;
          resolveStop();
        });
      });
    } else {
      try { process.kill(-job.child.pid, 'SIGKILL'); }
      catch (e) { if (e.code !== 'ESRCH') job.cleanupIncomplete = true; }
    }
    await Promise.race([job.closed, new Promise(r => setTimeout(() => {
      if (job.child.exitCode === null && job.child.signalCode === null) {
        job.cleanupIncomplete = true;
        job.child.kill('SIGKILL');
      }
      r();
    }, 2000))]);
  }

  async function execute(row) {
    const p = row.params;
    const patch = p.name === 'apply_patch';
    if (!patch && p.name !== 'exec_command') return error(row.id, 'unsupported_tool');
    if (patch && config.mode === 'read-only') return error(row.id, 'policy_denied');
    if (resolve(p.workspace) !== config.workspace) return error(row.id, 'workspace_mismatch');
    const args = p.arguments;
    const command = args.command ?? args.cmd;
    const timeout = args.timeout_ms ?? 120000;
    if (!patch && (typeof command !== 'string' || !command.trim()
        || (args.command !== undefined && args.cmd !== undefined)
        || Object.keys(args).some(k => !['command', 'cmd', 'workdir', 'timeout_ms'].includes(k))
        || (args.workdir !== undefined && (typeof args.workdir !== 'string' || !args.workdir))
        || !Number.isInteger(timeout) || timeout < 1 || timeout > 600000)) {
      return error(row.id, 'invalid_arguments');
    }
    const cwd = await realpath(resolve(config.workspace, patch ? '.' : (args.workdir ?? '.')));
    const job = {id: row.id, patch, controller: new AbortController(), child: null,
      cancelled: false, cleanupIncomplete: false, timedOut: false, closed: null};
    active = job;
    let workerResult;
    let timer;
    try {
      const shell = patch ? {exe: config.python, args: ['-I', config.worker]} :
        {exe: p.shell, args: ['-NoProfile', '-NonInteractive', '-Command']};
      const text = patch ? '' :
        '[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new(); ' +
        '$OutputEncoding = [Console]::OutputEncoding; ' + command;
      timer = setTimeout(() => { job.timedOut = true; void stop(job); }, timeout);
      const wrapped = await manager.wrapWithSandboxArgv(
        text, shell, undefined, job.controller.signal, cwd, {commandId: row.id});
      if (job.cancelled) return error(row.id, 'cancelled');
      const child = spawn(wrapped.argv[0], wrapped.argv.slice(1), {
        shell: false, cwd, env: wrapped.env, windowsHide: true,
        detached: process.platform !== 'win32', stdio: ['pipe', 'pipe', 'pipe'],
      });
      job.child = child;
      job.closed = new Promise((ok, fail) => {
        child.once('error', fail);
        child.once('close', (code, signal) => ok({code, signal}));
      });
      // A rejected pipe must not become an uncaught event or a retry.
      child.stdin.on('error', () => { job.cleanupIncomplete = true; });
      const drain = async (pipe, stream) => {
        for await (const chunk of pipe) {
          await send({id: row.id, type: 'output', stream, data: chunk.toString('base64')});
        }
      };
      const readWorker = async () => {
        const lines = createInterface({input: child.stdout, crlfDelay: Infinity});
        for await (const line of lines) {
          if (Buffer.byteLength(line) > MAX_FRAME) throw new Error('worker_frame_too_large');
          const message = JSON.parse(line);
          if (message.id !== row.id) throw new Error('worker_correlation');
          if (message.type === 'event' && message.kind === 'patch_diff'
              && message.data.call_id === p.call_id) await send(message);
          else if (message.type === 'result' && !workerResult) workerResult = message;
          else throw new Error('invalid_worker_frame');
        }
      };
      if (patch) child.stdin.write(JSON.stringify({id: row.id, ...p}) + '\n');
      else child.stdin.end();
      const [exit] = await Promise.all([job.closed, patch ? readWorker() :
        drain(child.stdout, 'stdout'), drain(child.stderr, 'stderr')]);
      if (patch) {
        if (workerResult && !job.cleanupIncomplete) await send(workerResult);
        else await error(row.id, 'cleanup_incomplete', 'interrupted_unknown');
      } else {
        const code = job.cleanupIncomplete ? 'cleanup_incomplete' : job.timedOut ?
          'command_timeout' : job.cancelled ? 'cancelled' :
          exit.code !== 0 ? 'command_exit_nonzero' : null;
        await send({id: row.id, type: 'result', result: {exit_code: exit.code,
          timed_out: job.timedOut, cancelled: job.cancelled,
          cleanup_incomplete: job.cleanupIncomplete, error_code: code}});
      }
    } catch {
      await stop(job);
      await error(row.id, job.child ? 'cleanup_incomplete' : 'backend_unavailable',
        job.child ? 'interrupted_unknown' : 'not_executed');
    } finally {
      clearTimeout(timer);
      active = undefined;
    }
  }

  async function shutdown() {
    if (closing) return;
    closing = true;
    if (active) await stop(active);
    if (manager && config) await manager.reset();
    process.exitCode = 0;
    process.stdin.destroy();
  }
  const input = createInterface({input: process.stdin, crlfDelay: Infinity});
  // Bound the incoming byte stream before readline allocates an unlimited line.
  let bytes = 0;
  process.stdin.on('data', chunk => {
    for (const byte of chunk) {
      bytes = byte === 10 ? 0 : bytes + 1;
      if (bytes > MAX_FRAME) { void shutdown(); break; }
    }
  });
  input.on('line', line => {
    void (async () => {
      let row;
      try { row = JSON.parse(line); } catch { await shutdown(); return; }
      if (!row || typeof row.id !== 'string' || !row.id || closing) return;
      if (row.method === 'shutdown') { await shutdown(); return; }
      if (row.method === 'cancel' && active?.id === row.id) { await stop(active); return; }
      if (row.method === 'ack' && active?.id === row.id && active.patch) {
        if (!active.child.stdin.destroyed) active.child.stdin.write(line + '\n');
        return;
      }
      if (seen.has(row.id) || executing || initializing) {
        await error(row.id, 'bridge_busy'); return;
      }
      seen.add(row.id);
      if (row.method === 'initialize') {
        if (!manager) { await error(row.id, 'backend_blocked'); return; }
        if (config) { await error(row.id, 'already_initialized'); return; }
        initializing = true;
        try {
          const p = row.params;
          if (!['read-only', 'workspace-write'].includes(p.mode)) throw new Error('mode');
          const workspace = await realpath(p.workspace);
          const roots = p.mode === 'workspace-write' ?
            [workspace, ...await Promise.all((p.writable_roots ?? []).map(r => realpath(r)))] : [];
          const worker = await realpath(fileURLToPath(new URL('./patch_worker.py', import.meta.url)));
          const python = await realpath(p.python);
          // Source checkout inside a writable root is not a trusted Worker installation.
          if (roots.some(root => inside(worker, root) || inside(python, root))) {
            throw new Error('untrusted_worker_installation');
          }
          await manager.initialize({
            network: {allowedDomains: [], deniedDomains: [], allowLocalBinding: false},
            filesystem: {denyRead: [], allowWrite: roots, denyWrite: [
              resolve(worker, '../../..'), python,
              ...roots.flatMap(root => ['.git', '.nexus'].map(name => join(root, name))),
            ]},
          });
          config = {mode: p.mode, workspace, worker, python};
          await send({id: row.id, type: 'result', result: {initialized: true}});
        } catch {
          await manager.reset();
          await error(row.id, 'initialization_failed');
        } finally { initializing = false; }
      } else if (row.method === 'execute' && config) {
        executing = true;
        try { await execute(row); }
        finally { executing = false; }
      } else await error(row.id, 'backend_unavailable');
    })().catch(async () => { await shutdown(); });
  });
  input.on('close', () => { void shutdown(); });
}

// Acceptance gate is compiled into the production entry, not a model/env/config flag.
if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  await serve(null);
}
