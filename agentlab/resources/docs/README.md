# Agent Lab · 可用的本地 Agent

一套带本地浏览器界面的 Python Agent 框架。**默认使用真实模型**，用自然语言驱动 12 个内置工具完成检索、抓取、调用 API、执行代码与文件操作。Python **3.9+**，运行与测试只需标准库。

填入 API Key 即可开始；离线规则演示模式仍然保留，但需要显式开启，仅用于观察框架行为。

## 快速开始

```bash
# 1. 配置真实模型（任一 OpenAI 兼容服务）
export AGENTLAB_API_KEY='你的密钥'
export AGENTLAB_MODEL='deepseek-flash'                      # 换成支持函数工具的模型名
export AGENTLAB_BASE_URL='https://api.deepseek.com'         # 换成你的服务地址

# 2. 用自然语言提任务
python3 -m agentlab run '搜索一下 Python 3.13 的新特性，然后用 Python 算一下它们的数量'

# 3. 交互式多轮对话（--stream 可逐字输出）
python3 -m agentlab chat --session work --stream

# 4. 单次任务流式输出
python3 -m agentlab run --stream '用三句话解释什么是 BM25'
```

**流式说明**：`--stream` 现在**支持工具调用回合**——流式分片里的 `tool_calls` 参数会按
index 对齐重组，经完整校验后才执行，不会执行半截参数。未实现 `stream()` 的 Provider
（如 `DemoProvider`）会自动回退到普通请求。

也可以在浏览器界面里配置：运行 `python3 -m agentlab serve --open`，在**设置**中填写模型、API 地址与 Key 并保存。**界面输入的 Key 只保留在服务内存中，停止服务后需重新填写。**

## 内置工具（12 个）

| 工具 | 作用 | 风险 |
| --- | --- | --- |
| `web_search` | 联网检索，返回标题/链接/摘要。默认免密钥后端，也可配 Brave / Tavily / SearXNG | 读 |
| `fetch_url` | 抓取网页并转为纯文本，便于阅读正文 | 读 |
| `http_request` | 调用外部 HTTP API，支持自定义方法与请求头 | 读（POST/PUT/PATCH/DELETE 需审批） |
| `run_python` | 在受限子进程中执行 Python，用 `result` 返回数据 | **写**（需审批） |
| `search_knowledge` | 检索本地知识库（中英文 BM25） | 读 |
| `read_file` / `list_files` / `write_file` | 工作区内文件读取、列目录与写入，写入需审批 | 读 / 读 / **写** |
| `calculator` | AST 白名单数值计算，不使用 `eval` | 读 |
| `remember` / `recall` | 会话级键值记忆，写入需审批 | **写** / 读 |
| `search_memory` | **跨会话**长期记忆检索（BM25 + IDF 排序） | 读 |

所有联网请求都经过内置的 **SSRF 防护**：拒绝回环、私网、链路本地、CGNAT 与云元数据地址（含 IPv6 与映射地址），DNS 解析出的每个地址都要通过校验，并在校验后的 IP 上建立连接以消除重绑定窗口；默认不自动跟随重定向，`Host`/`Content-Length` 等请求头禁止模型覆盖。

## 安全边界（请务必了解）

- **`run_python` 不是沙箱。** 代码在独立子进程中以**当前用户身份**运行，仍可访问文件系统与网络。框架提供了超时（默认 20 秒）、内存上限（默认 1 GiB，macOS 用 `libproc` 实时 RSS 监控 + Linux 用 `/proc`）、进程组终止与环境变量净化（密钥不会传入子进程），但**不提供操作系统级隔离**。要执行不可信代码，请自行接入容器或 `seccomp`。
- **SSRF 防护是应用层防线**，作用于工具调用路径；子进程内的 `socket` 不受其约束。
- 工作目录限制、路径规范化（`O_NOFOLLOW` + `dir_fd`）是教学级防线，**不是**操作系统沙箱。
- 检索结果、网页正文、API 响应都是**不可信输入**，Agent 会被告知把它们当作数据而不是指令。
- 所有数据（会话、消息、知识库）以**明文**存在本机 SQLite；不要把真实凭据写进会话或知识文件。

## 记忆：会话内与跨会话

三种记忆各自独立：

| 类型 | 存储 | 作用域 | 检索方式 |
| --- | --- | --- | --- |
| 短期对话 | `sessions.messages` | 单个会话 | 按完整轮次裁剪后送入模型 |
| 会话记忆 | `memories` 表 | 单个会话 | `recall`，子串匹配 |
| **跨会话记忆** | `memories` 表 | **全部会话** | `search_memory`，BM25 + IDF 排序 |

`search_memory` 让 Agent 能找回你在**其它会话**里保存过的偏好与事实（例如"我之前说用什么
Python 版本"）。排序使用与知识库一致的 BM25，并用 IDF 抑制噪声：像"索"这类几乎每条记忆都
含有的字权重趋近 0，只有真正罕见的词才有区分度；再叠加查询词覆盖率与相对分数门槛，
避免"股票行情"因共享一个单字而误命中"检索笔记"。

`exclude_current=true` 可把当前会话排除在外。检索结果带 `session_id`，可以追溯到来源会话。
**这些记忆在会话之间是相互可见的**，属于个人偏好类数据；同机其它工具能读到本机 SQLite，
因此不要往记忆里放密钥。

## 从命令行观察框架

以下命令均在 `agent-lab` 目录运行：

```bash
# 观察完整的 model → tool → model 事件流
python3 -m agentlab --verbose run '/calc (20 + 1) * 2'

# 观察真实模型自主选择工具（需要已配置密钥）
python3 -m agentlab run '计算 (126 + 78) * 3 并解释结果'

# 导入随项目提供的中文知识库，再通过 Agent 检索
python3 -m agentlab ingest knowledge
python3 -m agentlab run '/search Agent 工具调用 记忆'

# 离线规则演示模式（不消耗 Token，仅用于观察框架）
python3 -m agentlab --provider demo run '/calc (20 + 1) * 2'

# 运行测试和离线评测
python3 -m unittest discover -s tests -v
python3 -m agentlab eval
```

第一条运行命令应输出包含 `42` 的工具结果，状态为 `completed`。离线演示的 token 数显示为 0，因为没有真实模型请求。

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
  tools.py         工具注册、校验、12 个内置工具
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
