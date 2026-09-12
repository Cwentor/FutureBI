"""语义目录：逻辑字段 -> 物理表/列 的受控映射。

这是"杜绝随意 Join / SQL 注入"的关键防线：编译器只允许引用本目录登记的字段，
表连接关系也只由本目录声明，禁止任意 Join。

多事实表模型：
- FACT_TABLE 是主事实表（查询锚点，FROM 主表）；
- 第二事实表（如 fact_refunds）通过 FACT_JOIN_RULES 受控连接，且与主事实表
  在业务上保证 1:1（每订单至多一条退款），避免一对多扇出放大聚合结果。

数据驱动（P0-2）：本文件的默认目录只是"内置回退"。生产启动时由
semantic.catalog_loader.refresh_catalog() 从 DuckDB information_schema 元数据 +
config/semantic.yaml 覆写重建目录（改配置即可新增表/字段，不再需要改 Python）。
compiler / guard 一律通过 `catalog.XXX` 动态读取本模块当前状态。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class FieldMeta:
    """字段元数据：物理表 + 列名 + 类型（dtype 用于字面量安全转义）。"""

    table: str
    column: str
    dtype: str  # 用于字面量安全转义：str / int / float / bool / timestamp


@dataclass(frozen=True)
class JoinRule:
    """受控连接声明（P0-2：不再裸拼 SQL）。

    - join_type：inner / left；
    - on：一或多个 (joined_table_col, fact_table_col) 字段对，
      渲染为 `{joined_alias}.{col1} = {fact_alias}.{col2}`。
    """

    join_type: str
    on: tuple[tuple[str, str], ...] = ()


# 逻辑字段 -> 物理字段（内置默认目录；生产环境由 catalog_loader 从元数据+YAML 重建）
COLUMNS: dict[str, FieldMeta] = {
    # fact_orders（主事实表）
    "order_id": FieldMeta("fact_orders", "order_id", "int"),
    "user_id": FieldMeta("fact_orders", "user_id", "int"),
    "product_id": FieldMeta("fact_orders", "product_id", "int"),
    "order_amount": FieldMeta("fact_orders", "order_amount", "float"),
    "discount_amount": FieldMeta("fact_orders", "discount_amount", "float"),
    "pay_status": FieldMeta("fact_orders", "pay_status", "str"),
    "order_time": FieldMeta("fact_orders", "order_time", "timestamp"),
    "shop_id": FieldMeta("fact_orders", "shop_id", "int"),
    # fact_refunds（第二事实表：退款）
    "refund_id": FieldMeta("fact_refunds", "refund_id", "int"),
    "refund_amount": FieldMeta("fact_refunds", "refund_amount", "float"),
    "refund_time": FieldMeta("fact_refunds", "refund_time", "timestamp"),
    "refund_status": FieldMeta("fact_refunds", "refund_status", "str"),
    # dim_user
    "province": FieldMeta("dim_user", "province", "str"),
    "gender": FieldMeta("dim_user", "gender", "str"),
    "register_time": FieldMeta("dim_user", "register_time", "timestamp"),
    # dim_product
    "category": FieldMeta("dim_product", "category", "str"),
    "brand": FieldMeta("dim_product", "brand", "str"),
    "unit_price": FieldMeta("dim_product", "unit_price", "float"),
    "product_name": FieldMeta("dim_product", "product_name", "str"),
    # dim_shop
    "shop_name": FieldMeta("dim_shop", "shop_name", "str"),
}

# 表别名（编译器内部使用）
ALIASES: dict[str, str] = {
    "fact_orders": "f",
    "fact_refunds": "r",
    "dim_user": "u",
    "dim_product": "p",
    "dim_shop": "s",
}

# 主事实表（查询锚点，FROM 主表）
FACT_TABLE: str = "fact_orders"

# 全部事实表（用于校验/文档）
FACT_TABLES: tuple[str, ...] = ("fact_orders", "fact_refunds")

# 受控连接规则：只允许从主事实表星型连接维度表
JOIN_RULES: dict[str, JoinRule] = {
    "dim_user": JoinRule("inner", (("user_id", "user_id"),)),
    "dim_product": JoinRule("inner", (("product_id", "product_id"),)),
    "dim_shop": JoinRule("inner", (("shop_id", "shop_id"),)),
}

# 第二事实表 -> 主事实表 的受控连接（LEFT JOIN，业务上 1:1，无扇出）
FACT_JOIN_RULES: dict[str, JoinRule] = {
    "fact_refunds": JoinRule("left", (("order_id", "order_id"),)),
}

# 维度成员词汇表（逻辑字段 -> 成员值）：启发式解析与多轮会话继承从问题文本
# 抽取维度值用（审计 §3.2-4：原 heuristic.PROVINCES/CATEGORIES 硬编码常量数据化）。
# 内置默认仅作"库不可用"时的离线回退；服务启动时由
# catalog_loader.refresh_catalog() 从数仓 dim 表 distinct 值重建——
# 新增省份/品类等维度成员只需改库，离线启发式路径不再失明。
DIMENSION_MEMBERS: dict[str, tuple[str, ...]] = {
    "province": ("广东", "浙江", "江苏", "北京", "上海", "四川", "湖北", "山东"),
    "gender": ("M", "F"),
    "category": ("数码", "家电", "服饰", "美妆", "食品", "家居"),
    "brand": (
        "华为",
        "小米",
        "苹果",
        "联想",
        "美的",
        "格力",
        "海尔",
        "TCL",
        "优衣库",
        "耐克",
        "阿迪达斯",
        "李宁",
        "兰蔻",
        "雅诗兰黛",
        "欧莱雅",
        "自然堂",
        "三只松鼠",
        "良品铺子",
        "蒙牛",
        "伊利",
        "宜家",
        "顾家",
        "全友",
        "林氏木业",
    ),
}

# 可下钻字符串维度白名单（诊断归因候选维度池）：只有这些字段才允许作为
# 「用户未显式指定维度」时的自动下钻候选——分省只是候选之一，由信息增益
# 裁决入选者，严禁把 province 当默认第一梯队写死。高基数字段
# （product_name/shop_name）与身份属性字段（gender）不在列。
DRILLDOWN_DIM_FIELDS: tuple[str, ...] = ("province", "brand", "category")

# 大区 -> 省份成员映射（审计修复 M1 区域词展开）。
# 行政区划归属是业务知识（保留常量），但展开值域必须与数仓实际存在的省份取交集：
# mock 数仓 dim_user.province 仅含 广东/浙江/江苏/北京/上海/四川/湖北/山东，
# 生产环境由 catalog_loader 从数仓 distinct 值重建 DIMENSION_MEMBERS，
# 消费方（agent 启发式/LLM 路径、编排器规范化）一律经 region_provinces()
# 与成员词汇表求交——库中裁撤的省份不会产出 province IN ('无效省') 空过滤。
# 严禁把映射值直接当字面值写 SQL（province = '华东' 属错误口径，M1 缺陷根源）。
REGION_PROVINCE_MAPPING: dict[str, tuple[str, ...]] = {
    "华北": ("北京",),
    "华东": ("上海", "江苏", "浙江", "山东"),
    "华南": ("广东",),
    "华中": ("湖北",),
    "西南": ("四川",),
}
