"""Multi-Tool Agent 调度内核：Plan & Select -> Execute & Guard -> Replan & Synthesize。

把原有单路径"NL -> DSL -> SQL 执行"升级为"工具调度状态循环"：
1. **Plan & Select**：将已注册工具清单（Function Calling JSON Schema）注入 LLM
   上下文（或使用确定性规则），由 LLM/规则决定直接回答、反问澄清或调用一个
   或多个工具；
2. **Execute & Guard**：入参经 Pydantic 严格校验（args_schema, extra="forbid"），
   触发工具执行；未知工具名 / 非法参数 / 越权行为一律被拦截并结构化记录；
3. **Replan（观察驱动重规划，R1）**：支持迭代的规划器（LLMPlanner）在每批调用
   执行完毕后拿到完整调度轨迹（含结果摘要），自行决策：继续调用工具补齐信息、
   给出最终洞察、反问澄清或终止；中间结果（observation）真正参与导航，
   而非仅用于失败自愈；
4. **Self-Correction & Synthesize**：工具报错触发一次自愈修复（受 Max Steps
   约束）；最终合成综合洞察 + 图表渲染指令（ChartSpec）+ 导出链接。

调度轨迹（ToolInvocationRecord）包含每一步的工具名、入参、耗时、成功/异常状态
与输出摘要，可完整接入审计链路（web.service 落 audit record.steps）。

确定性兜底：未配置 LLM 时使用关键词规则规划（离线可运行、可单测），
与既有确定性 Agent 哲学一致；确定性规划器不参与重规划循环（iterative=False），
行为与单批调度完全一致。
"""

from __future__ import annotations

import json
import re
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from agent.agent import extract_json
from agent.clarify import Clarification, detect_clarifications
from agent.errors import PipelineError
from agent.heuristic import REGIONS, dimension_members
from agent.llm import resolve_default_client
from agent.router import (
    BLOCKED_DESTRUCTIVE_REPLY,
    BLOCKED_SENSITIVE_REPLY,
    IntentType,
)
from audit.logging import get_logger
from config import settings
from providers import chat_text
from semantic.dsl_schema import QueryDSL
from tools.base import ToolContext, ToolResult
from tools.registry import ToolRegistry, default_registry

logger = get_logger("agent.tool_agent")

CHITCHAT_REPLY = "抱歉，我是数据分析助手，只能回答与业务数据相关的问题。"

# 空结果统一话术（审计修复 D3：0 行/全 NULL 结果严禁"已成功查询"式肯定答复）
NO_DATA_REPLY = "未查询到符合条件的数据。可能原因：{reason}"

# 数仓数据域上界（与评测锚点一致的元数据；空结果时间归因用它判断超界）
_DATA_DOMAIN_TIP = "所选时间范围可能超出当前数仓数据域（数据基准日期 2024-06-30）"

# 确定性规划关键词
_EXPORT_KEYWORDS = (
    "导出",
    "下载",
    "表格",
    "明细",
    "清单",
    "报表",
    "csv",
    "excel",
    "markdown",
    "转储",
)
_TREND_KEYWORDS = (
    "环比",
    "同比",
    "趋势",
    "走势",
    "累计",
    "移动平均",
    "滑动平均",
    "补零",
    "每日",
    "每周",
    "每月",
    "按天",
    "按月",
    "按周",
    "连续",
    "yoy",
    "mom",
    "变化",
)

# --------------------------------------------------------------------------- #
# R2 对比型问题（确定性分解）：触发词 / 脚手架词 / 实体候选池
# --------------------------------------------------------------------------- #
_COMPARATIVE_TRIGGERS = (
    "哪个",
    "哪方",
    "谁更",
    "谁高",
    "对比",
    "比较",
    "相比",
    "更高",
    "更低",
    "更多",
    "更少",
)
_COMPARATIVE_SCAFFOLD = re.compile(
    r"哪个|哪方|谁|更(高|低|多|少|大|小|好|差)|相比|对比|比较|分别|各自"
)


def _is_comparative_question(query: str) -> bool:
    """是否为对比型问题（A 和 B 哪个更 X），供分解 / 反思 / 综合三处共用。"""
    return any(k in query for k in _COMPARATIVE_TRIGGERS)


def _comparative_entities(query: str) -> list[str]:
    """按出现位置提取查询中的可对比实体（大区/省份/品类，值域全部来自语义目录）。"""
    pools = [REGIONS.keys(), dimension_members("province"), dimension_members("category")]
    seen: dict[str, int] = {}
    for pool in pools:
        for entity in pool:
            if entity and entity in query and entity not in seen:
                seen[entity] = query.index(entity)
    return [e for e, _ in sorted(seen.items(), key=lambda kv: kv[1])]


def decompose_comparison(query: str) -> list[tuple[str, str]] | None:
    """对比型问题确定性分解（R2）：『A 和 B 哪个<指标>更X』-> [(A, 子查询A), (B, 子查询B)]。

    仅当命中对比触发词且识别到 >=2 个可对比实体（大区/省份/品类，值域来自
    语义目录白名单）时分解；子查询剔除其余实体与对比脚手架词，保留时间窗口
    与指标表述，可被启发式解析为合法 DSL（区域过滤 + 时间过滤均已验证）。
    任何一步剔除后为空即返回 None（保持整问单查），绝不冒险猜测。
    """
    if not _is_comparative_question(query):
        return None
    entities = _comparative_entities(query)
    if len(entities) < 2:
        return None
    pairs: list[tuple[str, str]] = []
    for keep in entities:
        sub = query
        for other in entities:
            if other != keep:
                sub = re.sub(rf"[和与跟]?{re.escape(other)}", "", sub)
        sub = _COMPARATIVE_SCAFFOLD.sub("", sub)
        sub = re.sub(rf"[和与跟](?={re.escape(keep)})", "", sub)
        sub = re.sub(r"呢\s*[？?！!。]*$", "", sub).strip(" 　，,。：:？?！!")
        if not sub or keep not in sub:
            return None
        pairs.append((keep, sub))
    return pairs


def _comparison_label(data: dict[str, Any]) -> str:
    """从单值结果的 DSL 过滤条件推导对比标签（大区 > 省份 > 品类 > 退化为解释前缀）。"""
    dsl = data.get("dsl")
    filters = dsl.get("filters") if isinstance(dsl, dict) else None
    if isinstance(filters, list):
        for f in filters:
            if not isinstance(f, dict):
                continue
            field_name, value = f.get("field"), f.get("value")
            if field_name == "province":
                if isinstance(value, list) and value:
                    province = str(value[0])
                    for region, provinces in REGIONS.items():
                        if province in provinces:
                            return region
                    return province
                if isinstance(value, str):
                    return value
            if field_name == "category" and isinstance(value, str):
                return value
    explanation = data.get("explanation")
    return str(explanation)[:12] if explanation else "对比项"


def _metric_label(outputs: list[ToolResult]) -> str:
    """从最后一组成功数据输出的主指标列推导中文标签（与 present.labels 同源）。"""
    for o in reversed(outputs):
        data = o.data if o.success and isinstance(o.data, dict) else None
        columns = (data or {}).get("columns")
        if columns:
            col = str(columns[0])
            try:
                from present.labels import FIELD_LABELS

                return str(FIELD_LABELS.get(col, col))
            except ImportError:  # pragma: no cover - 依赖缺失时回退原始列名
                return col
    return "指标"


# --------------------------------------------------------------------------- #
# 数据结构
# --------------------------------------------------------------------------- #
@dataclass
class ToolCall:
    """一次工具调用计划（调度内核的最小执行单元）。"""

    tool: str
    args: dict[str, Any] = field(default_factory=dict)
    reason: str = ""


@dataclass
class ToolInvocationRecord:
    """一次工具调用的完整轨迹（审计与前端展示共用）。"""

    step: int
    tool: str
    args: dict[str, Any]
    success: bool
    duration_ms: float = 0.0
    error_msg: str | None = None
    error_type: str | None = None
    display_type: str | None = None
    summary: str | None = None
    output: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        """序列化为前端 / 审计消费的字典（Serialize for audit & frontend）。"""
        return {
            "step": self.step,
            "tool": self.tool,
            "args": self.args,
            "success": self.success,
            "duration_ms": self.duration_ms,
            "error_msg": self.error_msg,
            "error_type": self.error_type,
            "display_type": self.display_type,
            "summary": self.summary,
            "output": self.output,
        }


@dataclass
class PlanResult:
    """规划结果：调用哪些工具 / 直接回答 / 反问澄清。"""

    calls: list[ToolCall] = field(default_factory=list)
    answer: str | None = None
    clarifications: list[Clarification] = field(default_factory=list)


@dataclass
class AgentResult:
    """Agent 一次调度的最终结果（复合输出：洞察 + 图表 + 导出链接 + 轨迹）。"""

    query: str
    answer: str = ""
    steps: list[ToolInvocationRecord] = field(default_factory=list)
    error: str | None = None
    error_type: str | None = None
    degraded: bool = False
    intent: str = IntentType.DATA_QUERY.value

    # 数据工具产物（供 web 层透传）
    dsl: QueryDSL | None = None
    sql: str | None = None
    columns: list[str] | None = None
    rows: list[list[Any]] | None = None
    explanation: str | None = None
    viz: dict[str, Any] | None = None
    chart_spec: dict[str, Any] | None = None
    download_urls: list[str] = field(default_factory=list)
    documents: list[dict[str, Any]] = field(default_factory=list)
    clarifications: list[dict[str, Any]] = field(default_factory=list)
    rewrites: int = 0
    scan_rows: int = 0
    # R1 观察驱动重规划：执行后基于轨迹追加的调度轮数（0 = 单批调度）
    replans: int = 0
    # R3 反思层：调度终止后的结果充分性自检留痕（None = 未启用/未触发）
    reflection: dict[str, Any] | None = None

    def step_tools(self) -> list[str]:
        """返回调度轨迹中依次调用的工具名（Tool names in execution order）。"""
        return [s.tool for s in self.steps]

    def to_dict(self) -> dict[str, Any]:
        """序列化为 API 响应字典（Serialize to the API response payload）。"""
        return {
            "query": self.query,
            "answer": self.answer,
            "steps": [s.to_dict() for s in self.steps],
            "error": self.error,
            "error_type": self.error_type,
            "degraded": self.degraded,
            "intent": self.intent,
            "chart_spec": self.chart_spec,
            "download_urls": self.download_urls,
            "documents": self.documents,
            "clarifications": self.clarifications,
            "replans": self.replans,
            "reflection": self.reflection,
        }


# --------------------------------------------------------------------------- #
# 规划器
# --------------------------------------------------------------------------- #
class Planner(ABC):
    """规划器抽象：决定本轮调度调用哪些工具（或直接回答/反问）。"""

    # R1 观察驱动重规划：是否支持在执行后基于轨迹继续决策（ToolAgent 据此
    # 决定是否进入重规划循环）。确定性规划器保持单批调度语义，不参与循环。
    iterative: bool = False

    @abstractmethod
    def plan(
        self,
        query: str,
        principal: str | None,
        registry: ToolRegistry,
        *,
        history: Any = None,
        last_dsl: Any = None,
    ) -> PlanResult:
        """规划本轮调度：产出工具调用 / 直接作答 / 反问澄清三选一（Decide this turn's actions）。"""
        ...

    def plan_next(
        self,
        query: str,
        principal: str | None,
        registry: ToolRegistry,
        *,
        steps: list[ToolInvocationRecord],
        outputs: list[ToolResult],
        remaining_steps: int,
    ) -> PlanResult:
        """观察驱动重规划：把已执行轨迹交给规划器，决策下一步动作。

        返回值语义与 ``plan`` 一致：``calls`` 继续执行 / ``answer`` 直接作答 /
        ``clarifications`` 反问澄清；三者皆空表示信息已充分（终止调度）。
        默认实现保守终止：不支持迭代的规划器不会在此被调用（ToolAgent 以
        ``iterative`` 门控），此处仅作协议兜底。
        """
        return PlanResult()

    def correct(
        self,
        query: str,
        principal: str | None,
        failed: ToolCall,
        record: ToolInvocationRecord,
    ) -> ToolCall | None:
        """自愈修复：工具失败后返回修正后的调用（None 表示不修复）。"""
        return None


class DeterministicPlanner(Planner):
    """确定性规划：消费统一五分类意图判决（IntentRouter）+ 关键词（趋势/导出）-> 工具。

    双路由合并（历史缺陷修复）：不再使用旧的独立三分类 classify_intent，
    而是复用 agent.router.intent_router 的五分类判决中心（Fast-Path -> LLM -> 规则
    兜底），保证 Agent 内部分派与 web.service 的分流决策完全一致，杜绝"两层路由
    结论打架"导致的意图漂移。
    """

    def plan(
        self,
        query: str,
        principal: str | None,
        registry: ToolRegistry,
        *,
        history: Any = None,
        last_dsl: Any = None,
    ) -> PlanResult:
        """确定性规划：五分类意图判决 + 关键词规则产出调用计划（Rule-based, zero LLM）。"""
        from agent.router import IntentType, route_decision

        # 与 web.service 分流共用同一五分类判决中心：携带会话状态（history/last_dsl），
        # 使"那华南呢"这类上下文追问被正确判为 DATA_QUERY（而非孤立输入误判 CLARIFY）
        decision = route_decision(query, history=history, last_dsl=last_dsl, principal=principal)
        intent = decision.intent

        if intent == IntentType.CHITCHAT:
            return PlanResult(answer=CHITCHAT_REPLY)
        if intent == IntentType.UNSAFE_ACTION:
            # 安全拦截意图（破坏性指令 / 越界敏感实体）：如实拒绝，绝不查询、
            # 绝不产出"操作已完成"式答复（审计修复 D2）
            blocked_reason = decision.extracted_entities.get("blocked_reason")
            return PlanResult(
                answer=(
                    BLOCKED_SENSITIVE_REPLY
                    if blocked_reason == "sensitive_entity"
                    else BLOCKED_DESTRUCTIVE_REPLY
                )
            )
        if intent == IntentType.SYSTEM_ACTION:
            # 系统控制动作由 web.service 白名单执行；Agent 层不触达数仓引擎
            return PlanResult(answer="系统操作已由上层安全处理，无需查询数据。")
        if intent == IntentType.GLOSSARY_EXPLAIN:
            return PlanResult(calls=[ToolCall("explain_glossary", {"query": query})])
        if intent == IntentType.CLARIFY:
            # 澄清反问：优先使用路由判决预提取的澄清问题（缺失时间 / 未定义指标 / 信息不足）
            clarifications = decision.extracted_entities.get("clarifications") or []
            if not clarifications:
                clarifications = [c.to_dict() for c in detect_clarifications(query)]
            return PlanResult(
                clarifications=[Clarification(**c) for c in clarifications if isinstance(c, dict)]
            )

        # DATA_QUERY：关键词分派（趋势 / 导出 / 对比分解 / 即时点查）
        ql = query.lower()
        if any(k in ql for k in _EXPORT_KEYWORDS):
            # 组合调用：先查询（复用 query_metric），再把结果交给导出工具
            return PlanResult(
                calls=[
                    ToolCall("query_metric", {"query": query}, reason="导出前先查询数据"),
                    ToolCall("export_report", {"query": query}, reason="导出为可下载文件"),
                ]
            )
        if any(k in ql for k in _TREND_KEYWORDS):
            return PlanResult(
                calls=[ToolCall("trend_analysis", {"query": query}, reason="时序/对比分析")]
            )
        # R2 对比分解：『A 和 B 哪个更 X』-> 分别查询每个对比对象，由合成器跨步对比
        decomposed = decompose_comparison(query)
        if decomposed:
            return PlanResult(
                calls=[
                    ToolCall("query_metric", {"query": sub}, reason=f"对比分解：{entity}")
                    for entity, sub in decomposed
                ]
            )
        return PlanResult(calls=[ToolCall("query_metric", {"query": query}, reason="即时指标点查")])


class LLMPlanner(Planner):
    """LLM 规划：把工具清单（JSON Schema）注入上下文，由 LLM 决策工具调用。

    协议：LLM 只输出一个 JSON 对象，取值四选一：
    - ``{"tool": "<已注册工具名>", "args": {...}}``：调用工具；
    - ``{"answer": "..."}``：直接回答（无需工具）；
    - ``{"clarify": "..."}``：反问澄清；
    - ``{"done": "..."}```：信息已充分，终止调度（重规划阶段使用）。
    任何非法工具名 / 非法参数都会被校验拦截并反馈 LLM 重试（max_retries 次）。

    R1 观察驱动重规划（iterative=True）：``plan_next`` 把已执行轨迹（含每步
    结果摘要与解释）喂回 LLM，由其基于中间结果决定继续查询 / 作答 / 反问 /
    终止——中间结果（observation）真正参与调度导航，而非仅用于失败自愈。
    """

    iterative = True

    def __init__(
        self,
        client: Any,
        registry: ToolRegistry | None = None,
        max_retries: int = 2,
    ):
        """初始化 LLM 规划器（Bind the OpenAI-compatible client and retry budget）。"""
        self.client = client
        self.registry = registry  # 构造注入：correct() 自愈路径依赖工具注册表校验
        self.max_retries = max_retries

    def plan(
        self,
        query: str,
        principal: str | None,
        registry: ToolRegistry,
        *,
        history: Any = None,
        last_dsl: Any = None,
    ) -> PlanResult:
        """首轮规划：把工具清单（JSON Schema）注入上下文，由 LLM 决策调用 / 作答 / 反问。

        会话上下文（审计修复 M2）：last_dsl 非 None 时注入上轮查询结构（指标/
        维度/时间窗口摘要），使"按品类展开""那华南呢"等省略指代下钻指令能被
        规划器正确理解为继承上轮口径的查询，而非反问澄清。
        """
        tools_json = json.dumps(registry.tool_definitions(), ensure_ascii=False)
        context_block = ""
        if last_dsl is not None:
            try:
                dsl_summary = (
                    last_dsl.model_dump(mode="json")
                    if hasattr(last_dsl, "model_dump")
                    else last_dsl
                )
                context_block = (
                    "\n上一轮查询口径（本轮为省略指代/下钻指令时，继承其中未提及的"
                    "指标与时间窗口，仅做用户要求的增量调整，直接调用 query_metric）：\n"
                    + json.dumps(dsl_summary, ensure_ascii=False)
                    + "\n"
                )
            except Exception:  # 摘要失败不阻塞规划
                context_block = ""
        messages = [
            {
                "role": "system",
                "content": (
                    "你是数据分析 Agent 的规划器。根据用户问题决定是否调用工具。\n"
                    "可用的工具清单（OpenAI Function Calling 规范）：\n"
                    + tools_json
                    + context_block
                    + "\n输出要求：只输出一个 JSON 对象，三选一：\n"
                    '{ "tool": "<工具名>", "args": {...} }\n'
                    '{ "answer": "无需查询的直接回答文本" }\n'
                    '{ "clarify": "需要向用户追问他的一句问题" }\n'
                    "禁止输出解释或多余文字。若问题需要数据但缺少关键信息，输出 clarify。\n"
                    "对比类问题（如『A 和 B 哪个更高』）应分别查询每个对比对象"
                    "（多次调用 query_metric，每次查询聚焦单个对象），全部查完后再综合作答。"
                ),
            },
            {"role": "user", "content": f"问题：{query}"},
        ]
        return self._decide(query, registry, messages, context="规划")

    def plan_next(
        self,
        query: str,
        principal: str | None,
        registry: ToolRegistry,
        *,
        steps: list[ToolInvocationRecord],
        outputs: list[ToolResult],
        remaining_steps: int,
    ) -> PlanResult:
        """观察驱动重规划：携带完整执行轨迹再次决策（R1 核心入口）。"""
        tools_json = json.dumps(registry.tool_definitions(), ensure_ascii=False)
        trajectory = json.dumps(self._trajectory_view(steps, outputs), ensure_ascii=False)
        messages = [
            {
                "role": "system",
                "content": (
                    "你是数据分析 Agent 的规划器。已经执行过若干工具调用，"
                    "执行轨迹（含结果摘要）如下，请判断当前信息是否足以回答问题：\n"
                    + trajectory
                    + '\n\n若信息已足够 -> 输出 {"answer": "基于轨迹的最终中文洞察"}；'
                    "若还差数据（如对比类问题只查了一个对象）-> 继续调用工具补齐；"
                    "若需用户补充 -> 输出 clarify；若无需继续 -> 输出 done。\n"
                    "可用的工具清单（OpenAI Function Calling 规范）：\n"
                    + tools_json
                    + "\n\n剩余可用步数："
                    + str(remaining_steps)
                    + "（必须在预算内决策，预算紧张时优先收敛作答）。\n"
                    "输出要求：只输出一个 JSON 对象，四选一：\n"
                    '{ "tool": "<工具名>", "args": {...} }\n'
                    '{ "answer": "基于已有轨迹的最终中文洞察" }\n'
                    '{ "clarify": "需要向用户追问的一句问题" }\n'
                    '{ "done": "信息已充分或无必要继续" }\n'
                    "禁止输出解释或多余文字。"
                ),
            },
            {"role": "user", "content": f"问题：{query}"},
        ]
        return self._decide(query, registry, messages, context="重规划")

    def _decide(
        self,
        query: str,
        registry: ToolRegistry,
        messages: list[dict[str, str]],
        *,
        context: str,
    ) -> PlanResult:
        """共享决策核：调 LLM -> 解析校验 -> 非法输出反馈重试（plan/plan_next 共用）。"""
        last_error: Exception | None = None
        for _ in range(self.max_retries + 1):
            raw = chat_text(self.client, messages)
            try:
                obj = extract_json(raw)
                if "answer" in obj:
                    return PlanResult(answer=str(obj["answer"]))
                if "clarify" in obj:
                    return PlanResult(
                        clarifications=[
                            Clarification(
                                kind="llm_clarify", term=None, question=str(obj["clarify"])
                            )
                        ]
                    )
                name = str(obj.get("tool", ""))
                if not name:
                    # {"done": ...} 或空对象：信息已充分，终止调度
                    return PlanResult()
                args = obj.get("args") or {}
                tool = registry.get_tool(name)  # 未注册 -> UnknownToolError
                tool.validate_args(args)  # 非法参数 -> ValidationError
                reason = "LLM 决策" if context == "规划" else "LLM 重规划"
                return PlanResult(calls=[ToolCall(name, dict(args), reason=reason)])
            except Exception as exc:
                last_error = exc
                messages = [
                    *messages[:2],
                    {"role": "assistant", "content": raw},
                    {
                        "role": "user",
                        "content": f"你上次的输出无效：{str(exc)[:400]}\n请重新输出合法 JSON。",
                    },
                ]
        raise PipelineError(
            f"LLM {context}器重试 {self.max_retries} 次后仍无法产出合法决策: {last_error}"
        ) from last_error

    @staticmethod
    def _trajectory_view(
        steps: list[ToolInvocationRecord], outputs: list[ToolResult]
    ) -> list[dict[str, Any]]:
        """把调度轨迹压缩为 LLM 可消费的观察视图（截断防 Token 膨胀）。

        审计修复（D6 答案层数值透传）：成功的数据步骤附带列名与前 10 行样本值，
        使重规划作答（plan_next 的 answer）与反思判定都能引用真实数值，
        而非只知道 row_count 答不出数。
        """
        view: list[dict[str, Any]] = []
        for s, o in zip(steps, outputs, strict=False):
            item: dict[str, Any] = {
                "step": s.step,
                "tool": s.tool,
                "args": s.args,
                "success": s.success,
            }
            if s.success:
                if s.summary:
                    item["summary"] = s.summary
                data = o.data if isinstance(o.data, dict) else {}
                if data.get("explanation"):
                    item["explanation"] = str(data["explanation"])[:200]
                if isinstance(data.get("rows"), list):
                    item["row_count"] = len(data["rows"])
                    columns = data.get("columns")
                    if isinstance(columns, list) and columns:
                        item["columns"] = [str(c) for c in columns]
                        item["rows_sample"] = [
                            [_coerce_scalar(v) for v in row] for row in data["rows"][:10]
                        ]
            else:
                item["error"] = (s.error_msg or "")[:200]
            view.append(item)
        return view

    def correct(
        self,
        query: str,
        principal: str | None,
        failed: ToolCall,
        record: ToolInvocationRecord,
    ) -> ToolCall | None:
        """自愈修复：把工具失败原因喂回 LLM，重新规划一次。

        依赖构造注入的 ``registry`` 校验修正后的工具调用（get_tool + validate_args），
        未注入注册表时无法自愈，安全返回 None（绝不静默吞掉内部错误）。
        """
        if self.registry is None:
            logger.warning(
                "llm_correct_skipped",
                extra={
                    "event": "llm_correct_skipped",
                    "reason": "missing_registry",
                    "tool": failed.tool,
                },
            )
            return None
        messages = [
            {
                "role": "system",
                "content": (
                    "你是数据分析 Agent 的规划器。上一次工具调用失败，请根据报错"
                    "重新输出 JSON：{ 'tool': ..., 'args': {...} } 或 { 'answer': ... }。"
                ),
            },
            {
                "role": "user",
                "content": (
                    f"问题：{query}\n失败工具：{failed.tool}\n"
                    f"失败原因：{record.error_msg or ''}\n请给出修正后的调用。"
                ),
            },
        ]
        try:
            raw = chat_text(self.client, messages)
            obj = extract_json(raw)
            name = str(obj.get("tool", ""))
            args = obj.get("args") or {}
            tool = self.registry.get_tool(name)
            tool.validate_args(args)
            return ToolCall(name, dict(args), reason="LLM 自愈修复")
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            # 收窄异常范围：仅捕获可预期的解析/校验错误，杜绝"静默吞 AttributeError"类死代码
            logger.warning(
                "llm_correct_failed",
                extra={
                    "event": "llm_correct_failed",
                    "error": f"{type(exc).__name__}: {exc}",
                    "tool": failed.tool,
                },
            )
            return None


# --------------------------------------------------------------------------- #
# 数据上下文注入（审计修复 D6 答案层与数据层断裂）：把真实行值/标量注入总结提示词
# --------------------------------------------------------------------------- #
def _coerce_scalar(value: Any) -> Any:
    """把 DuckDB 返回的 datetime 等类型转为 JSON 可序列化标量。"""
    if isinstance(value, datetime):
        return value.isoformat(sep=" ")
    if isinstance(value, float) and value != value:  # NaN
        return None
    return value


def build_data_context(columns: list[str] | None, rows: list[list[Any]] | None) -> str:
    """把查询结果集构造为总结 LLM 可直接引用的数据上下文。

    规则（审计修复 D6 / T02）：
    - 标量或行数 <= 10：完整结果集以 Markdown 表格注入（"报数"类问题据此引用数值）；
    - 行数 > 10：注入前 5 行样本 + 全局聚合统计（数值列 sum/avg/min/max + 行数）；
    - 空结果返回空字符串（由空结果话术接管，不喂给 LLM 编造）。
    """
    cols = [str(c) for c in (columns or [])]
    data_rows = [[_coerce_scalar(v) for v in row] for row in (rows or [])]
    if not cols or not data_rows:
        return ""

    def _md_table(rs: list[list[Any]]) -> str:
        head = "| " + " | ".join(cols) + " |"
        sep = "|" + "|".join([" --- "] * len(cols)) + "|"
        body = "\n".join(
            "| " + " | ".join("" if v is None else str(v) for v in r) + "|" for r in rs
        )
        return "\n".join([head, sep, body])

    if len(data_rows) <= 10:
        return f"查询结果（{len(data_rows)} 行，可直接引用数值）：\n{_md_table(data_rows)}"

    # >10 行：前 5 行样本 + 全局数值聚合统计
    stats: dict[str, dict[str, float]] = {}
    for i, col in enumerate(cols):
        nums = [
            float(r[i])
            for r in data_rows
            if i < len(r) and isinstance(r[i], (int, float)) and not isinstance(r[i], bool)
        ]
        if nums:
            stats[col] = {
                "sum": round(sum(nums), 4),
                "avg": round(sum(nums) / len(nums), 4),
                "min": round(min(nums), 4),
                "max": round(max(nums), 4),
            }
    stat_lines = [
        f"- {col}: sum={s['sum']}, avg={s['avg']}, min={s['min']}, max={s['max']}"
        for col, s in stats.items()
    ]
    parts = [
        f"查询结果共 {len(data_rows)} 行（以下为前 5 行样本，完整数据未全部列出）：",
        _md_table(data_rows[:5]),
    ]
    if stat_lines:
        parts.append("数值列全局统计（求和/平均/最小/最大，可用于汇总性回答）：")
        parts.extend(stat_lines)
    return "\n".join(parts)


def empty_result_reason(dsl: Any) -> str | None:
    """空结果的确定性归因（审计修复 D3：空集必须解释可能原因，绝不谎报成功）。

    - 时间窗口超出数仓数据域（相对时间锚定 AS_OF_DATE=2024-06-30 后仍超界，
      或绝对窗口整体晚于数据域上界）-> 时间超界假设；
    - 其余空集 -> 过滤条件无匹配假设。
    """
    if dsl is None:
        return "过滤条件可能无匹配数据"
    tf = getattr(dsl, "time_filter", None)
    if tf is not None:
        try:
            from agent.time_utils import time_window_outside_domain

            if time_window_outside_domain(tf):
                return _DATA_DOMAIN_TIP
        except Exception:  # 归因失败不阻塞话术兜底
            pass
    return "过滤条件（时间/地区/品类等）可能与数据不匹配，或该维度下确无记录"


def _is_empty_result(
    columns: list[str] | None,
    rows: list[list[Any]] | None,
    *,
    has_ratio_metric: bool = False,
) -> bool:
    """空结果判定（审计修复 D3 / T08）。

    - 0 行 / 无列：空；
    - 聚合空集产出的单行全 NULL（SUM 空集 = [[None]]）：空；
    - 单行全 0（COUNT 空集 = [[0]]，如 2025-01 超界窗口）：视为无匹配数据，
      统一走"未查询到符合条件的数据"诚实话术；
    - 含比率指标的结果例外：比率为 0 是有效业务语义（R4a：无退款记录的品类
      退款率 = 0.00%），仅按全 NULL 判空。
    """
    if not rows or not columns:
        return True
    if has_ratio_metric:
        return all(all(v is None for v in row) for row in rows)
    return all(all(v is None or v == 0 for v in row) for row in rows)


# --------------------------------------------------------------------------- #
# 总结器
# --------------------------------------------------------------------------- #
class Synthesizer(ABC):
    """总结器抽象：把工具执行结果合成最终洞察。"""

    @abstractmethod
    def synthesize(self, result: AgentResult, outputs: list[ToolResult], query: str) -> None:
        """把工具输出合成为最终洞察（Compose the final insight；原地写入 result）。"""


class DeterministicSynthesizer(Synthesizer):
    """确定性合成：基于工具输出拼装洞察 + 图表指令（零幻觉、可测）。"""

    def synthesize(self, result: AgentResult, outputs: list[ToolResult], query: str) -> None:
        """确定性合成：按末位工具类型分派作答模板并回填数据字段（zero LLM, zero hallucination）。"""
        if result.clarifications:
            result.answer = "；".join(c["question"] for c in result.clarifications)
            return
        if not outputs:
            result.answer = CHITCHAT_REPLY
            return

        last = outputs[-1]
        if not last.success:
            result.error = last.error_msg
            result.error_type = (last.meta or {}).get("error_type")
            result.answer = last.error_msg or "工具执行失败"
            return

        data = last.data or {}
        tool_name = result.steps[-1].tool if result.steps else ""
        if tool_name == "explain_glossary":
            docs = data.get("documents", [])
            result.documents = docs
            titles = "、".join(d.get("title", "") for d in docs)
            result.answer = f"已检索到 {len(docs)} 条口径文档：{titles}"
            return
        if tool_name == "export_report":
            result.download_urls = (
                [last.meta.get("download_url", "")] if last.meta.get("download_url") else []
            )
            url = last.meta.get("download_url", "")
            notes = data.get("notes") or []
            note_txt = "；".join(notes)
            result.answer = (
                f"已生成导出文件（{data.get('filename', '')}，{data.get('row_count', 0)} 行）。"
                + (f"下载链接：{url}" if url else "")
                + (f"；{note_txt}" if note_txt else "")
            )
            return
        # query_metric / trend_analysis：数据型工具
        result.explanation = data.get("explanation")
        result.viz = data.get("viz")
        result.chart_spec = data.get("chart_spec")
        result.dsl = (
            QueryDSL.model_validate(data["dsl"]) if isinstance(data.get("dsl"), dict) else None
        )
        result.sql = data.get("sql")
        result.columns = data.get("columns")
        result.rows = data.get("rows")
        result.rewrites = int(data.get("rewrites") or 0)
        result.scan_rows = int(data.get("scan_rows") or 0)
        result.degraded = result.degraded or bool(data.get("degraded"))

        # R2 对比综合：对比型问题拿到 >=2 组单值结果 -> 跨步对比作答 + 柱状图
        if self._apply_comparison_answer(result, outputs):
            return

        rows = result.rows or []
        viz = result.viz or {}
        explanation = (result.explanation or "").rstrip("。")
        # 空结果强制诚实话术（审计修复 D3）：0 行 / 单行全 NULL / 单行全 0 时统一
        # "未查询到符合条件的数据 + 可能原因"，严禁在空集上输出"已成功查询"式答复；
        # 比率指标例外（R4a：0 是有效语义）
        has_ratio = bool(result.dsl and any(m.kind == "ratio" for m in result.dsl.metrics))
        if _is_empty_result(result.columns, rows, has_ratio_metric=has_ratio):
            reason = empty_result_reason(result.dsl)
            result.answer = NO_DATA_REPLY.format(reason=reason)
            return
        if viz.get("chart") == "number" and rows:
            label = viz.get("y") or (result.columns[0] if result.columns else "数值")
            value = rows[0][0]
            if isinstance(value, float):
                value = round(value, 2)
            result.answer = f"{label} = {value}；{explanation}。"
        else:
            result.answer = f"{explanation}。返回 {len(rows)} 行结果。"

    # ------------------------------------------------------------------ #
    @staticmethod
    def _apply_comparison_answer(result: AgentResult, outputs: list[ToolResult]) -> bool:
        """R2 对比综合：>=2 组成功单值结果 -> 跨步对比作答 + 对比柱状图。

        只在对比型问题且数据可比较时生效（返回 True 表示已接管作答）；
        其余情况返回 False 走既有单输出作答路径。
        """
        if not _is_comparative_question(result.query):
            return False
        pairs: list[tuple[str, Any]] = []
        for o in outputs:
            data = o.data if o.success and isinstance(o.data, dict) else None
            rows = (data or {}).get("rows")
            if isinstance(rows, list) and len(rows) == 1 and rows[0]:
                pairs.append((_comparison_label(data or {}), rows[0][0]))
        numeric = [
            (label, float(value))
            for label, value in pairs
            if isinstance(value, (int, float)) and not isinstance(value, bool)
        ]
        if len(numeric) < 2:
            return False
        rendered = [(label, round(value, 2)) for label, value in numeric]
        metric = _metric_label(outputs)
        parts = "，".join(f"{label} {metric}={value}" for label, value in rendered)
        hi_label, hi = max(rendered, key=lambda p: p[1])
        _, lo = min(rendered, key=lambda p: p[1])
        if hi == lo:
            tail = "两者持平"
        else:
            tail = f"{hi_label} 更高，高出约 {abs(hi - lo) / lo * 100:.1f}%" if lo else ""
        result.answer = f"对比结果：{parts}；{tail}。"
        result.viz = {"chart": "bar", "x": "对比项", "y": metric}
        result.chart_spec = {
            "chart": "bar",
            "x": "对比项",
            "y": metric,
            "echarts": {
                "tooltip": {"trigger": "axis"},
                "legend": {"show": False, "data": [metric]},
                "xAxis": {"type": "category", "name": "对比项", "axisLabel": {"rotate": 0}},
                "yAxis": {"type": "value"},
                "series": [{"name": metric, "type": "bar", "encode": {"x": 0, "y": 1}}],
            },
            "columns": ["对比项", metric],
            "rows": [[label, value] for label, value in rendered],
        }
        return True


class LLMSynthesizer(Synthesizer):
    """LLM 总结：把工具输出喂回 LLM 合成最终洞察（含图表指令）。"""

    def __init__(self, client: Any):
        """绑定 LLM 客户端（适配器或 OpenAI 兼容客户端，经 providers.chat_text 流转）。"""
        self.client = client

    def synthesize(self, result: AgentResult, outputs: list[ToolResult], query: str) -> None:
        """把工具输出喂回 LLM 合成洞察；失败时优雅回退确定性合成（Graceful deterministic fallback）。

        数据字段（dsl/sql/rows/viz/chart_spec）一律先经确定性回填——LLM 只负责
        润色 answer 文本。修复（M1/M2 关联）：此前正常 LLM 路径不回填数据字段，
        会话继承轮（无重规划 preset）的响应缺失 dsl/rows，答案层与数据层断裂。
        """
        if result.answer:
            # R1 重规划作答：规划器已基于完整轨迹给出最终洞察，
            # 这里仅回填数据字段（dsl/sql/rows/viz/chart_spec），不再重复调 LLM
            preset = result.answer
            DeterministicSynthesizer().synthesize(result, outputs, query)
            result.answer = preset
            return
        # 先确定性回填数据字段 + 兜底 answer（空结果话术/标量/对比综合均在此接管）
        DeterministicSynthesizer().synthesize(result, outputs, query)
        if not outputs:
            return
        last = outputs[-1]
        if not last.success:
            return
        # 空结果强制诚实话术（审计修复 D3）：LLM 路径同样禁止在空集上输出
        # "已成功查询"式答复——先于 LLM 调用直接接管作答（比率 0 除外，R4a）
        data = last.data or {}
        last_columns = data.get("columns")
        last_rows = data.get("rows") if isinstance(data.get("rows"), list) else None
        dsl_dict = data.get("dsl") if isinstance(data.get("dsl"), dict) else None
        if "rows" in data:
            has_ratio = bool(
                dsl_dict
                and any(
                    m.get("kind") == "ratio"
                    for m in dsl_dict.get("metrics", [])
                    if isinstance(m, dict)
                )
            )
            if _is_empty_result(last_columns, last_rows, has_ratio_metric=has_ratio):
                result.dsl = QueryDSL.model_validate(dsl_dict) if dsl_dict is not None else None
                result.columns = last_columns
                result.rows = last_rows
                reason = empty_result_reason(result.dsl)
                result.answer = NO_DATA_REPLY.format(reason=reason)
                return
        # 数据上下文注入（审计修复 D6 / T02）：把真实行值/标量喂给总结 LLM，
        # "报数"类问题据此引用具体数值，禁止再答"未能获取具体数量"
        data_context = build_data_context(last_columns, last_rows)
        tools_summary = json.dumps(
            [s.to_dict() for s in result.steps if s.success and s.output],
            ensure_ascii=False,
        )
        messages = [
            {
                "role": "system",
                "content": (
                    "你是数据分析助手。基于工具返回结果，给用户一段简洁的中文洞察。"
                    '输出 JSON：{"answer": "洞察文本"}。\n'
                    "硬性约束：\n"
                    "1. 下方提供的查询结果数值是唯一事实来源：回答报数/统计类问题时"
                    '必须直接引用其中的具体数值，严禁回答"未能获取具体数量/数值"；\n'
                    "2. 严禁虚构任何未出现在查询结果中的数字；\n"
                    '3. 严禁使用"已成功查询"等空洞措辞替代实际数值。'
                ),
            },
            {
                "role": "user",
                "content": (
                    f"问题：{query}\n工具结果：{tools_summary}"
                    + (
                        f"\n\n查询结果数据（唯一事实来源）：\n{data_context}"
                        if data_context
                        else ""
                    )
                ),
            },
        ]
        try:
            raw = chat_text(self.client, messages)
            obj = extract_json(raw)
            result.answer = str(obj.get("answer", ""))
            if obj.get("chart") and result.chart_spec is None:
                result.chart_spec = obj["chart"]
        except Exception:
            DeterministicSynthesizer().synthesize(result, outputs, query)


# --------------------------------------------------------------------------- #
# 反思器（R3）：调度终止后的结果充分性自检
# --------------------------------------------------------------------------- #
@dataclass
class ReflectionVerdict:
    """一次充分性反思的判定结果。

    - ``sufficient``：现有轨迹是否足以回答问题；
    - ``reason``：判定理由（审计与前端展示）；
    - ``follow_up``：判不充分时建议的一次追加调用（仅 LLM 反思器会给出，
      确定性反思器只判定留痕、不生成调用；是否执行由 ToolAgent 按剩余
      预算决定，全程工具执行总数仍受 max_steps 硬约束）。
    """

    sufficient: bool
    reason: str = ""
    follow_up: ToolCall | None = None


class Reflector(ABC):
    """反思器抽象：审计执行轨迹，判定结果充分性（可选择性给出追加调用）。"""

    @abstractmethod
    def reflect(
        self,
        query: str,
        principal: str | None,
        registry: ToolRegistry,
        *,
        steps: list[ToolInvocationRecord],
        outputs: list[ToolResult],
        remaining_steps: int,
    ) -> ReflectionVerdict:
        """审计执行轨迹并判定结果充分性（Judge whether the trajectory suffices to answer）。"""
        ...


class DeterministicReflector(Reflector):
    """确定性充分性自检（零 LLM，离线可用）：只判定与留痕，不生成追加调用。

    判不充分的三类规则（全部可确定性复现）：
    1. 轨迹存在失败步骤；
    2. 数据查询全部成功但无匹配数据（0 行，或聚合空集产出的单行 NULL）；
    3. 对比型问题的数据点不足（单值结果 <2 组且总有效行数 <2，对比维度不完整）。
    """

    @staticmethod
    def _is_no_data(rows: list) -> bool:
        """无数据判定：0 行，或聚合查询在空区间上的单行 NULL（SUM 空集 = [[None]]）。"""
        if not rows:
            return True
        return all(len(r) == 0 or (len(r) == 1 and r[0] is None) for r in rows)

    def reflect(
        self,
        query: str,
        principal: str | None,
        registry: ToolRegistry,
        *,
        steps: list[ToolInvocationRecord],
        outputs: list[ToolResult],
        remaining_steps: int,
    ) -> ReflectionVerdict:
        """零 LLM 充分性判定：失败步骤 / 空数据 / 对比不完整三类规则（Deterministic reflection）。"""
        failed = [s for s in steps if not s.success]
        if failed:
            return ReflectionVerdict(False, f"存在失败步骤：{failed[-1].error_type or 'unknown'}")
        data_rows = [
            (o.data or {}).get("rows") for o in outputs if o.success and isinstance(o.data, dict)
        ]
        data_rows = [r for r in data_rows if isinstance(r, list)]
        if data_rows and all(self._is_no_data(r) for r in data_rows):
            return ReflectionVerdict(False, "查询成功但无匹配数据（结果为空）")
        single_value = [r for r in data_rows if len(r) == 1]
        total_rows = sum(len(r) for r in data_rows)
        if _is_comparative_question(query) and len(single_value) < 2 and total_rows < 2:
            return ReflectionVerdict(False, "对比型问题仅获得一组数据，对比维度不完整")
        return ReflectionVerdict(True, "轨迹完整，结果足以支撑回答")


class LLMReflector(Reflector):
    """LLM 充分性反思：审计轨迹缺口（数据完整性/对比完整性/答非所问），
    判不充分时可给出一次追加工具调用（经注册表校验，非法即丢弃只留痕）。
    """

    def __init__(
        self,
        client: Any,
        registry: ToolRegistry | None = None,
        max_retries: int = 1,
    ):
        """初始化 LLM 反思器（registry 注入后，判不充分时才可能产出追加调用）。"""
        self.client = client
        self.registry = registry  # 注入后追加调用才可能产出（未注入只判定不追加）
        self.max_retries = max_retries

    def reflect(
        self,
        query: str,
        principal: str | None,
        registry: ToolRegistry,
        *,
        steps: list[ToolInvocationRecord],
        outputs: list[ToolResult],
        remaining_steps: int,
    ) -> ReflectionVerdict:
        """LLM 审计轨迹缺口；非法输出带错误反馈重试，重试耗尽抛 PipelineError（LLM-based reflection）。"""
        trajectory = json.dumps(LLMPlanner._trajectory_view(steps, outputs), ensure_ascii=False)
        tools_json = json.dumps(registry.tool_definitions(), ensure_ascii=False)
        messages = [
            {
                "role": "system",
                "content": (
                    "你是数据分析 Agent 的反思器。请审计以下工具执行轨迹，判断"
                    "结果是否足以回答用户问题（数据完整性 / 对比完整性 / 答非所问）。\n"
                    "可用的工具清单（OpenAI Function Calling 规范）：\n"
                    + tools_json
                    + "\n已执行轨迹（含结果摘要）：\n"
                    + trajectory
                    + "\n剩余可用步数："
                    + str(remaining_steps)
                    + "（追加调用必须在预算内）。\n"
                    "输出要求：只输出一个 JSON 对象，二选一：\n"
                    '{ "sufficient": true, "reason": "判定理由" }\n'
                    '{ "sufficient": false, "reason": "缺口说明", "tool": "<工具名>", "args": {...} }\n'
                    "判定充分时禁止携带 tool 字段；禁止输出解释或多余文字。"
                ),
            },
            {"role": "user", "content": f"问题：{query}"},
        ]
        last_error: Exception | None = None
        for _ in range(self.max_retries + 1):
            raw = chat_text(self.client, messages)
            try:
                obj = extract_json(raw)
                sufficient = bool(obj.get("sufficient"))
                reason = str(obj.get("reason", ""))
                if sufficient:
                    return ReflectionVerdict(True, reason)
                follow_up: ToolCall | None = None
                name = str(obj.get("tool", ""))
                if name and self.registry is not None:
                    args = obj.get("args") or {}
                    tool = self.registry.get_tool(name)  # 未注册 -> UnknownToolError
                    tool.validate_args(args)  # 非法参数 -> ValidationError
                    follow_up = ToolCall(name, dict(args), reason="LLM 反思追加")
                return ReflectionVerdict(False, reason, follow_up=follow_up)
            except Exception as exc:
                last_error = exc
                messages = [
                    *messages[:2],
                    {"role": "assistant", "content": raw},
                    {
                        "role": "user",
                        "content": f"你上次的输出无效：{str(exc)[:400]}\n请重新输出合法 JSON。",
                    },
                ]
        raise PipelineError(
            f"LLM 反思器重试 {self.max_retries} 次后仍无法产出合法判定: {last_error}"
        ) from last_error


# --------------------------------------------------------------------------- #
# Agent 调度循环
# --------------------------------------------------------------------------- #
class ToolAgent:
    """Multi-Tool 调度状态循环（Max Steps 受控，杜绝无限循环）。"""

    def __init__(
        self,
        registry: ToolRegistry | None = None,
        planner: Planner | None = None,
        synthesizer: Synthesizer | None = None,
        max_steps: int = 5,
        reflector: Reflector | None = None,
    ) -> None:
        """装配调度内核：max_steps 受控 3~5，reflector=None 表示关闭 R3 反思层（Controlled dispatch loop）。"""
        self.registry = registry or default_registry()
        self.planner = planner or DeterministicPlanner()
        self.synthesizer = synthesizer or DeterministicSynthesizer()
        if not 3 <= max_steps <= 5:
            raise ValueError("max_steps 必须在 3~5 之间（受控调度，杜绝无限循环）")
        self.max_steps = max_steps
        # R3 反思层：None 表示关闭（默认工厂按 AGENT_REFLECTION_ENABLED 装配）
        self.reflector = reflector
        self._history: Any = None
        self._last_dsl: Any = None

    # ------------------------------------------------------------------ #
    def run(
        self,
        query: str,
        principal: str | None = None,
        conn: Any = None,
        *,
        executor: Any = None,
        rewriter: Any = None,
        request_id: str | None = None,
        base_dsl: Any = None,
        history: Any = None,
        last_dsl: Any = None,
    ) -> AgentResult:
        """执行一次完整的多工具调度，返回复合结果 AgentResult（不抛异常）。

        base_dsl：会话上下文继承注入的结构化 DSL（agent.memory 合并产物），
        非 None 时数据工具以其为基础执行，仍走安全守卫 + 编译 + 执行护栏。
        history / last_dsl：本轮会话状态透传给规划器（与 web.service 分流
        共用同一意图判决中心，保证"上下文追问"不被误判为澄清）。
        """
        result = AgentResult(query=query)
        self._history = history
        self._last_dsl = last_dsl

        if base_dsl is not None:
            # 会话继承轮（web 层 resolve_context 已确定性判定 inherit/drilldown）：
            # 复用确定性规划器的关键词分派（趋势 -> trend_analysis，导出 ->
            # export_report，其余 -> query_metric），跳过 LLM 规划的再判定——
            # 既省一次 LLM 调用，也消除下钻轮跨会话漂移（审计修复 M1/M2：LLM
            # 对"那华南呢/按品类展开"偶发误判 clarify，破坏多轮确定性）。
            # 合并 DSL 经 ToolContext.base_dsl 注入，仍走全部安全守卫与护栏。
            ql = query.lower()
            if any(k in ql for k in _TREND_KEYWORDS):
                plan = PlanResult(
                    calls=[ToolCall("trend_analysis", {"query": query}, reason="继承轮趋势分析")]
                )
            elif any(k in ql for k in _EXPORT_KEYWORDS):
                plan = PlanResult(
                    calls=[
                        ToolCall("query_metric", {"query": query}, reason="导出前先查询数据"),
                        ToolCall("export_report", {"query": query}, reason="导出为可下载文件"),
                    ]
                )
            else:
                plan = PlanResult(
                    calls=[
                        ToolCall(
                            "query_metric",
                            {"query": query},
                            reason="会话上下文继承（确定性执行）",
                        )
                    ]
                )
        else:
            try:
                plan = self.planner.plan(
                    query,
                    principal,
                    self.registry,
                    history=self._history,
                    last_dsl=self._last_dsl,
                )
            except Exception as exc:
                result.error = f"{type(exc).__name__}: {exc}"
                result.error_type = type(exc).__name__
                result.answer = result.error
                return result

        if plan.answer is not None:
            result.answer = plan.answer
            # 直接回答：五分类体系无独立意图，保留旧兼容字符串供调用方区分
            result.intent = "direct_answer"
            return result
        if plan.clarifications:
            result.clarifications = [c.to_dict() for c in plan.clarifications]
            result.answer = "；".join(c.question for c in plan.clarifications)
            result.intent = IntentType.CLARIFY.value
            return result

        outputs: list[ToolResult] = []
        replan_answer: str | None = None

        # 首轮计划执行（含失败自愈；True 表示不可恢复失败，终止调度）
        stopped = self._execute_calls(
            calls=plan.calls,
            query=query,
            principal=principal,
            conn=conn,
            executor=executor,
            rewriter=rewriter,
            request_id=request_id,
            base_dsl=base_dsl,
            result=result,
            outputs=outputs,
        )

        # R1 观察驱动重规划：每批执行完毕后把完整轨迹喂回规划器，由其基于
        # 中间结果决定"继续查 / 作答 / 反问 / 终止"。仅 iterative 规划器参与，
        # 全程受 max_steps 硬预算约束，杜绝无限循环。
        # 会话继承轮（base_dsl 非 None）跳过：合并口径已确定性判定，规划器
        # 再判只会引入跨轮漂移（M1/M2）。
        if not stopped and base_dsl is None and self.planner.iterative:
            while (
                len(result.steps) < self.max_steps
                and replan_answer is None
                and not result.clarifications
            ):
                try:
                    nxt = self.planner.plan_next(
                        query,
                        principal,
                        self.registry,
                        steps=result.steps,
                        outputs=outputs,
                        remaining_steps=self.max_steps - len(result.steps),
                    )
                except Exception as exc:
                    # 重规划失败不推翻已成功的执行结果：记录告警后按现有轨迹收敛作答
                    logger.warning(
                        "replan_failed",
                        extra={
                            "event": "replan_failed",
                            "error": f"{type(exc).__name__}: {exc}"[:300],
                        },
                    )
                    break
                if nxt.answer is not None:
                    replan_answer = nxt.answer
                    break
                if nxt.clarifications:
                    result.clarifications = [c.to_dict() for c in nxt.clarifications]
                    replan_answer = "；".join(c.question for c in nxt.clarifications)
                    result.intent = IntentType.CLARIFY.value
                    break
                if not nxt.calls:
                    break  # 规划器判定信息已充分（done）
                result.replans += 1
                stopped = self._execute_calls(
                    calls=nxt.calls,
                    query=query,
                    principal=principal,
                    conn=conn,
                    executor=executor,
                    rewriter=rewriter,
                    request_id=request_id,
                    base_dsl=base_dsl,
                    result=result,
                    outputs=outputs,
                )
                if stopped:
                    break

        # R3 反思层：调度终止后自检结果充分性。仅在预算有余时触发（预算耗尽
        # 说明已全力调度）；重规划已作答时跳过（规划器已做过充分性判断）；
        # 会话继承轮跳过（口径已确定性合并，无信息缺口可补——M1/M2）。
        # 追加查询受同一硬预算约束：全程工具执行总数仍 <= max_steps。
        if (
            self.reflector is not None
            and base_dsl is None
            and result.steps
            and replan_answer is None
            and len(result.steps) < self.max_steps
        ):
            verdict: ReflectionVerdict | None = None
            try:
                verdict = self.reflector.reflect(
                    query,
                    principal,
                    self.registry,
                    steps=result.steps,
                    outputs=outputs,
                    remaining_steps=self.max_steps - len(result.steps),
                )
            except Exception as exc:
                # 反思器自身失效不阻塞已有结果作答：记录告警，无留痕即视为未反思
                logger.warning(
                    "reflect_failed",
                    extra={
                        "event": "reflect_failed",
                        "error": f"{type(exc).__name__}: {exc}"[:300],
                    },
                )
            if verdict is not None:
                result.reflection = {"sufficient": verdict.sufficient, "reason": verdict.reason}
                if (
                    not verdict.sufficient
                    and verdict.follow_up is not None
                    and len(result.steps) < self.max_steps
                ):
                    rec3, res3 = self._execute_once(
                        verdict.follow_up,
                        query,
                        principal,
                        conn,
                        executor,
                        rewriter,
                        request_id,
                        len(result.steps) + 1,
                        outputs,
                        base_dsl,
                    )
                    outputs.append(res3)
                    result.steps.append(rec3)
                    result.reflection["follow_up"] = {
                        "tool": verdict.follow_up.tool,
                        "args": verdict.follow_up.args,
                        "success": rec3.success,
                    }

        if replan_answer is not None:
            # 预置规划器洞察：合成器据此跳过重复 LLM 调用，仅回填数据字段
            result.answer = replan_answer
        self._log_steps(result.steps)

        try:
            self.synthesizer.synthesize(result, outputs, query)
        except Exception as exc:
            result.error = f"{type(exc).__name__}: {exc}"
            result.error_type = type(exc).__name__
        if replan_answer is not None:
            # 规划器基于完整轨迹给出的洞察优先于单输出数据拼装答案
            result.answer = replan_answer

        result.degraded = result.degraded or any((o.meta or {}).get("degraded") for o in outputs)
        return result

    # ------------------------------------------------------------------ #
    def _execute_calls(
        self,
        *,
        calls: list[ToolCall],
        query: str,
        principal: str | None,
        conn: Any,
        executor: Any,
        rewriter: Any,
        request_id: str | None,
        base_dsl: Any,
        result: AgentResult,
        outputs: list[ToolResult],
    ) -> bool:
        """顺序执行一批计划调用（失败触发一次自愈修复）。

        返回 True 表示调度应终止（不可恢复失败）；False 表示本批正常完成
        或因预算耗尽收敛（预算耗尽由调用方的循环条件自然兜住）。
        """
        for call in calls:
            if len(result.steps) >= self.max_steps:
                return False
            record, tool_result = self._execute_once(
                call,
                query,
                principal,
                conn,
                executor,
                rewriter,
                request_id,
                len(result.steps) + 1,
                outputs,
                base_dsl,
            )
            outputs.append(tool_result)
            result.steps.append(record)

            if record.success:
                continue  # 成功 -> 继续下一个计划调用

            # Self-Correction：工具失败时触发一次修复（受 Max Steps 约束）
            if self._is_permanent_error(tool_result) or len(result.steps) >= self.max_steps:
                return True
            try:
                corrected = self.planner.correct(query, principal, call, record)
            except Exception:
                corrected = None
            if corrected is None:
                return True
            rec2, res2 = self._execute_once(
                corrected,
                query,
                principal,
                conn,
                executor,
                rewriter,
                request_id,
                len(result.steps) + 1,
                outputs,
                base_dsl,
            )
            outputs.append(res2)
            result.steps.append(rec2)
            if not rec2.success:
                return True
        return False

    # ------------------------------------------------------------------ #
    def _execute_once(
        self,
        call: ToolCall,
        query: str,
        principal: str | None,
        conn: Any,
        executor: Any,
        rewriter: Any,
        request_id: str | None,
        step_no: int,
        prior_outputs: list[ToolResult],
        base_dsl: Any = None,
    ) -> tuple[ToolInvocationRecord, ToolResult]:
        """执行一次工具调用，返回 (轨迹记录, 工具结果)。"""
        try:
            tool = self.registry.get_tool(call.tool)  # UnknownToolError -> run() 捕获
        except Exception:
            # 工具不存在时返回失败结果，避免向上抛异常（保持run()的"不抛异常"承诺）
            error_msg = f"Unknown tool: {call.tool}"
            error_type = "UnknownToolError"
            tool_result = ToolResult(
                success=False,
                error_msg=error_msg,
                meta={"error_type": error_type},
            )
            record = ToolInvocationRecord(
                step=step_no,
                tool=call.tool,
                args=call.args,
                success=False,
                duration_ms=0.0,
                error_msg=error_msg,
                error_type=error_type,
            )
            return record, tool_result

        ctx = ToolContext(
            conn=conn,
            principal=principal,
            executor=executor,
            rewriter=rewriter,
            request_id=request_id,
            prior=prior_outputs[-1] if prior_outputs else None,
            base_dsl=base_dsl,
        )
        tool_result = tool.run(call.args, ctx)
        # 打点：工具调用成功/失败计入进程级可观测性（audit.metrics）
        try:
            from audit.metrics import default_registry as _metrics_registry

            _metrics_registry().record_tool_call(success=tool_result.success)
        except Exception:  # pragma: no cover - 打点失败不影响主流程
            pass
        record = ToolInvocationRecord(
            step=step_no,
            tool=call.tool,
            args=call.args,
            success=tool_result.success,
            duration_ms=tool_result.duration_ms,
            error_msg=tool_result.error_msg,
            error_type=(tool_result.meta or {}).get("error_type"),
            display_type=tool_result.display_type,
        )
        record.summary = _summarize(tool_result)
        record.output = _summarize(tool_result)
        return record, tool_result

    @staticmethod
    def _is_permanent_error(tool_result: ToolResult) -> bool:
        """越权/未注册等确定性错误不重试（避免无意义的自愈循环）。"""
        error_type = (tool_result.meta or {}).get("error_type")
        return error_type in {
            "SecurityError",
            "UnknownToolError",
            "PermissionError",
        }

    @staticmethod
    def _log_steps(steps: list[ToolInvocationRecord]) -> None:
        for s in steps:
            logger.info(
                "tool_call",
                extra={
                    "event": "tool_call",
                    "tool": s.tool,
                    "success": s.success,
                    "duration_ms": s.duration_ms,
                },
            )


def _summarize(tool_result: ToolResult) -> dict[str, Any] | None:
    """把工具输出压缩为可审计/可展示的摘要（避免把整表数据塞进审计）。"""
    if not tool_result.success:
        return None
    data = tool_result.data
    if isinstance(data, dict):
        summary: dict[str, Any] = {}
        for key in ("row_count", "download_url", "format", "filename", "count", "matched_keys"):
            if key in data:
                summary[key] = data[key]
        if "columns" in data and isinstance(data["columns"], list):
            summary["columns"] = list(data["columns"])
        if "viz" in data:
            summary["chart"] = (data.get("viz") or {}).get("chart")
        return summary
    return {"value": data}


# --------------------------------------------------------------------------- #
# 默认 Agent 工厂
# --------------------------------------------------------------------------- #
_default_agent: ToolAgent | None = None
_agent_lock = threading.Lock()


def default_tool_agent() -> ToolAgent:
    """进程内复用的默认 ToolAgent（LLM 可用 -> LLM 规划 + 总结；否则确定性）。

    LLM 客户端从 Model Provider 网关解析（`resolve_default_client` 返回请求感知
    的分发代理：按请求上下文 provider_id + model_id 动态转发真实协议适配器，
    未绑定请求上下文时回落默认适配器）；反思层（R3）按
    AGENT_REFLECTION_ENABLED 装配——LLM 模式用 LLMReflector（判不充分可追加
    一次受控查询），确定性模式用 DeterministicReflector（只判定留痕）。
    双检锁保护并发首调（就绪度评审 P3 卫生项处置：原工厂无锁且 _agent_lock
    为死变量，并发首调可能重复构造）。
    """
    global _default_agent
    if _default_agent is None:
        with _agent_lock:
            if _default_agent is None:
                registry = default_registry()
                client = resolve_default_client()
                reflector: Reflector | None = None
                if client is not None:
                    planner = LLMPlanner(
                        client, registry=registry, max_retries=settings.LLM_MAX_RETRIES
                    )
                    synthesizer = LLMSynthesizer(client)
                    if settings.AGENT_REFLECTION_ENABLED:
                        reflector = LLMReflector(
                            client, registry=registry, max_retries=settings.LLM_MAX_RETRIES
                        )
                else:
                    planner = DeterministicPlanner()
                    synthesizer = DeterministicSynthesizer()
                    if settings.AGENT_REFLECTION_ENABLED:
                        reflector = DeterministicReflector()
                _default_agent = ToolAgent(
                    registry=registry,
                    planner=planner,
                    synthesizer=synthesizer,
                    max_steps=int(getattr(settings, "MAX_AGENT_STEPS", 5)),
                    reflector=reflector,
                )
    return _default_agent


# 允许测试注入自定义 Agent（与 web.service 现有 monkeypatch 风格一致）
def set_default_tool_agent(agent: ToolAgent | None) -> None:
    """注入 / 重置进程级默认 ToolAgent（供测试替换；None 恢复懒加载装配）。"""
    global _default_agent
    _default_agent = agent


__all__ = [
    "AgentResult",
    "DeterministicPlanner",
    "DeterministicReflector",
    "DeterministicSynthesizer",
    "LLMPlanner",
    "LLMReflector",
    "LLMSynthesizer",
    "PlanResult",
    "Planner",
    "ReflectionVerdict",
    "Reflector",
    "Synthesizer",
    "ToolAgent",
    "ToolCall",
    "ToolInvocationRecord",
    "decompose_comparison",
    "default_tool_agent",
    "set_default_tool_agent",
]
