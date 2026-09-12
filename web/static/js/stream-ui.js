/* 对话流渲染（AgentChatStream，含 Agent 活动时间线）。
 *
 * 单列表渲染：store.timelineEvents 全部条目按 kind 渲染进中央对话流——
 *   对话语义：user（右侧气泡）/ hitl（澄清卡）/ done（完成卡）/ error（错误卡）；
 *   执行语义：plan / tool / reflection 以时间线活动行呈现（左竖线 + 节点圆点，
 *   工具行默认收起、点击展开 DSL/代码/输出；计划节点默认展开步骤清单）。
 *
 * 虚拟化模型：超过 VIRTUALIZE_THRESHOLD 条后仅渲染可视窗口 ±BUFFER 的条目，
 * 窗口外用上下 spacer 撑起滚动高度；条目以 uid 键控协调（重规划替换 plan 不串位），
 * 高度按 uid 实测缓存，滚动时 rAF 节流滑动窗口。
 */
(function () {
  "use strict";

  var PLAN_STATUS_ICON = { pending: "○", running: "◌", done: "●", failed: "✕" };
  var PLAN_STATUS_TEXT = { pending: "等待", running: "执行中", done: "完成", failed: "失败" };
  var TOOL_LABELS = AgentProtocol.TOOL_LABELS;
  var VIRTUALIZE_THRESHOLD = 50; // 超过该条数后启用窗口化（规格：>50 步虚拟化）
  var BUFFER = 6;                // 视口上下各多渲染的条目数
  var EST_H = { user: 44, plan: 150, tool: 46, reflection: 36, hitl: 150, done: 52, error: 44 };

  var TOOL_BADGES = {
    futurebi_dsl_query: { cls: "dsl", text: "DSL" },
    python_sandbox: { cls: "sandbox", text: "Python" },
    metric_meta_lookup: { cls: "meta", text: "元数据" }
  };

  var scrollBox, listBox;
  var spacerTop, spacerBottom;
  var stickBottom = true;
  var uidSeq = 0;        // 时间线条目的稳定 uid（重规划替换 plan 不影响他项）
  var scanIdx = 0;       // 已渲染到的事件下标（增量渲染游标）
  var hasItems = false;  // 是否已有条目（空态占位判定）
  var itemH = {};        // uid -> 实测高度（缺省用估算）
  var windowStart = 0;   // 当前窗口起点
  var windowEnd = -1;    // 当前窗口终点（不含）
  var rafPending = false;

  function $(id) { return document.getElementById(id); }

  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  }

  function initScroll() {
    scrollBox = $("chat-scroll");
    listBox = $("chat-stream");
    // 虚拟化结构：上下 spacer + 窗口内容
    spacerTop = document.createElement("div");
    spacerTop.className = "virtual-spacer";
    spacerBottom = document.createElement("div");
    spacerBottom.className = "virtual-spacer";
    listBox.insertBefore(spacerTop, listBox.firstChild);
    listBox.appendChild(spacerBottom);
    scrollBox.addEventListener("scroll", function () {
      var near = scrollBox.scrollHeight - scrollBox.scrollTop - scrollBox.clientHeight;
      stickBottom = near < 40; // 距底 40px 内视为贴底
      scheduleWindow();
    });
    window.addEventListener("resize", scheduleWindow);
  }

  function autoScroll() {
    if (stickBottom) { scrollBox.scrollTop = scrollBox.scrollHeight; }
  }

  /** rAF 节流的窗口滑动（scroll/resize 高频触发）。 */
  function scheduleWindow() {
    if (rafPending) { return; }
    rafPending = true;
    requestAnimationFrame(function () {
      rafPending = false;
      renderWindow();
    });
  }

  // ------------------------------------------------------------ 高度模型
  function ensureUid(item) {
    if (!item.uid) { item.uid = "t" + (++uidSeq); }
    return item.uid;
  }

  function estHeight(item) {
    if (itemH[item.uid]) { return itemH[item.uid]; }
    return EST_H[item.kind] || 56;
  }

  function offsets(items) {
    var prefix = [0];
    for (var i = 0; i < items.length; i++) { prefix.push(prefix[i] + estHeight(items[i])); }
    return prefix;
  }

  function measureNode(node, uid) {
    if (node && node.offsetHeight) { itemH[uid] = node.offsetHeight; }
  }

  // ------------------------------------------------------------ 对话语义节点
  function elUserMessage(item) {
    var div = document.createElement("div");
    div.className = "msg-user";
    div.textContent = item.text;
    return div;
  }

  function elDone(item) {
    var div = document.createElement("div");
    div.className = "done-card";
    div.innerHTML = '<span class="r-decision">✓ 分析完成</span>'
      + '<span class="r-reason">报告与产物已生成。</span> ';
    var link = document.createElement("button");
    link.type = "button";
    link.className = "done-link";
    link.textContent = "查看报告 →";
    link.addEventListener("click", function () {
      if (window.AgentSidebarUI) { AgentSidebarUI.showView("report"); }
    });
    div.appendChild(link);
    return div;
  }

  function elHitl(item) {
    var card = document.createElement("div");
    card.className = "hitl-card";
    card.innerHTML = '<div class="hitl-q">' + esc(item.question || "需要补充信息") + "</div>";
    var opts = item.options || [];
    if (opts.length) {
      var row = document.createElement("div");
      row.className = "hitl-options";
      opts.forEach(function (o) {
        var pill = document.createElement("button");
        pill.type = "button";
        pill.className = "hitl-pill";
        pill.dataset.value = o;
        pill.textContent = o;
        row.appendChild(pill);
      });
      card.appendChild(row);
    }
    // 自由输入兜底（无预置选项或选项都不匹配时使用）
    var inputRow = document.createElement("div");
    inputRow.className = "hitl-input-row";
    inputRow.innerHTML = '<input class="hitl-input" placeholder="或输入自定义答复…">'
      + '<button type="button" class="hitl-pill hitl-send">发送</button>';
    card.appendChild(inputRow);
    return card;
  }

  function elError(item) {
    var div = document.createElement("div");
    div.className = "act-err";
    div.style.margin = "0";
    div.textContent = "执行出错：" + (item.error || "未知错误");
    return div;
  }

  function elThinking() {
    var div = document.createElement("div");
    div.className = "chat-thinking";
    div.dataset.chatThinking = "1";
    div.innerHTML = "<span>正在分析</span><span class='dots'></span>";
    return div;
  }

  function elEmpty() {
    var div = document.createElement("div");
    div.className = "chat-empty";
    div.dataset.chatEmpty = "1";
    div.innerHTML = '<div><span class="ce-icon">'
      // Lucide "bot"（ISC License）
      + '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
      + '<path d="M12 8V4H8"/><rect width="16" height="12" x="4" y="8" rx="2"/>'
      + '<path d="M2 14h2"/><path d="M20 14h2"/><path d="M15 13v2"/><path d="M9 13v2"/></svg></span></div>'
      + '<div class="ce-title">向 DataAgent 提问</div>'
      + '<div class="ce-sub">Agent 将自动规划取数、沙箱分析并生成报告，执行过程以时间线呈现。</div>';
    return div;
  }

  // ------------------------------------------------------------ 执行语义节点（活动时间线）
  function elPlanCard(item) {
    var card = document.createElement("div");
    card.className = "act act-plan node-plan";
    card.dataset.planCard = "1";
    var head = document.createElement("div");
    head.className = "act-head";
    var n = (item.plan || []).length;
    head.innerHTML = '<span class="act-icon">◈</span>'
      + '<span class="act-label">任务计划（' + n + " 步）</span>"
      + '<span class="act-meta" data-role="plan-progress"></span>';
    var stepsBox = document.createElement("div");
    stepsBox.className = "plan-steps";
    card.appendChild(head);
    card.appendChild(stepsBox);
    fillPlanSteps(stepsBox, item.plan);
    fillPlanProgress(head, item.plan);
    return card;
  }

  function fillPlanSteps(stepsBox, plan) {
    stepsBox.innerHTML = "";
    (plan || []).forEach(function (s) {
      var row = document.createElement("div");
      row.className = "plan-step";
      row.dataset.status = s.status;
      row.dataset.stepId = s.id;
      row.innerHTML = '<span class="p-icon">' + (PLAN_STATUS_ICON[s.status] || "○") + "</span>"
        + '<span class="p-goal">' + esc(s.title) + "</span>"
        + '<span class="p-status">' + (PLAN_STATUS_TEXT[s.status] || s.status) + "</span>";
      stepsBox.appendChild(row);
    });
  }

  function fillPlanProgress(head, plan) {
    var prog = head.querySelector('[data-role="plan-progress"]');
    if (!prog) { return; }
    var list = plan || [];
    var done = list.filter(function (s) { return s.status === "done"; }).length;
    prog.textContent = done + "/" + list.length + " 完成";
  }

  /** 就地更新计划卡步骤状态与进度（复用 DOM，不重绘整卡）。 */
  function updatePlanCard(plan) {
    var card = listBox.querySelector('[data-plan-card]');
    if (!card) { return; }
    fillPlanSteps(card.querySelector(".plan-steps"), plan);
    fillPlanProgress(card.querySelector(".act-head"), plan);
  }

  function elTool(item) {
    var acc = document.createElement("div");
    acc.className = "act";
    acc.dataset.toolId = item.toolId;
    acc.dataset.name = item.name;
    if (item.ended) { acc.classList.add(item.error ? "node-fail" : "node-ok"); }
    else { acc.classList.add("node-running"); }
    acc.__input = item.input || null; // 缓存 input 供 tool_end 合并渲染
    var badge = TOOL_BADGES[item.name]
      || { cls: "dsl", text: esc(TOOL_LABELS[item.name] || item.name || "工具") };
    var label = item.name === "futurebi_dsl_query" && item.metric
      ? item.metric
      : (TOOL_LABELS[item.name] || item.name || "工具调用");
    var head = document.createElement("button");
    head.type = "button";
    head.className = "act-head";
    // 状态/耗时锚点常驻（data-role 供 tool_end 就地更新；未结束时留空）
    head.innerHTML = '<span class="act-icon">⚙</span>'
      + '<span class="tool-badge ' + badge.cls + '">' + badge.text + "</span>"
      + '<span class="act-label">' + esc(label) + "</span>"
      + '<span data-role="state" class="act-meta">' + (item.ended ? (item.error ? "✕ 失败" : "✓ 完成") : "执行中") + "</span>"
      + '<span data-role="dur" class="act-meta">' + (item.duration_ms != null ? fmtDur(item.duration_ms) : "") + "</span>"
      + '<span class="act-chevron">▾</span>';
    var body = document.createElement("div");
    body.className = "act-body";
    head.addEventListener("click", function () { acc.classList.toggle("open"); });
    acc.appendChild(head);
    acc.appendChild(body);
    fillToolBody(body, item);
    return acc;
  }

  function fillToolBody(body, item) {
    body.innerHTML = "";
    if (item.name === "futurebi_dsl_query" && item.input && item.input.dsl) {
      body.innerHTML += "<h4>DSL（AST 契约）</h4><pre data-lang='json'>"
        + esc(JSON.stringify(item.input.dsl, null, 2)) + "</pre>";
    }
    if (item.name === "python_sandbox" && item.input && item.input.code) {
      body.innerHTML += "<h4>Python 分析脚本</h4><pre><code class='language-python'>"
        + esc(item.input.code) + "</code></pre>";
    }
    if (item.output) {
      body.innerHTML += "<h4>执行结果</h4><pre>" + esc(JSON.stringify(item.output, null, 2)) + "</pre>";
    }
    if (item.error) {
      body.innerHTML += '<div class="act-err">' + esc(item.error) + "</div>";
    }
    if (window.Prism) { Prism.highlightAllUnder(body); }
  }

  function elReflection(item) {
    var r = item.reflection || {};
    var icons = { proceed: "✓", retry: "↻", replan: "⟳" };
    var labels = { proceed: "检查通过", retry: "自愈重试", replan: "重规划" };
    var div = document.createElement("div");
    div.className = "act act-reflect"
      + (r.decision === "proceed" ? " node-ok" : r.decision === "retry" || r.decision === "replan" ? " act-warn" : "");
    var meta = r.observation || "";
    if (r.reason) { meta += (meta ? " · " : "") + r.reason; }
    div.innerHTML = '<div class="act-head">'
      + '<span class="act-icon">' + (icons[r.decision] || "•") + "</span>"
      + '<span class="act-label">' + (labels[r.decision] || esc(r.decision || "反思")) + "</span>"
      + '<span class="act-meta grow" title="' + esc(meta) + '">' + esc(meta) + "</span>"
      + "</div>";
    return div;
  }

  function elItemFor(item) {
    if (item.kind === "user") { return elUserMessage(item); }
    if (item.kind === "plan") { return elPlanCard(item); }
    if (item.kind === "tool") { return elTool(item); }
    if (item.kind === "reflection") { return elReflection(item); }
    if (item.kind === "hitl") { return elHitl(item); }
    if (item.kind === "done") { return elDone(item); }
    if (item.kind === "error") { return elError(item); }
    return null;
  }

  // ------------------------------------------------------------ 窗口化虚拟渲染
  /** 计算可视窗口边界（前缀和二分 + BUFFER 外扩，钳制到 [0, n]）。 */
  function windowBounds(items, prefix) {
    var viewportH = scrollBox.clientHeight || 600;
    var top = Math.max(scrollBox.scrollTop, 0);
    var bottom = top + viewportH;
    // 二分找第一个 offset > top 的条目
    var lo = 0, hi = items.length;
    while (lo < hi) {
      var mid = (lo + hi) >> 1;
      if (prefix[mid + 1] <= top) { lo = mid + 1; } else { hi = mid; }
    }
    var start = Math.max(lo - BUFFER, 0);
    var end = start;
    while (end < items.length && prefix[end] < bottom) { end++; }
    return { start: start, end: Math.min(end + BUFFER, items.length) };
  }

  /** 键控协调：让 spacerTop..spacerBottom 之间的 DOM 与 want 序列严格一致
   *（uid 相同复用节点，乱序则移动）。空态占位与运行中指示位于 spacerBottom 之后，不受影响。 */
  function reconcile(want) {
    var existing = {};
    var nodes = listBox.querySelectorAll("[data-uid]");
    for (var k = 0; k < nodes.length; k++) { existing[nodes[k].dataset.uid] = nodes[k]; }

    var wantSet = {};
    want.forEach(function (item) { wantSet[item.uid] = 1; });
    Object.keys(existing).forEach(function (uid) {
      if (!wantSet[uid]) { existing[uid].remove(); delete existing[uid]; }
    });

    var cursor = spacerTop.nextSibling;
    want.forEach(function (item) {
      var node = existing[item.uid];
      if (node) { delete existing[item.uid]; }
      else {
        node = elItemFor(item);
        node.dataset.uid = item.uid;
        measureNode(node, item.uid);
      }
      if (node === cursor) {
        cursor = node.nextSibling;
      } else {
        listBox.insertBefore(node, cursor);
      }
    });
  }

  /** 渲染当前窗口：窗外条目移除、窗内条目补齐、更新 spacer。 */
  function renderWindow(items) {
    if (items.length <= VIRTUALIZE_THRESHOLD) { return; } // 非虚拟化模式
    var prefix = offsets(items);
    var w = windowBounds(items, prefix);
    windowStart = w.start;
    windowEnd = w.end;
    reconcile(items.slice(w.start, w.end));
    spacerTop.style.height = prefix[w.start] + "px";
    spacerBottom.style.height = (prefix[items.length] - prefix[w.end]) + "px";
  }

  /** 总渲染入口：增量扫描 + 虚拟化/简单协调 + 空态与运行指示。 */
  function render(state) {
    // 计划卡状态就地刷新（避免整卡重排）
    if (state.activePlan.length) { updatePlanCard(state.activePlan); }

    var events = state.timelineEvents;
    // 会话重置（事件数收缩）：全量清空重建（含高度缓存与窗口状态）
    if (events.length < scanIdx) {
      listBox.innerHTML = "";
      listBox.appendChild(spacerTop);
      listBox.appendChild(spacerBottom);
      itemH = {};
      scanIdx = 0;
      hasItems = false;
      windowStart = 0;
      windowEnd = -1;
    }
    for (var i = scanIdx; i < events.length; i++) { ensureUid(events[i]); }
    scanIdx = events.length;

    var items = events;
    if (items.length > VIRTUALIZE_THRESHOLD) {
      // 虚拟化模式：渲染当前窗口（贴底时先滚到末尾坐标）
      renderWindow(items);
    } else if (items.length) {
      reconcile(items);
      spacerTop.style.height = "0px";
      spacerBottom.style.height = "0px";
    }
    if (items.length) { hasItems = true; }

    // 空态占位
    var empty = listBox.querySelector("[data-chat-empty]");
    if (!hasItems && !empty) { listBox.appendChild(elEmpty()); }
    if (hasItems && empty) { empty.remove(); }

    // 运行中指示（时间线末尾）
    var oldThinking = listBox.querySelector("[data-chat-thinking]");
    if (oldThinking) { oldThinking.remove(); }
    if (state.running && hasItems) { listBox.appendChild(elThinking()); }

    autoScroll();
  }

  /** 就地更新工具块：状态/耗时/结果体/节点圆点（tool_end 增量合并后触发）。
   * 注：subscribe 回放只传 state（无 item），此处须防御空 item。 */
  function updateToolBlock(_state, item) {
    if (!item || !item.toolId) { return; }
    var target = listBox.querySelector('[data-tool-id="' + cssEscape(item.toolId) + '"]:not([data-ended])');
    if (!target) { return; }
    target.dataset.ended = "1";
    target.classList.remove("node-running");
    target.classList.add(item.error ? "node-fail" : "node-ok");
    var stateEl = target.querySelector('[data-role="state"]');
    var durEl = target.querySelector('[data-role="dur"]');
    if (stateEl) {
      stateEl.textContent = item.error ? "✕ 失败" : "✓ 完成";
      stateEl.className = "act-meta " + (item.error ? "fail" : "ok");
    }
    if (durEl && item.duration_ms != null) { durEl.textContent = fmtDur(item.duration_ms); }
    if (item.output || item.error) {
      fillToolBody(target.querySelector(".act-body"), {
        name: item.name || target.dataset.name || "",
        input: target.__input || null,
        output: item.output, error: item.error
      });
    }
  }

  function cssEscape(s) {
    return (window.CSS && CSS.escape) ? CSS.escape(s) : s.replace(/["\\:]/g, "\\$&");
  }

  function fmtDur(ms) {
    var n = Number(ms) || 0;
    return n >= 1000 ? (n / 1000).toFixed(1) + " s" : Math.round(n) + " ms";
  }

  window.AgentStreamUI = {
    init: function () {
      initScroll();
      AgentStore.subscribe("timelineEvents", render);
      AgentStore.subscribe("toolUpdate", updateToolBlock);
      AgentStore.subscribe("activePlan", function (state) {
        if (state.activePlan.length) { updatePlanCard(state.activePlan); }
      });
      AgentStore.subscribe("running", render);
    },
    resetScroll: function () { stickBottom = true; }
  };
})();
