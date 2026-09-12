/* 产物渲染（AgentCanvas）：四视图内容 + 导出。
 *
 * 视图：执行报告（Markdown）/ 图表（ECharts 交互 + PNG 导出）/
 *       代码沙箱（Prism 高亮 + stdout）/ 数据审计（分块滚动表格）。
 * 视图切换由侧边栏（sidebar-ui.js）负责，本模块只负责内容渲染与导出；
 * 渲染模型：store.currentArtifacts 变更时全量重绘（产物量级小）；
 * ECharts 实例池按 host 复用，重绘前 dispose 防泄漏。
 */
(function () {
  "use strict";

  var charts = []; // [{instance, el}]
  var COLORS = ["#4f6ef7", "#22b8cf", "#12b886", "#f59f00", "#e64980", "#845ef7", "#74b816", "#f76707"];

  function $(id) { return document.getElementById(id); }
  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  }
  function fmt(v) {
    if (typeof v === "number") { return v.toLocaleString("zh-CN", { maximumFractionDigits: 2 }); }
    return String(v == null ? "" : v);
  }

  function disposeCharts() {
    charts.forEach(function (c) { try { c.instance.dispose(); } catch (e) { /* 忽略 */ } });
    charts = [];
  }

  /** 图表视图激活时调用：隐藏容器初始化尺寸为 0 的兜底。 */
  function resizeCharts() {
    charts.forEach(function (c) { try { c.instance.resize(); } catch (e) { /* 忽略 */ } });
  }

  // ------------------------------------------------------------ 报告 Tab
  function renderReports(arts) {
    var box = $("report-content");
    var empty = $("report-empty");
    if (!arts.reports.length) {
      box.classList.add("hidden"); empty.classList.remove("hidden");
      return;
    }
    empty.classList.add("hidden"); box.classList.remove("hidden");
    box.innerHTML = MdLite.render(arts.reports[arts.reports.length - 1].content);
  }

  // ------------------------------------------------------------ 图表 Tab
  function renderCharts(arts) {
    var box = $("charts-content");
    var empty = $("charts-empty");
    disposeCharts();
    if (!arts.charts.length) {
      box.classList.add("hidden"); empty.classList.remove("hidden");
      return;
    }
    empty.classList.add("hidden"); box.classList.remove("hidden");
    box.innerHTML = "";
    arts.charts.forEach(function (artifact, idx) {
      var block = document.createElement("div");
      block.className = "chart-block";
      block.innerHTML = '<div class="chart-block-title">' + esc(artifact.title || ("图表 " + (idx + 1))) + "</div>"
        + '<div class="chart-host" data-host="1"></div>'
        + '<div class="chart-export-row"><button type="button" class="mini-btn" data-export="png">⬇ PNG</button></div>';
      box.appendChild(block);
      block.querySelector('[data-export="png"]').addEventListener("click", function () {
        var entry = charts.find(function (c) { return c.el === host; });
        if (entry) {
          var a = document.createElement("a");
          a.href = entry.instance.getDataURL({ pixelRatio: 2, backgroundColor: "#fff" });
          a.download = (artifact.title || "chart") + ".png";
          a.click();
        }
      });
      var host = block.querySelector(".chart-host");
      if (window.echarts) {
        var instance = echarts.init(host);
        var option = artifact.content || {};
        if (!option.color) { option.color = COLORS; }
        instance.setOption(option);
        charts.push({ instance: instance, el: host });
      }
    });
  }

  // ------------------------------------------------------------ 代码沙箱 Tab
  function renderCodes(arts) {
    var box = $("code-content");
    var empty = $("code-empty");
    var codes = arts.codes;
    // python_sandbox 工具的入参代码也进入沙箱 Tab（tool 事件驱动，由 app.js push）
    if (!codes.length) {
      box.classList.add("hidden"); empty.classList.remove("hidden");
      return;
    }
    empty.classList.add("hidden"); box.classList.remove("hidden");
    box.innerHTML = "";
    codes.forEach(function (code, idx) {
      var block = document.createElement("div");
      block.className = "code-block";
      block.innerHTML = '<div class="code-block-head"><span class="lang-tag">PYTHON</span>'
        + esc(code.title || ("analysis_" + (idx + 1) + ".py")) + "</div>"
        + "<pre><code class='language-python'>" + esc(code.content) + "</code></pre>"
        + (code.stdout
          ? '<div class="code-stdout"><span class="out-label">STDOUT / RESULT</span>' + esc(code.stdout) + "</div>"
          : "");
      box.appendChild(block);
    });
    if (window.Prism) { Prism.highlightAllUnder(box); }
  }

  // ------------------------------------------------------------ 数据审计 Tab
  function renderTables(arts) {
    var box = $("data-content");
    var empty = $("data-empty");
    if (!arts.tables.length) {
      box.classList.add("hidden"); empty.classList.remove("hidden");
      return;
    }
    empty.classList.add("hidden"); box.classList.remove("hidden");
    box.innerHTML = "";
    arts.tables.forEach(function (t, idx) {
      var block = document.createElement("div");
      block.className = "data-block";
      var rows = t.rows || [];
      var cols = t.columns || (rows[0] ? rows[0].map(function (_, i) { return "col" + (i + 1); }) : []);
      var totalLabel = t.totalRows != null ? fmt(t.totalRows) + " 行（预览 " + rows.length + "）" : fmt(rows.length) + " 行";
      var head = '<div class="data-block-head">' + esc(t.title || ("数据集 " + (idx + 1)))
        + '<span class="rows-tag">' + totalLabel + " × " + cols.length + " 列</span></div>";
      var html = head + '<div class="table-scroll"><table><thead><tr>';
      cols.forEach(function (c) { html += "<th>" + esc(c) + "</th>"; });
      html += "</tr></thead><tbody>";
      // 审计表上限 200 行（超限提示；完整数据走导出）
      rows.slice(0, 200).forEach(function (r) {
        html += "<tr>";
        r.forEach(function (v) {
          html += "<td" + (typeof v === "number" ? " class='num'" : "") + ">" + esc(fmt(v)) + "</td>";
        });
        html += "</tr>";
      });
      html += "</tbody></table></div>";
      if (rows.length > 200) {
        html += '<div class="rows-tag" style="padding:8px 14px">仅展示前 200 行，共 ' + fmt(rows.length) + " 行</div>";
      }
      block.innerHTML = html;
      box.appendChild(block);
    });
  }

  // ------------------------------------------------------------ 导出
  function download(filename, content, mime) {
    var blob = new Blob([content], { type: mime });
    var a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = filename;
    a.click();
    setTimeout(function () { URL.revokeObjectURL(a.href); }, 500);
  }

  function exportMarkdown() {
    var state = AgentStore.get();
    var md = state.finalReport || "# 分析报告\n\n（本次运行未生成报告）";
    md += "\n\n---\n\n## 执行摘要\n";
    state.currentArtifacts.tables.forEach(function (t) {
      md += "\n### " + (t.title || "数据集") + "\n\n";
      md += "| " + (t.columns || []).join(" | ") + " |\n";
      md += "|" + (t.columns || []).map(function () { return "---"; }).join("|") + "|\n";
      (t.rows || []).slice(0, 30).forEach(function (r) {
        md += "| " + r.map(function (v) { return String(v); }).join(" | ") + " |\n";
      });
    });
    download("dataagent-report.md", md, "text/markdown");
  }

  function exportHtml() {
    var state = AgentStore.get();
    var body = MdLite.render(state.finalReport || "# 分析报告\n\n（本次运行未生成报告）");
    var html = "<!DOCTYPE html><html lang='zh-CN'><head><meta charset='utf-8'>"
      + "<title>DataAgent 分析报告</title>"
      + "<style>body{font-family:system-ui,-apple-system,'Segoe UI',sans-serif;max-width:860px;margin:40px auto;padding:0 24px;line-height:1.8;color:#1a1d2e}"
      + "table{border-collapse:collapse;width:100%}th,td{border:1px solid #e6e8f0;padding:6px 10px;font-size:13px}"
      + "th{background:#f6f7fb}blockquote{border-left:3px solid #4f6ef7;background:#f6f8ff;padding:8px 14px;margin:10px 0}</style></head><body>"
      + body + "</body></html>";
    download("dataagent-report.html", html, "text/html");
  }

  // ------------------------------------------------------------ 入口
  function render(arts) {
    renderReports(arts);
    renderCharts(arts);
    renderCodes(arts);
    renderTables(arts);
  }

  window.AgentCanvas = {
    init: function () {
      $("export-md").addEventListener("click", exportMarkdown);
      $("export-html").addEventListener("click", exportHtml);
      AgentStore.subscribe("currentArtifacts", function (state) {
        render(state.currentArtifacts);
      });
    },
    /** 新一轮开始：清空产物。视图切换由侧边栏负责。 */
    reset: function () {
      AgentStore.get().currentArtifacts = { reports: [], charts: [], codes: [], tables: [] };
      disposeCharts();
      render(AgentStore.get().currentArtifacts);
    },
    resizeCharts: resizeCharts
  };
})();
