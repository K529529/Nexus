"""Offline agent tree and parallel lanes embedded in the existing case report."""

from html import escape

from nexus.app.agent_history import elapsed_ms
from nexus.app.agent_view import usage_label
from nexus.core.types import Json, json_text


def render_agents(facts: Json, case_id: str) -> str:
    if not facts.get("delegated"):
        return ""
    agents = facts["agents"]
    operations = [op for a in agents for op in a["timeline"]]
    start = min((op["start"] for op in operations), default=None)
    end = max((op["end"] for op in operations), default=None)
    span = max(1, elapsed_ms(start, end)) if start and end else 1
    options = ['<option value="all">所有代理</option>']
    lanes = []
    for agent in agents:
        ident = agent["agent_id"]
        title = f"{'↳ ' * agent.get('depth', 0)}{ident[:8]} · {agent['role']} · {agent['state']}"
        options.append(f'<option value="{escape(ident, quote=True)}">{escape(title)}</option>')
        bars = []
        for operation in agent["timeline"]:
            left = elapsed_ms(start, operation["start"]) / span * 100 if start else 0
            width = max(.2, elapsed_ms(operation["start"], operation["end"]) / span * 100)
            label = f"{operation['name']} {operation['start']} → {operation['end']}"
            bars.append(f'<span class="agent-bar {operation["kind"]}" '
                        f'style="left:{left:.4f}%;width:{width:.4f}%" '
                        f'title="{escape(label, quote=True)}"></span>')
        events = "\n\n".join(f"{e['timestamp']} {e['kind']}\n{json_text(e['data'])}"
                            for e in agent["events"])
        lanes.append(f'<article data-agent="{escape(ident, quote=True)}">'
            f'<h3>{escape(title)}</h3><p>{escape(agent.get("assignment", ""))}</p>'
            f'<p>Parent {escape(str(agent.get("parent_id") or "—"))} · '
            f'{escape(str(agent.get("model") or "root model"))} · '
            f'Reasoning {escape(str(agent.get("reasoning_effort") or "default"))} · '
            f'{escape(usage_label(agent["usage"]))}</p>'
            f'<div class="agent-lane">{"".join(bars)}</div>'
            f'<details><summary>消息 / 等待 / 通信与工具轨迹</summary><pre>{escape(events)}</pre>'
            '</details></article>')
    warnings = " · ".join(facts.get("warnings", []))
    return (f'<section class="agent-trace" id="trace-{escape(case_id, quote=True)}">'
        f'<h2>{escape(case_id)} · Agent tree / parallel timeline</h2>'
        f'<p>全树 {escape(usage_label(facts["usage"]))} · 费用 unknown · '
        '并行时长不相加；横轴使用实际时间，悬停查看操作时间。</p>'
        f'<label>按代理筛选 <select class="agent-filter">{"".join(options)}</select></label>'
        f'<p>{escape(warnings)}</p>{"".join(lanes)}</section>')


STYLE = """
.agent-trace{margin-top:24px;padding:20px;background:white;border:1px solid #dce3e8;
border-radius:12px}.agent-trace article{padding:12px 0;border-top:1px solid #e5eaee}
.agent-trace h3{font-size:14px}.agent-trace p{overflow-wrap:anywhere;font-size:12px}
.agent-trace pre{white-space:pre-wrap;overflow-wrap:anywhere;max-height:450px;overflow:auto;
background:#f4f6f8;padding:14px}.agent-lane{height:18px;position:relative;background:#eef2f5;
margin:12px 0}.agent-bar{position:absolute;height:100%;min-width:2px;background:#8b5cf6}
.agent-bar.tool{background:#1e9b8a}.agent-filter{padding:6px;margin:8px}
"""

SCRIPT = """
document.querySelectorAll('.agent-filter').forEach(select=>{
 select.addEventListener('change',()=>{
  select.closest('.agent-trace').querySelectorAll('[data-agent]').forEach(lane=>{
   lane.hidden=select.value!=='all'&&lane.dataset.agent!==select.value;
  });
 });
});
"""
