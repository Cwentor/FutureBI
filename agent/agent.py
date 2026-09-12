"""LLM 驱动的 NL -> DSL Agent。

流程（零幻觉约束）：
1. 调用 LLM，要求只输出 QueryDSL JSON；
2. 从输出中提取 JSON（容忍少量 Markdown 代码块包裹）；
3. QueryDSL.model_validate 严格校验 —— 任何未知字段/非法枚举直接判失败；
4. 校验失败时把错误反馈给 LLM 重试（最多 max_retries 次）；
5. 重试耗尽仍失败 -> 抛 PipelineError（拒绝，绝不猜测）。

该实现不直接接触 SQL：DSL 产出后交给 compiler 编译，从机制上杜绝注入。
"""

from __future__ import annotations

import json
import re
from typing import Any

from pydantic import ValidationError

from agent.errors import PipelineError
from agent.prompts import build_fix_messages, build_messages, build_rewrite_messages
from agent.semantic_check import (
    align_time_granularity,
    expand_region_filters,
    normalize_time_anchor,
    validate_semantics,
)
from providers import chat_text
from semantic.dsl_schema import QueryDSL

BT = chr(96)  # backtick
FENCE = BT * 3


def extract_json(text: str) -> dict[str, Any]:
    """从 LLM 文本中提取 JSON 对象。

    优先直接解析；失败则尝试剥离 Markdown 代码块围栏后再解析。
    """
    text = text.strip()
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            return obj
        raise ValueError("顶层不是 JSON 对象")
    except json.JSONDecodeError:
        pass

    fence = re.search(FENCE + r"(?:json)?\s*(.*?)\s*" + FENCE, text, re.DOTALL)
    if fence:
        return json.loads(fence.group(1))

    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        return json.loads(text[start : end + 1])
    raise ValueError("输出中未找到 JSON 对象")


class LLMNL2DSL:
    """基于 LLM 的 NL -> DSL Agent。

    client 兼容两种形态：Model Provider 适配器（``BaseAdapter``，走统一
    chat_text / JSON Mode 抹平）或旧形态 OpenAI 兼容客户端（``chat(messages)``）。
    """

    def __init__(self, client: Any, max_retries: int = 2) -> None:
        """绑定 LLM 客户端与重试上限（Bind the client and retry budget）。"""
        self.client = client
        self.max_retries = max_retries

    def run(self, query: str, principal: str | None = None) -> QueryDSL:
        """生成 QueryDSL。

        principal 非 None 时，Prompt 注入按主体过滤的字段白名单（守卫前移）：
        越权字段根本不进入模型视野；生成结果仍由调用方施加 apply_policy 纵深防御。
        """
        last_error: Exception | None = None
        messages = build_messages(query, principal)
        for _ in range(self.max_retries + 1):
            raw = chat_text(self.client, messages)
            try:
                obj = extract_json(raw)
                if "error" in obj and obj.get("error"):
                    raise PipelineError("LLM 拒绝解析: " + str(obj["error"]))
                candidate = QueryDSL.model_validate(obj)
                # 确定性规范化（审计修复 M1/M2）：区域词展开 + 相对时间锚点补齐 +
                # 时间粒度对齐（契约默认 day 会切坏"上个月"等区间口径）
                candidate = align_time_granularity(
                    normalize_time_anchor(expand_region_filters(candidate)), query
                )
                semantic_errors = validate_semantics(query, candidate, principal)
                if semantic_errors:
                    raise PipelineError("语义校验失败：" + "；".join(semantic_errors))
                return candidate
            except (ValueError, TypeError, KeyError, ValidationError, PipelineError) as exc:
                last_error = exc
                messages = build_fix_messages(query, raw, str(exc)[:400], principal)
        raise PipelineError(
            "LLM 重试 " + str(self.max_retries) + " 次后仍无法产出合法 DSL: " + str(last_error)
        ) from last_error

    def rewrite(
        self,
        query: str,
        dsl: QueryDSL,
        error: str,
        attempts: int = 1,
        principal: str | None = None,
    ) -> QueryDSL:
        """SQL 执行自愈：把精确的编译/引擎报错喂回 LLM，重写 DSL。

        至少调用一次 LLM（attempts >= 1）并附上 error 上下文；重写结果同样经过
        严格校验，失败时继续反馈校验错误重试。全部失败抛 PipelineError。
        重写 Prompt 同样按主体过滤字段白名单（守卫前移）。
        """
        last_error: Exception | None = None
        messages = build_rewrite_messages(query, dsl.model_dump(mode="json"), error, principal)
        for _ in range(max(attempts, 1)):
            raw = chat_text(self.client, messages)
            try:
                obj = extract_json(raw)
                if "error" in obj and obj.get("error"):
                    raise PipelineError("LLM 拒绝解析: " + str(obj["error"]))
                # 自愈重写同样做规范化（与首跑口径一致：区域词展开 + 时间锚补齐 + 粒度对齐）
                return align_time_granularity(
                    normalize_time_anchor(expand_region_filters(QueryDSL.model_validate(obj))),
                    query,
                )
            except (ValueError, TypeError, KeyError, ValidationError, PipelineError) as exc:
                last_error = exc
                messages = build_fix_messages(query, raw, str(exc)[:400], principal)
        raise PipelineError(
            "LLM 重写 DSL 失败（尝试 " + str(max(attempts, 1)) + " 次）: " + str(last_error)
        ) from last_error
