# Agent Lab · 从源码学习 Agent

一套带本地浏览器界面的 Python Agent 学习框架。可以直接聊天、配置模型、查看工具过程、批准写入、导入知识库，再逐层阅读对应源码。Python **3.9+**，运行与测试均只需标准库。

默认使用离线规则模型，不需要 API Key；接入支持函数工具的 OpenAI 兼容 Chat Completions 服务后，可以执行自然语言任务。离线模式用于观察框架行为，本身没有大模型推理能力。

## 打开学习界面

macOS 用户直接双击项目目录中的 **`启动 Agent Lab.command`**。程序会打开终端、启动本地服务，并在浏览器打开 [Agent Lab](http://127.0.0.1:8765/)。保留这个终端窗口即可继续使用；重复双击会复用已启动的 Agent Lab。关闭服务时，在该终端按 **Control+C**。

打开后可以先用离线 Demo 发送 `/calc (20 + 1) * 2`，观察模型决策、计算器执行与最终回复。需要自然语言任务时，在界面设置中选择真实模型并填写 API 地址、模型名和 API Key。**通过界面输入的 Key 只保留在当前服务内存中；停止服务后需要重新填写。** 对话、知识库和非敏感设置保存在本机。

也可以在项目目录运行：

```bash
python3 -m agentlab serve --open
```

自定义端口使用 `python3 -m agentlab serve --port 9876 --open`。浏览器界面的完整说明见 [界面使用指南](docs/web-ui.md)。此服务只监听本机 `127.0.0.1`，不需要部署或注册网站。

## 从命令行观察框架

以下命令均在 `agent-lab` 目录运行：

```bash
# 先进入项目的 agent-lab 目录（克隆后即仓库根目录）
cd agent-lab

# 不需要 pip install，先观察完整的 model → tool → model 事件流
python3 -m agentlab --verbose run '/calc (20 + 1) * 2'

# 导入随项目提供的中文知识库，再通过 Agent 检索
python3 -m agentlab ingest knowledge
python3 -m agentlab run '/search Agent 工具调用 记忆'

# 启动多轮聊天（/quit 退出）
python3 -m agentlab chat --session learning

# 运行测试和离线评测
python3 -m unittest discover -s tests -v
python3 -m agentlab eval
```

第一条运行命令应输出包含 `42` 的工具结果，状态为 `completed`，模型调用 2 次、工具调用 1 次。Demo 的 token 数显示为 0，因为没有真实模型请求。原有 CLI 和 Python 库入口可与浏览器界面并行用于学习。

## 已实现的功能

| 能力 | 实现与入口 | 你可以观察什么 |
| --- | --- | --- |
| 浏览器界面 | 双击启动器 / `serve --open` | 聊天、模型配置、审批、知识库、运行过程 |
| Agent 循环 | `agentlab/agent.py` | 模型决策、多个工具结果回传、自然语言结束 |
| 模型适配 | Demo / Scripted / OpenAICompatible | 厂商协议与 Agent 逻辑分离、错误脱敏、有限重试 |
| 工具系统 | 注册表、参数 Schema、同步/异步处理器 | 白名单、严格类型检查、超时、输出截断 |
| 内置工具 | 计算器、读写文件、知识检索、记住/回忆 | 可直接执行的 6 个工具 |
| 人工审批 | `approve` / `deny` | 写入前暂停、查看具体参数、重启后继续 |
| 对话与持久化 | SQLite 会话、检查点、事件、租约 | 多轮上下文、同会话互斥、中断显式恢复 |
| 长期记忆 | `remember` / `recall` | 按会话隔离、跨进程保存、明确写入授权 |
| RAG | UTF-8 Markdown/TXT 导入、中英文词法 BM25 检索 | 分块、重叠、去重、更新删除、来源与 chunk 引用 |
| 上下文管理 | 按完整对话轮次裁剪 | 保留工具调用与响应配对，避免破坏协议 |
| 规划与结构化输出 | `agentlab/planning.py` | JSON 校验、有限修复、计划 DAG 校验后执行 |
| 多 Agent 协作 | DAG + 独立 Agent / 工具权限 | 有限并发、任务依赖、失败传播、重试与取消 |
| 可观测性 | `--verbose`、`trace`、`inspect` | 实时运行事件、持久化结果、工具轨迹和 usage |
| 运行约束 | 步数、工具次数、上下文、token、时间预算 | 超限停止，防止无边界循环 |
| 评测 | `agentlab/evaluation.py` / `eval` | 输出、状态、工具顺序断言与通过率 |

## 审批、记忆与会话恢复

```bash
# 只产生审批检查点，此时尚未写入文件
python3 -m agentlab run '/write notes/day1.txt 今天学会了工具调用' --session notebook

# 阅读参数后执行批准；也可用 --call CALL_ID 只批准指定操作
python3 -m agentlab approve notebook --all
python3 -m agentlab run '/read notes/day1.txt' --session notebook

# 持久化个人学习目标，remember 也属于写入，需要批准
python3 -m agentlab run '/remember goal 掌握Agent执行循环' --session notebook
python3 -m agentlab approve notebook --all
python3 -m agentlab run '/recall goal' --session notebook

# 观察保存的会话、消息及事件
python3 -m agentlab sessions
python3 -m agentlab inspect notebook
python3 -m agentlab trace notebook
```

用 `deny SESSION` 拒绝待审批操作。交互聊天中可以直接输入 `/approve` 或 `/deny`。批准与调用 ID、参数所在检查点、工作目录、工具声明和模型端点绑定；下一次新的写入仍需审批。

默认状态存入 `.agentlab/agentlab.sqlite3`，工具只可读写 `workspace/`。全局选项放在子命令之前：

```bash
python3 -m agentlab --data-dir .state-demo --workspace my-files --json run '/calc 9 * 9'
```

恢复时沿用相同的配置。CLI 打印的审批命令会保留这些选项。如果进程异常退出，状态留在 `running`，待运行租约到期后执行 `python3 -m agentlab recover SESSION`。恢复会记录未知结果并结束中断的运行，**不会重放工具操作**；检查工作区后再发起任务。

## 接入真实模型

浏览器用户直接在设置中填写模型、API 根地址与 Key 并保存即可。以下环境变量方式供 CLI 使用：

```bash
export AGENTLAB_API_KEY='替换为你的密钥'
export AGENTLAB_MODEL='替换为支持函数工具的模型名'
export AGENTLAB_BASE_URL='https://api.openai.com/v1'

python3 -m agentlab --provider openai run '请用工具计算 (126 + 78) * 3，并解释结果'
python3 -m agentlab --provider openai chat --session real-learning
```

可将 `AGENTLAB_BASE_URL` 改为其他兼容服务的 API 根地址，程序追加 `/chat/completions`。`.env.example` 仅是变量说明，程序不自动读取 `.env`。真实模型调用会由对应服务计费。API 兼容条件、重试与配置详见 [模型适配器](docs/providers.md)。

使用 DeepSeek 时可设置 `AGENTLAB_MODEL=deepseek-flash`、`AGENTLAB_BASE_URL=https://api.deepseek.com`，并使用 DeepSeek 的 Key。框架对该官方地址自动关闭思考模式，以兼容当前工具消息协议。

目前适配器使用非流式 Chat Completions 和 `max_tokens`；不兼容只接受 `max_completion_tokens` 或 Responses API 的模型。`--verbose` 显示运行事件，不是逐 token 文本流。没有配置真实凭据时，所有验证都使用离线模型和模拟 HTTP。

## 学习顺序

1. [学习路线与实验](docs/learning-path.md)：按七节课逐层走通。
2. [架构与运行状态](docs/architecture.md)：理解职责、数据流和审批检查点。
3. [工具与权限](docs/tools.md)：添加自己的 API/业务工具。
4. [模型适配器](docs/providers.md)：学习请求序列化、重试和响应校验。
5. [记忆与工作流](docs/memory-workflows.md)：理解检索索引、会话租约、DAG。
6. [配置、边界与排障](docs/operations.md)：修改预算、定位失败、扩展到实际项目。
7. [验证记录](docs/validation.md)：自动化测试及示例运行结果。

四个例子均使用临时数据，不会改变你的现有项目：

```bash
python3 -m examples.basic
python3 -m examples.custom_tool
python3 -m examples.multi_agent
python3 -m examples.plan_and_execute
```

也可以直接运行 `python3 -m agentlab workflow`，观察“计算 + 检索 → 汇总”。先执行 `ingest knowledge` 可以看到带来源的检索结果。

## 作为库使用

```python
import asyncio
from agentlab import Agent, DemoProvider, SQLiteStore

async def main():
    with SQLiteStore(".agentlab/app.sqlite3") as store:
        agent = Agent(DemoProvider(), store=store, workspace="workspace")
        result = await agent.run("/calc 6 * 7", session_id="my-app")
        print(result.status, result.output)

asyncio.run(main())
```

若需要命令行入口 `agentlab`，可在虚拟环境执行 `python3 -m pip install -e .`；构建工具可能需要联网安装，但直接 `python3 -m agentlab` 不需要安装。

开发者修改 README、`docs/` 或 `knowledge/` 后，运行 `python3 scripts/sync_web_resources.py` 更新包内文档与示例快照；`--check` 可只检查是否同步。源码运行优先读取原文，安装包使用随包分发的资源。

## 项目结构

```text
agentlab/
  types.py         消息、工具调用、模型响应协议
  agent.py         Agent 状态机、审批、预算、恢复
  providers.py     三种模型实现
  tools.py         工具注册、校验、6 个内置工具
  storage.py       SQLite、记忆、知识索引、会话租约
  workflows.py     DAG 调度、并发、失败传播
  planning.py      结构化输出与计划执行
  evaluation.py    回归评测
  cli.py           命令行与聊天入口
  server.py        本地 HTTP API 与后台任务
  launcher.py      macOS 双击启动与服务复用
  web/             浏览器界面的 HTML、CSS、JavaScript
启动 Agent Lab.command  macOS 双击入口
examples/          四个可运行教学例子
knowledge/         中文示例知识库
tests/             单元测试和 CLI 集成测试
docs/              分模块讲解、实验与排障
```

这是一套本地学习和二次开发框架。它不提供操作系统级沙箱、多租户服务、分布式队列、向量数据库或 MCP 服务；相关接口可以在理解核心流程后扩展。浏览器界面用于本机个人学习，检索为词法匹配，DAG 不提供跨进程自动续跑。更具体的约束见 [运行边界](docs/operations.md)。
