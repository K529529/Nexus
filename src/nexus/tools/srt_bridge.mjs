// Sequential JSONL RPC. Only trusted host configuration reaches initialize().
// Exported serve(manager) permits protocol tests with a visibly separate fake manager.
import { spawn } from 'node:child_process';
import { createInterface } from 'node:readline';
import { resolve, relative, isAbsolute, join } from 'node:path';
import { realpath, mkdtemp, writeFile, unlink, rmdir } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { fileURLToPath } from 'node:url';
import { loadSRT } from './srt_adapter.mjs';

const MAX_FRAME = 5 * 1024 * 1024;
const inside = (path, root) => {
  const rel = relative(root, path);
  return rel === '' || (!rel.startsWith('..') && !isAbsolute(rel));
};

// These paths already expose OS-managed executables through Windows read/execute
// ACLs. SRT allowRead/denyWrite both MUTATE ACLs; neither belongs on these trees.
async function systemDirectories() {
  if (process.platform !== 'win32') return [];
  const paths = ['SystemRoot', 'windir', 'ProgramFiles', 'ProgramFiles(x86)', 'ProgramW6432']
    .map(key => process.env[key]).filter(Boolean);
  const roots = await Promise.all(paths.map(p => realpath(p)));
  if (!roots.length) throw new Error('Windows system directories unavailable');
  return [...new Set(roots)];
}

export function filesystemPolicy({workspace, roots, readRoots, worker, python, ipcDirectory},
  systemRoots) {
  // A workspace/grant containing a system tree is equally unsafe: ACL inheritance
  // would reach the protected directory even if its literal path was filtered out.
  for (const path of [workspace, ...roots, ipcDirectory]) {
    if (systemRoots.some(root => inside(path, root) || inside(root, path))) {
      throw new Error('System directory cannot be an SRT workspace/write/IPC root: ' + path);
    }
  }
  const mutableAclTargets = paths => [...new Set(paths.filter(path => {
    if (systemRoots.some(root => inside(path, root))) return false;
    if (systemRoots.some(root => inside(root, path))) {
      throw new Error('Runtime ACL root contains a Windows system directory: ' + path);
    }
    return true;
  }))];
  return {
    denyRead: [],
    allowRead: [workspace, ...mutableAclTargets(readRoots), ipcDirectory],
    allowWrite: roots,
    denyWrite: mutableAclTargets([
      resolve(worker, '../../..'), python, ...readRoots, ipcDirectory,
      ...roots.flatMap(root => ['.git/config', '.git/hooks', '.git/config.worktree',
        '.gitmodules', '.nexus'].map(name => join(root, name))),
    ]),
  };
}

export async function serve(manager, packageRoot) {
  const nativeSRT = Boolean(packageRoot);
  const send = value => new Promise((ok, fail) => {
    process.stdout.write(JSON.stringify(value) + '\n', error => error ? fail(error) : ok());
  });
  let config;
  let active;
  let initializing = false;
  let executing = false;
  let pendingExecution;
  let closing = false;
  let ipcDirectory;
  const seen = new Set();
  const failure = (id, code, state = 'not_executed', detail = '') =>
    ({id, type: 'error', code, state, detail: String(detail).slice(0, 4000)});
  const error = (...args) => send(failure(...args));

  async function acknowledge(job, value) {
    job.ackWrite = (job.ackWrite ?? Promise.resolve()).then(() =>
      writeFile(job.ackPath, JSON.stringify(value)));
    await job.ackWrite;
  }

  async function stop(job) {
    // A result may already have reached Python while execute() removes IPC files.
    // Do not recreate the completed worker's ACK mailbox during shutdown.
    if (job.finished) return;
    job.cancelled = true;
    job.controller.abort();
    if (!job.child) return;
    if (job.patch && job.ackPath) {
      await acknowledge(job, {id: job.id, method: 'cancel'});
      // Worker cancellation at its per-file persistence barrier preserves partial facts.
      await Promise.race([job.closed, new Promise(r => setTimeout(r, 200))]);
    }
    if (job.finished) return;
    if (nativeSRT) {
      // srt-win watches --held-by (this bridge PID) and closes its native job
      // when the holder exits. The host cannot taskkill the other account's tree.
      // Python must close this bridge before returning cancellation to the caller.
      job.requiresReset = true;
      if (job.child.exitCode === null) job.child.kill('SIGKILL');
    } else if (process.platform === 'win32') {
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
      if (!job.finished) {
        job.cleanupIncomplete = true;
        if (job.child.exitCode === null) job.child.kill('SIGKILL');
        job.child.stdin.destroy();
        job.child.stdout.destroy();
        job.child.stderr.destroy();
      }
      r();
    }, 2000))]);
  }

  async function execute(row) {
    const p = row.params;
    const patch = p.name === 'apply_patch';
    if (!patch && p.name !== 'exec_command') return failure(row.id, 'unsupported_tool');
    if (patch && config.mode === 'read-only') return failure(row.id, 'policy_denied');
    if (resolve(p.workspace) !== config.workspace) return failure(row.id, 'workspace_mismatch');
    const args = p.arguments;
    const command = args.command ?? args.cmd;
    const timeout = args.timeout_ms ?? 120000;
    if (!patch && (typeof command !== 'string' || !command.trim()
        || (args.command !== undefined && args.cmd !== undefined)
        || Object.keys(args).some(k => !['command', 'cmd', 'workdir', 'timeout_ms'].includes(k))
        || (args.workdir !== undefined && (typeof args.workdir !== 'string' || !args.workdir))
        || !Number.isInteger(timeout) || timeout < 1 || timeout > 600000)) {
      return failure(row.id, 'invalid_arguments');
    }
    const cwd = await realpath(resolve(config.workspace, patch ? '.' : (args.workdir ?? '.')));
    const job = {id: row.id, patch, controller: new AbortController(), child: null,
      cancelled: false, cleanupIncomplete: false, timedOut: false, closed: null};
    active = job;
    let workerResult;
    let timer;
    try {
      if (patch) {
        job.ackPath = join(ipcDirectory, row.id + '.ack.json');
        job.requestPath = join(ipcDirectory, row.id + '.request.json');
        await writeFile(job.requestPath,
          JSON.stringify({id: row.id, ...p, ack_path: job.ackPath}) + '\n', {flag: 'wx'});
      }
      const shell = patch ? {exe: config.python,
        args: ['-I', config.worker, '--request-file', job.requestPath]} :
        {exe: p.shell, args: ['-NoProfile', '-NonInteractive', '-Command']};
      const text = patch ? '' :
        "try { Set-Location -LiteralPath '" + cwd.replaceAll("'", "''") +
        "' -ErrorAction Stop } catch { Write-Error $_; exit 1 }; " +
        '[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new(); ' +
        '$OutputEncoding = [Console]::OutputEncoding; ' + command;
      timer = setTimeout(() => { job.timedOut = true; void stop(job); }, timeout);
      // A session-level root deny can be kept by SRT after a WW→RO switch.
      // Apply the same RO protection to each exec instead, under its own holder;
      // SRT releases it with that command. No write grants are added in RO.
      const perCommand = config.mode === 'read-only' ?
        {filesystem: {denyWrite: [config.workspace]}} : undefined;
      const wrapped = await manager.wrapWithSandboxArgv(
        text, shell, perCommand, job.controller.signal, cwd, {commandId: row.id});
      if (job.cancelled) return failure(row.id, 'cancelled');
      const child = spawn(wrapped.argv[0], wrapped.argv.slice(1), {
        shell: false, cwd, env: wrapped.env, windowsHide: true,
        detached: process.platform !== 'win32', stdio: ['pipe', 'pipe', 'pipe'],
      });
      job.child = child;
      job.closed = new Promise((ok, fail) => {
        child.once('error', fail);
        child.once('close', (code, signal) => { job.finished = true; ok({code, signal}); });
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
          if (message.type === 'event'
              && ['patch_diff', 'patch_committed', 'patch_manifest'].includes(message.kind)
              && message.data.call_id === p.call_id) await send(message);
          else if (message.type === 'result' && !workerResult) workerResult = message;
          else throw new Error('invalid_worker_frame');
        }
      };
      // Windows SRT does not deliver our pipe to Python stdin. Requests and
      // ACKs are structured host-owned files, read-only to the sandbox token.
      child.stdin.end();
      const [exit] = await Promise.all([job.closed, patch ? readWorker() :
        drain(child.stdout, 'stdout'), drain(child.stderr, 'stderr')]);
      if (patch) {
        if (workerResult && !job.cleanupIncomplete) return workerResult;
        return failure(row.id, 'cleanup_incomplete', 'interrupted_unknown');
      } else {
        const code = job.cleanupIncomplete ? 'cleanup_incomplete' : job.timedOut ?
          'command_timeout' : job.cancelled ? 'cancelled' :
          exit.code !== 0 ? 'command_exit_nonzero' : null;
        return {id: row.id, type: 'result', result: {exit_code: exit.code,
          timed_out: job.timedOut, cancelled: job.cancelled,
          cleanup_incomplete: job.cleanupIncomplete, error_code: code,
          requires_reset: job.requiresReset === true}};
      }
    } catch {
      await stop(job);
      return failure(row.id, job.child ? 'cleanup_incomplete' : 'backend_unavailable',
        job.child ? 'interrupted_unknown' : 'not_executed');
    } finally {
      clearTimeout(timer);
      if (job.ackWrite) await job.ackWrite.catch(() => {});
      if (job.ackPath) await unlink(job.ackPath).catch(() => {});
      if (job.requestPath) await unlink(job.requestPath).catch(() => {});
      active = undefined;
    }
  }

  async function shutdown() {
    if (closing) return;
    closing = true;
    // Initialization owns session ACL/proxy state until its finally block.
    // Reset must not race an in-flight initialize, including Ctrl+C at startup.
    while (initializing) await new Promise(r => setTimeout(r, 10));
    if (active) await stop(active);
    try {
      if (pendingExecution) await pendingExecution;
      if (manager && config) await manager.reset();
      if (ipcDirectory) await rmdir(ipcDirectory);
      process.exitCode = active?.cleanupIncomplete ? 1 : 0;
    } catch (e) {
      process.stderr.write('SRT shutdown failed: ' + String(e.message).slice(0, 4000) + '\n');
      process.exitCode = 1;
    } finally {
      process.stdin.destroy();
    }
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
        await acknowledge(active, {id: row.id, method: 'ack', index: row.index,
          event_kind: row.event_kind});
        return;
      }
      if (seen.has(row.id) || executing || initializing) {
        await error(row.id, 'bridge_busy'); return;
      }
      seen.add(row.id);
      if (row.method === 'initialize') {
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
          ipcDirectory = await mkdtemp(join(tmpdir(), 'nexus-srt-ipc-'));
          const readRoots = await Promise.all((p.read_roots ?? []).map(r => realpath(r)));
          // Source checkout inside a writable root is not a trusted Worker installation.
          if (roots.some(root => inside(worker, root) || inside(python, root)
              || readRoots.some(r => inside(r, root)))) {
            throw new Error('untrusted_worker_installation');
          }
          for (const list of [p.allowed_domains ?? [], p.denied_domains ?? []]) {
            if (!Array.isArray(list) || list.some(d => typeof d !== 'string'
                || !/^(\*\.)?[a-zA-Z0-9]([a-zA-Z0-9.-]*[a-zA-Z0-9])?$/.test(d)
                || d.includes('..'))) throw new Error('invalid_network_domains');
          }
          const filesystem = filesystemPolicy({workspace, roots, readRoots, worker, python,
            ipcDirectory}, await systemDirectories());
          let windows;
          if (!manager) {
            const installation = await realpath(packageRoot);
            if ([workspace, ...roots].some(r => inside(installation, r))) {
              throw new Error('untrusted_srt_installation');
            }
            manager = await loadSRT(installation);
            windows = {srtWin: {path: await realpath(join(installation,
              'vendor', 'srt-win', process.arch, 'srt-win.exe'))}};
          }
          await manager.initialize({
            network: {allowedDomains: p.allowed_domains ?? [],
              deniedDomains: p.denied_domains ?? [], allowLocalBinding: false},
            filesystem,
            ...(windows ? {windows} : {}),
          });
          config = {mode: p.mode, workspace, worker, python};
          await send({id: row.id, type: 'result', result: {initialized: true}});
        } catch (e) {
          try { if (manager) await manager.reset(); }
          catch (cleanup) { await error(row.id, 'cleanup_incomplete', 'not_executed', cleanup.message); return; }
          await error(row.id, 'initialization_failed', 'not_executed', e.message);
        } finally { initializing = false; }
      } else if (row.method === 'execute' && config) {
        executing = true;
        pendingExecution = execute(row);
        let response;
        try { response = await pendingExecution; }
        finally { executing = false; pendingExecution = undefined; }
        // Terminal result means ready for the next sequential call: finish IPC
        // cleanup and release the busy gate before Python can observe it.
        await send(response);
      } else await error(row.id, 'backend_unavailable');
    })().catch(async () => { await shutdown(); });
  });
  input.on('close', () => { void shutdown(); });
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  await serve(null, process.argv[2]);
}
