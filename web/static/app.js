/* DataAgent 主控制台前端逻辑（三栏 Data Agent 工作台）。
 *
 * 鉴权铁律（P0）：
 * - 页面启动只允许先调用轻量身份校验端点 /api/auth/me；校验通过前严禁发起
 *   任何业务与配置 API（/api/query、/api/settings/providers 等）；
 * - 未登录一律重定向 /login（保留 redirect_url），业务页面不承载未登录态；
 * - 任意受保护 API 返回 401 => 全局登出（清凭证 + 跳转登录页）。
 *
 * 主流程（SSE 事件驱动）：
 *   提问 -> GET /api/v1/agent/chat/stream（fetch 流式）-> AgentStreamEvent
 *   -> AgentStore（plan/timeline/artifacts/hitl）
 *   -> 中央对话流 + 右侧执行流程 + 侧边栏切换的产物视图。
 */
(function () {
  "use strict";

  var $ = function (id) { return document.getElementById(id); };
  var TOKEN_KEY = "dataagent_token";
  var SESSION_KEY = "dataagent_session";
  var SID_KEY = "dataagent_sid";
  var CURRENT_SELECTION_KEY = "dataagent_model_selection";
  var LOGIN_PATH = "/login";

  var authExpiredHandled = false; // 防止并发 401 触发多次跳转
  var activeStream = null;        // 当前 SSE StreamHandle

  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  }

  function fmt(v) {
    if (typeof v === "number") { return v.toLocaleString("zh-CN", { maximumFractionDigits: 2 }); }
    return String(v == null ? "" : v);
  }

  function hideError() { $("error").classList.add("hidden"); $("error").textContent = ""; }
  function showError(msg) {
    var e = $("error");
    e.textContent = msg;
    e.classList.remove("hidden");
  }

  // ---------------------------------------------------------------- Toast
  function toast(msg, type) {
    var box = $("toast-box");
    var el = document.createElement("div");
    el.className = "toast " + (type || "info");
    el.textContent = msg;
    box.appendChild(el);
    setTimeout(function () {
      el.style.opacity = "0";
      el.style.transition = "opacity .25s";
      setTimeout(function () { el.remove(); }, 260);
    }, type === "err" ? 6000 : 3000);
  }

  // ---------------------------------------------------------------- 凭证存取
  function localGet(key) { try { return localStorage.getItem(key) || ""; } catch (e) { return ""; } }
  function sessionGet(key) { try { return sessionStorage.getItem(key) || ""; } catch (e) { return ""; } }
  function sessionSet(key, v) { try { sessionStorage.setItem(key, v); } catch (e) { /* 忽略 */ } }

  function getToken() { return sessionGet(TOKEN_KEY) || localGet(TOKEN_KEY); }
  function getSid() { return sessionGet(SID_KEY) || localGet(SESSION_KEY) || ""; }
  function clearCredentials() {
    try {
      sessionStorage.removeItem(TOKEN_KEY);
      sessionStorage.removeItem(SID_KEY);
      sessionStorage.removeItem(CURRENT_SELECTION_KEY);
      localStorage.removeItem(TOKEN_KEY);
      localStorage.removeItem(SESSION_KEY);
    } catch (e) { /* 忽略 */ }
  }

  function redirectToLogin(msg) {
    if (authExpiredHandled) { return; }
    authExpiredHandled = true;
    clearCredentials();
    var target = window.location.pathname + window.location.search;
    var url = LOGIN_PATH + "?redirect_url=" + encodeURIComponent(target);
    if (msg) {
      try { sessionStorage.setItem("dataagent_logout_reason", msg); } catch (e) { /* 忽略 */ }
    }
    window.location.replace(url);
  }

  // 全局 401 钩子（stream.js 等低层模块经由 window.App.onAuthExpired 上抛）
  window.App = { onAuthExpired: function (msg) { redirectToLogin(msg); } };

  // ---------------------------------------------------------------- 统一 API 封装
  function api(path, opts) {
    opts = opts || {};
    opts.headers = opts.headers || {};
    var token = getToken();
    if (token) { opts.headers.Authorization = "Bearer " + token; }
    var sid = getSid();
    if (sid) { opts.headers["X-Session-ID"] = sid; }
    return fetch(path, opts).then(function (r) {
      return r.json().then(function (data) {
        if (r.status === 401) {
          redirectToLogin("会话已过期，请重新登录");
        } else if (r.status === 403 && !(data && data.error)) {
          data = { error: "没有权限执行该操作" };
        }
        return data;
      });
    });
  }

  // ---------------------------------------------------------------- 启动引导（Auth Guard）
  function boot() {
    var token = getToken();
    var headers = {};
    if (token) { headers.Authorization = "Bearer " + token; }
    if (getSid()) { headers["X-Session-ID"] = getSid(); }
    fetch("/api/auth/me", { headers: headers })
      .then(function (r) {
        if (r.status === 401 && token) {
          redirectToLogin(null);
          return null;
        }
        return r.json();
      })
      .then(function (data) {
        if (data && data.username) { initConsole(data); }
        else { redirectToLogin(null); }
      })
      .catch(function (err) {
        console.error("[boot] 初始化失败:", err && err.stack ? err.stack : err);
        redirectToLogin(null);
      });
  }

  function initConsole(user) {
    renderUserCenter(user);
    AgentStreamUI.init();
    AgentCanvas.init();
    AgentSidebarUI.init();
    initAgentStatus();
    bindEvents();
    fillModelSwitch();
  }

  // ---------------------------------------------------------------- Agent 状态灯
  var STATUS_TEXT = { idle: "空闲", planning: "规划中", executing: "执行中", awaiting: "等待用户输入" };
  function initAgentStatus() {
    AgentStore.subscribe("agentStatus", function (state) {
      var el = $("agent-status");
      el.className = "agent-status " + state.agentStatus;
      $("agent-status-text").textContent = STATUS_TEXT[state.agentStatus] || state.agentStatus;
    });
  }

  // ---------------------------------------------------------------- 用户中心
  var ROLE_LABELS = { admin: "Admin", analyst: "Analyst", ops: "Ops" };
  function roleLabel(r) { return ROLE_LABELS[r] || r.charAt(0).toUpperCase() + r.slice(1); }

  function renderUserCenter(user) {
    var initial = (user.display_name || user.username || "?").trim().charAt(0).toUpperCase();
    $("user-avatar").textContent = initial;
    $("display-name").textContent = user.display_name || user.username;
    var roles = (user.roles || []).map(roleLabel).join(" / ") || "Member";
    $("user-roles").textContent = roles;
    $("menu-display-name").textContent = (user.display_name || user.username) + "（" + user.username + "）";
    $("menu-principal").textContent = "主体：" + user.principal + " · 权限由服务端映射";

    var btn = $("user-menu-btn");
    var menu = $("user-menu");
    btn.addEventListener("click", function (e) {
      e.stopPropagation();
      var open = menu.classList.toggle("hidden");
      btn.setAttribute("aria-expanded", open ? "false" : "true");
    });
    document.addEventListener("click", function (e) {
      if (!$("user-center").contains(e.target)) { closeUserMenu(); }
    });
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape") { closeUserMenu(); }
    });

    $("menu-profile").addEventListener("click", function () {
      closeUserMenu();
      toast("当前主体：" + user.principal + "，角色：" + roles + "（权限由服务端强制映射）", "info");
    });
    $("menu-model-settings").addEventListener("click", function () {
      closeUserMenu();
      openSettings();
    });
    $("menu-logout").addEventListener("click", function () {
      closeUserMenu();
      logout();
    });
  }

  function closeUserMenu() {
    $("user-menu").classList.add("hidden");
    $("user-menu-btn").setAttribute("aria-expanded", "false");
  }

  function logout() {
    var headers = { "Content-Type": "application/json" };
    var token = getToken();
    if (token) { headers.Authorization = "Bearer " + token; }
    var sid = getSid();
    if (sid) { headers["X-Session-ID"] = sid; }
    if (activeStream) { activeStream.abort(); }
    fetch("/api/auth/logout", { method: "POST", headers: headers })
      .catch(function () { /* 忽略网络错误 */ })
      .finally(function () { redirectToLogin("已安全退出登录"); });
  }

  // ---------------------------------------------------------------- 模型供应商设置
  // 预置仅 OpenAI / Anthropic；自定义供应商可自由添加，
  // 接口协议限白名单（Chat Completions / Responses / Anthropic Messages）。
  var PROTOCOL_LABELS = {
    openai_chat: "Chat Completions",
    openai_responses: "Responses",
    anthropic: "Anthropic"
  };
  var CAPABILITY_LABELS = { vision: "视觉", function_calling: "函数调用", json_schema: "JSON Schema" };
  var providers = [];
  var currentProviderId = "";

  function getCurrentSelection() {
    try { return sessionStorage.getItem(CURRENT_SELECTION_KEY) || ""; } catch (e) { return ""; }
  }
  function setCurrentSelection(v) {
    try { sessionStorage.setItem(CURRENT_SELECTION_KEY, v); } catch (e) { /* 忽略 */ }
  }

  function selectedProviderModel() {
    var v = $("model-switch").value;
    if (!v) { return {}; }
    var idx = v.indexOf("|");
    return { provider_id: v.slice(0, idx), model_id: v.slice(idx + 1) };
  }

  /** 模型选择可用性：无任何可选模型时展示配置引导横幅。 */
  function updateModelIndicator() {
    var sel = $("model-switch");
    var options = Array.prototype.slice.call(sel.options || []);
    var hasChoice = options.length > 1;
    $("model-banner").classList.toggle("hidden", hasChoice);
  }

  function applyChoices(choices) {
    var sel = $("model-switch");
    var saved = getCurrentSelection();
    var html = "<option value=''>默认模型（自动选择）</option>";
    (choices || []).forEach(function (p) {
      html += "<optgroup label='" + esc(p.provider_name) + "'>";
      (p.models || []).forEach(function (m) {
        var value = p.provider_id + "|" + m.id;
        html += "<option value='" + esc(value) + "'>"
          + esc(m.name || m.id) + " · " + (PROTOCOL_LABELS[p.protocol] || p.protocol) + "</option>";
      });
      html += "</optgroup>";
    });
    sel.innerHTML = html;
    if (saved && sel.querySelector("option[value='" + saved.replace(/"/g, '\\"') + "']")) {
      sel.value = saved;
    }
    updateModelIndicator();
  }

  function fetchModelChoices(cb) {
    api("/api/settings/providers").then(function (data) {
      if (!data || data.error) { cb([]); return; }
      providers = data.providers || [];
      renderProviderList();
      cb(data.choices || []);
    }).catch(function () { cb([]); });
  }

  function fillModelSwitch() {
    fetchModelChoices(applyChoices);
  }

  function renderProviderList() {
    var box = $("provider-list");
    var html = "";
    providers.forEach(function (p) {
      var badge = p.is_preset
        ? "<span class='p-badge preset'>预置</span>"
        : "<span class='p-badge custom'>自定义</span>";
      if (!p.enabled) { badge += "<span class='p-badge off'>已禁用</span>"; }
      html += "<div class='provider-item" + (p.id === currentProviderId ? " active" : "") + "'"
        + " data-id='" + esc(p.id) + "'>"
        + "<span class='p-name'>" + esc(p.name) + "</span>" + badge + "</div>";
    });
    box.innerHTML = html
      || "<div class='provider-empty'>尚未配置供应商——点击上方「＋ 添加供应商」接入你的模型端点。</div>";
    box.querySelectorAll(".provider-item").forEach(function (el) {
      el.addEventListener("click", function () { openProvider(el.getAttribute("data-id")); });
    });
  }

  function openProvider(id) {
    currentProviderId = id;
    renderProviderList();
    var p = providers.find(function (x) { return x.id === id; });
    $("provider-form").classList.remove("hidden");
    $("provider-empty").classList.add("hidden");
    $("pf-delete").classList.toggle("hidden", !!(p && p.is_preset));
    $("pf-test-result").classList.add("hidden");
    $("pf-name").value = p ? p.name : "";
    $("pf-enabled").checked = p ? !!p.enabled : true;
    $("pf-protocol").value = p ? p.protocol : "openai_chat";
    $("pf-base-url").value = p ? p.base_url : "";
    $("pf-api-key").value = "";
    $("pf-api-key").placeholder = p && p.has_api_key
      ? "已配置密钥（留空 = 不修改；输入新值 = 替换）"
      : "粘贴 API Key";
    $("pf-api-key").type = "password";
    var badge = $("pf-key-badge");
    if (p && p.has_api_key) {
      badge.textContent = "✓ 已保存";
      badge.classList.remove("hidden");
    } else {
      badge.classList.add("hidden");
    }
    renderModelChips(p ? p.models : []);
  }

  function openNewProvider() {
    currentProviderId = "";
    renderProviderList();
    $("provider-form").classList.remove("hidden");
    $("provider-empty").classList.add("hidden");
    $("pf-delete").classList.add("hidden");
    $("pf-test-result").classList.add("hidden");
    $("pf-name").value = "";
    $("pf-enabled").checked = true;
    $("pf-protocol").value = "openai_chat";
    $("pf-base-url").value = "";
    $("pf-api-key").value = "";
    $("pf-api-key").placeholder = "输入 API Key";
    $("pf-api-key").type = "password";
    $("pf-key-badge").classList.add("hidden");
    renderModelChips([]);
    $("pf-name").focus();
  }

  function currentFormModels() { return window.__pfModels || []; }
  function setCurrentFormModels(models) { window.__pfModels = models || []; }

  function renderModelChips(models) {
    setCurrentFormModels(models);
    var box = $("pf-models");
    if (!models || !models.length) {
      box.innerHTML = "<div class='provider-empty' style='padding:8px 0'>尚未配置模型——输入模型 ID 后点「＋添加模型」（保存时输入框内容会自动收编）</div>";
      return;
    }
    var html = "";
    models.forEach(function (m, i) {
      var tags = "";
      (m.capabilities || []).forEach(function (c) {
        if (CAPABILITY_LABELS[c]) { tags += "<span class='chip-tag'>" + CAPABILITY_LABELS[c] + "</span>"; }
      });
      if (m.context_window) {
        tags += "<span class='chip-tag ctx'>上下文 " + fmt(m.context_window) + "</span>";
      }
      html += "<span class='model-chip'>"
        + "<span class='m-id'>" + esc(m.id) + "</span>" + tags
        + "<button type='button' class='chip-act' data-act='test' data-i='" + i + "' title='测试该模型连通性'>⚡</button>"
        + "<button type='button' class='chip-act chip-del' data-act='del' data-i='" + i + "' title='移除模型'>✕</button>"
        + "</span>";
    });
    box.innerHTML = html;
    box.querySelectorAll(".chip-act").forEach(function (btn) {
      btn.addEventListener("click", function () {
        var i = Number(btn.getAttribute("data-i"));
        var act = btn.getAttribute("data-act");
        var models2 = currentFormModels();
        if (act === "del") {
          models2.splice(i, 1);
          renderModelChips(models2);
        } else if (act === "test") {
          testConnection({ model_id: models2[i].id });
        }
      });
    });
  }

  function absorbPendingModelInput() {
    var input = $("pf-model-input");
    var id = input.value.trim();
    if (!id) { return currentFormModels(); }
    var models = currentFormModels();
    if (!models.some(function (m) { return m.id === id; })) {
      models.push({ id: id, name: id, capabilities: [], context_window: null });
    }
    input.value = "";
    renderModelChips(models);
    return models;
  }

  function collectForm() {
    var models = absorbPendingModelInput();
    return {
      name: $("pf-name").value.trim(),
      enabled: $("pf-enabled").checked,
      protocol: $("pf-protocol").value,
      base_url: $("pf-base-url").value.trim(),
      api_key: $("pf-api-key").value,
      models: models
    };
  }

  function saveProvider() {
    var form = collectForm();
    if (!form.name) { toast("请填写供应商名称", "err"); return; }
    if (!form.base_url) { toast("请填写 Base URL", "err"); return; }
    if (!form.models || !form.models.length) {
      toast("请至少添加一个模型（输入模型 ID 后点「＋添加模型」）", "err");
      return;
    }
    var body = JSON.stringify(form);
    var path = currentProviderId
      ? "/api/settings/providers/" + encodeURIComponent(currentProviderId)
      : "/api/settings/providers";
    var method = currentProviderId ? "PUT" : "POST";
    api(path, { method: method, headers: { "Content-Type": "application/json" }, body: body })
      .then(handleSaved);
  }

  function handleSaved(data) {
    if (!data) { return; }
    if (data.error) { toast(data.error, "err"); return; }
    toast("供应商配置已保存", "ok");
    currentProviderId = data.provider ? data.provider.id : currentProviderId;
    fetchModelChoices(function (choices) {
      applyChoices(choices);
      openProvider(currentProviderId);
    });
  }

  function deleteProvider() {
    if (!currentProviderId) { return; }
    if (!window.confirm("确认删除该供应商？删除后不可恢复。")) { return; }
    api("/api/settings/providers/" + encodeURIComponent(currentProviderId), { method: "DELETE" })
      .then(function (data) {
        if (!data) { return; }
        if (data.error) { toast(data.error, "err"); return; }
        toast("供应商已删除", "ok");
        currentProviderId = "";
        $("provider-form").classList.add("hidden");
        $("provider-empty").classList.remove("hidden");
        fetchModelChoices(applyChoices);
      });
  }

  function friendlyProviderError(msg) {
    var s = String(msg || "");
    if (/鉴权|401|unauthorized|invalid.{0,12}key|api.?key|密钥/i.test(s)) {
      return "API Key 无效或提供商连通失败，请检查密钥与 Base URL";
    }
    if (/429|配额|rate.?limit|限流/i.test(s)) { return "模型服务配额超限或被限流，请稍后再试"; }
    if (/timed? ?out|超时/i.test(s)) { return "连接模型服务超时，请检查网络或 Base URL"; }
    return s;
  }

  function testConnection(extra) {
    var payload = extra || {};
    var form = collectForm();
    if (currentProviderId && $("pf-api-key").value === "") {
      payload.provider_id = currentProviderId;
      if (!payload.model_id) { payload.model_id = (form.models[0] || {}).id || ""; }
    } else {
      payload.base_url = form.base_url;
      payload.api_key = form.api_key;
      payload.protocol = form.protocol;
      payload.custom_headers = {};
      if (!payload.model_id) {
        payload.model_id = (form.models[0] || {}).id || $("pf-model-input").value.trim();
      }
    }
    if (!payload.model_id) { toast("请先添加或填写要测试的模型 ID", "err"); return; }
    var btn = $("pf-test");
    btn.disabled = true; btn.textContent = "测试中…";
    api("/api/settings/providers/test", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload)
    }).then(function (data) {
      if (!data) { return; }
      var box = $("pf-test-result");
      box.classList.remove("hidden");
      if (data.success) {
        box.className = "test-result ok";
        box.textContent = "✓ 连接成功 · 延时 " + fmt(data.latency_ms) + " ms";
        toast("连接成功（" + fmt(data.latency_ms) + " ms）", "ok");
      } else {
        var msg = friendlyProviderError(data.error || "连接失败");
        box.className = "test-result fail";
        box.textContent = "✗ " + msg;
        toast(msg, "err");
      }
    }).catch(function (err) {
      toast("测试请求失败：" + err, "err");
    }).finally(function () {
      btn.disabled = false; btn.textContent = "测试连通性";
    });
  }

  function openSettings() {
    $("settings-modal").classList.remove("hidden");
    fetchModelChoices(function () {});
  }
  function closeSettings() {
    $("settings-modal").classList.add("hidden");
    fillModelSwitch();
  }

  function initSettingsUi() {
    $("settings-close").addEventListener("click", closeSettings);
    $("settings-modal").addEventListener("click", function (e) {
      if (e.target === $("settings-modal")) { closeSettings(); }
    });
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape" && !$("settings-modal").classList.contains("hidden")) {
        closeSettings();
      }
    });
    $("goto-model-settings").addEventListener("click", openSettings);
    $("provider-add").addEventListener("click", openNewProvider);
    $("pf-model-add").addEventListener("click", function () {
      var input = $("pf-model-input");
      var id = input.value.trim();
      if (!id) { toast("请输入模型 ID", "err"); return; }
      var models = currentFormModels();
      if (models.some(function (m) { return m.id === id; })) { toast("模型已存在", "err"); return; }
      models.push({ id: id, name: id, capabilities: [], context_window: null });
      input.value = "";
      renderModelChips(models);
    });
    $("pf-model-input").addEventListener("keydown", function (e) {
      if (e.key === "Enter") { e.preventDefault(); $("pf-model-add").click(); }
    });
    $("pf-eye").addEventListener("click", function () {
      var input = $("pf-api-key");
      if (input.type === "text") {
        input.type = "password";
        this.textContent = "👁";
        this.title = "查看已保存的密钥";
        return;
      }
      if (!currentProviderId || input.value) {
        input.type = input.type === "password" ? "text" : "password";
        this.textContent = input.type === "text" ? "🙈" : "👁";
        return;
      }
      var btn = this;
      btn.disabled = true;
      api("/api/settings/providers/" + encodeURIComponent(currentProviderId) + "/reveal", {
        method: "POST"
      }).then(function (data) {
        btn.disabled = false;
        if (!data) { return; }
        if (data.error) { toast(data.error, "err"); return; }
        if (!data.api_key) { toast("该供应商尚未配置 API Key", "info"); return; }
        input.value = data.api_key;
        input.type = "text";
        btn.textContent = "🙈";
        btn.title = "再次点击隐藏";
        toast("密钥已取回显示（服务端已记录本次查看）", "info");
      }).catch(function () {
        btn.disabled = false;
        toast("获取密钥失败", "err");
      });
    });
    $("pf-save").addEventListener("click", saveProvider);
    $("pf-delete").addEventListener("click", deleteProvider);
    $("pf-test").addEventListener("click", function () { testConnection(null); });
    $("settings-btn").addEventListener("click", openSettings);
    $("model-switch").addEventListener("change", function () {
      setCurrentSelection($("model-switch").value);
      updateModelIndicator();
      var sel = selectedProviderModel();
      if (sel.provider_id) {
        toast("本次查询将使用：" + sel.provider_id + " / " + sel.model_id, "info");
      }
    });
  }

  // ---------------------------------------------------------------- HITL 交互
  function renderHitl(state) {
    // HITL 卡片渲染在左栏时间线里（store.pushTimeline('hitl') 驱动），
    // 这里只负责 pill 按钮与自由输入的提交行为。
    // 注意：.hitl-send 复用 pill 样式但不是选项，必须排除（否则一次点击双提交）。
    var hitl = state.hitlState;
    if (!hitl) { return; }
    document.querySelectorAll(".hitl-pill:not(.hitl-send)").forEach(function (pill) {
      pill.addEventListener("click", function () {
        submitHitlReply(pill.dataset.value || pill.textContent, pill);
      });
    });
    var form = document.querySelector(".hitl-input-row");
    if (form) {
      form.querySelector(".hitl-send").addEventListener("click", function () {
        var input = form.querySelector(".hitl-input");
        var v = input.value.trim();
        if (v) { submitHitlReply(v, null); }
      });
      form.querySelector(".hitl-input").addEventListener("keydown", function (e) {
        if (e.key === "Enter") {
          e.preventDefault();
          var v = this.value.trim();
          if (v) { submitHitlReply(v, null); }
        }
      });
    }
  }

  function submitHitlReply(reply, pillEl) {
    document.querySelectorAll(".hitl-pill").forEach(function (p) { p.disabled = true; });
    var inputRow = document.querySelector(".hitl-input-row");
    if (inputRow) { inputRow.remove(); }
    var answered = document.createElement("div");
    answered.className = "hitl-answered";
    answered.textContent = "已答复：" + reply;
    var card = document.querySelector(".hitl-card");
    if (card) { card.appendChild(answered); }
    AgentStore.setHitl(null);
    // 以恢复语义重新发起流（resume_token + human_reply）
    var sel = selectedProviderModel();
    AgentStore.setRunning(true);
    activeStream = AgentEventSource.open(
      AgentProtocol.buildStreamUrl(currentQuery, {
        human_reply: reply,
        resume_token: hitlResumeToken,
        provider_id: sel.provider_id,
        model_id: sel.model_id
      }),
      streamHandlers()
    );
  }

  // ---------------------------------------------------------------- 主流程（SSE 事件驱动）
  var currentQuery = "";
  var hitlResumeToken = "";

  function handleAgentEvent(ev) {
    if (ev.event === "__stream_end__") {
      // 传输层结束：done/error 事件已驱动状态；此处兜底复位
      if (AgentStore.get().running) { AgentStore.setRunning(false); }
      if (!$("run").disabled) { return; }
      $("run").disabled = false;
      $("run").textContent = "开始分析";
      return;
    }
    if (ev.turn_id) { AgentStore.setTurnId(ev.turn_id); }
    var p = ev.payload || {};

    switch (ev.event) {
      case "plan_created":
        AgentStore.setPlan(AgentProtocol.normalizePlan(p.plan));
        AgentStore.pushTimeline({ kind: "plan", plan: AgentProtocol.normalizePlan(p.plan) });
        break;

      case "step_start":
        AgentStore.setAgentStatus("executing");
        break;

      case "tool_start": {
        var tool = p.tool || {};
        var item = {
          kind: "tool",
          toolId: (p.step_id || "s") + ":" + (tool.name || "t"),
          name: tool.name || "",
          input: tool.input || {},
          metric: tool.input && tool.input.dsl && Array.isArray(tool.input.dsl.metrics) && tool.input.dsl.metrics[0]
            ? String(tool.input.dsl.metrics[0].alias || tool.input.dsl.metrics[0].field || tool.name)
            : "",
          output: null, error: null, duration_ms: null
        };
        AgentStore.upsertToolEvent(item);
        // 沙箱代码同步进右栏「代码沙箱」Tab
        if (tool.name === "python_sandbox" && tool.input && tool.input.code) {
          AgentStore.pushArtifact({ type: "code_snippet", title: "沙箱分析脚本", content: tool.input.code });
        }
        break;
      }

      case "tool_end": {
        var tool2 = p.tool || {};
        // end 事件只带结果字段；toolId 与 start 同构（同步骤同名工具），由
        // store.upsertToolEvent 向上匹配最近一个未结束的同名块完成增量合并
        AgentStore.upsertToolEvent({
          kind: "tool",
          toolId: (p.step_id || "s") + ":" + (tool2.name || "t"),
          ended: true,
          name: tool2.name || "",
          output: tool2.output || null,
          error: tool2.error || null,
          duration_ms: tool2.duration_ms || null
        });
        // DSL 取数结果 -> 数据审计 Tab（rows 为总行数，preview_rows 为前 30 行切片）
        if (tool2.name === "futurebi_dsl_query" && tool2.output && tool2.output.preview_rows && tool2.status === "ok") {
          AgentStore.pushArtifact({
            type: "table",
            title: (p.step_id || "dataset") + " · " + (tool2.output.dataset || ""),
            columns: tool2.output.columns || [],
            rows: tool2.output.preview_rows,
            totalRows: tool2.output.rows
          });
        }
        AgentStore.setStepStatus(p.step_id || "", tool2.status === "ok" ? "done" : "failed");
        break;
      }

      case "reflection":
        AgentStore.pushTimeline({ kind: "reflection", reflection: p.reflection || {} });
        AgentStore.setAgentStatus("planning");
        break;

      case "hitl_request":
        hitlResumeToken = (p.hitl && p.hitl.resume_token) || "";
        AgentStore.setHitl({
          question: (p.hitl && p.hitl.question) || "请补充分析需求",
          options: (p.hitl && p.hitl.options) || []
        });
        AgentStore.pushTimeline({
          kind: "hitl",
          question: (p.hitl && p.hitl.question) || "",
          options: (p.hitl && p.hitl.options) || []
        });
        AgentStore.setRunning(false);
        $("run").disabled = false;
        $("run").textContent = "开始分析";
        renderHitl(AgentStore.get());
        break;

      case "artifact_emit":
        // 主对话页只用于对话：产物入账后不自动跳转视图，
        // 由侧边栏徽标提示（计数亮起），用户自行切换查看。
        if (p.artifact) {
          AgentStore.pushArtifact(p.artifact);
        }
        break;

      case "done":
        AgentStore.pushTimeline({ kind: "done" });
        AgentStore.setRunning(false);
        $("run").disabled = false;
        $("run").textContent = "开始分析";
        break;

      case "error":
        AgentStore.pushTimeline({ kind: "error", error: p.error || "未知错误" });
        AgentStore.setRunning(false);
        $("run").disabled = false;
        $("run").textContent = "开始分析";
        toast(p.error || "执行出错", "err");
        break;
    }
  }

  function streamHandlers() {
    return {
      onEvent: handleAgentEvent,
      onError: function (err) {
        AgentStore.pushTimeline({ kind: "error", error: "连接中断：" + (err && err.message ? err.message : err) });
        AgentStore.setRunning(false);
        $("run").disabled = false;
        $("run").textContent = "开始分析";
        showError("Agent 流连接失败，请重试");
      }
    };
  }

  function run() {
    var q = $("query").value.trim();
    if (!q) { showError("请输入问题"); return; }
    hideError();
    currentQuery = q;
    hitlResumeToken = "";
    if (activeStream) { activeStream.abort(); }
    AgentStore.reset();
    AgentStreamUI.resetScroll();
    AgentCanvas.reset();
    AgentStore.pushUserMessage(q);
    AgentStore.setRunning(true);
    $("run").disabled = true;
    $("run").textContent = "分析中…";
    var sel = selectedProviderModel();
    // 客户端不提交 principal：主体由服务端从身份映射（P0）
    var stream = AgentEventSource.open(
      AgentProtocol.buildStreamUrl(q, {
        provider_id: sel.provider_id,
        model_id: sel.model_id
      }),
      streamHandlers()
    );
    activeStream = stream;
    window.__activeStream = stream; // 供「新对话」中断当前流
    // 线程历史：提交即记录（含当前事件数，供历史列表展示规模）
    AgentSidebarUI.recordThread(q, AgentStore.get().timelineEvents.length);
  }

  function bindEvents() {
    initSettingsUi();
    $("run").addEventListener("click", run);
    $("query").addEventListener("keydown", function (e) { if (e.key === "Enter") run(); });
  }

  // Auth Guard 入口：校验通过前不发起任何业务/配置 API
  boot();
})();
