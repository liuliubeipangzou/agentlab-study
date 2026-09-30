"""结构化输出和 Plan-and-Execute：先验证计划，再交给 DAG 执行器。

Planner 只生成声明式任务依赖，不生成可执行代码。Runner 决定任务的具体能力。
"""
import asyncio
import copy
import json
from typing import Callable, List

from .tools import validate_schema
from .types import Message, Provider
from .workflows import Workflow, WorkflowStep


class StructuredOutputError(ValueError):
    pass


def parse_structured(text: str, schema: dict):
    """解析完整 JSON（允许外层 ```json fence），拒绝多余文本/重复键/非有限值。"""
    if len(text) > 64000:
        raise StructuredOutputError("结构化输出超过 64000 字符限制")
    stripped = text.strip()
    if stripped.startswith("```json\n") and stripped.endswith("\n```"):
        stripped = stripped[8:-4]
    def pairs(items):
        value = {}
        for key, item in items:
            if key in value:
                raise ValueError("重复 JSON key: " + key)
            value[key] = item
        return value
    def invalid_constant(value):
        raise ValueError("非有限数值: " + value)
    try:
        value = json.loads(stripped, object_pairs_hook=pairs, parse_constant=invalid_constant)
        validate_schema(value, schema)
        return value
    except (ValueError, TypeError, RecursionError) as exc:
        raise StructuredOutputError("结构化输出校验失败：" + type(exc).__name__) from None


async def structured_complete(provider: Provider, prompt: str, schema: dict, retries: int = 1,
                              timeout: float = 30):
    """校验不通过时提供一次修复机会；没有工具执行权限。"""
    if type(retries) is not int or not 0 <= retries <= 3:
        raise ValueError("retries 必须在 0 到 3 之间")
    if type(timeout) not in (int, float) or not 0 < timeout <= 300:
        raise ValueError("timeout 必须在 0 到 300 秒之间")
    messages = [Message("system", "仅输出满足以下 JSON Schema 的 JSON，不要输出其他文字：\n" + json.dumps(schema, ensure_ascii=False)),
                Message("user", prompt)]
    for attempt in range(retries + 1):
        response = await asyncio.wait_for(provider.complete(messages, []), timeout=timeout)
        try:
            if response.tool_calls:
                raise StructuredOutputError("结构化输出不允许调用工具")
            return parse_structured(response.content, schema)
        except StructuredOutputError:
            if attempt == retries:
                raise
            # 无效回复不加入上下文；避免重复送入大段不可信数据或悬空工具调用。
            messages.append(Message("user", "上一回复未通过完整 JSON / Schema 校验。请重新输出符合 schema 的 JSON。"))


PLAN_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["steps"],
    "properties": {"steps": {"type": "array", "minItems": 1, "maxItems": 8,
        "items": {"type": "object", "additionalProperties": False,
            "required": ["name", "instruction", "depends_on"], "properties": {
                "name": {"type": "string", "minLength": 1, "maxLength": 64},
                "instruction": {"type": "string", "minLength": 1, "maxLength": 4000},
                "depends_on": {"type": "array", "maxItems": 8, "items": {"type": "string"}},
            }}}},
}


class Planner:
    def __init__(self, provider: Provider):
        self.provider = provider

    async def plan(self, task: str) -> List[dict]:
        value = await structured_complete(self.provider,
            "为下面任务制定不超过 8 个步骤的计划。步骤名唯一，depends_on 只能引用计划内的步骤，依赖不能成环。\n任务：" + task,
            PLAN_SCHEMA)
        steps = value["steps"]
        async def noop(results):
            return None
        # 复用 DAG 校验以保证执行器能消费该计划。
        Workflow([WorkflowStep(s["name"], noop, s["depends_on"]) for s in steps])
        return steps

    async def execute(self, plan: List[dict], runner: Callable, concurrency: int = 3):
        """runner(instruction, dependency_outputs) 必须是异步函数，可在内部调用 Agent.run。"""
        validate_schema({"steps": plan}, PLAN_SCHEMA)
        steps = []
        for item in copy.deepcopy(plan):
            async def run(results, step=item):
                dependencies = {name: results[name] for name in step["depends_on"]}
                return await runner(step["instruction"], dependencies)
            steps.append(WorkflowStep(item["name"], run, list(item["depends_on"])))
        return await Workflow(steps, concurrency=concurrency).run()
