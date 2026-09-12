"""审计修复 R1/R2/R3 的回归锚点：报告综合重构 / 归因口径对齐 / 降级保护。

- R1：Synthesizer 商业分析师提示词契约 + LLM 四段式综合 + 反契约 JSON 防御
  + 确定性兜底的人读数值渲染（严禁 raw dict/json 直出给用户）；
- R2：归因拆分强制继承总览 WHERE 过滤与时间窗口（口径对齐层）；
- R3：自愈额度耗尽 => 降级 Summarize 简报，严禁吐未加工 scratchpad/工具结果。
"""

from __future__ import annotations

import json

import pandas as pd
import pytest

from core.orchestrator.state import MAX_RETRIES, AgentState, Artifact, ToolRecord


# --------------------------------------------------------------------------- #
# R1：Synthesizer 提示词契约
# --------------------------------------------------------------------------- #
def test_synthesizer_prompt_contract():
    """商业分析师提示词必须含四段式标题、raw JSON 禁令与人读数值要求。"""
    from core.orchestrator.prompts import SYNTHESIZER_SYSTEM

    assert "资深商业数据分析师" in SYNTHESIZER_SYSTEM
    assert "严禁向用户输出 raw dict/json" in SYNTHESIZER_SYSTEM
    assert "### 核心结论" in SYNTHESIZER_SYSTEM
    assert "### 归因维度定位" in SYNTHESIZER_SYSTEM
    assert "### 驱动因素分析（买家数/客单价/转化率）" in SYNTHESIZER_SYSTEM
    assert "### 业务假设与排查建议" in SYNTHESIZER_SYSTEM
    assert "万元" in SYNTHESIZER_SYSTEM and "主要矛盾" in SYNTHESIZER_SYSTEM
    # 归因维度口径纪律：只呈现材料实际下钻的维度并说明入选依据
    assert "归因维度口径" in SYNTHESIZER_SYSTEM


def test_degraded_summarizer_prompt_contract():
    """降级简报器提示词必须带惩罚约束：禁内部参数、必须注明口径差异。"""
    from core.orchestrator.prompts import DEGRADED_SUMMARIZER_SYSTEM

    assert "惩罚约束" in DEGRADED_SUMMARIZER_SYSTEM
    assert "严禁输出 raw dict/json" in DEGRADED_SUMMARIZER_SYSTEM
    assert "数据口径差异" in DEGRADED_SUMMARIZER_SYSTEM
    assert "### 部分结论（基于已获取数据）" in DEGRADED_SUMMARIZER_SYSTEM


# --------------------------------------------------------------------------- #
# R1：确定性兜底渲染（无 LLM 路径同样禁 raw 直出）
# --------------------------------------------------------------------------- #
def _summary_state(tmp_path) -> AgentState:
    """构造带分省归因 summary 产物的状态（模拟沙箱 analyze 产物）。"""
    return AgentState(
        session_id="r1",
        turn_id="t1",
        trace_id="tr1",
        user_query="分析一下 2024 年 5 月第一周比第二周 GMV 下滑的原因，按地区定位",
        artifacts=[
            Artifact(
                kind="summary",
                name="s2",
                payload={
                    "summary": {
                        "title": "两期对比与分省归因",
                        "metrics": {"baseline": 669300.0, "current": 616800.0, "delta": -52500.0},
                        "table": {
                            "columns": ["province", "baseline", "current", "delta", "share"],
                            "rows": [
                                ["北京", 200000.0, 120000.0, -80000.0, -0.65],
                                ["浙江", 150000.0, 140000.0, -10000.0, -0.08],
                            ],
                        },
                        "findings": [
                            "GMV 从 669300.00 变至 616800.00，下滑 -7.8%",
                            "主要矛盾省份 [北京] 贡献了 65% 的总偏差",
                        ],
                    }
                },
            )
        ],
    )


def test_synthesize_deterministic_fallback_is_human_readable(tmp_path, monkeypatch):
    """无 LLM 时综合兜底渲染：metrics 万元化、归因表 Markdown 化，禁 raw dict。"""
    from config import settings
    from core.orchestrator.nodes import synthesize_node

    monkeypatch.setattr(settings, "WORKSPACE_ROOT", tmp_path)
    out = synthesize_node(_summary_state(tmp_path))
    report = out.report
    assert "66.93 万元" in report and "61.68 万元" in report
    assert "| 北京 |" in report  # 归因表渲染为 Markdown 表格
    assert '{"baseline"' not in report  # 严禁 raw JSON 直出
    assert "json.dumps" not in report
    assert report.count("### 两期对比与分省归因") == 1


class _FakeLLM:
    """契约内 LLM 桩：返回四段式商业报告。"""

    def __init__(self, text: str) -> None:
        self._text = text

    def chat(self, messages):
        return self._text


def test_synthesize_llm_report_replaces_raw_rendering(tmp_path, monkeypatch):
    """LLM 可用时综合报告采用四段式商业叙事（不再直出内部渲染）。"""
    from config import settings
    from core.orchestrator import nodes as orch_nodes
    from core.orchestrator.nodes import synthesize_node

    monkeypatch.setattr(settings, "WORKSPACE_ROOT", tmp_path)
    four_part = (
        "### 核心结论\nGMV 下滑 7.8%，主要矛盾是北京（贡献 65%）。\n\n"
        "### 区域归因定位\n北京下滑 8.0 万元。\n\n"
        "### 驱动因素分析（买家数/客单价/转化率）\n本轮数据未覆盖。\n\n"
        "### 业务假设与排查建议\n排查北京促销活动退出影响。"
    )
    monkeypatch.setattr(orch_nodes, "_resolve_llm", lambda: _FakeLLM(four_part))
    out = synthesize_node(_summary_state(tmp_path))
    assert out.report == four_part
    assert "### 核心结论" in out.report and "### 业务假设与排查建议" in out.report


def test_synthesize_rejects_raw_json_llm_output(tmp_path, monkeypatch):
    """LLM 违反契约回吐 JSON => 视为失败，回落确定性分析师渲染（不直出 raw）。"""
    from config import settings
    from core.orchestrator import nodes as orch_nodes
    from core.orchestrator.nodes import synthesize_node

    monkeypatch.setattr(settings, "WORKSPACE_ROOT", tmp_path)
    raw = json.dumps({"baseline": 669300.0, "current": 616800.0}, ensure_ascii=False)
    monkeypatch.setattr(orch_nodes, "_resolve_llm", lambda: _FakeLLM(raw))
    out = synthesize_node(_summary_state(tmp_path))
    assert '{"baseline"' not in out.report  # 反契约输出被拦截
    assert "66.93 万元" in out.report  # 走确定性人读兜底


# --------------------------------------------------------------------------- #
# R2：口径对齐层
# --------------------------------------------------------------------------- #
def test_inherit_overview_scope_inherits_filters_and_window():
    """拆分 DSL 缺失状态过滤与时间窗口时，强制继承总览口径并产出说明。"""
    from core.orchestrator.nodes import _inherit_overview_scope

    overview = {
        "metrics": [{"kind": "aggregate", "field": "order_amount", "agg": "sum", "alias": "gmv"}],
        "filters": [
            {"field": "pay_status", "operator": "eq", "value": "SUCCESS"},
            {"field": "order_amount", "operator": "gte", "value": 10},
        ],
        "time_filter": {
            "range_type": "absolute",
            "absolute": {"start": "2024-05-01", "end": "2024-05-08"},
        },
    }
    split = {"metrics": overview["metrics"], "dimensions": [{"field": "province"}]}
    aligned, notes = _inherit_overview_scope(split, [overview])
    # 状态类 eq 条件被继承；范围类 gte 条件不盲继承
    fields = [f["field"] for f in aligned["filters"]]
    assert "pay_status" in fields and "order_amount" not in fields
    assert aligned["time_filter"] == overview["time_filter"]
    assert any("pay_status" in n for n in notes) and any("时间窗口" in n for n in notes)


def test_inherit_overview_scope_keeps_declared_window():
    """拆分 DSL 已声明时间窗口（两期差异属合法）时保留，但记入口径说明。"""
    from core.orchestrator.nodes import _inherit_overview_scope

    overview = {
        "filters": [{"field": "pay_status", "operator": "eq", "value": "SUCCESS"}],
        "time_filter": {
            "range_type": "absolute",
            "absolute": {"start": "2024-05-01", "end": "2024-05-08"},
        },
    }
    split = {
        "dimensions": [{"field": "province"}],
        "time_filter": {
            "range_type": "absolute",
            "absolute": {"start": "2024-05-08", "end": "2024-05-15"},
        },
    }
    aligned, _notes = _inherit_overview_scope(split, [overview])
    assert aligned["time_filter"]["absolute"]["start"] == "2024-05-08"


def test_diagnostic_dsl_pair_carries_dimension_pool():
    """诊断兜底两期对未点名维度时取候选维度池（而非只取省份），两期同口径。"""
    from core.orchestrator.nodes import _diagnostic_dsl_pair

    base, curr = _diagnostic_dsl_pair("分析 5 月第一周比第二周 GMV 下滑原因")
    for dsl in (base, curr):
        dims = [d["field"] for d in dsl["dimensions"]]
        # 候选池覆盖省份/品牌/品类：由分析层按信息增益裁决主因维度
        assert dims == ["province", "brand", "category"]
        assert {"field": "pay_status", "operator": "eq", "value": "SUCCESS"} in dsl["filters"]
    assert base["time_filter"]["absolute"]["end"] == curr["time_filter"]["absolute"]["start"]


def test_diagnostic_dsl_pair_honors_explicit_dimension():
    """用户显式点名维度时只取该维度（"按品类"不得再带省份）。"""
    from core.orchestrator.nodes import _diagnostic_dsl_pair

    base, curr = _diagnostic_dsl_pair("分析 5 月第一周比第二周 GMV 下滑原因，按品类定位")
    for dsl in (base, curr):
        assert [d["field"] for d in dsl["dimensions"]] == ["category"]


def test_diagnostic_e2e_report_contains_dimension_attribution(tmp_path, monkeypatch):
    """端到端：诊断问题报告必须含维度归因定位与驱动因素叙述（不再只有对比图）。"""
    from config import settings
    from core.orchestrator.agent import run_agent

    monkeypatch.setattr(settings, "WORKSPACE_ROOT", tmp_path)
    trace = run_agent(
        "分析一下 2024 年 5 月第一周比第二周 GMV 下滑的原因，按地区定位", session_id="r2e2e"
    )
    assert trace.phase == "done"
    assert "主要矛盾" in trace.report  # 主因维度取值叙述
    # ECharts 必须是维度下钻对比（不再是 baseline/current 两根柱）
    charts = [a for a in trace.artifacts if a["kind"] == "echarts"]
    assert charts and charts[0]["payload"]["xAxis"]["data"] != ["baseline", "current"]


# --------------------------------------------------------------------------- #
# R3：额度耗尽降级保护
# --------------------------------------------------------------------------- #
def _exhausted_state(tmp_path) -> AgentState:
    """重试额度耗尽 + 无产物的状态（模拟多轮自愈失败后的终局）。"""
    state = AgentState(
        session_id="r3",
        turn_id="t1",
        trace_id="tr1",
        user_query="分析一下 2024 年 5 月第一周比第二周 GMV 下滑的原因",
        scratchpad=["[planner] heuristic", "[critic-llm] LLM 反思判定产物不充分"],
        tool_calls=[ToolRecord(tool="run_code", ok=False, error="KeyError: 'uv'（内部调试细节）")],
    )
    for i in range(MAX_RETRIES + 1):
        state.error_context.record(f"error {i}: KeyError 内部细节")
    return state


def test_exhausted_retries_degrade_to_structured_brief(tmp_path, monkeypatch):
    """额度耗尽 => 降级简报：结构化小节 + 口径差异说明，无内部轨迹泄漏。"""
    from config import settings
    from core.orchestrator.nodes import synthesize_node

    monkeypatch.setattr(settings, "WORKSPACE_ROOT", tmp_path)
    out = synthesize_node(_exhausted_state(tmp_path))
    assert out.phase == "done"
    assert "### 部分结论（基于已获取数据）" in out.report
    assert "### 数据口径差异说明" in out.report
    # 严禁吐内部调试信息：scratchpad / 工具错误原文不得出现
    assert "critic-llm" not in out.report
    assert "KeyError" not in out.report
    assert "自愈记录" not in out.report
    assert "执行轨迹" not in out.report


def test_exhausted_retries_llm_brief_with_penalty_constraints(tmp_path, monkeypatch):
    """LLM 可用时降级路径调用带惩罚约束的 Summarize，输出结构化简报。"""
    from config import settings
    from core.orchestrator import nodes as orch_nodes
    from core.orchestrator.nodes import synthesize_node

    monkeypatch.setattr(settings, "WORKSPACE_ROOT", tmp_path)
    brief = (
        "### 部分结论（基于已获取数据）\n北京、浙江等省份出现下滑。\n\n"
        "### 已定位的明细\n（表格）\n\n### 数据口径差异说明\n明细合计与总览存在口径出入。\n\n"
        "### 后续建议\n缩小范围重试。"
    )
    monkeypatch.setattr(orch_nodes, "_resolve_llm", lambda: _FakeLLM(brief))
    out = synthesize_node(_exhausted_state(tmp_path))
    assert out.report == brief  # 降级简报来自 Summarize 模型
    assert "KeyError" not in out.report  # 未加工错误仍被拦截


def test_pure_query_path_unaffected_by_diagnostic_dimensions(tmp_path, monkeypatch):
    """非诊断问题兜底取数保持单期总量（标量问题不带省份维度）。"""
    from config import settings
    from core.orchestrator.agent import run_agent

    monkeypatch.setattr(settings, "WORKSPACE_ROOT", tmp_path)
    trace = run_agent("2024 年 5 月成功支付订单的 GMV 总额是多少？", session_id="r3scalar")
    assert trace.phase == "done"
    assert "查询答案：" in trace.report
    assert "万元" in trace.report


# --------------------------------------------------------------------------- #
# M2：维度下钻约束（"没问分省却走了分省"回归锚点）
# --------------------------------------------------------------------------- #
def test_explicit_dimensions_normalizes_dimension_terms():
    """维度词归一：地区/省份/大区 -> province，品类/类目 -> category。"""
    from core.orchestrator.nodes import _explicit_dimensions

    assert _explicit_dimensions("按地区定位下滑主因") == ["province"]
    assert _explicit_dimensions("看看品类结构") == ["category"]
    assert _explicit_dimensions("按品牌拆一下") == ["brand"]
    # 泛化表述不锚定具体维度 => 交由信息增益自动择优
    assert _explicit_dimensions("分析下滑原因") == []


def test_diagnostic_dimension_pool_explicit_beats_pool():
    """显式维度优先于候选池：点名品类时不得再夹带省份。"""
    from core.orchestrator.nodes import _diagnostic_dimension_pool

    assert _diagnostic_dimension_pool("按品类看下滑原因") == ["category"]
    assert _diagnostic_dimension_pool("为什么下滑") == ["province", "brand", "category"]


def test_heuristic_plan_layers_factor_before_dimension():
    """诊断兜底 DAG 强制分层：先因子分解，再维度下钻，两者并行喂给综合。"""
    from core.orchestrator.nodes import _heuristic_plan

    steps = _heuristic_plan("分析一下 5 月 GMV 为什么下滑")
    kinds = [(s.id, s.kind) for s in steps]
    assert kinds == [("s1", "query"), ("s2", "analyze"), ("s3", "analyze"), ("s4", "synthesize")]
    factor_step, dim_step = steps[1], steps[2]
    assert "因子" in factor_step.goal  # 第一步先拆量/价
    assert "信息增益" in dim_step.goal and "候选维度池" in dim_step.goal
    assert factor_step.depends_on == ["s1"] and dim_step.depends_on == ["s1"]
    assert steps[3].depends_on == ["s2", "s3"]  # 综合消费因子与维度双产物


def test_heuristic_plan_honors_explicit_dimension_in_goal():
    """显式点名维度时下钻步骤目标写明该维度（不再声称全维度扫描）。"""
    from core.orchestrator.nodes import _heuristic_plan

    steps = _heuristic_plan("按品类分析 GMV 为什么下滑")
    assert "category" in steps[2].goal
    assert "候选维度池" not in steps[2].goal


def test_drilldown_template_emits_selection_rationale_and_no_default_province():
    """下钻模板必须给出入选维度依据，且不把省份写死为主图维度。"""
    from core.orchestrator.nodes import _drilldown_template

    code = _drilldown_template(["s1_v0", "s1_v1"])
    assert "信息增益" in code and "优先下钻该维度定位主因" in code
    assert "候选维度自动发现" in code  # 维度由数据列动态发现
    assert 'primary["dimension"]' in code  # 主图维度取信息增益胜出者
    assert 'table["rows"]' in code and "save_echarts_spec" in code


def test_e2e_unprompted_dimension_is_not_province_only(tmp_path, monkeypatch):
    """端到端：未点名维度的诊断问题不得只按分省下钻。"""
    from config import settings
    from core.orchestrator.agent import run_agent

    monkeypatch.setattr(settings, "WORKSPACE_ROOT", tmp_path)
    trace = run_agent("分析一下 2024 年 5 月第一周比第二周 GMV 下滑的原因", session_id="m2pool")
    assert trace.phase == "done"
    summaries = [a["payload"]["summary"] for a in trace.artifacts if a["kind"] == "summary"]
    drill = next(s for s in summaries if s["title"] == "维度信息增益归因")
    # 入选依据话术必须存在（解释"为什么下钻这个维度"）
    assert any("候选维度信息增益扫描" in f for f in drill["findings"])
    # 候选维度全景必须覆盖多个维度（而非只扫省份）
    gains = (drill["extra"] or {}).get("gain_table") or {}
    dims = [row[0] for row in gains.get("rows", [])]
    assert len(dims) >= 2 and "province" in dims
    # 未点名维度时不得出现"分省"字样（分省不是默认口径）
    assert "分省" not in trace.report


def test_e2e_explicit_category_dimension_only_drills_category(tmp_path, monkeypatch):
    """端到端：点名品类时主图与归因矩阵只呈现品类。"""
    from config import settings
    from core.orchestrator.agent import run_agent

    monkeypatch.setattr(settings, "WORKSPACE_ROOT", tmp_path)
    trace = run_agent(
        "分析一下 2024 年 5 月第一周比第二周 GMV 下滑的原因，按品类定位",
        session_id="m2cat",
    )
    assert trace.phase == "done"
    summaries = [a["payload"]["summary"] for a in trace.artifacts if a["kind"] == "summary"]
    drill = next(s for s in summaries if s["title"] == "维度信息增益归因")
    assert (drill["extra"] or {}).get("primary_dimension") == "category"
    assert drill["table"]["columns"][0] == "category"
    charts = [a for a in trace.artifacts if a["kind"] == "echarts"]
    assert charts and "品类" in charts[0]["payload"]["title"]["text"]


def test_planner_prompt_states_dimension_discipline():
    """Planner 提示词必须写明维度下钻纪律与"先因子后维度"分层。"""
    from core.orchestrator.prompts import PLANNER_FEWSHOT, PLANNER_SYSTEM

    assert "维度下钻纪律" in PLANNER_SYSTEM
    assert "先因子后维度" in PLANNER_SYSTEM
    assert "候选维度池" in PLANNER_SYSTEM
    assert "没问分省却只出分省" in PLANNER_SYSTEM
    assert "候选维度池" in PLANNER_FEWSHOT


# --------------------------------------------------------------------------- #
# 时间粒度口径对齐（LLM 漂移修复：契约默认 day 不得覆盖问句语义粒度）
# --------------------------------------------------------------------------- #
def test_align_time_granularity_fixes_llm_default_day():
    """同一时间窗口下，LLM 的 day 粒度被问句语义粒度纠正（回归锚点）。

    此前 DSL 契约 granularity 默认 day，LLM 漏写该字段时"上个月"会被切成
    日粒度，同一问句多次独立调用出现 month/day 漂移（多轮评测偶发失败）。
    """
    from agent.semantic_check import align_time_granularity
    from semantic.dsl_schema import QueryDSL

    dsl = QueryDSL.model_validate(
        {
            "metrics": [
                {"kind": "aggregate", "field": "order_amount", "agg": "sum", "alias": "gmv"}
            ],
            "time_filter": {
                "granularity": "day",
                "range_type": "relative",
                "relative": {"amount": 1, "unit": "month", "mode": "calendar"},
            },
        }
    )
    out = align_time_granularity(dsl, "上个月的GMV是多少？")
    assert out.time_filter.granularity.value == "month"


def test_align_time_granularity_keeps_genuine_daily_intent():
    """问句本就要求日粒度时不误改（"每日"语义必须保留 day）。"""
    from agent.semantic_check import align_time_granularity
    from semantic.dsl_schema import QueryDSL

    dsl = QueryDSL.model_validate(
        {
            "metrics": [
                {"kind": "aggregate", "field": "order_amount", "agg": "sum", "alias": "gmv"}
            ],
            "time_filter": {
                "granularity": "day",
                "range_type": "relative",
                "relative": {"amount": 30, "unit": "day", "mode": "trailing"},
            },
        }
    )
    out = align_time_granularity(dsl, "近30天每日GMV是多少？")
    assert out.time_filter.granularity.value == "day"


def test_align_time_granularity_ignores_different_windows():
    """时间窗口本身不一致时不改粒度（真实语义差异交由校验与自愈处理）。"""
    from agent.semantic_check import align_time_granularity
    from semantic.dsl_schema import QueryDSL

    dsl = QueryDSL.model_validate(
        {
            "metrics": [
                {"kind": "aggregate", "field": "order_amount", "agg": "sum", "alias": "gmv"}
            ],
            "time_filter": {
                "granularity": "day",
                "range_type": "relative",
                "relative": {"amount": 3, "unit": "month", "mode": "trailing"},
            },
        }
    )
    out = align_time_granularity(dsl, "上个月的GMV是多少？")
    # 窗口（3 个月 vs 1 个月）不同 => 粒度保持 LLM 原样，不做臆测改写
    assert out.time_filter.granularity.value == "day"


# --------------------------------------------------------------------------- #
# 辅助：归因模板在沙箱外的等价性验证（pandas 逻辑单测）
# --------------------------------------------------------------------------- #
def test_region_attribution_math_matches_template_logic():
    """维度加法归因的数学与模板一致：Δ = 当前期 - 基线期，share 按 |Δ| 归一。"""
    from core.skills.decomposition import additive_decomposition

    df = pd.DataFrame(
        {
            "dimension": ["北京", "浙江", "北京", "浙江"],
            "value": [200000.0, 150000.0, 120000.0, 140000.0],
            "period": ["baseline", "baseline", "current", "current"],
        }
    )
    out = additive_decomposition(df)
    assert out["total_delta"] == pytest.approx(-90000.0, abs=0.01)
    top = out["items"][0]
    assert top["dimension"] == "北京"
    assert abs(top["share"]) == pytest.approx(8 / 9, abs=0.01)  # 下滑贡献为负份额
