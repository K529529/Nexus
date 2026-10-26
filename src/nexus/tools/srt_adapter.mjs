// Fixed-version production adapter; installation is selected only by the host.
import { readFile, realpath } from 'node:fs/promises';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';

export async function loadSRT(trustedPackageRoot) {
  if (!trustedPackageRoot) throw new Error('SRT installation path missing');
  const root = await realpath(trustedPackageRoot);
  const pkg = JSON.parse(await readFile(join(root, 'package.json'), 'utf8'));
  if (pkg.name !== '@anthropic-ai/sandbox-runtime' || pkg.version !== '0.0.81') {
    throw new Error('Expected SRT 0.0.81');
  }
  const [major, minor] = process.versions.node.split('.').map(Number);
  if (major < 22 || (major === 22 && minor < 12)) throw new Error('Node >=22.12 required');
  const {SandboxManager} = await import(pathToFileURL(join(root, 'dist/index.js')).href);
  if (typeof SandboxManager.wrapWithSandboxArgv !== 'function') {
    throw new Error('SRT argv API unavailable');
  }
  return SandboxManager;
}
