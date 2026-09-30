# 持久化、知识检索与工作流

对应源码：`agentlab/storage.py`、`agentlab/workflows.py`。两个模块仅使用 Python 标准库，可独立阅读和运行。

## 三种状态各自负责什么

| 数据 | 用途 | 方法 |
| --- | --- | --- |
| Session checkpoint | 保存一次会话的完整 JSON 状态，用于恢复对话上下文 | `save_session` / `load_session` |
| Event log | 按产生顺序记录事件，便于解释执行过程 | `append_event` / `events` |
| Session memory | 保存用户偏好、摘要等显式键值记忆 | `remember` / `recall` |

```python
from agentlab.storage import SQLiteStore

with SQLiteStore(".agentlab/state.db") as store:
    store.save_session("lesson", {"status": "ready", "messages": []})
    store.remember("lesson", "preferred_language", "中文")
    store.append_event("lesson", {"kind": "memory_saved"})
    print(store.recall("lesson", "language"))
```

`save_session` 原子替换整个 checkpoint；新状态中没有的旧字段会被移除。JSON 序列化失败或 SQL 事务失败时，已有数据保持完整。记忆和事件按 session 隔离，`delete_session` 在一个事务中删除该会话的状态、事件、记忆及租约。

连接使用 `check_same_thread=False`，由 `RLock` 保护，因此可以通过 `asyncio.to_thread` 使用同一个 store。文件数据库启用 WAL；多个进程的写入由 SQLite 协调。数据库保存明文，适合本地学习；不要把真实凭据放进会话或知识文件。

这些记录提供持久化和可观察性，不保证外部副作用恰好执行一次。例如 HTTP 写请求成功后进程崩溃，数据库可能来不及记录成功。恢复会话时，应检查未完成的工具调用，不能自动重放写操作。需要更强保证的工具应使用下游支持的幂等键。

## 会话租约

```python
import uuid

owner = uuid.uuid4().hex
if not store.acquire_session("lesson", owner, ttl=300):
    raise RuntimeError("此会话正在被另一个执行者使用")
try:
    # 执行一个 turn；耗时较长时用相同 owner 提前续约。
    store.save_session("lesson", {"status": "completed"})
finally:
    store.release_session("lesson", owner)
```

同一 owner 再次 acquire 会续约；不同 owner 只能在租约过期或释放后获得它。错误 owner 不能释放别人的租约。TTL 使用系统时钟，进程崩溃后会自然过期；超过 TTL 的长任务必须主动续约。租约是调用方遵守的互斥约定，不是数据库对每次写入的强制鉴权。每个并发执行者应使用唯一 owner，租约失效后应停止后续副作用。

## 可解释的本地检索

```python
from pathlib import Path

print(store.ingest(Path("knowledge")))
for hit in store.search("智能体记忆 agent memory", limit=3):
    print(f"[{hit['source']}#{hit['chunk']}] {hit['text']}")
```

导入接受 UTF-8 `.md` 和 `.txt`；递归遍历时跳过隐藏目录、隐藏文件、符号链接和其他文件类型。默认一次最多 1,000 个文档、20,000 个目录条目、单文件 2 MiB、总计 32 MiB。读取或校验失败会保留原来的索引。单个 chunk 为最多 800 个字符，相邻 chunk 重叠 120 个字符；编号从 1 开始。

检索将英文拆为单词，将中文拆为单字和相邻双字，再使用 BM25 按词频、逆文档频率和长度归一化计算分数。返回 `source / chunk / text / score`，可以直接形成引用。这是词法检索，不具备 embedding 的语义相似能力；同义词可能需要显式写进问题。

重复导入通过文件内容哈希识别未变化文档，不重复创建 chunk。文件修改后，旧 chunk 和词项在同一事务中被替换。重新导入目录会移除已消失文件的索引；如果另一个导入根也登记过该文件，其索引会继续保留，直到所有相应目录完成清理。独立导入单文件会保留该文件的快照，单文件删除后重新导入会报文件不存在。API 返回本次根目录的文档/chunk 数，以及 `added / updated / unchanged / removed` 计数。

## 并行 DAG 工作流

```python
import asyncio
from agentlab.workflows import Workflow, WorkflowStep

async def research(inputs):
    return "查到的背景资料"

async def constraints(inputs):
    return ["回答要简洁", "引用需要来源"]

async def write(inputs):
    return {"draft": inputs["research"], "rules": inputs["constraints"]}

async def main():
    workflow = Workflow([
        WorkflowStep("research", research, retries=1, timeout=10),
        WorkflowStep("constraints", constraints),
        WorkflowStep("write", write, depends_on=["research", "constraints"]),
    ], concurrency=2)
    result = await workflow.run({"topic": "Agent 的记忆机制"})
    print(result.ok, result.statuses, result.outputs, result.errors)

asyncio.run(main())
```

`research` 和 `constraints` 可以并行运行；只有两者成功，`write` 才会启动。每个异步步骤只拿到 `initial` 输入及显式 `depends_on` 的输出，并使用深拷贝隔离嵌套数据。每次重试重新创建输入快照；没有依赖关系的分支无法读取彼此输出。输入 key 不能与步骤名重合，值需要支持 `deepcopy`。

构造工作流时检查重复步骤名、缺失依赖和循环依赖。结果的 `statuses` 是 `success`、`failed` 或 `skipped`，`errors` 包含失败和跳过原因，`outputs` 包含初始输入及成功步骤结果。失败步骤的后继会跳过；其他独立分支继续执行。`result.ok` 表示所有步骤成功。

`timeout` 限制每次尝试，`retries=2` 表示最多尝试 3 次。默认不重试；开启重试前，应确保步骤幂等。取消整个 workflow 会取消正在运行的协程并等待它们清理。Python 的异步取消是协作式的：吞掉取消信号的协程或已经启动的后台线程，无法靠此 runner 强制终止。因此耗时网络操作还应设置自己的底层超时。

把多个 Agent 的 `run` 包装为步骤闭包，就能构成研究、评审、汇总等多 Agent 流程。需要独立对话状态的 Agent 应使用不同 session；依赖关系通过步骤输出传递。工作流 runner 本身不持久化 DAG 进度，也不在崩溃后自动重试。

## 建议的学习实验

1. 修改同一个知识文件并重新导入，验证旧关键词检索结果消失。
2. 用两个 store 打开同一个数据库，尝试同时获取同一会话租约。
3. 给工作流增加一个必定失败的分支，观察后继跳过、独立分支成功。
4. 分别测试超时、重试和取消，观察它们对步骤清理逻辑的影响。

运行对应测试：

```bash
python3 -m unittest discover -s tests -p 'test_storage.py' -v
python3 -m unittest discover -s tests -p 'test_workflows.py' -v
```
