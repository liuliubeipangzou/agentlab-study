# 工具调用与权限边界

模型只能提出工具调用。`ToolRegistry.execute()` 根据工具白名单、JSON 参数规则、
授权状态和超时设置决定是否执行。应用层展示确认界面后，才应传入
`approved=True`。`write_file` 和 `remember` 都需要确认；工具执行层仍然再次检查，
所以绕过 Agent 直接调用注册表也不会跳过这一检查。

## 扩展一个工具

```python
from agentlab.tools import Tool, ToolContext, ToolRegistry

async def temperature(arguments: dict, context: ToolContext):
    return {"fahrenheit": arguments["celsius"] * 9 / 5 + 32}

registry = ToolRegistry()
registry.register(Tool(
    name="temperature",
    description="把摄氏度换算成华氏度。",
    parameters={
        "type": "object",
        "properties": {"celsius": {"type": "number"}},
        "required": ["celsius"],
        "additionalProperties": False,
    },
    handler=temperature,
))
```

处理器接收 `(arguments, context)`，返回可序列化为 JSON 的值。结果格式为
`{"ok": true, "value": ...}`，失败格式为 `{"ok": false, "error": ...}`。
`ToolContext.max_output_chars` 默认 8000，最小 128。超过限制的值会转成包含
`truncated`、`preview`、`original_chars` 的对象；限制计算包括值的 JSON 转义，
不包括最外层 `ok` / `value` 包装。截断结果不是原始结构，调用方必须检查。

## 参数和结构化输出校验

`validate_schema(value, schema)` 成功返回 `None`，失败抛出 `ValueError`。
支持 `type`、`properties`、`required`、`additionalProperties`、`items`、`enum`、
`minimum` / `maximum`、`minLength` / `maxLength`、`minItems` / `maxItems`。
`description`、`title`、`default` 作为注解接受，其中 `default` 不会自动填值。
支持 object、array、string、number、integer、boolean、null 单一类型；不支持
`$ref`、`oneOf`、`pattern`、联合类型等完整 JSON Schema 特性，未知关键字会报错。
最大 JSON 嵌套深度是 20。布尔值不能伪装成整数；非有限浮点数和非 JSON 对象被拒绝。

## 文件操作

文件路径必须相对于工作区，不接受绝对路径、`..`、`.` 和反斜杠。
POSIX 文件操作使用 `dir_fd`、`O_NOFOLLOW` 和逐层目录描述符；工作区内的符号链接
无论出现在父目录还是目标文件，都被拒绝。读取仅接受普通 UTF-8 文件，拒绝 FIFO，
文件和写入内容最多 256 KiB。写入先使用同目录独占临时文件，再原子替换目标，
不会跟随被并发替换成符号链接的目标。缺失的父目录会在确认后创建。

工作区是可信的本机目录；初始化上下文时应由应用选定，不能让模型指定。操作系统
自身的祖先路径（例如 macOS 的 `/var`）会解析到实际工作区。这些检查不防御其他
进程恶意重命名已打开的目录、改变挂载点或预先设置硬链接，也不是操作系统沙箱。
缺少 POSIX `O_NOFOLLOW` / `dir_fd` 支持的系统会明确拒绝文件工具，其他工具仍可使用。

## 超时与取消

协作式 async 处理器会由 `asyncio.wait_for` 取消。处理器应在耗时操作中使用真正
可等待的异步 API，不应捕获并吞掉取消信号。普通回调（包括短文件操作）在工作线程
中执行；超时停止等待，但 Python 无法杀掉这个线程，副作用可能稍后完成。因此，
对写入结果未知的超时不要盲目重试，应先读取状态，或为自定义外部工具实现幂等键。
文件替换的原子性防止半个文件，并不等同于线程超时后的回滚。

内置 SQLite 记忆调用在当前线程执行以保持连接的线程约束；它适合短的本地查询。
若接入耗时的远程记忆服务，应提供 async 方法。任意自定义插件仍拥有 Python
进程的权限，注册来源不可信的回调需要另行使用容器或独立受限进程。

计算器通过 AST 白名单解释数值表达式，没有 `eval`、函数调用、属性访问或变量。
表达式最多 256 字符和 64 个 AST 节点，指数绝对值最多 100，数值幅度不超过
`1e100`；这些边界用于防止简单的计算资源耗尽。
