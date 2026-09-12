"""NL -> DSL 语义一致性校验与区域词/时间锚规范化。"""

from __future__ import annotations

import json

from agent.errors import PipelineError
from agent.heuristic import DeterministicNL2DSL, region_provinces
from semantic import catalog
from semantic.dsl_schema import Filter, FilterOperator, QueryDSL, TimeRangeType

_H = DeterministicNL2DSL()


def expand_region_filters(dsl: QueryDSL) -> QueryDSL:
    """展开 province 过滤中的区域词（审计修复 M1：区域词口径）。

    LLM 产出 ``province = '华东'`` 属错误口径（华东不在成员词汇表，必然空集）：
    检测到区域词即确定性展开为 ``province IN (数仓实际存在的省份列表)``，
    展开值域经 region_provinces() 与语义目录成员词汇表求交，绝不产出
    ``IN ('无效省')`` 空过滤。非区域词过滤原样透传。
    """
    region_words = set(catalog.REGION_PROVINCE_MAPPING)
    changed = False
    new_filters: list[Filter] = []
    for f in dsl.filters:
        if f.field == "province" and f.operator in (FilterOperator.EQ, FilterOperator.IN):
            values = f.value if isinstance(f.value, list) else [f.value]
            hit_regions = [v for v in values if isinstance(v, str) and v in region_words]
            if hit_regions:
                provinces: list[str] = []
                for region in hit_regions:
                    for p in region_provinces(region):
                        if p not in provinces:
                            provinces.append(p)
                if provinces:
                    new_filters.append(
                        Filter(field="province", operator=FilterOperator.IN, value=provinces)
                    )
                    changed = True
                    continue
        new_filters.append(f)
    if not changed:
        return dsl
    return dsl.model_copy(update={"filters": new_filters})


def normalize_time_anchor(dsl: QueryDSL) -> QueryDSL:
    """相对时间窗口补齐 reference_date 锚点（缺省锚 AS_OF_DATE）。

    编译语义等价（compiler 在 reference_date 为 None 时回退 config.AS_OF_DATE），
    此处做确定性结构规范化：相对窗口一律显式携带锚点日期，使 LLM 生成 DSL 与
    golden 契约逐字段可比（oracle 评测 M1/M2 多轮结构断言消除噪声）。
    """
    tf = dsl.time_filter
    if tf is not None and tf.range_type == TimeRangeType.RELATIVE and tf.reference_date is None:
        from config import settings

        return dsl.model_copy(
            update={"time_filter": tf.model_copy(update={"reference_date": settings.AS_OF_DATE})}
        )
    return dsl


def align_time_granularity(dsl: QueryDSL, query: str) -> QueryDSL:
    """时间粒度确定性对齐（口径漂移修复）。

    时间粒度必须由问句语义决定，而非由契约默认值兜底。DSL 契约的
    ``granularity`` 默认值恰是 ``day``，LLM 漏写该字段时聚合区间（如"上个月"）
    会被切成日粒度，与启发式口径不一致导致多轮评测偶发漂移（同一问句 5 次
    独立调用中出现 1 次 day）。

    规则：仅当启发式能从问句确定性解析出粒度、且与 LLM 产出的粒度不同、且
    两者的时间段与单位一致（同一时间窗口的粒度表达分歧）时，采用启发式粒度；
    时间窗口本身不一致时不动（属真实语义差异，交由语义校验与自愈处理）。
    """
    tf = dsl.time_filter
    if tf is None:
        return dsl
    try:
        expected = _H.run(query, principal=None)
    except PipelineError:
        return dsl
    etf = expected.time_filter
    if etf is None or etf.granularity == tf.granularity:
        return dsl
    # 只在"同一时间窗口"上对齐粒度：区间类型/单位/总量/锚点需一致
    if etf.range_type != tf.range_type:
        return dsl
    if etf.range_type == TimeRangeType.RELATIVE:
        e_rel, a_rel = etf.relative, tf.relative
        if e_rel is None or a_rel is None:
            return dsl
        same_window = (e_rel.unit, e_rel.amount, e_rel.mode) == (
            a_rel.unit,
            a_rel.amount,
            a_rel.mode,
        )
    else:
        same_window = etf.absolute == tf.absolute
    if not same_window:
        return dsl
    return dsl.model_copy(
        update={"time_filter": tf.model_copy(update={"granularity": etf.granularity})}
    )


def validate_semantics(query: str, dsl: QueryDSL, principal: str | None = None) -> list[str]:
    """校验可由确定性规则确认的关键语义槽位。

    只检查“问题明确要求、DSL 却缺失”的槽位；不要求 LLM 复刻启发式的所有默认值。
    规则无法可靠解析的问题返回空列表，继续由 LLM 自行处理。
    """
    try:
        expected = _H.run(query, principal=principal)
    except PipelineError:
        return []

    errors: list[str] = []
    actual_metrics = {(m.field, m.agg.value) for m in dsl.metrics if m.kind == "aggregate"}
    expected_metrics = {(m.field, m.agg.value) for m in expected.metrics if m.kind == "aggregate"}
    if expected_metrics and not expected_metrics <= actual_metrics:
        errors.append(f"指标缺失：期望包含 {sorted(expected_metrics - actual_metrics)}")

    actual_dims = {d.field for d in dsl.dimensions}
    expected_dims = {d.field for d in expected.dimensions}
    if expected_dims - actual_dims:
        errors.append(f"分组维度缺失：期望包含 {sorted(expected_dims - actual_dims)}")

    actual_order = {(o.field, o.direction.value) for o in dsl.order_by}
    expected_order = {(o.field, o.direction.value) for o in expected.order_by}
    if expected_order - actual_order:
        errors.append(f"排序缺失：期望包含 {sorted(expected_order - actual_order)}")

    # 过滤条件一致性（审计修复 T02）："成功/成交"等明确口径词被启发式识别为
    # pay_status 过滤、区域/品类词被识别为维度过滤时，LLM DSL 必须包含同义过滤，
    # 否则口径漂移（如漏掉 SUCCESS 过滤导致去重用户数 123 != 139）。
    def _filter_sig(f) -> str:
        value = json.dumps(f.value, ensure_ascii=False, sort_keys=True)
        return f"{f.field}|{f.operator.value}|{value}"

    expected_filters = {_filter_sig(f) for f in expected.filters}
    actual_filters = {_filter_sig(f) for f in dsl.filters}
    if expected_filters - actual_filters:
        errors.append(
            "过滤条件缺失：期望包含 "
            + str(sorted(expected_filters - actual_filters))
            + "（问题中的口径词如'成功/成交'必须落到对应过滤条件）"
        )

    if expected.limit == 1 and dsl.limit != 1:
        errors.append(f"返回条数错误：问题要求单个结果，实际 limit={dsl.limit}")

    return errors


__all__ = ["expand_region_filters", "normalize_time_anchor", "validate_semantics"]
