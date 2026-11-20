"use strict";
// TUM 学习小助理 — local web UI. All Moodle/Artemis content is untrusted data:
// it is only ever inserted as text (never innerHTML), and links are limited to http(s).

// ---------------------------------------------------------------- token & api

const TOKEN_KEY = "moodle-ui-token";
(function takeToken() {
  const match = location.hash.match(/^#t=([\w-]+)/);
  if (match) {
    try { sessionStorage.setItem(TOKEN_KEY, match[1]); } catch (e) { window.__token = match[1]; }
    history.replaceState(null, "", location.pathname + "#/");
  }
})();
// Opened from another device through `tailscale serve`: there is no launch token; the server
// checks the Tailscale login instead, so any non-empty value lets the page run.
const VIA_TAILNET = !["127.0.0.1", "localhost"].includes(location.hostname);
function token() {
  let stored = "";
  try { stored = sessionStorage.getItem(TOKEN_KEY) || ""; } catch (e) { /* in-memory fallback */ }
  return stored || window.__token || (VIA_TAILNET ? "tailnet" : "");
}

// Hold one open request while this page is open. Started from the desktop shortcut, the
// helper quits about 20 s after the last such connection ends, i.e. its last tab closed (a reload reconnects sooner).
(function stayAlive() {
  let wait = 1000;
  const connect = () => {
    if (!token()) return;
    fetch("/api/alive", { headers: { "X-UI-Token": token() }, cache: "no-store" })
      .then(response => {
        if (response.status === 403) return "stop";  // an old page from an earlier start
        if (!response.ok || !response.body) throw new Error("alive");
        wait = 1000;
        const reader = response.body.getReader();
        const drain = () => reader.read().then(({ done }) => (done ? null : drain()));
        return drain();
      })
      .catch(() => null)
      .then(result => {
        if (result === "stop") return;
        setTimeout(connect, wait);
        wait = Math.min(wait * 2, 30000);
      });
  };
  connect();
})();

// Pick up updates without closing anything: the server reports a new "boot" after it restarted
// itself for changed program files, and a new "web" when the page files changed on disk.
// An older helper has no /api/version (404) and is simply left alone.
function safeToReload() {
  // TODO(human): return false while a reload would lose something the user is doing right now.
  return true;
}

(function watchVersion() {
  let known = null;
  const check = () => {
    if (!token() || document.hidden) return;
    fetch("/api/version", { headers: { "X-UI-Token": token() }, cache: "no-store" })
      .then(response => (response.ok ? response.json() : null))
      .then(info => {
        if (!info || typeof info.boot !== "string" || typeof info.web !== "string") return;
        const seen = info.boot + "/" + info.web;
        if (known === null) known = seen;
        else if (seen !== known && safeToReload()) location.reload();  // not safe: try again next time
      })
      .catch(() => null);  // restarting: the next check will see the new copy
  };
  setInterval(check, 3000);
  document.addEventListener("visibilitychange", check);
  check();
})();

class ApiError extends Error {}
let inflight = 0;
let statusText = "";

async function api(path, body) {
  inflight++;
  updateStatus();
  try {
    const options = { headers: { "X-UI-Token": token() } };
    if (body !== undefined) {
      options.method = "POST";
      options.headers["Content-Type"] = "application/json";
      options.body = JSON.stringify(body);
    }
    let response;
    try {
      response = await fetch("/api" + path, options);
    } catch (e) {
      throw new ApiError("连不上小助理：如果程序刚更新过，它正在重启，几秒后页面会自动刷新；否则它已经退出了（关掉它的标签页约 20 秒后会自动退出），请重新点击桌面上的“TUM 学习小助理”。");
    }
    if (response.status === 403) {
      throw new ApiError("这是之前打开的旧页面，已经不能用了。请关闭它，重新点击桌面上的“TUM 学习小助理”（每次启动的地址都不同）。");
    }
    // The page files are read from disk, the server code only at start: a helper started before
    // an update serves the new page but does not know its new requests yet.
    const outdated = "小助理是在程序更新之前启动的，还不认识这个功能。稍等几秒，它会自动重启并刷新页面；如果一直这样，请关掉小助理的所有标签页，等 20 秒左右，再点桌面上的“TUM 学习小助理”重新打开。";
    if (response.status === 404 || response.status === 405) throw new ApiError(outdated);
    let data;
    try { data = await response.json(); } catch (e) { throw new ApiError(outdated); }
    if (data && typeof data === "object" && !Array.isArray(data) && data.error) {
      let message = String(data.error);
      if (/setup/i.test(message)) message += "\n→ 请先在命令行运行：python -I tum_moodle_auto.py setup，保存 TUM 账号和密码。";
      throw new ApiError(message);
    }
    return data;
  } finally {
    inflight--;
    updateStatus();
  }
}

// A small spinner in the corner while requests run; it only appears after 300 ms so quick
// local reads (plan, course map) do not flash it. The text is for screen readers and the tooltip.
let busyTimer = null;
function updateStatus() {
  const busy = $("#status");
  const text = statusText || "正在读取（第一次使用会先自动登录 TUM，可能需要十几秒）";
  $("#status-text").textContent = text;
  busy.title = text;
  if (inflight > 0) {
    if (busy.hidden && !busyTimer) busyTimer = setTimeout(() => { busyTimer = null; if (inflight > 0) busy.hidden = false; }, 300);
  } else {
    clearTimeout(busyTimer);
    busyTimer = null;
    busy.hidden = true;
  }
}
function spinner(label) {
  return h("span", { class: "spinner small", role: "img", "aria-label": label, title: label });
}

// ---------------------------------------------------------------- dom helpers

function $(selector, root = document) { return root.querySelector(selector); }

function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "class") el.className = value;
    else if (key.startsWith("on")) el.addEventListener(key.slice(2), value);
    else if (key === "href") { const safe = safeUrl(value); if (safe) el.setAttribute("href", safe); }
    else el.setAttribute(key, value === true ? "" : value);
  }
  append(el, children);
  return el;
}
function append(el, children) {
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false) continue;
    el.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return el;
}
function safeUrl(value) {
  if (typeof value !== "string") return null;
  if (value.startsWith("#")) return value;
  try {
    const url = new URL(value, "https://www.moodle.tum.de");
    return url.protocol === "https:" || url.protocol === "http:" ? url.href : null;
  } catch (e) { return null; }
}
function external(url, text) {
  const safe = safeUrl(url);
  return safe ? h("a", { href: safe, target: "_blank", rel: "noopener noreferrer" }, text) : null;
}
function button(text, onclick, cls = "btn") { return h("button", { type: "button", class: cls, onclick }, text); }
function chip(text, kind = "") { return h("span", { class: "chip " + kind }, text); }
function notice(text, kind = "error") { return h("div", { class: "notice " + kind }, text); }
function empty(text) { return h("div", { class: "empty" }, text); }
function show(...children) {
  const main = $("#main");
  main.replaceChildren();
  append(main, children);
}

let toastTimer;
function toast(text, error = false) {
  const el = $("#toast");
  el.textContent = text;
  el.className = "toast" + (error ? " error" : "");
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.hidden = true; }, error ? 8000 : 4000);
}

// Render an async section: placeholder → content, or a readable error.
async function load(container, work) {
  container.replaceChildren(h("div", { class: "empty" }, "正在读取…"));
  try {
    const result = await work();
    container.replaceChildren();
    append(container, [result]);
  } catch (e) {
    container.replaceChildren(notice(e.message || String(e)));
  }
}

function confirmDialog(title, body, okText = "确定") {
  const dialog = $("#dialog");
  $("#dialog-title").textContent = title;
  const holder = $("#dialog-body");
  holder.replaceChildren();
  append(holder, [body]);
  $("#dialog-ok").textContent = okText;
  dialog.returnValue = "";
  dialog.showModal();
  return new Promise(resolve => {
    dialog.addEventListener("close", () => resolve(dialog.returnValue === "ok"), { once: true });
  });
}

// ---------------------------------------------------------------- theme
// Light and dark follow the system until the user picks one; the choice is a per-browser convenience.

const THEME_KEY = "moodle-ui-theme";
const darkQuery = window.matchMedia("(prefers-color-scheme: dark)");
let themeChoice = null;
try { themeChoice = localStorage.getItem(THEME_KEY); } catch (e) { /* private window: system theme */ }
function applyTheme() {
  if (themeChoice === "light" || themeChoice === "dark") document.documentElement.dataset.theme = themeChoice;
  else delete document.documentElement.dataset.theme;
  const effective = document.documentElement.dataset.theme || (darkQuery.matches ? "dark" : "light");
  document.querySelectorAll(".mode button").forEach(b => b.setAttribute("aria-pressed", String(b.dataset.mode === effective)));
}
document.querySelectorAll(".mode button").forEach(b => b.addEventListener("click", () => {
  themeChoice = b.dataset.mode;
  try { localStorage.setItem(THEME_KEY, themeChoice); } catch (e) { /* kept for this page only */ }
  applyTheme();
}));
darkQuery.addEventListener("change", applyTheme);
applyTheme();

// ---------------------------------------------------------------- formatting

const WEEKDAYS = ["周日", "周一", "周二", "周三", "周四", "周五", "周六"];
function dayLabel(date) {
  const today = new Date(); today.setHours(0, 0, 0, 0);
  const day = new Date(date); day.setHours(0, 0, 0, 0);
  const diff = Math.round((day - today) / 86400000);
  const base = `${date.getMonth() + 1} 月 ${date.getDate()} 日 ${WEEKDAYS[date.getDay()]}`;
  return diff === 0 ? `今天 · ${base}` : diff === 1 ? `明天 · ${base}` : base;
}
function timeLabel(date) {
  return `${String(date.getHours()).padStart(2, "0")}:${String(date.getMinutes()).padStart(2, "0")}`;
}
function remaining(date) {
  const hours = (date - new Date()) / 3600000;
  if (hours < 0) return "已过期";
  if (hours < 1) return `还剩 ${Math.max(1, Math.round(hours * 60))} 分钟`;
  if (hours < 48) return `还剩 ${Math.round(hours)} 小时`;
  return `还剩 ${Math.round(hours / 24)} 天`;
}
function dateText(value) {
  if (!value) return "";
  const date = new Date(value);
  if (isNaN(date)) return String(value);
  return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, "0")}-${String(date.getDate()).padStart(2, "0")}`;
}

const TYPES = {
  resource: "文件", folder: "文件夹", assign: "作业", forum: "论坛", page: "页面", url: "链接", quiz: "测验",
  label: "说明", lti: "外部工具", choice: "投票", feedback: "问卷", glossary: "词汇表", book: "电子书",
  h5pactivity: "H5P", workshop: "互评", lesson: "课时", wiki: "Wiki", scorm: "SCORM", data: "数据库",
  bigbluebuttonbn: "直播", zoom: "Zoom", choicegroup: "分组", course: "课程", attendance: "考勤",
};
function typeName(type) { return TYPES[type] || type || "其他"; }

// ---------------------------------------------------------------- shared data

const cache = { courses: {}, map: null };
async function courseMap(refresh = false) {
  if (!cache.map || refresh) cache.map = await api("/map");
  return cache.map;
}
async function courseList(which) {
  if (!cache.courses[which]) cache.courses[which] = await api("/courses?which=" + which);
  return cache.courses[which];
}
async function findCourse(id) {
  for (const list of Object.values(cache.courses)) {
    const hit = list.find(c => c.id === id);
    if (hit) return hit;
  }
  return (await courseList("all")).find(c => c.id === id) || null;
}
// Course names from the course map (English) instead of Moodle's own, which are partly German.
async function titlesById() {
  try {
    const map = await courseMap();
    return new Map(map.entries.filter(e => e.moodle_id).map(e => [e.moodle_id, e.title]));
  } catch (e) { return new Map(); }
}
async function titlesByMoodleName() {
  const byId = await titlesById();
  try {
    return new Map((await courseList("all")).filter(c => byId.has(c.id)).map(c => [c.name, byId.get(c.id)]));
  } catch (e) { return new Map(); }
}

async function defaultInbox(courseId, key = "moodle_id") {
  try {
    const map = await courseMap();
    const entry = courseId ? map.entries.find(e => e[key] === courseId) : null;
    return entry ? entry.inbox : map.inbox;
  } catch (e) { return ""; }
}

// ---------------------------------------------------------------- downloads & reader

async function downloadFile(file, courseId, artemis = false) {
  const dest = await defaultInbox(courseId, artemis ? "artemis_id" : "moodle_id");
  const input = h("input", { class: "path-input", value: dest, "aria-label": "保存到文件夹" });
  const ok = await confirmDialog("下载文件",
    [h("p", null, "文件：", h("strong", null, file.name || file.filename || "（未命名）")),
     h("p", { class: "muted small" }, "保存到这个文件夹（不存在会自动新建；同名文件不会被覆盖）："), input],
    "下载");
  if (!ok) return;
  try {
    const result = await api("/download", { url: file.url, dest_dir: input.value.trim(), confirm: true });
    const saved = Array.isArray(result) ? result : [result];
    toast(`已保存：${saved.join("，")}`);
  } catch (e) { toast(e.message, true); }
}

function openReader(title) {
  $("#reader-title").textContent = title || "阅读";
  $("#reader").hidden = false;
  return $("#reader-body");
}
$("#reader-close")?.addEventListener("click", () => { $("#reader").hidden = true; });
document.addEventListener("keydown", e => { if (e.key === "Escape") $("#reader").hidden = true; });

function readPage(url, title, courseId) {
  const body = openReader(title);
  load(body, async () => {
    const page = await api("/page", { url });
    $("#reader-title").textContent = page.title || title || "阅读";
    const parts = [h("p", null, external(url, "在 Moodle 中打开 ↗")), renderMarkdown(page.text || "")];
    const links = (page.links || []).filter(l => safeUrl(l.url));
    if (links.length) {
      parts.push(h("div", { class: "links" }, h("h3", null, "页面里的文件和活动"),
        links.map(link => h("div", { class: "file" },
          h("span", { class: "fname" }, link.text || link.url),
          link.url.includes("pluginfile.php")
            ? button("下载", () => downloadFile({ name: link.text, url: link.url }, courseId), "btn tiny")
            : button("阅读", () => readPage(link.url, link.text, courseId), "btn tiny"),
          external(link.url, "↗")))));
    }
    return parts;
  });
}

// Minimal Markdown for read_page output: headings, lists, tables, paragraphs.
function renderMarkdown(text) {
  const root = h("div", { class: "md" });
  const lines = text.split(/\r?\n/);
  let paragraph = [];
  let list = null;
  const flush = () => {
    if (paragraph.length) root.append(h("p", null, paragraph.join(" ")));
    paragraph = [];
  };
  const splitRow = line => line.trim().replace(/^\|/, "").replace(/\|$/, "")
    .split(/(?<!\\)\|/).map(c => c.trim().replace(/\\\|/g, "|"));
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    if (!line.trim()) { flush(); list = null; continue; }
    if (line.trim().startsWith("|")) {
      flush(); list = null;
      const rows = [];
      while (i < lines.length && lines[i].trim().startsWith("|")) rows.push(splitRow(lines[i++]));
      i--;
      const body = rows.filter((row, index) => !(index === 1 && row.every(c => /^:?-{3,}:?$/.test(c))));
      const [head, ...rest] = body;
      root.append(h("div", { class: "table-wrap" }, h("table", null,
        h("thead", null, h("tr", null, head.map(c => h("th", null, c)))),
        h("tbody", null, rest.map(row => h("tr", null, row.map(c => h("td", null, c))))))));
      continue;
    }
    const heading = line.match(/^(#{1,6})\s+(.*)$/);
    if (heading) {
      flush(); list = null;
      root.append(h("h" + Math.min(6, heading[1].length + 1), null, heading[2]));
      continue;
    }
    const item = line.match(/^(\s*)(?:[-*+]|\d+[.)])\s+(.*)$/);
    if (item) {
      flush();
      if (!list) { list = h("ul"); root.append(list); }
      const inner = item[2].match(/^#{1,6}\s+(.*)$/);
      const li = h("li", null, inner ? h("strong", null, inner[1]) : item[2]);
      li.style.marginLeft = (Math.floor(item[1].length / 2) * 1.2) + "rem";
      list.append(li);
      continue;
    }
    list = null;
    paragraph.push(line.trim());
  }
  flush();
  return root;
}

// ---------------------------------------------------------------- page: study plan
// Plans come from the planner folder (written by the nightly task); ticks go back there.

const STATE_LABELS = [["done", "完成"], ["half", "一半"], ["no", "没做"]];
const INVENTORY_LABELS = [["done", "完成"], ["half", "不够好"], ["no", "没做"]];
const COURSE_CLASSES = [["INHN0001", "c-iti"], ["INHN0002", "c-iti"], ["INHN0004", "c-ds"], ["INHN0011", "c-db"],
  ["INHN0014", "c-an"], ["INHN0003", "c-ca"], ["MGTHN0133", "c-en"], ["CITHN2014", "c-ai"], ["research", "c-rs"], ["extracurricular", "c-ex"]];
function courseClass(key) {
  const hit = COURSE_CLASSES.find(([prefix]) => String(key || "").startsWith(prefix));
  return hit ? hit[1] : "";
}
function isoDate(date) {
  return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, "0")}-${String(date.getDate()).padStart(2, "0")}`;
}
function localDay(text) { return new Date(text + "T12:00:00"); }
function daysBetween(from, to) {
  const utc = text => { const [y, m, d] = text.split("-").map(Number); return Date.UTC(y, m - 1, d); };
  return Math.round((utc(to) - utc(from)) / 86400000);
}
function berlinDate(value = new Date()) {
  const parts = new Intl.DateTimeFormat("en", { timeZone: "Europe/Berlin", year: "numeric", month: "2-digit", day: "2-digit" }).formatToParts(value);
  const part = name => parts.find(p => p.type === name).value;
  return `${part("year")}-${part("month")}-${part("day")}`;
}
function berlinStamp(value) {
  const d = new Date(value);
  return isNaN(d) ? "时间不明" : new Intl.DateTimeFormat("zh-CN", { timeZone: "Europe/Berlin", month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit", hour12: false }).format(d);
}
function shortDate(text) { const d = localDay(text); return `${d.getMonth() + 1}/${d.getDate()}`; }
function planDayLabel(text, today) {
  const d = localDay(text);
  const base = `${d.getMonth() + 1} 月 ${d.getDate()} 日 · ${WEEKDAYS[d.getDay()].replace("周", "星期")}`;
  const diff = daysBetween(today, text);
  return diff === 0 ? `今天 · ${base}` : diff === 1 ? `明天 · ${base}` : diff === -1 ? `昨天 · ${base}` : base;
}
function hoursText(minutes) { return String(Math.round(minutes / 6) / 10); }
function minutesText(minutes) {
  const m = Number(minutes) || 0;
  return m >= 120 && m % 30 === 0 ? `${m / 60} 小时` : `${m} 分钟`;
}
const STATE_SHARE = { done: 1, half: 0.5 };
const FINISHED_STATES = ["done", "dropped"];
function courseAnchor(key) { return "course-" + String(key).replace(/[^\w-]/g, "_"); }
function jumpToCourse(key, name) {
  const target = document.getElementById(courseAnchor(key));
  if (!target) { toast(`所选日期没有${name || "这门课"}的任务。`); return; }
  target.scrollIntoView({ behavior: "smooth", block: "start" });
  target.classList.remove("flash");
  void target.offsetWidth;  // restart the highlight when the same course is clicked again
  target.classList.add("flash");
}

function viewPlan(date) {
  const holder = h("div");
  show(holder);
  statusText = "正在读取计划…";
  load(holder, async () => {
    try {
      return planPage(await api("/plan" + (date ? "?date=" + encodeURIComponent(date) : "")), !date);
    } finally { statusText = ""; }
  });
}

let planToday = berlinDate();
const planOpenMemory = new Map();
function planDisclosure(key, title, ...children) {
  const storageKey = "plan-open:" + key;
  let open = planOpenMemory.get(storageKey) || false;
  try { const stored = sessionStorage.getItem(storageKey); if (stored !== null) open = stored === "1"; } catch (e) { /* in-memory fallback */ }
  const details = h("details", { class: "plan-disclosure", open: open || null }, h("summary", null, title), children);
  details.addEventListener("toggle", () => {
    planOpenMemory.set(storageKey, details.open);
    try { sessionStorage.setItem(storageKey, details.open ? "1" : "0"); } catch (e) { /* in-memory fallback */ }
  });
  return details;
}
function taskAnchor(day, id) { return `task-${day}-${id}`; }
function planVisible(element) {
  if (!element || !element.getClientRects().length) return false;
  for (let parent = element.parentElement; parent; parent = parent.parentElement) {
    if (parent.tagName === "DETAILS" && !parent.open) return false;
  }
  return true;
}
function jumpToTask(day, id) {
  const target = document.getElementById(taskAnchor(day, id));
  if (!target) return;
  for (let parent = target.parentElement; parent; parent = parent.parentElement) if (parent.tagName === "DETAILS") parent.open = true;
  target.scrollIntoView({ behavior: window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "instant" : "smooth", block: "center" });
  target.focus({ preventScroll: true });
}
function planWeek(entries, shown, today) {
  const byDate = new Map(entries.filter(e => !e.inventory).map(e => [e.date, e]));
  const start = localDay(shown);
  start.setDate(start.getDate() - (start.getDay() + 6) % 7);
  return h("nav", { class: "plan-week", "aria-label": "本周计划日期" }, Array.from({ length: 7 }, (_, i) => {
    const date = new Date(start); date.setDate(date.getDate() + i);
    const iso = isoDate(date), entry = byDate.get(iso);
    const cls = "week-day" + (iso === shown ? " selected" : "") + (iso === today ? " today" : "");
    const children = [h("span", null, WEEKDAYS[date.getDay()]), h("b", null, String(date.getDate())),
      h("small", null, entry ? [`${entry.planned_done ?? entry.done}/${entry.planned_tasks ?? entry.tasks}`, h("span", { class: "week-count-label" }, " 项安排")] : iso === today ? "尚无计划" : "—")];
    return entry || iso === today ? h("a", { class: cls, href: iso === today ? "#/" : "#/plan/" + iso,
      "aria-current": iso === shown ? "date" : null, "aria-label": planDayLabel(iso, today) }, children) : h("span", { class: cls + " unavailable" }, children);
  }));
}

function generationBlock(data) {
  const g = data.generation;
  if (!g) return notice("请重新启动小助理，以读取更新后的计划状态。", "info");
  const stateNames = { published: "已发布", running: "正在生成", stale: "长时间未更新，可能中断", failed: "生成未成功", missing: "尚无正式计划" };
  const stageNames = { practice: "每日 Java 练习", publish: "计划发布", calendar: "课表与日历", materials: "资料解读", feedback: "学习反馈" };
  const lastStage = stageNames[g.last_stage] || (/^sync-moodle/.test(g.last_stage) ? "Moodle 资料同步" : /^sync-artemis/.test(g.last_stage) ? "Artemis 资料同步" : "资料与计划准备");
  const details = [h("p", null, `生成目标：${g.target_date} · ${stateNames[g.state] || "状态不明"}`),
    g.last_activity_at ? h("p", null, "最后更新：", berlinStamp(g.last_activity_at), "（柏林时间）") : null,
    g.last_stage ? h("p", null, "最后完成阶段：", lastStage, g.last_stage_at ? " · " + berlinStamp(g.last_stage_at) : "") : null,
    g.draft_available && g.state !== "published" ? h("p", null, "已有草稿，尚未发布。") : null,
    g.message ? h("p", null, g.message) : null];
  const material = data.materials_status;
  if (material) details.push(h("p", null, material.error || `待解读或待安排资料 ${material.pending || 0} 份；需重新核对 ${material.unavailable || 0} 份。`));
  for (const source of ((data.plan || {}).sources || []).filter(s => s && s.ok === false)) {
    details.push(h("p", { class: "generation-error" }, `没读到 ${source.name || "某个来源"}：${source.note || "原因不明"}。这部分可能不完整。`));
  }
  return h("div", { class: "generation " + g.state },
    h("span", { class: "generation-label" }, `${shortDate(g.target_date)} 计划 · ${stateNames[g.state] || "状态不明"}`),
    planDisclosure(`${data.date || data.today}/generation`, "生成详情", details));
}

function planPriority(tasks, preferred = [], finished = () => false) {
  const order = new Map(preferred.map((id, i) => [id, i]));
  const rank = task => task.kind === "deadline" ? 0 : task.kind === "test_prep" ? 1 : 2;
  return tasks.filter(t => t.section !== "research" && !finished(t))
    .map((task, i) => ({ task, i })).sort((a, b) =>
      (order.get(a.task.id) ?? 3) - (order.get(b.task.id) ?? 3) || rank(a.task) - rank(b.task) || a.i - b.i)
    .slice(0, 3).map(({ task }) => task);
}

function upcomingTiming(item, today) {
  const days = item.date ? daysBetween(today, item.date) : NaN;
  return Number.isFinite(days) ? { days, label: shortDate(item.date) + (item.time ? " " + item.time : "") }
    : { days: null, label: item.date_text || "时间待定" };
}

function planPage(data, automatic) {
  const plan = data.plan, today = data.today, day = data.date;
  planToday = today;
  const derived = data.derived || {};
  const currentPlan = { ...plan, ...derived, learning_feedback: data.learning_feedback };
  const refreshPage = async (options = {}) => {
    const route = location.hash, scroll = window.scrollY;
    const anchor = options.anchor && document.getElementById(options.anchor);
    const group = anchor && anchor.closest(".course-group");
    const before = anchor && anchor.getBoundingClientRect().top;
    const groupBefore = group && group.getBoundingClientRect().top;
    const fallback = anchor && [...document.querySelectorAll('.task')].find(el => el !== anchor && planVisible(el) && el.getBoundingClientRect().top > before);
    const fallbackBefore = fallback && fallback.getBoundingClientRect().top;
    const latest = await api("/plan?date=" + encodeURIComponent(day));
    if (location.hash !== route) return;
    show(...planPage(latest, automatic));
    await new Promise(resolve => requestAnimationFrame(() => {
      const next = options.anchor && document.getElementById(options.anchor);
      const nextGroup = group && document.getElementById(group.id);
      const nextFallback = fallback && document.getElementById(fallback.id);
      if (planVisible(next)) window.scrollBy(0, next.getBoundingClientRect().top - before);
      else if (planVisible(nextFallback)) window.scrollBy(0, nextFallback.getBoundingClientRect().top - fallbackBefore);
      else if (nextGroup) window.scrollBy(0, nextGroup.getBoundingClientRect().top - groupBefore);
      else window.scrollTo(0, scroll);
      resolve();
    }));
  };
  const calendar = data.calendar || [];
  const inventoryDates = new Set(calendar.filter(e => e.inventory).map(e => e.date));
  const isInventory = inventoryDates.has(day);
  const daily = data.dates.filter(d => !inventoryDates.has(d));
  const index = day ? daily.indexOf(day) : -1;
  const prev = index > 0 ? daily[index - 1] : null;
  const next = index >= 0 && index < daily.length - 1 ? daily[index + 1] : null;
  const nav = h("div", { class: "day-nav" },
    prev ? button("‹ " + shortDate(prev), () => { location.hash = "#/plan/" + prev; }, "btn tiny") : null,
    !automatic ? button("回到今天", () => { location.hash = "#/"; }, "btn tiny") : null,
    button("刷新计划", () => refreshPage(), "btn tiny"),
    next ? button(shortDate(next) + " ›", () => { location.hash = "#/plan/" + next; }, "btn tiny") : null);
  const parts = [];
  if (data.generation && data.generation.today_missing && day && day < today) {
    parts.push(notice(`今天（${shortDate(today)}）尚无正式计划，正在展示 ${shortDate(day)} 的旧计划。`, "info"));
  }
  parts.push(h("div", { class: "plan-top" },
    h("div", { class: "plan-date" }, "正在查看：", isInventory ? "开学盘点" : planDayLabel(day || today, today)), nav),
    h("h1", null, (plan && plan.title) || (day === today ? "今天的计划" : day ? shortDate(day) + " 的计划" : "今日计划")),
    generationBlock(data), planWeek(calendar, day || today, today),
    planDisclosure(`${day || today}/calendar`, "展开月历", planCalendar(calendar, day || today, today)));
  if (!plan) {
    parts.push(notice(data.error || "这一天还没有正式计划。每晚的计划发布后会显示在这里。", "info"));
    return parts;
  }
  if (plan.generated_at) parts.push(h("p", { class: "plan-stamp muted small" }, "计划生成于 ", berlinStamp(plan.generated_at), "（柏林时间）"));
  if (plan.revision && plan.revision.at) parts.push(h("p", { class: "plan-stamp muted small" },
    "核对更新于 ", berlinStamp(plan.revision.at), "（柏林时间） · ", plan.revision.reason));
  if (day < today) parts.push(h("p", { class: "historical-note" }, `以下是 ${shortDate(day)} 的当日记录，正文中的“今天”“明天”按当时日期理解。`));
  const renderNotice = item => {
    const text = typeof item === "string" ? item : item && item.text;
    return text ? h("div", { class: "plan-notice" }, h("span", { class: "dot" }), h("span", null, text), item.url ? external(item.url, "查看 ↗") : null) : null;
  };
  const notices = (plan.notices || []).filter(item => typeof item === "string" || item && item.text);
  if (notices.length) parts.push(h("section", { class: "plan-alerts", "aria-label": day < today ? "当日提醒记录" : "重要提醒" },
    notices.slice(0, 2).map(renderNotice),
    notices.length > 2 ? planDisclosure(`${day}/notices`, `展开其余提醒（${notices.length - 2} 条）`, notices.slice(2).map(renderNotice)) : null));
  const states = { [day]: { ...data.states } };
  const tasks = plan.tasks.map(t => ({ ...t, remaining: (derived.task_remaining || {})[day + "/" + t.id] }));
  if (isInventory || plan.kind === "inventory") return parts.concat(inventoryBody(plan, day, states[day], refreshPage));
  const fresh = tasks.filter(t => !t.auto_carry);
  const carried = tasks.filter(t => t.auto_carry);
  const finished = t => FINISHED_STATES.includes(states[day][t.id]) || t.remaining && t.remaining.resolved;
  const leftMinutes = t => t.remaining ? Number(t.remaining.minutes_left) || 0
    : finished(t) ? 0 : (Number(t.minutes) || 0) * (1 - (STATE_SHARE[states[day][t.id]] || 0));
  // An already-running helper serves new assets before its Python code is restarted.
  // Keep its existing task totals readable while generationBlock explains the restart.
  const sumMinutes = items => items.reduce((sum, t) => sum + (Number(t.minutes) || 0), 0);
  const summary = data.summary || {
    planned_minutes: sumMinutes(fresh), course_minutes: sumMinutes(fresh.filter(t => t.section !== 'research')),
    research_minutes: sumMinutes(fresh.filter(t => t.section === 'research')),
    completed_minutes: fresh.reduce((sum, t) => sum + (Number(t.minutes) || 0) * (STATE_SHARE[states[day][t.id]] || 0), 0),
    debt_hours: derived.debt && derived.debt.total_hours, recorded_focus_seconds: null,
  };
  const stat = (label, value, unit, detail, cls = "") => h("div", { class: "stat " + cls }, h("div", { class: "k" }, label),
    h("div", { class: "v" }, value, h("small", null, unit)), detail ? h("p", { class: "stat-detail" }, detail) : null);
  const progress = h("i");
  progress.style.width = Math.min(100, summary.planned_minutes ? summary.completed_minutes / summary.planned_minutes * 100 : 0) + "%";
  parts.push(h("section", { class: "stats", "aria-label": "计划概览" },
    stat("当天安排", hoursText(summary.planned_minutes || 0), "小时",
      `课程 ${hoursText(summary.course_minutes || 0)} · 科研与课外 ${hoursText(summary.research_minutes || 0)} 小时`),
    h("div", { class: "stat" }, h("div", { class: "k" }, "安排完成量"), h("div", { class: "v" }, hoursText(summary.completed_minutes || 0),
      h("small", null, `/ ${hoursText(summary.planned_minutes || 0)} 小时`)), h("div", { class: "bar" }, progress),
      h("p", { class: "stat-detail" }, `按打勾估算；另补旧任务 ${minutesText(summary.carried_completed_minutes || 0)}`)),
    stat("累计待补", String(summary.debt_hours ?? "—"), "小时", `截至 ${shortDate(day)} 之前，按原项去重`, "debt"),
    stat("已记录专注", summary.recorded_focus_seconds === null || summary.recorded_focus_seconds === undefined ? "—" : durText(summary.recorded_focus_seconds),
      "", summary.focus_error || `${shortDate(day)} 实际记录；不按任务预计时长计算`)));
  const priority = planPriority(fresh, plan.priority_tasks || [], finished);
  if (day === today && priority.length) {
    parts.push(h("section", { class: "next-tasks", "aria-label": "建议先做" }, h("div", { class: "section-h" }, h("h2", null, "建议先做"),
      h("span", null, "从原计划中选取")), priority.map(task => h("div", { class: "next-task " + courseClass(task.course_key) },
      h("div", null, h("span", { class: "course" }, task.course), button(task.title, () => jumpToTask(day, task.id), "task-jump")),
      h("span", { class: "muted small" }, minutesText(leftMinutes(task))), button("专注这项", () => focusOnTask(task, day), "btn tiny")))));
  }
  const main = h("div");
  const row = (task, rowDay = day, rowStates = states[day], feedback = data.feedback) => taskRow(task, rowDay, rowStates, refreshPage, STATE_LABELS, feedback && feedback[task.id]);
  main.append(h("div", { class: "section-h" }, h("h2", null, "各门课程"), h("span", null, "当天安排在前 · 顺延任务按需展开")));
  const groups = new Map();
  for (const task of [...fresh.filter(t => t.section !== "research"), ...carried]) {
    const key = task.course_key || task.course || "other";
    if (!groups.has(key)) groups.set(key, { key, course: (task.course || "其他").split(" · ")[0], fresh: [], carried: [] });
    groups.get(key)[task.auto_carry ? "carried" : "fresh"].push(task);
  }
  if (!groups.size) main.append(empty("这一天没有课程任务。"));
  for (const g of groups.values()) {
    const activeFresh = g.fresh.filter(t => !finished(t)), activeCarried = g.carried.filter(t => !finished(t));
    const closed = [...g.fresh, ...g.carried].filter(finished);
    main.append(h("section", { class: "course-group " + courseClass(g.key), id: courseAnchor(g.key) },
      h("div", { class: "group-h" }, h("i"), h("b", null, g.course),
        h("span", null, `当天待做 ${activeFresh.length} 项 · 预计剩余 ${hoursText(activeFresh.reduce((sum, t) => sum + leftMinutes(t), 0))} 小时`)),
      activeFresh.length ? h("div", { class: "list" }, activeFresh.map(t => row(t))) : h("p", { class: "muted small" }, "当天安排已处理，或这门课没有新增安排。"),
      activeCarried.length ? planDisclosure(`${day}/${g.key}/carry`,
        `顺延待补 · ${activeCarried.length} 项 · 预计剩余 ${hoursText(activeCarried.reduce((sum, t) => sum + leftMinutes(t), 0))} 小时`,
        h("div", { class: "list" }, activeCarried.map(t => row(t)))) : null,
      closed.length ? planDisclosure(`${day}/${g.key}/closed`, `已完成或不再补 · ${closed.length} 项`,
        h("div", { class: "list" }, closed.map(t => row(t)))) : null));
  }
  const courseTotal = (summary.course_minutes || 0) + carried.filter(t => !finished(t)).reduce((sum, t) => sum + leftMinutes(t), 0);
  if (summary.target_max_minutes && courseTotal > summary.target_max_minutes) {
    main.append(h("p", { class: "muted small workload-note" },
      `全部课程加顺延尚有约 ${hoursText(courseTotal)} 小时；当天课程目标 ${plan.targets.course_hours} 小时。顺延保留待补，不要求今天全部完成。`));
  }
  const research = fresh.filter(t => t.section === "research"), week = currentPlan.research_week;
  if (research.length || week) {
    append(main, [h("div", { class: "section-h" }, h("h2", null, "科研与课外"),
      week ? h("span", null, `本周 ${week.done_hours ?? 0} / ${week.target_hours ?? "?"} 小时`) : null),
      research.filter(t => !finished(t)).length ? h("div", { class: "list" }, research.filter(t => !finished(t)).map(t => row(t))) : null,
      research.filter(finished).length ? planDisclosure(`${day}/research/closed`, "已完成的科研与课外",
        h("div", { class: "list" }, research.filter(finished).map(t => row(t)))) : null]);
  }
  const previous = data.previous;
  if (previous && previous.tasks.length) {
    states[previous.date] = { ...previous.states };
    main.append(planDisclosure(`${day}/previous`, previous.inventory ? "补记开学盘点" : `补记 ${shortDate(previous.date)} 的任务（${previous.tasks.length} 项）`,
      previous.inventory ? h("a", { href: "#/plan/" + previous.date }, "打开盘点") : h("div", { class: "list" },
        previous.tasks.map(t => row({ ...t, remaining: (derived.task_remaining || {})[previous.date + "/" + t.id] }, previous.date, states[previous.date], previous.feedback)))));
  }
  parts.push(h("div", { class: "plan-grid" }, main, planAside(currentPlan, today)));
  return parts;
}

// Month board above the plan: a day with a plan opens it, and a thin bar shows how much of
// it is ticked. The inventory is a tile of its own beside the month, not listed under its date.
const CAL_WEEKDAYS = ["一", "二", "三", "四", "五", "六", "日"];
function planCalendar(entries, shown, today) {
  if (!entries.length) return null;
  const share = e => e.tasks ? Math.min(1, (e.done + e.half / 2) / e.tasks) : 0;
  const bar = e => { const i = h("i"); i.style.width = Math.round(share(e) * 100) + "%"; return h("span", { class: "cal-bar" }, i); };
  const byDate = new Map(entries.filter(e => !e.inventory).map(e => [e.date, e]));
  const months = [...byDate.keys(), today].map(d => d.slice(0, 7)).sort();
  let month = (byDate.has(shown) ? shown : byDate.has(today) || !byDate.size ? today : [...byDate.keys()].pop()).slice(0, 7);
  const grid = h("div", { class: "cal-grid" });
  const title = h("b");
  const back = button("‹", () => { month = shiftMonth(month, -1); render(); }, "btn tiny");
  const ahead = button("›", () => { month = shiftMonth(month, 1); render(); }, "btn tiny");
  back.setAttribute("aria-label", "上个月");
  ahead.setAttribute("aria-label", "下个月");

  function render() {
    const [y, m] = month.split("-").map(Number);
    title.textContent = `${y} 年 ${m} 月`;
    back.disabled = month <= months[0];
    ahead.disabled = month >= months[months.length - 1];
    const lead = (new Date(y, m - 1, 1).getDay() + 6) % 7;  // weeks start on Monday
    const cells = CAL_WEEKDAYS.map(d => h("span", { class: "cal-wd" }, d));
    for (let i = 0; i < lead; i++) cells.push(h("span", { class: "cal-day blank" }));
    for (let d = 1; d <= new Date(y, m, 0).getDate(); d++) {
      const iso = `${month}-${pad2(d)}`;
      const entry = byDate.get(iso);
      const cls = "cal-day" + (iso === today ? " today" : "") + (entry && iso === shown ? " sel" : "");
      if (!entry) { cells.push(h("span", { class: cls }, h("span", { class: "num" }, String(d)))); continue; }
      cells.push(h("a", { class: cls + " has", href: iso === today ? "#/" : "#/plan/" + iso,
        title: `${planDayLabel(iso, today)} · ${entry.tasks} 项，完成 ${entry.done} 项` + (entry.half ? `，一半 ${entry.half} 项` : ""),
        "aria-current": iso === shown ? "date" : null },
        h("span", { class: "num" }, String(d)), bar(entry)));
    }
    grid.replaceChildren(...cells);
  }
  render();

  const inventories = entries.filter(e => e.inventory).map(e => h("a", {
    class: "cal-inv" + (e.date === shown ? " sel" : ""), href: "#/plan/" + e.date, "aria-current": e.date === shown ? "page" : null },
    h("span", { class: "k" }, "开学盘点"),
    h("b", null, String(e.done), h("small", null, ` / ${e.tasks} 项完成`)),
    e.half ? h("span", { class: "k" }, `另有 ${e.half} 项不够好`) : null, bar(e)));
  return h("section", { class: "cal" + (inventories.length ? "" : " solo"), "aria-label": "计划日历" },
    inventories.length ? h("div", { class: "cal-side" }, inventories) : null,
    h("div", { class: "cal-month" }, h("div", { class: "cal-head" }, title, h("span", { class: "cal-nav" }, back, ahead)), grid));
}
function shiftMonth(month, step) {
  const [y, m] = month.split("-").map(Number);
  const d = new Date(y, m - 1 + step, 1);
  return `${d.getFullYear()}-${pad2(d.getMonth() + 1)}`;
}

function inventoryBody(plan, day, dayStates, refreshPage) {
  const stats = h("section", { class: "stats" });
  const tasks = plan.tasks;
  const renderStats = () => {
    const marked = tasks.filter(t => dayStates[t.id]).length;
    const done = tasks.filter(t => dayStates[t.id] === "done").length;
    const left = tasks.filter(t => t.required !== false)
      .reduce((sum, t) => sum + (Number(t.minutes) || 0) * (1 - (STATE_SHARE[dayStates[t.id]] || 0)), 0);
    stats.replaceChildren(
      h("div", { class: "stat" }, h("div", { class: "k" }, "要盘点"), h("div", { class: "v" }, String(tasks.length), h("small", null, "项"))),
      h("div", { class: "stat" }, h("div", { class: "k" }, "已标记"), h("div", { class: "v" }, String(marked), h("small", null, `/ ${tasks.length} 项`))),
      h("div", { class: "stat" }, h("div", { class: "k" }, "已完成"), h("div", { class: "v" }, String(done), h("small", null, "项"))),
      h("div", { class: "stat debt" + (left === 0 ? " clear" : "") }, h("div", { class: "k" }, "还要补（估计，不含拓展）"),
        h("div", { class: "v" }, hoursText(left), h("small", null, "小时"))));
  };
  renderStats();
  const groups = new Map();
  for (const task of tasks) {
    const key = task.course || "其他";
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(task);
  }
  const lists = [...groups].map(([course, items]) => [
    h("div", { class: "section-h" }, h("h2", null, course), h("span", null, `${items.length} 项`)),
    h("div", { class: "list" }, items.map(t => taskRow(t, day, dayStates, refreshPage || renderStats, INVENTORY_LABELS)))]);
  return [stats, h("div", { class: "inventory" }, lists)];
}

function taskRow(task, day, dayStates, refresh, labels = STATE_LABELS, feedback = {}) {
  const row = h("div", { class: "task " + courseClass(task.course_key), id: taskAnchor(day, task.id), tabindex: "-1" });
  row.dataset.state = dayStates[task.id] || "";
  const historical = day < planToday || task.debt_from && task.debt_from < planToday;
  const tags = (task.tags || []).map(tag => {
    const text = typeof tag === "string" ? tag : tag && tag.text;
    if (!text || task.due_at && /今天|明天|截止|天后/.test(text)) return null;
    return h("span", { class: "tag" + (!historical && tag.hot ? " hot" : "") }, historical && /今天|明天|昨天|天后/.test(text) ? "原记录：" + text : text);
  });
  if (task.due_at) {
    const due = new Date(task.due_at);
    if (!isNaN(due)) {
      const days = daysBetween(planToday, berlinDate(due)), expired = due.getTime() < Date.now();
      tags.push(h("span", { class: "tag" + (expired || days === 0 ? " hot" : "") },
        expired ? "已过期 · " + berlinStamp(due) : days === 0 ? "今天截止 · " + berlinStamp(due) : days === 1 ? "明天截止 · " + berlinStamp(due) : berlinStamp(due) + " 截止"));
    }
  }
  const links = (task.links || []).map(link => link && external(link.url, (link.text || "链接") + " ↗")).filter(Boolean);
  const buttons = labels.map(([value, label]) => h("button", { type: "button", "data-v": value,
    onclick: () => save(row.dataset.state === value ? null : value) }, label));
  const drop = task.origin === "debt" ? h("button", { type: "button", class: "drop", onclick: async () => {
    if (row.dataset.state === "dropped") return save(null);
    const ok = await confirmDialog("不再补这一项？",
      [h("p", null, "这会放弃整个原始学习项的剩余部分，包括其他日期安排的同一项；记录会保留。"),
       h("p", { class: "muted small" }, "改主意了可以再点“恢复”。")], "不再补");
    if (ok) save("dropped");
  } }) : null;
  const paint = () => {
    if (drop) drop.textContent = row.dataset.state === "dropped" ? "恢复" : "不再补";
    buttons.forEach(b => b.setAttribute("aria-pressed", String(b.dataset.v === row.dataset.state)));
  };

  async function save(state) {
    buttons.forEach(b => { b.disabled = true; });
    try {
      const result = await api("/progress", { date: day, task: task.id, state });
      for (const key of Object.keys(dayStates)) delete dayStates[key];
      Object.assign(dayStates, result.states);
      row.dataset.state = dayStates[task.id] || "";
      paint();
      await refresh({ anchor: row.id });
    } catch (e) { toast(e.message, true); }
    finally { buttons.forEach(b => { b.disabled = false; }); }
  }

  append(row, [
    h("div", { class: "stripe" }),
    h("div", null,
      h("div", { class: "course" }, task.course || "",
        task.origin === "debt" ? h("span", { class: "tag hot" }, (task.origin_note || "欠账") + (task.debt_from ? " · " + shortDate(task.debt_from) : "")) : null,
        task.required === false && task.section !== "research" ? h("span", { class: "tag" }, "拓展") : null, tags),
      h("div", { class: "title" }, task.title),
      task.resource_note ? h("div", { class: "muted small" }, task.resource_note) : null,
      task.remaining && task.remaining.resolved && !["done", "dropped"].includes(dayStates[task.id])
        ? h("div", { class: "muted small" }, task.remaining.dropped ? "这项原任务已选择不再补。" : "这部分已在其他日期的计划中完成，无需重复学习。") : null,
      task.remaining && !task.remaining.resolved && task.origin === "debt"
        ? h("div", { class: "muted small" }, "这次安排还剩约 ", minutesText(task.remaining.minutes_left)) : null,
      historical && task.detail ? h("div", { class: "record-label" }, "当日记录 · ", shortDate(task.debt_from || day)) : null,
      task.detail ? h("div", { class: "detail" }, task.detail) : null,
      links.length ? h("div", { class: "task-links" }, links) : null,
      (task.materials || []).length ? h("div", { class: "muted small material-refs" }, task.materials.map(m => {
        const path = String(m.path || ""), folder = path.replace(/[\\/][^\\/]+$/, "");
        return h("div", { class: "material-ref" }, h("span", null, "资料：", path.split(/[\\/]/).pop(),
          Array.isArray(m.pages) ? ` · 第 ${m.pages[0]}–${m.pages[1]} 页` : "", m.unavailable ? " · 文件缺失" : ""),
          folder && folder !== path && !m.unavailable ? button("打开所在文件夹", () => openFolder(folder), "btn tiny") : null);
      })) : null,
      task.practice ? practiceBox(task.practice) : null,
      labels === STATE_LABELS ? feedbackBox(task, day, dayStates, feedback || {}, refresh) : null),
    h("div", { class: "side" }, task.minutes ? h("span", { class: "dur" }, minutesText(task.minutes)) : null,
      task.focus_seconds ? h("span", { class: "dur spent", title: "番茄钟记到这项任务上的专注时间" }, "已专注 " + durText(task.focus_seconds)) : null,
      h("div", { class: "state" }, buttons), drop,
      labels === STATE_LABELS ? button("专注这项", () => focusOnTask(task, day), "btn tiny focus-go") : null),
  ]);
  paint();
  return row;
}

function feedbackBox(task, day, dayStates, feedback, refresh) {
  const minutes = h("input", { type: "number", min: "1", max: "1440", step: "1", "aria-label": "实际用时（分钟）" });
  minutes.value = feedback.actual_minutes ?? "";
  const understanding = h("select", { "aria-label": "理解程度" },
    [["", "暂不填写"], ["understood", "理解"], ["hard", "吃力"], ["not_understood", "没懂"]].map(([value, text]) => h("option", { value }, text)));
  understanding.value = feedback.understanding || "";
  const saved = h("span", { class: "muted small", role: "status" });
  const save = button("保存反馈", async () => {
    const value = minutes.value.trim();
    if (value && (!Number.isInteger(Number(value)) || Number(value) < 1 || Number(value) > 1440)) {
      saved.textContent = "用时请填 1–1440 的整数。"; return;
    }
    save.disabled = true;
    try {
      await api("/progress", { date: day, task: task.id, state: dayStates[task.id] || null,
        actual_minutes: value ? Number(value) : null, understanding: understanding.value || null });
      await refresh({ anchor: taskAnchor(day, task.id) });
      toast("反馈已保存，不影响完成状态。");
    } catch (e) { saved.textContent = e.message; }
    finally { save.disabled = false; }
  }, "btn tiny");
  const details = planDisclosure(`${day}/${task.id}/feedback`, "学习反馈（可选）",
    h("div", { class: "feedback-fields" }, h("label", null, "实际用时（分钟）", minutes),
      h("label", null, "理解程度", understanding), save, saved));
  details.classList.add("task-feedback");
  return details;
}

// From the plan to the timer: the next focus is about this task (it does not start by itself).
async function focusOnTask(task, day) {
  const choices = await focusCourses();
  const key = task.course_key || "other";
  timer.draft = { course_key: key, course: courseName(choices.names, key, task.course), title: task.title,
                  plan_date: day, plan_task: task.id };
  timer.mode = "focus";
  if (timer.state && timer.state.active && !isBreak(timer.state.active.mode)) {
    try { await timerAction({ action: "task", task: timer.draft }); } catch (e) { toast(e.message, true); }
  }
  location.hash = "#/focus";
}

function practiceBox(practice) {
  const total = Math.max(0, Math.min(40, Number(practice.tests_total) || 0));
  const checked = practice.attempt_state === "attempted" && practice.tests_passed !== null && practice.tests_passed !== undefined;
  const passed = checked ? Math.max(0, Number(practice.tests_passed) || 0) : 0;
  const level = Math.max(0, Math.min(5, Number(practice.difficulty) || 0));
  const checkedAt = practice.checked_at ? new Date(practice.checked_at) : null;
  const meta = [
    practice.attempt_state === "untouched" ? "最近检测：未作答（仍为框架）" : checked ? `最近检测：已作答 · ${passed} / ${total} 个测试点` : "尚无可靠检测记录",
    !checked && total ? `${total} 个测试点` : null,
    level ? "难度 " + "★".repeat(level) + "☆".repeat(Math.max(0, 3 - level)) : null,
    checkedAt && !isNaN(checkedAt) ? `检测于 ${shortDate(isoDate(checkedAt))} ${timeLabel(checkedAt)}` : null,
  ].filter(Boolean).join(" · ");
  return h("div", { class: "code" },
    h("div", { class: "tests" }, Array.from({ length: total }, (_, i) => h("i", { class: i < passed ? "pass" : "" }))),
    h("span", { class: "meta" }, meta),
    practice.record_note ? h("span", { class: "muted small" }, practice.record_note) : null,
    practice.error ? h("span", { class: "muted small" }, String(practice.error)) : null,
    practice.folder_missing ? h("span", { class: "muted small" }, "练习文件夹缺失") : practice.folder ? button("打开文件夹", () => openFolder(practice.folder), "btn tiny") : null);
}

function planAside(plan, today) {
  const aside = h("aside");
  const upcoming = (plan.upcoming || []).filter(u => u && (u.date || u.date_text) && u.title);
  if (upcoming.length) {
    const upcomingRow = item => {
      const { days, label } = upcomingTiming(item, today);
      return h("div", { class: "up " + courseClass(item.course_key) + (days !== null && days <= 7 && item.kind !== "registration" ? " soon" : "") },
        h("div", { class: "when" }, h("b", null, days === null ? "—" : days < 0 ? "过" : days === 0 ? "今" : String(days)), h("span", null, days === null ? "待定" : days < 0 ? "已过期" : days === 0 ? "今天" : "天后")),
        h("div", { class: "what" }, [item.course, item.title].filter(Boolean).join(" · "),
          h("small", null, [label, item.note].filter(Boolean).join(" · ")), item.url ? external(item.url, "查看依据 ↗") : null));
    };
    const future = upcoming.filter(item => upcomingTiming(item, today).days === null || upcomingTiming(item, today).days >= 0);
    const expired = upcoming.filter(item => upcomingTiming(item, today).days !== null && upcomingTiming(item, today).days < 0);
    aside.append(h("div", { class: "pcard" }, h("h3", null, "即将到来"), future.map(upcomingRow),
      !future.length ? h("p", { class: "muted small" }, "这份计划中没有未过期的近期事项。") : null,
      expired.length ? planDisclosure(`${plan.date}/expired`, `已过期事项（${expired.length} 项）`, expired.map(upcomingRow)) : null));
  }
  const progress = (plan.week_progress || []).filter(p => p && p.course);
  if (progress.length) {
    // Each cell fills by the share of this week's pieces that are done (half counts half), so
    // every tick shows; plans written before the counts existed fall back to the three states.
    const pip = (p, kind) => {
      const c = p.counts && p.counts[kind];
      if (!c) return h("span", { class: "pip " + (p[kind] === "done" || p[kind] === "half" ? p[kind] : "") });
      if (!c.total) return h("span", { class: "pip none", title: "本周没有安排" });
      const share = Math.round((c.done + c.half / 2) / c.total * 100);
      const el = h("span", { class: "pip " + (share >= 100 ? "done" : share > 0 ? "part" : ""),
        title: `完成 ${c.done} / ${c.total} 项` + (c.half ? `，另有 ${c.half} 项做了一半` : "") });
      el.style.setProperty("--p", share);
      return el;
    };
    aside.append(h("div", { class: "pcard" }, h("h3", null, "本周进度"),
      h("div", { class: "prog" }, h("span"), h("span", { class: "hd" }, "讲座"), h("span", { class: "hd" }, "习题"),
        h("span", { class: "hd" }, "拓展"),
        progress.map(p => [h("button", { type: "button", class: "name " + courseClass(p.course_key),
            title: `看所选日期 ${p.course} 的全部任务`, onclick: () => jumpToCourse(p.course_key, p.course) }, h("i"), p.course),
          pip(p, "lecture"), pip(p, "exercise"), pip(p, "extension")])),
      h("p", { class: "muted small prog-note" }, "点课程名跳到所选日期的任务；圆点按该周完成比例填满，鼠标停在上面能看到几项。")));
  }
  const debts = ((plan.debt && plan.debt.by_course) || []).filter(d => d && d.hours > 0);
  if (debts.length) {
    const most = Math.max(...debts.map(d => d.hours));
    aside.append(h("div", { class: "pcard" }, h("h3", null, `累计待补明细 · 共 ${plan.debt.total_hours ?? ""} 小时`),
      debts.map(d => {
        const fill = h("i");
        fill.style.width = Math.max(4, d.hours / most * 100) + "%";
        return h("div", { class: "debt-row " + courseClass(d.course_key) }, h("span", null, d.course),
          h("span", { class: "track" }, fill), h("b", null, String(d.hours)));
      }),
      h("p", { class: "muted small prog-note" }, "统计所选日期之前的必做原项，重复顺延不重复计算。课程内数字只表示该组的剩余工作量。")));
  }
  const feedback = plan.learning_feedback;
  if (feedback && feedback.courses && feedback.courses.length) {
    aside.append(h("div", { class: "pcard" }, h("h3", null, "近七天学习反馈"),
      feedback.courses.map(c => h("p", { class: "small" }, c.course || c.course_key, "：", `${c.observations} 次反馈`,
        c.difficult ? `，${c.difficult} 次觉得吃力或没懂` : "",
        c.suggested_time_factor ? `；同类任务可参考约 ${c.suggested_time_factor} 倍原估时` : "")),
      h("p", { class: "muted small" }, "只作下次计划参考，课程比例与每日预算不变。")));
  }
  return aside;
}

// ---------------------------------------------------------------- pomodoro timer (all pages)
// The timer itself lives in the planner folder (focus.py): this page only sends actions and
// counts down from the server's timestamps, so reloads, other pages, closed tabs and sleep
// never lose or double a record. Title and the sidebar badge show the time on every page.

const MODE_NAMES = { focus: "专注", stopwatch: "正计时", short: "短休息", long: "长休息" };
const REASONS = [["phone", "手机"], ["social", "社交媒体"], ["tired", "疲劳"], ["errand", "临时事务"], ["other", "其他"]];
const REASON_NAMES = Object.fromEntries(REASONS);
const STOPWATCH_LIMIT = 3 * 3600;
const BASE_TITLE = document.title;
const timer = { state: null, offset: 0, interval: null, endTimer: null, settling: false, render: null, tickView: null,
                draft: null, mode: null, audio: null };

function pad2(n) { return String(n).padStart(2, "0"); }
function clockText(seconds) {
  const s = Math.max(0, Math.round(seconds));
  const hh = Math.floor(s / 3600);
  return hh ? `${hh}:${pad2(Math.floor(s % 3600 / 60))}:${pad2(s % 60)}` : `${pad2(Math.floor(s / 60))}:${pad2(s % 60)}`;
}
function durText(seconds) {
  const m = Math.floor((Number(seconds) || 0) / 60);  // never claim a minute that was not reached
  if (m < 1) return seconds > 0 ? "不到 1 分钟" : "0 分钟";
  if (m < 60) return `${m} 分钟`;
  return m % 60 ? `${Math.floor(m / 60)} 小时 ${m % 60} 分` : `${m / 60} 小时`;
}
// [number, unit] for the big digits of a stat card.
function durParts(seconds) {
  const s = Number(seconds) || 0;
  if (s < 3600) return [String(Math.round(s / 60)), "分钟"];
  const hours = s / 3600;
  return [String(hours >= 100 ? Math.round(hours) : Math.round(hours * 10) / 10), "小时"];
}
function clockOf(iso) { return timeLabel(new Date(iso)); }

function timerNow() { return Date.now() + timer.offset; }
function timerElapsed() {
  const a = timer.state && timer.state.active;
  if (!a) return 0;
  return a.elapsed + (a.paused ? 0 : Math.max(0, (timerNow() - Date.parse(timer.state.now)) / 1000));
}
function timerRemaining() {
  const a = timer.state && timer.state.active;
  return a ? (a.planned_seconds || STOPWATCH_LIMIT) - timerElapsed() : null;
}
function isBreak(mode) { return mode === "short" || mode === "long"; }

async function timerCall(path, body) {
  const state = await api(path, body);
  timerApply(state);
  return state;
}
function timerAction(body) { return timerCall("/focus/action", body); }

function timerApply(state) {
  timer.state = state;
  timer.offset = Date.parse(state.now) - Date.now();
  timer.settling = false;
  clearTimeout(timer.endTimer);
  clearInterval(timer.interval);
  const a = state.active;
  if (a) {
    timer.interval = setInterval(timerTick, 1000);
    // A one-off timeout fires on time even when the 1 s interval is throttled in a background tab.
    if (!a.paused) timer.endTimer = setTimeout(timerSettle, Math.max(0, timerRemaining() * 1000) + 400);
  }
  for (const event of state.events || []) timerEvent(event);
  timerTick();
  if (timer.render) timer.render();
}

// Time is up: ask the server, which records the session at the moment it really ended.
function timerSettle() {
  if (timer.settling) return;
  timer.settling = true;
  timerCall("/focus").catch(() => { setTimeout(() => { timer.settling = false; }, 30000); });
}

function timerTick() {
  const a = timer.state && timer.state.active;
  const badge = $("#nav-timer");
  if (!a) {
    document.title = BASE_TITLE;
    if (badge) badge.hidden = true;
  } else {
    const remaining = timerRemaining();
    const text = clockText(a.mode === "stopwatch" ? timerElapsed() : remaining);
    document.title = `${a.paused ? "⏸ " : ""}${text} ${MODE_NAMES[a.mode]} · TUM 小助理`;
    if (badge) {
      badge.hidden = false;
      badge.textContent = (a.paused ? "⏸ " : "") + text;
      badge.classList.toggle("brk", isBreak(a.mode));
    }
    if (!a.paused && remaining <= 0) timerSettle();
  }
  if (timer.tickView) timer.tickView();
}

function timerEvent(event) {
  const ended = Date.parse(event.type === "focus_done" ? event.session.end : event.end);
  const fresh = timerNow() - ended < 120000;
  if (event.type === "focus_done") {
    const s = event.session;
    const what = s.task && (s.task.title || s.task.course) ? " · " + (s.task.title || s.task.course) : "";
    if (s.kind === "stopwatch") {
      toast(`正计时已满 3 小时，自动停止并记录了 ${durText(s.focus_seconds)}。`);
    } else if (fresh) {
      alertUser("🍅 番茄完成", `专注了 ${durText(s.focus_seconds)}${what}。` + (timer.state.active ? "休息开始。" : "休息一下吧。"));
    } else {
      toast(`你不在的时候，${clockOf(s.end)} 完成了一个番茄（${durText(s.focus_seconds)}${what}），已记录。`);
    }
  } else if (fresh) {
    alertUser("休息结束", "可以开始下一个番茄了。");
    if (timer.state.settings.auto_focus && !timer.state.active && timerNow() - ended < 15000) {
      timerAction({ action: "start", mode: "focus", task: timer.state.last_task || {} }).catch(e => toast(e.message, true));
    }
  }
}

function alertUser(title, body) {
  const settings = timer.state.settings;
  if (settings.sound) chime();
  if (settings.notify && "Notification" in window && Notification.permission === "granted") {
    try { new Notification(title, { body, icon: "icon.png", tag: "tum-focus" }); } catch (e) { /* the toast still shows */ }
  }
  toast(`${title}：${body}`);
}

// Three soft rising notes, generated (no sound files to load).
function chime() {
  try {
    const ctx = timer.audio = timer.audio || new AudioContext();
    [660, 880, 990].forEach((freq, i) => {
      const at = ctx.currentTime + i * 0.22;
      const osc = ctx.createOscillator();
      const gain = ctx.createGain();
      osc.frequency.value = freq;
      gain.gain.setValueAtTime(0.0001, at);
      gain.gain.exponentialRampToValueAtTime(0.22, at + 0.02);
      gain.gain.exponentialRampToValueAtTime(0.0001, at + 0.6);
      osc.connect(gain).connect(ctx.destination);
      osc.start(at);
      osc.stop(at + 0.65);
    });
  } catch (e) { /* no audio on this computer */ }
}

// Browsers allow sound and the notification prompt only after a click, so ask on "start".
function primeAlerts() {
  try { timer.audio = timer.audio || new AudioContext(); timer.audio.resume(); } catch (e) { /* no audio */ }
  const settings = timer.state && timer.state.settings;
  if (settings && settings.notify && "Notification" in window && Notification.permission === "default") {
    Notification.requestPermission().catch(() => null);
  }
}

document.addEventListener("visibilitychange", () => {
  if (!document.hidden && token()) timerCall("/focus").catch(() => null);
});

// ---------------------------------------------------------------- courses for focus records
// The course map's folder names ("INHN0002 Fundamentals of Programming") give short English names.

const EXTRA_COURSES = [["research", "科研"], ["other", "其他"]];
let courseChoices = null;
async function focusCourses() {
  if (courseChoices) return courseChoices;
  let entries = [];
  try { entries = (await courseMap()).entries || []; } catch (e) { /* only research and other */ }
  const names = new Map();
  const active = [];
  for (const entry of entries) {
    const folder = String(entry.folder || "").split(/[\\/]/).pop();
    const [key, ...rest] = folder.split(" ");
    if (!/^[A-Za-z0-9_-]{1,40}$/.test(key || "") || names.has(key)) continue;
    names.set(key, rest.join(" ") || entry.title || key);
    if (!entry.completed) active.push([key, names.get(key)]);
  }
  for (const [key, name] of EXTRA_COURSES) { names.set(key, name); active.push([key, name]); }
  courseChoices = { names, active };
  return courseChoices;
}
function courseName(names, key, fallback) { return names.get(key || "other") || fallback || key || "其他"; }

function courseSelect(choices, value, blank = "选择课程…") {
  const select = h("select", { class: "field", "aria-label": "课程" }, h("option", { value: "" }, blank),
    choices.active.map(([key, name]) => h("option", { value: key }, name)));
  if (value && !choices.active.some(([key]) => key === value)) select.append(h("option", { value }, courseName(choices.names, value)));
  select.value = value || "";
  return select;
}

// What the next or running focus is about: a task of today's plan, or a course plus free text.
async function taskPicker() {
  const [choices, planData] = await Promise.all([focusCourses(), api("/plan").catch(() => null)]);
  const plan = planData && planData.plan;
  const states = (planData && planData.states) || {};
  const planTasks = plan ? plan.tasks.filter(t => !["done", "dropped"].includes(states[t.id])) : [];
  const select = h("select", { class: "field", "aria-label": "专注于" }, h("option", { value: "" }, "不指定"),
    planTasks.length ? h("optgroup", { label: planData.date === planData.today ? "今天的计划" : `${shortDate(planData.date)} 的计划` },
      planTasks.map(t => h("option", { value: "plan:" + t.id }, `${t.course ? t.course + " · " : ""}${t.title}`))) : null,
    h("optgroup", { label: "课程与其他" }, choices.active.map(([key, name]) => h("option", { value: "course:" + key }, name))));
  const title = h("input", { class: "field", maxlength: "120", placeholder: "具体在做什么（可选）", "aria-label": "具体内容" });
  const current = () => {
    const [kind, id] = select.value.split(":");
    if (kind === "plan") {
      const t = planTasks.find(x => x.id === id);
      const key = t.course_key || "other";
      return { course_key: key, course: courseName(choices.names, key, t.course), title: title.value.trim() || t.title,
               plan_date: planData.date, plan_task: t.id };
    }
    const task = { title: title.value.trim() || null };
    if (kind === "course") Object.assign(task, { course_key: id, course: courseName(choices.names, id) });
    return task;
  };
  const save = () => {
    timer.draft = current();
    if (timer.state && timer.state.active && !isBreak(timer.state.active.mode)) {
      timerAction({ action: "task", task: timer.draft }).catch(e => toast(e.message, true));
    }
  };
  select.addEventListener("change", () => {
    const t = planTasks.find(x => "plan:" + x.id === select.value);
    if (t) title.value = t.title;
    save();
  });
  title.addEventListener("change", save);
  const set = task => {
    task = task || {};
    if (task.plan_task && task.plan_date === (planData && planData.date) && planTasks.some(t => t.id === task.plan_task)) {
      select.value = "plan:" + task.plan_task;
    } else if (task.course_key) {
      if (![...select.options].some(o => o.value === "course:" + task.course_key)) {
        select.append(h("option", { value: "course:" + task.course_key }, courseName(choices.names, task.course_key, task.course)));
      }
      select.value = "course:" + task.course_key;
    } else select.value = "";
    title.value = task.title || "";
  };
  const element = h("div", { class: "picker" }, select, title);
  return { element, set, current, editing: () => element.contains(document.activeElement) };
}

function currentTask() {
  const s = timer.state;
  if (s && s.active && !isBreak(s.active.mode)) return s.active.task || {};
  return timer.draft || (s && s.last_task) || {};
}

// ---------------------------------------------------------------- page: pomodoro

function viewFocus() {
  const holder = h("div");
  show(holder);
  load(holder, async () => {
    await timerCall("/focus");
    return focusPage(await taskPicker());
  });
}

function focusPage(picker) {
  const R = 92, CIRC = 2 * Math.PI * R;
  const NS = "http://www.w3.org/2000/svg";
  const ring = document.createElementNS(NS, "svg");
  ring.setAttribute("viewBox", "0 0 200 200");
  ring.setAttribute("class", "ring");
  ring.setAttribute("aria-hidden", "true");
  const circle = cls => {
    const c = document.createElementNS(NS, "circle");
    for (const [k, v] of Object.entries({ cx: 100, cy: 100, r: R, class: cls })) c.setAttribute(k, v);
    ring.append(c);
    return c;
  };
  circle("track");
  const arc = circle("arc");
  arc.setAttribute("stroke-dasharray", String(CIRC));
  arc.setAttribute("transform", "rotate(-90 100 100)");

  const clock = h("div", { class: "clock", role: "timer", "aria-live": "off" });
  const label = h("div", { class: "clock-label" });
  const modes = h("div", { class: "seg modes", role: "group", "aria-label": "计时模式" });
  const lengths = h("div", { class: "seg lengths", role: "group", "aria-label": "专注时长" });
  const controls = h("div", { class: "controls" });
  const cycle = h("div", { class: "cycle" });
  const taskLine = h("div", { class: "task-line" });
  const card = h("section", { class: "timer-card" },
    modes, lengths, h("div", { class: "dial" }, ring, h("div", { class: "dial-text" }, clock, label)), taskLine, controls, cycle,
    h("div", { class: "timer-foot" },
      button("全屏专注", () => toggleZen(), "btn ghost tiny zen-toggle"),
      button("设置", () => focusSettings(), "btn ghost tiny")));

  const goalBox = h("div");
  const records = h("div");
  const aside = h("aside", { class: "focus-aside" },
    h("div", { class: "pcard" }, h("h3", null, "专注于"), picker.element,
      h("p", { class: "muted small" }, "选今天计划里的任务，时间会记到那项任务上；也可以只选课程。")),
    h("div", { class: "pcard" }, h("h3", null, "今天"), goalBox),
    h("div", { class: "pcard" }, h("h3", null, "今天的记录"), records,
      h("p", { class: "small" }, h("a", { href: "#/stats" }, "查看专注统计 →"))));

  const shownMode = () => (timer.state.active ? timer.state.active.mode : timer.mode || timer.state.next_mode || "focus");
  const fullSeconds = mode => mode === "stopwatch" ? 0 : timer.state.settings[mode + "_minutes"] * 60;

  const tickView = () => {
    const s = timer.state;
    const a = s.active;
    const mode = shownMode();
    let shown, progress;
    if (a) {
      const elapsed = timerElapsed();
      shown = a.mode === "stopwatch" ? elapsed : timerRemaining();
      progress = a.mode === "stopwatch" ? (elapsed % 3600) / 3600 : Math.min(1, elapsed / a.planned_seconds);
    } else {
      shown = fullSeconds(mode);
      progress = 0;
    }
    clock.textContent = clockText(shown);
    arc.setAttribute("stroke-dashoffset", String(CIRC * (1 - progress)));
    arc.classList.toggle("empty", progress <= 0);
    card.dataset.mode = isBreak(mode) ? "break" : "focus";
    card.dataset.run = a ? (a.paused ? "paused" : "running") : "idle";
    const live = a && !isBreak(a.mode) ? timerElapsed() : 0;
    renderGoal(goalBox, s, live);
  };

  const render = () => {
    const s = timer.state;
    const a = s.active;
    const mode = shownMode();
    modes.replaceChildren(...Object.entries(MODE_NAMES).map(([key, name]) => h("button", {
      type: "button", class: key === mode ? "on" : "", disabled: !!a, "aria-pressed": String(key === mode),
      onclick: () => { timer.mode = key; render(); } }, name)));
    // Focus length: a few common choices plus any length; the choice becomes the default.
    const current = s.settings.focus_minutes;
    lengths.hidden = !!a || mode !== "focus";
    lengths.replaceChildren(...[...new Set([...FOCUS_LENGTHS, current])].sort((x, y) => x - y).map(m =>
      h("button", { type: "button", class: m === current ? "on" : "", "aria-pressed": String(m === current),
        onclick: () => setFocusLength(m) }, m === 60 ? "1 小时" : `${m} 分钟`)),
      h("button", { type: "button", onclick: () => customFocusLength(current) }, "自定义…"));
    label.textContent = a ? (a.paused ? "已暂停" : MODE_NAMES[a.mode] + "中")
      : mode === "stopwatch" ? "正计时，最长 3 小时" : `${MODE_NAMES[mode]} ${s.settings[mode + "_minutes"]} 分钟`;
    const task = isBreak(mode) ? null : currentTask();
    taskLine.replaceChildren(task && (task.title || task.course)
      ? h("span", { class: "task-chip " + courseClass(task.course_key) }, h("i"), [task.course, task.title].filter(Boolean).join(" · "))
      : isBreak(mode) ? h("span", { class: "muted" }, "离开屏幕，喝点水，活动一下。") : h("span", { class: "muted" }, "还没选择任务"));
    if (!picker.editing()) picker.set(currentTask());

    const startButton = button(`开始${MODE_NAMES[mode]}`, async () => {
      primeAlerts();
      try { await timerAction({ action: "start", mode, task: isBreak(mode) ? {} : currentTask() }); timer.mode = null; }
      catch (e) { toast(e.message, true); }
    }, "btn primary big");
    const act = (text, body, cls = "btn big") => button(text, () => timerAction(body).catch(e => toast(e.message, true)), cls);
    if (!a) controls.replaceChildren(startButton);
    else if (isBreak(a.mode)) {
      controls.replaceChildren(a.paused ? act("继续", { action: "resume" }, "btn primary big") : act("暂停", { action: "pause" }),
        act("跳过休息", { action: "stop" }));
    } else {
      controls.replaceChildren(a.paused ? act("继续", { action: "resume" }, "btn primary big") : act("暂停", { action: "pause" }),
        button(a.mode === "stopwatch" ? "停止并记录" : "提前结束", () => stopFocus(), "btn big"),
        button("放弃", () => discardFocus(), "btn ghost big"));
    }
    const every = s.settings.long_every;
    const done = s.today.cycle;
    const inRound = done % every || (done ? every : 0);
    cycle.replaceChildren(h("span", { class: "dots" }, Array.from({ length: every }, (_, i) => h("i", { class: i < inRound ? "on" : "" }))),
      h("span", null, done ? `今天第 ${done} 个番茄已完成` : "今天还没有完成的番茄",
        a ? "" : ` · 下一步：${MODE_NAMES[s.next_mode] || "专注"}`));
    records.replaceChildren(...(s.today.records.length ? s.today.records.map(r => recordLine(r, courseChoices && courseChoices.names))
      : [h("p", { class: "muted small" }, "还没有记录。")]));
    tickView();
  };

  timer.render = render;
  timer.tickView = tickView;
  render();
  return h("div", { class: "focus-page" }, h("div", { class: "focus-grid" }, card, aside));
}

const FOCUS_LENGTHS = [25, 35, 45, 60];

async function setFocusLength(minutes) {
  if (minutes === timer.state.settings.focus_minutes) return;
  try { await timerCall("/focus/settings", { changes: { focus_minutes: minutes } }); }
  catch (e) { toast(e.message, true); }
}

async function customFocusLength(current) {
  const input = h("input", { type: "number", class: "field num-field", step: "5", inputmode: "numeric", "aria-label": "专注分钟数" });
  input.value = current;
  const ok = await confirmDialog("自定义专注时长", [h("label", { class: "form-row inline" }, h("span", null, "每个番茄专注"),
    h("span", null, input, " 分钟")), h("p", { class: "muted small" }, "1 到 180 分钟。之后开始的番茄都用这个时长，可以随时再改。")], "确定");
  const minutes = Number(input.value);
  if (!ok) return;
  if (!Number.isInteger(minutes) || minutes < 1 || minutes > 180) { toast("时长请填 1 到 180 之间的整数分钟。", true); return; }
  setFocusLength(minutes);
}

function renderGoal(box, state, live) {
  const goal = state.settings;
  const seconds = state.today.focus_seconds + live;
  const bar = share => { const i = h("i"); i.style.width = Math.min(100, share * 100) + "%"; return h("div", { class: "bar" }, i); };
  box.replaceChildren(
    h("div", { class: "goal-row" }, h("span", null, "番茄"), h("b", null, `${state.today.pomodoros} / ${goal.goal_pomodoros}`)),
    bar(state.today.pomodoros / goal.goal_pomodoros),
    h("div", { class: "goal-row" }, h("span", null, "专注时间"), h("b", null, `${durText(seconds)} / ${durText(goal.goal_minutes * 60)}`)),
    bar(seconds / (goal.goal_minutes * 60)));
}

function recordStatus(r) {
  if (r.kind === "manual") return chip("补录", "accent");
  if (r.kind === "stopwatch") return chip(r.auto_stopped ? "正计时 · 自动停止" : "正计时");
  if (r.completed) return chip("完成", "ok");
  return chip("中断" + (r.reason ? " · " + REASON_NAMES[r.reason] : ""), "warn");
}

function recordLine(r, names) {
  const task = r.task || {};
  return h("div", { class: "record " + courseClass(task.course_key) },
    h("span", { class: "when" }, `${clockOf(r.start)}–${clockOf(r.end)}`),
    h("span", { class: "what" }, h("i"), task.title || (names ? courseName(names, task.course_key, task.course) : task.course) || "未指定任务",
      task.title && task.course ? h("small", null, task.course) : null),
    h("span", { class: "dur" }, durText(r.focus_seconds)), recordStatus(r));
}

async function stopFocus() {
  const a = timer.state.active;
  if (!a) return;
  if (a.mode === "stopwatch") { timerAction({ action: "stop" }).catch(e => toast(e.message, true)); return; }
  const elapsed = timerElapsed();
  const reason = h("select", { class: "field", "aria-label": "中断原因" }, h("option", { value: "" }, "不填写"),
    REASONS.map(([value, text]) => h("option", { value }, text)));
  const ok = await confirmDialog("提前结束这个番茄？", [
    h("p", null, `已专注 ${durText(elapsed)}。`, elapsed >= 60 ? "这段时间会记录下来，但不算完成的番茄。" : "不到 1 分钟，不会记录。"),
    elapsed >= 60 ? h("label", { class: "form-row" }, "为什么中断（可选，用于统计）", reason) : null], "结束");
  if (!ok) return;
  try {
    const result = await timerAction({ action: "stop", reason: reason.value || null });
    toast(result.saved ? `已记录 ${durText(result.saved.focus_seconds)}。` : "已结束，没有记录。");
  } catch (e) { toast(e.message, true); }
}

async function discardFocus() {
  const ok = await confirmDialog("放弃这次计时？", h("p", null, "这次的时间不会记录。如果只是想提前结束并保留时间，请用“提前结束”。"), "放弃");
  if (ok) timerAction({ action: "discard" }).catch(e => toast(e.message, true));
}

function toggleZen(force) {
  const on = force === undefined ? !document.body.classList.contains("zen") : force;
  document.body.classList.toggle("zen", on);
  document.querySelectorAll(".zen-toggle").forEach(b => { b.textContent = on ? "退出全屏" : "全屏专注"; });
  try {
    if (on && !document.fullscreenElement) document.documentElement.requestFullscreen().catch(() => null);
    if (!on && document.fullscreenElement) document.exitFullscreen().catch(() => null);
  } catch (e) { /* the page-only full view still works */ }
}
document.addEventListener("fullscreenchange", () => { if (!document.fullscreenElement) toggleZen(false); });

// Space starts or pauses on the timer page (not while typing or on a button).
document.addEventListener("keydown", e => {
  if (e.key !== " " || !timer.render || e.target.closest("input, select, textarea, button, a, dialog")) return;
  e.preventDefault();
  const a = timer.state && timer.state.active;
  const controls = $(".controls .btn");
  if (!a && controls) controls.click();
  else if (a) timerAction({ action: a.paused ? "resume" : "pause" }).catch(err => toast(err.message, true));
});

async function focusSettings() {
  const s = timer.state.settings;
  const number = (key, text, unit) => {
    const input = h("input", { type: "number", class: "field num-field", step: "1", inputmode: "numeric", "aria-label": text });
    input.value = s[key];
    return [key, input, h("label", { class: "form-row inline" }, h("span", null, text), h("span", null, input, " ", unit))];
  };
  const check = (key, text) => {
    const input = h("input", { type: "checkbox" });
    input.checked = s[key];
    return [key, input, h("label", { class: "form-row check" }, input, text)];
  };
  const fields = [number("focus_minutes", "专注", "分钟"), number("short_minutes", "短休息", "分钟"),
    number("long_minutes", "长休息", "分钟"), number("long_every", "每完成几个番茄长休息一次", "个"),
    number("goal_pomodoros", "每日目标：番茄", "个"), number("goal_minutes", "每日目标：专注时间", "分钟"),
    check("auto_break", "番茄结束后自动开始休息"), check("auto_focus", "休息结束后自动开始下一个番茄"),
    check("sound", "结束时播放提示音"), check("notify", "结束时显示系统通知（第一次会询问浏览器权限）")];
  const ok = await confirmDialog("番茄钟设置", h("div", { class: "settings-form" }, fields.map(f => f[2])), "保存");
  if (!ok) return;
  const changes = {};
  for (const [key, input] of fields) {
    const value = input.type === "checkbox" ? input.checked : Number(input.value);
    if (value !== s[key]) changes[key] = value;
  }
  if (!Object.keys(changes).length) return;
  try {
    await timerCall("/focus/settings", { changes });
    if (changes.notify) primeAlerts();
    toast("设置已保存。");
  } catch (e) { toast(e.message, true); }
}

// ---------------------------------------------------------------- charts
// Plain HTML marks: every bar is a button with its own tooltip and accessible label.

const chartTip = h("div", { class: "chart-tip", role: "tooltip", hidden: true });
document.body.append(chartTip);
function showTip(target) {
  chartTip.textContent = target.dataset.tip;
  chartTip.hidden = false;
  const box = target.getBoundingClientRect();
  const tip = chartTip.getBoundingClientRect();
  chartTip.style.left = Math.max(8, Math.min(innerWidth - tip.width - 8, box.left + box.width / 2 - tip.width / 2)) + "px";
  chartTip.style.top = Math.max(8, box.top - tip.height - 8) + "px";
}
document.addEventListener("pointerover", e => {
  const target = e.target.closest && e.target.closest("[data-tip]");
  if (target) showTip(target); else chartTip.hidden = true;
});
document.addEventListener("focusin", e => { if (e.target.dataset && e.target.dataset.tip) showTip(e.target); else chartTip.hidden = true; });
document.addEventListener("scroll", () => { chartTip.hidden = true; }, true);

function columnChart(items, { label, tip, onPick, mark }) {
  const most = Math.max(1, ...items.map(i => i.seconds));
  return h("div", { class: "columns" }, items.map((item, index) => {
    const fill = h("i", { class: "fill" + (mark && mark(item) ? " mark" : "") });
    fill.style.height = (item.seconds ? Math.max(2, item.seconds / most * 100) : 0) + "%";
    const text = tip(item);
    return h(onPick ? "button" : "div", { type: onPick ? "button" : null, class: "col", "data-tip": text, "aria-label": text,
      onclick: onPick ? () => onPick(item) : null },
      h("span", { class: "plot" }, fill), h("span", { class: "lab" }, label(item, index) || " "));
  }));
}

function shareRows(rows) {
  const total = rows.reduce((sum, r) => sum + r.value, 0) || 1;
  const most = Math.max(1, ...rows.map(r => r.value));
  return h("div", { class: "hbars" }, rows.map(r => {
    const fill = h("i");
    fill.style.width = Math.max(2, r.value / most * 100) + "%";
    return h("div", { class: "hbar " + (r.cls || ""), "data-tip": `${r.name} · ${r.text}` },
      h("span", { class: "name" }, r.dot === false ? null : h("i", { class: "dot" }), r.name),
      h("span", { class: "track" }, fill),
      h("b", null, r.text), h("small", null, r.percent === false ? "" : Math.round(r.value / total * 100) + "%"));
  }));
}

function weekdayDate(text) { const d = localDay(text); return `${d.getMonth() + 1}/${d.getDate()} ${WEEKDAYS[d.getDay()]}`; }

function heatmap(data, onPick) {
  const goal = data.goal.goal_minutes * 60;
  const start = localDay(data.heatmap.start);
  const grid = h("div", { class: "heat" });
  const months = h("div", { class: "heat-months" });
  const weeks = Math.ceil(data.heatmap.days.length / 7);
  grid.style.gridTemplateColumns = months.style.gridTemplateColumns = `repeat(${weeks}, var(--cell))`;
  let lastMonth = -1;
  let lastLabel = null;
  data.heatmap.days.forEach((seconds, i) => {
    const day = new Date(start);
    day.setDate(start.getDate() + i);
    const iso = isoDate(day);
    const level = !seconds ? 0 : seconds < goal * 0.25 ? 1 : seconds < goal * 0.5 ? 2 : seconds < goal ? 3 : 4;
    const text = `${weekdayDate(iso)} · ${seconds ? durText(seconds) : "没有专注"}`;
    const cell = h("button", { type: "button", class: "cell l" + level, "data-tip": text, "aria-label": text, onclick: () => onPick(iso) });
    cell.style.gridColumn = String(Math.floor(i / 7) + 1);
    cell.style.gridRow = String(i % 7 + 1);
    grid.append(cell);
    if (i % 7 === 0 && day.getMonth() !== lastMonth) {
      lastMonth = day.getMonth();
      const column = Math.floor(i / 7) + 1;
      // A partial first month would collide with the next label: keep the later one.
      if (lastLabel && column - lastLabel.column < 3) lastLabel.span.remove();
      const m = h("span", null, `${day.getMonth() + 1}月`);
      m.style.gridColumn = String(column);
      months.append(m);
      lastLabel = { column, span: m };
    }
  });
  const scroller = h("div", { class: "heat-scroll" }, months, grid);
  requestAnimationFrame(() => { scroller.scrollLeft = scroller.scrollWidth; });
  return [scroller, h("div", { class: "heat-legend" }, "少", [0, 1, 2, 3, 4].map(l => h("i", { class: "cell l" + l })), "多",
    h("span", { class: "muted" }, ` · 最深的格子 = 当天达到 ${durText(goal)} 目标`))];
}

// ---------------------------------------------------------------- page: focus statistics

let statsPeriod = "week";
let statsAnchor = null;
let statsCustom = { start: "", end: "" };
const PERIODS = [["day", "日"], ["week", "周"], ["month", "月"], ["year", "年"], ["custom", "自定义"]];
const PREVIOUS_NAMES = { day: "前一天", week: "上周", month: "上个月", year: "去年", custom: "前一段" };

function viewStats(tab) {
  const tabs = h("div", { class: "tabs" }, [["overview", "概览", "#/stats"], ["history", "记录", "#/stats/history"]].map(([key, text, href]) =>
    h("button", { type: "button", class: key === tab ? "on" : "", onclick: () => { location.hash = href; } }, text)));
  const body = h("div");
  show(h("h1", null, "专注统计"),
    h("p", { class: "muted" }, "来自番茄钟和补录。暂停和休息不算专注；提前结束的番茄只记时长，不算完成的番茄。"), tabs, body);
  if (tab === "history") historyView(body); else statsView(body);
}

function rangeLabel(data) {
  const s = localDay(data.start);
  if (data.period === "day") return planDayLabel(data.start, data.today);
  if (data.period === "month") return `${s.getFullYear()} 年 ${s.getMonth() + 1} 月`;
  if (data.period === "year") return `${s.getFullYear()} 年`;
  return `${shortDate(data.start)} – ${shortDate(data.end)}` + (data.period === "custom" ? `（${s.getFullYear()}）` : "");
}

function shiftAnchor(data, step) {
  const d = localDay(data.anchor);
  if (data.period === "day") d.setDate(d.getDate() + step);
  else if (data.period === "week") d.setDate(d.getDate() + 7 * step);
  else if (data.period === "month") { d.setDate(1); d.setMonth(d.getMonth() + step); }
  else d.setFullYear(d.getFullYear() + step);
  return isoDate(d);
}

function statsView(body) {
  const rerender = () => { body.replaceChildren(); statsView(body); };
  const go = (period, anchor) => { statsPeriod = period; statsAnchor = anchor; rerender(); };
  const nav = h("div", { class: "day-nav" });
  const seg = h("div", { class: "seg" }, PERIODS.map(([key, text]) =>
    h("button", { type: "button", class: key === statsPeriod ? "on" : "", onclick: () => go(key, statsAnchor) }, text)));
  const toolbar = h("div", { class: "toolbar" }, seg, h("span", { class: "grow" }), nav);
  const content = h("div");
  append(body, [toolbar, content]);
  if (statsPeriod === "custom") {
    const today = isoDate(new Date());
    const from = h("input", { type: "date", class: "field", value: statsCustom.start || shiftDays(today, -29), "aria-label": "开始日期" });
    const to = h("input", { type: "date", class: "field", value: statsCustom.end || today, "aria-label": "结束日期" });
    nav.append(from, "–", to, button("查看", () => { statsCustom = { start: from.value, end: to.value }; rerender(); }, "btn tiny"));
    statsCustom = { start: from.value, end: to.value };
  }
  load(content, async () => {
    const query = statsPeriod === "custom"
      ? `period=custom&start=${statsCustom.start}&end=${statsCustom.end}`
      : `period=${statsPeriod}` + (statsAnchor ? "&date=" + statsAnchor : "");
    const [data, choices] = await Promise.all([api("/focus/stats?" + query), focusCourses()]);
    if (statsPeriod !== "custom") {
      const isNow = data.start <= data.today && data.today <= data.end;
      nav.replaceChildren();
      append(nav, [button("‹", () => go(data.period, shiftAnchor(data, -1)), "btn tiny"),
        h("span", { class: "range" }, rangeLabel(data)),
        data.end < data.today ? button("›", () => go(data.period, shiftAnchor(data, 1)), "btn tiny") : null,
        isNow ? null : button("回到现在", () => go(data.period, null), "btn tiny")]);
    }
    return statsBody(data, choices.names, go);
  });
}

function shiftDays(iso, days) { const d = localDay(iso); d.setDate(d.getDate() + days); return isoDate(d); }

function statsBody(data, names, go) {
  if (!data.overall.focus_seconds) {
    return h("div", { class: "welcome" }, notice("还没有专注记录。", "info"),
      h("p", null, "到", h("a", { href: "#/focus" }, "番茄钟"), "开始第一个番茄；忘了计时的学习可以在“记录”里补录。"));
  }
  const k = data.kpis;
  const card = (key, [value, unit], sub, cls = "") => h("div", { class: "stat " + cls }, h("div", { class: "k" }, key),
    h("div", { class: "v" }, value, h("small", null, unit)), sub ? h("div", { class: "sub-k" }, sub) : null);
  const diff = k.focus_seconds - k.previous_seconds;
  const compare = k.previous_seconds || k.focus_seconds
    ? `比${PREVIOUS_NAMES[data.period]}${diff >= 0 ? "多" : "少"} ${durText(Math.abs(diff))}` : null;
  const rate = k.completion_rate === null ? null : `完成率 ${Math.round(k.completion_rate * 100)}%`;
  const parts = [h("section", { class: "stats" },
    card("专注时间", durParts(k.focus_seconds), compare),
    card("完成的番茄", [String(k.pomodoros), "个"], [rate, k.interrupted ? `中断 ${k.interrupted} 次` : null].filter(Boolean).join(" · ") || null),
    data.period === "day"
      ? card("专注次数", [String(k.sessions), "次"], "番茄与正计时，不含补录")
      : card("日均专注", durParts(k.avg_active_day), `按有专注的 ${k.active_days} 天平均（共 ${k.days} 天）`),
    card("最长一次", durParts(k.longest_seconds),
      data.period !== "day" && k.best_day ? `最多的一天：${shortDate(k.best_day)} · ${durText(k.best_day_seconds)}` : null))];

  const title = data.period === "day" ? "每小时专注" : data.period === "year" ? "每月专注" : "每天专注";
  const few = data.buckets.length <= 7;
  const trend = columnChart(data.buckets, {
    label: (b, i) => data.period === "day" ? (i % 3 === 0 ? String(i) : "")
      : data.period === "year" ? `${Number(b.key.slice(5))}月`
      : few ? WEEKDAYS[localDay(b.key).getDay()] : (i === 0 || localDay(b.key).getDate() % 5 === 0 ? String(localDay(b.key).getDate()) : ""),
    tip: b => (data.period === "day" ? `${b.key}:00–${Number(b.key) + 1}:00` : data.period === "year" ? `${b.key.slice(0, 4)} 年 ${Number(b.key.slice(5))} 月` : weekdayDate(b.key))
      + ` · ${b.seconds ? durText(b.seconds) : "没有专注"}` + (b.pomodoros ? ` · ${b.pomodoros} 个番茄` : ""),
    onPick: data.period === "day" ? null : data.period === "year" ? b => go("month", b.key + "-01") : b => go("day", b.key),
    mark: b => b.key === data.today,
  });
  parts.push(h("div", { class: "pcard chart-card" }, h("h3", null, title,
    data.period === "day" ? null : h("span", { class: "muted" }, " · 点柱子看那一" + (data.period === "year" ? "月" : "天"))), trend));

  const row = [];
  if (data.period !== "day") {
    const peak = data.hours.indexOf(Math.max(...data.hours));
    row.push(h("div", { class: "pcard chart-card" }, h("h3", null, "一天中的专注时段",
      data.hours[peak] ? h("span", { class: "muted" }, ` · 最多在 ${peak}–${peak + 1} 点`) : null),
      columnChart(data.hours.map((seconds, hour) => ({ key: String(hour), seconds })), {
        label: (b, i) => i % 6 === 0 ? String(i) : "",
        tip: b => `${b.key}:00–${Number(b.key) + 1}:00 · ${b.seconds ? durText(b.seconds) : "没有专注"}` })));
  }
  row.push(h("div", { class: "pcard" }, h("h3", null, "按课程"),
    data.by_course.length ? shareRows(data.by_course.map(c => ({ name: courseName(names, c.course_key, c.course), value: c.seconds,
      text: durText(c.seconds), cls: courseClass(c.course_key) }))) : h("p", { class: "muted small" }, "这段时间没有专注。")));
  parts.push(h("div", { class: "chart-row" }, row));

  const interrupted = Object.entries(data.reasons).sort((a, b) => b[1] - a[1]);
  parts.push(h("div", { class: "chart-row" },
    h("div", { class: "pcard" }, h("h3", null, "任务排行"),
      data.by_task.length ? h("ol", { class: "rank" }, data.by_task.map(t => h("li", { class: courseClass(t.course_key) },
        h("span", { class: "what" }, h("i"), t.title || "（没写具体任务）",
          h("small", null, courseName(names, t.course_key, t.course), ` · ${t.sessions} 次`)),
        h("b", null, durText(t.seconds))))) : h("p", { class: "muted small" }, "这段时间没有专注。")),
    h("div", { class: "pcard" }, h("h3", null, "中断原因"),
      interrupted.length ? shareRows(interrupted.map(([reason, count]) => ({ name: REASON_NAMES[reason] || "没填原因",
        value: count, text: `${count} 次`, dot: false, cls: "neutral" })))
        : h("p", { class: "muted small" }, "这段时间没有提前结束的番茄。"),
      h("p", { class: "muted small" }, "提前结束番茄时可以选原因；之后也能在“记录”里补填。"))));

  if (data.records) {
    parts.push(h("div", { class: "pcard" }, h("h3", null, "这一天的记录"),
      data.records.length ? data.records.map(r => recordLine(r, names)) : h("p", { class: "muted small" }, "这一天没有记录。")));
  }

  const o = data.overall;
  parts.push(h("div", { class: "pcard" }, h("h3", null, "最近一年"), heatmap(data, day => go("day", day)),
    h("div", { class: "overall" },
      h("div", null, h("b", null, durText(o.focus_seconds)), h("span", null, "累计专注")),
      h("div", null, h("b", null, String(o.pomodoros)), h("span", null, "累计番茄")),
      h("div", null, h("b", null, `${o.streak} 天`), h("span", null, `连续专注（最长 ${o.best_streak} 天）`)),
      h("div", null, h("b", null, `${o.goal_streak} 天`), h("span", null, `连续达标（最长 ${o.best_goal_streak} 天）`)),
      h("div", null, h("b", null, durText(o.avg_active_day)), h("span", null, `日均（${o.active_days} 个有专注的日子）`)))));
  return parts;
}

// ---------------------------------------------------------------- page: focus records

let historyFilter = { start: "", end: "", course: "", q: "" };

async function historyView(body) {
  const choices = await focusCourses();
  const today = isoDate(new Date());
  const from = h("input", { type: "date", class: "field", value: historyFilter.start || shiftDays(today, -29), "aria-label": "开始日期" });
  const to = h("input", { type: "date", class: "field", value: historyFilter.end || today, "aria-label": "结束日期" });
  const course = courseSelect(choices, historyFilter.course, "全部课程");
  const q = h("input", { type: "search", class: "field", placeholder: "搜索任务或备注", value: historyFilter.q, "aria-label": "搜索" });
  const list = h("div");
  const refresh = () => {
    historyFilter = { start: from.value, end: to.value, course: course.value, q: q.value.trim() };
    const params = new URLSearchParams(Object.entries(historyFilter).filter(([, v]) => v));
    load(list, async () => historyTable(await api("/focus/history?" + params), choices, refresh));
  };
  q.addEventListener("keydown", e => { if (e.key === "Enter") refresh(); });
  const file = h("input", { type: "file", accept: ".json,application/json", hidden: true, onchange: () => importRecords(file, refresh) });
  append(body, [h("div", { class: "toolbar filters" }, from, "–", to, course, q, button("查询", refresh, "btn"),
    h("span", { class: "grow" }),
    button("补录…", () => addRecord(choices, refresh), "btn"),
    button("导出备份", exportRecords, "btn ghost"),
    button("导入…", () => file.click(), "btn ghost"), file), list]);
  refresh();
}

function historyTable(result, choices, refresh) {
  if (!result.records.length) return empty("这段时间没有专注记录。");
  return [h("p", { class: "muted small" }, `共 ${result.records.length} 条，合计 ${durText(result.total_seconds)}`,
      result.truncated ? "（只显示最近 500 条，请缩小日期范围）" : ""),
    h("div", { class: "table-wrap" }, h("table", { class: "records" },
      h("thead", null, h("tr", null, ["日期", "时间", "专注", "课程", "任务", "状态", ""].map(t => h("th", null, t)))),
      h("tbody", null, result.records.map(r => {
        const task = r.task || {};
        return h("tr", null,
          h("td", { class: "num" }, weekdayDate(isoDate(new Date(r.start)))),
          h("td", { class: "num" }, `${clockOf(r.start)}–${clockOf(r.end)}`),
          h("td", { class: "num" }, durText(r.focus_seconds)),
          h("td", null, h("span", { class: "course-dot " + courseClass(task.course_key) }, h("i"),
            courseName(choices.names, task.course_key, task.course))),
          h("td", null, task.title || h("span", { class: "muted" }, "—"), r.note ? h("div", { class: "muted small" }, r.note) : null),
          h("td", null, recordStatus(r)),
          h("td", { class: "actions" }, button("编辑", () => editRecord(r, choices, refresh), "btn tiny"), " ",
            button("删除", () => deleteRecord(r, refresh), "btn tiny ghost")));
      }))))];
}

function recordFields(choices, task = {}, note = "") {
  const course = courseSelect(choices, task.course_key);
  const title = h("input", { class: "field", maxlength: "120", value: task.title || "", placeholder: "具体内容（可选）" });
  const memo = h("textarea", { class: "field", maxlength: "500", rows: "2", placeholder: "备注（可选）" });
  memo.value = note || "";
  const value = () => {
    const result = { title: title.value.trim() || null };
    if (course.value) Object.assign(result, { course_key: course.value, course: courseName(choices.names, course.value) });
    if (task.plan_task) Object.assign(result, { plan_date: task.plan_date, plan_task: task.plan_task });
    return result;
  };
  return { value, note: () => memo.value.trim() || null,
    rows: [h("label", { class: "form-row" }, "课程", course), h("label", { class: "form-row" }, "任务", title),
           h("label", { class: "form-row" }, "备注", memo)] };
}

async function addRecord(choices, refresh) {
  const start = new Date(Date.now() - 30 * 60000);
  start.setMinutes(Math.floor(start.getMinutes() / 5) * 5);
  const when = h("input", { type: "datetime-local", class: "field", value: `${isoDate(start)}T${timeLabel(start)}` });
  const minutes = h("input", { type: "number", class: "field num-field", value: "30", step: "5", inputmode: "numeric" });
  const fields = recordFields(choices, currentTask());
  const ok = await confirmDialog("补录专注时间", [
    h("p", { class: "muted small" }, "给忘了计时的学习补一条记录。补录会标为“补录”，计入专注时间，但不算番茄，也不能和已有记录重叠。"),
    h("label", { class: "form-row" }, "开始时间", when), h("label", { class: "form-row" }, "时长（分钟）", minutes), fields.rows], "补录");
  if (!ok) return;
  try {
    await api("/focus/records", { start: when.value, minutes: Number(minutes.value), task: fields.value(), note: fields.note() });
    toast("已补录。");
    refresh();
  } catch (e) { toast(e.message, true); }
}

async function editRecord(record, choices, refresh) {
  const fields = recordFields(choices, record.task, record.note);
  const interrupted = record.kind === "pomodoro" && !record.completed;
  const reason = h("select", { class: "field" }, h("option", { value: "" }, "不填写"), REASONS.map(([value, text]) => h("option", { value }, text)));
  reason.value = record.reason || "";
  const ok = await confirmDialog("编辑记录", [
    h("p", { class: "muted small" }, `${weekdayDate(isoDate(new Date(record.start)))} ${clockOf(record.start)}–${clockOf(record.end)}，专注 ${durText(record.focus_seconds)}。时间不能改，需要的话删除后补录。`),
    fields.rows, interrupted ? h("label", { class: "form-row" }, "中断原因", reason) : null], "保存");
  if (!ok) return;
  const changes = { task: fields.value(), note: fields.note() };
  if (interrupted) changes.reason = reason.value || null;
  try {
    await api("/focus/records/edit", { id: record.id, changes });
    refresh();
  } catch (e) { toast(e.message, true); }
}

async function deleteRecord(record, refresh) {
  const ok = await confirmDialog("删除这条记录？",
    h("p", null, `${weekdayDate(isoDate(new Date(record.start)))} ${clockOf(record.start)}，${durText(record.focus_seconds)}。删除后统计里不再计算，无法恢复（导出过的备份里仍有）。`), "删除");
  if (!ok) return;
  try {
    await api("/focus/records/delete", { id: record.id, confirm: true });
    toast("已删除。");
    refresh();
  } catch (e) { toast(e.message, true); }
}

async function exportRecords() {
  try {
    const data = await api("/focus/export");
    const url = URL.createObjectURL(new Blob([JSON.stringify(data, null, 1)], { type: "application/json" }));
    const link = document.createElement("a");
    link.setAttribute("href", url);
    link.setAttribute("download", `专注记录备份-${isoDate(new Date())}.json`);
    document.body.append(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 10000);
    toast(`已导出 ${data.sessions.length} 条记录（在浏览器的下载文件夹里）。`);
  } catch (e) { toast(e.message, true); }
}

async function importRecords(input, refresh) {
  const chosen = input.files && input.files[0];
  input.value = "";
  if (!chosen) return;
  let data;
  try { data = JSON.parse(await chosen.text()); } catch (e) { toast("这个文件不是有效的 JSON。", true); return; }
  const count = Array.isArray(data && data.sessions) ? data.sessions.length : 0;
  const ok = await confirmDialog("导入专注记录？", [h("p", null, `文件：${chosen.name}，含 ${count} 条记录。`),
    h("p", { class: "muted small" }, "只会添加新的记录；已有的（同一条，或时间重叠的）会跳过，不会覆盖或删除任何记录。")], "导入");
  if (!ok) return;
  try {
    const result = await api("/focus/import", { data, confirm: true });
    const i = result.imported;
    toast(`导入完成：新增 ${i.added} 条，跳过 ${i.skipped} 条` + (i.invalid ? `，${i.invalid} 条格式不对` : "") + "。");
    refresh();
  } catch (e) { toast(e.message, true); }
}

// ---------------------------------------------------------------- page: deadlines

let deadlineDays = 30;
function viewDeadlines() {
  const list = h("div");
  const seg = h("div", { class: "seg" }, [7, 30, 90].map(days =>
    h("button", { type: "button", class: days === deadlineDays ? "on" : "",
      onclick: () => { deadlineDays = days; viewDeadlines(); } }, `${days} 天`)));
  show(h("h1", null, "截止日期"),
       h("p", { class: "muted" }, "Moodle 上所有课程里需要你处理的事项。"),
       h("div", { class: "toolbar" }, seg), list);
  load(list, async () => {
    const events = await api("/deadlines?days=" + deadlineDays);
    if (!events.length) return empty(`接下来 ${deadlineDays} 天没有待办的截止日期 🎉`);
    const names = await titlesByMoodleName();
    const groups = new Map();
    for (const event of events.slice().sort((a, b) => new Date(a.due) - new Date(b.due))) {
      const date = new Date(event.due);
      const key = dayLabel(date);
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push([event, date]);
    }
    return [...groups].map(([label, items]) => h("div", { class: "day" },
      h("div", { class: "day-head" }, label),
      items.map(([event, date]) => {
        const soon = date - new Date() < 48 * 3600000;
        return h("div", { class: "card event" + (soon ? " soon" : "") },
          h("div", { class: "time" }, timeLabel(date)),
          h("div", { class: "body" },
            h("div", { class: "name" }, event.name),
            h("div", { class: "muted small" }, [names.get(event.course) || event.course, event.type ? typeName(event.type) : null]
              .filter(Boolean).join(" · "))),
          h("div", { class: "left" }, remaining(date)),
          event.url ? external(event.url, "打开 ↗") : null);
      })));
  });
}

// ---------------------------------------------------------------- page: courses

let courseWhich = "inprogress";
function viewCourses() {
  const grid = h("div");
  const names = { inprogress: "进行中", future: "将来", past: "已结束", all: "全部" };
  const seg = h("div", { class: "seg" }, Object.entries(names).map(([key, label]) =>
    h("button", { type: "button", class: key === courseWhich ? "on" : "",
      onclick: () => { courseWhich = key; viewCourses(); } }, label)));
  show(h("h1", null, "课程"), h("div", { class: "toolbar" }, seg), grid);
  load(grid, async () => {
    const courses = await courseList(courseWhich);
    if (!courses.length) return empty("这里没有课程。");
    return h("div", { class: "grid" }, courses.map(course => h("div", {
      class: "card course-card", tabindex: "0", role: "link",
      onclick: () => { location.hash = "#/course/" + course.id; },
      onkeydown: e => { if (e.key === "Enter") location.hash = "#/course/" + course.id; },
    },
      h("div", { class: "title" }, course.local ? course.local.title : course.name),
      h("div", { class: "muted small" }, `编号 ${course.id}`),
      h("div", null, course.local
        ? [chip(course.local.folder, "accent"), " ", course.local.artemis_id ? chip("Artemis", "ok") : null,
           " ", course.local.completed ? chip("已完成", "ok") : null]
        : chip("未在课程对照表中")))));
  });
}

// ---------------------------------------------------------------- page: course detail

async function viewCourse(id, tab = "content") {
  const head = h("div", null, h("h1", null, "课程 " + id));
  const tabs = h("div", { class: "tabs" });
  const body = h("div");
  show(h("p", null, h("a", { href: "#/courses" }, "← 所有课程")), head, tabs, body);

  let course = null;
  let entry = null;
  try {
    course = await findCourse(id);
    const map = await courseMap().catch(() => null);
    entry = map ? map.entries.find(e => e.moodle_id === id) : null;
  } catch (e) { /* the tab content will show the error */ }
  head.replaceChildren(
    h("h1", null, entry ? entry.title : course ? course.name : "课程 " + id),
    h("div", { class: "muted small" },
      ["编号 " + id, entry && "本地：" + entry.course_dir].filter(Boolean).join(" · "), " ",
      course ? external(course.url, "在 Moodle 打开 ↗") : null));

  const names = { content: "内容", assignments: "作业" };
  if (entry && entry.artemis_id) names.artemis = "Artemis 课件";
  for (const [key, label] of Object.entries(names)) {
    tabs.append(h("button", { type: "button", class: key === tab ? "on" : "",
      onclick: () => { location.hash = `#/course/${id}/${key}`; } }, label));
  }
  if (tab === "assignments") load(body, () => assignmentsView(id));
  else if (tab === "artemis" && entry && entry.artemis_id) load(body, () => artemisView(entry.artemis_id));
  else load(body, () => contentView(id));
}

async function contentView(id) {
  const activities = await api(`/courses/${id}/activities`);
  if (!activities.length) return empty("这门课没有可见的内容。");
  const sections = new Map();
  for (const a of activities) {
    const key = a.section_number;
    if (!sections.has(key)) sections.set(key, { title: a.section || `第 ${key} 节`, items: [] });
    sections.get(key).items.push(a);
  }
  return [...sections.values()].map((section, index) => h("details", { class: "section", open: index < 3 },
    h("summary", null, section.title, " ", h("span", { class: "muted small" }, `（${section.items.length}）`)),
    section.items.map(a => activityRow(a, id))));
}

function activityRow(a, courseId) {
  if (a.type === "label") {
    return h("div", { class: "activity label" + (a.available ? "" : " off") },
      h("span", { class: "type" }, chip("说明")), h("span", { class: "name" }, a.name || "（空白说明）"));
  }
  const row = h("div", { class: "activity" + (a.available ? "" : " off") });
  const files = h("div", { class: "files" });
  files.hidden = true;
  const actions = [];
  if (!a.available) actions.push(chip("未开放", "warn"));
  else if (a.url && (a.type === "resource" || a.type === "folder")) {
    actions.push(button("文件", () => {
      files.hidden = !files.hidden;
      if (!files.hidden && !files.dataset.loaded) {
        files.dataset.loaded = "1";
        load(files, async () => {
          const list = await api("/files", { url: a.url });
          if (!list.length) return h("div", { class: "muted small" }, "没有可下载的文件。");
          return list.map(file => h("div", { class: "file" },
            h("span", { class: "fname" }, file.name),
            button("下载", () => downloadFile(file, courseId), "btn tiny")));
        });
      }
    }, "btn tiny"));
  } else if (a.url) {
    actions.push(button("阅读", () => readPage(a.url, a.name, courseId), "btn tiny"));
  }
  append(row, [
    h("span", { class: "type" }, chip(typeName(a.type), a.type === "assign" ? "accent" : "")),
    h("span", { class: "name" }, a.name),
    actions,
    a.url ? external(a.url, "↗") : null,
    files,
  ]);
  return row;
}

const SUBMISSION = { submitted: ["已提交", "ok"], draft: ["草稿", "warn"], new: ["未提交", "danger"], unknown: ["无法识别", ""] };
const GRADING = { graded: ["已评分", "ok"], notgraded: ["未评分", ""], unknown: ["无法识别", ""] };

async function assignmentsView(id) {
  const rows = await api(`/courses/${id}/assignments`);
  const intro = notice("作业页面的解析还没有用真实的 TUM 作业核对过；“无法识别”不代表没交，请以 Moodle 页面为准。", "warn");
  if (!rows.length) return [intro, empty("这门课没有作业。")];
  return [intro, h("div", { class: "table-wrap" }, h("table", null,
    h("thead", null, h("tr", null, ["作业", "截止", "提交", "评分", "分数", ""].map(t => h("th", null, t)))),
    h("tbody", null, rows.map(r => {
      const sub = SUBMISSION[r.submission] || SUBMISSION.unknown;
      const grade = GRADING[r.grading] || GRADING.unknown;
      const unread = !r.fetched;
      return h("tr", null,
        h("td", null, r.name, r.error ? h("div", { class: "small", style: null }, chip(r.error, "danger")) : null),
        h("td", { class: "num" }, r.due || (unread ? "—" : "")),
        h("td", null, unread ? chip(r.available ? "未读取" : "未开放", "warn") : chip(sub[0], sub[1])),
        h("td", null, unread ? "" : chip(grade[0], grade[1])),
        h("td", { class: "num" }, r.grade || ""),
        h("td", null, r.url && r.available ? button("详情", () => readPage(r.url, r.name, id), "btn tiny") : null));
    }))))];
}

async function artemisView(artemisId) {
  const lectures = await api(`/artemis/${artemisId}/lectures`);
  const intro = h("p", { class: "muted small" }, "来自 artemis.tum.de。第一次使用时会登录 Artemis，Artemis 可能给你发一封“新登录”提醒邮件。");
  if (!lectures.length) return [intro, empty("没有可见的讲座。")];
  return [intro, lectures.map(lecture => h("div", { class: "card" },
    h("div", null, h("strong", null, lecture.title), " ",
      h("span", { class: "muted small" }, dateText(lecture.start_date)),
      lecture.is_tutorial ? [" ", chip("练习课")] : null),
    lecture.attachments.length
      ? lecture.attachments.map(att => h("div", { class: "file" },
          h("span", { class: "fname" }, att.name, " ", h("span", { class: "muted small" }, att.filename || "")),
          att.url ? button("下载", () => downloadFile({ name: att.filename || att.name, url: att.url }, artemisId, true), "btn tiny")
                  : external(att.external_url, "外部链接 ↗")))
      : h("div", { class: "muted small" }, "没有附件。")))];
}

// ---------------------------------------------------------------- page: Artemis exercises
// The ticks are the user's own ("I have done this"), stored locally and independent of the
// Artemis score. Repository URLs come from Artemis; this page never starts a participation.

const EXERCISE_TYPES = { programming: "编程", quiz: "测验", modeling: "建模", text: "文字", "file-upload": "上传" };
const EXERCISE_COUNTS = { graded: ["计分", ""], bonus: ["加分", "accent"], not_graded: ["不计分", ""] };
const DIFFICULTY = { EASY: "简单", MEDIUM: "中等", HARD: "困难" };
const exerciseView = { filter: "open", type: "programming" };

async function copyText(text, what) {
  try {
    await navigator.clipboard.writeText(text);
    toast(`已复制${what}。`);
  } catch (e) {
    toast("浏览器不让复制，请手动选中地址复制。", true);
  }
}

function repoLine(label, url, primary) {
  return h("div", { class: "repo" + (primary ? " primary" : "") },
    h("span", { class: "repo-label" }, label),
    h("code", { class: "repo-url" }, url),
    button("复制地址", () => copyText(url, label + "地址"), "btn tiny"),
    button("复制 git clone", () => copyText("git clone " + url, " git clone 命令"), "btn tiny"));
}

function exerciseRepos(ex) {
  if (ex.type !== "programming") return null;
  if (ex.repositories_error) return h("div", { class: "muted small" }, ex.repositories_error);
  const { practice, graded } = ex.repositories || {};
  const lines = [];
  if (practice) lines.push(repoLine("练习仓库", practice, true));
  else if (ex.status === "closed") {
    lines.push(h("div", { class: "muted small" }, "还没有练习仓库：在 Artemis 打开这道题，点「Practice / 练习」开始后，回来点「刷新」。"));
  }
  if (graded) lines.push(repoLine("正式仓库", graded, !practice));
  return lines.length ? h("div", { class: "repos" }, lines) : null;
}

// Sorted by release date (start date if none). Groups are release weeks (Monday to Sunday,
// Berlin time), since one lecture's exercises come out within the same week. Titles follow
// "<kind><lesson>E<number> - name" (T = tutorial example, H = homework, P = practice,
// L = lecture quiz, two digits = lesson); a week is labelled with the lesson most of its
// exercises name, so a mistyped number (an "H01" released in week 2) still lands in its week.
// Exercises released at the same moment: T before H before P before L, then by number.
// Without any date an exercise goes last ("发布时间不明") in the server's due-date order.
const EXERCISE_KIND_ORDER = "THPL";
function exerciseCode(title) {
  const match = /^\s*([A-Za-z])(\d{1,2})E(\d{1,2})(?!\d)/i.exec(String(title || ""));
  if (!match) return null;
  return { kind: match[1].toUpperCase(), lesson: Number(match[2]), number: Number(match[3]) };
}
function exerciseReleased(ex) {
  const date = new Date(ex.release_date || ex.start_date || "");
  return isNaN(date) ? null : date;
}
function releaseWeek(date) {
  const day = berlinDate(date);
  const [y, m, d] = day.split("-").map(Number);
  const weekday = (new Date(Date.UTC(y, m - 1, d)).getUTCDay() + 6) % 7;  // Monday = 0
  return new Date(Date.UTC(y, m - 1, d - weekday)).toISOString().slice(0, 10);
}
function weekLessons(exercises) {
  const counts = new Map();
  for (const ex of exercises) {
    const released = exerciseReleased(ex), code = exerciseCode(ex.title);
    if (!released || !code) continue;
    const week = releaseWeek(released);
    if (!counts.has(week)) counts.set(week, new Map());
    const tally = counts.get(week);
    tally.set(code.lesson, (tally.get(code.lesson) || 0) + 1);
  }
  const lessons = new Map();
  for (const [week, tally] of counts) {
    lessons.set(week, [...tally].sort((a, b) => b[1] - a[1] || a[0] - b[0])[0][0]);
  }
  return lessons;
}
function exerciseGroups(exercises, all = exercises) {
  const kindRank = kind => { const i = EXERCISE_KIND_ORDER.indexOf(kind); return i < 0 ? EXERCISE_KIND_ORDER.length : i; };
  const rows = exercises.map((ex, index) => ({ ex, index, released: exerciseReleased(ex), code: exerciseCode(ex.title) }));
  const dated = rows.filter(r => r.released), undated = rows.filter(r => !r.released);
  const rank = r => r.code ? [kindRank(r.code.kind), r.code.kind, r.code.number] : [EXERCISE_KIND_ORDER.length + 1, "", 0];
  dated.sort((a, b) => {
    const [ak, akind, an] = rank(a), [bk, bkind, bn] = rank(b);
    return a.released - b.released || ak - bk || akind.localeCompare(bkind) || an - bn || a.index - b.index;
  });
  const lessons = weekLessons(all);
  const groups = [];
  for (const { ex, released } of dated) {
    const week = releaseWeek(released), last = groups[groups.length - 1];
    if (last && last.week === week) last.items.push(ex);
    else groups.push({ week, lesson: lessons.has(week) ? lessons.get(week) : null, items: [ex] });
  }
  if (undated.length) groups.push({ week: null, lesson: null, items: undated.map(r => r.ex) });
  return groups;
}
function exerciseGroupTitle(group) {
  if (!group.week) return "发布时间不明";
  const last = new Date(Date.parse(group.week + "T00:00:00Z") + 6 * 86400000).toISOString().slice(0, 10);
  const span = `${shortDate(group.week)}–${shortDate(last)} 发布`;
  return group.lesson === null ? span : `第 ${group.lesson} 课 · ${span}`;
}

function exerciseRow(ex, courseId, onChange) {
  const box = h("input", { type: "checkbox", class: "ex-check", "aria-label": "我已完成：" + ex.title });
  box.checked = !!ex.checked;
  const row = h("div", { class: "card ex-row" + (ex.checked ? " done" : "") });
  box.addEventListener("change", async () => {
    const done = box.checked;
    box.disabled = true;
    try {
      const result = await api("/artemis/check", { course: courseId, exercise: ex.id, done, title: ex.title });
      ex.checked = Object.prototype.hasOwnProperty.call(result.checked, String(ex.id));
      ex.checked_at = result.checked[String(ex.id)] || null;
    } catch (e) {
      toast(e.message, true);
    }
    box.checked = !!ex.checked;
    box.disabled = false;
    row.classList.toggle("done", !!ex.checked);
    onChange();
  });
  const counts = EXERCISE_COUNTS[ex.counts];
  const due = ex.due_date ? new Date(ex.due_date) : null;
  const released = exerciseReleased(ex);
  const facts = [
    released ? `发布 ${dayLabel(released)} ${timeLabel(released)}` : null,
    due ? `截止 ${dayLabel(due)} ${timeLabel(due)}` : "没有截止时间",
    ex.status === "not_released" ? "还没开放" : null,
    ex.score !== null && ex.score !== undefined ? `Artemis 得分 ${ex.score}%` : null,
    ex.practice_score !== null && ex.practice_score !== undefined ? `练习得分 ${ex.practice_score}%` : null,
    ex.checked && ex.checked_at ? `勾选于 ${dateText(ex.checked_at)}` : null,
  ].filter(Boolean).join(" · ");
  append(row, [
    h("label", { class: "ex-box" }, box),
    h("div", { class: "ex-body" },
      h("div", { class: "ex-head" },
        h("span", { class: "ex-title" }, ex.title), " ",
        external(ex.url, "Artemis ↗")),
      h("div", { class: "ex-chips" },
        chip(EXERCISE_TYPES[ex.type] || ex.type || "其他"),
        ex.difficulty ? chip(DIFFICULTY[ex.difficulty] || ex.difficulty) : null,
        counts ? chip(counts[0], counts[1]) : null,
        (ex.categories || []).map(c => chip(c)),
        ex.status === "open" ? chip("进行中", "warn") : null),
      h("div", { class: "muted small" }, facts),
      exerciseRepos(ex)),
  ]);
  return row;
}

async function artemisCourseNames() {
  try {
    const map = await courseMap();
    return new Map(map.entries.filter(e => e.artemis_id).map(e => [e.artemis_id, e.title]));
  } catch (e) { return new Map(); }
}

async function viewExercises(courseId) {
  const tabs = h("div", { class: "tabs ex-tabs" });
  const body = h("div");
  show(h("h1", null, "Artemis 练习"),
       h("p", { class: "muted" }, "自己做完一道就勾上：勾选只记在这台电脑（学习计划文件夹的 Artemis练习.json），和 Artemis 的分数无关。",
         "练习仓库要先在 Artemis 网页上点「Practice」开始才会有。"),
       tabs, body);
  let courses;
  try {
    courses = await api("/artemis/courses");
  } catch (e) { body.replaceChildren(notice(e.message)); return; }
  if (!courses.length) { body.replaceChildren(empty("Artemis 上没有课程。")); return; }
  const names = await artemisCourseNames();
  const current = courses.find(c => c.id === courseId) || courses[0];
  append(tabs, courses.map(c => h("button", { type: "button", class: c.id === current.id ? "on" : "",
    onclick: () => { location.hash = "#/artemis/" + c.id; } }, names.get(c.id) || c.title)));
  const refresh = () => load(body, () => exerciseList(current.id));
  refresh();
}

async function exerciseList(courseId) {
  const data = await api(`/artemis/${courseId}/exercises`);
  const all = data.exercises;
  const list = h("div");
  const summary = h("span", { class: "muted small" });
  const segment = (key, options) => h("div", { class: "seg" }, options.map(([value, label]) =>
    h("button", { type: "button", class: exerciseView[key] === value ? "on" : "",
      onclick: e => { exerciseView[key] = value; e.target.parentNode.querySelectorAll("button")
        .forEach(b => b.classList.toggle("on", b === e.target)); draw(); } }, label)));
  function draw() {
    const shown = all.filter(ex => (exerciseView.type === "all" || ex.type === exerciseView.type)
      && (exerciseView.filter === "all" || (exerciseView.filter === "open") === !ex.checked));
    const pool = all.filter(ex => exerciseView.type === "all" || ex.type === exerciseView.type);
    summary.textContent = `已勾 ${pool.filter(ex => ex.checked).length} / ${pool.length}`;
    list.replaceChildren();
    if (!shown.length) {
      append(list, [empty(exerciseView.filter === "open" && pool.length ? "这里的都勾完了 🎉" : "这里没有练习。")]);
      return;
    }
    const onChange = () => { summary.textContent = `已勾 ${pool.filter(e => e.checked).length} / ${pool.length}`; };
    append(list, exerciseGroups(shown, all).map(group => h("section", { class: "ex-group" },
      h("h2", { class: "ex-group-head" }, exerciseGroupTitle(group),
        h("span", { class: "muted small" }, ` · ${group.items.length} 道`)),
      group.items.map(ex => exerciseRow(ex, courseId, onChange)))));
  }
  draw();
  return [
    data.checks_error ? notice(data.checks_error) : null,
    h("div", { class: "toolbar" },
      segment("filter", [["open", "没勾的"], ["done", "已勾的"], ["all", "全部"]]),
      segment("type", [["programming", "编程题"], ["all", "所有类型"]]),
      h("span", { class: "grow" }), summary,
      button("刷新", () => viewExercises(courseId), "btn tiny")),
    list,
  ];
}

// ---------------------------------------------------------------- page: grades

function viewGrades() {
  const body = h("div");
  show(h("h1", null, "成绩"), h("p", { class: "muted" }, "各门课在 Moodle 上的总成绩。正式成绩请到 TUMonline 查。"), body);
  load(body, async () => {
    const rows = await api("/grades");
    if (!rows.length) return empty("还没有成绩。");
    const titles = await titlesById();
    return h("div", { class: "table-wrap" }, h("table", null,
      h("thead", null, h("tr", null, h("th", null, "课程"), h("th", null, "总成绩"))),
      h("tbody", null, rows.map(r => h("tr", null,
        h("td", null, titles.get(r.course_id) || r.course_name), h("td", { class: "num" }, r.grade || h("span", { class: "muted" }, "—")))))));
  });
}

// ---------------------------------------------------------------- page: sync

const KIND_LABELS = { new: ["新增", "accent"], updated: ["更新", "warn"], unchanged: ["未变", ""],
                      removed_online: ["线上已删除", ""], failed: ["失败", "danger"], local_modified: ["本地已变更", "warn"],
                      trashed: ["移到回收站", ""], kept_local: ["保留你改过的", "warn"], renamed: ["改名", ""] };
let syncRows = [];
let syncBusy = false;

async function viewSync(refresh = false) {
  const body = h("div");
  show(h("h1", null, "同步中心"),
       h("p", { class: "muted" }, "按课程对照表，把 Moodle 和 Artemis 上的资料同步到每门课的 Official 文件夹。先预览，确认后才下载；本地和线上保持一致：更新后的旧版和线上已删除的文件移到回收站，你改过（比如批注过）的文件会另存为「名字 (批注)」保留。"),
       body);
  load(body, async () => {
    const map = await courseMap(refresh);
    if (map.error) return [notice(map.error), h("p", { class: "muted small" }, "对照表应放在：" + map.map_path)];
    const previous = new Map(syncRows.map(r => [r.key, r]));
    syncRows = [];
    for (const e of map.entries) {
      if (e.moodle_id) syncRows.push({ key: "moodle:" + e.moodle_id, source: "moodle", course: e.moodle_id, entry: e,
                                       dest: e.moodle_dest, exists: e.moodle_dest_exists });
      if (e.artemis_id) syncRows.push({ key: "artemis:" + e.artemis_id, source: "artemis", course: e.artemis_id, entry: e,
                                        dest: e.artemis_dest, exists: e.artemis_dest_exists });
    }
    for (const row of syncRows) {
      const old = previous.get(row.key);
      if (old) Object.assign(row, { dest: old.dest, result: old.result, error: old.error, checked: old.checked });
      else row.checked = !row.entry.completed;
    }
    if (!syncRows.length) return empty("对照表里没有填 Moodle 或 Artemis 编号的课程。");
    return syncTable();
  });
}

function syncTable() {
  const table = h("tbody");
  const all = h("input", { type: "checkbox", "aria-label": "全选", onchange: e => {
    syncRows.forEach(r => { r.checked = e.target.checked; });
    table.querySelectorAll("input[type=checkbox]").forEach(box => { box.checked = e.target.checked; });
  } });
  all.checked = syncRows.every(r => r.checked);
  for (const row of syncRows) {
    const counts = h("div", { class: "counts" });
    const detail = h("tr", { class: "sync-detail" });
    detail.hidden = true;
    const box = h("input", { type: "checkbox", "aria-label": "选中", onchange: e => { row.checked = e.target.checked; } });
    box.checked = !!row.checked;
    const input = h("input", { class: "path-input", value: row.dest, title: row.dest, "aria-label": "目标文件夹",
      onchange: e => { row.dest = e.target.value.trim(); input.title = row.dest; row.result = null; renderCounts(); } });
    // Long paths: show the end (course folder\Official\…), which is the part that differs.
    requestAnimationFrame(() => { input.scrollLeft = input.scrollWidth; });
    const renderCounts = () => {
      counts.replaceChildren();
      if (row.running) counts.append(spinner(row.running));
      else if (row.error) counts.append(chip("出错：" + row.error, "danger"));
      else if (row.result) {
        for (const [kind, [label, cls]] of Object.entries(KIND_LABELS)) {
          const n = row.result[kind] ? row.result[kind].count : 0;
          if (n || kind === "new" || kind === "updated") counts.append(chip(`${label} ${n}`, n ? cls : ""));
        }
        if (!row.result.dry_run) counts.append(chip("已同步", "ok"));
        counts.append(button(detail.hidden ? "明细" : "收起", () => { detail.hidden = !detail.hidden; renderCounts(); }, "btn tiny"));
        detail.replaceChildren(h("td", { colspan: "5" }, Object.entries(KIND_LABELS).map(([kind, [label]]) => {
          const files = (row.result[kind] || {}).files || [];
          return files.length ? [h("strong", null, `${label}（${files.length}）`), h("ul", null, files.map(f => h("li", null, f)))] : null;
        }), (row.result.errors || []).length
          ? [h("strong", null, "错误"), h("ul", null, row.result.errors.map(e => h("li", null, `${e.path}：${e.error}`)))] : null));
      } else counts.append(h("span", { class: "muted small" }, "尚未预览"));
    };
    row.render = renderCounts;
    renderCounts();
    table.append(h("tr", { class: "sync-row" },
      h("td", null, box),
      h("td", null, h("div", null, row.entry.folder), h("div", { class: "muted small" }, row.entry.title),
        chip(row.source === "moodle" ? "Moodle " + row.course : "Artemis " + row.course, row.source === "moodle" ? "accent" : "ok"),
        row.entry.completed ? [" ", chip("已完成")] : null),
      h("td", null, input, h("div", { class: "small " + (row.exists ? "muted" : "") }, row.exists ? "文件夹已存在" : "首次同步时会新建")),
      h("td", null, counts),
      h("td", null, button("预览", () => runSync([row], true), "btn tiny"), " ",
        button("打开", () => openFolder(row.dest), "btn tiny"))), detail);
  }
  return [
    h("div", { class: "toolbar" },
      button("预览选中", () => runSync(syncRows.filter(r => r.checked), true), "btn"),
      button("同步选中…", () => confirmSync(syncRows.filter(r => r.checked)), "btn primary"),
      h("span", { class: "grow" }),
      button("重新读取对照表", () => viewSync(true), "btn ghost")),
    h("div", { class: "table-wrap" }, h("table", null,
      h("thead", null, h("tr", null, h("th", null, all), h("th", null, "课程"), h("th", null, "目标文件夹"),
        h("th", null, "结果"), h("th", null, ""))),
      table)),
  ];
}

async function runSync(rows, dryRun) {
  if (syncBusy) { toast("已有同步在进行，请等它完成。", true); return; }
  if (!rows.length) { toast("请先勾选课程。", true); return; }
  syncBusy = true;
  document.querySelectorAll("#main .toolbar .btn").forEach(b => { b.disabled = true; });
  try {
    for (const row of rows) {
      row.running = dryRun ? "正在预览…" : "正在同步…";
      row.error = null;
      row.render();
      statusText = `${dryRun ? "正在预览" : "正在同步"}：${row.entry.folder}（${row.source === "moodle" ? "Moodle" : "Artemis"}）`;
      try {
        row.result = await api("/sync", { source: row.source, course: row.course, dest_dir: row.dest,
                                          dry_run: dryRun, confirm: !dryRun });
        if (!dryRun) row.exists = true;
      } catch (e) {
        row.result = null;
        row.error = e.message;
      }
      row.running = null;
      row.render();
    }
  } finally {
    statusText = "";
    syncBusy = false;
    document.querySelectorAll("#main .toolbar .btn").forEach(b => { b.disabled = false; });
  }
  const failed = rows.filter(r => r.error || (r.result && r.result.failed && r.result.failed.count)).length;
  toast(failed ? `${dryRun ? "预览" : "同步"}完成，其中 ${failed} 项有错误，请看结果列。` : `${dryRun ? "预览" : "同步"}完成。`, failed > 0);
}

async function confirmSync(rows) {
  if (!rows.length) { toast("请先勾选课程。", true); return; }
  const items = rows.map(r => {
    const res = r.result && r.result.dry_run ? r.result : null;
    const summary = res ? `新增 ${res.new.count}，更新 ${res.updated.count}` : "尚未预览";
    return h("li", null, `${r.entry.folder}（${r.source === "moodle" ? "Moodle" : "Artemis"}）：${summary}`,
      h("div", { class: "muted small" }, "→ " + r.dest));
  });
  const previewed = rows.filter(r => r.result && r.result.dry_run);
  const files = previewed.reduce((sum, r) => sum + r.result.new.count + r.result.updated.count, 0);
  const unknown = rows.length - previewed.length;
  const summary = unknown
    ? `选中了 ${rows.length} 项（每门课的 Moodle 或 Artemis 算一项），其中 ${unknown} 项还没预览，不知道有没有新文件。同步只会下载新增和更新的文件。`
    : files
      ? `选中了 ${rows.length} 项（每门课的 Moodle 或 Artemis 算一项），预览一共发现 ${files} 个新增或更新的文件，同步会下载它们。`
      : `预览显示选中的 ${rows.length} 项都没有新文件，现在不需要同步。`;
  const ok = await confirmDialog("开始同步？",
    [h("p", null, summary), h("ul", null, items),
     h("p", { class: "muted small" }, "旧版本和线上已删除的文件移到回收站（可以从回收站恢复）；你改过的文件不会丢。")], "开始同步");
  if (ok) runSync(rows, false);
}

async function openFolder(path) {
  try { await api("/open-folder", { path }); }
  catch (e) { toast(e.message, true); }
}

// ---------------------------------------------------------------- router

function route() {
  const parts = (location.hash.replace(/^#\/?/, "") || "").split("/").filter(Boolean);
  const [page, arg, sub] = parts;
  document.querySelectorAll(".sidebar a").forEach(a => {
    const nav = a.dataset.nav;
    a.classList.toggle("active", nav === (page === "course" ? "courses" : page || "plan"));
  });
  $("#reader").hidden = true;
  timer.render = timer.tickView = null;
  if (page !== "focus") toggleZen(false);
  if (!token()) {
    show(h("h1", null, "TUM 学习小助理"),
         notice("这个页面没有访问权限。请点击桌面上的“TUM 学习小助理”打开（它会自动打开正确的地址）。"));
    return;
  }
  if (!timer.state) timerCall("/focus").catch(() => null);  // the title and sidebar show a running timer everywhere
  if (page === "deadlines") viewDeadlines();
  else if (page === "focus") viewFocus();
  else if (page === "stats") viewStats(arg === "history" ? "history" : "overview");
  else if (page === "plan" && /^\d{4}-\d{2}-\d{2}$/.test(arg || "")) viewPlan(arg);
  else if (page === "courses") viewCourses();
  else if (page === "course" && /^\d+$/.test(arg || "")) viewCourse(Number(arg), sub);
  else if (page === "grades") viewGrades();
  else if (page === "artemis") viewExercises(/^\d+$/.test(arg || "") ? Number(arg) : null);
  else if (page === "sync") viewSync();
  else viewPlan();
  $("#main").focus();
}

window.addEventListener("hashchange", route);
route();
