"""运行：python3 -m examples.multi_agent；两名 Agent 并行执行，随后汇总。"""
import asyncio
import tempfile
from pathlib import Path

from agentlab.agent import Agent
from agentlab.providers import DemoProvider
from agentlab.storage import SQLiteStore
from agentlab.tools import ToolRegistry, create_builtin_tools
from agentlab.workflows import Workflow, WorkflowStep


async def main():
    with tempfile.TemporaryDirectory() as workspace:
        with SQLiteStore(":memory:") as store:
            store.ingest(Path(__file__).resolve().parents[1] / "knowledge")
            # 最小权限：计算员只拿 calculator，研究员只拿 search_knowledge。
            builtin = create_builtin_tools()
            math_tools, research_tools = ToolRegistry(), ToolRegistry()
            math_tools.register(builtin.get("calculator"))
            research_tools.register(builtin.get("search_knowledge"))
            mathematician = Agent(DemoProvider(), tools=math_tools, store=store, workspace=workspace)
            researcher = Agent(DemoProvider(), tools=research_tools, store=store, workspace=workspace)
            async def calculate(outputs):
                result = await mathematician.run("/calc 60 / 3")
                if result.status != "completed":
                    raise RuntimeError(result.output)
                return result.output
            async def research(outputs):
                result = await researcher.run("/search Agent 记忆")
                if result.status != "completed":
                    raise RuntimeError(result.output)
                return result.output
            async def summarize(outputs):
                return "每天学习时长：\n" + outputs["math"] + "\n参考资料：\n" + outputs["research"]
            result = await Workflow([
                WorkflowStep("math", calculate), WorkflowStep("research", research),
                WorkflowStep("report", summarize, depends_on=["math", "research"]),
            ], concurrency=2).run()
            print(result.statuses)
            print(result.outputs.get("report", result.errors))


if __name__ == "__main__":
    asyncio.run(main())
