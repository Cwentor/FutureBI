/* 双列表执行流渲染（AgentChatStream 拆分渲染）。
 *
 * 布局契约：中央对话流（chat）只承载对话语义节点；右侧执行流程（process）
 * 承载机器执行语义节点。数据源同为 store.timelineEvents（append-only，
 * 重规划时 plan 项被替换），按 kind 路由：
 *   chat    -> user / hitl / done / error（低频，简单追加渲染）
 *   process -> plan / tool / reflection（高频，>50 条启用窗口化虚拟渲染）
 *
 * 虚拟化模型：process 列表超过 VIRTUALIZE_THRESHOLD 条后仅渲染可视窗口
 * ±BUFFER 的条目，窗口外用上下 spacer 撑起滚动高度；滚动时 rAF 节流滑动窗口。
 * 条目以 uid 键控协调（重规划替换 plan 时不串位），高度按 uid 实测缓存。
 */
(function () {
  "use strict";

  var PLAN_STATUS_ICON = { pending: "○", running: "◌", done: "●", failed: "✕" };
  var PLAN_STATUS_TEXT = { pending: "等待", running: "执行中", done: "完成", failed: "失败" };
  var TOOL_LABELS = AgentProtocol.TOOL_LABELS;
  var VIRTUALIZE_THRESHOLD = 50; // 超过该条数后启用窗口化（规格：>50 步虚拟化）
  var BUFFER = 6;                // 视口上下各多渲染的条目数
  var EST_H = { plan: 130, tool: 46, reflection: 64 };

  var CHAT_KINDS = { user: 1, hitl: 1, done: 1, error: 1 };

  var chatScrollBox, chatBox;    // 对话流（中央）
  var scrollBox, listBox;        // 执行流（右侧）
  var spacerTop, spacerBottom;
  var stickChatBottom = true;
  var stickProcBottom = true;
  var uidSeq = 0;          // 时间线条目的稳定 uid（重规划替换 plan 不影响他项）
  var chatScanIdx = 0;         // 对话流已扫描到的事件下标（增量渲染游标）
  var chatHasItems = false;    // 对话流是否已有条目（空态占位判定）
  var procItems = [];          // 执行流条目（timeline 的 plan/tool/reflection 子集）
  var itemH = {};              // uid -> 实测高度（缺省用估算）
  var windowStart = 0;         // 当前窗口起点
  var windowEnd = -1;          // 当前窗口终点（不含）
  var rafPending = false;

  function $(id) { return document.getElementById(id); }

  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  }

  function initScroll() {
    chatScrollBox = $("chat-scroll");
    chatBox = $("chat-stream");
    scrollBox = $("stream-scroll");
    listBox = $("stream");
    // 虚拟化结构：上下 spacer + 窗口内容
    spacerTop = document.createElement("div");
    spacerTop.className = "virtual-spacer";
    spacerBottom = document.createElement("div");
    spacerBottom.className = "virtual-spacer";
    listBox.insertBefore(spacerTop, listBox.firstChild);
    listBox.appendChild(spacerBottom);

    chatScrollBox.addEventListener("scroll", function () {
      var near = chatScrollBox.scrollHeight - chatScrollBox.scrollTop - chatScrollBox.clientHeight;
      stickChatBottom = near < 40;
    });
    scrollBox.addEventListener("scroll", function () {
      var near = scrollBox.scrollHeight - scrollBox.scrollTop - scrollBox.clientHeight;
      stickProcBottom = near < 40; // 距底 40px 内视为贴底
      scheduleWindow();
    });
    window.addEventListener("resize", scheduleWindow);
  }

  function autoScrollChat() {
    if (stickChatBottom) { chatScrollBox.scrollTop = chatScrollBox.scrollHeight; }
  }
  function autoScrollProc() {
    if (stickProcBottom) { scrollBox.scrollTop = scrollBox.scrollHeight; }
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

  // ------------------------------------------------------------ 高度模型（执行流）
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

  // ------------------------------------------------------------ 对话流节点
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
    card.className = "stream-item hitl-card";
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
    div.className = "stream-item tool-err";
    div.style.margin = "0";
    div.textContent = "执行出错：" + (item.error || "未知错误");
    return div;
  }

  function elChatThinking() {
    var div = document.createElement("div");
    div.className = "chat-thinking";
    div.dataset.chatThinking = "1";
    div.innerHTML = "<span>正在分析</span><span class='dots'></span>";
    return div;
  }

  function elChatEmpty() {
    var div = document.createElement("div");
    div.className = "chat-empty";
    div.dataset.chatEmpty = "1";
    div.innerHTML = '<div><span class="ce-icon">'
      // Lucide "bot"（ISC License）
      + '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
      + '<path d="M12 8V4H8"/><rect width="16" height="12" x="4" y="8" rx="2"/>'
      + '<path d="M2 14h2"/><path d="M20 14h2"/><path d="M15 13v2"/><path d="M9 13v2"/></svg></span></div>'
      + '<div class="ce-title">向 DataAgent 提问</div>'
      + '<div class="ce-sub">Agent 将自动规划取数、沙箱分析并生成报告；执行细节见右侧流程。</div>';
    return div;
  }

  // ------------------------------------------------------------ 执行流节点
  function elPlanCard(item) {
    var card = document.createElement("div");
    card.className = "stream-item plan-card";
    card.dataset.planCard = "1";
    card.innerHTML = '<div class="plan-title">任务计划（DAG）</div>';
    var stepsBox = document.createElement("div");
    stepsBox.className = "plan-steps";
    card.appendChild(stepsBox);
    fillPlanSteps(stepsBox, item.plan);
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

  /** 就地更新计划卡步骤状态（复用 DOM，不重绘整卡）。 */
  function updatePlanCard(plan) {
    var card = listBox.querySelector('[data-plan-card]');
    if (!card) { return; }
    fillPlanSteps(card.querySelector(".plan-steps"), plan);
  }

  function elTool(item) {
    var acc = document.createElement("div");
    acc.className = "stream-item tool-acc";
    acc.dataset.toolId = item.toolId;
    acc.dataset.name = item.name;
    if (item.ended) { acc.dataset.ended = "1"; }
    acc.__input = item.input || null; // 缓存 input 供 tool_end 合并渲染
    var label = TOOL_LABELS[item.name] || item.name;
    var badgeText = item.name === "futurebi_dsl_query"
      ? (item.metric || label)
      : label;
    var head = document.createElement("button");
    head.type = "button";
    head.className = "tool-head";
    // 状态/耗时锚点常驻（data-role 供 tool_end 就地更新；未结束时留空）
    head.innerHTML = '<span class="tool-badge ' + (item.name === "python_sandbox" ? "sandbox" : item.name === "metric_meta_lookup" ? "meta" : "dsl") + '">'
      + esc(badgeText) + "</span>"
      + '<span data-role="state" class="tool-state">' + (item.ended ? (item.error ? "✕ 失败" : "✓ 完成") : "") + "</span>"
      + '<span data-role="dur" class="tool-dur">' + (item.duration_ms != null ? fmtDur(item.duration_ms) : "") + "</span>"
      + '<span class="tool-chevron">▾</span>';
    var body = document.createElement("div");
    body.className = "tool-body";
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
      body.innerHTML += '<div class="tool-err">' + esc(item.error) + "</div>";
    }
    if (window.Prism) { Prism.highlightAllUnder(body); }
  }

  function elReflection(item) {
    var div = document.createElement("div");
    div.className = "stream-item reflection-card";
    var r = item.reflection || {};
    div.innerHTML = '<span class="r-decision ' + esc(r.decision) + '">'
      + ({ proceed: "✓ 检查通过", retry: "↻ 自愈重试", replan: "⟳ 重规划" }[r.decision] || esc(r.decision))
      + "</span>" + esc(r.observation || "")
      + (r.reason ? '<div class="r-reason">' + esc(r.reason) + "</div>" : "");
    return div;
  }

  function elProcThinking() {
    var div = document.createElement("div");
    div.className = "stream-thinking";
    div.dataset.thinking = "1";
    div.innerHTML = '<span>Agent 正在执行</span><span class="dots"></span>';
    return div;
  }

  function elProcEmpty() {
    var div = document.createElement("div");
    div.className = "stream-empty";
    div.dataset.procEmpty = "1";
    div.innerHTML = "执行流程将在此展示：<br>任务计划（DAG）· 工具调用 · 反思自愈";
    return div;
  }

  function elItemFor(item) {
    if (item.kind === "plan") { return elPlanCard(item); }
    if (item.kind === "tool") { return elTool(item); }
    if (item.kind === "reflection") { return elReflection(item); }
    return null;
  }

  // ------------------------------------------------------------ 对话流渲染（简单增量追加）
  function renderChat(state) {
    var events = state.timelineEvents;
    // 会话重置（事件数收缩）：全量清空重建
    var i;
    if (events.length < chatScanIdx) {
      chatBox.innerHTML = "";
      chatScanIdx = 0;
      chatHasItems = false;
    }
    // 增量扫描新事件：对话类追加渲染，机器类跳过（由执行流负责）
    for (i = chatScanIdx; i < events.length; i++) {
      var item = events[i];
      if (!CHAT_KINDS[item.kind]) { continue; }
      ensureUid(item);
      var node = item.kind === "user" ? elUserMessage(item)
        : item.kind === "hitl" ? elHitl(item)
        : item.kind === "done" ? elDone(item)
        : elError(item);
      chatBox.appendChild(node);
      chatHasItems = true;
    }
    chatScanIdx = events.length;

    // 空态占位
    var empty = chatBox.querySelector("[data-chat-empty]");
    if (!chatHasItems && !empty) { chatBox.appendChild(elChatEmpty()); }
    if (chatHasItems && empty) { empty.remove(); }

    // 运行中指示
    var oldThinking = chatBox.querySelector("[data-chat-thinking]");
    if (oldThinking) { oldThinking.remove(); }
    if (state.running && chatHasItems) { chatBox.appendChild(elChatThinking()); }

    autoScrollChat();
  }

  // ------------------------------------------------------------ 执行流渲染
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

  /** 键控协调：让 DOM 与 want 序列严格一致（uid 相同复用节点，乱序则移动）。 */
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
  function renderWindow() {
    if (procItems.length <= VIRTUALIZE_THRESHOLD) { return; } // 非虚拟化模式
    var prefix = offsets(procItems);
    var w = windowBounds(procItems, prefix);
    windowStart = w.start;
    windowEnd = w.end;
    reconcile(procItems.slice(w.start, w.end));
    spacerTop.style.height = prefix[w.start] + "px";
    spacerBottom.style.height = (prefix[procItems.length] - prefix[w.end]) + "px";
  }

  function renderProcess(state) {
    // 重建执行流子集（重规划替换 plan 时长度不变，须每次重建）
    procItems = [];
    state.timelineEvents.forEach(function (item) {
      if (item.kind === "plan" || item.kind === "tool" || item.kind === "reflection") {
        ensureUid(item);
        procItems.push(item);
      }
    });

    // 会话重置：全量重建（含高度缓存与窗口状态）
    if (!procItems.length) {
      listBox.innerHTML = "";
      listBox.appendChild(spacerTop);
      listBox.appendChild(spacerBottom);
      itemH = {};
      windowStart = 0;
      windowEnd = -1;
    }

    if (procItems.length > VIRTUALIZE_THRESHOLD) {
      // 虚拟化模式：贴底时先滚到末尾坐标，再渲染当前窗口
      renderWindow();
    } else if (procItems.length) {
      reconcile(procItems);
      spacerTop.style.height = "0px";
      spacerBottom.style.height = "0px";
    }

    // 空态占位 + 运行中指示
    var empty = listBox.querySelector("[data-proc-empty]");
    if (!procItems.length && !empty) { listBox.appendChild(elProcEmpty()); }
    if (procItems.length && empty) { empty.remove(); }
    var oldThinking = listBox.querySelector("[data-thinking]");
    if (oldThinking) { oldThinking.remove(); }
    if (state.running && procItems.length) { listBox.appendChild(elProcThinking()); }

    autoScrollProc();
  }

  /** 总渲染入口：对话流 + 执行流各自按需更新。 */
  function render(state) {
    // 计划卡状态就地刷新（避免整卡重排）
    if (state.activePlan.length) { updatePlanCard(state.activePlan); }
    renderChat(state);
    renderProcess(state);
  }

  /** 就地更新工具块：状态/耗时/结果体（tool_end 增量合并后触发）。
   * 注：subscribe 回放只传 state（无 item），此处须防御空 item。 */
  function updateToolBlock(_state, item) {
    if (!item || !item.toolId) { return; }
    var target = listBox.querySelector('[data-tool-id="' + cssEscape(item.toolId) + '"]:not([data-ended])');
    if (!target) { return; }
    target.dataset.ended = "1";
    var stateEl = target.querySelector('[data-role="state"]');
    var durEl = target.querySelector('[data-role="dur"]');
    if (stateEl) {
      stateEl.textContent = item.error ? "✕ 失败" : "✓ 完成";
      stateEl.className = "tool-state " + (item.error ? "fail" : "ok");
    }
    if (durEl && item.duration_ms != null) { durEl.textContent = fmtDur(item.duration_ms); }
    if (item.output || item.error) {
      fillToolBody(target.querySelector(".tool-body"), {
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
    resetScroll: function () {
      stickChatBottom = true;
      stickProcBottom = true;
    }
  };
})();
