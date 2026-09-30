/* 原始 app.js 状态函数回归：仅模拟 DOM、HTTP 和视图绘制，不重写业务逻辑。
 * 运行：node --test tests/web-state.test.cjs；无 npm 依赖、无真实网络请求。 */
"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const source = fs.readFileSync(path.join(__dirname, "../agentlab/web/app.js"), "utf8");
const listenerMarker = "// Install listeners once:";
const listenerOffset = source.indexOf(listenerMarker);
assert.notEqual(listenerOffset, -1, "需要保留原始事件监听器安装段以测试真实输入事件");

function element() {
  const listeners = new Map(), classes = new Set();
  return {
    value: "", textContent: "", disabled: false,
    classList: {
      toggle(name, enabled) { if (enabled === undefined) enabled = !classes.has(name); enabled ? classes.add(name) : classes.delete(name); },
      add(...names) { names.forEach(name => classes.add(name)); },
      remove(...names) { names.forEach(name => classes.delete(name)); },
    },
    setAttribute() {}, removeAttribute() {}, focus() {},
    addEventListener(kind, callback) { if (!listeners.has(kind)) listeners.set(kind, []); listeners.get(kind).push(callback); },
    dispatch(kind) { for (const callback of listeners.get(kind) || []) callback({target: this, currentTarget: this, preventDefault() {}}); },
  };
}

function harness(respond) {
  const nodes = new Map(), requests = [], toasts = [], polls = [];
  const node = selector => { if (!nodes.has(selector)) nodes.set(selector, element()); return nodes.get(selector); };
  const context = vm.createContext({
    console, setTimeout, clearTimeout, AbortController,
    setInterval() { return 0; },
    document: {querySelector: node, querySelectorAll: () => [], body: element()},
    location: {hash: "#chat"}, window: {addEventListener() {}},
    recordToast(message, tone) { toasts.push({message, tone}); },
    recordedPolls: polls,
    async fetch(url, options = {}) {
      const request = {path: url, method: options.method || "GET", body: options.body ? JSON.parse(options.body) : null};
      requests.push(request);
      const response = await respond(request), status = response.status || 200;
      return {status, ok: status >= 200 && status < 300, json: async () => response.payload};
    },
  });
  vm.runInContext(source.slice(0, listenerOffset), context, {filename: "app.js"});
  // 被测函数 api/selectSession/sendPrompt/refreshCurrent/poll/finishJob 保持原样。
  // 禁用整页渲染和启动请求；保留原输入监听器，显式驱动原 poll 并等待其完成。
  vm.runInContext(`
    renderChat = () => {}; renderInspector = () => {}; renderSessions = () => {};
    updateJobButtons = () => {}; navigate = () => {}; bootstrap = async () => {};
    toast = (message, tone) => recordToast(message, tone);
    const originalPollForTest = poll;
    poll = (...args) => { const promise = originalPollForTest(...args); recordedPolls.push(promise); return promise; };
  `, context);
  vm.runInContext(source.slice(listenerOffset), context, {filename: "app-listeners.js"});
  vm.runInContext('state.ready = true; state.online = true; state.csrf = "test-only-token";', context);
  return {
    node, requests, toasts,
    run(expression) { return vm.runInContext(expression, context); },
    type(text) { node("#prompt").value = text; node("#prompt").dispatch("input"); },
    async settle() { while (polls.length) await Promise.all(polls.splice(0)); },
  };
}

function session(id, extra = {}) {
  return {session_id: id, status: "completed", active: false, active_job_id: null,
    messages: [], pending: [], events: [], memory: [], ...extra};
}

test("切换会话隔离草稿，再切回保留各自内容", async () => {
  const h = harness(request => {
    if (request.path === "/api/sessions/A") return {payload: session("A")};
    if (request.path === "/api/sessions/B") return {payload: session("B")};
    throw new Error("未预期的请求：" + request.path);
  });
  await h.run('selectSession("A")');
  h.type("只属于 A 的待发送消息");
  await h.run('selectSession("B")');
  assert.equal(h.node("#prompt").value, "", "A 的草稿不可无提示带入 B");
  assert.equal(h.run("state.current.session_id"), "B");
  h.type("只属于 B 的待发送消息");
  await h.run('selectSession("A")');
  assert.equal(h.node("#prompt").value, "只属于 A 的待发送消息");
  await h.run('selectSession("B")');
  assert.equal(h.node("#prompt").value, "只属于 B 的待发送消息");
  h.run("newSession()");
  assert.equal(h.node("#prompt").value, "");
  await h.run('selectSession("A")');
  assert.equal(h.node("#prompt").value, "只属于 A 的待发送消息");
  assert.ok(h.requests.every(request => request.method === "GET"), "编辑和切换草稿不会提交运行");
});

test("新任务在创建会话前失败，恢复输入并退出不存在的会话", async () => {
  let jobStatus = "running";
  const h = harness(request => {
    if (request.path === "/api/run") return {payload: {job_id: "job-new", session_id: "not-created"}};
    if (request.path === "/api/sessions/not-created") return {status: 404, payload: {error: "未找到会话"}};
    if (request.path === "/api/jobs/job-new") return {payload: {status: jobStatus, result: null, error: jobStatus === "failed" ? "API Key 尚未配置" : null}};
    if (request.path === "/api/sessions") return {payload: {sessions: []}};
    throw new Error("未预期的请求：" + request.path);
  });
  const prompt = "请计算 6 × 7，并保留这条原始问题";
  h.type(prompt);
  await h.run("sendPrompt()");
  await h.settle();
  assert.equal(h.run("running()"), true);
  jobStatus = "failed";
  await h.run("poll()");
  await h.settle();
  assert.equal(h.node("#prompt").value, prompt, "失败后用户无需重新输入整段任务");
  assert.equal(h.run("state.currentId"), null, "不可把不存在的会话继续当成选中会话");
  assert.equal(h.run("state.current"), null);
  assert.equal(h.run("running()"), false);
  assert.ok(h.toasts.some(value => value.message.includes("API Key 尚未配置")), "显示原始启动失败原因");
  const missingReads = h.requests.filter(request => request.path === "/api/sessions/not-created").length;
  const sessionListReads = h.requests.filter(request => request.path === "/api/sessions").length;
  h.run("state.lastRefresh = 0");
  await h.run("poll()");
  await h.settle();
  assert.equal(h.requests.filter(request => request.path === "/api/sessions/not-created").length, missingReads);
  assert.ok(h.requests.filter(request => request.path === "/api/sessions").length > sessionListReads, "失败不能永久阻断列表刷新");
});

test("已受理任务的临时会话 404 只等待，不误报失败或丢失消息", async () => {
  let saved = false, completed = false;
  const prompt = "等待后台保存会话的测试任务";
  const h = harness(request => {
    if (request.path === "/api/run") return {payload: {job_id: "job-pending", session_id: "pending-session"}};
    if (request.path === "/api/jobs/job-pending") return {payload: {
      status: completed ? "completed" : "running", error: null,
      result: completed ? {status: "completed", output: "任务完成"} : null,
    }};
    if (request.path === "/api/sessions/pending-session") return saved ? {payload: session("pending-session", {
      status: completed ? "completed" : "running", active: !completed,
      active_job_id: completed ? null : "job-pending", output: completed ? "任务完成" : "",
      messages: [{role: "user", content: prompt}],
    })} : {status: 404, payload: {error: "未找到会话"}};
    if (request.path === "/api/sessions") return {payload: {sessions: saved ? [{session_id: "pending-session", status: completed ? "completed" : "running"}] : []}};
    throw new Error("未预期的请求：" + request.path);
  });
  h.type(prompt);
  await assert.doesNotReject(() => h.run("sendPrompt()"));
  await h.settle();
  await assert.doesNotReject(() => h.run("poll()"));
  await h.settle();
  assert.equal(h.run("state.currentId"), "pending-session");
  assert.equal(h.run("state.current.messages[0].content"), prompt);
  assert.equal(h.run("running()"), true);
  assert.deepEqual(h.toasts, [], "排队阶段不存在会话是可等待状态");
  saved = true;
  completed = true;
  await h.run("poll()");
  await h.settle();
  assert.equal(h.run("state.current.status"), "completed");
  assert.equal(h.run("state.current.output"), "任务完成");
  assert.equal(h.run("running()"), false);
  assert.deepEqual(h.toasts, []);
  assert.equal(h.requests.filter(request => request.path === "/api/run").length, 1, "轮询不重复提交任务");
});
