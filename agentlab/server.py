"""Local-only web workbench; the UI drives the same Agent and SQLite checkpoints.

HTTP threads never run Agent coroutines themselves. A dedicated asyncio loop owns
bounded background jobs, and each job reports its actual events and final result.
"""

import asyncio
import copy
import hmac
import json
import os
import re
import secrets
import socket
import tempfile
import threading
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import urllib.parse
from urllib.parse import urlsplit

from .agent import Agent, AgentConfig, SessionError
from .approvals import DEFAULT_ALLOWED_COMMANDS, MODES, build_preview, suggest_rule
from .evaluation import EvalCase, evaluate
from .providers import DemoProvider, OpenAICompatibleProvider, ProviderError
from .storage import SQLiteStore
from .tools import create_builtin_tools
from .workspace_files import import_files as upload_workspace_files
from .workspace_files import list_workspace as list_workspace_files
from .workspace_files import read_file as read_workspace_file
from .types import Message, ToolCall


PACKAGE = Path(__file__).resolve().parent
ROOT = PACKAGE.parent
STATIC = PACKAGE / "web"
RESOURCES = PACKAGE / "resources"
DOC_NAMES = ("architecture", "learning-path", "tools", "providers", "memory-workflows", "operations", "validation", "web-ui")
IDENTIFIER = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
MAX_BODY = 2 * 1024 * 1024
# 单个任务保留的实时增量上限（字符）；超出即截断，避免内存被撑爆。
DELTA_LIMIT = 200000


def _query_int(query, name, default):
    """从查询串取一个非负整数参数。

    缺失时返回默认值；**存在但非法时明确报错**，避免静默退化成 0 让客户端误以为
    需要重新拉取全量事件。
    """
    values = urllib.parse.parse_qs(query).get(name)
    if not values:
        return default
    try:
        value = int(values[0])
    except (TypeError, ValueError):
        raise APIError(400, "%s 必须是非负整数" % name) from None
    if value < 0:
        raise APIError(400, "%s 必须是非负整数" % name)
    return value


class APIError(Exception):
    def __init__(self, status, message):
        self.status, self.message = status, message


# 可在设置里调整的运行预算：名称 -> (下限, 上限)。未设置的项使用 AgentConfig 默认值。
BUDGET_LIMITS = {"max_steps": (1, 500), "max_tool_calls": (1, 2000),
                 "max_total_tokens": (1000, 50000000), "run_timeout": (10, 86400),
                 "max_context_chars": (4000, 2000000)}


class App:
    MAX_ACTIVE_JOBS = 4
    MAX_JOBS = 100

    def __init__(self, data_dir=".agentlab", workspace="workspace", host="127.0.0.1"):
        if host not in ("127.0.0.1", "localhost", "::1"):
            raise ValueError("Web 工作台仅允许绑定本机回环地址")
        self.host = host
        self.data_dir = Path(data_dir).expanduser().resolve()
        self.workspace = Path(workspace).expanduser().resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.store = SQLiteStore(self.data_dir / "agentlab.sqlite3")
        self.tools = create_builtin_tools()
        self.csrf_token = secrets.token_urlsafe(32)
        self._lock = threading.RLock()
        self._import_lock = threading.Lock()
        self._jobs = {}
        self._servers = []
        self._closed = False
        self.config_warning = ""
        # 默认使用真实模型。demo 是离线规则演示，必须显式开启：
        # 同时设置 AGENTLAB_ALLOW_DEMO=1 与 AGENTLAB_PROVIDER=demo。
        self.allow_demo = os.environ.get("AGENTLAB_ALLOW_DEMO", "").strip().lower() in ("1", "true", "yes", "on")
        requested = os.environ.get("AGENTLAB_PROVIDER", "").strip().lower()
        default_provider = "openai"
        if requested == "demo" and self.allow_demo:
            default_provider = "demo"
        requested_mode = os.environ.get("AGENTLAB_APPROVAL_MODE", "").strip().lower()
        self._config = {"provider": default_provider,
                        "model": os.environ.get("AGENTLAB_MODEL") or "deepseek-flash",
                        "base_url": os.environ.get("AGENTLAB_BASE_URL") or "https://api.deepseek.com",
                        "streaming": True,
                        # 浏览器界面默认让工作区内的写入自动放行，这样长任务不必每步点击；
                        # 执行命令等仍然询问。可改为 ask（逐次询问）或 trust（全部自动）。
                        "approval_mode": requested_mode if requested_mode in MODES else "auto-workspace"}
        self._api_key = os.environ.get("AGENTLAB_API_KEY", "").strip()
        # 检索后端配置（web_search 使用）；随 settings 下发到工具上下文。
        self._search_settings = {
            "backend": os.environ.get("AGENTLAB_SEARCH_BACKEND", "").strip() or "duckduckgo",
            "api_key": os.environ.get("AGENTLAB_SEARCH_API_KEY", "").strip(),
            "searx_url": os.environ.get("AGENTLAB_SEARX_URL", "").strip(),
        }
        self._secrets = {value for value in (self._api_key, self._search_settings["api_key"]) if value}
        self._budgets = {}
        try:
            self._search_settings = self._validate_search(self._search_settings)
        except APIError:
            self._search_settings = {"backend": "duckduckgo", "api_key": "", "searx_url": ""}
            self.config_warning = "环境中的检索配置无效，当前使用 DuckDuckGo；请在设置中重新保存。"
        settings = self.data_dir / "web-settings.json"
        if settings.is_file():
            try:
                saved = json.loads(settings.read_text(encoding="utf-8"))
                candidate = {name: saved.get(name, default) for name, default in self._config.items()}
                self._validate_config(candidate, self._api_key)
                search = dict(self._search_settings,
                              backend=saved.get("search_backend", self._search_settings["backend"]),
                              searx_url=saved.get("searx_url", self._search_settings["searx_url"]))
                self._search_settings = self._validate_search(search)
                self._budgets = self._validate_budgets(saved.get("budgets", {}))
                self._config = candidate
            except (APIError, ValueError, TypeError, AttributeError, OSError, ProviderError):
                mode = "演示模式" if default_provider == "demo" else "真实模型默认配置"
                self.config_warning = "已保存的配置无效，当前使用%s；请在设置中重新保存。" % mode
        self._loop = asyncio.new_event_loop()
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._run_loop, name="agentlab-asyncio", daemon=True)
        self._thread.start()
        self._ready.wait(5)

    def _run_loop(self):
        asyncio.set_event_loop(self._loop)
        self._ready.set()
        self._loop.run_forever()
        self._loop.close()

    def public_config(self):
        with self._lock:
            return dict(self._config, has_api_key=bool(self._api_key),
                        search_backend=self._search_settings.get("backend") or "",
                        has_search_key=bool(self._search_settings.get("api_key")),
                        searx_url=self._search_settings.get("searx_url") or "",
                        budgets=dict(self._budgets), budget_defaults=self._budget_defaults(),
                        approval_modes=list(MODES), default_commands=list(DEFAULT_ALLOWED_COMMANDS),
                        allow_demo=self.allow_demo)

    @staticmethod
    def _budget_defaults():
        defaults = AgentConfig()
        return {name: getattr(defaults, name) for name in BUDGET_LIMITS}

    @staticmethod
    def _validate_budgets(value):
        """只接受已知预算项的有限数值；空对象表示全部恢复默认。"""
        if not isinstance(value, dict):
            raise APIError(400, "budgets 必须为对象")
        clean = {}
        for name, raw in value.items():
            if name not in BUDGET_LIMITS:
                raise APIError(400, "未知的预算项：" + str(name))
            low, high = BUDGET_LIMITS[name]
            if isinstance(raw, bool) or not isinstance(raw, (int, float)) or not low <= raw <= high:
                raise APIError(400, "%s 必须在 %s 到 %s 之间" % (name, low, high))
            if name != "run_timeout" and raw != int(raw):
                raise APIError(400, name + " 必须为整数")
            clean[name] = float(raw) if name == "run_timeout" else int(raw)
        return clean

    def _validate_config(self, config, key):
        if config.get("provider") not in ("demo", "openai"):
            raise APIError(400, "provider 只能为 demo 或 openai")
        if config["provider"] == "demo" and not self.allow_demo:
            raise APIError(400, "演示模式已停用；请选择真实模型并填写 API Key")
        if type(config.get("streaming")) is not bool:
            raise APIError(400, "streaming 必须为布尔值")
        if config.get("approval_mode") not in MODES:
            raise APIError(400, "approval_mode 必须是 %s 之一" % "、".join(MODES))
        if any(not isinstance(config.get(name), str) for name in ("model", "base_url")):
            raise APIError(400, "model 和 base_url 必须为字符串")
        if not isinstance(key, str):
            raise APIError(400, "API Key 必须为字符串")
        # Constructor validates endpoints and credentials without making any request.
        try:
            OpenAICompatibleProvider(config["model"], key or "validation-only", config["base_url"])
        except ProviderError as exc:
            # 配置类错误必须以 400 返回；否则会冒到 HTTP 层被当成 500 内部错误。
            raise APIError(400, str(exc)) from None

    def _validate_search(self, search):
        backend, url, key = search.get("backend"), search.get("searx_url"), search.get("api_key")
        if not isinstance(backend, str) or not isinstance(url, str) or not isinstance(key, str):
            raise APIError(400, "检索配置必须为文本")
        backend = {"": "duckduckgo", "ddg": "duckduckgo", "searx": "searxng"}.get(
            backend.strip().lower(), backend.strip().lower())
        if backend not in ("duckduckgo", "brave", "tavily", "searxng"):
            raise APIError(400, "请选择 DuckDuckGo、Brave、Tavily 或 SearXNG 检索后端")
        url, key = url.strip(), key.strip()
        if len(key) > 4000 or any(ord(character) < 32 for character in key):
            raise APIError(400, "检索 API Key 格式无效")
        if url:
            try:
                parsed = urlsplit(url)
                port = parsed.port
                if (len(url) > 2000 or parsed.scheme not in ("http", "https") or not parsed.hostname
                        or parsed.username is not None or parsed.password is not None
                        or parsed.query or parsed.fragment or any(character.isspace() for character in url)
                        or (port is not None and not 1 <= port <= 65535)):
                    raise ValueError()
            except ValueError:
                raise APIError(400, "SearXNG 地址需要 HTTP(S) URL，不能包含认证、查询参数或片段") from None
        if backend == "searxng" and not url:
            raise APIError(400, "SearXNG 后端需要实例地址")
        return {"backend": backend, "api_key": key, "searx_url": url}

    def configure(self, payload):
        with self._lock:
            if any(job["status"] == "running" for job in self._jobs.values()):
                raise APIError(409, "请等待当前任务完成或取消后再修改配置")
            candidate = {name: payload.get(name, value) for name, value in self._config.items()}
            if candidate.get("provider") == "demo" and not self.allow_demo:
                raise APIError(400, "演示模式已停用；请选择真实模型并填写 API Key")
            supplied = payload.get("api_key", "")
            if not isinstance(supplied, str):
                raise APIError(400, "API Key 必须为字符串")
            for flag in ("clear_api_key", "clear_search_api_key"):
                if flag in payload and type(payload[flag]) is not bool:
                    raise APIError(400, flag + " 必须为布尔值")
            if payload.get("clear_api_key") and supplied.strip():
                raise APIError(400, "不能同时设置并清除 API Key")
            key = "" if payload.get("clear_api_key") else supplied.strip() or self._api_key
            self._validate_config(candidate, key)
            # Empty credential fields retain the current key; clearing is explicit.
            search = dict(self._search_settings)
            for field, limit in (("search_backend", 32), ("search_api_key", 4000), ("searx_url", 2000)):
                if field in payload:
                    value = payload.get(field)
                    if value is None:
                        continue
                    if not isinstance(value, str) or len(value) > limit:
                        raise APIError(400, "检索配置字段无效：" + field)
                    target = {"search_backend": "backend", "search_api_key": "api_key",
                              "searx_url": "searx_url"}[field]
                    if field != "search_api_key" or value.strip():
                        search[target] = value.strip()
            if payload.get("clear_search_api_key"):
                if isinstance(payload.get("search_api_key"), str) and payload["search_api_key"].strip():
                    raise APIError(400, "不能同时设置并清除检索 API Key")
                search["api_key"] = ""
            search = self._validate_search(search)
            budgets = self._validate_budgets(payload["budgets"]) if "budgets" in payload else dict(self._budgets)
            temporary = self.data_dir / (".web-settings-" + uuid.uuid4().hex + ".tmp")
            try:
                saved = dict(candidate, search_backend=search["backend"], searx_url=search["searx_url"],
                             budgets=budgets)
                temporary.write_text(json.dumps(saved, ensure_ascii=False, indent=2), encoding="utf-8")
                temporary.replace(self.data_dir / "web-settings.json")
            finally:
                temporary.unlink(missing_ok=True)
            self._config, self._api_key = candidate, key
            self._search_settings = search
            self._budgets = budgets
            self.config_warning = ""
            if key:
                self._secrets.add(key)
            if search.get("api_key"):
                self._secrets.add(search["api_key"])
            return {"config": self.public_config()}

    def provider_ready(self):
        """当前配置是否足以创建 provider（真实模型必须有 Key）。"""
        with self._lock:
            if self._config["provider"] == "demo":
                return self.allow_demo
            return bool(self._api_key)

    def _provider(self):
        with self._lock:
            if self._config["provider"] == "demo":
                if not self.allow_demo:
                    raise APIError(400, "演示模式已停用；请在设置中选择模型并填写 API Key")
                return DemoProvider()
            if not self._api_key:
                raise APIError(400, "尚未配置 API Key：请在设置中填写并保存，"
                                    "或设置环境变量 AGENTLAB_API_KEY 后重启服务")
            return OpenAICompatibleProvider(self._config["model"], self._api_key, self._config["base_url"])

    def _redact(self, value):
        if isinstance(value, str):
            for secret in self._secrets:
                value = value.replace(secret, "[REDACTED]")
            return value
        if isinstance(value, dict):
            return {self._redact(key): self._redact(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self._redact(item) for item in value]
        return value

    def _agent(self, job):
        def event(event):
            with self._lock:
                if len(job["events"]) < 5000:
                    job["events"].append(copy.deepcopy(event))
                if event.get("type") == "run_started":
                    for row in self.store.list_sessions():
                        state = self.store.load_session(row["session_id"])
                        if state and state.get("run_id") == event.get("run_id"):
                            job["_session_ids"].add(row["session_id"])
                            break
        with self._lock:
            budgets = dict(self._budgets)
            approval_mode = self._config["approval_mode"]
        return Agent(self._provider(), tools=self.tools, store=self.store,
                     workspace=self.workspace, on_event=event,
                     config=AgentConfig(approval_mode=approval_mode, **budgets),
                     tool_settings={"search": dict(self._search_settings)})

    def _delta_handler(self, job):
        """把模型增量累积到任务上，供界面边生成边显示。

        增量不写入 events 数组：它是高频数据，塞进去会挤掉真正有价值的执行事件
        （上限 5000 条），也会让每次增量轮询都重复传输。
        """
        if not self._config.get("streaming", True):
            return None

        def on_delta(piece):
            if not isinstance(piece, str) or not piece:
                return
            with self._lock:
                buffer = job.get("_delta") or ""
                if len(buffer) >= DELTA_LIMIT:
                    return
                # 严格截断：单个超大片段也不能让缓冲越界。
                job["_delta"] = (buffer + piece)[:DELTA_LIMIT]

        return on_delta

    def submit(self, operation, session_id=None, require_provider=False):
        with self._lock:
            if self._closed:
                raise APIError(503, "工作台正在关闭")
            if require_provider and not self.provider_ready():
                # 在提交阶段就拒绝，避免用户拿到一个必然失败的后台任务。
                if self._config["provider"] == "demo":
                    raise APIError(400, "演示模式已停用；请在设置中选择模型并填写 API Key")
                raise APIError(400, "尚未配置 API Key：请在设置中填写并保存，"
                                    "或设置环境变量 AGENTLAB_API_KEY 后重启服务")
            running = [job for job in self._jobs.values() if job["status"] == "running"]
            if len(running) >= self.MAX_ACTIVE_JOBS:
                raise APIError(429, "后台任务已满，请等待任务结束")
            if session_id and any(job.get("session_id") == session_id for job in running):
                raise APIError(409, "此会话已有运行中的任务")
            while len(self._jobs) >= self.MAX_JOBS:
                oldest = next((key for key, job in self._jobs.items() if job["status"] != "running"), None)
                if oldest is None:
                    raise APIError(429, "后台任务已满")
                del self._jobs[oldest]
            job_id = uuid.uuid4().hex
            job = {"job_id": job_id, "status": "running", "result": None, "error": None,
                   "events": [], "session_id": session_id, "_task": None, "_cancel": False,
                   "_session_ids": set(), "_delta": ""}
            self._jobs[job_id] = job
            asyncio.run_coroutine_threadsafe(self._run_job(job, operation), self._loop)
            return job_id

    async def _run_job(self, job, operation):
        try:
            with self._lock:
                job["_task"] = asyncio.current_task()
                cancelled = job["_cancel"]
            if cancelled:
                raise asyncio.CancelledError()
            result = await operation(job)
            if hasattr(result, "to_dict"):
                result = result.to_dict()
            with self._lock:
                job.update(status="completed", result=result)
        except asyncio.CancelledError:
            with self._lock:
                job.update(status="cancelled", error="任务已取消")
        except (APIError, SessionError, ProviderError, ValueError) as exc:
            with self._lock:
                message = exc.message if isinstance(exc, APIError) else str(exc)
                job.update(status="failed", error=self._redact(message))
        except Exception as exc:
            with self._lock:
                job.update(status="failed", error="后台任务失败：" + type(exc).__name__)

    def job(self, job_id):
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise APIError(404, "未找到后台任务")
            payload = {key: value for key, value in job.items() if not key.startswith("_")}
            # 增量正文单独放行：内部字段以下划线开头，但它正是界面需要的实时内容。
            payload["delta"] = job.get("_delta") or ""
            return copy.deepcopy(payload)

    def job_events(self, job_id, since=0):
        """增量获取任务事件，避免轮询时反复传输完整的 5000 条事件列表。

        `since` 是客户端已见的事件数。它等于总数时返回空列表（正常的"无新事件"），
        超出总数时同样返回空——**不能重发全量**，否则前端会反复重渲染同一批事件。
        """
        if type(since) is not int or since < 0:
            raise APIError(400, "since 必须是非负整数")
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise APIError(404, "未找到后台任务")
            events = job["events"]
            start = min(since, len(events))
            return {"job_id": job_id, "status": job["status"],
                    "total": len(events), "since": start,
                    "delta": job.get("_delta") or "",
                    "events": copy.deepcopy(events[start:])}

    def cancel(self, job_id):
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise APIError(404, "未找到后台任务")
            if job["status"] == "running":
                job["_cancel"] = True
                if job["_task"] is not None:
                    self._loop.call_soon_threadsafe(job["_task"].cancel)
        return {"ok": True}

    def _session(self, session_id):
        if not IDENTIFIER.fullmatch(session_id):
            raise APIError(400, "无效的会话 ID")
        state = self.store.load_session(session_id)
        if state is None:
            raise APIError(404, "未找到会话")
        with self._lock:
            active_job_id = next((job["job_id"] for job in self._jobs.values()
                if job["status"] == "running" and (job.get("session_id") == session_id
                    or session_id in job["_session_ids"])), None)
        # 由服务端判定每个待处理调用的审批状态，前端不再自己推断：
        #   needs_approval  需要用户现在决定（需要审批且还没有决定）；
        #   auto_approved   已被审批策略自动放行（模式或规则），只展示；
        #   risk / preview / suggested_rule  供审批卡片展示风险、改动预览与“总是允许”的选项。
        pending, decisions = [], state.get("decisions", {})
        for raw in state.get("pending", []):
            item = dict(raw)
            try:
                call = ToolCall.from_dict(raw)
                risk = self.tools.call_risk(call)
                gated = risk != "read"
                item.update(risk=risk, interactive=self.tools.is_interactive(call),
                            needs_approval=gated and raw["id"] not in decisions,
                            auto_approved=gated and decisions.get(raw["id"]) is True)
                if item["needs_approval"]:
                    item["preview"] = build_preview(call, self.workspace)
                    item["suggested_rule"] = suggest_rule(call, risk)
            except Exception:
                item.update(risk="destructive", interactive=False, needs_approval=True, auto_approved=False)
            pending.append(item)
        return dict(state, pending=pending, events=self.store.events(session_id),
                    memory=self.store.recall(session_id), approvals=self.store.list_approvals(session_id),
                    active=active_job_id is not None, active_job_id=active_job_id)

    async def _evaluate(self, job):
        with tempfile.TemporaryDirectory(prefix="agentlab-web-eval-") as temporary:
            stores = []
            def record(event):
                with self._lock:
                    job["events"].append(copy.deepcopy(event))

            def factory():
                store = SQLiteStore(":memory:")
                stores.append(store)
                return Agent(DemoProvider(), store=store, workspace=temporary,
                             on_event=record)
            try:
                return await evaluate(factory, [
                    EvalCase("算术", "/calc 6 * 7", ["42"], ["calculator"]),
                    EvalCase("括号优先级", "/calc (2 + 3) * 3", ["15"], ["calculator"]),
                    EvalCase("工具错误可观察", "/calc 1 / 0", ['"ok": false'], ["calculator"]),
                ])
            finally:
                for store in stores:
                    store.close()

    def import_files(self, payload):
        files = payload.get("files")
        if not isinstance(files, list) or not 1 <= len(files) <= 100:
            raise APIError(400, "请选择 1 至 100 个 Markdown/TXT 文件")
        prepared = {}
        for entry in files:
            if not isinstance(entry, dict):
                raise APIError(400, "文件内容格式错误")
            name, content = entry.get("name"), entry.get("content")
            if (not isinstance(name, str) or not 1 <= len(name) <= 128
                    or name.startswith(".") or any(c in name for c in "/\\\x00")
                    or any(ord(c) < 32 for c in name) or Path(name).suffix.lower() not in (".md", ".txt")):
                raise APIError(400, "文件名必须是无路径的 .md 或 .txt 文件名")
            if not isinstance(content, str) or len(content.encode("utf-8")) > SQLiteStore.MAX_FILE_BYTES:
                raise APIError(400, "文件内容必须为不超过 2 MiB 的文本")
            if name in prepared:
                raise APIError(400, "本次导入包含重复文件名")
            prepared[name] = content
        with self._import_lock:
            directory = self.data_dir / "knowledge"
            if directory.is_symlink():
                raise APIError(400, "知识库目录不可为符号链接")
            directory.mkdir(exist_ok=True)
            for name, content in prepared.items():
                temporary = directory / (".upload-" + uuid.uuid4().hex)
                try:
                    temporary.write_text(content, encoding="utf-8")
                    temporary.replace(directory / name)
                finally:
                    temporary.unlink(missing_ok=True)
            return self.store.ingest(directory)

    def api(self, method, path, payload):
        # 查询串只用于 GET 的增量参数（如 since），路径匹配仍用不含查询串的部分。
        split = urlsplit(path)
        path, query = split.path, split.query
        if method == "GET":
            if path == "/api/health":
                return {"app": "agentlab", "status": "ok"}
            if path == "/api/bootstrap":
                definitions = self.tools.definitions()
                for definition in definitions:
                    tool = self.tools.get(definition["function"]["name"])
                    definition.update(risk=tool.risk, timeout=tool.timeout,
                                      conditional_approval=tool.approval is not None or tool.risk_of is not None,
                                      interactive=tool.interactive,
                                      approval_description=tool.approval_description)
                return {"csrf_token": self.csrf_token, "config": self.public_config(),
                        "warning": self.config_warning,
                        "stats": dict(self.store.knowledge_stats(), sessions=len(self.store.list_sessions())),
                        "tools": definitions}
            if path == "/api/sessions":
                sessions = self.store.list_sessions()
                for row in sessions:
                    state = self.store.load_session(row["session_id"])
                    row["title"] = next((m.get("content", "")[:30] for m in state.get("messages", [])
                                         if m.get("role") == "user"), "未命名会话")
                return {"sessions": sessions}
            if path.startswith("/api/sessions/"):
                return self._session(path[len("/api/sessions/"):])
            if path.startswith("/api/jobs/"):
                remainder = path[len("/api/jobs/"):]
                if remainder.endswith("/events"):
                    return self.job_events(remainder[:-len("/events")], since=_query_int(query, "since", 0))
                return self.job(remainder)
            if path == "/api/approval/rules":
                return {"rules": self.store.list_approval_rules(), "mode": self.public_config()["approval_mode"]}
            if path == "/api/knowledge":
                return {"documents": self.store.list_documents(), "stats": self.store.knowledge_stats()}
            if path == "/api/files":
                try:
                    return list_workspace_files(self.workspace)
                except ValueError as exc:
                    raise APIError(400, str(exc)) from None
            if path.startswith("/api/files/"):
                relative = urllib.parse.unquote(path[len("/api/files/"):])
                try:
                    return read_workspace_file(self.workspace, relative)
                except ValueError as exc:
                    raise APIError(400, str(exc)) from None
            if path.startswith("/api/docs/"):
                name = path[len("/api/docs/"):]
                if name.endswith(".md"):
                    name = name[:-3]
                if name == "README":
                    document = ROOT / "README.md"
                elif name in DOC_NAMES:
                    document = ROOT / "docs" / (name + ".md")
                else:
                    raise APIError(404, "未找到学习文档")
                if not document.is_file():
                    document = RESOURCES / "docs" / (name + ".md")
                if not document.is_file():
                    raise APIError(404, "当前安装不包含此学习文档，请查看源码目录")
                return {"content": document.read_text(encoding="utf-8")}
        elif method == "POST":
            if path == "/api/config":
                return self.configure(payload)
            if path == "/api/cancel":
                return self.cancel(payload.get("job_id"))
            if path == "/api/run":
                prompt = payload.get("prompt")
                session_id = payload.get("session_id") or uuid.uuid4().hex[:16]
                if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 20000:
                    raise APIError(400, "请输入 1 至 20000 字的任务")
                if not isinstance(session_id, str) or not IDENTIFIER.fullmatch(session_id):
                    raise APIError(400, "无效的会话 ID")
                async def run(job):
                    agent = self._agent(job)
                    return await agent.run(prompt, session_id, on_delta=self._delta_handler(job))
                return {"session_id": session_id, "job_id": self.submit(run, session_id, require_provider=True)}
            match = re.fullmatch(r"/api/sessions/([A-Za-z0-9_-]{1,128})/(approve|recover|delete|answer)", path)
            if match:
                session_id, action = match.groups()
                self._session(session_id)
                if action == "delete":
                    with self._lock:
                        busy = any(job["status"] == "running" and (job.get("session_id") == session_id
                                   or session_id in job["_session_ids"]) for job in self._jobs.values())
                    if busy:
                        raise APIError(409, "会话正在运行，请先停止后再删除")
                    try:
                        Agent(DemoProvider(), tools=self.tools, store=self.store,
                              workspace=self.workspace).delete(session_id)
                    except SessionError as exc:
                        raise APIError(409, str(exc)) from None
                    return {"deleted": session_id}
                if action == "recover":
                    return Agent(DemoProvider(), tools=self.tools, store=self.store,
                                 workspace=self.workspace).recover(session_id).to_dict()
                if action == "answer":
                    call_id, text = payload.get("call_id"), payload.get("answer")
                    if not isinstance(call_id, str) or not isinstance(text, str) or not 1 <= len(text.strip()) <= 8000:
                        raise APIError(400, "需要 call_id，以及 1 至 8000 字的 answer")
                    async def answer(job):
                        agent = self._agent(job)
                        return await agent.answer(session_id, call_id, text, on_delta=self._delta_handler(job))
                    return {"session_id": session_id, "job_id": self.submit(answer, session_id, require_provider=True)}
                ids = payload.get("approved_call_ids")
                if not isinstance(ids, list) or any(not isinstance(item, str) for item in ids):
                    raise APIError(400, "approved_call_ids 必须为调用 ID 列表，空列表表示全部拒绝")
                feedback, remember = payload.get("feedback"), payload.get("remember")
                if feedback is not None and (not isinstance(feedback, str) or len(feedback) > 2000):
                    raise APIError(400, "feedback 必须是不超过 2000 字的文本")
                if remember not in (None, "session", "global"):
                    raise APIError(400, "remember 只能是 session 或 global")
                async def approve(job):
                    agent = self._agent(job)
                    return await agent.resume(session_id, ids, on_delta=self._delta_handler(job),
                                              feedback=feedback, remember=remember)
                return {"session_id": session_id, "job_id": self.submit(approve, session_id, require_provider=True)}
            if path == "/api/approval/rules/delete":
                rule_id = payload.get("id")
                if not isinstance(rule_id, str) or not rule_id:
                    raise APIError(400, "需要规则 id")
                if not self.store.delete_approval_rule(rule_id):
                    raise APIError(404, "未找到该规则")
                return {"rules": self.store.list_approval_rules()}
            match = re.fullmatch(r"/api/sessions/([A-Za-z0-9_-]{1,128})/rules/delete", path)
            if match:
                session_id = match.group(1)
                self._session(session_id)
                rule_id = payload.get("id")
                if not isinstance(rule_id, str) or not rule_id:
                    raise APIError(400, "需要规则 id")
                try:
                    removed = Agent(DemoProvider(), tools=self.tools, store=self.store,
                                    workspace=self.workspace).revoke_rule(session_id, rule_id)
                except SessionError as exc:
                    raise APIError(409, str(exc)) from None
                if not removed:
                    raise APIError(404, "未找到该规则")
                return {"ok": True}
            if path == "/api/connection-test":
                async def connection(job):
                    provider = self._provider()
                    response = await provider.complete([Message("user", "请只回复：连接成功")], [])
                    return {"content": response.content, "usage": {
                        "input_tokens": response.usage.input_tokens, "output_tokens": response.usage.output_tokens}}
                return {"job_id": self.submit(connection, require_provider=True)}
            if path == "/api/knowledge/import":
                return self.import_files(payload)
            if path == "/api/files/import":
                try:
                    return upload_workspace_files(self.workspace, payload)
                except ValueError as exc:
                    raise APIError(400, str(exc)) from None
            if path == "/api/knowledge/search":
                query = payload.get("query")
                if not isinstance(query, str) or len(query) > 4000:
                    raise APIError(400, "query 必须为不超过 4000 字的文本")
                return {"results": self.store.search(query)}
            if path == "/api/knowledge/example":
                directory = ROOT / "knowledge"
                if not directory.is_dir():
                    directory = RESOURCES / "knowledge"
                return self.store.ingest(directory)
            if path == "/api/workflow":
                from .cli import run_workflow
                async def workflow(job):
                    return await run_workflow(self._agent(job))
                return {"job_id": self.submit(workflow, require_provider=True)}
            if path == "/api/evaluate":
                return {"job_id": self.submit(self._evaluate)}
        raise APIError(404, "未找到接口")

    def create_server(self, port=0):
        if not isinstance(port, int) or isinstance(port, bool) or not 0 <= port <= 65535:
            raise ValueError("port 必须在 0 至 65535 之间")
        app = self

        class Server(ThreadingHTTPServer):
            address_family = socket.AF_INET6 if app.host == "::1" else socket.AF_INET
            # Wait for in-flight HTTP handlers before closing their SQLite store.
            daemon_threads = False
            allow_reuse_address = True

            def __init__(self, *args):
                self.serving = threading.Event()
                super().__init__(*args)

            def serve_forever(self, poll_interval=0.1):
                self.serving.set()
                try:
                    super().serve_forever(poll_interval)
                finally:
                    self.serving.clear()

        class Handler(BaseHTTPRequestHandler):
            server_version = "AgentLab"
            sys_version = ""

            def setup(self):
                super().setup()
                self.connection.settimeout(10)

            def log_message(self, format, *args):
                pass  # Request paths and payloads can contain private task text.

            def _allowed(self):
                port = self.server.server_address[1]
                hosts = {"[::1]:{}".format(port), "localhost:{}".format(port)} if app.host == "::1" else {
                    "127.0.0.1:{}".format(port), "localhost:{}".format(port)}
                values = self.headers.get_all("Host", [])
                if len(values) != 1 or values[0] not in hosts:
                    raise APIError(403, "Host 不被允许")
                origins = self.headers.get_all("Origin", [])
                if len(origins) > 1 or (origins and origins[0] != "http://" + values[0]):
                    raise APIError(403, "Origin 不被允许")

            def _headers(self, status, content_type, length):
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(length))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; font-src 'self'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'")
                self.send_header("X-Frame-Options", "DENY")
                self.end_headers()

            def _json(self, status, value):
                with app._lock:
                    body = json.dumps(app._redact(value), ensure_ascii=False, allow_nan=False).encode("utf-8")
                self._headers(status, "application/json; charset=utf-8", len(body))
                self.wfile.write(body)

            def _dispatch(self, method):
                try:
                    self._allowed()
                    # request_target 保留查询串交给 api()（增量拉取用 since 参数，
                    # 剥掉会让 api() 永远读到空查询串）；path 仅用于静态资源路由匹配。
                    # 注意不要与下面读取请求体的局部变量重名。
                    request_target = self.path
                    path = urlsplit(request_target).path
                    payload = {}
                    if method == "POST":
                        tokens = self.headers.get_all("X-AgentLab-Token", [])
                        if len(tokens) != 1 or not hmac.compare_digest(tokens[0], app.csrf_token):
                            raise APIError(403, "缺少有效的工作台请求令牌，请刷新页面")
                        if self.headers.get_content_type() != "application/json":
                            raise APIError(415, "POST 请求需要 application/json")
                        lengths = self.headers.get_all("Content-Length", [])
                        if self.headers.get("Transfer-Encoding") or len(lengths) != 1:
                            raise APIError(400, "请求需要唯一的 Content-Length")
                        try:
                            length = int(lengths[0])
                        except ValueError:
                            raise APIError(400, "无效的 Content-Length")
                        if not 0 <= length <= MAX_BODY:
                            raise APIError(413, "请求体不能超过 2 MiB")
                        raw = self.rfile.read(length)
                        if len(raw) != length:
                            raise APIError(400, "请求体不完整")
                        try:
                            payload = json.loads(raw.decode("utf-8"), parse_constant=lambda value: (_ for _ in ()).throw(ValueError()))
                        except (ValueError, UnicodeDecodeError):
                            raise APIError(400, "请求体必须为有效 JSON")
                        if not isinstance(payload, dict):
                            raise APIError(400, "JSON 请求体必须为对象")
                    if path.startswith("/api/"):
                        self._json(200, app.api(method, request_target, payload))
                        return
                    static = {"/": ("index.html", "text/html"), "/index.html": ("index.html", "text/html"),
                              "/app.js": ("app.js", "application/javascript"), "/style.css": ("style.css", "text/css")}
                    if method != "GET" or path not in static:
                        raise APIError(404, "未找到资源")
                    filename, content_type = static[path]
                    try:
                        body = (STATIC / filename).read_bytes()
                    except FileNotFoundError:
                        raise APIError(404, "Web 静态资源未安装")
                    self._headers(200, content_type + "; charset=utf-8", len(body))
                    self.wfile.write(body)
                except APIError as exc:
                    self._json(exc.status, {"error": exc.message})
                except (SessionError, ProviderError, ValueError) as exc:
                    self._json(400, {"error": str(exc)})
                except (BrokenPipeError, ConnectionResetError, TimeoutError):
                    pass
                except Exception as exc:
                    self._json(500, {"error": "请求失败：" + type(exc).__name__})

            def do_GET(self):
                self._dispatch("GET")

            def do_POST(self):
                self._dispatch("POST")

            def _unsupported(self):
                try:
                    self._allowed()
                    self._json(405, {"error": "该 HTTP 方法不被允许"})
                except APIError as exc:
                    self._json(exc.status, {"error": exc.message})

            do_OPTIONS = _unsupported
            do_PUT = _unsupported
            do_PATCH = _unsupported
            do_DELETE = _unsupported

            def send_error(self, code, message=None, explain=None):
                # BaseHTTPRequestHandler's default errors are HTML and echo input.
                self._json(code, {"error": "HTTP 请求无效或方法不被支持"})

        server = Server(("127.0.0.1" if self.host == "localhost" else self.host, port), Handler)
        self._servers.append(server)
        return server

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
        for server in self._servers:
            if server.serving.is_set():
                server.shutdown()
            server.server_close()

        async def shutdown():
            tasks = [task for task in asyncio.all_tasks() if task is not asyncio.current_task()]
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await self._loop.shutdown_asyncgens()
            await self._loop.shutdown_default_executor()

        future = asyncio.run_coroutine_threadsafe(shutdown(), self._loop)
        try:
            future.result(timeout=40)
        finally:
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._thread.join(timeout=5)
            self.store.close()


def serve(data_dir=".agentlab", workspace="workspace", host="127.0.0.1", port=8765, open_browser=False):
    app = App(data_dir=data_dir, workspace=workspace, host=host)
    try:
        server = app.create_server(port)
        hostname = "[::1]" if host == "::1" else "127.0.0.1"
        url = "http://{}:{}".format(hostname, server.server_address[1])
        print("Agent Lab Web 工作台：" + url, flush=True)
        if open_browser:
            webbrowser.open(url)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("\n正在关闭 Agent Lab…", flush=True)
    finally:
        app.close()
