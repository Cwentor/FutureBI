"""共享的时间工具函数"""

from __future__ import annotations

import re

from config import settings
from semantic.dsl_schema import Comparison, Granularity, TimeFilter, TimeRangeType


def default_compare_window(
    comparison: Comparison | None, granularity: Granularity = Granularity.MONTH
) -> TimeFilter:
    """无窗口时的默认趋势窗口（确定性，锚定 AS_OF_DATE）。

    此函数供 heuristic.py 和 trend_analysis_tool.py 共享使用，
    避免默认窗口常量的重复实现。
    """
    if comparison == Comparison.YOY:
        amount, unit = 12, "month"
    elif comparison == Comparison.MOM:
        amount, unit = 6, "month"
    elif granularity == Granularity.QUARTER:
        amount, unit = 4, "quarter"
    elif granularity == Granularity.MONTH:
        amount, unit = 6, "month"
    elif granularity == Granularity.WEEK:
        amount, unit = 12, "week"
    else:
        amount, unit = 30, "day"
    return TimeFilter.model_validate(
        {
            "granularity": granularity.value,
            "range_type": TimeRangeType.RELATIVE.value,
            "relative": {"amount": amount, "unit": unit, "mode": "trailing"},
            "comparison": comparison.value if comparison else Comparison.NONE.value,
            "reference_date": settings.AS_OF_DATE.isoformat(),
        }
    )


def parse_explicit_time_window(query: str) -> tuple[str, str] | None:
    """从问题中解析显式绝对时间窗口（"YYYY年M月" / "YYYY年"），返回 [start, end) ISO 日期对。

    与 agent/heuristic.py 绝对时间分支同一实现（单一来源），供编排链路的
    确定性兜底 DSL 复用——用户显式指定的年份/月份必须被尊重，严禁兜底窗口
    静默替换用户时间（否则会用域内数据冒充用户问的时段，构成数据造假）。
    未显式给出年份的月份（如 "6月GMV"）返回 None，由调用方锚定缺省窗口。
    """
    m = re.search(r"(\d{4})\s*年\s*(\d{1,2})\s*月", query)
    if m:
        year, month = int(m.group(1)), int(m.group(2))
        end_year, end_month = (year + 1, 1) if month == 12 else (year, month + 1)
        return (f"{year:04d}-{month:02d}-01", f"{end_year:04d}-{end_month:02d}-01")
    m = re.search(r"(\d{4})\s*年", query)
    if m:
        year = int(m.group(1))
        return (f"{year:04d}-01-01", f"{year + 1:04d}-01-01")
    return None


def time_window_outside_domain(tf: TimeFilter) -> bool:
    """时间窗口是否整体晚于数仓数据域上界（必然空集，确定性可证）。

    编排链路与单轮 chat 链路（``empty_result_reason``）共用的诚实守卫判据：
    窗口起点晚于 ``settings.DATA_DOMAIN_END`` 即判定超界。此类查询严禁继续
    因子分解 / 维度下钻 / 综合报告，必须如实告知用户该时段无数据。
    """
    from compiler.sql_compiler import resolve_time_window

    start, _end = resolve_time_window(tf)
    return start.date() > settings.DATA_DOMAIN_END
