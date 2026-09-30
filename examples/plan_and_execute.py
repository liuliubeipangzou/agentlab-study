"""运行：python3 -m examples.plan_and_execute；预设计划 + 真实 Agent 工具执行。"""
import asyncio
import json
import tempfile

from agentlab.agent import Agent
from agentlab.planning import Planner
from agentlab.providers import DemoProvider, ScriptedProvider
from agentlab.types import ModelResponse


async def main():
    # 换成 OpenAICompatibleProvider 后，计划由真实模型生成；其余代码无需改动。
    provider = ScriptedProvider([ModelResponse(json.dumps({"steps": [
        {"name": "first", "instruction": "/calc 6 * 7", "depends_on": []},
        {"name": "second", "instruction": "/calc 100 - 42", "depends_on": ["first"]},
    ]}))])
    planner = Planner(provider)
    plan = await planner.plan("计算 6 * 7，然后计算它与 100 的差")
    print("校验后的计划：", json.dumps(plan, ensure_ascii=False))
    with tempfile.TemporaryDirectory() as workspace:
        agent = Agent(DemoProvider(), workspace=workspace)
        try:
            async def runner(instruction, dependencies):
                # Demo 只识别斜线命令；真实模型可接收序列化的依赖结果作为任务数据。
                print("已完成的依赖：", list(dependencies))
                result = await agent.run(instruction)
                if result.status != "completed":
                    raise RuntimeError("步骤需处理：" + result.session_id + " " + result.status)
                return result.output
            result = await planner.execute(plan, runner)
            print(result.outputs)
        finally:
            agent.store.close()


if __name__ == "__main__":
    asyncio.run(main())
