"""确定性启发式 NL2DSL（离线兜底实现）。

不依赖 LLM，用规则把 golden 覆盖的高频问法映射为 QueryDSL。用途：
1. 无 API Key 时让整条链路离线可运行、可单测、可评测；
2. 作为 LLM 输出的独立对照（cross-check）。

超出规则范围的提问会抛 PipelineError（拒绝而非猜测），与 LLM 路径行为一致。

覆盖的语义能力：
- 单/多指标聚合（GMV、订单数、去重用户、客单价、退款金额）；
- 比率指标（退款率、ARPU）；
- 同比/环比（comparison）；
- 窗口函数（累计 cumsum / 移动平均 moving_avg）；
- 日期连续补零（fill_gaps）；
- 分组 Top-N（每省/每品牌/每品类 Top N）；
- 维度基数探查（"有几个地区" -> count_distinct 自动兜底）。
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any

from agent.clarify import undefined_metric_terms
from agent.errors import PipelineError
from agent.time_utils import parse_explicit_time_window
from config import settings
from security.errors import SecurityError
from security.scope import scoped_fields
from semantic import catalog
from semantic.dsl_schema import Comparison, Granularity, QueryDSL, RatioMetric, WindowMetric

# 大区 -> 省份映射单一事实来源已迁至 semantic.catalog.REGION_PROVINCE_MAPPING
# （审计修复 M1：区域词展开的口径归口语义目录）。此别名保持既有导入路径兼容。
REGIONS: dict[str, list[str]] = {
    region: list(provinces) for region, provinces in catalog.REGION_PROVINCE_MAPPING.items()
}


def dimension_members(field: str) -> tuple[str, ...]:
    """维度成员词汇表：语义目录数据驱动，catalog_loader 启动时从数仓 distinct 重建。

    审计 §3.2-4：原 PROVINCES/CATEGORIES 模块常量与数仓硬绑定，新增维度成员后
    LLM 路径可用、离线路径失明；改为每次动态读取 catalog.DIMENSION_MEMBERS，
    与编译器消费 catalog.COLUMNS 同模式，目录刷新即时生效。
    """
    return catalog.DIMENSION_MEMBERS.get(field, ())


def region_provinces(region: str) -> list[str]:
    """大区 -> 数仓实际存在的省份列表（行政区划映射 ∩ 成员词汇表）。"""
    members = set(dimension_members("province"))
    return [p for p in REGIONS.get(region, ()) if p in members]


def _quarter_start(d: date) -> date:
    """给定日期所在季度的第一天。"""
    q = (d.month - 1) // 3
    return date(d.year, q * 3 + 1, 1)


def _current_quarter_bounds(ref: date) -> tuple[date, date]:
    """当前自然季度半开区间 [季初, 下季初)。"""
    start = _quarter_start(ref)
    y, m = (start.year + 1, 1) if start.month == 10 else (start.year, start.month + 3)
    return start, date(y, m, 1)


class DeterministicNL2DSL:
    """关键词规则版的 NL -> DSL（覆盖 golden 高频场景）。"""

    def rewrite(self, query: str, dsl: QueryDSL, error: str, attempts: int = 1) -> QueryDSL:
        """确定性兜底无自愈能力：执行/编译失败无法用规则可靠修正，拒绝而非猜测。

        保持与 LLM 路径一致的接口形态，但明确抛错，让上层自愈循环知难而退，
        并把原始引擎报错透传给用户。
        """
        raise PipelineError("确定性兜底不支持 SQL 自愈重写（未配置 LLM）：" + str(error))

    def run(self, query: str, principal: str | None = None) -> QueryDSL:
        """关键词规则 -> QueryDSL（守卫前移：生成完成前按主体过滤字段作用域）。"""
        q = query.strip()
        # 禁止静默回退默认值：未定义业务指标（如"高活用户"/"高活跃用户"）宁可拒绝，
        # 也不近似映射为已有指标（如"活跃用户"）。路由层负责主动反问，此处兜底拒绝。
        undefined = undefined_metric_terms(q)
        if undefined:
            raise PipelineError(
                "检测到未定义业务指标（" + "、".join(undefined) + "），"
                "请先补充其业务口径，无法可靠解析。"
            )
        try:
            top_n = self._top_n(q)
            dims = self._dimensions(q)
            # 分组 Top-N：分区维度排在最前，其余为排名维度
            if top_n:
                partition = set(top_n["partition_by"])
                rank_dims = [d for d in dims if d["field"] not in partition]
                dims = [{"field": p} for p in top_n["partition_by"]] + rank_dims

            # 维度基数/枚举探查：统一修正分组维度
            # - count 型（"有几个地区/多少品类"）：唯一指标即维度 count_distinct 兜底，
            #   不分组（提示词要求）
            # - 枚举型（"有哪些地区/所有品牌"）：除 count_distinct 指标外，将维度字段
            #   加入 dimensions 以输出成员去重枚举
            probe_field = self._enum_dimension_field(q)
            metrics = self._metrics(q)
            count_probe = (
                metrics[0]
                if (
                    len(metrics) == 1
                    and isinstance(metrics[0], dict)
                    and metrics[0].get("kind") == "aggregate"
                    and metrics[0].get("agg") == "count_distinct"
                    and probe_field is not None
                    and metrics[0].get("field") == probe_field
                )
                else None
            )
            if probe_field is not None:
                if self._is_dim_enum_form(q):
                    # 枚举型（"有哪些地区/所有品牌"）：确保维度字段在分组中以去重枚举
                    if probe_field not in {d["field"] for d in dims}:
                        dims.append({"field": probe_field})
                elif count_probe is not None:
                    # count 型（"有几个地区/多少品类"）：唯一指标即维度 count_distinct
                    # 兜底，不分组（提示词要求）
                    dims = [d for d in dims if d["field"] != probe_field]

            dsl: dict[str, Any] = {
                "metrics": metrics,
                "dimensions": dims,
                "filters": self._filters(q),
                "order_by": self._order_by(q),
                "limit": self._limit(q),
            }
            time_filter = self._time_filter(q)
            if time_filter:
                dsl["time_filter"] = time_filter
            comparison = self._comparison(q)
            if comparison:
                # 有对比意图但无时间窗口时，补一个确定性默认窗口（禁止 KeyError / 静默丢弃）
                if "time_filter" not in dsl:
                    # 导入共享的默认窗口函数以避免重复实现
                    from agent.time_utils import default_compare_window

                    # heuristic.py 只需要基于 comparison 的简单窗口
                    # 为了保持与原有行为一致，我们只使用 comparison 部分
                    time_filter = default_compare_window(
                        comparison=(
                            Comparison.YOY
                            if comparison == "yoy"
                            else Comparison.MOM if comparison == "mom" else None
                        ),
                        granularity=Granularity.MONTH,  # heuristic.py 默认使用 month 粒度
                    )
                    dsl["time_filter"] = time_filter.model_dump()
                dsl["time_filter"]["comparison"] = comparison
            # 时间主轴解绑（报告缺陷修复）：按问句语义设置 time_field
            # （退款时序用 refund_time；其余默认 order_time）
            if "time_filter" in dsl:
                dsl["time_filter"]["time_field"] = self._time_dim_field(q)
            if self._fill_gaps(q):
                dsl["fill_gaps"] = True
            if top_n:
                dsl["top_n"] = top_n
            parsed = QueryDSL.model_validate(dsl)
        except PipelineError:
            raise
        except SecurityError:
            raise
        except Exception as exc:
            raise PipelineError(f"无法解析提问: {query!r} ({exc})") from exc

        # 守卫前移：生成完成前按主体过滤可用字段；越权字段直接拒绝（SecurityError）。
        self._enforce_scope(parsed, principal)
        return parsed

    @staticmethod
    def _enforce_scope(dsl: QueryDSL, principal: str | None) -> None:
        """校验 DSL 引用的字段全部在主体作用域内；越权字段抛 SecurityError。

        在确定性路径中，这就是"生成前"过滤：候选 DSL 尚未提交/编译即被拒绝，
        而不是生成后靠守卫兜底（apply_policy 仍作为第二道纵深防御）。
        """
        allowed = scoped_fields(principal)
        referenced: set[str] = set()
        for m in dsl.metrics:
            if isinstance(m, RatioMetric):
                referenced.add(m.numerator.field)
                referenced.add(m.denominator.field)
            elif isinstance(m, WindowMetric):
                referenced.add(m.base.field)
            else:
                referenced.add(m.field)
        for d in dsl.dimensions:
            referenced.add(d.field)
        for f in dsl.filters:
            referenced.add(f.field)
        forbidden = referenced - allowed
        if forbidden:
            raise SecurityError(f"主体 {principal!r} 无权访问字段: {sorted(forbidden)}")

    # ------------------------------------------------------------------ #
    # 指标
    # ------------------------------------------------------------------ #
    def _metrics(self, q: str) -> list[dict[str, Any]]:
        ql = q.lower()

        # 窗口指标（累计/移动平均）优先
        wm = self._window_metric(q)
        if wm is not None:
            return [wm]

        metrics: list[dict[str, Any]] = []

        # 比率指标：退款率（早退，独占）
        if "退款率" in q or "退款金额/订单金额" in q:
            return [
                {
                    "kind": "ratio",
                    "numerator": {
                        "kind": "aggregate",
                        "field": "refund_amount",
                        "agg": "sum",
                        "alias": "refund_amount",
                    },
                    "denominator": {
                        "kind": "aggregate",
                        "field": "order_amount",
                        "agg": "sum",
                        "alias": "gmv",
                    },
                    "alias": "refund_rate",
                }
            ]
        if "退款金额" in q or "退款总额" in q:
            metrics.append(
                {
                    "kind": "aggregate",
                    "field": "refund_amount",
                    "agg": "sum",
                    "alias": "refund_amount",
                }
            )
        if "arpu" in ql or "人均消费" in q:
            metrics.append(
                {
                    "kind": "ratio",
                    "numerator": {
                        "kind": "aggregate",
                        "field": "order_amount",
                        "agg": "sum",
                        "alias": "gmv",
                    },
                    "denominator": {
                        "kind": "aggregate",
                        "field": "user_id",
                        "agg": "count_distinct",
                        "alias": "active_users",
                    },
                    "alias": "arpu",
                }
            )
        if any(k in ql for k in ("gmv", "销售额", "销售总额", "成交额", "成交金额", "总销售")):
            metrics.append(
                {"kind": "aggregate", "field": "order_amount", "agg": "sum", "alias": "gmv"}
            )
        if any(k in q for k in ("去重用户", "活跃用户")):
            metrics.append(
                {
                    "kind": "aggregate",
                    "field": "user_id",
                    "agg": "count_distinct",
                    "alias": "active_users",
                }
            )
        if any(k in q for k in ("订单总数", "订单数", "订单量")):
            metrics.append(
                {"kind": "aggregate", "field": "order_id", "agg": "count", "alias": "order_count"}
            )
        if "客单价" in q:
            metrics.append(
                {
                    "kind": "aggregate",
                    "field": "order_amount",
                    "agg": "avg",
                    "alias": "avg_order_amount",
                }
            )

        if not metrics:
            # 明细/清单类问题（如"未履约订单明细"）：退化为订单计数 + 明细维度
            if any(k in q for k in ("明细", "清单")):
                metrics.append(
                    {
                        "kind": "aggregate",
                        "field": "order_id",
                        "agg": "count",
                        "alias": "order_count",
                    }
                )
            else:
                # 数量提问 -> COUNT/COUNT_DISTINCT 度量映射（"有多少订单/几个用户/多少商品"），
                # 优先于维度基数兜底：这类问法命中"多少/几个 + 实体"即视为全新计数度量。
                count_entity = self._count_entity_metric(q)
                if count_entity is not None:
                    metrics.append(count_entity)
                elif self._dim_count_fallback(q) is not None:
                    # 维度基数探查：自动将维度字段映射为 count_distinct 指标
                    metrics.append(self._dim_count_fallback(q))
                else:
                    raise PipelineError("无法识别指标（需要 GMV/订单数/去重用户/ARPU/客单价 之一）")
        return metrics

    def _count_entity_metric(self, q: str) -> dict[str, Any] | None:
        """'数量提问 -> COUNT/COUNT_DISTINCT' 度量映射。

        当问句命中"多少/几个 [订单|单|笔]"、"多少 [用户/客户/人]"、"多少 [商品/产品]"等
        数量式提问时，返回对应主键的计数度量：订单 -> COUNT(order_id)，
        用户/客户 -> COUNT(DISTINCT user_id)，商品/产品 -> COUNT(DISTINCT product_id)。

        语义定位：这类问法代表**全新计数指标**而非对上一轮指标的微调继承，故与
        memory._has_metric_term 同源——命中即把多轮判定推向 topic_switch（RESET），
        从根源上阻断"仅凭时间词就沿用上轮 SUM(order_amount)/gmv"的贪婪判定。
        """
        # 触发数量语气词（不取裸"几"，避免误伤）
        if not any(w in q for w in ("多少", "几个", "多少个", "几笔", "几单")):
            return None
        if any(k in q for k in ("订单", "单子", "笔", "单")):
            return {
                "kind": "aggregate",
                "field": "order_id",
                "agg": "count",
                "alias": "order_count",
            }
        if any(k in q for k in ("用户", "客户", "人")) and "user_id" in catalog.COLUMNS:
            return {
                "kind": "aggregate",
                "field": "user_id",
                "agg": "count_distinct",
                "alias": "active_users",
            }
        if any(k in q for k in ("商品", "产品")) and "product_id" in catalog.COLUMNS:
            return {
                "kind": "aggregate",
                "field": "product_id",
                "agg": "count_distinct",
                "alias": "product_count",
            }
        return None

    def _dim_count_fallback(self, q: str) -> dict[str, Any] | None:
        """维度基数查询兜底：将维度字段映射为 count_distinct 聚合指标。

        当用户询问"有几个 [维度]""[维度]数量"等无明确指标的基数问题时，
        自动生成针对该维度的 count_distinct 指标，无需强制用户指定业务度量。
        alias 必须符合 DSL 契约的英文字母数字标识符白名单（IDENTIFIER_PATTERN）。
        """
        for keywords, field, alias in self._DIM_KEYWORDS:
            if any(k in q for k in keywords):
                # 确认字段在语义目录中注册
                if field in catalog.COLUMNS:
                    return {
                        "kind": "aggregate",
                        "field": field,
                        "agg": "count_distinct",
                        "alias": alias,
                    }
                return None
        return None

    # 维度基数/枚举探查：关键词组 -> (field, alias, 维度中文名)
    _DIM_KEYWORDS: tuple[tuple[tuple[str, ...], str, str], ...] = (
        (("地区", "省份", "省"), "province", "region_count"),
        (("品牌",), "brand", "brand_count"),
        (("品类", "类别"), "category", "category_count"),
        (("用户",), "user_id", "user_count"),
        (("性别",), "gender", "gender_count"),
        (("支付状态", "支付方式"), "pay_status", "pay_status_count"),
    )

    def _enum_dimension_field(self, q: str) -> str | None:
        """维度枚举探查：识别"有哪些 [维度]""[所有/全部] [维度]"返回维度字段。

        枚举查询（纯维度列表）除 count_distinct 指标外，还需把维度字段加入
        dimensions 用于成员去重枚举。命中多个关键词时取首个注册字段。
        """
        for keywords, field, _alias in self._DIM_KEYWORDS:
            if any(k in q for k in keywords):
                if field in catalog.COLUMNS:
                    return field
                return None
        return None

    @classmethod
    def _is_dim_enum_form(cls, q: str) -> bool:
        """枚举型语气词："有哪些/所有/全部/都有/列一下"。"""
        return any(
            k in q for k in ("有哪些", "有哪几", "所有", "全部", "都有哪些", "列表", "清单列出")
        )

    def _window_metric(self, q: str) -> dict[str, Any] | None:
        """识别窗口指标：累计（cumsum）/ 移动平均（moving_avg）。"""
        is_ma = "移动平均" in q or "滑动平均" in q
        is_cum = "累计" in q
        if not (is_ma or is_cum):
            return None

        if "订单数" in q or "订单量" in q:
            base = {
                "kind": "aggregate",
                "field": "order_id",
                "agg": "count",
                "alias": "order_count",
            }
            stem = "order_count"
        else:
            base = {"kind": "aggregate", "field": "order_amount", "agg": "sum", "alias": "gmv"}
            stem = "gmv"

        if is_ma:
            m = re.search(r"(\d+)\s*日", q)
            size = int(m.group(1)) if m else 7
            return {
                "kind": "window",
                "base": base,
                "func": "moving_avg",
                "window_size": size,
                "alias": f"ma{size}_{stem}",
            }
        return {"kind": "window", "base": base, "func": "cumsum", "alias": f"cum_{stem}"}

    # ------------------------------------------------------------------ #
    # 维度
    # ------------------------------------------------------------------ #
    @staticmethod
    def _time_dim_field(q: str) -> str:
        """时间主轴字段推断（解除 order_time 硬编码）。

        规则：仅当问句是**退款时间序列**（含时间维度词 + 退款金额/退款总额）时才
        切换到 refund_time；"退款率"是比率指标，时间维度保持 order_time。
        """
        is_refund_amount = any(k in q for k in ("退款金额", "退款总额"))
        is_time_series = any(
            k in q
            for k in (
                "每日",
                "按天",
                "每天",
                "趋势",
                "累计",
                "移动平均",
                "滑动平均",
                "补零",
                "补齐",
            )
        )
        if is_refund_amount and is_time_series:
            return "refund_time"
        return "order_time"

    def _dimensions(self, q: str) -> list[dict[str, str]]:
        dims: list[dict[str, str]] = []
        seen: set[str] = set()

        def add(field: str) -> None:
            if field not in seen:
                dims.append({"field": field})
                seen.add(field)

        if any(
            k in q
            for k in (
                "每日",
                "按天",
                "每天",
                "趋势",
                "累计",
                "移动平均",
                "滑动平均",
                "补零",
                "补齐",
            )
        ):
            add(self._time_dim_field(q))  # 时间主轴：退款场景用 refund_time
        if any(k in q for k in ("各品类", "按品类", "分品类", "品类分布", "每品类", "品类")):
            add("category")
        if "品牌" in q:
            add("brand")
        # 商品/店铺实体 -> 维度名称字段（"问什么就出什么维度"，仅当字段已在目录登记）
        if any(k in q for k in ("产品", "商品")):
            if "product_name" in catalog.COLUMNS:
                add("product_name")
        if any(k in q for k in ("店铺", "门店")):
            if "shop_name" in catalog.COLUMNS:
                add("shop_name")
        if any(k in q for k in ("各省", "按省份", "分省", "省份分布", "每省")):
            add("province")
        # "地区"分组语境（区别于 count 型"有几个地区"，后者不分组）
        if any(k in q for k in ("各地区", "每个地区", "按地区", "分地区", "每地区", "地区分布")):
            add("province")
        if "支付状态" in q:
            add("pay_status")
        # 明细/清单：逐订单下钻
        if any(k in q for k in ("明细", "清单")):
            add("order_id")
            add(self._time_dim_field(q))
        return dims

    # ------------------------------------------------------------------ #
    # 过滤
    # ------------------------------------------------------------------ #
    def _filters(self, q: str) -> list[dict[str, Any]]:
        filters: list[dict[str, Any]] = []

        # 支付口径：成功/成交
        if any(k in q for k in ("成功", "成交")):
            filters.append({"field": "pay_status", "operator": "eq", "value": "SUCCESS"})

        # 未履约/未完成订单：支付状态非成功（数据集中为 CANCELLED）
        if any(k in q for k in ("未履约", "未完成", "未支付")):
            filters.append({"field": "pay_status", "operator": "ne", "value": "SUCCESS"})

        # 省份（单个或多个 -> in）
        provinces = [p for p in dimension_members("province") if p in q]
        if provinces:
            if len(provinces) == 1:
                filters.append({"field": "province", "operator": "eq", "value": provinces[0]})
            else:
                filters.append({"field": "province", "operator": "in", "value": provinces})

        # 大区（华东/华南等）-> 省份 in 过滤（与省份过滤互斥，先命中大区）
        if not provinces:
            for region, region_list in REGIONS.items():
                if region in q:
                    in_region = [p for p in region_list if p in dimension_members("province")]
                    if in_region:
                        filters.append({"field": "province", "operator": "in", "value": in_region})
                        break

        # 类目
        cats = [c for c in dimension_members("category") if c in q]
        if cats:
            filters.append({"field": "category", "operator": "eq", "value": cats[0]})

        # 数值区间：金额A到B元
        m = re.search(r"金额?\s*(\d+)\s*到\s*(\d+)\s*元", q)
        if m:
            filters.append(
                {
                    "field": "order_amount",
                    "operator": "between",
                    "value": [int(m.group(1)), int(m.group(2))],
                }
            )
        return filters

    # ------------------------------------------------------------------ #
    # 时间
    # ------------------------------------------------------------------ #
    def _time_filter(self, q: str) -> dict[str, Any] | None:
        # 绝对：2024年6月 / 2024年（解析单一实现于 agent.time_utils，编排兜底同源）
        window = parse_explicit_time_window(q)
        if window:
            start, end = window
            return {
                "granularity": "day",
                "range_type": "absolute",
                "absolute": {"start": start, "end": end},
            }

        # 相对：上个月 / 这个月 / 过去N天 / 过去N个月 / 过去半年 / 季度 / 至今（MTD/QTD/YTD）
        if "上个月" in q or "上月" in q:
            return {
                "granularity": "month",
                "range_type": "relative",
                "relative": {"amount": 1, "unit": "month", "mode": "calendar"},
                "reference_date": settings.AS_OF_DATE.isoformat(),
            }
        if "上季度" in q or "上个季度" in q or "上一季度" in q:
            # 上季度：完整上一个自然季度（calendar quarter）
            return {
                "granularity": "quarter",
                "range_type": "relative",
                "relative": {"amount": 1, "unit": "quarter", "mode": "calendar"},
                "reference_date": settings.AS_OF_DATE.isoformat(),
            }
        if "本季度至今" in q or "本季度到目前" in q or "本qtd" in q.lower():
            # QTD：本季度起点至锚点日期
            return {
                "granularity": "quarter",
                "range_type": "relative",
                "relative": {"amount": 1, "unit": "quarter", "mode": "to_date"},
                "reference_date": settings.AS_OF_DATE.isoformat(),
            }
        if "本季度" in q or "这个季度" in q or "当季" in q:
            # 本季度（完整）：[季初, 下季初)；沿用 to_date 会截断到当前日，这里完整给整季
            # 为保持确定性，以 AS_OF_DATE 所在季度为"当前季度"，输出整个自然季度
            start, end = _current_quarter_bounds(settings.AS_OF_DATE)
            return {
                "granularity": "quarter",
                "range_type": "absolute",
                "absolute": {"start": start.isoformat(), "end": end.isoformat()},
            }
        if "本月至今" in q or "本月到目前" in q or "本月初至今" in q or "月至今" in q:
            # MTD：本月 1 日至锚点日期
            return {
                "granularity": "day",
                "range_type": "relative",
                "relative": {"amount": 1, "unit": "month", "mode": "to_date"},
                "reference_date": settings.AS_OF_DATE.isoformat(),
            }
        if (
            "本年度至今" in q
            or "本年至今" in q
            or "年初至今" in q
            or "今年至今" in q
            or "ytd" in q.lower()
        ):
            # YTD：本年 1 月 1 日至锚点日期
            return {
                "granularity": "month",
                "range_type": "relative",
                "relative": {"amount": 1, "unit": "year", "mode": "to_date"},
                "reference_date": settings.AS_OF_DATE.isoformat(),
            }
        if "这个月" in q or "本月" in q:
            # 当前自然月（锚定 AS_OF_DATE）：[当月1日, 次月1日)
            ref = settings.AS_OF_DATE
            start = ref.replace(day=1)
            end = (
                start.replace(year=start.year + 1, month=1)
                if start.month == 12
                else start.replace(month=start.month + 1)
            )
            return {
                "granularity": "day",
                "range_type": "absolute",
                "absolute": {"start": start.isoformat(), "end": end.isoformat()},
            }
        m = re.search(r"(?:过去|最近|近)\s*(\d+)\s*个?月", q)
        if m:
            return {
                "granularity": "month",
                "range_type": "relative",
                "relative": {
                    "amount": int(m.group(1)),
                    "unit": "month",
                    "mode": "trailing",
                },
                "reference_date": settings.AS_OF_DATE.isoformat(),
            }
        m = re.search(r"(?:过去|最近|近)\s*(\d+)\s*个?季度", q)
        if m:
            return {
                "granularity": "quarter",
                "range_type": "relative",
                "relative": {
                    "amount": int(m.group(1)),
                    "unit": "quarter",
                    "mode": "trailing",
                },
                "reference_date": settings.AS_OF_DATE.isoformat(),
            }
        if "半年" in q:
            return {
                "granularity": "month",
                "range_type": "relative",
                "relative": {"amount": 6, "unit": "month", "mode": "trailing"},
                "reference_date": settings.AS_OF_DATE.isoformat(),
            }
        m = re.search(r"(?:过去|最近|近)\s*(\d+)\s*周", q)
        if m:
            return {
                "granularity": "week",
                "range_type": "relative",
                "relative": {
                    "amount": int(m.group(1)),
                    "unit": "week",
                    "mode": "trailing",
                },
                "reference_date": settings.AS_OF_DATE.isoformat(),
            }
        m = re.search(r"(?:过去|最近|近)\s*(\d+)\s*(?:天|日)", q)
        if m:
            return {
                "granularity": "day",
                "range_type": "relative",
                "relative": {"amount": int(m.group(1)), "unit": "day", "mode": "trailing"},
                "reference_date": settings.AS_OF_DATE.isoformat(),
                "time_field": self._time_dim_field(q),
            }
        # 无年份的月份（如 "6月GMV" / "6月每日"）：锚定 AS_OF_DATE 所在年份
        m = re.search(r"(?<![\d])(\d{1,2})\s*月", q)
        if m:
            year, month = settings.AS_OF_DATE.year, int(m.group(1))
            end_year, end_month = (year + 1, 1) if month == 12 else (year, month + 1)
            return {
                "granularity": "day",
                "range_type": "absolute",
                "absolute": {
                    "start": f"{year:04d}-{month:02d}-01",
                    "end": f"{end_year:04d}-{end_month:02d}-01",
                },
                "time_field": self._time_dim_field(q),
            }
        return None

    # ------------------------------------------------------------------ #
    # 对比（同比/环比）
    # ------------------------------------------------------------------ #
    def _comparison(self, q: str) -> str | None:
        ql = q.lower()
        if "同比" in q or "yoy" in ql:
            return "yoy"
        if "环比" in q or "mom" in ql:
            return "mom"
        return None

    # ------------------------------------------------------------------ #
    # 补零 / 分组 Top-N
    # ------------------------------------------------------------------ #
    def _fill_gaps(self, q: str) -> bool:
        return any(k in q for k in ("补零", "补齐", "补全"))

    def _top_n(self, q: str) -> dict[str, Any] | None:
        ql = q.lower()
        n = None
        m = re.search(r"top\s*(\d+)", ql)
        if not m:
            m = re.search(r"前\s*(\d+)\s*[个名]", q)
        if not m:
            return None
        n = int(m.group(1))

        partition: list[str] = []
        if any(k in q for k in ("每省", "各省", "按省")):
            partition.append("province")
        elif any(k in q for k in ("每品牌", "各品牌")):
            partition.append("brand")
        elif any(k in q for k in ("每品类", "各品类")):
            partition.append("category")
        if not partition:
            return None
        return {
            "n": n,
            "partition_by": partition,
            "order_by": [{"field": self._primary_alias(q), "direction": "desc"}],
        }

    # ------------------------------------------------------------------ #
    # 排序 / 截断
    # ------------------------------------------------------------------ #
    @staticmethod
    def _extreme_kind(q: str) -> str | None:
        """极值修饰词方向：最高/最大/最好 -> max（降序）；最低/最小/最差 -> min（升序）。"""
        if any(k in q for k in ("最高", "最大", "最好", "最多", "最强")):
            return "max"
        if any(k in q for k in ("最低", "最小", "最差", "最少", "最弱", "最便宜")):
            return "min"
        return None

    def _order_by(self, q: str) -> list[dict[str, Any]]:
        # 趋势/窗口/补零 -> 按时间主轴升序（退款时序用 refund_time）
        if any(
            k in q
            for k in (
                "每日",
                "按天",
                "每天",
                "趋势",
                "累计",
                "移动平均",
                "滑动平均",
                "补零",
                "补齐",
            )
        ):
            return [{"field": self._time_dim_field(q), "direction": "asc"}]
        # 极值修饰词优先："GMV最高的产品" -> 按主指标降序；"退款率最低的3个店铺" -> 升序
        ek = self._extreme_kind(q)
        if ek == "min":
            return [{"field": self._primary_alias(q), "direction": "asc"}]
        if ek == "max":
            return [{"field": self._primary_alias(q), "direction": "desc"}]
        # 最高/排名/前N -> 按主指标降序
        if any(k in q for k in ("最高", "排名", "前")):
            return [{"field": self._primary_alias(q), "direction": "desc"}]
        # 有维度且非"分布"型 -> 按主指标降序（可控默认；"分布"视为不排序的清单型问题）
        if self._dimensions(q) and "分布" not in q:
            return [{"field": self._primary_alias(q), "direction": "desc"}]
        return []

    def _primary_alias(self, q: str) -> str:
        m = self._metrics(q)
        if len(m) == 1 and isinstance(m[0], dict):
            return m[0]["alias"]  # 聚合 / 比率 / 窗口指标均携带 alias
        return "gmv"

    def _limit(self, q: str) -> int:
        # 显式 Top-N：前N个 / 最高的N个 / N个[实体]
        m = re.search(
            r"(?:前|最高|最低|最大|最小|最好|最差|最)?\s*(\d+)\s*"
            r"个(?:产品|商品|店铺|门店|品牌|品类|地区|省份|用户|订单)?",
            q,
        )
        if m:
            return int(m.group(1))
        # 单数极值："最高的X是什么/哪一个" -> 只展示 1 条，避免硬编码返回 100 条标量
        if self._extreme_kind(q) is not None and any(
            k in q for k in ("是什么", "是哪个", "哪一个", "哪个")
        ):
            return 1
        # 维度基数统计只返回一个总数，不强制用户补充 limit。
        if any(k in q for k in ("有几个", "有多少个", "数量", "数目")):
            if self._dim_count_fallback(q) is not None:
                return 1
        return 100
