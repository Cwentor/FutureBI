"""编排器单测：图引擎语义 / 状态契约 / HITL 中断恢复 / 端到端确定性路径。

覆盖（对应企业级 Data Agent 需求 §2.A + §4 步骤 3/4）：
- StateGraph：条件路由、HITL 中断与 resume、迭代护栏、缺出边报错；
- AgentState：契约冻结（extra=forbid）、token 预算修剪、错误上下文上限；
- 六节点端到端（确定性兜底路径，真实 mock 数仓 + 沙箱）：
  诊断问题 -> 多步日志 + 沙箱产物 + 报告；澄清门 HITL 两段式。
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from core.orchestrator.agent import run_agent
from core.orchestrator.graph import GraphError, StateGraph
from core.orchestrator.state import MAX_RETRIES, AgentState, ToolRecord


# --------------------------------------------------------------------------- #
# 图引擎
# --------------------------------------------------------------------------- #
def test_graph_conditional_routing_and_end():
    graph = StateGraph()
    graph.add_node("a", lambda s: s.apply(phase="analyze"))
    graph.add_node("b", lambda s: s.apply(phase="done", report="ok"))
    graph.set_entry("a")
    graph.add_conditional_edges(
        "a", lambda s: "go_b" if s.phase == "analyze" else "stop", {"go_b": "b", "stop": "END"}
    )
    graph.add_edge("b", "END")
    final = graph.run(AgentState(user_query="x"))
    assert final.report == "ok" and final.iteration == 2


def test_graph_hitl_interrupt_and_resume():
    graph = StateGraph()
    graph.add_node("ask", lambda s: s.apply(phase="clarify", clarification="请补充时间范围"))
    graph.add_node("plan", lambda s: s.apply(phase="done", report=f"计划基于: {s.user_query}"))
    graph.set_entry("ask")
    graph.add_edge("ask", "plan")
    graph.add_edge("plan", "END")

    paused = graph.run(AgentState(user_query="GMV 为什么下滑"))
    assert paused.phase == "clarify" and paused.clarification

    resumed = graph.resume(paused.apply(human_reply="2024 年 5 月上旬"))
    assert resumed.phase == "done"
    assert "2024 年 5 月上旬" in resumed.report


def test_graph_resume_requires_clarify_phase():
    graph = StateGraph()
    graph.add_node("a", lambda s: s)
    with pytest.raises(GraphError):
        graph.resume(AgentState(phase="plan"))


def test_graph_max_iteration_guard():
    loop_state = lambda s: s.apply(phase="plan")  # noqa: E731
    graph = StateGraph(max_iterations=5)
    graph.add_node("a", loop_state)
    graph.set_entry("a")
    graph.add_conditional_edges("a", lambda s: "self", {"self": "a"})
    final = graph.run(AgentState(user_query="x"))
    assert "强制终止" in final.report


def test_graph_missing_edge_raises():
    graph = StateGraph()
    graph.add_node("a", lambda s: s.apply(phase="done"))
    graph.set_entry("a")
    with pytest.raises(GraphError):
        graph.run(AgentState(user_query="x"))


# --------------------------------------------------------------------------- #
# 状态契约
# --------------------------------------------------------------------------- #
def test_agent_state_forbids_extra_fields():
    with pytest.raises(ValidationError):
        AgentState(user_query="x", rogue_field=1)


def test_error_context_retry_cap():
    state = AgentState(user_query="x")
    for i in range(MAX_RETRIES):
        assert state.error_context.record(f"err {i}") is True
    assert state.error_context.record("final") is False
    assert state.error_context.retries == MAX_RETRIES + 1


def test_tool_history_pruning():
    state = AgentState(user_query="x")
    for _ in range(50):
        state.tool_calls.append(ToolRecord(tool="t", summary="s" * 5000))
    state.prune_tool_history(budget_chars=30_000)
    total = sum(len(r.summary) for r in state.tool_calls)
    assert total <= 30_000 + 5000  # 单条超预算时保留最后一条
    assert len(state.tool_calls) < 50


# --------------------------------------------------------------------------- #
# 六节点端到端（确定性兜底，真实 mock 数仓 + 沙箱）
# --------------------------------------------------------------------------- #
def test_run_agent_diagnostic_e2e(tmp_path, monkeypatch):
    """诊断问题端到端：多步日志 + 数据集 + 沙箱产物 + 报告 + ECharts。"""
    from config import settings

    monkeypatch.setattr(settings, "WORKSPACE_ROOT", tmp_path)
    trace = run_agent(
        "分析一下 2024 年 5 月第一周比第二周 GMV 下滑的原因，按地区定位", session_id="e2e"
    )
    assert isinstance(trace, dict) is False
    assert trace.phase == "done"
    # 多步日志：至少 取数 + 沙箱 两条成功轨迹
    ok_tools = [s for s in trace.steps if s["ok"]]
    assert any(s["tool"] == "execute_dsl_query" for s in ok_tools)
    assert any(s["tool"] == "run_code" for s in ok_tools)
    # 产物：summary + echarts
    kinds = {a["kind"] for a in trace.artifacts}
    assert "summary" in kinds and "echarts" in kinds
    # 报告含结论与自愈统计行
    assert "分析报告" in trace.report
    assert "执行轨迹" in trace.report


def test_run_agent_hitl_flow(tmp_path, monkeypatch):
    """歧义问题 -> 澄清中断 -> 用户答复 -> 完成全流程。"""
    from config import settings

    monkeypatch.setattr(settings, "WORKSPACE_ROOT", tmp_path)
    paused = run_agent("GMV呢？", session_id="hitl")
    assert not isinstance(paused, dict)
    assert paused.phase == "clarify" and paused.clarification

    final = run_agent(
        "GMV呢？",
        session_id="hitl",
        resume_state=paused.apply(human_reply="2024 年 5 月按省份的订单金额"),
    )
    assert final.phase == "done"
    assert any(s["ok"] for s in final.steps)


def test_run_agent_simple_query_path(tmp_path, monkeypatch):
    """非诊断问题走单查询路径：取数 + 综合即完成。"""
    from config import settings

    monkeypatch.setattr(settings, "WORKSPACE_ROOT", tmp_path)
    trace = run_agent("2024 年 5 月成功支付订单的 GMV 总额是多少？", session_id="simple")
    assert trace.phase == "done"
    assert any(s["tool"] == "execute_dsl_query" and s["ok"] for s in trace.steps)


def test_synthesize_consumes_datasets_for_pure_query(tmp_path, monkeypatch):
    """纯查询问题（无沙箱 analyze 步骤）报告必须消费取数结果。

    回归锚点：此前 synthesize 只认沙箱 summary 产物，基数/标量问题即使
    取数成功也输出"未能获得有效的分析产物"（用户可见的能力缺口）。
    """
    from config import settings

    monkeypatch.setattr(settings, "WORKSPACE_ROOT", tmp_path)
    trace = run_agent("2024 年 5 月成功支付订单的 GMV 总额是多少？", session_id="pureq")
    assert trace.phase == "done"
    assert "查询结果" in trace.report
    assert "未能获得有效的分析产物" not in trace.report
    # 标量聚合直接给答案行，且数值必须人读化（万元，R1 严禁 raw 字节面值直出）
    assert "查询答案：" in trace.report
    assert "万元" in trace.report
    assert "gmv =" not in trace.report


def test_tool_end_preview_rows_points_at_inputs_dir(tmp_path, monkeypatch):
    """tool_end 审计预览必须读到物化 Parquet（路径 = workspace/inputs/<ref.path>）。

    回归锚点：此前拼成 workspace/<ref.path> 导致 preview_rows 恒为空。
    """
    from config import settings

    monkeypatch.setattr(settings, "WORKSPACE_ROOT", tmp_path)
    events: list[dict] = []
    trace = run_agent(
        "2024 年 5 月成功支付订单的 GMV 总额是多少？", session_id="preview", on_event=events.append
    )
    assert trace.phase == "done"
    ends = [
        e
        for e in events
        if e["event"] == "tool_end" and e["payload"]["tool"]["name"] == "futurebi_dsl_query"
    ]
    assert ends
    for e in ends:
        preview = e["payload"]["tool"]["output"].get("preview_rows")
        assert preview, "预览行不应为空（物化文件在 workspace/inputs/ 下）"
        assert all(len(r) >= 1 for r in preview)


# --------------------------------------------------------------------------- #
# LLM DSL 草稿规范化（宽容接受，严格校验）
# --------------------------------------------------------------------------- #
def test_normalize_dsl_draft_repairs_common_llm_typos():
    """裸字符串维度 / time_range 笔误 / ge-le 操作符自动纠正为契约形态。"""
    from core.orchestrator.nodes import _normalize_dsl_draft

    d = _normalize_dsl_draft(
        {
            "metrics": [
                {"kind": "aggregate", "field": "order_amount", "agg": "sum", "alias": "gmv"}
            ],
            "dimensions": ["province", {"field": "category"}],
            "time_range": {
                "range_type": "absolute",
                "absolute": {"start": "2024-05-01", "end": "2024-05-08"},
            },
            "filters": [
                {"field": "pay_status", "operator": "eq", "value": "SUCCESS"},
                {"field": "order_amount", "operator": "ge", "value": 10},
            ],
        }
    )
    assert d["dimensions"] == [{"field": "province"}, {"field": "category"}]
    assert "time_range" not in d and "time_filter" in d
    assert [f["operator"] for f in d["filters"]] == ["eq", "gte"]


def test_normalize_dsl_draft_passes_valid_payload_unchanged():
    """合法载荷规范化后语义不变（可继续通过网关契约校验）。"""
    from core.orchestrator.nodes import _normalize_dsl_draft
    from core.retrieval.guardrails import validate_dsl_payload

    d = _normalize_dsl_draft(
        {
            "metrics": [
                {"kind": "aggregate", "field": "order_amount", "agg": "sum", "alias": "gmv"}
            ],
            "dimensions": [{"field": "province"}],
            "time_filter": {
                "range_type": "absolute",
                "absolute": {"start": "2024-05-01", "end": "2024-05-08"},
            },
        }
    )
    validate_dsl_payload(d, where="test")  # 不抛即通过契约


def test_planner_prompt_contract_examples_align_with_schema():
    """提示词中的 DSL 示例必须与真实契约对齐（防再次系统性带偏 LLM）。

    回归锚点：dimensions 示例是对象数组、时间字段名是 time_filter、
    操作符白名单不含 like/ge/le。
    """
    from core.orchestrator.prompts import PLANNER_SYSTEM

    assert 'dimensions: [{"field"' in PLANNER_SYSTEM
    assert "严禁写成裸字符串" in PLANNER_SYSTEM
    assert "time_filter" in PLANNER_SYSTEM
    assert '"time_range"' not in PLANNER_SYSTEM
    assert 'operator": "eq|ne|in|gt|gte|lt|lte|between"' in PLANNER_SYSTEM


# --------------------------------------------------------------------------- #
# 重规划自愈上下文（pi-agent-harness 对齐：行动项 3）
# --------------------------------------------------------------------------- #
def test_planner_prompt_injects_error_context():
    """planner_prompt 带 error_context：必须注入失败记录并要求针对性修正。"""
    from core.orchestrator.prompts import planner_prompt

    prompt = planner_prompt(
        "查 GMV", "- gmv (fact_orders.order_amount)", error_context="CompileError: 字段不存在"
    )
    assert "上次失败记录" in prompt
    assert "CompileError: 字段不存在" in prompt
    assert "严禁原样重复上一轮计划" in prompt


def test_planner_prompt_without_error_context_unchanged():
    """planner_prompt 不带 error_context：不出现失败记录小节（首轮规划不变）。"""
    from core.orchestrator.prompts import planner_prompt

    prompt = planner_prompt("查 GMV", "- gmv (fact_orders.order_amount)")
    assert "上次失败记录" not in prompt


def test_planner_node_feeds_error_context_to_llm(monkeypatch):
    """重规划时 planner_node 把最近失败摘要注入 LLM 提示词（断裂点修复实锤）。"""
    import core.orchestrator.nodes as nodes
    from core.orchestrator.state import AgentState

    captured: dict[str, str] = {}
    monkeypatch.setattr(nodes, "_resolve_llm", lambda: object())

    def fake_llm_json(llm, system, user):
        captured["user"] = user
        return None  # 走启发式兜底，重点在捕获提示词

    monkeypatch.setattr(nodes, "_llm_json", fake_llm_json)
    state = AgentState(user_query="查 GMV")
    state.error_context.record("CompileError: 字段 nonexistent 不在语义目录")
    nodes.planner_node(state)
    assert "上次失败记录" in captured["user"]
    assert "nonexistent 不在语义目录" in captured["user"]


def test_critic_trace_digest_carries_error_history(monkeypatch):
    """LLM 反思的执行轨迹必须包含自愈错误记录（反思层看得见失败历史）。"""
    import core.orchestrator.nodes as nodes
    from core.orchestrator.state import AgentState, Artifact

    captured: dict[str, str] = {}
    monkeypatch.setattr(nodes, "_resolve_llm", lambda: object())

    def fake_llm_json(llm, system, user):
        captured["user"] = user
        return {"verdict": "sufficient", "reasons": ["ok"]}

    monkeypatch.setattr(nodes, "_llm_json", fake_llm_json)
    state = AgentState(
        user_query="为什么下滑",
        datasets={"s1": {"path": "x.parquet", "rows": 1, "columns": ["gmv"]}},
        artifacts=[Artifact(kind="summary", name="s2", payload={"summary": {"title": "归因"}})],
    )
    state.error_context.record("沙箱执行失败: NameError")
    nodes.critic_node(state)
    assert "自愈错误记录" in captured["user"]
    assert "NameError" in captured["user"]


# --------------------------------------------------------------------------- #
# 重规划数据集归属（回归锚点：跨轮次错配 -> 假下滑结论）
# --------------------------------------------------------------------------- #
def test_resolve_step_inputs_follows_dependency_not_dict_order():
    """analyze 输入必须取自依赖步骤的本轮产出，而不是 datasets 字典首尾。

    回归锚点：此前按 list(state.datasets)[0]/[-1] 取两期输入，重规划后
    datasets 累积上轮键，首尾会指向上轮遗留数据集，产出"下滑 57.9%"式错配。
    """
    from core.orchestrator.nodes import _resolve_step_inputs
    from core.orchestrator.state import AgentState, PlanStep

    state = AgentState(user_query="分析 5 月 GMV 下滑原因")
    state = state.apply(
        datasets={
            "s1_v0": {"path": "a.parquet", "rows": 8, "columns": ["province", "gmv"]},
            "s1_v1": {"path": "b.parquet", "rows": 8, "columns": ["province", "gmv"]},
        },
        step_outputs={"s1": ["s1_v0", "s1_v1"]},
        plan_steps=[
            PlanStep(id="s1", goal="取两期明细", kind="query"),
            PlanStep(id="s2", goal="归因", kind="analyze", depends_on=["s1"]),
        ],
    )
    assert _resolve_step_inputs(state, state.plan_steps[1]) == ["s1_v0", "s1_v1"]


def test_resolve_step_inputs_ignores_stale_datasets_from_prior_round():
    """重规划后 datasets 含上轮遗留键时，必须只读本轮依赖产出（不复用旧键）。"""
    from core.orchestrator.nodes import _resolve_step_inputs
    from core.orchestrator.state import AgentState, PlanStep

    state = AgentState(user_query="分析 5 月 GMV 下滑原因")
    # 上轮遗留 s1/s2/s3，本轮 s1 覆盖为 s1_v0/s1_v1
    state = state.apply(
        datasets={
            "s1": {"path": "old1.parquet", "rows": 8, "columns": ["province", "gmv"]},
            "s2": {"path": "old2.parquet", "rows": 8, "columns": ["province", "gmv"]},
            "s3": {"path": "old3.parquet", "rows": 8, "columns": ["province", "gmv"]},
            "s1_v0": {"path": "new0.parquet", "rows": 8, "columns": ["province", "gmv"]},
            "s1_v1": {"path": "new1.parquet", "rows": 8, "columns": ["province", "gmv"]},
        },
        step_outputs={"s1": ["s1_v0", "s1_v1"]},
        plan_steps=[
            PlanStep(id="s1", goal="取两期明细", kind="query"),
            PlanStep(id="s2", goal="归因", kind="analyze", depends_on=["s1"]),
        ],
    )
    assert _resolve_step_inputs(state, state.plan_steps[1]) == ["s1_v0", "s1_v1"]


def test_diagnostic_dsl_pair_carries_driver_factor_metrics():
    """诊断兜底两期对必须同时带订单量与买家数因子（反思归因诉求首轮即满足）。"""
    from core.orchestrator.nodes import _diagnostic_dsl_pair

    base, curr = _diagnostic_dsl_pair("分析 5 月第一周比第二周 GMV 下滑原因")
    for dsl in (base, curr):
        aliases = {m["alias"] for m in dsl["metrics"]}
        assert {"gmv", "orders", "buyers"} <= aliases


# --------------------------------------------------------------------------- #
# 反思护栏（回归锚点：不可执行缺口 / 计划无进展 -> 禁止空转重规划）
# --------------------------------------------------------------------------- #
def _critic_state(**overrides):
    """构造带 summary 产物的诊断状态（默认产物列为 gmv/orders/buyers）。"""
    from core.orchestrator.state import AgentState, Artifact

    state = AgentState(user_query="分析一下 2024 年 5 月第一周比第二周 GMV 下滑的原因，按地区定位")
    defaults = {
        "datasets": {
            "s1_v0": {
                "path": "a.parquet",
                "rows": 8,
                "columns": ["province", "gmv", "orders", "buyers"],
            }
        },
        "artifacts": [Artifact(kind="summary", name="s2", payload={"summary": {"title": "归因"}})],
    }
    defaults.update(overrides)
    return state.apply(**defaults)


def test_reflector_scope_lists_available_fields():
    """反思提示词必须携带数仓可用字段清单（判定边界的客观依据）。"""
    from core.orchestrator.nodes import _reflector_available_scope

    scope = _reflector_available_scope()
    assert "数仓可用字段清单" in scope
    assert "order_amount" in scope and "province" in scope
    assert "流量" in scope  # 明示清单外概念不得作为重规划理由


def test_guard_rejects_out_of_scope_reasons():
    """反思以数仓未采集的维度（流量/活动/异常单）为由判不充分 => 不可执行。"""
    import core.orchestrator.nodes as nodes

    verdict = {
        "verdict": "insufficient",
        "reasons": ["缺少对订单量、客单价、流量、活动、异常单等影响因素的归因分析"],
    }
    assert nodes._insufficient_is_actionable(verdict, _critic_state()) is False


def test_guard_rejects_when_reasons_all_covered_by_products():
    """理由提到的概念已被本轮产物覆盖 => 属分析深度诉求，不可执行。"""
    import core.orchestrator.nodes as nodes

    verdict = {
        "verdict": "insufficient",
        "reasons": ["最终结果只列出下降幅度较大的地区及指标，未解释具体下滑原因"],
        "missing": ["缺少订单量与买家数的归因分析"],
    }
    assert nodes._insufficient_is_actionable(verdict, _critic_state()) is False


def test_guard_allows_actionable_gap_within_scope():
    """理由指向可用域内尚未取到的数据（如品类）=> 可执行，允许重规划。"""
    import core.orchestrator.nodes as nodes

    verdict = {
        "verdict": "insufficient",
        "reasons": ["未按品类拆分下滑贡献，无法定位品类级主因"],
        "missing": ["取品类维度的两期明细"],
    }
    # 产物列为 province/gmv/orders/buyers，品类字段未取到 => 可执行
    assert nodes._insufficient_is_actionable(verdict, _critic_state()) is True


def test_guard_rejects_when_replan_makes_no_progress():
    """产物指纹与上次重规划相同 => 重规划无进展，直接综合（防空转）。"""
    import core.orchestrator.nodes as nodes

    state = _critic_state()
    state = state.apply(last_replan_fingerprint=nodes._artifact_fingerprint(state))
    verdict = {
        "verdict": "insufficient",
        "reasons": ["未按品类拆分下滑贡献"],
        "missing": ["取品类维度明细"],
    }
    assert nodes._insufficient_is_actionable(verdict, state) is False


def test_critic_guard_converts_unsatisfiable_replan_to_synthesize(monkeypatch):
    """LLM 反思判定不充分但缺口不可执行时，critic 直接转综合（不空烧重试）。"""
    import core.orchestrator.nodes as nodes

    monkeypatch.setattr(nodes, "_resolve_llm", lambda: object())
    monkeypatch.setattr(
        nodes,
        "_llm_json",
        lambda llm, system, user: {
            "verdict": "insufficient",
            "reasons": ["缺少对订单量、客单价、流量、活动、异常单等影响因素的归因分析"],
        },
    )
    out = nodes.critic_node(_critic_state())
    assert out.phase == "synthesize"
    assert out.error_context.retries == 0  # 未消耗重试额度（非空转重规划）


def test_critic_replans_and_records_progress_fingerprint(monkeypatch):
    """缺口可执行时照常重规划，并记录本轮产物指纹供下轮无进展判定。"""
    import core.orchestrator.nodes as nodes

    monkeypatch.setattr(nodes, "_resolve_llm", lambda: object())
    monkeypatch.setattr(
        nodes,
        "_llm_json",
        lambda llm, system, user: {
            "verdict": "insufficient",
            "reasons": ["未按品类拆分下滑贡献"],
            "missing": ["取品类维度明细"],
        },
    )
    state = _critic_state()
    out = nodes.critic_node(state)
    assert out.phase == "plan"
    assert out.error_context.retries == 1
    assert out.last_replan_fingerprint == nodes._artifact_fingerprint(state)


# --------------------------------------------------------------------------- #
# 无数据诚实守卫（2026-09 审计修复：编造时段严禁产出归因报告）
# --------------------------------------------------------------------------- #
def test_parse_explicit_time_window():
    """显式年份/月份解析：年月 / 整年；无年份月份返回 None 由调用方锚定。"""
    from agent.time_utils import parse_explicit_time_window

    assert parse_explicit_time_window("分析一下 2030 年 5 月第二周比第三周 GMV 下滑") == (
        "2030-05-01",
        "2030-06-01",
    )
    assert parse_explicit_time_window("2024年GMV是多少") == ("2024-01-01", "2025-01-01")
    assert parse_explicit_time_window("12月GMV是多少") is None
    assert parse_explicit_time_window("上个月GMV") is None


def test_time_window_outside_domain():
    """数据域守卫判据：窗口整体晚于数据域上界 = 必然空集（确定性可证）。"""
    from agent.time_utils import time_window_outside_domain
    from semantic.dsl_schema import TimeFilter

    future = TimeFilter.model_validate(
        {"range_type": "absolute", "absolute": {"start": "2030-05-01", "end": "2030-06-01"}}
    )
    past = TimeFilter.model_validate(
        {"range_type": "absolute", "absolute": {"start": "2024-05-01", "end": "2024-06-01"}}
    )
    assert time_window_outside_domain(future) is True
    assert time_window_outside_domain(past) is False


def test_diagnostic_dsl_pair_respects_explicit_time():
    """兜底两期对必须尊重用户显式时间（严禁静默替换成 2024-05 域内窗口）。"""
    from core.orchestrator.nodes import _diagnostic_dsl_pair, _scalar_dsl

    baseline, current = _diagnostic_dsl_pair("分析一下 2030 年 5 月 GMV 下滑的原因")
    assert baseline["time_filter"]["absolute"]["start"] == "2030-05-01"
    assert current["time_filter"]["absolute"]["end"] == "2030-06-01"
    # 两期相邻不重叠（半开区间共用分界）
    assert baseline["time_filter"]["absolute"]["end"] == current["time_filter"]["absolute"]["start"]

    scalar = _scalar_dsl("2030 年 5 月的 GMV 总额是多少？")
    assert scalar["time_filter"]["absolute"] == {"start": "2030-05-01", "end": "2030-06-01"}

    # 无显式时间 => 缺省锚不变（评测确定性回归锚点）
    b_default, c_default = _diagnostic_dsl_pair("为什么 GMV 下滑了")
    assert b_default["time_filter"]["absolute"] == {"start": "2024-05-01", "end": "2024-05-08"}
    assert c_default["time_filter"]["absolute"] == {"start": "2024-05-08", "end": "2024-05-15"}


def test_run_agent_fabricated_year_reports_no_data(tmp_path, monkeypatch):
    """E2E：编造年份（2030）严禁产出归因报告，必须如实说明无数据。

    回归锚点（2026-09 审计）：此前兜底窗口硬编码 2024-05，用域内数据冒充
    用户问的 2030 时段产出"下滑归因"报告 = 数据造假。
    """
    from config import settings

    monkeypatch.setattr(settings, "WORKSPACE_ROOT", tmp_path)
    trace = run_agent(
        "分析一下 2030 年 5 月第二周比第三周 GMV 下滑的原因，按地区定位",
        session_id="fab-year",
    )
    assert trace.phase == "done"
    # 严禁沙箱假产物：无数据时不做因子分解/维度下钻
    assert not any(a["kind"] == "summary" for a in trace.artifacts)
    assert not any(a["kind"] == "echarts" for a in trace.artifacts)
    # 取数被守卫拦截（不产出数据集）
    query_steps = [s for s in trace.steps if s["tool"] == "execute_dsl_query"]
    assert query_steps and all(not s["ok"] for s in query_steps)
    # 报告如实说明超界与数据域边界，且不出现编造结论话术
    assert "无任何数据" in trace.report
    assert "2024-06-30" in trace.report
    assert "驱动因子分解" not in trace.report
    assert "归因矩阵" not in trace.report


def test_critic_short_circuits_on_no_data_reason(monkeypatch):
    """时间域守卫拦截后 critic 直接转综合（严禁重规划空转、不进 LLM 反思）。"""
    import core.orchestrator.nodes as nodes

    calls: list[int] = []
    monkeypatch.setattr(nodes, "_resolve_llm", lambda: calls.append(1) or object())
    state = _critic_state(
        no_data_reason="查询时间范围 2030-05-01 ~ 2030-06-01 整体晚于数仓数据域上界",
        datasets={},
        artifacts=[],
    )
    out = nodes.critic_node(state)
    assert out.phase == "synthesize"
    assert out.error_context.retries == 0
    assert not calls


def test_critic_short_circuits_on_all_empty_datasets(monkeypatch):
    """全部数据集 0 行：critic 转综合如实说明（不判'缺归因产物'触发重规划）。"""
    import core.orchestrator.nodes as nodes

    calls: list[int] = []
    monkeypatch.setattr(nodes, "_resolve_llm", lambda: calls.append(1) or object())
    state = _critic_state(
        datasets={
            "s1_v0": {"path": "a.parquet", "rows": 0, "columns": ["province", "gmv"]},
            "s1_v1": {"path": "b.parquet", "rows": 0, "columns": ["province", "gmv"]},
        },
        artifacts=[],
    )
    out = nodes.critic_node(state)
    assert out.phase == "synthesize"
    assert out.error_context.retries == 0
    assert not calls


def test_analysis_template_guards_empty_inputs():
    """依赖数据集全 0 行 => 无匹配数据模板（严禁在空 DataFrame 上跑分解/下钻）。"""
    import core.orchestrator.nodes as nodes
    from core.orchestrator.state import AgentState, PlanStep

    state = AgentState(
        user_query="分析一下 2024 年 5 月 GMV 下滑的原因",
        plan_steps=[
            PlanStep(id="s1", goal="取数", kind="query"),
            PlanStep(id="s2", goal="沙箱内做乘法因子分解", kind="analyze", depends_on=["s1"]),
        ],
        datasets={"s1_v0": {"path": "a.parquet", "rows": 0, "columns": ["province", "gmv"]}},
        step_outputs={"s1": ["s1_v0"]},
    )
    code = nodes._analysis_template(state, state.plan_steps[1])
    assert "无匹配数据" in code
    # 严禁落到因子分解模板（不读数据集、不产出分解小节）
    assert "read_input" not in code
    assert "驱动因子分解" not in code


def test_synthesize_no_data_skips_llm(monkeypatch):
    """无数据时 synthesize 跳过 LLM 综合，输出确定性数据说明（严禁编故事）。"""
    import core.orchestrator.nodes as nodes
    from core.orchestrator.state import AgentState

    calls: list[str] = []
    monkeypatch.setattr(nodes, "_resolve_llm", lambda: object())
    monkeypatch.setattr(
        nodes, "_synthesize_with_llm", lambda state, material: calls.append(material)
    )
    state = AgentState(user_query="2030 年 5 月 GMV 下滑原因", no_data_reason="查询时间范围超界")
    out = nodes.synthesize_node(state)
    assert out.phase == "done"
    assert not calls
    assert "无法进行" in out.report
    assert "2024-06-30" in out.report
    assert "不会以其他时段的数据代替作答" in out.report
