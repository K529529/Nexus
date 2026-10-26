// Native driver for the existing sequential IPC/patch-audit transport. Importing
// serve does not load Anthropic SRT; no SRT manager/package is supplied or used.
import {lstat, mkdtemp, realpath, rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {fileURLToPath} from 'node:url';
import {serve} from './srt_bridge.mjs';

const settings = process.argv[2] ? JSON.parse(process.argv[2]) : undefined;
const runner = fileURLToPath(new URL('../_vendor/pi_windows/runner.js', import.meta.url));
let privateTemp;
let config;

const allowed = new Set([
  'SYSTEMROOT', 'WINDIR', 'SYSTEMDRIVE', 'COMSPEC', 'PATH', 'PATHEXT',
  'USERPROFILE', 'HOMEDRIVE', 'HOMEPATH', 'USERNAME', 'USERDOMAIN',
  'PROGRAMFILES', 'PROGRAMFILES(X86)', 'PROGRAMW6432', 'PROGRAMDATA',
  'LOCALAPPDATA', 'APPDATA', 'NUMBER_OF_PROCESSORS', 'PROCESSOR_ARCHITECTURE',
  'PROCESSOR_IDENTIFIER', 'OS', 'COMMONPROGRAMFILES',
  'COMMONPROGRAMFILES(X86)', 'COMMONPROGRAMW6432', 'LANG', 'LC_ALL',
]);
export function childEnvironment(source, secrets, temp, koffi) {
  const excluded = new Set(secrets.map(name => name.toUpperCase()));
  const env = Object.fromEntries(Object.entries(source).filter(([key]) =>
    allowed.has(key.toUpperCase()) && !excluded.has(key.toUpperCase())));
  return {...env, TEMP:temp, TMP:temp, NEXUS_NATIVE_KOFFI:koffi,
    PYTHONIOENCODING:'utf-8', PYTHONDONTWRITEBYTECODE:'1'};
}

if (settings) await serve({
  async initialize(value) {
    if (value.network.allowedDomains.length || value.network.deniedDomains.length)
      throw Error('Windows Native has Network: unrestricted; domain policies are unsupported');
    config = value.filesystem;
    // Only this newly owned child directory is ever granted. Host TEMP unchanged.
    privateTemp = await mkdtemp(join(tmpdir(), 'nexus-native-temp-'));
    // Retained by BridgeClient for actionable diagnostics after a hard crash.
    process.stderr.write('Native session TEMP (owned, never reused): ' + privateTemp + '\n');
  },
  async wrapWithSandboxArgv(text, shell) {
    const roots = config.allowWrite;
    const argv = [process.execPath, runner, '--workspace', config.allowRead[0],
      '--temp', privateTemp, '--mode', roots.length ? 'workspace-write' : 'read-only'];
    for (const root of roots.slice(1)) argv.push('--writable', root);
    argv.push('--', shell.exe, ...shell.args, text);
    const env = childEnvironment(process.env, settings.secret_env, privateTemp, settings.koffi);
    env.NEXUS_NATIVE_LOCK_ROOT = settings.lock_root;
    env.NEXUS_NATIVE_PRIVATE_TEMP = '1';
    env.NEXUS_NATIVE_PARENT_PID = String(process.pid);
    return {argv, env};
  },
  async reset() {
    if (privateTemp) {
      // Owned random fixture only; never follow a substituted root or repair ACLs.
      if ((await lstat(privateTemp)).isSymbolicLink()
          || (await realpath(privateTemp)).toLowerCase() !== privateTemp.toLowerCase())
        throw Error('Private TEMP changed; retained for inspection');
      await rm(privateTemp, {recursive:true, force:true});
      privateTemp = undefined;
    }
  },
});
