/* 左侧边栏 UI（AgentSidebarUI）。
 *
 * 职责：
 * - 视图切换：主区在「对话 / 执行报告 / 图表 / 代码沙箱 / 数据审计」间切换，
 *   主对话页面只用于对话，产物经侧边栏切换查看；
 * - 产物徽标：各产物视图的条目计数（新产物入账即亮起）；
 * - 知识目录：语义目录字段清单（懒加载弹出面板）；
 * - 会话历史：本机 localStorage 最近 20 条，点击回填输入框并回到对话视图；
 * - 新对话：中断当前流并重置工作区（store/对话流/画布/徽标）。
 */
(function () {
  "use strict";

  var THREAD_KEY = "dataagent_threads";
  var MAX_THREADS = 20;

  var VIEW_TITLES = {
    chat: "对话",
    report: "执行报告",
    charts: "图表",
    code: "代码沙箱",
    data: "数据审计"
  };
  var ARTIFACT_BADGES = { report: "reports", charts: "charts", code: "codes", data: "tables" };
  var currentView = "chat";

  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  }

  function fmtTime(ts) {
    var d = new Date(ts);
    return (d.getMonth() + 1) + "/" + d.getDate() + " " +
      String(d.getHours()).padStart(2, "0") + ":" + String(d.getMinutes()).padStart(2, "0");
  }

  // ---------------------------------------------------------------- 视图切换
  function showView(name) {
    if (!VIEW_TITLES[name]) { return; }
    currentView = name;
    document.querySelectorAll(".side-nav-item").forEach(function (btn) {
      btn.classList.toggle("active", btn.dataset.view === name);
    });
    document.querySelectorAll(".main-view").forEach(function (v) {
      v.classList.toggle("active", v.id === "view-" + name);
    });
    var title = document.getElementById("view-title");
    if (title) { title.textContent = VIEW_TITLES[name]; }
    // 导出按钮只在产物视图出现
    var exportRow = document.getElementById("export-row");
    if (exportRow) { exportRow.classList.toggle("hidden", name === "chat"); }
    // 图表视图激活时 resize（隐藏容器初始化尺寸为 0 的情况）
    if (name === "charts" && window.AgentCanvas) { AgentCanvas.resizeCharts(); }
  }

  // ---------------------------------------------------------------- 产物徽标
  function renderBadges(state) {
    var arts = state.currentArtifacts || {};
    Object.keys(ARTIFACT_BADGES).forEach(function (view) {
      var n = (arts[ARTIFACT_BADGES[view]] || []).length;
      var el = document.getElementById("badge-" + view);
      if (!el) { return; }
      el.classList.toggle("hidden", !n);
      el.textContent = n > 99 ? "99+" : String(n);
    });
  }

  /** Agent 等待用户输入时，在「对话」项上挂提示圆点。 */
  function renderChatDot(state) {
    var dot = document.getElementById("nav-chat-dot");
    if (dot) { dot.classList.toggle("hidden", state.agentStatus !== "awaiting"); }
  }

  // ---------------------------------------------------------------- 线程存储
  function loadThreads() {
    try { return JSON.parse(localStorage.getItem(THREAD_KEY) || "[]"); } catch (e) { return []; }
  }
  function saveThreads(list) {
    try { localStorage.setItem(THREAD_KEY, JSON.stringify(list.slice(0, MAX_THREADS))); } catch (e) { /* 忽略 */ }
  }
  function recordThread(query, eventCount) {
    var list = loadThreads().filter(function (t) { return t.q !== query; });
    list.unshift({ q: query, ts: Date.now(), events: eventCount || 0 });
    saveThreads(list);
    renderThreads();
  }
  function removeThread(ts) {
    saveThreads(loadThreads().filter(function (t) { return t.ts !== ts; }));
  }

  // ---------------------------------------------------------------- 历史列表
  function renderThreads() {
    var body = document.getElementById("thread-list");
    var threads = loadThreads();
    if (!threads.length) {
      body.innerHTML = '<div class="hp-empty">暂无历史会话</div>';
      return;
    }
    body.innerHTML = "";
    threads.forEach(function (t) {
      var item = document.createElement("div");
      item.className = "thread-item";
      item.innerHTML = '<span class="t-q" title="' + esc(t.q) + '">' + esc(t.q) + "</span>"
        + '<span class="t-time">' + fmtTime(t.ts) + "</span>"
        + '<button type="button" class="t-del" title="删除该条">✕</button>';
      item.querySelector(".t-q").addEventListener("click", function () {
        showView("chat");
        var input = document.getElementById("query");
        input.value = t.q;
        input.focus();
      });
      item.querySelector(".t-del").addEventListener("click", function (e) {
        e.stopPropagation();
        removeThread(t.ts);
        renderThreads();
      });
      body.appendChild(item);
    });
  }

  // ---------------------------------------------------------------- 知识目录（Schema）
  var schemaLoaded = false;

  function renderSchema(data) {
    var body = document.getElementById("schema-panel-body");
    if (!data || !data.tables || !data.tables.length) {
      body.innerHTML = '<div class="hp-empty">语义目录为空</div>';
      return;
    }
    var html = "";
    data.tables.forEach(function (t) {
      html += '<div class="schema-table-group">'
        + '<div class="schema-table-name">▤ ' + esc(t.table)
        + '<span class="t-count">' + t.fields.length + " 字段</span></div>";
      t.fields.forEach(function (f) {
        html += '<div class="schema-field">'
          + '<span class="f-logical">' + esc(f.field) + "</span>"
          + '<span class="f-column">' + esc(f.column) + "</span>"
          + '<span class="f-dtype">' + esc(f.dtype) + "</span></div>";
      });
      html += "</div>";
    });
    body.innerHTML = html;
  }

  function loadSchema() {
    if (schemaLoaded) { return; }
    var headers = {};
    var token = sessionStorage.getItem("dataagent_token") || localStorage.getItem("dataagent_token") || "";
    if (token) { headers.Authorization = "Bearer " + token; }
    var sid = sessionStorage.getItem("dataagent_sid") || localStorage.getItem("dataagent_session") || "";
    if (sid) { headers["X-Session-ID"] = sid; }
    fetch("/api/schema/summary", { headers: headers })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (data) {
        schemaLoaded = true;
        renderSchema(data);
      })
      .catch(function () {
        document.getElementById("schema-panel-body").innerHTML =
          '<div class="hp-empty">加载失败，请稍后重试</div>';
      });
  }

  function closeSchema() {
    document.getElementById("schema-panel").classList.add("hidden");
    document.getElementById("schema-btn").setAttribute("aria-expanded", "false");
  }

  // ---------------------------------------------------------------- 新对话
  function newThread() {
    if (window.__activeStream) { window.__activeStream.abort(); }
    AgentStore.reset();
    if (window.AgentStreamUI) { AgentStreamUI.resetScroll(); }
    if (window.AgentCanvas) { AgentCanvas.reset(); }
    showView("chat");
    var input = document.getElementById("query");
    input.value = "";
    input.focus();
    closeSchema();
  }

  // ---------------------------------------------------------------- 入口
  function init() {
    // 视图切换
    document.querySelectorAll(".side-nav-item").forEach(function (btn) {
      btn.addEventListener("click", function () { showView(btn.dataset.view); });
    });

    // 产物徽标 + 等待输入圆点
    AgentStore.subscribe("currentArtifacts", renderBadges);
    AgentStore.subscribe("agentStatus", renderChatDot);

    // 知识目录弹出面板
    var schemaBtn = document.getElementById("schema-btn");
    schemaBtn.addEventListener("click", function (e) {
      e.stopPropagation();
      var panel = document.getElementById("schema-panel");
      var open = panel.classList.contains("hidden");
      panel.classList.toggle("hidden", !open);
      schemaBtn.setAttribute("aria-expanded", open ? "true" : "false");
      if (open) { loadSchema(); }
    });
    document.addEventListener("click", function (e) {
      if (!e.target.closest(".side-pop-anchor")) { closeSchema(); }
    });
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape") { closeSchema(); }
    });

    document.getElementById("new-thread-btn").addEventListener("click", newThread);

    renderThreads();
  }

  window.AgentSidebarUI = {
    init: init,
    showView: showView,
    recordThread: recordThread,
    newThread: newThread
  };
})();
