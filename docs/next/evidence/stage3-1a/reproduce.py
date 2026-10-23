"""Offline real-filesystem rename comparison against the exact Stage 3 base.

Run from repository root: python docs/next/evidence/stage3-1a/reproduce.py
Requires the locked dev environment; performs no model/network calls.
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import json
import subprocess
import sys
import tempfile
import types
from copy import deepcopy
from dataclasses import asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path[:0] = [str(ROOT / 'src'), str(ROOT)]

from rich.console import Console

from nexus.app.events import Events
from nexus.app.session import SessionLog, resume_session
from nexus.app.tui import Transcript
from nexus.core.agent import run_turn
from nexus.core.context import ContextBuilder, groups
from nexus.core.tokens import TokenEstimator
from nexus.core.types import Limits, Message, Session, Tool, Usage, json_text
from nexus.tools.patch import apply_patch
from nexus.tools.registry import PATCH_SPEC
from tests.conftest import ScriptedModel, call, reply

BASE = 'a4248fa109b120d8e7fffc89685cd72b539d1609'
OUT = Path(__file__).resolve().parent


def fixture():
    initial = {}
    for index in range(12):
        initial[f'components/view{index}.ts'] = ''.join(
            f'export const label{row} = (item: Item) => item.display_name + "row-{row}";\n'
            for row in range(30)
        )
    batches = []
    paths = list(initial)
    for start in range(0, len(paths), 4):
        sections = []
        for path in paths[start:start + 4]:
            before = initial[path]
            after = before.replace('display_name', 'displayName')
            sections.append(f'*** Update File: {path}\n@@\n' +
                            ''.join('-' + line for line in before.splitlines(True)) +
                            ''.join('+' + line for line in after.splitlines(True)))
        batches.append('*** Begin Patch\n' + ''.join(sections) + '*** End Patch\n')
    return initial, batches


async def execute(label, implementation):
    initial, batches = fixture()
    with tempfile.TemporaryDirectory(prefix=f'nexus-patch-{label}-') as directory:
        workspace = Path(directory)
        for path, text in initial.items():
            target = workspace / path
            target.parent.mkdir(exist_ok=True)
            target.write_bytes(text.encode())
        session = Session(workspace)
        writer = SessionLog.create(session, 'Offline displayName rename fixture', {}, workspace)
        output = io.StringIO()
        events = Events(session, writer, Transcript(Console(file=output, color_system=None, width=90)))
        replies = [reply(call('apply_patch', {'patch': p}, f'rename-{i}')) for i, p in enumerate(batches)]
        replies.append(reply(text='Offline scripted rename finished.'))
        for item in replies:
            item.usage = Usage()  # no provider call; never label synthetic usage as reported
        model = ScriptedModel(replies)
        try:
            result = await run_turn(session, 'Rename display_name to displayName in all views.',
                                    model, {'apply_patch': Tool(PATCH_SPEC, implementation)},
                                    events, Limits(context_window=1000000))
            assert result.outcome == 'completed' and result.tool_calls == 3
            raw = writer.path.read_bytes()
            session_path = writer.path
        finally:
            writer.close()
        restored, resumed_writer, warnings = resume_session(session_path, workspace, workspace)
        resumed_writer.close()
        assert not warnings
        groups(ContextBuilder().build_active_context(restored))
        assert [m.public() for m in restored.messages] == [m.public() for m in session.messages]
        hashes = {}
        for path, text in initial.items():
            data = (workspace / path).read_bytes()
            assert data == text.replace('display_name', 'displayName').encode()
            hashes[path] = hashlib.sha256(data).hexdigest()
        observations = []
        for message in session.messages:
            if message.role == 'tool':
                value = json.loads(message.content)
                assert value['ok'] and not value['truncated']
                assert value['data']['omitted_files'] == 0
                value['duration_ms'] = 0  # normalize timing only for reproducible token counts
                observations.append(Message('tool', json_text(value), tool_call_id=message.tool_call_id))
        (OUT / f'{label}-session.jsonl').write_bytes(raw)
        (OUT / f'{label}-tui.txt').write_text(output.getvalue(), encoding='utf-8')
        (OUT / f'{label}-tool-results.jsonl').write_text(
            ''.join(json_text(m.public()) + '\n' for m in observations), encoding='utf-8')
        audit_lines = [line for line in raw.splitlines(keepends=True)
                       if json.loads(line)['kind'] == 'patch_diff']
        return observations, {
            'session_jsonl_bytes': len(raw),
            'audit_event_count': len(audit_lines),
            'audit_jsonl_bytes': sum(map(len, audit_lines)),
            'audit_diff_utf8_bytes': sum(json.loads(line)['data']['diff_bytes'] for line in audit_lines),
            'audit_omitted_count': sum(json.loads(line)['data']['omitted'] for line in audit_lines),
            'file_hashes': hashes,
            'resume': 'PASS',
        }


async def main():
    source = subprocess.run(['git', 'show', f'{BASE}:src/nexus/tools/patch.py'],
                            cwd=ROOT, check=True, capture_output=True).stdout.decode('utf-8')
    baseline = types.ModuleType('nexus.tools._stage3_baseline')
    sys.modules[baseline.__name__] = baseline
    exec(compile(source, f'{BASE}:src/nexus/tools/patch.py', 'exec'), baseline.__dict__)
    before, old = await execute('before', baseline.apply_patch)
    after, new = await execute('after', apply_patch)
    assert old['file_hashes'] == new['file_hashes']
    initial, patches = fixture()
    for b, a in zip(before, after, strict=True):
        old_value, new_value = json.loads(b.content), json.loads(a.content)
        cleaned = deepcopy(old_value)
        for fact in cleaned['data']['files']:
            fact.pop('diff')
            fact.pop('truncated')
        assert cleaned == new_value
    profiles = {}
    for estimator in [TokenEstimator('deepseek-flash', 'https://api.deepseek.com'), TokenEstimator()]:
        original = sum(estimator.raw([m], []) for m in before)
        compact = sum(estimator.raw([m], []) for m in after)
        profiles[estimator.profile] = {
            'before_tool_result_tokens': original, 'after_tool_result_tokens': compact,
            'saved_tokens': original - compact,
            'saved_percent': round(100 * (original - compact) / original, 4),
            'per_call_before': [estimator.raw([m], []) for m in before],
            'per_call_after': [estimator.raw([m], []) for m in after],
        }
    report = {
        'base_commit': BASE, 'baseline_patch_source_sha256': hashlib.sha256(source.encode()).hexdigest(),
        'sample': 'Deterministic equivalent rename; 12 TypeScript files, 30 changed lines each, 3 calls',
        'model_calls': 'NOT RUN (scripted model; actual filesystem edits)',
        'measurement': 'Sum TokenEstimator.raw([ToolResult.message()], []); duration_ms=0 on both sides; no calibration',
        'fixture_sha256': hashlib.sha256(json_text({'initial': initial, 'patches': patches}).encode()).hexdigest(),
        'profiles': profiles, 'before': old, 'after': new,
        'jsonl_total_delta_bytes': new['session_jsonl_bytes'] - old['session_jsonl_bytes'],
    }
    (OUT / 'token-comparison.json').write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({k: report[k] for k in ['profiles', 'jsonl_total_delta_bytes']}, indent=2))
    print('audit_jsonl_bytes:', new['audit_jsonl_bytes'])


if __name__ == '__main__':
    asyncio.run(main())
