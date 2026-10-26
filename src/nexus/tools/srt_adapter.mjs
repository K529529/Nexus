// Dormant integration seam, intentionally not reachable from the production entry.
// Acceptance must precede enabling SRT initialization (which may alter Windows ACLs).
import { readFile, realpath } from 'node:fs/promises';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';

export async function loadSRT(trustedPackageRoot) {
  const root = await realpath(trustedPackageRoot);
  const pkg = JSON.parse(await readFile(join(root, 'package.json'), 'utf8'));
  if (pkg.name !== '@anthropic-ai/sandbox-runtime' || pkg.version !== '0.0.81') {
    throw new Error('Expected SRT 0.0.81');
  }
  const {SandboxManager} = await import(pathToFileURL(join(root, 'dist/index.js')).href);
  if (typeof SandboxManager.wrapWithSandboxArgv !== 'function') {
    throw new Error('SRT argv API unavailable');
  }
  return SandboxManager;
}
