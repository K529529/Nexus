"""Opt-in paid model acceptance, one physical process/phase and isolated Nexus home."""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import runpy
import shutil
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

from nexus.app.bootstrap import Conversation
from nexus.app.config import load_config
from nexus.app.events import redact
from nexus.core.agent import append_message
from nexus.core.types import Message, RuntimeEvent
from scripts.memory_qmd_smoke import wait_ready

REMEMBER = (
    "这是受控的 FilePilot Stage2 验证项目。请记住长期工程约定：清理目录时必须跳过 "
    "junction 和 access_denied 路径，保留失败证据，不擅自修改权限。"
    "另外请将这条历史记录放入 2026-09-01 的 Daily："
    "FilePilot preview server uses port 4317; status polling interval is two seconds. "
    "这次只保存记忆，不改工程文件。"
)
AUTONOMOUS = (
    "这是 FilePilot Stage2 验证项目。修复 sort_events.py 的时间排序缺陷，"
    "运行已有 unittest，简要说明根因和验证结果。"
    "后续这个项目的分组接口也会需要同样的时间排序语义。"
)
TASK = (
    "这是 FilePilot Stage2 验证项目。在当前目录实现 group_events.py 的 group_events(events)："
    "返回 user 到事件列表的字典，组内使用这个项目此前确定的时间排序语义，保留事件全部原字段。"
    "需要时使用现有历史知识；如果找不到此前约定请明确说明，不要猜。验证你的实现。"
)
QUERY = (
    "请查找 FilePilot Stage2 项目的历史记录：预览服务端口是多少，状态轮询间隔是多少？"
    "必须依据已有记忆给出来源。另请确认我们是否记录过生产数据库密码；没有就明确说没有，"
    "不要生成或猜测密码。"
)
MAINTAIN = (
    "FilePilot Stage2 工程约定需要纠正：仍须跳过 junction 和 access_denied，"
    "但失败证据应保存至少 7 天。请更正原有条目，避免重复追加相互矛盾的版本。"
    "然后演示可恢复删除这条已更新的约定，并用返回的 recovery_id 恢复；"
    "最后读取原文确认恢复成功。只操作这条约定，保留其他记忆。"
)
RULES = (
    "This is an isolated FilePilot acceptance fixture. Operate only inside this workspace. "
    "Use Python standard library. Do not install packages, use network commands, or modify Git. "
    "Run relevant checks and preserve their exit status."
)
SORT_TEST = """import unittest
from sort_events import sort_events

class SortTests(unittest.TestCase):
    def test_offsets(self):
        a={"id":"late","timestamp":"2026-01-01T09:00:00+08:00"}
        b={"id":"early","timestamp":"2026-01-01T00:30:00Z"}
        self.assertEqual(sort_events([a,b]), [b,a])
    def test_invalid_last_and_stable_ties(self):
        a={"id":"first","timestamp":"2026-01-01T00:00:00Z"}
        b={"id":"second","timestamp":"2026-01-01T08:00:00+08:00"}
        c={"id":"bad","timestamp":"not-a-time"}
        d={"id":"missing"}
        original=[c,a,b,d]
        self.assertEqual(sort_events(original), [a,b,c,d])
        self.assertEqual(original, [c,a,b,d])

if __name__ == "__main__": unittest.main()
"""


def prepare(args: argparse.Namespace) -> tuple[Path, Path, Path]:
    root, workspace, output = (
        Path(args.home).resolve(),
        Path(args.workspace).resolve(),
        Path(args.output).resolve(),
    )
    output.mkdir(parents=True, exist_ok=False)
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "AGENTS.md").write_text(RULES, encoding="utf-8")
    if args.phase == "autonomous":
        (workspace / "sort_events.py").write_text(
            'def sort_events(events):\n    return sorted(events, key=lambda e: e["timestamp"])\n',
            encoding="utf-8",
        )
        (workspace / "test_sort_events.py").write_text(SORT_TEST, encoding="utf-8")
    if args.phase == "task":
        (workspace / "group_events.py").write_text(
            "def group_events(events):\n    raise NotImplementedError\n", encoding="utf-8"
        )
    return root, workspace, output


def verify_group(workspace: Path) -> dict[str, Any]:
    spec = importlib.util.spec_from_file_location(
        "candidate_group_events", workspace / "group_events.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    checks: dict[str, Any] = {}
    try:
        spec.loader.exec_module(module)
        events = [
            {"user": "a", "id": "bad", "timestamp": "invalid", "payload": 7},
            {"user": "a", "id": "late", "timestamp": "2026-01-01T09:00:00+08:00"},
            {"user": "b", "id": "b", "timestamp": "2026-01-01T00:00:00Z"},
            {"user": "a", "id": "early", "timestamp": "2026-01-01T00:30:00Z"},
            {"user": "a", "id": "tie", "timestamp": "2026-01-01T08:30:00+08:00"},
            {"user": "a", "id": "missing"},
        ]
        original = json.dumps(events, sort_keys=True)
        result = module.group_events(events)
        checks = {
            "groups": set(result) == {"a", "b"},
            "time_order_invalid_last_stable_ties": [e["id"] for e in result["a"]]
            == ["early", "tie", "late", "bad", "missing"],
            "no_input_mutation": json.dumps(events, sort_keys=True) == original,
            "fields_preserved": {e["id"]: e for group in result.values() for e in group}
            == {e["id"]: e for e in events},
        }
    except Exception as exc:
        checks["error"] = f"{type(exc).__name__}: {exc}"
    return {"passed": bool(checks) and all(v is True for v in checks.values()), "checks": checks}


async def run(args: argparse.Namespace, paths: tuple[Path, Path, Path]) -> None:
    root, workspace, output = paths
    cfg = load_config()
    cfg.memory_enabled = not args.off
    cfg.memory_qmd_command = (
        ("nexus-nonexistent-qmd",)
        if args.qmd_missing
        else (shutil.which("node") or "node", args.qmd_entry)
    )
    cfg.model.max_output_tokens = cfg.limits.max_output_tokens = 8192
    cfg.limits.max_steps = 12
    cfg.mcp_servers = {}  # Never inherit operator's external tools into a controlled fixture.
    receipts: list[dict[str, Any]] = []
    report: dict[str, Any] = {
        "phase": args.phase,
        "memory_on": not args.off,
        "status": "RUNNING",
        "model": asdict(cfg.model),
        "limits": asdict(cfg.limits),
        "compaction_model": asdict(cfg.compaction_model) if cfg.compaction_model else None,
    }
    secrets = (cfg.model.key(), cfg.compaction_model.key() if cfg.compaction_model else "")

    def encoded(value: Any) -> str:
        return (
            json.dumps(redact(value, secrets), ensure_ascii=False)
            .replace(json.dumps(str(root))[1:-1], "<isolated-home>")
            .replace(json.dumps(str(workspace))[1:-1], "<workspace>")
        )

    async def consume(event: RuntimeEvent) -> None:
        receipt = asdict(event)
        receipts.append(receipt)
        with (output / "events.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(encoded(receipt) + "\n")
        if event.kind in {
            "model_finished",
            "tool_finished",
            "memory_index",
            "compaction_completed",
        }:
            print(
                json.dumps(
                    {
                        "kind": event.kind,
                        "status": event.data.get("status"),
                        "ok": event.data.get("ok"),
                        "purpose": event.data.get("purpose"),
                    }
                ),
                flush=True,
            )

    convo = Conversation(
        workspace, cfg, consume, home=root, resume=Path(args.resume) if args.resume else None
    )
    resumed_snapshot = convo.session.memory_snapshot
    started = time.monotonic()
    try:
        if args.phase == "compact":
            result = await convo.turn("继续这段会话，简述已确定的 FilePilot 约定；无需修改文件。")
            prefix = next(m.content for m in convo.session.messages if m.role == "system")
            assert convo.events
            # Explicitly synthetic history is only a capacity fixture, never a memory benefit claim.
            for round_number in range(2):
                for i in range(32):
                    await append_message(
                        convo.session,
                        Message(
                            "user",
                            f"Synthetic compaction fixture {round_number}/{i}. " + "context " * 900,
                        ),
                        convo.events,
                    )
                    await append_message(
                        convo.session, Message("assistant", "Fixture observed."), convo.events
                    )
                assert await convo.compact()
                assert (
                    next(m.content for m in convo.session.messages if m.role == "system") == prefix
                )
            report["two_real_compactions_stable_snapshot"] = True
        else:
            prompt = {
                "remember": REMEMBER,
                "autonomous": AUTONOMOUS,
                "task": TASK,
                "query": QUERY,
                "maintain": MAINTAIN,
                "resume": "继续这段会话，简述已确定的 FilePilot 约定；无需修改文件或保存重复记忆。",
                "offline": (
                    "在此隔离工作区创建 health_check.py，实现 twice(x) 返回 x*2，"
                    "用 Python 验证整数和负数。不需要历史知识。"
                ),
            }[args.phase]
            result = await convo.turn(prompt)
        report["run"] = asdict(result)
        if convo.memory_index and not args.qmd_missing:
            # Tools return before indexing; the acceptance boundary explicitly waits for it.
            await wait_ready(convo.memory_index)
        report["tools"] = [r["data"]["name"] for r in receipts if r["kind"] == "tool_started"]
        report["successful_memory_writes"] = sum(
            r["kind"] == "message"
            and r["data"].get("role") == "tool"
            and bool(json.loads(r["data"]["content"]).get("data", {}).get("written"))
            for r in receipts
        )
        report["snapshot_tokens"] = (convo.session.memory_snapshot or {}).get("tokens", 0)
        if args.resume:
            report["snapshot_preserved_on_resume"] = (
                convo.session.memory_snapshot == resumed_snapshot
            )
        if args.phase == "offline":
            candidate = runpy.run_path(str(workspace / "health_check.py"))["twice"]
            report["independent_verifier"] = {"passed": candidate(4) == 8 and candidate(-3) == -6}
        report["session_file"] = str(convo.writer.path) if convo.writer else None
        if args.phase == "task":
            report["independent_verifier"] = verify_group(workspace)
            for path in workspace.glob("*.py"):
                shutil.copyfile(path, output / path.name)
        if args.phase == "autonomous":
            report["autonomous_memory_write"] = report["successful_memory_writes"] > 0
        report["status"] = "EXECUTED" if result.outcome == "completed" else "FAIL"
    except BaseException as exc:
        report["status"] = "FAIL"
        report["error"] = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        await convo.close()
        report["seconds"] = time.monotonic() - started
        (output / "report.json").write_text(encoded(report), encoding="utf-8")
        # This private local pointer enables an actual new process to resume the saved session.
        if convo.writer:
            (root / f"{args.phase}-session-path.txt").write_text(
                str(convo.writer.path), encoding="utf-8"
            )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--phase",
        choices=[
            "remember",
            "autonomous",
            "task",
            "query",
            "maintain",
            "compact",
            "resume",
            "offline",
        ],
        required=True,
    )
    parser.add_argument("--home", required=True)
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--qmd-entry", required=True)
    parser.add_argument("--off", action="store_true")
    parser.add_argument("--qmd-missing", action="store_true")
    parser.add_argument("--resume")
    options = parser.parse_args()
    asyncio.run(run(options, prepare(options)))
