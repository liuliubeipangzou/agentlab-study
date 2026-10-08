"""命令行仅负责输入输出；业务逻辑仍可在 Python 代码中独立使用。"""
import argparse
import asyncio
import json
import os
import shlex
import sys
import tempfile
from pathlib import Path

from .agent import Agent, AgentConfig, SessionError
from .evaluation import EvalCase, evaluate
from .providers import DemoProvider, ProviderError, provider_from_env
from .storage import SQLiteStore
from .tools import create_builtin_tools


def _budget_flags(args):
    return [(flag, getattr(args, name)) for flag, name in (
        ("--max-steps", "max_steps"), ("--max-tool-calls", "max_tool_calls"),
        ("--max-tokens", "max_tokens"), ("--timeout", "timeout")) if getattr(args, name, None) is not None]


def _config_from_args(args):
    overrides = {}
    for field, name in (("max_steps", "max_steps"), ("max_tool_calls", "max_tool_calls"),
                        ("max_total_tokens", "max_tokens"), ("run_timeout", "timeout")):
        value = getattr(args, name, None)
        if value is not None:
            overrides[field] = value
    return AgentConfig(**overrides)


def parser():
    root = argparse.ArgumentParser(prog="agentlab", description="Agent Lab · 可阅读、可运行的 Python Agent 学习框架")
    root.add_argument("--provider", choices=["openai", "demo"], default="openai",
                      help="openai 兼容 API（默认，需配置模型与 Key）；demo 为离线规则演示，仅供观察框架行为")
    root.add_argument("--data-dir", default=".agentlab", help="SQLite 状态目录")
    root.add_argument("--workspace", default="workspace", help="工具可读写的唯一文件根目录")
    root.add_argument("--json", action="store_true", help="以 JSON 输出结果")
    root.add_argument("--verbose", action="store_true", help="在 stderr 实时显示运行事件")
    root.add_argument("--stream", action="store_true",
                      help="流式打印模型回复，支持工具调用回合（需模型支持流式）")
    # 同一个开关也挂到子命令上，使 `run --stream ...` 这种自然写法同样有效。
    # 预算类参数留空时使用 AgentConfig 的默认值。
    root.add_argument("--max-steps", type=int, default=None, help="单次运行的最大模型调用步数")
    root.add_argument("--max-tool-calls", type=int, default=None, help="单次运行的最大工具调用次数")
    root.add_argument("--max-tokens", type=int, default=None, help="单次运行累计 token 上限")
    root.add_argument("--timeout", type=float, default=None, help="单次运行的时间预算（秒，不含等待审批）")
    sub = root.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="执行一次任务")
    run.add_argument("prompt")
    run.add_argument("--session", help="继续已有会话")
    run.add_argument("--stream", action="store_true", default=argparse.SUPPRESS,
                     help="流式打印模型回复")
    chat = sub.add_parser("chat", help="交互式多轮会话")
    chat.add_argument("--session")
    chat.add_argument("--stream", action="store_true", default=argparse.SUPPRESS, help="流式打印模型回复")
    approve = sub.add_parser("approve", help="明确批准一次检查点中的操作")
    approve.add_argument("session")
    selection = approve.add_mutually_exclusive_group(required=True)
    selection.add_argument("--all", action="store_true", help="批准当前检查点的全部写入")
    selection.add_argument("--call", action="append", default=[], help="批准指定调用 ID，可重复")
    for name, help_text in [("deny", "拒绝待审批操作并继续"), ("inspect", "查看会话检查点"),
                            ("trace", "查看事件轨迹"), ("recover", "结束中断的运行，不重放工具"),
                            ("delete", "删除会话及其事件与记忆")]:
        command = sub.add_parser(name, help=help_text)
        command.add_argument("session")
    sub.add_parser("sessions", help="列出持久化会话")
    sub.add_parser("tools", help="查看工具及参数 schema")
    ingest = sub.add_parser("ingest", help="导入本地 Markdown/TXT 知识库")
    ingest.add_argument("path")
    search = sub.add_parser("search", help="检索知识库")
    search.add_argument("query")
    search.add_argument("--limit", type=int, default=5)
    sub.add_parser("eval", help="运行离线工具回归评测")
    sub.add_parser("workflow", help="运行多 Agent DAG 演示")
    serve = sub.add_parser("serve", help="启动本地浏览器学习界面")
    serve.add_argument("--port", type=int, default=8765, help="本机服务端口，默认 8765")
    serve.add_argument("--open", action="store_true", help="启动后自动打开浏览器")
    return root


def emit(value):
    print(json.dumps(value, ensure_ascii=False, indent=2))


def _delta_printer(args):
    """构造流式增量打印回调；未开启 --stream 时返回 None。"""
    if not getattr(args, "stream", False) or getattr(args, "json", False):
        return None
    state = {"wrote": False}

    def on_delta(piece):
        if not state["wrote"]:
            # 增量第一次到达时才换行，避免与进度提示挤在同一行。
            print()
            state["wrote"] = True
        sys.stdout.write(piece)
        sys.stdout.flush()
        on_delta.text += piece

    on_delta.text = ""
    return on_delta


def _already_streamed(result, on_delta):
    """只省略真正已输出的完整回答；fallback、审批和失败信息必须显示。"""
    return (result.status == "completed" and bool(result.output)
            and bool(on_delta) and on_delta.text.endswith(result.output))


def _tool_settings_from_env():
    """从环境变量读取工具配置（当前用于检索后端凭据）。"""
    settings = {}
    backend = os.environ.get("AGENTLAB_SEARCH_BACKEND")
    api_key = os.environ.get("AGENTLAB_SEARCH_API_KEY")
    searx_url = os.environ.get("AGENTLAB_SEARX_URL")
    if backend or api_key or searx_url:
        settings["search"] = {"backend": backend, "api_key": api_key, "searx_url": searx_url}
    return settings


def show_result(result, as_json=False, args=None, streamed=False):
    if as_json:
        emit(result.to_dict())
        return
    if streamed and result.output.strip():
        # 正文已由流式增量打印，这里只补结尾换行，避免重复输出一遍。
        print()
    else:
        print("\n" + result.output)
    print("\n[{}] session={} · steps={} · tools={} · tokens={}".format(
        result.status, result.session_id, result.steps, result.tool_calls, result.usage.total_tokens))
    if result.status == "waiting_approval":
        print("待执行参数：")
        emit(result.pending)
        base = ["python3", "-m", "agentlab"]
        if args is not None:
            base += ["--provider", args.provider, "--data-dir", str(Path(args.data_dir).resolve()),
                     "--workspace", str(Path(args.workspace).resolve())]
            for flag, value in _budget_flags(args):
                base += [flag, str(value)]
        print("批准：" + shlex.join(base + ["approve", result.session_id, "--all"]))
        print("拒绝：" + shlex.join(base + ["deny", result.session_id]))


async def run_workflow(agent):
    from .workflows import Workflow, WorkflowStep
    async def calculate(results):
        result = await agent.run("/calc (12 + 8) * 3")
        if result.status != "completed":
            raise RuntimeError(result.output)
        return result.output
    async def research(results):
        result = await agent.run("/search Agent 工具 记忆")
        if result.status != "completed":
            raise RuntimeError(result.output)
        return result.output
    async def report(results):
        # 汇总节点可换成另一名 Agent；这里用确定性拼接，便于看清 DAG 数据流。
        return "计算 Agent：\n{}\n\n检索 Agent：\n{}".format(results["calculate"], results["research"])
    workflow = Workflow([WorkflowStep("calculate", calculate), WorkflowStep("research", research),
                         WorkflowStep("report", report, depends_on=["calculate", "research"])])
    result = await workflow.run()
    return {"outputs": result.outputs, "errors": result.errors, "statuses": result.statuses}


async def dispatch(args):
    if args.command == "tools":
        emit(create_builtin_tools().definitions())
        return 0
    if args.command == "eval":
        # 评测不读用户工作区，不调用在线模型，不需要凭据。
        with tempfile.TemporaryDirectory(prefix="agentlab-eval-") as temporary:
            stores = []
            def factory():
                store = SQLiteStore(":memory:")
                stores.append(store)
                return Agent(DemoProvider(), store=store, workspace=temporary)
            try:
                report = await evaluate(factory, [
                    EvalCase("算术", "/calc 6 * 7", ["42"], ["calculator"]),
                    EvalCase("括号优先级", "/calc (2 + 3) * 3", ["15"], ["calculator"]),
                    EvalCase("工具错误可观察", "/calc 1 / 0", ['"ok": false'], ["calculator"]),
                ])
                emit(report)
                return 0 if report["passed"] == report["total"] else 1
            finally:
                for store in stores:
                    store.close()
    data_dir = Path(args.data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    store = SQLiteStore(data_dir / "agentlab.sqlite3")
    try:
        if args.command == "sessions":
            emit(store.list_sessions())
            return 0
        if args.command in ("inspect", "trace"):
            state = store.load_session(args.session)
            if not state:
                raise SessionError("未找到会话：" + args.session)
            emit(state if args.command == "inspect" else store.events(args.session))
            return 0
        if args.command == "ingest":
            emit(store.ingest(Path(args.path)))
            return 0
        if args.command == "search":
            emit(store.search(args.query, limit=args.limit))
            return 0
        def trace(event):
            print("[{}] {}".format(event["type"], json.dumps(event["data"], ensure_ascii=False)), file=sys.stderr)
        provider = provider_from_env(args.provider)
        agent = Agent(provider, store=store, workspace=args.workspace,
                      config=_config_from_args(args), on_event=trace if args.verbose else None,
                      tool_settings=_tool_settings_from_env())
        on_delta = _delta_printer(args)
        if args.command == "run":
            result = await agent.run(args.prompt, args.session, on_delta=on_delta)
        elif args.command == "approve":
            state = store.load_session(args.session)
            if not state:
                raise SessionError("未找到会话")
            from .types import ToolCall
            approved = [x["id"] for x in state.get("pending", []) if agent.tools.requires_approval(ToolCall.from_dict(x))] if args.all else args.call
            result = await agent.resume(args.session, approved, on_delta=on_delta)
        elif args.command == "deny":
            result = await agent.resume(args.session, [], on_delta=on_delta)
        elif args.command == "recover":
            result = agent.recover(args.session)
        elif args.command == "delete":
            agent.delete(args.session)
            emit({"deleted": args.session})
            return 0
        elif args.command == "workflow":
            report = await run_workflow(agent)
            emit(report)
            return 1 if report["errors"] else 0
        elif args.command == "chat":
            session = args.session
            print("Agent Lab · {} 模式。/quit 退出；工具命令见 README。".format(args.provider))
            while True:
                try:
                    prompt = input("\n你> ").strip()
                except EOFError:
                    break
                if prompt in ("/quit", "/exit"):
                    break
                if not prompt:
                    continue
                try:
                    on_delta = _delta_printer(args)
                    if prompt in ("/approve", "/deny"):
                        state = store.load_session(session) if session else None
                        if not state:
                            raise SessionError("当前没有会话")
                        from .types import ToolCall
                        ids = [x["id"] for x in state.get("pending", []) if agent.tools.requires_approval(ToolCall.from_dict(x))] if prompt == "/approve" else []
                        result = await agent.resume(session, ids, on_delta=on_delta)
                    else:
                        result = await agent.run(prompt, session, on_delta=on_delta)
                    session = result.session_id
                    show_result(result, args.json, args, streamed=_already_streamed(result, on_delta))
                    if result.status == "waiting_approval":
                        print("在聊天中输入 /approve 批准以上参数，或 /deny 拒绝。")
                except (SessionError, ValueError) as exc:
                    print(str(exc), file=sys.stderr)
            return 0
        show_result(result, args.json, args, streamed=_already_streamed(result, on_delta))
        return 0 if result.status in ("completed", "waiting_approval") else 1
    finally:
        store.close()


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if args.command == "serve":
            # HTTP 服务自己管理后台任务，不嵌套在 CLI 的 asyncio 事件循环中。
            from .server import serve
            serve(data_dir=args.data_dir, workspace=args.workspace, host="127.0.0.1",
                  port=args.port, open_browser=args.open)
            return 0
        return asyncio.run(dispatch(args))
    except (ValueError, OSError, SessionError, ProviderError) as exc:
        print("错误：" + str(exc), file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\n已取消。", file=sys.stderr)
        return 130
