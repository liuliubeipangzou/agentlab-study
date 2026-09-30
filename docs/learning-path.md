# 七节课学习路线

每节课先运行例子，再打开对应源码。无需先理解全部模块。

## 1. 观察一次 Agent 循环

运行 `python3 -m examples.basic`。阅读 `types.py`，接着阅读 `Agent._loop()`。画出四条消息的角色顺序，找出 Agent 在何时决定继续调用模型、何时结束。

实验：把计算表达式改成 `1 / 0`，观察工具失败如何作为数据回到模型。Demo 会展示错误，不会真正推理修复；之后用 ScriptedProvider 或真实模型构建修复流程。

## 2. 添加一个工具

运行 `python3 -m examples.custom_tool`。阅读 `ToolRegistry.register()` 和 `execute()`。理解模型输入必须经过 Schema 校验，处理器才能被调用。

实验：传入 `celsius="25"` 或低于绝对零度的数值，查看校验结果；再实现一个长度单位转换工具。不要将模型输出交给 `eval()` 或 shell。

## 3. 理解权限与检查点

按 README 执行 `/write`，在批准前确认文件不存在。运行 `inspect SESSION`，找到 `pending`、`in_flight`、`execution` 与 `decisions`。

实验：先拒绝一次，再重新发起并批准；退出 Python 进程后继续审批。阅读 `test_approval_id_cannot_be_reused_for_second_write`，理解权限为何属于一次具体调用。

## 4. 区分三种记忆

短期对话是 messages；长期个人记忆是 memories；知识库是 documents/chunks/terms。运行 `/remember` 并批准，重启后用同一个会话 `/recall`；换一个会话再查询，验证隔离。

实验：缩小 `AgentConfig.max_context_chars`，观察旧轮次不再发送给模型，但数据库历史仍存在。裁剪不等于摘要，也不等于删除存储。

## 5. 理解 RAG

运行 `ingest knowledge`，用 `search '工具 安全'` 查看带 source/chunk 的结果。阅读 `storage.py` 的 tokenization、chunking 和 BM25 评分。

实验：向知识目录新增一个 `.md` 文件、重新导入，再修改和删除文件，各次检查 chunks 如何变化。思考为什么“有检索命中”不能保证“回答正确”，以及为什么需要输出引用。

## 6. 协作与规划

运行 `examples.multi_agent` 与 `examples.plan_and_execute`。先理解静态 DAG，再理解模型生成的 JSON 如何变成同一种 DAG。

实验：给一个节点加 `await asyncio.sleep(1)`，看独立分支是否并行；让一个节点抛异常，看依赖分支被跳过。尝试把 planner 模型替换成真实 Provider，用真实依赖输出生成后续任务输入。

工作流 runner 如果收到 Agent 的 `waiting_approval`，应把会话 ID 交还给用户处理；不要默默把等待状态当作成功。示例会检测非 completed 状态并中止该分支。

## 7. 接入真实模型与回归评测

读 `providers.py` 的序列化与响应解析，配置环境变量，先执行一个小的计算任务。对照离线模式，观察模型如何自主选择工具。

运行 `python3 -m agentlab eval`，再阅读 `evaluation.py`。增加一条 `EvalCase`，要求具体答案和工具顺序。随后测试错误输入、无检索命中、拒绝审批、模型返回格式错误等情形。

可继续扩展的项目：接入自己的只读业务 API；实现向量检索的独立存储适配器；为 Provider 添加 SSE 流式事件；做一个只调用公共 Python 接口的本地 UI。每项扩展都先定义协议，再写能证明行为的测试。
