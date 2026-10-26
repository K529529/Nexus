"""One owned, newly created directory; intentionally do not repair inherited rights."""
import asyncio
import json
import os
import subprocess
from dataclasses import asdict
from pathlib import Path
from uuid import uuid4

from nexus.core.security import SecurityPolicy
from nexus.core.types import ExecutionContext
from nexus.tools.windows_native import WindowsNativeBackend


async def main():
    root = Path('D:/WorkSpace') / ('nexus-hardening-missing-rights-' + uuid4().hex)
    root.mkdir()
    shell = os.environ['SystemRoot'] + '/System32/WindowsPowerShell/v1.0/powershell.exe'
    before = subprocess.check_output(['icacls', str(root)]).decode(errors='replace')
    backend = WindowsNativeBackend(SecurityPolicy(
        'workspace-write', windows_acl_roots=(str(root),),
        windows_koffi='D:/WorkSpace/nexus-native-runtime-20261026/node_modules/koffi',
    ))

    async def emit(*args):
        pass

    try:
        result = await backend.execute('exec_command', {'command': 'Set-Content must-not-run.txt bad'},
                                       ExecutionContext(root, 'missing-rights', shell), emit)
    finally:
        await backend.close()
    report = {'root': str(root), 'before': before, 'result': asdict(result),
              'command_ran': (root / 'must-not-run.txt').exists(),
              'after': subprocess.check_output(['icacls', str(root)]).decode(errors='replace')}
    Path(__file__).with_name('missing-rights.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    assert not result.ok and not report['command_ran']
    assert 'WRITE_DAC / WRITE_OWNER' in result.data.get('detail', '')
    print('PASS: real missing OS rights, clear diagnostic, no command execution')


asyncio.run(main())
