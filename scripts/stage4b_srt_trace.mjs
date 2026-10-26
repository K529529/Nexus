// Opt-in timing observer for normal integration acceptance. Never modifies SRT
// files or flags. Only operation names/times and filesystem policies are logged;
// exec argv/env/proxy tokens and command output are deliberately not recorded.
import cp from 'node:child_process';
import {syncBuiltinESMExports} from 'node:module';
import {appendFileSync} from 'node:fs';
import {basename, join} from 'node:path';
import {pathToFileURL} from 'node:url';

const output = process.env.NEXUS_SRT_TRACE;
const packageRoot = process.env.NEXUS_SRT_TRACE_PACKAGE;
if (!output || !packageRoot) throw Error('Explicit trace output and SRT package required');
const record = row => appendFileSync(output, JSON.stringify({pid:process.pid,
  at_ms:performance.now(), ...row}) + '\n');
const describe = (exe, args = []) => {
  const tool = basename(String(exe)).toLowerCase();
  const a = args[0] === '--srt-win' ? args.slice(1) : args;
  return {tool, operation:tool === 'srt-win.exe' ? a.slice(0, 2).join(' ') : 'subprocess'};
};
const originalSync = cp.spawnSync;
cp.spawnSync = function(exe, args, options) {
  const started = performance.now(), info = describe(exe, args);
  const result = originalSync.call(this, exe, args, options);
  record({...info, kind:'spawnSync', elapsed_ms:performance.now()-started,
    status:result.status, error:result.error?.code});
  return result;
};
const originalSpawn = cp.spawn;
cp.spawn = function(exe, args, options) {
  const started = performance.now(), info = describe(exe, args);
  const child = originalSpawn.call(this, exe, args, options);
  child.once('close', status => record({...info, kind:'spawn',
    elapsed_ms:performance.now()-started, status}));
  return child;
};
syncBuiltinESMExports();
const imported = performance.now();
const {SandboxManager:manager} = await import(pathToFileURL(join(packageRoot, 'dist/index.js')));
record({phase:'import', elapsed_ms:performance.now()-imported});
for (const method of ['initialize', 'reset', 'wrapWithSandboxArgv']) {
  const original = manager[method];
  manager[method] = async function(...args) {
    const started = performance.now();
    if (method === 'initialize') record({phase:'policy', filesystem:args[0].filesystem});
    try { return await original.apply(this,args); }
    finally { record({phase:method, elapsed_ms:performance.now()-started}); }
  };
}
