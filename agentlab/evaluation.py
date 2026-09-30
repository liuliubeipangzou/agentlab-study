"""小型回归评测：结果断言、工具轨迹断言与成功率。"""
from dataclasses import dataclass, field
from typing import Callable, List


@dataclass
class EvalCase:
    name: str
    prompt: str
    contains: List[str] = field(default_factory=list)
    expected_tools: List[str] = field(default_factory=list)
    expected_status: str = "completed"


async def evaluate(agent_factory: Callable, cases: List[EvalCase]) -> dict:
    """每例由 factory 提供独立 Agent；不要让上一个用例的会话污染当前用例。"""
    rows = []
    for case in cases:
        agent = agent_factory()
        result = await agent.run(case.prompt)
        events = agent.store.events(result.session_id)
        actual_tools = [e["data"]["name"] for e in events if e["type"] == "tool_started"]
        checks = {"status": result.status == case.expected_status,
                  "content": all(value in result.output for value in case.contains),
                  "tools": actual_tools == case.expected_tools}
        rows.append({"name": case.name, "passed": all(checks.values()), "checks": checks,
                     "output": result.output, "actual_tools": actual_tools})
    passed = sum(row["passed"] for row in rows)
    return {"total": len(rows), "passed": passed, "pass_rate": passed / len(rows) if rows else 0.0, "cases": rows}
