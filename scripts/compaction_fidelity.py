"""Opt-in, one-call fixed-input compaction experiment. No coding tools or main model.

Example: python scripts/compaction_fidelity.py --case ABF-original --protocol current
         --output artifacts/fidelity/abf-current-1 --run
Without --run this only writes the frozen request and NOT_RUN receipt. Never overwrites a run.
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
from nexus.core.compaction import (
    PROMPT_VERSION,
    parse_summary,
    summary_input,
    summary_message,
    validate_summary,
)
from nexus.core.model import ChatModel, CompactionModel
from nexus.core.tokens import estimate
from nexus.core.types import Message, ModelError, Session, json_text

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"


def digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()


def load_case(case_id: str) -> dict[str, Any]:
    cases = json.loads((FIXTURES / "compaction-fidelity.json").read_text(encoding="utf-8"))
    case: dict[str, Any] = next(c for c in cases if c["id"] == case_id)
    frozen = {
        k: case[k] for k in ("messages", "run_outcomes", "previous_summary", "previous_summary_seq")
    }
    if digest(frozen) != case["input_sha256"]:
        raise ValueError("Frozen input hash mismatch")
    return case


def prepare(case: dict[str, Any], protocol: str) -> tuple[list[Message], list[Message]]:
    history = [Message.from_data(m, seq=m["seq"], run_id=m["run_id"]) for m in case["messages"]]
    prior = case["previous_summary"]
    prefix = ([summary_message(prior, case["previous_summary_seq"])] if prior else []) + history
    session = Session(ROOT, messages=history, run_outcomes=case["run_outcomes"])
    if protocol == "current":
        return prefix, summary_input(session, prefix, 4096, ())
    request = {
        "previous_summary": prior,
        "history": [{"seq": m.seq, "run_id": m.run_id, **m.public()} for m in history],
        "run_outcomes": case["run_outcomes"],
        "summary_budget_tokens": 4096,
    }
    prompt = (FIXTURES / "compaction-baseline-prompt.txt").read_text(encoding="utf-8")
    return prefix, [Message("system", prompt), Message("user", json_text(request))]


async def experiment(args: argparse.Namespace) -> None:
    case = load_case(args.case)
    prefix, request = prepare(case, args.protocol)
    output = Path(args.output)
    await asyncio.to_thread(output.mkdir, parents=True, exist_ok=False)
    config = ModelConfig(
        "Qwen/Qwen3.5-35B-A3B",
        128000,
        "https://api.siliconflow.cn/v1",
        "SILICONFLOW_API_KEY",
        4096,
        request_timeout_seconds=120,
        enable_thinking=False,
    )
    wire = [m.public() for m in request]
    result: dict[str, Any] = {
        "case": args.case,
        "input_sha256": case["input_sha256"],
        "request_sha256": digest(wire),
        "prompt_version": PROMPT_VERSION if args.protocol == "current" else "0880514",
        "model_parameters": asdict(config),
        "temperature": "omitted (provider default)",
        "git_head": (
            await asyncio.to_thread(
                subprocess.check_output, ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
            )
        ).strip(),
        "working_diff_sha256": hashlib.sha256(
            await asyncio.to_thread(subprocess.check_output, ["git", "diff"], cwd=ROOT)
        ).hexdigest(),
        "estimated_input_tokens": estimate(request, []),
        "mechanism": "NOT_RUN",
        "fact_review": "PENDING_MANUAL_REVIEW",
        "events": [],
    }
    (output / "request.json").write_text(
        json.dumps(wire, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    async def receive(kind: str, data: dict[str, Any], **_: Any) -> int:
        if kind in {"model_started", "model_finished"}:
            result["events"].append({"kind": kind, "data": data})
        return 0

    started = time.monotonic()
    model = None
    try:
        if args.run:
            model = (
                CompactionModel(config)
                if args.protocol == "current"
                else ChatModel(config, json_object=True)
            )
            reply = await model.complete(request, [], receive)
            result["usage"] = asdict(reply.usage)
            result["finish_reason"] = reply.finish_reason
            (output / "response.txt").write_text(reply.message.content, encoding="utf-8")
            if reply.finish_reason != "stop" or reply.message.tool_calls:
                raise ModelError("invalid_compaction_response")
            if args.protocol == "current":
                result["summary"] = parse_summary(reply.message.content, prefix, 4096)
            else:
                value = validate_summary(json.loads(reply.message.content), version=1)
                prior = case["previous_summary"]
                if prior and any(
                    not set(prior[f]).issubset(value[f])
                    for f in ("goal", "constraints_preferences", "key_decisions_rationale")
                ):
                    raise ModelError("compaction_constraint_loss")
                result["summary"] = value
            result["mechanism"] = "PASS"
    except Exception as exc:
        result["mechanism"] = "FAIL"
        result["error"] = getattr(exc, "code", type(exc).__name__)
    finally:
        if model is not None:
            await model.close()
        result["duration_ms"] = int((time.monotonic() - started) * 1000)
        (output / "result.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    print(json.dumps({k: result[k] for k in ("case", "mechanism", "duration_ms", "fact_review")}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--case", required=True, choices=["ABF-original", "CDE-initial", "DE-update"]
    )
    parser.add_argument("--protocol", choices=["baseline", "current"], default="current")
    parser.add_argument("--output", required=True)
    parser.add_argument(
        "--run",
        action="store_true",
        help="Perform one paid request (bounded adapter retries apply)",
    )
    asyncio.run(experiment(parser.parse_args()))
