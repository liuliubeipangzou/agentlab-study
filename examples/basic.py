"""运行：python3 -m examples.basic"""
import asyncio
import tempfile
from pathlib import Path

from agentlab.agent import Agent
from agentlab.providers import DemoProvider
from agentlab.storage import SQLiteStore


async def main():
    with tempfile.TemporaryDirectory() as directory:
        with SQLiteStore(Path(directory) / "state.sqlite3") as store:
            agent = Agent(DemoProvider(), store=store, workspace=directory,
                          on_event=lambda event: print("EVENT", event["type"]))
            result = await agent.run("/calc (20 + 1) * 2", session_id="lesson-1")
            print(result.output)
            print("可持久化的消息数：", len(store.load_session("lesson-1")["messages"]))


if __name__ == "__main__":
    asyncio.run(main())
