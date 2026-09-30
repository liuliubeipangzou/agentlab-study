"""运行：python3 -m examples.custom_tool；没有网络或真实模型费用。"""
import asyncio
import tempfile

from agentlab.agent import Agent
from agentlab.providers import ScriptedProvider
from agentlab.tools import Tool, ToolRegistry
from agentlab.types import ModelResponse, ToolCall


async def temperature(arguments, context):
    return {"fahrenheit": arguments["celsius"] * 9 / 5 + 32}


async def main():
    registry = ToolRegistry()
    registry.register(Tool("temperature", "把摄氏温度转成华氏温度", {
        "type": "object", "properties": {"celsius": {"type": "number", "minimum": -273.15}},
        "required": ["celsius"], "additionalProperties": False,
    }, temperature))
    provider = ScriptedProvider([ModelResponse(tool_calls=[ToolCall("temperature", {"celsius": 25})]),
                                 ModelResponse("25°C 对应 77°F。此结论是脚本预设，用于验证工具协议。")])
    with tempfile.TemporaryDirectory() as workspace:
        agent = Agent(provider, tools=registry, workspace=workspace)
        try:
            print((await agent.run("25°C 转成华氏温度")).output)
            print("模型第二次接收的工具结果：", provider.calls[1]["messages"][-1].content)
        finally:
            agent.store.close()


if __name__ == "__main__":
    asyncio.run(main())
