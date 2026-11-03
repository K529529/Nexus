"""Small shared event projection for live and saved agent usage."""

from dataclasses import dataclass, field

from nexus.core.types import Json, RuntimeEvent


@dataclass
class AgentUsage:
    started: int = 0
    finished: int = 0
    reports: list[Json] = field(default_factory=list)

    def observe(self, event: RuntimeEvent) -> None:
        if event.kind == "model_started":
            self.started += 1
        elif event.kind == "model_finished":
            self.finished += 1
            self.reports.append(event.data.get("usage") or {})

    def public(self) -> Json:
        result: Json = {"requests": self.started, "finished": self.finished}
        for name in ("input_tokens", "output_tokens", "total_tokens", "cached_input_tokens"):
            known = [r[name] for r in self.reports if type(r.get(name)) is int]
            complete = self.started == self.finished == len(known)
            result[name] = sum(known) if complete else None
            result[name + "_known"] = sum(known)
            result[name + "_coverage"] = len(known)
        result["cost"] = None  # No price guess; cached tokens are a subset of input.
        return result


def usage_label(usage: Json) -> str:
    total = usage.get("total_tokens")
    return f"{total} tok" if total is not None else f"≥{usage.get('total_tokens_known', 0)} tok?"


def combined_usage(ledgers: list[AgentUsage]) -> Json:
    return AgentUsage(sum(v.started for v in ledgers), sum(v.finished for v in ledgers),
                      [r for v in ledgers for r in v.reports]).public()
