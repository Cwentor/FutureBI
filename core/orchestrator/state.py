"""编排状态契约（AgentState）：图式编排器的事实来源。

需求 §2.A 状态字段全覆盖：
- 身份：session_id / turn_id / trace_id；
- 任务：user_query、plan_steps（含依赖的任务列表）、clarification；
- 执行：tool_calls / tool_results（带 token 预算修剪的轨迹）、scratchpad
  （假设 / 检验 / 中间推理）、artifacts（ParquetRef / summary / ECharts）；
- 韧性：error_context（自愈栈 + 重试计数，上限 MAX_RETRIES=3）；
- 控制：phase（图路由信号）、human_reply（HITL 恢复载荷）。

全部模型 extra="forbid"：编排层自身的演进受契约约束，与项目纪律一致。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

# 自愈重试上限（需求：max 3 retries）
MAX_RETRIES = 3

# 工具轨迹 token 预算（估算：1 token ≈ 4 字符）
TOOL_HISTORY_BUDGET_CHARS = 60_000

Phase = Literal[
    "clarify",  # 需要澄清（HITL 中断）
    "plan",  # 规划 / 重规划
    "query",  # DSL 取数
    "analyze",  # 沙箱分析
    "critique",  # 反思
    "synthesize",  # 综合报告
    "done",  # 终态
]


class PlanStep(BaseModel):
    """计划步骤：目标 + 类型 + 依赖（DAG）。"""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(..., description="步骤标识（plan 内唯一，如 s1/s2）")
    goal: str = Field(..., description="该步骤要回答/完成的子目标")
    kind: Literal["query", "analyze", "synthesize"] = Field(
        ..., description="步骤类型：取数 / 沙箱分析 / 综合"
    )
    depends_on: list[str] = Field(default_factory=list, description="前置步骤 id 列表")
    # 取数步骤的 DSL 草稿（query 类型时由 Planner 给出；analyze 可空）
    dsl: dict[str, Any] | None = None
    # 分析步骤的代码草稿 / 技能调用（analyze 类型时给出）
    code: str | None = None
    status: Literal["pending", "running", "done", "failed"] = "pending"


class ToolRecord(BaseModel):
    """一次工具调用记录（轨迹与审计的最小单元）。"""

    model_config = ConfigDict(extra="forbid")

    step_id: str = ""
    tool: str = Field(..., description="工具名（execute_dsl_query / run_code / skill:*）")
    args: dict[str, Any] = Field(default_factory=dict)
    ok: bool = True
    # 结果摘要（字符串化，供 LLM 上下文与 token 预算修剪）
    summary: str = ""
    duration_ms: float = 0.0
    error: str | None = None


class Artifact(BaseModel):
    """产物：数据集 / 统计摘要 / 图表规格。"""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["parquet", "summary", "echarts"]
    name: str
    payload: dict[str, Any] = Field(default_factory=dict)


class ErrorContext(BaseModel):
    """自愈上下文：结构化错误栈 + 重试计数。"""

    model_config = ConfigDict(extra="forbid")

    errors: list[str] = Field(default_factory=list, description="按序追加的错误摘要")
    retries: int = Field(default=0, ge=0, le=MAX_RETRIES)

    def record(self, error: str) -> bool:
        """记录一次错误；超过重试上限返回 False（编排层据此终止）。"""
        self.errors.append(error[-2000:])
        self.retries += 1
        return self.retries <= MAX_RETRIES


class AgentState(BaseModel):
    """编排器全局状态（跨节点共享，图引擎不可变传递、节点返回增量）。"""

    model_config = ConfigDict(extra="forbid")

    # 身份
    session_id: str = "default"
    turn_id: str = "t1"
    trace_id: str = "tr1"
    # 任务
    user_query: str = ""
    plan_steps: list[PlanStep] = Field(default_factory=list)
    clarification: str | None = Field(default=None, description="向用户发出的澄清问题")
    human_reply: str | None = Field(default=None, description="HITL 恢复时的用户答复")
    # 执行轨迹
    tool_calls: list[ToolRecord] = Field(default_factory=list)
    scratchpad: list[str] = Field(default_factory=list, description="中间推理/假设/检验记录")
    artifacts: list[Artifact] = Field(default_factory=list)
    # 报告
    report: str = ""
    # 控制与韧性
    phase: Phase = "plan"
    error_context: ErrorContext = Field(default_factory=ErrorContext)
    # 数据集名 -> ParquetRef（编排器内传递，不进 LLM prompt）
    datasets: dict[str, dict[str, Any]] = Field(default_factory=dict)
    # 计划步骤 id -> 该步骤产出的数据集名列表（重规划时按同 id 覆盖）。
    # analyze 步骤据此解析本轮依赖的真实输入——严禁按 datasets 字典首尾
    # 取数：跨轮次累积时首尾会指向上（几）轮遗留数据集，产出假结论。
    step_outputs: dict[str, list[str]] = Field(default_factory=dict)
    # 上次因 LLM 反思触发重规划时的产物进展指纹（重规划无进展护栏）：
    # 指纹不变说明重规划未带来任何新数据/新分析，必须停止空转。
    last_replan_fingerprint: str = ""
    # 无数据诚实守卫（2026-09 审计修复）：取数执行前的时间域守卫拦截原因
    # （查询窗口整体晚于数仓数据域上界 = 必然空集）。置位后 analyze/critic
    # 直接短路到 synthesize 的"如实说明无数据"报告——严禁重规划空转、
    # 严禁拿兜底窗口数据冒充用户指定时段、严禁对空集编造归因结论。
    no_data_reason: str | None = Field(
        default=None, description="时间域守卫拦截原因（无数据诚实报告）"
    )
    iteration: int = Field(default=0, ge=0, description="图迭代步数（防死循环护栏）")

    def apply(self, **updates: Any) -> AgentState:
        """不可变更新：返回应用增量后的新状态（图引擎的节点返回语义）。"""
        return self.model_copy(update=updates, deep=True)

    def prune_tool_history(self, budget_chars: int = TOOL_HISTORY_BUDGET_CHARS) -> None:
        """token 预算修剪：保留最近轨迹，超预算从最旧开始丢弃（原地）。"""
        total = sum(len(r.summary) + len(r.error or "") for r in self.tool_calls)
        while total > budget_chars and len(self.tool_calls) > 1:
            removed = self.tool_calls.pop(0)
            total -= len(removed.summary) + len(removed.error or "")


def estimate_tokens(state: AgentState) -> int:
    """粗估状态的可注入字符量（编排层预算决策用）。"""
    text = (
        state.user_query
        + state.report
        + "".join(state.scratchpad)
        + "".join(r.summary for r in state.tool_calls)
    )
    return len(text) // 4
