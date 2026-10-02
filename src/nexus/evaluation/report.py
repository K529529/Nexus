"""Small JSON evidence and the same compact report for terminal and disk."""

from __future__ import annotations

import hashlib
import io
import json
import subprocess
from html import escape
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit

from rich.console import Console
from rich.table import Table
from rich.text import Text

from nexus.app.tui import terminal_text
from nexus.core.types import Json


def write_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def implementation_identity(root: Path) -> Json:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, check=False
    )
    hashes = {}
    for path in sorted(
        [
            *root.glob("src/**/*.py"),
            *root.glob("evaluation/next-dev-v0/**/*"),
            root / "pyproject.toml",
            root / "uv.lock",
        ]
    ):
        if path.is_file() and "__pycache__" not in path.parts:
            hashes[path.relative_to(root).as_posix()] = hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
    return {
        "commit": result.stdout.decode().strip() if result.returncode == 0 else None,
        "runtime_file_hashes": hashes,
        "runtime_digest": hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest(),
    }


def public_url(url: str) -> str:
    parts = urlsplit(url)
    host = parts.hostname or ""
    if ":" in host:
        host = "[" + host + "]"
    return urlunsplit(
        (parts.scheme, host + (f":{parts.port}" if parts.port else ""), parts.path, "", "")
    )


def number(value: object) -> str:
    return "unknown" if value is None else f"{value:,}" if isinstance(value, int) else str(value)


def usage(value: Json) -> str:
    return (
        ">=" if value["reported"] is not None and value["coverage"] < value["calls"] else ""
    ) + number(value["reported"])


def save_report(directory: Path, results: list[Json], selected: list[str]) -> str:
    by_id = {result["case_id"]: result for result in results}
    counts = {
        status: sum(r["status"] == status for r in results)
        for status in ("PASS", "FAIL", "ERROR", "ABORTED", "NOT_RUN")
    }
    totals: Json = {}
    for field in ("input_tokens", "output_tokens", "total_tokens"):
        values = [r["metrics"]["usage"][field] for r in results if r["metrics"].get("available")]
        known = [v["reported"] for v in values if v["reported"] is not None]
        totals[field] = dict(
            reported=sum(known) if known else None,
            coverage=sum(v["coverage"] for v in values),
            calls=sum(v["calls"] for v in values),
        )
    for field in ("model_calls", "tool_calls", "duration_ms"):
        values = [r["metrics"].get(field) for r in results if r["metrics"].get("available")]
        totals[field] = sum(v for v in values if v is not None) if values else None
    totals["usage_coverage"] = sum(r["metrics"].get("usage_coverage", 0) for r in results)
    summary = dict(schema_version=1, cases=results, counts=counts, selected=selected, totals=totals)
    write_json(directory / "summary.json", summary)
    stream = io.StringIO()
    console = Console(file=stream, width=180, color_system=None, highlight=False)
    console.print("Next Dev Set V0 — local validation")
    console.print(
        f"Cases {len(selected)}  Passed {counts['PASS']}/{len(selected)}  "
        f"Failed {counts['FAIL']}  Errors {counts['ERROR']}  "
        f"Aborted {counts['ABORTED']}  Not run {counts['NOT_RUN']}"
    )
    console.print(
        f"Input {usage(totals['input_tokens'])}  Output {usage(totals['output_tokens'])}  "
        f"Usage coverage {totals['usage_coverage']}/{totals['input_tokens']['calls']}  "
        f"Model calls {number(totals['model_calls'])}  "
        f"Tool calls {number(totals['tool_calls'])}  "
        f"Agent time {number(totals['duration_ms'])} ms"
    )
    table = Table(
        "Case",
        "Result",
        "Input",
        "PeakCtx",
        "FinalCtx",
        "ToolResultBytes",
        "Tools",
        "Time",
        "Artifacts",
        box=None,
        padding=(0, 1),
    )
    for case_id in selected:
        result = by_id.get(case_id)
        if result is None:
            continue
        metrics = result["metrics"]
        available = metrics.get("available")
        table.add_row(
            case_id,
            result["status"],
            usage(metrics["usage"]["input_tokens"]) if available else "unknown",
            number(metrics.get("peak_context")),
            number(metrics.get("final_context")),
            number(metrics.get("tool_result_bytes") if available else None),
            number(metrics.get("tool_calls") if available else None),
            f"{metrics['duration_ms'] / 1000:.1f}s"
            if metrics.get("duration_ms") is not None
            else "unknown",
            case_id + "/",
        )
    console.print(table)
    for result in results:
        for diagnostic in result["diagnostics"]:
            console.print(
                Text(
                    terminal_text(
                        f"{result['case_id']}: {diagnostic['category']} — {diagnostic['reason']}"
                    )
                )
            )
    console.print(Text(f"Artifacts: {directory}"))
    report = stream.getvalue()
    (directory / "report.txt").write_text(report, encoding="utf-8")
    manifest_path = directory / "manifest.json"
    manifest = (
        json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {}
    )
    (directory / "report.html").write_text(render_html(summary, manifest), encoding="utf-8")
    return report


def render_html(summary: Json, manifest: Json) -> str:
    """Offline view of saved metrics; no JavaScript, remote assets or new statistics."""

    def text(value: object) -> str:
        return escape("unknown" if value is None else str(value))

    def duration(value: float | None) -> str:
        if value is None:
            return "unknown"
        seconds = value / 1000
        return f"{seconds / 60:.1f} min" if seconds >= 60 else f"{seconds:.1f} s"

    def byte_size(value: int | None) -> str:
        if value is None:
            return "unknown"
        for exponent, unit in enumerate(("B", "KiB", "MiB", "GiB")):
            size = value / 1024**exponent
            if size < 1024 or unit == "GiB":
                return f'<span title="{value:,} bytes">{size:,.1f} {unit}</span>'
        return "unknown"

    def link(path: str, label: str) -> str:
        # Artifacts are local relative paths. Encode filenames, never executable URLs.
        parts = path.replace("\\", "/").split("/")
        if any(part in ("", ".", "..") or ":" in part for part in parts):
            return text(label)
        return f'<a href="{quote("/".join(parts), safe="/")}">{text(label)}</a>'

    totals, counts = summary["totals"], summary["counts"]
    model = manifest.get("model", {})
    cards = "".join(
        f'<div class="card"><span>{label}</span><strong>{value}</strong></div>'
        for label, value in (
            ("验证通过", f"{counts['PASS']} / {len(summary['selected'])}"),
            ("验证失败", str(counts["FAIL"])),
            ("Total tokens", text(usage(totals["total_tokens"]))),
            ("Agent 耗时", duration(totals["duration_ms"])),
        )
    )
    by_id = {r["case_id"]: r for r in summary["cases"]}
    rows = []
    for case_id in summary["selected"]:
        result = by_id.get(case_id)
        if result is None:
            continue
        metrics = result["metrics"]
        available = metrics.get("available")
        agent = result.get("agent") or {}
        status = result["status"]
        tone = {"PASS": "pass", "FAIL": "fail", "ERROR": "fail"}.get(status, "neutral")
        diagnostics = "".join(
            f"<li><b>{text(d['category'])}</b> · {text(d['reason'])}</li>"
            for d in result["diagnostics"]
        )
        artifacts = " · ".join(
            link(f"{case_id}/{path}", label)
            for label, path in result["artifacts"].items()
            if isinstance(path, str)
        )
        details = "".join(
            f"<dt>{text(label)}</dt><dd>{text(value)}</dd>"
            for label, value in (
                ("Validator", result["validation"].get("reason")),
                ("Output tokens", usage(metrics["usage"]["output_tokens"]) if available else None),
                ("Model calls", number(metrics.get("model_calls"))),
                ("Compactions", number(metrics.get("compactions"))),
                (
                    "Usage coverage",
                    f"{metrics.get('usage_coverage', 0)} / {metrics.get('usage_calls', 0)}"
                    if available
                    else None,
                ),
            )
        )
        cells = (
            usage(metrics["usage"]["input_tokens"]) if available else "unknown",
            number(metrics.get("peak_context")),
            number(metrics.get("final_context")),
        )
        rows.append(
            f'<tr><th scope="row"><details><summary>{text(case_id)}</summary>'
            f'<div class="detail"><dl>{details}</dl><ul>{diagnostics}</ul>'
            f'<p class="links">{artifacts}</p></div></details></th>'
            f'<td><span class="badge {tone}">{text(status)}</span></td>'
            f'<td class="reason">{text(agent.get("outcome", "not started"))}'
            f"<small>{text(agent.get('reason') or '—')}</small></td>"
            + "".join(f'<td class="num">{text(cell)}</td>' for cell in cells)
            + '<td class="num">'
            f'{byte_size(metrics.get("tool_result_bytes") if available else None)}'
            f'</td><td class="num">{text(number(metrics.get("tool_calls") if available else None))}'
            f'</td><td class="num">{duration(metrics.get("duration_ms"))}</td></tr>'
        )
    coverage = f"{totals['usage_coverage']} / {totals['input_tokens']['calls']}"
    partial = any(
        value["reported"] is None or value["coverage"] < value["calls"]
        for value in (totals[field] for field in ("input_tokens", "output_tokens", "total_tokens"))
    ) or any(not r["metrics"].get("available") for r in summary["cases"])
    note = (
        "部分统计：≥ 表示已报告用量的下界；unknown 表示无数据，不是 0。"
        if partial
        else "Usage 完整。"
    )
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Nexus · Evaluation Report</title>
<style>
:root {{color-scheme:light; font-family:system-ui,"Segoe UI",sans-serif; color:#172b3a;
background:#f4f6f8; font-size:14px}}
* {{box-sizing:border-box}} body {{margin:0}}
main {{max-width:1560px;margin:auto;padding:40px 28px}}
.eyebrow {{color:#526879;letter-spacing:.15em;font-size:12px;font-weight:700}}
h1 {{font-size:30px;letter-spacing:-.03em;margin:8px 0 12px}} p {{line-height:1.7}}
.meta,footer {{color:#526879;overflow-wrap:anywhere}} .cards {{display:grid;
grid-template-columns:repeat(4,1fr);gap:16px;margin:28px 0 20px}}
.card {{background:white;border:1px solid #dce3e8;border-radius:12px;padding:20px}}
.card span {{display:block;color:#526879}}
.card strong {{display:block;font-size:28px;margin-top:8px}}
.notice {{border-left:3px solid #608ca7;background:#eaf1f5;padding:12px 16px;border-radius:4px}}
.table-wrap {{overflow:auto;max-height:70vh;border:1px solid #dce3e8;border-radius:12px;
background:white;margin-top:24px}} table {{border-collapse:separate;border-spacing:0;width:100%}}
caption {{text-align:left;padding:18px 20px;font-weight:600}}
th,td {{padding:16px 12px;border-top:1px solid #e5eaee;text-align:left;vertical-align:top}}
thead th {{position:sticky;top:0;background:#eef2f5;z-index:1;font-size:12px;white-space:nowrap}}
tbody th {{min-width:245px;max-width:370px;font-weight:500;overflow-wrap:anywhere}}
tbody tr:hover {{background:#f8fafc}} .num {{text-align:right;white-space:nowrap;
font-variant-numeric:tabular-nums}} .reason {{min-width:165px;overflow-wrap:anywhere}}
small {{display:block;color:#637483;margin-top:6px}} .badge {{display:inline-block;
border-radius:6px;padding:4px 8px;font-size:12px;font-weight:700;white-space:nowrap}}
.pass {{color:#176042;background:#e0f3e9}} .fail {{color:#9a293a;background:#fce7ea}}
.neutral {{color:#62551e;background:#f4efd8}} summary {{cursor:pointer;line-height:1.6}}
summary:focus-visible,a:focus-visible {{outline:2px solid #246991;outline-offset:3px}}
.detail {{font-size:12px;line-height:1.6}}
dl {{display:grid;grid-template-columns:auto 1fr;gap:6px 10px}}
dt {{color:#526879}} dd {{margin:0}} ul {{padding-left:18px}} a {{color:#21658d}}
.links {{line-height:2}} footer {{margin-top:20px;font-size:12px}}
@media(max-width:700px) {{main {{padding:24px 14px}} .cards {{grid-template-columns:repeat(2,1fr)}}
.card {{padding:14px}} .card strong {{font-size:22px}}}}
@media print {{main {{padding:0}} .table-wrap {{max-height:none;overflow:visible}}
thead th {{position:static}} .cards {{gap:8px}}}}
</style></head><body><main>
<div class="eyebrow">NEXUS / NEXT DEV SET V0</div><h1>Evaluation Report</h1>
<p class="meta">{text(manifest.get("evaluation_run_id"))}<br>
模型 {text(model.get("name"))} · Reasoning {text(model.get("reasoning_effort"))}
 · {text(manifest.get("context_policy"))}</p>
<section class="cards" aria-label="运行摘要">{cards}</section>
<p class="meta">ERROR {counts["ERROR"]} · ABORTED {counts["ABORTED"]}
 · NOT_RUN {counts["NOT_RUN"]} · Model calls {text(number(totals["model_calls"]))}
 · Tool calls {text(number(totals["tool_calls"]))} · Usage coverage {coverage}</p>
<p class="notice">{note}<br>验证结果与 Agent 停止原因独立；Agent 耗时不含环境准备和独立验证。</p>
<div class="table-wrap"><table><caption>Case 结果 · 点击名称展开诊断与产物</caption>
<thead><tr><th scope="col">Case</th><th scope="col">验证结果</th>
<th scope="col">Agent / 停止原因</th>
<th scope="col" class="num">Input tokens</th><th scope="col" class="num">PeakCtx</th>
<th scope="col" class="num">FinalCtx</th><th scope="col" class="num">ToolResultBytes</th>
<th scope="col" class="num">Tools</th><th scope="col" class="num">Agent time</th></tr></thead>
<tbody>{"".join(rows)}</tbody></table></div>
<footer>本地 Next Dev Set 验证，不代表官方 SWE-bench resolved。
上下文指标单位为 tokens；字节按 1024 换算，悬停可查看原始字节数。<br>
{link("summary.json", "summary.json")} · {link("manifest.json", "manifest.json")}
 · {link("report.txt", "report.txt")}</footer>
</main></body></html>"""
