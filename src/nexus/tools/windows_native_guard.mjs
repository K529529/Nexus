// Independent thread: the runner's main thread blocks in WaitForSingleObject.
// Hold a handle to the original bridge (not a recyclable PID) before any child
// starts. Bridge death terminates this runner, closing Pi's kill-on-close Job.
import {parentPort, workerData} from 'node:worker_threads';
import {win32, isNullPtr} from '../_vendor/pi_windows/ffi.js';

try {
  const api = await win32();
  const parent = api.openProcess(0x00100000, 0, workerData); // SYNCHRONIZE only
  if (isNullPtr(parent)) throw Error('Cannot watch Native bridge');
  parentPort.postMessage('ready');
  // Nonblocking polls avoid holding Koffi's shared binding lock across threads.
  while (api.waitForSingleObject(parent, 0) === 258) { // WAIT_TIMEOUT
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  // Either parent exit or a wait failure: never continue unsupervised.
} catch {
  parentPort.postMessage('failed');
}
process.kill(process.pid, 'SIGKILL');
