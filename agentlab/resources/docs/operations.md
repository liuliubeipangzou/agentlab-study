# 配置、边界与排障

## AgentConfig

| 参数 | 默认值 | 语义 |
| --- | ---: | --- |
| max_steps | 40 | 每个用户任务的最大模型调用数，审批恢复不重置 |
| max_tool_calls | 100 | 每个任务的工具调用预算，包括用户拒绝的调用 |
| max_context_chars | 96000 | system 文本与序列化消息的字符上限，不含工具 schema；超出时先把较早的轮次摘要进系统提示 |
| max_total_tokens | 2000000 | API 已报告 usage 的累计上限 |
| run_timeout | 900 秒 | 同一任务累计推进时间，审批等待不计入 |
| tool_output_chars | 16000 | 序列化工具 value 的长度上限，最低 128 |
| format_retries | 2 | 模型返回非法工具参数、空响应或被截断时，带纠正提示重试的次数；纠正提示不写入历史 |
| summarize_history | True | 上下文超限时用模型把较早的轮次压缩成滚动摘要（完整历史仍保存在会话里）；摘要失败则退回到整轮裁剪 |
| wrap_up | True | 步数、token 或工具次数触顶时，再发一次不带工具的调用让模型交代进展，会话可回复“继续”接着做；该调用允许略微超出预算 |
| system_prompt | 中文工作助手提示 | 可替换的应用行为描述；每次调用会附上当前日期、历史摘要与待办清单 |
| approval_mode | ask | `ask` 逐次询问；`auto-workspace` 工作区内写入与默认清单里的命令自动放行；`trust` 全部自动。库里直接构造 Agent 默认 `ask`，命令行与浏览器默认 `auto-workspace` |

token 预算在响应后累计，达到限制便停止继续调用。它不是严格费用封顶：单次请求可能越过预算，重试也可能计费；未返回 usage 的兼容服务按 0 记录。若要更紧的控制，请同时设置 Provider 的 `max_output_tokens`、`max_retries` 和 Agent 步数。

```python
from agentlab import AgentConfig

config = AgentConfig(max_steps=5, max_tool_calls=8, run_timeout=60,
                     max_context_chars=16000, tool_output_chars=4000)
```

命令行可用 `--max-steps`、`--max-tool-calls`、`--max-tokens`、`--timeout` 覆盖对应预算；浏览器界面在“设置 → 运行预算”里配置，留空即使用默认值。

```python
# 需要旧版（演示级）的紧预算时：
config = AgentConfig(max_steps=8, max_tool_calls=16, max_total_tokens=20000, run_timeout=120)
```

## 审批策略

工具调用的风险等级由 `ToolRegistry.call_risk(call)` 给出：`read`、`write`、`exec`、`network_write`、`destructive`。除 `read` 外都需要审批。同一个工具的风险可以随参数变化——注册时给 `Tool` 传 `risk_of=callable`（并配套稳定的 `approval_policy` ID）：`git` 的 `status` 是 `read`，`commit` 是 `exec`，`push` 是 `network_write`，`reset --hard` 是 `destructive`；`http_request` 的 GET 是 `read`，POST 是 `network_write`；`run_shell` 对 `rm`、`sudo`、`git reset --hard` 等做启发式识别并按 `destructive` 处理（shell 字符串无法被可靠分析，没命中不代表安全）。`risk_of` 抛出异常或返回未知值时按 `destructive` 处理（fail closed）。

`ApprovalPolicy.decide(call, risk, session_rules)` 决定一次需要审批的调用是否自动放行：

1. `trust` 模式：全部放行。
2. `auto-workspace` 模式：`write` 放行；`run_shell` 的 `exec` 命令若满足下面三条则放行——不含 shell 元字符（`; & | \` $ < > ( )`、换行、反斜杠）、没有绝对路径/`~`/`..` 之类指向工作区之外的参数、且以 `DEFAULT_ALLOWED_COMMANDS` 中某一项为 token 前缀。
3. 其余按用户记住的规则（会话级在 `state["approval_rules"]`，全局在 `approval_rules` 表）匹配。规则种类：`command_prefix`（命令 token 前缀）、`git_sub`（git 子命令）、`host`（HTTP 主机）、`tool`（仅限 `write` 级工具）。**`destructive` 永远不匹配规则**；带 shell 元字符的命令也不会命中 `command_prefix`。
4. 都不满足则暂停等待用户。

执行顺序：同一批调用按顺序执行，排在前面的只读调用先执行；走到第一个必须由用户决定的调用才暂停（`waiting_approval`），并把此后所有待决定的调用一起交给用户。被策略自动放行的调用写入 `state["decisions"]` 并发出 `approval_auto` 事件，不会出现在待用户决定的列表里。`resume(session, approved_ids, feedback=..., remember="session"|"global")`：`feedback` 连同拒绝一起回灌给模型；`remember` 把本次批准的调用归纳成规则（见 `approvals.suggest_rule`：不为 `curl`、`find`、`sed`、裸 `python -c` 这类“一个词就能做任何事”的命令，也不为 `destructive` 提供规则）。

所有决定都写入 `approvals` 审计表（自动/批准/拒绝、来源、风险、参数摘要、拒绝理由），`agentlab approvals SESSION` 或会话接口的 `approvals` 字段可查看；删除会话时一并清除。工具集在等待期间发生变化时，已批准与已自动放行但尚未执行的调用一律取消，不沿用旧批准。

**自动放行不等于安全。** `auto-workspace` 放行测试/构建命令，意味着 Agent 刚写下的代码可以不经确认地运行；工具对 `.git/`、`.env*` 的保护只约束文件工具，不约束命令。需要更强的隔离时使用 `ask` 模式，并在容器或受限账户中运行。

## 支持范围

- 文件工具要求支持 `dir_fd` 和 `O_NOFOLLOW` 的 POSIX 环境（macOS/Linux）。文件限制 256 KiB，只接受工作区内相对路径，拒绝符号链接；Windows 尚未验证。
- 工具处理器是可信的本地 Python 代码；框架不能隔离恶意插件或阻止其直接访问系统。需要运行不可信代码时，应另加进程/容器沙箱。
- 同步工具和 HTTP 请求使用线程。协程超时只停止等待，不能强杀线程；写入可能已经生效。取消之后先确认副作用，再决定是否重复发起。
- SQLite 支持本地多个进程协作，非分布式数据库。消息历史、事件和文档可持续增长，需要自行备份与保留策略。所有数据均未加密。
- 知识库是有界的本地词法 BM25 索引，不提供语义 embedding。只接收 UTF-8 `.md`/`.txt`，不自动解析 PDF、网页或 Office 文件。
- Agent 的消息和审批检查点可恢复；DAG 当前只在进程内调度，不提供整个工作流自动续跑。重试不保证副作用幂等。
- Planner 仅做计划生成与结构校验，不等价于任务可完成性证明；其模型调用拥有单独的 timeout/有限修复次数，不计入 Agent 的 usage。评测器也不会自动关闭调用方创建的 Store，factory 的资源由调用方负责释放。
- 真实 API 支持范围详见 providers 文档。测试模拟 HTTP，验证协议和错误处理；在线服务连通性仍需使用自己的 Key 验证。

## 排障

| 现象 | 处理 |
| --- | --- |
| 输出 Demo 学习指南 | 离线模式仅识别斜线命令；自然语言任务使用 `--provider openai` |
| 缺少 AGENTLAB_MODEL/API_KEY | 用 shell export，`.env` 不会自动加载 |
| 审批提示执行环境不一致 | 沿用创建检查点时的 provider、workspace 和工具注册表 |
| 会话正在运行 | 等待原进程完成；崩溃后待租约到期再 recover |
| 检测到中断运行 | 运行 recover，核对可能发生的副作用后重新发起任务 |
| limited | inspect/trace 查看原因，再缩短任务或合理调整配置 |
| search 无结果 | 先 ingest；使用文档中实际出现的中文或英文关键词 |
| tool error | 查看 tool 消息中的结构化错误与参数 schema |
| 模型返回无效响应 | 兼容服务必须支持工具调用协议；trace 可定位模型阶段 |

命令行完成/等待审批退出码为 `0`，运行失败/超限为 `1`，输入或配置错误为 `2`，用户中断为 `130`。服务端、Web UI 或其他程序集成应优先检查 `AgentResult.status`，而不是只看是否有文本。
