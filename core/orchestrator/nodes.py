"""图节点实现：Clarify / Planner / DSLQuery / CodeExec / Critic / Synthesize。

节点函数签名统一为 ``Callable[[AgentState], AgentState]``（增量应用语义）：

- ``clarify_node``：歧义检测（确定性规则 + 可选 LLM），不达标即置
  phase=clarify 触发 HITL 中断；携带 human_reply 重入时把答复并入查询；
- ``planner_node``：LLM 规划（JSON 计划契约）+ 确定性启发式兜底（离线
  可运行）；产出 plan_steps（DAG）；
- ``dsl_query_node``：执行 query 步骤——经 retrieval 门面
  （网关守卫 + RLS + 编译 + 执行 + Parquet 物化），产物登记 datasets；
- ``code_exec_node``：执行 analyze 步骤——组装沙箱上下文（数据集名 +
  技能模板）跑 run_code，产物（summary / echarts）登记 artifacts；
- ``critic_node``：完整性/正确性/现实一致性三检，不充分且可补 => 重规划
  （回到 planner，受 MAX_RETRIES 约束）；
- ``synthesize_node``：汇总执行轨迹与产物为结构化 Markdown 报告 +
  ECharts 规格引用，置 phase=done。

所有节点对异常结构化处理：error_context.record()，超限置终态（不吞错、
不裸抛——编排层对上层永远返回可序列化状态）。
"""

from __future__ import annotations

import json
import time
from datetime import date
from typing import Any

from agent.heuristic import region_provinces
from agent.time_utils import parse_explicit_time_window
from audit.logging import get_logger
from core.orchestrator import events
from core.orchestrator.prompts import (
    DEGRADED_SUMMARIZER_SYSTEM,
    PLANNER_SYSTEM,
    REFLECTOR_SYSTEM,
    SYNTHESIZER_SYSTEM,
    planner_prompt,
)
from core.orchestrator.state import (
    MAX_RETRIES,
    AgentState,
    Artifact,
    PlanStep,
    ToolRecord,
)
from core.sandbox.api import run_code
from core.sandbox.ast_guard import static_check
from core.skills.decomposition import multiplicative_decomposition
from core.skills.drilldown import drilldown_by_information_gain
from semantic.catalog import DRILLDOWN_DIM_FIELDS as DRILLDOWN_DIM_FIELDS
from semantic.catalog import REGION_PROVINCE_MAPPING as REGION_PROVINCE_MAPPING

logger = get_logger("core.orchestrator")


# --------------------------------------------------------------------------- #
# 维度下钻候选（审计修复 M2：杜绝"没问分省却默认走分省"）
# --------------------------------------------------------------------------- #
# 维度词 -> 语义字段（用户显式指定维度时据此确定下钻口径）。
_DIMENSION_TERMS: dict[str, str] = {
    "province": "province",
    "省份": "province",
    "省": "province",
    "地区": "province",
    "地域": "province",
    "区域": "province",
    "大区": "province",
    "城市": "province",
    "category": "category",
    "品类": "category",
    "类目": "category",
    "品类结构": "category",
    "brand": "brand",
    "品牌": "brand",
    "店铺": "shop_name",
    "门店": "shop_name",
    "shop_name": "shop_name",
}

# 用户未显式指定维度时的候选维度池（有意收窄，与 DRILLDOWN_DIM_FIELDS 同源）：
# 联合明细同时覆盖 province 与 category，分析层按 info-gain 择优下钻，
# 而非默认向用户呈现分省。高基数字段（shop_name/brand 明细膨胀）不入池，
# 用户显式点名时才取。
_DIAGNOSTIC_DIM_POOL: tuple[str, ...] = DRILLDOWN_DIM_FIELDS


# 维度字段 -> 中文标签（图表标题与归因叙述的人读化；字段名仅保留在矩阵列头）
_DIMENSION_LABELS: dict[str, str] = {
    "province": "省份",
    "category": "品类",
    "brand": "品牌",
    "shop_name": "店铺",
}


def _dimension_label(field: str) -> str:
    """维度字段名 -> 中文标签（未知字段原样返回）。"""
    return _DIMENSION_LABELS.get(field, field)


def _explicit_dimensions(query: str) -> list[str]:
    """用户问题中显式点名的维度字段（按出现顺序去重）。

    - 泛化的"按维度拆分"（"按维度/分维度"）不锚定具体字段 => 返回空，
      交由分析层在候选池内按信息增益自动下钻；
    - "地区/省份/大区/城市"等词统一归一为 province；"品类/类目"-> category；
      "品牌"-> brand；"店铺/门店"-> shop_name。
    """
    found: list[str] = []
    lowered = query.lower()
    for term, field in _DIMENSION_TERMS.items():
        if term in lowered and field not in found:
            found.append(field)
    return found


def _diagnostic_dimension_pool(query: str) -> list[str]:
    """诊断问题的下钻维度池：显式点名的维度优先，否则用联合候选池。

    回归锚点（M2）：此前确定性兜底两期对硬编码 ``dimensions=[province]``，
    用户问"为什么下滑"却未提省份时仍走分省，且分析模板只产出分省图表；
    现改为——显式维度则按显式（如"按品类"只取品类），未显式则取联合候选池
    由信息增益定位主因维度（信息增益胜出者才渲染主图）。
    """
    explicit = _explicit_dimensions(query)
    return explicit if explicit else list(_DIAGNOSTIC_DIM_POOL)


# --------------------------------------------------------------------------- #
# 语义目录摘要（注入 Planner）
# --------------------------------------------------------------------------- #
def schema_digest(enum_values: dict[str, list[str]] | None = None) -> str:
    """语义字段 -> 紧凑文本摘要（Planner 可用字段清单）。

    ``enum_values``：低基数字段的实际取值（SchemaAgent 动态 profiling，
    core.retrieval.profiling）——注入后 Planner 不再臆造过滤字面值。
    """
    from semantic.catalog import COLUMNS

    lines = []
    for name, meta in sorted(COLUMNS.items()):
        line = f"- {name} ({meta.table}.{meta.column}, {meta.dtype})"
        values = (enum_values or {}).get(name)
        if values:
            line += " 可取值: " + "|".join(values)
        lines.append(line)
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# LLM 客户端解析（无配置时 None -> 确定性兜底）
# --------------------------------------------------------------------------- #
def _resolve_llm() -> Any | None:
    try:
        from agent.llm import resolve_default_client

        return resolve_default_client()
    except Exception:  # pragma: no cover - 防御式
        return None


def _llm_json(llm: Any, system: str, user: str) -> dict[str, Any] | None:
    """调用 LLM 并清洗出 JSON 对象；失败返回 None（调用方走兜底）。"""
    try:
        from agent.agent import extract_json
        from providers import chat_text

        text = chat_text(
            llm, [{"role": "system", "content": system}, {"role": "user", "content": user}]
        )
        return extract_json(text)
    except Exception as exc:
        # LLM 调用/解析失败：兜底路径可继续，但故障必须可采集（服务可用性信号）
        logger.warning(
            "LLM 调用或 JSON 解析失败，走确定性兜底",
            extra={"error": f"{type(exc).__name__}: {exc}"[:500]},
        )
        return None


# --------------------------------------------------------------------------- #
# 1) Clarify
# --------------------------------------------------------------------------- #
def clarify_node(state: AgentState) -> AgentState:
    """歧义检测：HITL 门（需求 §2.A ClarificationNode）。

    确定性规则：问题过短/无指标词/无时间锚且是多轮首问 => 请求澄清。
    用户已答复（human_reply）时把答复并入 user_query 并继续。
    """
    query = state.user_query.strip()
    if state.human_reply:
        merged = f"{query}（用户补充：{state.human_reply.strip()}）"
        return state.apply(user_query=merged, human_reply=None, phase="plan", clarification=None)

    metric_words = ("gmv", "销量", "金额", "订单", "退款", "率", "数", "额")
    has_metric = any(w in query.lower() for w in metric_words)
    if len(query) >= 12 and has_metric:
        return state.apply(phase="plan")
    question = (
        "为了准确定位，请补充：1) 你关注的指标（如 GMV / 订单量）与时间范围；"
        "2) 希望按哪个维度（地区/品类/店铺）分析？"
    )
    return state.apply(phase="clarify", clarification=question)


# --------------------------------------------------------------------------- #
# 2) Planner
# --------------------------------------------------------------------------- #
def _heuristic_plan(query: str) -> list[PlanStep]:
    """确定性兜底规划：诊断式 DAG（总量对比 -> 因子分解 -> 维度下钻 -> 综合）。

    触发词：为什么/下滑/下降/上涨/增长/归因/原因。其余问题走单查询+综合。

    诊断 DAG 强制分层（先因子后维度，审计修复 M2）：
    1. s1(query)：两期总量 + 驱动因子（orders/buyers）同源落库；
    2. s2(analyze)：乘法因子分解（GMV = 买家数 × 人均订单数 × 客单价），
       先定位"量跌还是价跌"；
    3. s3(analyze)：维度信息增益下钻（显式维度优先，否则候选池择优），
       定位"哪个维度-取值是主因"；s2/s3 的产物共同喂养综合。
    """
    diagnostic = any(
        w in query for w in ("为什么", "下滑", "下降", "下跌", "上涨", "增长", "归因", "原因")
    )
    if not diagnostic:
        return [
            PlanStep(id="s1", goal=f"查询回答问题所需数据：{query[:40]}", kind="query"),
            PlanStep(id="s2", goal="综合查询结果作答", kind="synthesize", depends_on=["s1"]),
        ]
    explicit = _explicit_dimensions(query)
    if explicit:
        dim_goal = f"按用户指定维度（{'/'.join(explicit)}）做信息增益下钻，输出归因矩阵"
    else:
        dim_goal = (
            f"在候选维度池（{'/'.join(_DIAGNOSTIC_DIM_POOL)}）内做信息增益下钻，"
            "择优定位主因维度，输出归因矩阵与入选依据"
        )
    return [
        PlanStep(
            id="s1",
            goal="取基线期与当前期的指标总量与驱动因子（订单量/买家数）明细",
            kind="query",
        ),
        PlanStep(
            id="s2",
            goal="沙箱内做乘法因子分解（GMV = 买家数 × 人均订单数 × 客单价），定位量跌还是价跌",
            kind="analyze",
            depends_on=["s1"],
        ),
        PlanStep(
            id="s3",
            goal=dim_goal,
            kind="analyze",
            depends_on=["s1"],
        ),
        PlanStep(
            id="s4",
            goal="汇总根因结论、量化贡献并给出建议",
            kind="synthesize",
            depends_on=["s2", "s3"],
        ),
    ]


def _plan_from_llm(payload: dict[str, Any]) -> list[PlanStep] | None:
    """把 LLM 计划 JSON 契约化为 PlanStep 列表（非法即 None 走兜底）。"""
    steps_raw = payload.get("steps")
    if not isinstance(steps_raw, list) or not (1 <= len(steps_raw) <= 6):
        return None
    steps: list[PlanStep] = []
    for item in steps_raw:
        if not isinstance(item, dict):
            return None
        step_id = str(item.get("id", ""))
        kind = item.get("kind")
        goal = str(item.get("goal", "")).strip()
        if not step_id or kind not in ("query", "analyze", "synthesize") or not goal:
            return None
        dsl = item.get("dsl") if isinstance(item.get("dsl"), dict) else None
        code = item.get("code") if isinstance(item.get("code"), str) else None
        if kind == "query" and dsl is None and code is None:
            # query 步骤既无 DSL 也无兜底说明 => 交给 query 节点的启发式 DSL
            pass
        if kind == "analyze" and code is not None:
            # Coder 产码先行静态校验，违规直接拒绝该计划（防注入到沙箱层）
            if not static_check(code).ok:
                return None
        steps.append(
            PlanStep(
                id=step_id,
                goal=goal,
                kind=kind,
                depends_on=[str(d) for d in item.get("depends_on", []) if isinstance(d, str)],
                dsl=dsl,
                code=code,
            )
        )
    if len({s.id for s in steps}) != len(steps):
        return None
    # 依赖必须指向已存在步骤（前向）
    ids = {s.id for s in steps}
    for s in steps:
        if any(d not in ids or d == s.id for d in s.depends_on):
            return None
    return steps


def planner_node(state: AgentState) -> AgentState:
    """规划节点：LLM JSON 计划优先，启发式兜底（需求 §2.A PlannerNode）。

    重规划自愈（行动项 3）：error_context 非空时把最近失败摘要注入 Planner
    提示词——LLM 必须针对性修正计划，而非盲重试。
    """
    llm = _resolve_llm()
    steps: list[PlanStep] | None = None
    if llm is not None:
        # SchemaAgent 动态 profiling（后续项）：低基数字段枚举值注入规划上下文；
        # 探查失败降级为空 dict（不阻断规划主链路），离线/无库环境无副作用
        from core.retrieval.profiling import profile_enum_values

        error_context = "\n".join(state.error_context.errors[-3:]) or None
        payload = _llm_json(
            llm,
            PLANNER_SYSTEM,
            planner_prompt(
                state.user_query,
                schema_digest(profile_enum_values()),
                error_context=error_context,
            ),
        )
        if payload:
            if payload.get("clarification"):
                return state.apply(phase="clarify", clarification=str(payload["clarification"]))
            steps = _plan_from_llm(payload)
    if steps is None:
        steps = _heuristic_plan(state.user_query)
        planner_used = "heuristic"
    else:
        planner_used = "llm"
    events.emit_plan(steps)
    return state.apply(plan_steps=steps, phase="query", scratchpad=[f"[planner] {planner_used}"])


# --------------------------------------------------------------------------- #
# 3) DSLQuery
# --------------------------------------------------------------------------- #
def _split_window_midpoint(start_s: str, end_s: str) -> str:
    """按天数中点把 [start, end) 切成相邻两期（诊断两期对的基线/当前分界）。"""
    start = date.fromisoformat(start_s)
    end = date.fromisoformat(end_s)
    return date.fromordinal((start.toordinal() + end.toordinal()) // 2).isoformat()


def _diagnostic_dsl_pair(query: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """诊断问题的确定性两期 DSL 对（基线/当前相邻窗口）。

    时间窗口（2026-09 审计修复：无数据诚实原则）：用户显式给出年份/月份时
    必须尊重——经 ``parse_explicit_time_window`` 解析后按天数中点切成基线/
    当前两期；未显式给时间才回退 2024-05 缺省锚（评测确定性）。解析出的
    超界窗口由取数执行前的时间域守卫拦截并如实告知——严禁静默替换成域内
    窗口（拿 2024 数据回答用户问的 2030 问题 = 数据造假）。

    维度口径（审计修复 R2 口径对齐 + M2 维度约束）：两期取数同口径同窗口，
    沙箱直接在行级明细上做总量对比 + 维度归因——明细合计与总量天然同源，
    杜绝总览 66.93 万 vs 拆分 61.68 万式的口径矛盾触发无谓重规划。

    维度选择（M2 回归锚点：此前硬编码 ``dimensions=[province]``，用户问
    "为什么下滑"却没提省份时仍机械走分省）：
    - 用户显式点名维度（"按品类/按地区定位"）=> 只取点名维度；
    - 未显式点名 => 取联合候选池（province + category），分析层按信息增益
      裁决主因维度并给出入选依据，不再默认分省。

    驱动因子随取数一并落库（回归锚点：此前只取 GMV 单指标，反思器看到
    "缺订单量/客单价/流量归因"就判不充分，重规划又取同一份数据，空转至
    额度耗尽降级）：订单量 orders 与买家数 buyers 与 GMV 同源同窗口，
    沙箱据此做 GMV = 买家数 × 客单价 的乘法分解，反思器的因子归因诉求
    在首轮即可满足。
    """
    explicit = parse_explicit_time_window(query)
    if explicit:
        baseline_window = {"start": explicit[0], "end": _split_window_midpoint(*explicit)}
        current_window = {"start": _split_window_midpoint(*explicit), "end": explicit[1]}
    else:
        baseline_window = {"start": "2024-05-01", "end": "2024-05-08"}
        current_window = {"start": "2024-05-08", "end": "2024-05-15"}
    metrics = [
        {"kind": "aggregate", "field": "order_amount", "agg": "sum", "alias": "gmv"},
        {"kind": "aggregate", "field": "order_id", "agg": "count", "alias": "orders"},
        {"kind": "aggregate", "field": "user_id", "agg": "count_distinct", "alias": "buyers"},
    ]
    dimensions = [{"field": f} for f in _diagnostic_dimension_pool(query)]
    return (
        {
            "metrics": [dict(m) for m in metrics],
            "dimensions": [dict(d) for d in dimensions],
            "filters": [{"field": "pay_status", "operator": "eq", "value": "SUCCESS"}],
            "time_filter": {
                "range_type": "absolute",
                "absolute": dict(baseline_window),
            },
        },
        {
            "metrics": [dict(m) for m in metrics],
            "dimensions": [dict(d) for d in dimensions],
            "filters": [{"field": "pay_status", "operator": "eq", "value": "SUCCESS"}],
            "time_filter": {
                "range_type": "absolute",
                "absolute": dict(current_window),
            },
        },
    )


def _scalar_dsl(query: str) -> dict[str, Any]:
    """非诊断问题的确定性单期总量 DSL（标量问题附维度与因子无意义）。

    与诊断路径严格区分：诊断取两期维度明细 + 驱动因子；标量问题只回答
    "这个数是多少"，多取指标会让"查询答案"式的单值呈现失效。
    时间窗口同 ``_diagnostic_dsl_pair``：用户显式时间优先，缺省回退
    2024-05 锚；超界窗口由取数执行前的时间域守卫拦截。
    """
    explicit = parse_explicit_time_window(query)
    window = (
        {"start": explicit[0], "end": explicit[1]}
        if explicit
        else {"start": "2024-05-01", "end": "2024-05-15"}
    )
    return {
        "metrics": [{"kind": "aggregate", "field": "order_amount", "agg": "sum", "alias": "gmv"}],
        "filters": [{"field": "pay_status", "operator": "eq", "value": "SUCCESS"}],
        "time_filter": {"range_type": "absolute", "absolute": window},
    }


def _inherit_overview_scope(
    dsl: dict[str, Any], overview_variants: list[dict[str, Any]]
) -> tuple[dict[str, Any], list[str]]:
    """地区拆分 DSL 强制继承总览口径（审计修复 R2：数据清洗与对齐层）。

    规则（确定性、可解释、可在报告中追溯）：
    - filters：总览中的状态类条件（eq/in，如订单状态/退款过滤）若拆分 DSL
      未声明同字段，则逐条继承——拆分明细合计与总览同口径；
    - time_filter：拆分 DSL 未声明时间窗口时继承总览窗口；已声明则保留
      （两期窗口属合法差异），但差异必须记入 notes 供报告"口径差异说明"引用，
      防止无谓的反思重规划；
    - 返回 (对齐后 DSL, 继承/差异说明列表)。继承的 filter 来自已通过网关
      强校验的总览 DSL，字段必在语义目录白名单内，不会引入非法条件。
    """
    notes: list[str] = []
    aligned = json.loads(json.dumps(dsl, ensure_ascii=False))
    filters = [f for f in (aligned.get("filters") or []) if isinstance(f, dict)]
    owned_fields = {f.get("field") for f in filters}
    for variant in overview_variants:
        for f in variant.get("filters") or []:
            if not isinstance(f, dict):
                continue
            field = f.get("field")
            if field in owned_fields or f.get("operator") not in ("eq", "in"):
                continue  # 已声明或范围类条件（金额阈值等）不盲继承
            filters.append(dict(f))
            owned_fields.add(field)
            notes.append(f"已继承总览过滤条件 {field}")
    aligned["filters"] = filters
    if aligned.get("time_filter") is None:
        inherited = next(
            (v.get("time_filter") for v in overview_variants if v.get("time_filter")), None
        )
        if inherited is not None:
            aligned["time_filter"] = json.loads(json.dumps(inherited, ensure_ascii=False))
            notes.append("已继承总览时间窗口")
    return aligned, notes


def _preview_rows(path: str, limit: int = 30) -> list[list[Any]]:
    """读取 Parquet 数据集前 N 行生成审计预览（转 JSON 原生类型）。

    预览失败不阻断取数（返回空列表），审计能力可降级。
    """
    try:
        import pandas as pd

        df = pd.read_parquet(path)
        out: list[list[Any]] = []
        for row in df.head(limit).itertuples(index=False):
            cells: list[Any] = []
            for v in row:
                if v is None or (isinstance(v, float) and v != v):  # NaN
                    cells.append(None)
                elif hasattr(v, "item"):
                    cells.append(v.item())
                else:
                    cells.append(v)
            out.append(cells)
        return out
    except Exception:
        return []


def _normalize_dsl_draft(dsl: dict[str, Any]) -> dict[str, Any]:
    """LLM DSL 草稿的确定性规范化（宽容接受，严格校验）。

    只做无歧义的常见笔误纠正，修不动的原样透传——契约层（网关 validate）
    仍是唯一裁决者，错误信息继续喂回规划节点自愈：
    - dimensions: ["province"]（裸字符串）-> [{"field": "province"}]；
    - time_range -> time_filter（字段名笔误，契约 extra="forbid" 必拒）；
    - 过滤操作符 ge/le -> gte/lte（白名单写法）；
    - 区域词展开（审计修复 M1）：province = '华东' -> province IN (数仓实际省份)。
    """
    d = dict(dsl)
    dims = d.get("dimensions")
    if isinstance(dims, list):
        d["dimensions"] = [
            {"field": item} if isinstance(item, str) else item
            for item in dims
            if isinstance(item, (str, dict))
        ]
    if "time_range" in d and "time_filter" not in d:
        d["time_filter"] = d.pop("time_range")
    filters = d.get("filters")
    if isinstance(filters, list):
        fixed: list[Any] = []
        for f in filters:
            if isinstance(f, dict) and f.get("operator") in ("ge", "le"):
                f = {**f, "operator": "gte" if f["operator"] == "ge" else "lte"}
            # 区域词展开：大区字面值 -> 省份 IN 列表（与 agent 路径同口径）
            if (
                isinstance(f, dict)
                and f.get("field") == "province"
                and f.get("operator") in ("eq", "in")
            ):
                value = f.get("value")
                values = value if isinstance(value, list) else [value]
                regions = [v for v in values if isinstance(v, str) and v in REGION_PROVINCE_MAPPING]
                if regions:
                    provinces: list[str] = []
                    for region in regions:
                        for p in region_provinces(region):
                            if p not in provinces:
                                provinces.append(p)
                    if provinces:
                        f = {**f, "operator": "in", "value": provinces}
            fixed.append(f)
        d["filters"] = fixed
    return d


def _outside_domain_window(dsl_payload: dict[str, Any]) -> str | None:
    """时间域守卫：查询窗口整体晚于数仓数据域上界时返回窗口描述（否则 None）。

    无数据诚实原则（2026-09 审计修复）：此类窗口是确定性必然空集，执行前
    即拒绝——执行只会得到 0 行，继续因子分解/维度下钻只会产出编造的归因；
    非法窗口草稿不在此拦截，交由网关校验报错喂回自愈。
    """
    from agent.time_utils import time_window_outside_domain
    from compiler.sql_compiler import resolve_time_window
    from semantic.dsl_schema import TimeFilter

    tf_payload = dsl_payload.get("time_filter")
    if not isinstance(tf_payload, dict):
        return None
    try:
        tf = TimeFilter.model_validate(tf_payload)
        if not time_window_outside_domain(tf):
            return None
        start, end = resolve_time_window(tf)
    except Exception:
        return None
    return f"{start:%Y-%m-%d} ~ {end:%Y-%m-%d}"


def _run_query_step(state: AgentState, step: PlanStep) -> tuple[AgentState, ToolRecord]:
    """执行单个 query 步骤：DSL -> 门面 -> ParquetRef。

    口径对齐层（审计修复 R2）：带维度拆分的取数步骤执行前，强制继承
    总览（无维度拆分的步骤）已声明的 WHERE 过滤条件与时间窗口，
    保证明细合计与总览同口径——消除 66.93 万 vs 61.68 万式的数据矛盾。
    """
    from config import settings
    from core.retrieval.tools import execute_dsl_query
    from core.sandbox.api import prepare_workspace

    workspace = settings.WORKSPACE_ROOT / f"{state.session_id}" / state.turn_id
    prepare_workspace(workspace)

    # 总览口径 = 本计划内无维度拆分的 query 步骤 DSL（LLM 计划）；确定性
    # 兜底两期对本身即总览口径。拆分步骤继承之，缺失时按 DSL 契约原样校验。
    overview_variants = [
        _normalize_dsl_draft(s.dsl)
        for s in state.plan_steps
        if s.kind == "query" and s.id != step.id and s.dsl and not s.dsl.get("dimensions")
    ]

    # 步骤 DSL：LLM 给出则规范化后交网关强校验，否则按问题类型走确定性兜底：
    # 诊断类 => 两期分省明细对（总量与归因同源，口径天然一致）；
    # 其余 => 单期总量（标量问题附维度无意义）
    if step.dsl is not None:
        dsl_variants = [_normalize_dsl_draft(step.dsl)]
    else:
        diagnostic = any(
            w in state.user_query
            for w in ("为什么", "下滑", "下降", "下跌", "上涨", "增长", "归因", "原因")
        )
        if diagnostic:
            baseline_dsl, current_dsl = _diagnostic_dsl_pair(state.user_query)
            dsl_variants = [baseline_dsl, current_dsl]
        else:
            dsl_variants = [_scalar_dsl(state.user_query)]

    # 维度拆分步骤 => 口径继承（数据清洗与对齐层，R2）
    has_dimensions = step.dsl is not None and bool(step.dsl.get("dimensions"))
    if has_dimensions and overview_variants:
        aligned_notes: list[str] = []
        scoped: list[dict[str, Any]] = []
        for variant in dsl_variants:
            aligned, notes = _inherit_overview_scope(variant, overview_variants)
            scoped.append(aligned)
            aligned_notes.extend(notes)
        dsl_variants = scoped
        if aligned_notes:
            state.scratchpad.append(
                f"[{step.id}] 口径对齐：{'；'.join(dict.fromkeys(aligned_notes))}"
            )

    notes: list[str] = []
    produced: list[str] = []
    blocked_windows: list[str] = []
    for i, dsl_payload in enumerate(dsl_variants):
        name = f"{step.id}_v{i}" if len(dsl_variants) > 1 else step.id
        events.emit_tool_start("futurebi_dsl_query", step.id, {"dataset": name, "dsl": dsl_payload})
        started = time.perf_counter()
        # 时间域守卫（无数据诚实原则）：必然空集的窗口执行前即拒绝，
        # 严禁拿兜底/域内数据冒充用户指定时段或对空集强行下钻归因
        blocked = _outside_domain_window(dsl_payload)
        if blocked:
            blocked_windows.append(blocked)
            notes.append(f"{name} 被时间域守卫拦截：查询时间范围 {blocked} 超出数仓数据域")
            events.emit_tool_end(
                "futurebi_dsl_query",
                step.id,
                ok=False,
                duration_ms=(time.perf_counter() - started) * 1000,
                output={
                    "dataset": name,
                    "no_data_reason": f"查询时间范围 {blocked} 超出数仓数据域上界",
                },
            )
            continue
        ref = execute_dsl_query(
            dsl_payload,
            principal="admin",  # RLS 主体由服务端身份决定（与 web 链路一致）
            workspace=workspace,
            name=name,
            query=state.user_query,
        )
        state.datasets[name] = ref.model_dump(by_alias=True)
        produced.append(name)
        notes.append(f"{name}: {ref.rows} 行 × {len(ref.columns)} 列")
        # DataQA/Guardrail 审计面（行动项 1/2）：结构化发现随事件下发前端，
        # QA 发现摘要并入 notes（=> record.summary => critic 反思视野）
        audit_payload = ref.audit or {}
        qa_findings = audit_payload.get("qa") or []
        guard_findings = audit_payload.get("guard") or []
        if qa_findings:
            qa_text = "；".join(f"[{f['check']}] {f['message']}" for f in qa_findings)
            notes.append(f"{name} 质检发现：{qa_text}")
        # 审计预览：读取物化 Parquet 前 30 行随 tool_end 下发（数据审计 Tab）。
        # ParquetRef.path 相对 workspace/inputs/（沙箱 read_input 同一约定）
        preview_rows = _preview_rows(str(workspace / "inputs" / ref.path))
        events.emit_tool_end(
            "futurebi_dsl_query",
            step.id,
            ok=True,
            duration_ms=(time.perf_counter() - started) * 1000,
            output={
                "dataset": name,
                "rows": ref.rows,
                "columns": list(ref.columns),
                "preview_rows": preview_rows,
                "guard_findings": guard_findings,
                "qa_findings": qa_findings,
            },
        )

    # 数据集经 state.datasets 传递（ParquetRef 契约），parquet 产物在 synthesize 汇总
    all_blocked = bool(blocked_windows) and not produced
    if all_blocked:
        windows = "、".join(dict.fromkeys(blocked_windows))
        no_data_reason = f"查询时间范围 {windows} 超出数仓数据覆盖范围，该时段无任何数据"
        summary = f"[{step.id}] 取数被时间域守卫拦截：{no_data_reason}"
    else:
        no_data_reason = None
        summary = f"[{step.id}] 取数完成：{'; '.join(notes)}"
    return (
        state.apply(
            step_outputs={**state.step_outputs, step.id: produced},
            no_data_reason=no_data_reason,
        ),
        ToolRecord(
            step_id=step.id,
            tool="execute_dsl_query",
            ok=not all_blocked,
            summary=summary,
        ),
    )


def dsl_query_node(state: AgentState) -> AgentState:
    """取数节点：顺次执行待完成的 query 步骤（需求 §2.A DSLQueryNode）。"""
    updated = state
    for step in [s for s in updated.plan_steps if s.kind == "query" and s.status == "pending"]:
        if updated.no_data_reason:
            # 时间域守卫已拦截：域外时段的窗口重取多少次都是必然空集，
            # 跳过剩余取数步骤（标记 done，无产物），交由 critic 短路诚实报告
            updated.plan_steps = [
                s.model_copy(update={"status": "done"}) if s.id == step.id else s
                for s in updated.plan_steps
            ]
            continue
        try:
            updated, record = _run_query_step(updated, step)
            updated.tool_calls.append(record)
            updated.scratchpad.append(record.summary)
            updated.plan_steps = [
                s.model_copy(update={"status": "done"}) if s.id == step.id else s
                for s in updated.plan_steps
            ]
        except Exception as exc:
            keep = updated.error_context.record(f"{type(exc).__name__}: {exc}")
            logger.exception(
                f"取数步骤执行失败: {step.id}",
                extra={"error": f"{type(exc).__name__}: {exc}"[:500]},
            )
            events.emit_tool_end(
                "futurebi_dsl_query",
                step.id,
                ok=False,
                duration_ms=0.0,
                error=f"{type(exc).__name__}: {exc}",
            )
            updated.tool_calls.append(
                ToolRecord(
                    step_id=step.id,
                    tool="execute_dsl_query",
                    ok=False,
                    error=f"{type(exc).__name__}: {exc}",
                )
            )
            updated.plan_steps = [
                s.model_copy(update={"status": "failed"}) if s.id == step.id else s
                for s in updated.plan_steps
            ]
            if not keep:
                logger.error(
                    f"取数自愈额度耗尽，编排终止: {step.id}",
                    extra={"error": str(exc)[:500]},
                )
                return updated.apply(
                    phase="done",
                    report=f"取数在 {MAX_RETRIES} 次自愈后仍失败，已终止。\n最后一次错误：{exc}",
                )
            logger.warning(
                "取数失败喂回规划节点重写 DSL 自愈",
                extra={"error": f"retries={updated.error_context.retries}, step={step.id}"},
            )
            events.emit_reflection(
                f"取数失败：{exc}", "retry", "错误已喂回规划节点重写 DSL 计划自愈"
            )
            return updated.apply(phase="plan")  # 自愈：回到规划节点重写计划
    return updated.apply(phase="analyze")


# --------------------------------------------------------------------------- #
# 4) CodeExec（沙箱分析）
# --------------------------------------------------------------------------- #
def _resolve_step_inputs(state: AgentState, step: PlanStep) -> list[str]:
    """解析 analyze 步骤的输入数据集名（按依赖步骤归属，禁用首尾取数）。

    规则（确定性、可解释）：
    - 有 depends_on：按声明顺序取各依赖步骤本轮产出的数据集名
      （``state.step_outputs``，重规划时按同 id 覆盖 => 只含本轮产物）；
    - 无 depends_on：回落到"本计划全部 query 步骤的产出"（顺序稳定）；
    - 两者皆空：回落到 datasets 的插入顺序（兜底不空转）。

    回归锚点：此前按 ``list(state.datasets)[0]`` / ``[-1]`` 取两期输入，
    重规划后 datasets 累积上轮键，首尾会指向上轮遗留数据集（如第一轮
    分省明细 + 第二轮第二周），产出"下滑 57.9%"式的错配结论。
    """
    names: list[str] = []
    for dep in step.depends_on:
        for name in state.step_outputs.get(dep, []):
            if name in state.datasets and name not in names:
                names.append(name)
    if not names:
        for query_step in state.plan_steps:
            if query_step.kind != "query" or query_step.id == step.id:
                continue
            for name in state.step_outputs.get(query_step.id, []):
                if name in state.datasets and name not in names:
                    names.append(name)
    if not names:
        names = list(state.datasets.keys())
    return names


def _analysis_template(state: AgentState, step: PlanStep) -> str:
    """analyze 步骤的代码来源优先级：LLM Coder 产码（已静态校验）> 确定性技能模板。

    确定性模板按步骤目标分流（先因子后维度，审计修复 M2）：
    - 目标含"因子/分解" => ``_factor_template``：乘法因子分解（量跌还是价跌）；
    - 其余（维度下钻/归因定位）=> ``_drilldown_template``：多维度信息增益下钻，
      择优渲染主因维度图并给出"入选维度"的解释话术；
    - 无可用输入（如纯标量问题被规划成 analyze）=> ``_scalar_template``。

    模板读取本步骤依赖的取数产物（两期明细），产物结构随数据列自动适配，
    综合节点直接消费 summary（table/findings/extra）。
    """
    if step.code:
        return step.code
    inputs = _resolve_step_inputs(state, step)
    if not inputs:
        return _SCALAR_ANALYSIS_TEMPLATE
    # 空输入纵深防护：依赖数据集全部 0 行 => 输出"无匹配数据"summary，
    # 严禁在空 DataFrame 上产出假方向/假归因（诚实兜底，配合 critic 短路）
    if all(int((state.datasets.get(name) or {}).get("rows", 0) or 0) == 0 for name in inputs):
        return _EMPTY_DATA_ANALYSIS_TEMPLATE
    if "因子" in step.goal or "分解" in step.goal:
        return _factor_template(inputs)
    return _drilldown_template(inputs)


_SCALAR_ANALYSIS_TEMPLATE = """
save_summary(
    title="数据概览",
    metrics={},
    table={"columns": [], "rows": []},
    findings=["本轮无维度明细可取，仅回到取数结果作答。"],
    extra={},
)
"""

# 空输入纵深防护（2026-09 审计修复）：依赖数据集全部 0 行时严禁跑分解/
# 下钻模板——空 DataFrame 会产出"GMV 从 0.00 变至 0.00"式废话与全 0
# 归因矩阵，被包装成"分析结论"即数据造假。
_EMPTY_DATA_ANALYSIS_TEMPLATE = """
save_summary(
    title="无匹配数据",
    metrics={},
    table={"columns": [], "rows": []},
    findings=[
        "输入数据集为空（0 行）：所选时间窗口/过滤条件下没有数据，"
        "无法开展因子分解或维度归因。"
    ],
    extra={},
)
"""


def _factor_template(inputs: list[str]) -> str:
    """乘法因子分解模板：GMV = 买家数 × 人均订单数 × 客单价（对数链式归因）。

    反思器会检查"订单量/客单价/买家数等驱动因素"是否被归因；取数阶段已把
    orders/buyers 与 GMV 同源落库，此处直接做乘法分解，避免"有数据未分析"
    触发无谓重规划（重规划只会取回同一份数据）。
    """
    baseline_name = inputs[0] if inputs else ""
    current_name = inputs[1] if len(inputs) > 1 else (inputs[0] if inputs else "")
    return f"""
import pandas as pd
import math

baseline_df = read_input("{baseline_name}")
current_df = read_input("{current_name}")

b_total = float(baseline_df["gmv"].sum()) if "gmv" in baseline_df.columns else 0.0
c_total = float(current_df["gmv"].sum()) if "gmv" in current_df.columns else 0.0
delta = c_total - b_total
direction = "下滑" if delta < 0 else ("增长" if delta > 0 else "持平")
change = delta / b_total if b_total else 0.0
findings = [
    f"GMV 从 {{b_total:.2f}} 变至 {{c_total:.2f}}，{{direction}} {{abs(change):.1%}}"
]
metrics = {{"baseline": b_total, "current": c_total, "delta": delta}}
table = {{"columns": ["factor", "baseline", "current", "change_rate", "share"], "rows": []}}

if all(col in baseline_df.columns and col in current_df.columns for col in ("orders", "buyers")):

    def _driver_factors(frame):
        # 一期明细聚合为乘法因子组（买家数 × 人均订单数 × 客单价 = GMV）
        gmv_sum = float(frame["gmv"].sum())
        orders_sum = float(frame["orders"].sum())
        buyers_sum = float(frame["buyers"].sum())
        return {{
            "买家数": buyers_sum,
            "人均订单数": (orders_sum / buyers_sum) if buyers_sum else 0.0,
            "客单价": (gmv_sum / orders_sum) if orders_sum else 0.0,
        }}

    base_factors = _driver_factors(baseline_df)
    curr_factors = _driver_factors(current_df)
    if all(v > 0 for v in base_factors.values()) and all(v > 0 for v in curr_factors.values()):
        log_deltas = {{
            k: math.log(curr_factors[k]) - math.log(base_factors[k]) for k in base_factors
        }}
        abs_log = sum(abs(v) for v in log_deltas.values())
        for k in base_factors:
            table["rows"].append(
                {{
                    "factor": k,
                    "baseline": round(base_factors[k], 4),
                    "current": round(curr_factors[k], 4),
                    "change_rate": round(curr_factors[k] / base_factors[k] - 1.0, 4),
                    "share": round(log_deltas[k] / abs_log, 4) if abs_log else 0.0,
                }}
            )
        table["rows"].sort(key=lambda r: abs(r["share"]), reverse=True)
        top_factor = table["rows"][0]
        findings.append(
            f"驱动因子分解（GMV = 买家数 × 人均订单数 × 客单价）：主要因子 "
            f"[{{top_factor['factor']}}] 变化 {{top_factor['change_rate']:+.1%}}，"
            f"贡献了 {{abs(top_factor['share']):.0%}} 的总偏差"
        )
        for r in table["rows"][1:]:
            findings.append(
                f"因子 [{{r['factor']}}] 变化 {{r['change_rate']:+.1%}}，贡献 {{abs(r['share']):.0%}}"
            )
    else:
        findings.append("因子存在零值，乘法对数分解不适用，仅呈现加法归因")
else:
    findings.append("本轮数据未覆盖订单量/买家数，驱动因子分解无法开展")

save_summary(
    title="驱动因子分解",
    metrics=metrics,
    table=table,
    findings=findings,
    extra={{}},
)
"""


def _drilldown_template(inputs: list[str]) -> str:
    """多维度信息增益下钻模板（审计修复 M2：不再默认分省）。

    - 候选维度 = 两期明细里实际存在的字符串维度列（province/category/brand…），
      由每维度的偏差集中度计算信息增益并降序排序；
    - 入选维度 = 信息增益最高者（显式维度已由取数层收窄，模板不再需要裁决）；
    - findings 首条给出"入选依据"话术（扫描了几个候选维度、谁胜出、依据数值），
      杜绝"没问却下钻"的突兀感；
    - 主归因矩阵与 ECharts 只渲染入选维度（分省不再是默认主图）；
    - 数值自洽：加法贡献 Δ 之和 == 总量差，share 按 |Δ| 归一。
    """
    baseline_name = inputs[0] if inputs else ""
    current_name = inputs[1] if len(inputs) > 1 else (inputs[0] if inputs else "")
    labels_literal = json.dumps(_DIMENSION_LABELS, ensure_ascii=False)
    return f"""
import pandas as pd
import json
import math

baseline_df = read_input("{baseline_name}")
current_df = read_input("{current_name}")

# 维度字段 -> 中文标签（叙述与图表标题人读化；矩阵列头保留字段名供数据审计）
_DIM_LABELS = json.loads({labels_literal!r})


def _dim_label(field):
    return _DIM_LABELS.get(field, field)


# —— 总量对比（两期明细同口径，sum 即总量）——
b_total = float(baseline_df["gmv"].sum()) if "gmv" in baseline_df.columns else 0.0
c_total = float(current_df["gmv"].sum()) if "gmv" in current_df.columns else 0.0
delta = c_total - b_total
direction = "下滑" if delta < 0 else ("增长" if delta > 0 else "持平")
change = delta / b_total if b_total else 0.0
findings = [
    f"GMV 从 {{b_total:.2f}} 变至 {{c_total:.2f}}，{{direction}} {{abs(change):.1%}}"
]
metrics = {{"baseline": b_total, "current": c_total, "delta": delta}}
key_span = abs(delta) if abs(delta) > 1e-12 else 1.0

# —— 候选维度自动发现（两期共有、非指标列的字符串维度）——
# 仅按 dtype 名称识别（兼容 pandas 各版本：object / string / str / category），
# 不用 pd.api.types 探测——沙箱 AST 守卫对深层属性访问更严格。
_METRIC_COLS = {{"gmv", "orders", "buyers"}}


def _is_dimension_col(series):
    dtype_name = str(series.dtype).lower()
    return any(tok in dtype_name for tok in ("object", "string", "str", "category"))


_candidates = [
    c
    for c in baseline_df.columns
    if c in current_df.columns and c not in _METRIC_COLS and _is_dimension_col(baseline_df[c])
]
ranked = []
if _candidates and "gmv" in baseline_df.columns and "gmv" in current_df.columns:
    for col in _candidates:
        b_by = baseline_df.groupby(col)["gmv"].sum()
        c_by = current_df.groupby(col)["gmv"].sum()
        b_al, c_al = b_by.align(c_by, fill_value=0.0)
        deltas = c_al - b_al
        # 偏差绝对值分布（占该维度总偏差的比例）：越集中 => 熵越小 => 增益越大。
        # 均匀熵基准取该维度取值数（信息增益 = log(n) - H），跨维度可比。
        abs_deltas = {{str(p): abs(float(deltas[p])) for p in deltas.index}}
        abs_sum = sum(abs_deltas.values())
        n_vals = max(len(abs_deltas), 1)
        uniform = math.log(n_vals) if n_vals > 1 else 0.0
        norm = [v / abs_sum for v in abs_deltas.values() if v > 0] if abs_sum else []
        entropy = -sum(p * math.log(p) for p in norm if p > 0)
        gain = uniform - entropy
        top_key = max(abs_deltas, key=abs_deltas.get) if abs_deltas else ""
        ranked.append(
            {{
                "dimension": str(col),
                "gain": round(gain, 4),
                "concentration": round(max(norm) if norm else 0.0, 4),
                "top_value": top_key,
                "share": round(abs_deltas.get(top_key, 0.0) / abs_sum, 4) if abs_sum else 0.0,
                "rows": [
                    {{
                        "value": str(p),
                        "baseline": round(float(b_al[p]), 2),
                        "current": round(float(c_al[p]), 2),
                        "delta": round(float(deltas[p]), 2),
                        "share": round(abs(float(deltas[p])) / key_span, 4),
                    }}
                    for p in deltas.index
                ],
            }}
        )
    ranked.sort(key=lambda r: r["gain"], reverse=True)

primary = ranked[0] if ranked else None
if primary:
    findings.insert(
        0,
        f"经 {{len(_candidates)}} 个候选维度信息增益扫描，维度 [{{_dim_label(primary['dimension'])}}] "
        f"的偏差最集中（信息增益 {{primary['gain']:.4f}}、集中度 {{primary['concentration']:.0%}}），"
        f"其取值 [{{primary['top_value']}}] 占该维度偏差 {{primary['share']:.0%}}，"
        f"优先下钻该维度定位主因",
    )

# —— 主归因矩阵（只渲染入选维度）+ 次要维度汇总 ——
if primary:
    rows = sorted(primary["rows"], key=lambda r: abs(r["delta"]), reverse=True)
    table = {{
        "columns": [primary["dimension"], "baseline", "current", "delta", "share"],
        "rows": [[r["value"], r["baseline"], r["current"], r["delta"], r["share"]] for r in rows[:20]],
    }}
    _label = _dim_label(primary["dimension"])
    top = rows[0]
    findings.append(
        f"主要矛盾（{{_label}}）[{{top['value']}}]：{{top['baseline']:.2f}} -> "
        f"{{top['current']:.2f}}（Δ{{top['delta']:+.2f}}），贡献了 {{abs(top['share']):.0%}} 的总偏差"
    )
    for r in rows[1:3]:
        findings.append(
            f"次要贡献（{{_label}}）[{{r['value']}}] 贡献 {{abs(r['share']):.0%}}"
            f"（Δ{{r['delta']:+.2f}}）"
        )
    if len(ranked) > 1:
        others = "、".join(
            f"{{_dim_label(r['dimension'])}}(增益 {{r['gain']:.3f}})" for r in ranked[1:]
        )
        findings.append(f"其余候选维度信息增益较低，未入选主因定位：{{others}}")
else:
    table = {{"columns": [], "rows": []}}
    findings.append("两期数据缺少可用维度列，无法做维度归因定位")

# —— 维度信息增益全景（全部候选，供反思/综合判定归因是否充分）——
gain_table = {{
    "columns": ["dimension", "gain", "concentration", "top_value", "share"],
    "rows": [
        [r["dimension"], r["gain"], r["concentration"], r["top_value"], r["share"]] for r in ranked
    ],
}}

save_summary(
    title="维度信息增益归因",
    metrics=metrics,
    table=table,
    findings=findings,
    extra={{"gain_table": gain_table, "primary_dimension": primary["dimension"] if primary else ""}},
)
if primary:
    _rows = table["rows"]
    save_echarts_spec({{
        "title": {{"text": f"{{_dim_label(primary['dimension'])}} 两期 GMV 对比（信息增益下钻）"}},
        "tooltip": {{}},
        "legend": {{"data": ["基线期", "当前期"]}},
        "xAxis": {{"type": "category", "data": [r[0] for r in _rows]}},
        "yAxis": {{"type": "value"}},
        "series": [
            {{"name": "基线期", "type": "bar", "data": [r[1] for r in _rows]}},
            {{"name": "当前期", "type": "bar", "data": [r[2] for r in _rows]}},
        ],
    }})
"""


def _has_same_echarts(artifacts: list[Artifact], spec: dict[str, Any]) -> bool:
    """判断已登记产物中是否已存在同规格 ECharts（审计修复 T14 图表去重）。

    判定键：序列类型 + x 轴类目 + 系列名/数据——同型同值的图表只保留一份，
    不同标题/步骤名但内容同质的重复发射也一并拦截。
    """
    import json

    def _chart_key(s: dict[str, Any]) -> str:
        try:
            series = s.get("series")
            if isinstance(series, list):
                series_key = json.dumps(
                    [
                        {
                            "type": (item or {}).get("type"),
                            "name": (item or {}).get("name"),
                            "data": (item or {}).get("data"),
                        }
                        for item in series
                        if isinstance(item, dict)
                    ],
                    ensure_ascii=False,
                    sort_keys=True,
                )
            else:
                series_key = json.dumps(series, ensure_ascii=False, sort_keys=True)
            return json.dumps(
                {
                    "series": series_key,
                    "x": (
                        (s.get("xAxis") or {}).get("data")
                        if isinstance(s.get("xAxis"), dict)
                        else s.get("xAxis")
                    ),
                    "title": (
                        (s.get("title") or {}).get("text")
                        if isinstance(s.get("title"), dict)
                        else s.get("title")
                    ),
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        except Exception:  # 序列化失败视为不同图表，宁可重复不误杀
            return json.dumps(s, ensure_ascii=False, sort_keys=True, default=str)

    key = _chart_key(spec)
    for a in artifacts:
        if a.kind == "echarts" and _chart_key(a.payload) == key:
            return True
    return False


def code_exec_node(state: AgentState) -> AgentState:
    """沙箱分析节点：执行 analyze 步骤（需求 §2.A CodeExecutionNode）。"""
    from config import settings

    if state.no_data_reason:
        # 时间域守卫已拦截取数：无数据可分析，严禁在空数据上跑分解/下钻
        # 产出编造结论，直接转入反思节点（=> 诚实说明无数据）
        return state

    updated = state
    for step in [s for s in updated.plan_steps if s.kind == "analyze" and s.status == "pending"]:
        code = _analysis_template(updated, step)
        workspace = settings.WORKSPACE_ROOT / updated.session_id / updated.turn_id
        events.emit_tool_start("python_sandbox", step.id, {"code": code})
        result = run_code(code, workspace, name=f"analysis_{step.id}")
        updated.tool_calls.append(
            ToolRecord(
                step_id=step.id,
                tool="run_code",
                ok=result.ok,
                summary=(
                    json.dumps(result.summary, ensure_ascii=False)[:800] if result.summary else ""
                ),
                duration_ms=result.duration_ms,
                error=result.error,
            )
        )
        events.emit_tool_end(
            "python_sandbox",
            step.id,
            ok=result.ok,
            duration_ms=result.duration_ms,
            summary=json.dumps(result.summary, ensure_ascii=False)[:400] if result.summary else "",
            error=result.error,
        )
        if result.ok and result.summary:
            updated.artifacts.append(
                Artifact(kind="summary", name=step.id, payload={"summary": result.summary})
            )
            events.emit_event(
                events.EVENT_ARTIFACT_EMIT,
                {
                    "artifact": {
                        "type": "table",
                        "title": (result.summary or {}).get("title", step.id),
                        "content": result.summary,
                    }
                },
            )
            # 图表产物去重（审计修复 T14）：同 payload 的 ECharts 规格严禁重复
            # 登记与发射——反思重规划多轮执行同模板分析时曾产出 4 份同质图表
            if result.echarts_spec and not _has_same_echarts(
                updated.artifacts, result.echarts_spec
            ):
                updated.artifacts.append(
                    Artifact(kind="echarts", name=f"{step.id}_chart", payload=result.echarts_spec)
                )
                events.emit_event(
                    events.EVENT_ARTIFACT_EMIT,
                    {
                        "artifact": {
                            "type": "echarts",
                            "title": f"{step.id}_chart",
                            "content": result.echarts_spec,
                        }
                    },
                )
            updated.plan_steps = [
                s.model_copy(update={"status": "done"}) if s.id == step.id else s
                for s in updated.plan_steps
            ]
            updated.scratchpad.append(
                f"[{step.id}] 沙箱分析完成：{result.summary.get('title', '')}"
            )
        else:
            keep = updated.error_context.record(result.error or "沙箱执行失败")
            logger.warning(
                f"沙箱分析执行失败: {step.id}",
                extra={"error": (result.error or "")[:500]},
            )
            updated.plan_steps = [
                s.model_copy(update={"status": "failed"}) if s.id == step.id else s
                for s in updated.plan_steps
            ]
            if not keep:
                logger.error(
                    f"沙箱自愈额度耗尽，转 Critic 如实放弃: {step.id}",
                    extra={"error": (result.error or "")[:500]},
                )
                return updated.apply(phase="critique")  # 让 Critic 决定如实放弃
            events.emit_reflection(
                f"沙箱执行失败：{result.error}", "retry", "回到规划节点修正分析代码"
            )
            return updated.apply(phase="plan")
    return updated.apply(phase="critique")


# --------------------------------------------------------------------------- #
# 5) Critic
# --------------------------------------------------------------------------- #
# 反思缺口判定的"业务词 -> 可用域字段"映射：反思器提到这些概念时，只有
# 语义目录确实覆盖（或本轮已取到）才算可执行的缺口。未列出的业务概念
# （流量/活动/投放/库存/物流等）数仓未采集，一律不可作为重规划理由。
_REFLECTOR_CONCEPT_FIELDS: dict[str, tuple[str, ...]] = {
    "订单量": ("order_id",),
    "订单": ("order_id", "order_amount"),
    "客单价": ("order_amount", "order_id"),
    "买家": ("user_id",),
    "用户": ("user_id", "register_time", "gender"),
    "地区": ("province",),
    "省份": ("province",),
    "城市": ("province",),
    "品类": ("category",),
    "商品": ("product_id", "product_name", "category", "brand", "unit_price"),
    "品牌": ("brand",),
    "店铺": ("shop_id", "shop_name"),
    "退款": ("refund_amount", "refund_status", "refund_time"),
    "折扣": ("discount_amount",),
    "支付": ("pay_status",),
    "性别": ("gender",),
    "时间": ("order_time",),
}

# 概念在本轮产物中的等价列名（确定性兜底模板的别名口径）：产物列名是
# 聚合别名（orders/buyers）而非语义字段名（order_id/user_id），缺了这层
# 映射会把"已取到的订单量与买家数"误判成可重规划的数据缺口。
_REFLECTOR_CONCEPT_ALIASES: dict[str, tuple[str, ...]] = {
    "订单量": ("orders",),
    "订单": ("orders",),
    "客单价": ("gmv", "orders"),  # 客单价 = GMV / 订单量，两者齐备即可推导
    "买家": ("buyers",),
    "用户": ("buyers",),
    "地区": ("province",),
    "省份": ("province",),
    "城市": ("province",),
    "支付": ("pay_status",),
}

# 数仓未覆盖的业务概念（出现在反思理由中即为不可执行缺口，禁止据此重规划）
_REFLECTOR_OUT_OF_SCOPE_TERMS = (
    "流量",
    "曝光",
    "点击",
    "访客",
    "uv",
    "pv",
    "活动",
    "促销",
    "投放",
    "广告",
    "预算",
    "库存",
    "物流",
    "履约",
    "竞品",
    "市场",
    "舆情",
    "客服",
    "异常单",
    "转化率",
    "留存",
    "复购",
)


def _reflector_available_scope() -> str:
    """反思提示词的"数仓可用字段清单"小节（判定边界的客观依据）。"""
    from config import settings
    from semantic.catalog import COLUMNS

    fields = "、".join(sorted(COLUMNS))
    return (
        "# 数仓可用字段清单（判定边界的唯一依据）\n"
        f"{fields}\n"
        "清单之外的业务维度/指标（流量、曝光、活动、投放、库存、物流、竞品等）"
        "数仓未采集，重规划也取不到，严禁作为 insufficient 的理由。\n"
        "# 数仓数据时间域\n"
        f"数据覆盖范围截至 {settings.DATA_DOMAIN_END.isoformat()}（数据基准日期）；"
        "查询时段整体晚于该日期时数仓没有任何数据，属不可执行缺口——"
        "严禁作为 insufficient 的理由要求重规划，更严禁对空数据推测结论。"
    )


def _concept_covered(concept: str, produced_fields: set[str]) -> bool:
    """该业务概念所需字段是否已在本轮产物中（语义字段名或聚合别名任一命中）。"""
    required = _REFLECTOR_CONCEPT_FIELDS[concept]
    aliases = _REFLECTOR_CONCEPT_ALIASES.get(concept, ())
    return all(f in produced_fields for f in required) or (
        bool(aliases) and all(a in produced_fields for a in aliases)
    )


def _gap_is_actionable(text: str, produced_fields: set[str]) -> bool:
    """单条反思理由是否构成"可执行的缺口"（用清单内字段可补齐）。

    - 提到数仓未采集的业务概念（流量/活动/投放/库存等）=> 不可执行，
      重规划也取不回该数据；
    - 提到可用域内的业务概念且对应字段本轮未取到 => 可执行（值得重规划）；
    - 未提及任何可用域概念 => 属分析深度/叙述诉求，不可执行。
    """
    lowered = text.lower()
    if any(term in lowered for term in _REFLECTOR_OUT_OF_SCOPE_TERMS):
        return False
    for concept in _REFLECTOR_CONCEPT_FIELDS:
        if (concept in text or concept.lower() in lowered) and not _concept_covered(
            concept, produced_fields
        ):
            return True
    return False


def _insufficient_is_actionable(verdict: dict[str, Any], state: AgentState) -> bool:
    """反思判定"不充分"是否可执行（确定性护栏，防重规划空转）。

    校验规则（任一不满足 => 判定不可执行，直接综合）：
    1. 至少存在一条可执行缺口（``_gap_is_actionable``：理由提到的可用域
       字段本轮确实未取到，且不含数仓未采集的业务概念）；
    2. 本轮产物指纹较上次重规划时有进展——指纹不变说明重规划没带来任何
       新数据/新分析（如兜底计划每轮产出同一份结果），继续重规划纯属空转。

    返回 True 表示允许重规划；False 表示应按充分处理转入综合。

    回归锚点：此前反思以"缺订单量/客单价/流量/活动/异常单归因"判不充分，
    而其中流量/活动/异常单数仓从未采集、订单量与客单价又已随取数落库，
    重规划只能取回同一份数据 => 空转至额度耗尽降级。
    """
    reasons = [str(x) for x in (verdict.get("reasons") or [])]
    missing = [str(x) for x in (verdict.get("missing") or [])]
    candidates = [t for t in (reasons + missing) if t.strip()]
    if not candidates:
        return False  # 无理由的 insufficient 不可执行（防 LLM 空判）
    fingerprint = _artifact_fingerprint(state)
    if fingerprint and fingerprint == state.last_replan_fingerprint:
        return False  # 重规划无进展：理由再多也是空转
    produced_fields: set[str] = set()
    for ref in state.datasets.values():
        produced_fields.update(str(c) for c in (ref.get("columns") or []))
    return any(_gap_is_actionable(text, produced_fields) for text in candidates)


def _artifact_fingerprint(state: AgentState) -> str:
    """本轮产物进展指纹：数据集（名/行列数）+ summary 内容 + 图表数量。"""
    import hashlib

    parts: list[str] = []
    for name, ref in sorted(state.datasets.items()):
        parts.append(f"{name}:{ref.get('rows')}x{len(ref.get('columns') or [])}")
    for artifact in state.artifacts:
        if artifact.kind == "summary":
            parts.append(
                json.dumps(artifact.payload.get("summary", {}), ensure_ascii=False, sort_keys=True)
            )
        else:
            parts.append(f"{artifact.kind}:{artifact.name}")
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()[:16]


def critic_node(state: AgentState) -> AgentState:
    """反思节点：完整性 / 正确性 / 一致性三检（需求 §2.A Critic/ReflectionNode）。

    确定性检查为主（LLM Reflector 增强可选）：
    - 无任何成功产物且重试未超限 => 重规划；
    - 诊断类问题但缺少 summary 产物 => 重规划；
    - 检查通过 => synthesize；重试耗尽 => 如实报告失败。
    """
    has_summary = any(a.kind == "summary" for a in state.artifacts)
    has_data = bool(state.datasets)
    diagnostic = any(
        w in state.user_query for w in ("为什么", "下滑", "下降", "上涨", "增长", "归因", "原因")
    )
    exhausted = state.error_context.retries >= MAX_RETRIES

    # 无数据诚实短路（2026-09 审计修复，先于一切重规划判定）：
    # 时间域守卫拦截 / 全部数据集为空（0 行）时，重规划取不回数据域之外的
    # 数据，严禁空转自愈，直接转入综合节点如实说明无数据。
    if state.no_data_reason:
        events.emit_reflection(
            f"时间域守卫拦截：{state.no_data_reason}",
            "proceed",
            "转入综合节点如实说明无数据",
        )
        return state.apply(phase="synthesize")
    if has_data and all(int(ref.get("rows", 0) or 0) == 0 for ref in state.datasets.values()):
        events.emit_reflection(
            "全部数据集均为空（0 行）：所选时间窗口/过滤条件下无匹配数据",
            "proceed",
            "转入综合节点如实说明无匹配数据",
        )
        return state.apply(phase="synthesize")

    if not has_data:
        if exhausted:
            logger.error("未获得任何数据集且自愈额度耗尽，转入综合节点如实报告失败")
            events.emit_reflection(
                "未获得任何数据集且重试额度耗尽", "proceed", "转入综合节点如实报告失败"
            )
            return state.apply(phase="synthesize")
        logger.warning("未获得任何数据集，触发重规划自愈")
        events.emit_reflection("未获得任何数据集", "replan", "取数失败，回到规划节点重写计划")
        return state.apply(phase="plan")
    if diagnostic and not has_summary and not exhausted:
        logger.warning("诊断类问题缺少归因 summary 产物，触发重规划补齐分析")
        events.emit_reflection(
            "诊断类问题缺少归因 summary 产物", "replan", "补齐沙箱归因分析后再综合"
        )
        return state.apply(phase="plan")
    # LLM Reflector 增强（可选；失败不影响确定性判定）
    llm = _resolve_llm()
    if llm is not None and has_summary:
        trace_digest = "\n".join(r.summary or r.error or "" for r in state.tool_calls[-6:])
        if state.error_context.errors:
            # 反思层必须看见失败历史（行动项 3：与 Planner 同一断裂点修复）
            trace_digest += "\n自愈错误记录：" + "；".join(state.error_context.errors[-3:])
        verdict = _llm_json(
            llm,
            REFLECTOR_SYSTEM,
            f"用户问题：{state.user_query}\n"
            f"{_reflector_available_scope()}\n"
            f"执行轨迹：\n{trace_digest}",
        )
        if verdict and verdict.get("verdict") == "insufficient":
            reasons = verdict.get("reasons")
            # 确定性护栏（回归锚点）：反思不得把"数仓未覆盖的维度"当数据缺口。
            # 此前反思以"缺订单量/流量/活动归因"判不充分，而可用域内数据与
            # 分析其实已齐备，重规划只能取回同一份数据 => 空转至额度耗尽降级。
            if not _insufficient_is_actionable(verdict, state):
                logger.info(
                    "LLM 反思判定不充分但缺口不在可用域内或计划无进展，按充分处理",
                    extra={"error": str(reasons)[:500]},
                )
                state.scratchpad.append(f"[critic-guard] 反思缺口不可执行，直接综合：{reasons}")
                events.emit_reflection(
                    str(reasons),
                    "proceed",
                    "反思缺口不在可用数据域内（重规划取不到），直接转入综合报告",
                )
                return state.apply(phase="synthesize")
            # LLM 反思重规划与工具自愈共用重试预算（防"不耗额度的无限重规划"，
            # 只能靠迭代护栏兜底而空烧 LLM 调用）
            keep = state.error_context.record(f"LLM 反思判定产物不充分: {reasons}")
            state.scratchpad.append(f"[critic-llm] {reasons}")
            logger.warning(
                "LLM 反思判定产物不充分，触发重规划",
                extra={"error": str(reasons)[:500]},
            )
            events.emit_reflection(str(reasons), "replan", "LLM 反思判定产物不充分，触发重规划")
            if not keep:
                logger.error("重规划额度耗尽，转入综合节点如实报告")
                events.emit_reflection("重规划额度耗尽", "proceed", "转入综合节点如实报告")
                return state.apply(phase="synthesize")
            # 记录本次重规划时的产物指纹：下轮反思若判定不充分但指纹未变，
            # 说明重规划没带来任何新数据/新分析，直接按充分处理（防空转）
            return state.apply(phase="plan", last_replan_fingerprint=_artifact_fingerprint(state))
    events.emit_reflection("完整性/正确性/一致性三检通过", "proceed", "转入综合报告")
    return state.apply(phase="synthesize")


# --------------------------------------------------------------------------- #
# 6) Synthesize
# --------------------------------------------------------------------------- #
def _fmt_wan(value: Any) -> str:
    """数值 -> 人读万元金额（审计修复 R1：综合报告禁数字节面值直出）。"""
    try:
        return f"{float(value) / 10000:.2f} 万元"
    except (TypeError, ValueError):
        return str(value)


def _fmt_pct(value: Any) -> str:
    """小数占比 -> 人读百分比（保留一位小数）。"""
    try:
        return f"{float(value):.1%}"
    except (TypeError, ValueError):
        return str(value)


def _dataset_analyst_markdown(
    name: str, ref: dict[str, Any], workspace: Any
) -> tuple[str, dict | None]:
    """把一个已物化数据集渲染为分析师口径的报告片段 + 前端 table 载荷。

    - 单行单列（标量聚合，如 count_distinct）=> 直接给答案行；
    - 多行 => 附前 5 行 Markdown 预览表；读取失败降级为行列摘要。
    （字段名在表格列头保留——数据审计必需；叙述文本不罗列内部字段名。）
    """
    cols = [str(c) for c in ref.get("columns", [])]
    total = ref.get("rows", 0)
    lines = [
        f"### 查询结果：{name}",
        f"- 共 {total} 条记录（{len(cols)} 个分析视角）",
    ]
    preview = _preview_rows(str(workspace / "inputs" / ref.get("path", "")), limit=30)
    if total == 1 and len(cols) == 1 and preview:
        lines.append(f"- **查询答案：{_fmt_wan(preview[0][0])}**")
    artifact: dict[str, Any] | None = None
    if preview:
        head = preview[:5]
        lines.append("")
        lines.append("| " + " | ".join(cols) + " |")
        lines.append("|" + "|".join([" --- "] * len(cols)) + "|")
        for row in head:
            lines.append("| " + " | ".join(str(v) for v in row) + " |")
        if total > 5:
            lines.append(f"（预览前 5 行，共 {total} 行）")
        artifact = {
            "type": "table",
            "title": f"查询结果 · {name}",
            "columns": cols,
            "rows": preview,
            "totalRows": total,
        }
    return "\n".join(lines), artifact


def _summary_analyst_markdown(summary: dict[str, Any]) -> list[str]:
    """沙箱 summary -> 分析师叙述（数值万元/百分比化，禁 raw dict 直出）。

    table 行按首列是因子名（factor）还是维度取值自动分流渲染：
    - factor 形态 => 驱动因子小节（值为比值/人数，不做万元换算）；
    - 其余 => 维度归因矩阵小节（baseline/current/delta 按万元）。
    extra.gain_table（维度信息增益全景）另起小节渲染。
    """
    lines: list[str] = []
    title = summary.get("title", "")
    findings = summary.get("findings", [])
    metrics = summary.get("metrics", {})
    lines.append(f"### {title}" if title else "### 归因结论")
    for f in findings:
        lines.append(f"- {f}")
    if metrics:
        rendered = "；".join(
            f"{k} = {_metric_human(v, k)}" if isinstance(v, (int, float)) else f"{k} = {v}"
            for k, v in metrics.items()
        )
        lines.append(f"- 关键指标：{rendered}")
    table = summary.get("table") or {}
    columns = [str(c) for c in (table.get("columns") or [])]
    if columns and columns[0] == "factor":
        lines.append("")
        lines.append("**驱动因子分解（GMV = 买家数 × 人均订单数 × 客单价）**")
        lines.extend(_render_table(table, limit=10, currency=False))
    else:
        lines.extend(_render_table(table, limit=10))
    gain_table = (summary.get("extra") or {}).get("gain_table") or {}
    if gain_table.get("rows"):
        lines.append("")
        lines.append("**维度信息增益全景（候选维度扫描结果）**")
        lines.extend(_render_table(gain_table, limit=10, currency=False))
    return lines


# 非金额指标键名（订单量/人数等计数类）——严禁按万元换算
_COUNT_METRIC_KEYS = ("orders", "buyers", "count", "users", "quantity", "qty")


def _metric_human(value: Any, key: str = "") -> str:
    """指标值人读化：计数类指标保留原值（可带小数），金额类转万元。"""
    if any(token in key.lower() for token in _COUNT_METRIC_KEYS):
        try:
            return f"{float(value):.2f}".rstrip("0").rstrip(".")
        except (TypeError, ValueError):
            return str(value)
    return _fmt_wan(value)


def _render_table(table: dict[str, Any], *, limit: int = 10, currency: bool = True) -> list[str]:
    """把 summary table 渲染为 Markdown 表格（兼容 list 行与 dict 行两种形态）。

    - list 行：按 columns 顺序逐列渲染（分省归因矩阵）；
    - dict 行：按 columns 取键渲染（驱动因子分解）；
    - ``currency=True``（金额矩阵）：baseline/current/delta/value/gmv 按万元；
      ``currency=False``（因子矩阵，值为人数/比值）：数值原样渲染，
      否则"买家数 11 人"会被误写成"0.00 万元"；
    - share/change_rate 一律百分比，其余（factor/province 等）原样。
    """
    rows = table.get("rows") or []
    cols = [str(c) for c in (table.get("columns") or [])]
    if not rows or not cols:
        return []
    lines = ["", "| " + " | ".join(cols) + " |", "|" + "|".join([" --- "] * len(cols)) + "|"]
    for row in rows[:limit]:
        if isinstance(row, dict):
            values = [row.get(c) for c in cols]
        else:
            values = list(row)
        cells: list[str] = []
        for c, v in zip(cols, values, strict=False):  # 行长不齐时容忍截断
            if c in ("share", "change_rate"):
                cells.append(_fmt_pct(v))
            elif currency and c in ("baseline", "current", "delta", "value", "gmv"):
                cells.append(_fmt_wan(v))
            else:
                cells.append(str(v))
        lines.append("| " + " | ".join(cells) + " |")
    if len(rows) > limit:
        lines.append(f"（仅列示前 {limit} 行，共 {len(rows)} 行）")
    return lines


def _synthesize_with_llm(state: AgentState, material: str) -> str | None:
    """LLM 商业分析师综合（审计修复 R1）；失败返回 None 走确定性兜底。"""
    llm = _resolve_llm()
    if llm is None:
        return None
    user_prompt = (
        f"# 用户问题\n{state.user_query}\n\n# 上游分析材料（唯一数据事实来源）\n{material}\n\n"
        "# 现在，按四段式结构输出最终分析报告（Markdown）。"
    )
    try:
        from agent.agent import extract_json
        from providers import chat_text

        text = chat_text(
            llm,
            [
                {"role": "system", "content": SYNTHESIZER_SYSTEM},
                {"role": "user", "content": user_prompt},
            ],
            json_mode=False,
        )
    except Exception as exc:
        logger.warning(
            "LLM 商业分析师综合失败，走确定性分析师兜底",
            extra={"error": f"{type(exc).__name__}: {exc}"[:500]},
        )
        return None
    if not text or not text.strip():
        logger.warning("LLM 商业分析师综合返回空文本，走确定性分析师兜底")
        return None
    stripped = text.strip()
    # 反契约输出防御：LLM 若仍回 JSON（被 extract_json 成功解析），视为失败走兜底
    if stripped.startswith("{") or stripped.startswith("["):
        if extract_json(stripped) is not None:
            logger.warning("LLM 综合输出反契约（JSON 形态），走确定性分析师兜底")
            return None
    return stripped


def _degraded_report(state: AgentState) -> str:
    """自愈额度耗尽的降级简报（审计修复 R3）。

    严禁把未加工的 scratchpad / 工具执行结果吐给前端：优先调用一次带
    惩罚约束的降级 Summarizer 生成结构化简报（只引用已定位的部分归因
    数据并注明口径差异）；LLM 不可用时回落到最小化确定性简报（不含
    任何内部轨迹细节）。
    """
    material = _analysis_material(state, include_trace=False)
    llm_report: str | None = None
    llm = _resolve_llm()
    if llm is not None:
        user_prompt = (
            f"# 用户问题\n{state.user_query}\n\n"
            f"# 已获取的部分归因数据\n{material}\n\n"
            f"# 失败概况\n自愈重试额度已耗尽（{len(state.error_context.errors)} 次），"
            "分析流程未完整走完。\n\n"
            "# 现在，按降级简报结构输出（Markdown）。"
        )
        try:
            from agent.agent import extract_json
            from providers import chat_text

            text = chat_text(
                llm,
                [
                    {"role": "system", "content": DEGRADED_SUMMARIZER_SYSTEM},
                    {"role": "user", "content": user_prompt},
                ],
                json_mode=False,
            )
            stripped = (text or "").strip()
            if stripped and not (
                (stripped.startswith("{") or stripped.startswith("["))
                and extract_json(stripped) is not None
            ):
                llm_report = stripped
        except Exception as exc:
            logger.warning(
                "降级 Summarizer LLM 调用失败，走确定性降级简报",
                extra={"error": f"{type(exc).__name__}: {exc}"[:500]},
            )
            llm_report = None
    if llm_report:
        return llm_report
    logger.warning("降级简报走确定性渲染（LLM 不可用或反契约）")
    # 确定性降级简报：只呈现部分数据事实与口径差异，无内部调试信息
    lines = ["### 部分结论（基于已获取数据）", ""]
    lines.append("本轮分析在多次自愈后仍未完整完成，以下仅呈现已获取的部分数据事实。")
    detail_lines = _degraded_detail_lines(state)
    if detail_lines:
        lines.append("")
        lines.append("### 已定位的明细")
        lines.append("")
        lines.extend(detail_lines)
    lines.append("")
    lines.append("### 数据口径差异说明")
    lines.append(
        "部分明细数据可能未继承总览的过滤条件（订单状态/退款/时间窗口），"
        "明细合计与总览数值可能存在口径出入，请以数据审计 Tab 的原始取数为准。"
    )
    lines.append("")
    lines.append("### 后续建议")
    lines.append("请更换提问方式缩小分析范围后重试，或联系管理员检查模型服务可用性。")
    return "\n".join(lines)


def _degraded_detail_lines(state: AgentState) -> list[str]:
    """从已物化数据集提取人读维度明细（降级简报用）。

    只处理两期维度明细结构（首个字符串维度列 + gmv 列，诊断兜底口径）；
    结构不符时返回空列表由调用方省略小节——严禁把原始行直接吐给前端。
    维度列按数据实际形态识别（province/category 均可），不再硬编码省份。
    """
    workspace = workspace_path(state)
    period_values: list[dict[str, float]] = []
    dim_col = ""
    for ref in state.datasets.values():
        preview = _preview_rows(str(workspace / "inputs" / ref.get("path", "")))
        cols = [str(c) for c in ref.get("columns", [])]
        if not preview or "gmv" not in cols:
            return []
        dims = [c for c in cols if c not in ("gmv", "orders", "buyers")]
        if not dims:
            return []
        dim_col = dim_col or dims[0]
        if dim_col not in cols:
            return []
        values: dict[str, float] = {}
        d_idx, v_idx = cols.index(dim_col), cols.index("gmv")
        for row in preview:
            try:
                values[str(row[d_idx])] = values.get(str(row[d_idx]), 0.0) + float(row[v_idx])
            except (TypeError, ValueError):
                continue
        period_values.append(values)
    if len(period_values) < 2:
        return []
    base, curr = period_values[0], period_values[-1]
    deltas = sorted(
        ((k, curr.get(k, 0.0) - base.get(k, 0.0)) for k in set(base) | set(curr)),
        key=lambda item: abs(item[1]),
        reverse=True,
    )
    lines = [f"| {dim_col} | 基线期 | 当前期 | 变化 |", "| --- | --- | --- | --- |"]
    for name, delta in deltas[:5]:
        lines.append(
            f"| {name} | {_fmt_wan(base.get(name, 0.0))} | {_fmt_wan(curr.get(name, 0.0))} "
            f"| {_fmt_wan(delta)} |"
        )
    return lines


def workspace_path(state: AgentState) -> Any:
    """本会话轮次的工作区路径（预览读取用；避免循环导入的独立小函数）。"""
    from config import settings

    return settings.WORKSPACE_ROOT / state.session_id / state.turn_id


def _analysis_material(state: AgentState, *, include_trace: bool = True) -> str:
    """把产物与数据集压缩为 LLM 综合的素材文本（结构化事实，非叙述）。"""
    parts: list[str] = []
    for artifact in state.artifacts:
        if artifact.kind == "summary":
            summary = artifact.payload.get("summary", {})
            parts.append(
                "[分析产物] " + json.dumps(summary, ensure_ascii=False, default=str)[:3000]
            )
    for name, ref in state.datasets.items():
        preview = _preview_rows(str(workspace_path(state) / "inputs" / ref.get("path", "")))
        rows_desc = (
            json.dumps(preview[:20], ensure_ascii=False, default=str)
            if preview
            else "（预览不可用）"
        )
        part = (
            f"[数据集 {name}] {ref.get('rows', 0)} 行，列 {list(ref.get('columns', []))}，"
            f"预览：{rows_desc[:3000]}"
        )
        # DataQA 质检发现进入 LLM 综合素材（报告必须如实引用质量提示）
        qa_findings = (ref.get("audit") or {}).get("qa") or []
        if qa_findings:
            qa_text = "；".join(f"[{f['check']}] {f['message']}" for f in qa_findings)
            part += f"\n[数据质检发现 {name}] {qa_text}"
        parts.append(part)
    if include_trace and state.error_context.errors:
        parts.append(f"[自愈记录] 共 {len(state.error_context.errors)} 次错误被捕获并重试")
    return "\n\n".join(parts)


def _no_data_report(state: AgentState) -> str:
    """无数据场景的确定性诚实报告（2026-09 审计修复）。

    时间超界 / 全部空集时严禁让 LLM 在空素材上编造"下滑归因"式结论：
    如实说明原因 + 数据域边界 + 可行动建议，一句话也不能多编。
    """
    from config import settings

    lines: list[str] = [f"## 数据说明：{state.user_query}", ""]
    if state.no_data_reason:
        lines.append(f"**本次分析无法进行：{state.no_data_reason}。**")
    else:
        lines.append(
            "**本次分析无法进行：查询未命中任何数据（0 行）。**可能原因：过滤条件"
            "（地区/品类/支付状态等）在所选时间窗口内没有匹配记录。"
        )
    lines.append("")
    lines.append(
        f"当前数仓的数据覆盖范围截至 {settings.DATA_DOMAIN_END.isoformat()}"
        "（数据基准日期）。超出该范围的时段没有任何数据，系统不会以其他时段的"
        "数据代替作答，也不会对空数据推测结论。"
    )
    lines.append("")
    lines.append(
        "建议：请把分析时段调整到数据覆盖范围内（例如 2024 年 1 月至 6 月），"
        "或调整过滤条件后重新提问。"
    )
    # 质检发现随诚实报告可见（不吞错）：空结果等 DataQA 断言必须向用户呈现，
    # 小节格式与确定性兜底报告的既有质检小节保持一致
    for name, ref in state.datasets.items():
        findings = (ref.get("audit") or {}).get("qa") or []
        if findings:
            lines.append("")
            lines.append(f"**数据质检（{name}）**：")
            lines.extend(f"- 质检提示（{f['check']}）：{f['message']}" for f in findings)
    return "\n".join(lines)


def synthesize_node(state: AgentState) -> AgentState:
    """综合节点：执行轨迹 + 产物 => 商业分析师口径的 Markdown 报告。

    审计修复（R1 报告综合重构）：
    - LLM 可用 => 商业分析师角色（SYNTHESIZER_SYSTEM）：四段式结构
      （核心结论 -> 区域归因定位 -> 驱动因素分析 -> 业务假设与排查建议），
      数值万元/百分比化，指出主要矛盾；严禁 raw dict/json 直出；
    - LLM 不可用（离线/测试/降级）=> 确定性分析师渲染兜底：同样禁 raw
      JSON——metrics 译为人读短语、归因表渲染为 Markdown 表格。

    审计修复（T14 报告调和）：同标题 summary 小节去重；同规格图表去重；
    空数据集诚实陈述。

    审计修复（R3 降级保护）：自愈额度耗尽 => _degraded_report（带惩罚
    约束的 Summarize 简报），严禁吐未加工的 scratchpad / 工具执行结果。
    """
    from config import settings

    workspace = settings.WORKSPACE_ROOT / state.session_id / state.turn_id

    # R3 降级保护：自愈额度耗尽 => 降级简报（无论是否已获取部分数据），
    # 严禁把未加工的 scratchpad / 工具执行结果吐给前端
    if state.error_context.retries >= MAX_RETRIES:
        logger.warning(
            "自愈额度耗尽，输出降级简报",
            extra={"error": f"retries={state.error_context.retries}"},
        )
        report = _degraded_report(state)
        events.emit_event(
            events.EVENT_ARTIFACT_EMIT,
            {"artifact": {"type": "markdown_report", "title": "分析简报", "content": report}},
        )
        return state.apply(report=report, phase="done")

    # 无数据诚实报告（2026-09 审计修复）：时间超界 / 全部空集时跳过 LLM
    # 综合——空素材上的"商业分析师报告"只能是编造，确定性话术如实说明
    empty_all = bool(state.datasets) and all(
        int(ref.get("rows", 0) or 0) == 0 for ref in state.datasets.values()
    )
    if state.no_data_reason or empty_all:
        report = _no_data_report(state)
        events.emit_event(
            events.EVENT_ARTIFACT_EMIT,
            {"artifact": {"type": "markdown_report", "title": "数据说明", "content": report}},
        )
        return state.apply(report=report, phase="done")

    seen_summary_titles: set[str] = set()
    rendered_charts: list[Artifact] = []
    deduped_summaries: list[dict[str, Any]] = []
    for artifact in state.artifacts:
        if artifact.kind == "summary":
            summary = artifact.payload.get("summary", {})
            title = summary.get("title", "")
            if title and title in seen_summary_titles:
                continue  # 同小节去重（T14）：重规划多轮执行同模板只呈现一次
            if title:
                seen_summary_titles.add(title)
            deduped_summaries.append(summary)

    # LLM 商业分析师综合（R1）；素材 = 去重后的 summary 产物 + 数据集预览
    llm_report: str | None = None
    if deduped_summaries or state.datasets:
        material = _analysis_material(state, include_trace=bool(state.error_context.errors))
        if material:
            llm_report = _synthesize_with_llm(state, material)

    if llm_report:
        report = llm_report
        events.emit_event(
            events.EVENT_ARTIFACT_EMIT,
            {"artifact": {"type": "markdown_report", "title": "分析报告", "content": report}},
        )
        return state.apply(report=report, phase="done")

    # ---- 确定性分析师兜底（无 LLM / LLM 输出反契约）---- #
    lines: list[str] = [f"## 分析报告：{state.user_query}", ""]
    for summary in deduped_summaries:
        lines.extend(_summary_analyst_markdown(summary))
        lines.append("")
        lines.append("")
    for artifact in state.artifacts:
        if artifact.kind == "echarts":
            # 同规格图表去重（T14）：只呈现第一份，其余同质产物不进报告
            if _has_same_echarts(rendered_charts, artifact.payload):
                continue
            rendered_charts.append(artifact)
            lines.append(f"- 图表产物：`{artifact.name}`（ECharts 规格已生成）")
    # 纯查询结果直接呈现（取数成功但没有沙箱分析的场景）
    for name, ref in state.datasets.items():
        section, table_artifact = _dataset_analyst_markdown(name, ref, workspace)
        lines.append("")
        lines.append(section)
        if table_artifact is not None:
            events.emit_event(events.EVENT_ARTIFACT_EMIT, {"artifact": table_artifact})
        # DataQA 质检小节（行动项 2）：结果断言发现必须向用户可见（不吞错）
        qa_findings = (ref.get("audit") or {}).get("qa") or []
        if qa_findings:
            lines.append("")
            lines.append(f"**数据质检（{name}）**：")
            for f in qa_findings:
                lines.append(f"- 质检提示（{f['check']}）：{f['message']}")
    if not state.artifacts and not state.datasets:
        lines.append("未能获得有效的分析产物。")
    if state.datasets and all(int(ref.get("rows", 0) or 0) == 0 for ref in state.datasets.values()):
        # 空集诚实陈述（审计修复 D3）：严禁在 0 行数据上宣称查询成功
        lines.append("")
        lines.append(
            "未查询到符合条件的数据。可能原因：时间范围超出数仓数据域"
            "（数据基准日期 2024-06-30），或过滤条件（地区/品类/支付状态）无匹配记录。"
        )
    if state.error_context.errors:
        lines.append("")
        lines.append(f"> 自愈记录：{len(state.error_context.errors)} 次错误被捕获并重试。")
    lines.append("")
    lines.append(f"（执行轨迹 {len(state.tool_calls)} 步；数据集 {len(state.datasets)} 个）")
    report = "\n".join(lines)
    events.emit_event(
        events.EVENT_ARTIFACT_EMIT,
        {"artifact": {"type": "markdown_report", "title": "分析报告", "content": report}},
    )
    return state.apply(report=report, phase="done")


__all__ = [
    "clarify_node",
    "code_exec_node",
    "critic_node",
    "drilldown_by_information_gain",
    "dsl_query_node",
    "multiplicative_decomposition",  # re-export 供测试
    "planner_node",
    "schema_digest",
    "synthesize_node",
]
