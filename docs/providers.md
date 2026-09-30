# 模型适配器

Provider 只负责“消息和工具定义 → 模型响应”。它返回工具调用意图，由 Agent
检查权限、执行工具，并把结果加入消息后再次调用 Provider。模型适配器本身不会
执行工具，也不会把远端 API 的返回文本当作代码运行。

```python
from agentlab.types import Message
from agentlab.providers import DemoProvider

provider = DemoProvider()
response = await provider.complete([Message("user", "你好")], tools=[])
print(response.content)
```

上面的 `await` 应放在异步函数内；CLI 会处理事件循环。

## 三种实现

| 实现 | 用途 | 是否请求外部服务 |
| --- | --- | --- |
| `DemoProvider` | 确定性规则模拟，学习工具执行循环 | 否 |
| `ScriptedProvider` | 用预设 `ModelResponse` 测试多步骤控制流 | 否 |
| `OpenAICompatibleProvider` | 使用 Chat Completions 格式的真实模型 | 是 |

Demo 不是 LLM，不具备开放式推理能力。它支持 `/calc EXPR`、`/search QUERY`、
`/read PATH`、`/write PATH CONTENT`、`/remember KEY VALUE`、`/recall QUERY`。
路径或参数包含空格时使用引号，例如 `/write "my notes.txt" "今天学习了工具调用"`。
每个新用户消息开启一轮，旧轮次的工具结果不会触发当前轮的总结。普通文本会得到
学习命令指南。工具必须先在 Agent 中注册；写入仍受 Agent 的权限策略约束。

`ScriptedProvider(responses)` 依次返回预设响应，并将每次调用的消息和工具快照
保存在 `calls`，格式为 `{"messages": [...], "tools": [...]}`。响应耗尽时会抛出
`ProviderError`，不会悄悄返回空答案。

## 配置真实模型

```bash
export AGENTLAB_MODEL='你的模型名称'
export AGENTLAB_API_KEY='你的 API Key'
export AGENTLAB_BASE_URL='https://api.openai.com/v1'
```

不要把密钥写入代码或提交到版本库。`provider_from_env("openai")` 读取上面的变量；
`provider_from_env()` 默认返回完全离线的 Demo。兼容服务应设置其 API 根地址，
例如 `https://example.com/v1`，适配器会追加 `/chat/completions`。
开发中的本地服务允许使用 `http://localhost:端口/v1` 或回环 IP 地址；其余地址需要
HTTPS。Base URL 不允许内嵌用户名、密码、查询参数或片段，HTTP 重定向也会被阻止。

### DeepSeek 配置

```bash
export AGENTLAB_API_KEY='你的 DeepSeek API Key'
export AGENTLAB_MODEL='deepseek-flash'
export AGENTLAB_BASE_URL='https://api.deepseek.com'
python3 -m agentlab --provider openai run '请用工具计算 123 * 456'
```

DeepSeek 官方的 `deepseek-flash` 支持工具调用且默认开启思考模式。当前框架没有保存和回传
`reasoning_content`，所以对主机名精确为 `api.deepseek.com` 的请求自动添加
`"thinking": {"type": "disabled"}`，使用非思考模式完成工具循环。无需额外配置。
其他服务地址不添加此字段；第三方 DeepSeek 代理需要自行适配其参数。此适配不代表完整支持
DeepSeek 思考模式。配置依据：[官方模型列表](https://api-docs.deepseek.com/quick_start/pricing)、
[思考模式及工具调用说明](https://api-docs.deepseek.com/guides/thinking_mode)。

```python
from agentlab.providers import OpenAICompatibleProvider

provider = OpenAICompatibleProvider(
    model="your-model",
    api_key="从环境读取的密钥",
    timeout=30,
    max_retries=2,
    max_output_tokens=2048,
)
```

适配器使用非流式 Chat Completions、函数工具和 `max_tokens` 参数。服务需支持这些
协议字段；Responses API、图像/音频输入、流式输出以及仅接受
`max_completion_tokens` 的模型不在此适配器的支持范围。`Usage` 记录 API 返回的
输入/输出 token 数，没有 usage 的兼容服务按零记录，不能据此估算真实账单。

## 错误、超时与重试

连接错误、HTTP 429 和 5xx 最多重试 `max_retries` 次，默认 2 次，因此最多发出
3 次请求。每次退避从 0.25 秒开始翻倍，并参考数值形式的 `Retry-After`，等待上限
为 5 秒；401、403、其他 4xx、重定向和无效响应不重试。API 异常只显示经过处理的
类别和状态码，不透出密钥、远端响应正文或异常地址。

请求在 `asyncio.to_thread` 中运行，避免阻塞 Agent 的事件循环。`timeout` 是底层
网络 I/O 超时，并非整轮任务的总时限：协程取消后，已启动的线程仍可能等待到
socket 超时，服务器也可能已经处理了请求。网络失败后的重试也可能重复计费；
需要严格控制请求次数时使用 `max_retries=0`。生产级客户端还应按服务端协议加入
幂等请求键、限流和更严格的全局并发管理。

响应限制为 4 MiB。解析会验证消息结构、函数名、调用 ID、重复调用 ID、参数必须
为 JSON 对象、有限数值和 usage；JSON 重复键、NaN、Infinity 与溢出数值也会被
拒绝。达到输出上限的响应不会执行其中的工具调用，避免执行不完整决策。

## 扩展适配器

实现下面的方法即可接入其他模型，无须继承具体基类：

```python
async def complete(self, messages: list, tools: list) -> ModelResponse:
    ...
```

`messages` 中工具结果通过 `tool_call_id` 与上一条 assistant 的 `ToolCall.id`
关联。返回多个 `ToolCall` 时，ID 必须唯一，参数必须为字典。工具定义可以使用
OpenAI 的 `{"type": "function", "function": {...}}` 格式，或扁平的
`{"name": "...", "description": "...", "parameters": {...}}` 格式。
适配器的测试均模拟 HTTP，不读取真实 Key、不调用付费模型。

## 流式输出

`OpenAICompatibleProvider.stream()` 读取 SSE（`stream: true`），逐段回调正文增量，最后返回
完整的 `ModelResponse`。它有三个刻意的取舍：

- **只在没有工具可调用的回合使用**。带工具时需要在流里重组 `tool_calls` 的分片参数
  （`index` 对齐、arguments 字符串拼接），收益低而正确性风险高；调用方应回退到 `complete()`。
  `Agent._complete()` 就是这样判断的：只有"调用方提供了增量回调"且"本轮没有工具定义"
  时才走流式。
- **阻塞读取放进线程**。SSE 是阻塞 I/O，直接在事件循环里迭代会卡住整个 Agent；因此先由
  `_stream_collect()` 在线程中读完并收集增量，回到事件循环后再触发回调。
- **复用非流式的安全策略**：禁止重定向、4 MiB 上限、严格 JSON（拒绝重复键与 NaN）。
  `finish_reason` 为 `length`（被截断）或 `content_filter` 时直接报错，不会把半句话当完整回答。

命令行用 `--stream` 开启：

```bash
python3 -m agentlab run --stream '用三句话解释什么是 BM25'
python3 -m agentlab chat --session work --stream
```

未实现 `stream()` 的 Provider（如 `DemoProvider`、`ScriptedProvider`）会自动回退到 `complete()`，
不需要任何改动。
