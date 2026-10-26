// Real Pi runner/API timings, never fake Win32 calls. Run only on owned fixtures.
// Usage: node this-file report.json --workspace OWNED --temp OWNED --mode ... -- exe args
import {writeFileSync} from 'node:fs';
import {main} from '../src/nexus/_vendor/pi_windows/runner.js';
import {win32} from '../src/nexus/_vendor/pi_windows/ffi.js';
const report = {steps:[]};
const start = performance.now();
const api = await win32();
report.binding_ms = performance.now()-start;
const timed = Object.fromEntries(Object.entries(api).map(([name, fn]) => [name,
  (...args) => {
    const t = performance.now();
    try { return fn(...args); }
    finally { report.steps.push({name, root: /NamedSecurityInfo/.test(name) ? args[0] : undefined,
      ms:performance.now()-t}); }
  }]));
try { report.exit_code = await main(process.argv.slice(3), {api:timed}); }
catch(error) { report.error = String(error); process.exitCode=1; }
finally { report.total_ms=performance.now()-start;
  writeFileSync(process.argv[2],JSON.stringify(report,null,2)); }
