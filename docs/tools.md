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

### 为干活准备的文件与命令工具

| 工具 | 行为要点 |
| --- | --- |
| `read_file` | 不带 `offset`/`limit` 保持原样：整文件字符串，超过 256 KiB 报错并提示分页。带任一参数则按行分页，返回带行号的 `content`、`total_lines` 与 `next_offset`，文件上限 8 MiB；一页的大小会按 `max_output_chars` 自动收敛，避免被截断成难读的 JSON 预览 |
| `write_file` / `append_file` | 原子写入，**保留已有文件的权限位**（新文件为 0600）。`append_file` 让长内容可以分多次写，避开单次输出被截断 |
| `edit_file` | `old_string` 必须与原文完全一致且唯一，否则报错并给出出现次数与行号；`replace_all` 替换全部。返回 unified diff。文件上限 2 MiB |
| `glob` | 支持 `**`、`?`、`[...]`、`{a,b}`；不含 `/` 的模式匹配任意深度的文件名。跳过符号链接、`.git`、`node_modules`、`__pycache__`、虚拟环境与缓存目录，遍历上限 20000 项 |
| `grep` | 默认正则，`fixed=true` 为字面搜索，可带 `glob`、`ignore_case`、`context`。**在独立子进程里执行**（20 秒时限）：线程无法中断，模型给出的 `(a+)+$` 这类灾难性回溯正则会让线程空转到进程退出，子进程则可以被杀掉 |
| `run_shell` | 经 `pysandbox.run_command`：独立进程组、超时后整组终止、RSS 看门狗、净化环境（密钥与代理变量不传入，`GIT_TERMINAL_PROMPT=0`、`PAGER=cat` 避免挂起）。stdout/stderr 同时保留**开头与结尾**——报错和汇总行几乎总在末尾。非零退出码表现为 `ok: false`，退出码和两个输出流放在 `details` 里，并按预算共享，失败时不会只剩开头的 2000 字符 |
| `git` | `args` 是参数列表，不经 shell。只读子命令（status、diff、log、show、blame、ls-files、rev-parse 等，以及 `branch` 的列表形式、`tag -l`、`stash list` 等）免审批；其余需要审批。拒绝 `-c`/`-C`/`--git-dir` 等全局选项与 `--upload-pack`/`--exec` 等能替换执行程序的选项；`--output`、`--ext-diff`、`--textconv` 即使在只读子命令上也要审批 |
| `todo_write` | 整体替换会话待办，存入会话状态、发 `todos_updated` 事件，并在每次调用时附在系统提示里，上下文被摘要后也不会丢 |
| `ask_user` | 交互式工具（`Tool.interactive=True`），不由注册表执行：Agent 遇到它会进入 `waiting_input`，用户回答后作为工具结果交给模型 |

**受保护路径**：文件工具拒绝 `.git/` 下的一切与 `.env`、`.env.*`（`.env.example`、`.env.sample`、`.env.template`、`.env.dist` 除外）。通过 `tool_settings={"protected_paths": ["*.pem"]}` 追加 fnmatch 模式。这只约束文件工具；`run_shell`、`run_python` 以你的权限运行，不受限制。

`ToolContext` 现在带有 `state`（当前会话的可变状态）和 `emit`（发事件）两个由 Agent 填充的字段，供 `todo_write` 这类需要读写会话状态的内置工具使用；单独使用注册表时它们为 `None`。

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

## 联网工具与 SSRF 防护

`web_search`、`fetch_url`、`http_request` 让 Agent 能获取外部信息。模型可以构造任意
URL，因此 `netguard` 是这三者的共同安全出口：

- **地址校验**：拒绝回环、私网、链路本地、组播、保留网段、CGNAT（100.64/10）与云元数据
  （`169.254.169.254`、`fd00:ec2::254`）；IPv4-mapped IPv6（如 `::ffff:127.0.0.1`）按
  IPv4 规则判断。DNS 解析出的**每一个**地址都必须通过检查，任一落在内网即整体拒绝。
- **连接固定**：校验后的 IP 直接作为连接目标，Host 头与 TLS SNI 仍使用原主机名，
  从而消除"先检查后连接"之间的 DNS 重绑定窗口。
- **禁代理**：显式传入空的 `ProxyHandler`，避免流量经系统代理离开本机而使校验失效。
- **不跟随重定向**：3xx 只把 `Location` 作为 `redirect_to` 返回，由调用方再次经过完整校验。
- **限额**：响应体默认 2 MiB 上限、超时上限 120 秒，且拒绝 `Host`、`Content-Length`、
  `Connection`、`Transfer-Encoding` 等由底层决定的请求头。

`web_search` 默认使用免密钥的 DuckDuckGo HTML 端点；若页面改版会明确报错而不是静默返回空结果，
此时可配置 API 后端：

```bash
export AGENTLAB_SEARCH_BACKEND=brave      # 或 tavily / searxng
export AGENTLAB_SEARCH_API_KEY=...
export AGENTLAB_SEARX_URL=https://searx.example.com   # 仅 searxng 需要
```

检索结果的链接同样会经过地址校验，指向内网的条目会被丢弃。**检索结果与网页正文是不可信输入**，
工具返回值中带有相应提示，Agent 的系统提示也要求把它们当作数据而不是指令。

## run_python：有界但非沙箱的代码执行

`run_python` 把模型提供的代码放进独立子进程执行，用 `result` 变量回传数据，`stdout`/`stderr`
一并捕获。它做了这些约束：

| 约束 | 实现 |
| --- | --- |
| 超时 | `asyncio.wait_for` + 超时后终止**整个进程组**（`start_new_session` + `killpg`） |
| 内存 | 父进程实时读取 RSS（macOS 用 `libproc.proc_pidinfo`，Linux 用 `/proc/<pid>/statm`），超限即杀；子进程另设 `RLIMIT_AS` 自保 |
| CPU | `RLIMIT_CPU` 作为超时之外的第二道闸 |
| 磁盘 | `RLIMIT_FSIZE` 限制单文件写入 |
| 环境 | 只保留 `PATH`/`LANG`/`TZ` 等最小变量，`*_API_KEY` 与代理变量一律不传入 |
| 解释器 | `python -I -B` 隔离模式，不读取 `PYTHONPATH` 与用户 site-packages |
| 工作目录 | `run_python` 工具以工作区根目录为工作目录，写出的文件会保留；直接调用 `pysandbox.run_python` 且不传 `cwd` 时才使用执行后删除的临时目录 |

**它不是安全沙箱**：子进程仍以当前用户身份运行，可读写文件系统、发起网络连接，因此
`netguard` 的地址校验对它内部的 `socket` 调用无效。要执行真正不可信的代码，必须另加
容器或 seccomp 级别的隔离。

代码失败（异常、超时、内存超限）会**在工具层表现为 `ok: false`**，而不是嵌套在成功结果里，
以免模型把"工具调用成功"误当成"代码执行成功"而继续编造结论。
