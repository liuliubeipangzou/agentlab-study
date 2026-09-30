/* Agent Lab: native DOM only. Untrusted output never enters innerHTML. */
"use strict";
const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const state = {csrf: null, config: null, tools: [], view: "chat", inspector: "trace", sessions: [], currentId: null, current: null, cache: new Map(), jobs: new Map(), drafts: new Map(), chatNotice: "", chatEpoch: 0, sending: false, approving: false, cancelling: false, polling: false, online: false, ready: false, lastRefresh: 0, messageStamp: "", inspectorStamp: "", approvalStamp: "", recoveryStamp: "", lesson: "learning-path.md", lessonEpoch: 0, knowledgeEpoch: 0};
const titles = {chat: "对话工作台", knowledge: "知识库", tools: "工具箱", learning: "学习路径", lab: "协作实验", settings: "设置"};
const statuses = {running: "运行中", completed: "已完成", waiting_approval: "等待审批", failed: "运行失败", cancelled: "已停止", limited: "达到限制", pending: "等待开始", success: "已完成", skipped: "已跳过"};
const lessons = [["learning-path.md", "从零开始", "学习地图与第一个 Agent"], ["architecture.md", "理解执行循环", "消息、模型与工具如何协作"], ["tools.md", "赋予 Agent 能力", "工具协议、权限和安全边界"], ["memory-workflows.md", "记忆与多步协作", "知识检索、会话记忆与 DAG"], ["providers.md", "接入真实模型", "兼容接口、工具调用与重试"], ["operations.md", "让系统稳定运行", "审批、恢复、配置与运维"], ["validation.md", "验证你的 Agent", "回归测试与可复现的检查"]];
const eventNames = {run_started: "开始处理任务", model_started: "调用模型", model_finished: "模型返回响应", tool_started: "开始执行工具", tool_finished: "工具执行完成", approval_requested: "等待你的批准", approval_resolved: "审批决定已提交", run_completed: "任务已完成", run_failed: "任务运行失败", run_cancelled: "任务已停止", run_limited: "达到运行限制", run_waiting_approval: "已暂停，等待审批"};
const toolInfo = {calculator: ["计算器", "terminal", "blue", "通过受限表达式完成数学计算。", "/calc (20 + 1) * 2"], read_file: ["文件读取", "file", "blue", "读取工作区内的 UTF-8 文本文件。", "/read notes/day1.txt"], write_file: ["文件写入", "file", "amber", "创建学习笔记，写入前由你确认。", "/write notes/day1.txt 今天学会了工具调用"], search_knowledge: ["知识检索", "search", "purple", "从本地资料中查找相关知识片段。", "/search Agent 记忆"], remember: ["保存记忆", "book", "green", "把重要信息保存在当前会话中。", "/remember goal 掌握Agent执行循环"], recall: ["回忆信息", "book", "purple", "按关键词查询这个会话的长期记忆。", "/recall goal"]};

function el(tag, className, text) { const value = document.createElement(tag); if (className) value.className = className; if (text !== undefined && text !== null) value.textContent = String(text); return value; }
function icon(name, extra = "") { const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg"), use = document.createElementNS("http://www.w3.org/2000/svg", "use"); svg.setAttribute("class", "icon " + extra); svg.setAttribute("aria-hidden", "true"); use.setAttribute("href", "#i-" + name); svg.append(use); return svg; }
function button(text, className = "button secondary", action) { const value = el("button", className, text); value.type = "button"; if (action) value.addEventListener("click", action); return value; }
function show(element, visible) { element.classList.toggle("hidden", !visible); }
function busy(element, value) { element.disabled = value; element.classList.toggle("is-loading", value); element.setAttribute("aria-busy", String(value)); }
function pretty(value) { if (typeof value === "string") return value; try { return JSON.stringify(value, null, 2); } catch (_) { return String(value); } }
function badge(text, extra = "subtle") { return el("span", "badge " + extra, text); }
function sourceHeading(source, fallback = "知识片段") { const value = String(source || fallback), heading = el("strong", "", value.split(/[\\/]/).pop() || value); heading.title = value; return heading; }
function empty(title, description, name = "info") { const value = el("div", "empty-state"); value.append(icon(name), el("h4", "", title), el("p", "", description)); return value; }
function details(title, value, key, opened = false) { const element = el("details", "json-details"); if (key) element.dataset.disclosureKey = key; element.open = opened; element.append(el("summary", "", title), el("pre", "code-block", pretty(value))); return element; }
function preserve(host, children) { const opened = new Set($$("details[open][data-disclosure-key]", host).map(element => element.dataset.disclosureKey)); host.replaceChildren(...children); $$("details[data-disclosure-key]", host).forEach(element => { if (opened.has(element.dataset.disclosureKey)) element.open = true; }); }
function toast(message, tone = "success") { const value = el("div", "toast " + tone); value.setAttribute("role", tone === "error" ? "alert" : "status"); value.append(icon(tone === "error" ? "info" : "check"), el("span", "", message), button("×", "toast-close", () => value.remove())); $("#toast-host").append(value); setTimeout(() => value.remove(), tone === "error" ? 9000 : 4500); }
function online(value) { state.online = value; $("#local-dot").classList.toggle("offline", !value); $("#local-status-text").textContent = value ? "本地服务运行中" : "本地服务未连接"; }
async function api(path, data) {
  const controller = new AbortController(), timer = setTimeout(() => controller.abort(), 20000);
  const options = {signal: controller.signal, credentials: "same-origin", headers: {Accept: "application/json"}};
  if (data !== undefined) { if (!state.csrf) { clearTimeout(timer); throw new Error("本地服务尚未连接，请稍后重试。"); } options.method = "POST"; options.headers["Content-Type"] = "application/json"; options.headers["X-AgentLab-Token"] = state.csrf; options.body = JSON.stringify(data); }
  try { const response = await fetch(path, options); online(true); const payload = await response.json(); if (!response.ok) { const error = new Error(payload.error || "请求失败（" + response.status + "）"); error.status = response.status; throw error; } return payload; }
  catch (error) { if (error.name === "AbortError") throw new Error("请求超时，请检查本地服务后重试。"); if (error instanceof TypeError) { online(false); throw new Error("无法连接本地服务，请确认 Agent Lab 仍在运行。"); } throw error; }
  finally { clearTimeout(timer); }
}
function guarded(action) { return async event => { try { await action(event); } catch (error) { toast(error.message, "error"); } }; }
function time(value) { if (!value) return ""; const date = new Date(typeof value === "number" ? value * 1000 : value); return Number.isNaN(date.valueOf()) ? "" : date.toLocaleTimeString("zh-CN", {hour: "2-digit", minute: "2-digit", second: "2-digit"}); }

function inline(text, parent) {
  const regex = /(`[^`\n]+`|\*\*[^*\n]+\*\*|\[[^\]\n]+\]\([^\s)]+\))/g; let cursor = 0;
  for (const match of text.matchAll(regex)) {
    parent.append(document.createTextNode(text.slice(cursor, match.index))); const token = match[0];
    if (token.startsWith("`")) parent.append(el("code", "", token.slice(1, -1)));
    else if (token.startsWith("**")) parent.append(el("strong", "", token.slice(2, -2)));
    else { const parts = /^\[([^\]]+)\]\(([^)]+)\)$/.exec(token), label = parts[1], target = parts[2], doc = target.split("/").pop();
      if (lessons.some(item => item[0] === doc) || doc === "README.md") parent.append(button(label, "inline-doc-link", () => { navigate("learning"); loadLesson(doc); }));
      else if (/^https?:\/\//i.test(target)) { const link = el("a", "", label); link.href = target; link.target = "_blank"; link.rel = "noopener noreferrer"; parent.append(link); }
      else parent.append(el("span", "", label + " (" + target + ")"));
    }
    cursor = match.index + token.length;
  }
  parent.append(document.createTextNode(text.slice(cursor)));
}
function markdown(text) {
  const fragment = document.createDocumentFragment(), lines = String(text || "").replace(/\r\n/g, "\n").split("\n"); let index = 0;
  while (index < lines.length) {
    const line = lines[index]; if (!line.trim()) { index++; continue; }
    if (/^\s*```/.test(line)) { const language = line.replace(/^\s*```/, "").trim(), block = el("div", "markdown-code"), content = []; index++; while (index < lines.length && !/^\s*```/.test(lines[index])) content.push(lines[index++]); if (index < lines.length) index++; if (language) block.append(el("span", "code-language", language)); block.append(el("pre", "code-block", content.join("\n"))); fragment.append(block); continue; }
    const heading = /^(#{1,6})\s+(.+)$/.exec(line); if (heading) { const value = el("h" + heading[1].length); inline(heading[2], value); fragment.append(value); index++; continue; }
    if (/^\s*([-*_])\1\1+\s*$/.test(line)) { fragment.append(el("hr")); index++; continue; }
    if (/^\s*>/.test(line)) { const value = el("blockquote"); inline(line.replace(/^\s*>\s?/, ""), value); fragment.append(value); index++; continue; }
    const list = /^\s*(?:([-*+])|(\d+)\.)\s+(.+)$/.exec(line);
    if (list) { const value = el(list[2] ? "ol" : "ul"); while (index < lines.length) { const item = /^\s*(?:([-*+])|(\d+)\.)\s+(.+)$/.exec(lines[index]); if (!item || Boolean(item[2]) !== Boolean(list[2])) break; const li = el("li"); inline(item[3], li); value.append(li); index++; } fragment.append(value); continue; }
    if (line.trim().startsWith("|") && index + 1 < lines.length && /^\s*\|[\s:|\-]+\|\s*$/.test(lines[index + 1])) {
      const wrapper = el("div", "markdown-table-wrap"), table = el("table"), thead = el("thead"), tr = el("tr"), cells = value => value.trim().replace(/^\||\|$/g, "").split("|").map(cell => cell.trim());
      cells(line).forEach(cell => { const th = el("th"); inline(cell, th); tr.append(th); }); thead.append(tr); table.append(thead); index += 2; const tbody = el("tbody");
      while (index < lines.length && lines[index].trim().startsWith("|")) { const row = el("tr"); cells(lines[index++]).forEach(cell => { const td = el("td"); inline(cell, td); row.append(td); }); tbody.append(row); }
      table.append(tbody); wrapper.append(table); fragment.append(wrapper); continue;
    }
    const content = []; while (index < lines.length && lines[index].trim() && !/^(\s*```|#{1,6}\s|\s*>|\s*[-*+]\s|\s*\d+\.\s)/.test(lines[index])) content.push(lines[index++]); if (!content.length) content.push(lines[index++]); const p = el("p"); inline(content.join("\n"), p); fragment.append(p);
  }
  return fragment;
}
function resetPageScroll() {
  // Reset page containers only: the conversation's #messages-scroll keeps its position.
  const containers = [$(".main-shell"), document.scrollingElement, document.documentElement, document.body];
  containers.forEach(element => { if (element) { element.scrollTop = 0; element.scrollLeft = 0; } });
  if (typeof window.scrollTo === "function") window.scrollTo(0, 0);
}
function navigate(view, updateHash = true) {
  if (!titles[view]) view = "chat"; const changed = state.view !== view; state.view = view;
  $$(".view[data-view]").forEach(element => show(element, element.dataset.view === view));
  $$("[data-nav]").forEach(element => { const active = element.dataset.nav === view; element.classList.toggle("active", active); if (active) element.setAttribute("aria-current", "page"); else element.removeAttribute("aria-current"); });
  $("#page-title").textContent = titles[view]; $("#breadcrumb-current").textContent = titles[view]; document.title = titles[view] + " · Agent Lab"; document.body.classList.remove("sidebar-open");
  if (changed) resetPageScroll();
  if (updateHash && location.hash !== "#" + view) history.replaceState(null, "", "#" + view);
  if (view === "knowledge" && state.ready) refreshKnowledge().catch(error => toast(error.message, "error"));
  if (view === "learning" && !$("#lesson-content").hasChildNodes()) loadLesson(state.lesson);
  if (view === "chat") { renderChat(); renderInspector(); }
}
function applyConfig(config) {
  state.config = config; const demo = config.provider !== "openai";
  $("#provider-badge").replaceChildren(el("span", "status-dot"), document.createTextNode(demo ? "演示模式" : "真实模型")); $("#provider-badge").classList.toggle("demo-badge", demo);
  $("#model-label").textContent = demo ? "Demo · 无需 API Key" : config.model;
  $("#mode-description").textContent = demo ? "当前使用演示模型，输入示例指令即可体验完整 Agent 流程。" : "已配置 " + config.model + "，可以直接用自然语言提出任务。写入操作需要审批。";
  $("#banner-settings").firstChild.textContent = demo ? "配置真实模型" : "模型设置";
  $("#api-key-status").textContent = config.has_api_key ? "本次启动已设置" : "未设置"; $("#settings-saved-status").textContent = "已保存 · " + (demo ? "演示模式" : "真实模型");
  $("#config-provider").value = config.provider || "demo"; $("#config-model").value = config.model || "deepseek-flash"; $("#config-base-url").value = config.base_url || "https://api.deepseek.com";
}
async function refreshSessions() { const result = await api("/api/sessions"); state.sessions = result.sessions || []; state.lastRefresh = Date.now(); renderSessions(); }
function renderSessions() {
  const host = $("#recent-sessions"); if (!state.sessions.length) { host.replaceChildren(el("p", "sidebar-empty", "还没有对话，从第一个实验开始")); return; }
  host.replaceChildren(...state.sessions.slice(0, 30).map(session => { const item = button("", "session-item" + (session.session_id === state.currentId ? " active" : ""), guarded(() => selectSession(session.session_id))); item.append(icon("chat", "small"), el("span", "session-title", session.title || "未命名对话")); item.title = (session.title || session.session_id) + " · " + (statuses[session.status] || session.status); if (["running", "waiting_approval"].includes(session.status)) item.append(el("span", "session-status " + session.status)); return item; }));
}
function resetStamps() { state.messageStamp = ""; state.inspectorStamp = ""; state.approvalStamp = ""; state.recoveryStamp = ""; }
function draftKey(id = state.currentId) { return id || "__new__"; }
function saveDraft() { state.drafts.set(draftKey(), $("#prompt").value); }
function newSession() { saveDraft(); state.drafts.delete(draftKey(null)); state.chatEpoch++; state.currentId = null; state.current = null; state.chatNotice = ""; resetStamps(); $("#prompt").value = ""; navigate("chat"); renderSessions(); renderChat(); renderInspector(); $("#prompt").focus(); }
async function selectSession(id) { saveDraft(); state.chatEpoch++; const epoch = state.chatEpoch; state.currentId = id; state.current = state.cache.get(id) || null; state.chatNotice = ""; $("#prompt").value = state.drafts.get(draftKey(id)) || ""; resetStamps(); navigate("chat"); renderSessions(); $("#run-state").textContent = "正在恢复会话…"; show($("#run-state"), !state.current); try { await refreshCurrent(id, epoch); } catch (error) { if (state.currentId === id) { $("#run-state").textContent = error.message; show($("#run-state"), true); } throw error; } }
async function refreshCurrent(id = state.currentId, epoch = state.chatEpoch) {
  if (!id) return; let session;
  try { session = await api("/api/sessions/" + encodeURIComponent(id)); }
  catch (error) {
    if (error.status === 404) {
      const matching = [...state.jobs.values()].filter(job => job.kind === "run" && job.sessionId === id), job = matching[matching.length - 1];
      if (job && job.status === "running") return;
      if (job && ["failed", "cancelled"].includes(job.status) && job.originalPrompt && state.currentId === id && state.chatEpoch === epoch) {
        state.cache.delete(id); state.drafts.delete(draftKey(id)); state.currentId = null; state.current = null; state.chatEpoch++;
        state.chatNotice = (job.error || (job.status === "cancelled" ? "任务在创建会话前已停止。" : "任务未能启动。")) + " 原消息已恢复到输入框。";
        $("#prompt").value = job.originalPrompt; state.drafts.set(draftKey(null), job.originalPrompt); resetStamps(); renderSessions(); renderChat(); renderInspector(); return;
      }
    }
    throw error;
  }
  state.cache.set(id, session); if (session.active_job_id && !state.jobs.has(session.active_job_id)) trackJob(session.active_job_id, "run", id); if (state.currentId !== id || epoch !== state.chatEpoch) return; state.current = session; renderChat(); renderInspector();
}
function currentJob() { return [...state.jobs.values()].find(job => job.kind === "run" && job.sessionId === state.currentId && job.status === "running"); }
function running() { return state.sending || Boolean(currentJob()) || Boolean(state.current && state.current.active); }
function pending() { return state.current && state.current.status === "waiting_approval"; }
function recovery() { return Boolean(state.current && state.current.status === "running" && !state.current.active && !currentJob() && !state.sending); }
function writeCall(call) { const definition = state.tools.find(tool => (tool.function || tool).name === call.name); return definition && definition.risk ? definition.risk === "write" : ["write_file", "remember"].includes(call.name); }
function callMap(messages) { const map = new Map(); messages.forEach(message => (message.tool_calls || []).forEach(call => map.set(call.id, call))); return map; }
function readableValue(value, host, toolName) {
  if (typeof value === "number") { host.append(el("p", "answer-result", "计算结果：" + value)); return; }
  if (typeof value === "string") { host.append(markdown(value)); return; }
  if (Array.isArray(value)) { if (!value.length) host.append(el("p", "", "没有找到匹配结果。可以先导入知识，或调整查询关键词。")); value.forEach(item => { const card = el("div", "search-result"); if (item && typeof item === "object") { card.append(sourceHeading(item.source || item.key, "检索结果"), el("p", "search-result-text", item.text || item.content || (item.value !== undefined ? pretty(item.value) : pretty(item)))); } else card.append(el("p", "", pretty(item))); host.append(card); }); return; }
  if (value && value.bytes_written !== undefined) { host.append(el("p", "", "已保存文件：" + value.path + "（" + value.bytes_written + " 字节）")); return; }
  if (value && value.remembered) { host.append(el("p", "", "已记住「" + value.remembered + "」。可以在右侧「记忆」查看，或通过 /recall 查询。")); return; }
  host.append(details("查看返回数据", value, "answer-" + (toolName || "result"), true));
}
function assistantContent(content, host) {
  const prefix = "离线 Demo 工具执行结果：\n";
  if (content.startsWith(prefix)) { try { const result = JSON.parse(content.slice(prefix.length)); if (result && typeof result === "object" && typeof result.ok === "boolean") { if (result.ok) readableValue(result.value, host); else host.append(el("p", "", "这次操作未完成：" + (result.error || "工具返回错误"))); return; } } catch (_) { /* Treat malformed provider output as plain Markdown. */ } }
  host.append(markdown(content));
}
function toolResult(message, call, index) {
  let result; try { result = JSON.parse(message.content); } catch (_) { result = {ok: true, value: message.content}; }
  const element = el("details", "tool-card " + (result.ok ? "success" : "error")); element.dataset.disclosureKey = "tool-" + (message.tool_call_id || index);
  const summary = el("summary", "tool-summary"); summary.append(icon("tools", "small"), el("strong", "", call ? call.name : "工具结果"), badge(result.ok ? "执行成功" : "未执行成功", result.ok ? "success" : "danger"), icon("chevron", "small")); element.append(summary);
  const body = el("div", "tool-card-body"); if (call) body.append(details("调用参数", call.arguments, "args-" + call.id)); if (result.ok) readableValue(result.value, body, call && call.name); else body.append(el("p", "tool-result-text", result.error)); body.append(details("原始返回值", result, "raw-" + (message.tool_call_id || index))); element.append(body); return element;
}
function renderChat() {
  const session = state.current, messages = session && Array.isArray(session.messages) ? session.messages : [], visible = messages.filter(message => message.role !== "system");
  const stamp = JSON.stringify([state.currentId, messages]);
  if (stamp !== state.messageStamp) {
    state.messageStamp = stamp; const scroller = $("#messages-scroll"), nearBottom = scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight < 140, names = callMap(messages), children = [];
    visible.forEach((message, index) => {
      if (message.role === "tool") { children.push(toolResult(message, names.get(message.tool_call_id), index)); return; }
      if (!message.content && !(message.tool_calls || []).length) return;
      const article = el("article", "message " + (message.role === "user" ? "user" : "assistant")), avatar = el("div", "message-avatar", message.role === "user" ? "你" : ""), body = el("div", "message-body");
      if (message.role !== "user") avatar.append(icon("spark", "small")); body.append(el("div", "message-author", message.role === "user" ? "你" : "Agent Lab"));
      if (message.content) { const content = el("div", "message-content markdown-body"); if (message.role === "user") content.append(document.createTextNode(message.content)); else assistantContent(message.content, content); body.append(content); }
      if ((message.tool_calls || []).length) { const calls = el("div", "message-tool-calls"); message.tool_calls.forEach(call => { const chip = el("span", "tool-call-chip"); chip.append(icon("tools", "small"), document.createTextNode(call.name)); calls.append(chip); }); body.append(calls); }
      article.append(avatar, body); children.push(article);
    });
    preserve($("#message-list"), children); show($("#welcome"), !visible.length && !state.currentId); if (nearBottom || visible.length <= 1) requestAnimationFrame(() => { scroller.scrollTop = scroller.scrollHeight; });
  }
  const active = running(), waiting = pending(), interrupted = recovery(), host = $("#run-state"); host.classList.remove("error");
  if (active) { host.replaceChildren(el("span", "loading-dot"), document.createTextNode("Agent 正在处理任务，右侧可查看执行进展…")); show(host, true); }
  else if (session && ["failed", "limited", "cancelled"].includes(session.status)) { host.textContent = session.output || statuses[session.status]; host.classList.toggle("error", session.status === "failed"); show(host, true); }
  else if (state.chatNotice) { host.textContent = state.chatNotice; host.classList.add("error"); show(host, true); }
  else show(host, false);
  renderApproval(); renderRecovery();
  const disabled = !state.ready || active || waiting || interrupted || state.approving; $("#prompt").disabled = disabled; $("#send-message").disabled = disabled || !$("#prompt").value.trim(); show($("#cancel-run"), Boolean(currentJob())); $("#cancel-run").disabled = state.cancelling;
  $("#prompt").placeholder = waiting ? "请先批准或拒绝上方工具调用，再继续对话…" : interrupted ? "请先恢复中断的会话状态…" : "向 Agent 提出问题，或选择上方示例开始…";
  $("#composer-status").textContent = waiting ? "等待你确认工具操作" : interrupted ? "检测到中断的运行" : active ? "运行中 · 任务在切换页面后继续" : state.currentId ? "可以继续这段对话" : "一切就绪，开始你的第一个实验";
}
function renderApproval() {
  const calls = pending() ? state.current.pending || [] : [], stamp = JSON.stringify([state.currentId, calls, state.approving]); if (stamp === state.approvalStamp) return; state.approvalStamp = stamp;
  const host = $("#approval-host"); if (!calls.length) { host.replaceChildren(); return; }
  const selected = new Set($$("input[data-call-id]:checked", host).map(input => input.dataset.callId)), hadChoices = Boolean($("input[data-call-id]", host)), card = el("section", "approval-card"), heading = el("div", "approval-heading");
  heading.append(icon("check"), el("h3", "", "这个操作需要你的确认")); card.append(heading, el("p", "muted", "检查参数后再继续。未勾选的写入操作会被拒绝，普通读取工具会按计划执行。"));
  calls.forEach(call => { const item = el("div", "approval-item"), title = el("label", "approval-tool-label"); if (writeCall(call)) { const checkbox = el("input"); checkbox.type = "checkbox"; checkbox.dataset.callId = call.id; checkbox.checked = hadChoices ? selected.has(call.id) : true; checkbox.disabled = state.approving; title.append(checkbox, el("strong", "", call.name), badge("需要批准", "warning")); } else title.append(el("strong", "", call.name), badge("读取操作")); item.append(title, el("pre", "code-block", pretty(call.arguments))); card.append(item); });
  const actions = el("div", "approval-actions"), buttons = [button("批准所选并继续", "button primary", guarded(() => approve($$("input[data-call-id]:checked", host).map(input => input.dataset.callId)))), button("全部批准", "button secondary", guarded(() => approve(calls.filter(writeCall).map(call => call.id)))), button("全部拒绝", "button ghost danger", guarded(() => approve([])))]; buttons.forEach(value => { value.disabled = state.approving; actions.append(value); }); card.append(actions); host.replaceChildren(card);
}
function renderRecovery() {
  const stamp = String(state.currentId) + ":" + recovery(); if (stamp === state.recoveryStamp) return; state.recoveryStamp = stamp; const host = $("#recovery-host"); host.replaceChildren(); if (!recovery()) return;
  const card = el("div", "approval-card recovery-card"); card.append(el("h3", "", "上次运行意外中断"), el("p", "", "恢复会话输入会关闭未完成的调用，不会重放工具。文件写入可能已经完成，请先检查工作区。"), button("恢复会话输入", "button secondary", guarded(async event => { const target = event.currentTarget, id = state.currentId; busy(target, true); try { await api("/api/sessions/" + encodeURIComponent(id) + "/recover", {}); if (id === state.currentId) await refreshCurrent(); await refreshSessions(); toast("已恢复输入，请确认上次操作结果后再继续。"); } finally { state.recoveryStamp = ""; renderChat(); } }))); host.append(card);
}
function inspectorEmpty() {
  const host = el("div", "inspector-empty"); host.append(el("span", "eyebrow", "UNDER THE HOOD"), el("h3", "", "不只看答案，也看过程"), el("p", "muted", "发起对话后，Agent 的每一步都会在这里出现。"));
  [["01", "理解任务", "模型读取你的消息与可用工具"], ["02", "调用工具", "校验参数，执行需要的操作"], ["03", "组织回答", "结合工具结果，给出最终回复"]].forEach(([number, title, text]) => { const step = el("div", "trace-placeholder-step"), body = el("div"); body.append(el("strong", "", title), el("p", "", text)); step.append(el("span", "step-number", number), body); host.append(step); }); const note = el("div", "inspector-learning-note"); note.append(icon("bolt", "small"), el("span", "", "先试试「体验工具调用」")); host.append(note); return host;
}
function renderInspector() {
  const session = state.current, status = session ? session.status : "pending", usage = session && session.usage || {};
  $("#inspector-status").textContent = session ? statuses[status] || status : "待开始"; $("#metric-steps").textContent = session ? session.steps || 0 : "—"; $("#metric-tools").textContent = session ? session.tool_count || session.tool_calls || 0 : "—"; $("#metric-tokens").textContent = session ? Number((usage.input_tokens || 0) + (usage.output_tokens || 0)).toLocaleString("zh-CN") : "—";
  const data = !session ? null : state.inspector === "trace" ? session.events || [] : state.inspector === "memory" ? session.memory || [] : session.messages || [], stamp = JSON.stringify([state.currentId, state.inspector, data]); if (stamp === state.inspectorStamp) return; state.inspectorStamp = stamp;
  const children = [];
  if (!session && state.inspector === "trace") children.push(inspectorEmpty());
  else if (!data || !data.length) children.push(empty(state.inspector === "memory" ? "还没有会话记忆" : state.inspector === "messages" ? "等待第一条消息" : "等待第一步执行", state.inspector === "memory" ? "试试 /remember goal 掌握Agent执行循环，批准后可在这里查看。" : "发起任务后，这里会记录 Agent 的真实执行过程。", state.inspector === "memory" ? "book" : "workflow"));
  else if (state.inspector === "trace") data.forEach((event, index) => { const row = el("div", "trace-event " + event.type), body = el("div", "trace-event-body"), heading = el("div", "trace-event-heading"); heading.append(el("strong", "", eventNames[event.type] || event.type), el("time", "", time(event.time))); body.append(heading); if (event.data && event.data.name) body.append(el("code", "trace-tool-name", event.data.name)); if (event.data && event.data.step) body.append(el("span", "trace-step-label", "第 " + event.data.step + " 轮")); if (event.data && Object.keys(event.data).length) body.append(details("查看事件数据", event.data, "event-" + index)); row.append(el("span", "trace-event-dot"), body); children.push(row); });
  else if (state.inspector === "memory") data.forEach((memory, index) => { const card = el("div", "memory-card"); card.append(el("strong", "", memory.key || "记忆 " + (index + 1)), el("p", "", pretty(memory.value === undefined ? memory : memory.value))); children.push(card); });
  else data.forEach((message, index) => children.push(details(String(index + 1).padStart(2, "0") + " · " + message.role + (message.tool_calls && message.tool_calls.length ? " · 工具调用" : ""), message, "message-json-" + index)));
  preserve($("#inspector-content"), children);
}

async function sendPrompt() {
  const prompt = $("#prompt").value.trim(); if (!prompt || running() || pending() || recovery() || !state.ready) return;
  const epoch = state.chatEpoch, originalId = state.currentId; state.sending = true; state.chatNotice = ""; renderChat();
  try {
    const result = await api("/api/run", {prompt, ...(originalId ? {session_id: originalId} : {})}); trackJob(result.job_id, "run", result.session_id, {originalPrompt: prompt, originalSessionId: originalId}); state.drafts.delete(draftKey(originalId));
    if (state.chatEpoch === epoch) {
      const previous = state.current || {}; state.currentId = result.session_id;
      state.current = {...previous, session_id: result.session_id, status: "running", active: true, active_job_id: result.job_id, pending: [], messages: [...(previous.messages || []), {role: "user", content: prompt}], output: "", steps: 0, tool_count: 0, usage: {input_tokens: 0, output_tokens: 0}};
      state.cache.set(result.session_id, state.current); state.drafts.set(draftKey(result.session_id), ""); $("#prompt").value = ""; renderChat(); renderInspector(); await refreshCurrent(result.session_id, epoch);
    }
    await refreshSessions();
  }
  finally { state.sending = false; renderChat(); }
  poll();
}
async function approve(ids) { if (state.approving || !state.currentId) return; const id = state.currentId; state.approving = true; renderChat(); try { const result = await api("/api/sessions/" + encodeURIComponent(id) + "/approve", {approved_call_ids: ids}); trackJob(result.job_id, "run", result.session_id || id); if (id === state.currentId) await refreshCurrent(); toast(ids.length ? "已提交批准，Agent 将继续执行。" : "已拒绝写入操作，Agent 将继续处理结果。"); } finally { state.approving = false; renderChat(); } poll(); }
function trackJob(id, kind, sessionId = null, metadata = {}) { if (!id) throw new Error("服务未返回任务编号，请刷新后查看会话状态。"); state.jobs.set(id, {id, kind, sessionId, status: "running", ...metadata}); updateJobButtons(); }
function updateJobButtons() { const active = kind => [...state.jobs.values()].some(job => job.kind === kind && job.status === "running"); busy($("#run-workflow"), active("workflow")); busy($("#run-evaluation"), active("evaluate")); busy($("#test-connection"), active("connection")); }
async function poll() {
  if (!state.ready || state.polling) return; state.polling = true;
  try { const active = [...state.jobs.values()].filter(job => job.status === "running"); await Promise.all(active.map(async job => { try { const result = await api("/api/jobs/" + encodeURIComponent(job.id)); job.status = result.status; job.result = result.result; job.error = result.error; if (result.status !== "running") finishJob(job); } catch (error) { if (error.status === 404) { job.status = "failed"; job.error = "该任务已不在当前服务中，请检查会话是否需要恢复。"; finishJob(job); } } })); if (state.currentId) await refreshCurrent(); if (Date.now() - state.lastRefresh > 5000 || active.some(job => job.status !== "running")) await refreshSessions(); updateJobButtons(); }
  catch (_) { /* Retry on the next poll; connection state is visible in the sidebar. */ }
  finally { state.polling = false; }
}
function finishJob(job) {
  if (job.notified) return; job.notified = true;
  if (job.kind === "run") {
    if (job.sessionId === state.currentId && state.current) { state.current.active = false; state.current.active_job_id = null; state.current.status = job.result && job.result.status || job.status; state.current.output = job.result && job.result.output || job.error || ""; renderChat(); }
    if (job.status === "failed" && job.error) toast(job.error, "error"); if (job.sessionId !== state.currentId && job.result) toast("后台对话" + (statuses[job.result.status] || "已完成") + "，可在最近对话中查看。");
  }
  else if (job.kind === "connection") { const host = $("#connection-result"); show(host, true); if (job.status === "completed") { host.className = "connection-result success"; host.replaceChildren(el("strong", "", "连接成功"), el("p", "", job.result && job.result.content || "模型已返回响应。")); if (job.result && job.result.usage) host.append(details("本次用量", job.result.usage)); } else { host.className = "connection-result error"; host.textContent = job.error || "连接测试未完成"; } }
  else if (job.kind === "workflow") renderWorkflow(job);
  else if (job.kind === "evaluate") renderEvaluation(job);
}

async function refreshKnowledge() {
  const epoch = ++state.knowledgeEpoch, result = await api("/api/knowledge"); if (epoch !== state.knowledgeEpoch) return;
  const documents = result.documents || [], stats = result.stats || {};
  $("#knowledge-count").textContent = stats.documents ?? documents.length; $("#stat-documents").textContent = stats.documents ?? documents.length; $("#stat-chunks").textContent = stats.chunks ?? documents.reduce((sum, item) => sum + (item.chunks || 0), 0);
  $("#knowledge-sources").replaceChildren(...(documents.length ? documents.map(document => { const row = el("div", "source-row"), information = el("div"); information.append(sourceHeading(document.source), el("small", "muted", String(document.chunks || 0) + " 个知识片段")); row.append(icon("file"), information, badge("已索引", "success")); return row; }) : [empty("知识库还是空的", "导入示例知识，或添加自己的学习资料。", "book")]));
}
async function importFiles(event) {
  const files = [...event.target.files]; if (!files.length) return; const input = event.target; input.disabled = true; $("#upload-status").textContent = "正在读取并导入 " + files.length + " 份资料…";
  try { for (const file of files) { if (!/\.(md|txt)$/i.test(file.name)) throw new Error("只支持 .md 和 .txt 文件：" + file.name); if (file.size > 1024 * 1024) throw new Error("单份文件请小于 1 MiB：" + file.name); } const prepared = await Promise.all(files.map(async file => ({name: file.name, content: await file.text()}))); await api("/api/knowledge/import", {files: prepared}); await refreshKnowledge(); $("#upload-status").textContent = "已导入 " + files.length + " 份资料，可以开始检索。"; toast("知识资料已导入并建立索引。"); }
  catch (error) { $("#upload-status").textContent = error.message; throw error; }
  finally { input.disabled = false; input.value = ""; }
}
async function searchKnowledge(event) {
  event.preventDefault(); const query = $("#knowledge-query").value.trim(); if (!query) return; const host = $("#search-results"); busy($("#knowledge-search-button"), true); host.replaceChildren(el("p", "loading-state", "正在检索本地知识库…"));
  try { const result = await api("/api/knowledge/search", {query}), rows = result.results || []; host.replaceChildren(...(rows.length ? rows.map((item, index) => { const card = el("article", "search-result"), heading = el("div", "search-result-heading"), title = sourceHeading(item.source); title.prepend(document.createTextNode(String(index + 1).padStart(2, "0") + " · ")); heading.append(title); if (item.score !== undefined) heading.append(badge("相关度 " + Number(item.score).toFixed(2))); card.append(heading, el("p", "search-result-text", item.text || item.content || pretty(item))); if (item.chunk !== undefined) card.append(el("small", "muted", "片段 " + item.chunk)); return card; }) : [empty("没有找到匹配片段", "试试更短的关键词，或先导入包含相关内容的资料。", "search")])); }
  catch (error) { host.replaceChildren(empty("检索没有完成", error.message)); throw error; }
  finally { busy($("#knowledge-search-button"), false); }
}
function renderTools() {
  $("#tool-grid").replaceChildren(...state.tools.map(definition => { const tool = definition.function || definition, info = toolInfo[tool.name] || [tool.name, "tools", "blue", tool.description, ""], write = definition.risk === "write" || ["write_file", "remember"].includes(tool.name), card = el("article", "card tool-definition-card"), top = el("div", "tool-definition-top"), mark = el("span", "example-icon " + info[2]); mark.append(icon(info[1])); top.append(mark, badge(write ? "需要审批" : "可直接执行", write ? "warning" : "success")); card.append(top, el("h3", "", info[0]), el("code", "tool-function-name", tool.name), el("p", "muted", info[3]), details("参数 Schema", tool.parameters, "schema-" + tool.name)); if (info[4]) card.append(button("在工作台试试 →", "text-button", () => useExample(info[4]))); return card; }));
}
function renderLessonList() {
  $("#lesson-list").replaceChildren(...lessons.map(([name, title, description], index) => { const item = button("", "lesson-item" + (state.lesson === name ? " active" : ""), () => loadLesson(name)), content = el("span", "lesson-label"); content.append(el("strong", "", title), el("small", "", description)); item.append(el("span", "lesson-index", String(index + 1).padStart(2, "0")), content, icon("chevron", "small")); return item; }));
}
async function loadLesson(name) {
  const changed = state.lesson !== name; state.lesson = name; const epoch = ++state.lessonEpoch; renderLessonList(); const position = lessons.findIndex(item => item[0] === name); $("#lesson-number").textContent = position >= 0 ? "CHAPTER " + String(position + 1).padStart(2, "0") : "PROJECT GUIDE";
  const host = $("#lesson-content"); host.replaceChildren(el("p", "loading-state", "正在加载学习内容…"));
  if (changed && state.view === "learning") { host.scrollTop = 0; const card = $(".lesson-content"); if (card) card.scrollTop = 0; resetPageScroll(); }
  try { const result = await api("/api/docs/" + encodeURIComponent(name)); if (epoch === state.lessonEpoch) host.replaceChildren(markdown(result.content)); }
  catch (error) { if (epoch === state.lessonEpoch) host.replaceChildren(empty("章节暂时无法加载", error.message), button("重新加载", "button secondary", () => loadLesson(name))); }
}
function renderWorkflow(job) {
  const host = $("#workflow-result"); show(host, true); if (job.status !== "completed") { host.replaceChildren(empty("工作流未完成", job.error || "任务已取消")); return; }
  const result = job.result || {}, nodeStatuses = result.statuses || {}, outputs = result.outputs || {}, errors = result.errors || {};
  $$("[data-node]").forEach(element => { const key = element.dataset.node === "summary" ? "report" : element.dataset.node, status = nodeStatuses[key] || "pending"; element.classList.remove("running", "success", "failed", "skipped"); element.classList.add(status); $(".node-status", element).textContent = statuses[status] || status; });
  const children = [el("h4", "", Object.keys(errors).length ? "工作流结束 · 部分节点失败" : "工作流已完成")];
  Object.entries(outputs).forEach(([name, value]) => {
    const card = el("div", "workflow-output"), heading = el("div", "section-heading");
    heading.append(el("strong", "", ({calculate: "计算结果", research: "资料检索", report: "结果汇总"}[name] || name) + " · " + name), badge(statuses[nodeStatuses[name]] || nodeStatuses[name] || "已返回", nodeStatuses[name] === "success" ? "success" : "subtle"));
    let summary = "已返回节点结果。", parsed = value;
    if (typeof value === "string" && value.startsWith("离线 Demo 工具执行结果：\n")) { try { parsed = JSON.parse(value.slice("离线 Demo 工具执行结果：\n".length)); } catch (_) { parsed = value; } }
    if (parsed && typeof parsed === "object" && typeof parsed.ok === "boolean") parsed = parsed.ok ? parsed.value : "工具未执行成功：" + parsed.error;
    if (name === "report" && outputs.calculate !== undefined && outputs.research !== undefined) summary = "已合并计算与检索两个上游节点的结果，完成本次协作。";
    else if (typeof parsed === "number") summary = "计算结果：" + parsed;
    else if (Array.isArray(parsed)) { const sources = [...new Set(parsed.map(item => item && item.source).filter(Boolean))]; summary = "返回 " + parsed.length + " 个知识片段" + (sources.length ? "，来自 " + sources.length + " 份资料。" : "。可导入知识库后再次运行检索。"); }
    else if (typeof parsed === "string") { const compact = parsed.replace(/\s+/g, " ").trim(); summary = compact.length > 140 ? compact.slice(0, 140) + "…" : compact; }
    else if (parsed && typeof parsed === "object") summary = "返回 " + Object.keys(parsed).length + " 项结构化数据，展开查看完整结果。";
    card.append(heading, el("p", "muted", summary), details("查看完整节点输出", value, "workflow-output-" + name)); children.push(card);
  });
  if (Object.keys(errors).length) children.push(details("节点错误", errors, "workflow-errors", true)); host.replaceChildren(...children);
}
function renderEvaluation(job) {
  const host = $("#evaluation-result"); if (job.status !== "completed") { host.replaceChildren(empty("评测未完成", job.error || "任务已取消")); return; }
  const result = job.result || {}, header = el("div", "evaluation-summary"); header.append(el("strong", "evaluation-rate", Math.round((result.pass_rate || 0) * 100) + "%"), el("span", "", "通过率 · " + (result.passed || 0) + " / " + (result.total || 0) + " 个用例"));
  const rows = (result.cases || []).map(item => { const row = el("div", "evaluation-row"), information = el("div"), checks = el("div", "evaluation-checks"); information.append(el("strong", "", item.name)); Object.entries(item.checks || {}).forEach(([key, passed]) => checks.append(badge(({status: "状态", content: "回答", tools: "工具轨迹"}[key] || key) + (passed ? " ✓" : " ×"), passed ? "success" : "danger"))); information.append(checks, details("查看评测输出", {output: item.output, actual_tools: item.actual_tools}, "eval-" + item.name)); row.append(icon(item.passed ? "check" : "info"), information, badge(item.passed ? "通过" : "失败", item.passed ? "success" : "danger")); return row; }); host.replaceChildren(header, ...rows);
}
async function startExperiment(kind) {
  const target = $(kind === "workflow" ? "#run-workflow" : "#run-evaluation"); busy(target, true);
  try { const result = await api(kind === "workflow" ? "/api/workflow" : "/api/evaluate", {}); trackJob(result.job_id, kind); if (kind === "workflow") { show($("#workflow-result"), true); $("#workflow-result").replaceChildren(el("p", "loading-state", "工作流正在运行，完成后显示各节点真实状态与结果…")); $$("[data-node]").forEach(element => { element.classList.remove("success", "failed", "skipped"); $(".node-status", element).textContent = "已提交"; }); } else $("#evaluation-result").replaceChildren(el("p", "loading-state", "正在运行离线回归用例…")); }
  catch (error) { busy(target, false); throw error; }
  poll();
}
async function saveSettings(event) {
  event.preventDefault(); const payload = {provider: $("#config-provider").value, model: $("#config-model").value.trim(), base_url: $("#config-base-url").value.trim()}, key = $("#config-api-key"); if (key.value.trim()) payload.api_key = key.value.trim(); busy($("#save-settings"), true);
  try { const result = await api("/api/config", payload); applyConfig(result.config); show($("#connection-result"), false); toast("模型配置已保存。切换模型后请新建对话。"); }
  finally { key.value = ""; delete payload.api_key; busy($("#save-settings"), false); }
}
async function testConnection() {
  busy($("#test-connection"), true); const host = $("#connection-result"); host.className = "connection-result"; host.textContent = "正在测试已保存的模型连接…";
  try { const result = await api("/api/connection-test", {}); trackJob(result.job_id, "connection"); }
  catch (error) { host.className = "connection-result error"; host.textContent = error.message; busy($("#test-connection"), false); throw error; }
  poll();
}
function useExample(prompt) { navigate("chat"); if (running() || pending() || recovery()) { toast("请先完成当前会话的运行或审批，也可以新建对话。", "error"); return; } $("#prompt").value = prompt; saveDraft(); renderChat(); $("#prompt").focus(); }
async function bootstrap() {
  busy($("#retry-bootstrap"), true);
  try { const result = await api("/api/bootstrap"); state.csrf = result.csrf_token; state.tools = result.tools || []; state.ready = true; applyConfig(result.config); renderTools(); renderLessonList(); if (result.stats) $("#knowledge-count").textContent = result.stats.documents || 0; show($("#boot-error"), false); if (result.warning) { toast(result.warning, "error"); $("#settings-saved-status").textContent = "配置需修复"; } await refreshSessions(); renderChat(); renderInspector(); if (state.view === "knowledge") await refreshKnowledge(); if (state.view === "learning") loadLesson(state.lesson); }
  catch (error) { state.ready = false; $("#boot-error-message").textContent = error.message; show($("#boot-error"), true); renderChat(); }
  finally { busy($("#retry-bootstrap"), false); }
}

// Install listeners once: polling never replaces the textarea or settings inputs.
$$("[data-nav]").forEach(element => element.addEventListener("click", () => navigate(element.dataset.nav)));
$$("[data-prompt]").forEach(element => element.addEventListener("click", () => useExample(element.dataset.prompt)));
$$("[data-doc]").forEach(element => element.addEventListener("click", () => { navigate("learning"); loadLesson(element.dataset.doc); }));
$("#new-chat").addEventListener("click", newSession);
$("#model-settings").addEventListener("click", () => navigate("settings"));
$("#banner-settings").addEventListener("click", () => navigate("settings"));
$("#toggle-sidebar").addEventListener("click", () => document.body.classList.toggle("sidebar-open"));
$("#refresh-sessions").addEventListener("click", guarded(refreshSessions));
$("#refresh-knowledge").addEventListener("click", guarded(refreshKnowledge));
$("#retry-bootstrap").addEventListener("click", bootstrap);
$("#chat-form").addEventListener("submit", guarded(async event => { event.preventDefault(); await sendPrompt(); }));
$("#prompt").addEventListener("input", () => { saveDraft(); $("#send-message").disabled = !state.ready || running() || pending() || recovery() || !$("#prompt").value.trim(); });
$("#prompt").addEventListener("keydown", event => { if (event.key === "Enter" && !event.shiftKey && !event.isComposing) { event.preventDefault(); $("#chat-form").requestSubmit(); } });
$("#cancel-run").addEventListener("click", guarded(async () => { const job = currentJob(); if (!job || state.cancelling) return; state.cancelling = true; renderChat(); try { await api("/api/cancel", {job_id: job.id}); toast("已请求停止当前任务。"); } finally { state.cancelling = false; renderChat(); } poll(); }));
$$("[data-inspect]").forEach(element => element.addEventListener("click", () => { state.inspector = element.dataset.inspect; $$("[data-inspect]").forEach(tab => { const active = tab === element; tab.classList.toggle("active", active); tab.setAttribute("aria-selected", String(active)); }); renderInspector(); }));
$("#knowledge-files").addEventListener("change", guarded(importFiles));
$("#knowledge-search-form").addEventListener("submit", guarded(searchKnowledge));
$("#import-example").addEventListener("click", guarded(async event => { const target = event.currentTarget; busy(target, true); try { await api("/api/knowledge/example", {}); await refreshKnowledge(); toast("示例知识已导入，试试搜索 Agent 记忆。"); } finally { busy(target, false); } }));
$("#settings-form").addEventListener("submit", guarded(saveSettings));
$("#test-connection").addEventListener("click", guarded(testConnection));
$("#run-workflow").addEventListener("click", guarded(() => startExperiment("workflow")));
$("#run-evaluation").addEventListener("click", guarded(() => startExperiment("evaluate")));
window.addEventListener("hashchange", () => navigate(location.hash.slice(1), false));
window.addEventListener("keydown", event => { if (event.key.toLowerCase() === "n" && (event.ctrlKey || event.metaKey) && event.altKey) { event.preventDefault(); newSession(); } });
navigate(location.hash.slice(1) || "chat", false); renderChat(); renderInspector(); bootstrap(); setInterval(poll, 500);
