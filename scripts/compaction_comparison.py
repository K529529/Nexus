"""Bounded A/B compaction experiment. Defaults to a frozen NOT_RUN manifest.

Two models x three frozen cases x two repetitions = twelve calls per stage.
Run A before implementing B. Never overwrite or retry a logical sample externally.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import subprocess
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

from nexus.app.config import ModelConfig
from nexus.core.compaction import PROMPT_VERSION, parse_summary, summary_message
from nexus.core.model import CompactionModel
from nexus.core.types import Session
from scripts.compaction_fidelity import ROOT, digest, load_case, prepare

MODELS = ["Qwen/Qwen3.5-35B-A3B", "deepseek-ai/DeepSeek-V4-Flash"]
CASES = ["ABF-original", "CDE-initial", "DE-update"]


def save(path: Path, value: Any) -> None:
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)


def prepare_stage(args: argparse.Namespace) -> Any:
    expected = {"A": "simple-v3", "B": "facts-v4"}[args.stage]
    if PROMPT_VERSION != expected:
        raise ValueError(f"Stage {args.stage} requires {expected}, found {PROMPT_VERSION}")
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=False)
    prepared = {}
    for case_id in CASES:
        case = load_case(case_id)
        prefix, request = prepare(case, "current")
        facts = None
        if args.stage == "B":
            from nexus.core.runtime_facts import project_facts

            facts = project_facts(Session(ROOT, run_outcomes=case["run_outcomes"]), prefix)
        prepared[case_id] = (case, prefix, request, facts)
        save(output / f"{case_id}.request.json", [m.public() for m in request])
        if facts is not None:
            save(output / f"{case_id}.facts.json", facts)
    # Interleave model order across repeats; no adaptation to preceding outputs.
    plan = [
        {"case": case, "model": model, "repeat": repeat}
        for repeat in (1, 2)
        for case in CASES
        for model in (MODELS if repeat == 1 else MODELS[::-1])
    ]
    manifest = {
        "stage": args.stage,
        "prompt_version": PROMPT_VERSION,
        "plan": plan,
        "git_head": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
        "working_diff_sha256": hashlib.sha256(
            subprocess.check_output(["git", "diff"])
        ).hexdigest(),
        "source_sha256": {
            str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in [Path(__file__).resolve(), *sorted((ROOT / "src/nexus/core").glob("*.py"))]
        },
        "inputs": {c: prepared[c][0]["input_sha256"] for c in CASES},
        "requests": {c: digest([m.public() for m in prepared[c][2]]) for c in CASES},
        "temperature": "omitted; provider defaults may differ between models",
        "retry_policy": "existing adapter: at most 2 physical attempts per logical sample",
        "status": "RUN_AUTHORIZED" if args.run else "NOT_RUN",
    }
    save(output / "manifest.json", manifest)
    if not args.run:
        return
    return output, prepared, manifest


async def sample(args: Any, output: Path, prepared: Any, manifest: Any, item: Any) -> None:
    case, prefix, request, facts = prepared[item["case"]]
    tag = "qwen" if item["model"] == MODELS[0] else "deepseek"
    folder = output / f"{item['case']}-{tag}-{item['repeat']}"
    await asyncio.to_thread(folder.mkdir, exist_ok=False)
    config = ModelConfig(
        item["model"], 128000, "https://api.siliconflow.cn/v1", "SILICONFLOW_API_KEY",
        4096, request_timeout_seconds=120, enable_thinking=False,
    )
    result = {
        **item, "stage": args.stage, "prompt_version": PROMPT_VERSION,
        "input_sha256": case["input_sha256"],
        "request_sha256": manifest["requests"][item["case"]],
        "model_parameters": asdict(config), "events": [], "response_models": [],
        "response_ids": [], "physical_calls": 0, "reasoning_characters": 0,
        "schema": "FAIL", "fact_fidelity": "PENDING_MANUAL_REVIEW",
    }
    content: list[str] = []

    async def receive(kind: str, data: Any, **_: Any) -> int:
        if kind in {"model_started", "model_finished"}:
            result["events"].append({"kind": kind, "data": data})
        return 0

    model = None
    started = time.monotonic()
    try:
        model = CompactionModel(config)
        create = model.adapter.client.chat.completions.create

        async def observed_create(**kwargs: Any) -> Any:
            result["physical_calls"] += 1
            assert not kwargs.get("tools")
            assert kwargs["extra_body"] == {"enable_thinking": False}
            assert digest(kwargs["messages"]) == result["request_sha256"]
            stream = await create(**kwargs)

            class ObservedStream:
                async def __aiter__(self) -> Any:
                    async for chunk in stream:
                        for key, target in [("model", "response_models"), ("id", "response_ids")]:
                            value = getattr(chunk, key, None)
                            if value and value not in result[target]:
                                result[target].append(value)
                        if chunk.usage:
                            result["raw_usage"] = chunk.usage.model_dump()
                        for choice in chunk.choices:
                            if choice.delta.content:
                                content.append(choice.delta.content)
                            result["reasoning_characters"] += len(
                                getattr(choice.delta, "reasoning_content", "") or ""
                            )
                            if choice.finish_reason:
                                result["finish_reason"] = choice.finish_reason
                        yield chunk

                async def close(self) -> None:
                    await stream.close()

            return ObservedStream()

        model.adapter.client.chat.completions.create = observed_create
        reply = await model.complete(request, [], receive)
        result["usage"] = asdict(reply.usage)
        parsed = parse_summary(reply.message.content, prefix, 4096)
        result["summary"] = parsed
        result["schema"] = "PASS"
        context = (
            summary_message(parsed, runtime_facts=facts)
            if facts is not None else summary_message(parsed)
        )
        save(folder / "continuation-context.json", context.public())
    except Exception as exc:
        result["error"] = getattr(exc, "code", type(exc).__name__)
    finally:
        if model is not None:
            await model.close()
        result["latency_ms"] = int((time.monotonic() - started) * 1000)
        raw = "".join(content)
        await asyncio.to_thread((folder / "response.txt").write_text, raw, encoding="utf-8")
        result["response_sha256"] = hashlib.sha256(raw.encode()).hexdigest()
        save(folder / "result.json", result)
    print(json.dumps({**item, "schema": result["schema"],
                      "latency_ms": result["latency_ms"]}), flush=True)


async def run(args: argparse.Namespace) -> None:
    prepared_stage = await asyncio.to_thread(prepare_stage, args)
    if prepared_stage is None:
        return
    output, prepared, manifest = prepared_stage
    for item in manifest["plan"]:
        await sample(args, output, prepared, manifest, item)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", choices=["A", "B"], required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--run", action="store_true")
    asyncio.run(run(parser.parse_args()))
