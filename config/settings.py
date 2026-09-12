"""全局配置：项目根目录、本地 DuckDB 路径、评测锚点日期与 LLM 接入。

说明：
- AS_OF_DATE 是 mock 数据生成与评测的统一时间锚点（数据与 golden 期望 SQL 均基于它），
  保证评测在任意机器、任意日期上都是确定性、可复现的。
- 生产环境由 Agent 在 TimeFilter.reference_date 中注入"今天"，此处仅作为缺省回退。
- LLM 相关配置全部通过环境变量注入；未配置 LLM_API_KEY 时 Agent 自动回退到
  确定性启发式 NL2DSL（agent.heuristic），保证离线可运行、可评测。
"""

from __future__ import annotations

import os
from datetime import date
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT: Path = Path(__file__).resolve().parents[1]

# 加载项目根目录 .env（若存在）。环境变量优先级高于 .env 文件，
# 便于 CI / 容器注入真实密钥。
load_dotenv(PROJECT_ROOT / ".env")

# 本地开发零成本数仓文件（模块 C 生成）
DB_PATH: Path = PROJECT_ROOT / "analytics_sandbox.duckdb"

# 编排器沙箱工作区根目录（Data Agent：Parquet 交换区 / 沙箱脚本 / 产物）
WORKSPACE_ROOT: Path = PROJECT_ROOT / "logs" / "workspaces"

# 数据与评测统一锚点日期
AS_OF_DATE: date = date(2024, 6, 30)

# 数仓数据域上界（含当天）：mock 订单最晚日期与 AS_OF_DATE 对齐。
# 时间窗口整体晚于该日 = 必然空集（确定性可证）——编排链路与单轮 chat
# 链路共用的"无数据诚实守卫"判据：超界查询严禁下钻归因或产出分析报告。
DATA_DOMAIN_END: date = AS_OF_DATE

# --------------------------------------------------------------------------- #
# LLM（NL -> DSL）接入配置 —— 通过环境变量注入，见 .env.example
# --------------------------------------------------------------------------- #
# API Key；为空则 Agent 使用确定性启发式 NL2DSL 兜底
LLM_API_KEY: str = os.getenv("LLM_API_KEY", "")
# OpenAI 兼容的 Chat Completions 端点
LLM_BASE_URL: str = os.getenv("LLM_BASE_URL", "https://api.openai.com/v1")
LLM_MODEL: str = os.getenv("LLM_MODEL", "gpt-4o-mini")
LLM_TEMPERATURE: float = float(os.getenv("LLM_TEMPERATURE", "0.0"))
LLM_TIMEOUT: int = int(os.getenv("LLM_TIMEOUT", "60"))
LLM_MAX_RETRIES: int = int(os.getenv("LLM_MAX_RETRIES", "2"))

# --------------------------------------------------------------------------- #
# SQL 执行层资源治理（P0/P1）—— 见 exec/ 包
# --------------------------------------------------------------------------- #
# 语句超时（毫秒）：超过则中断取消查询（DuckDB 侧用线程看门狗 + interrupt() 实现）
QUERY_TIMEOUT_MS: int = int(os.getenv("QUERY_TIMEOUT_MS", "30000"))
# 扫描行数上限：任一基表扫描超过即熔断拒绝执行（EXPLAIN ANALYZE 预检）
MAX_SCAN_ROWS: int = int(os.getenv("MAX_SCAN_ROWS", "10000000"))
# 返回行数硬上限：结果超过即熔断（LIMIT 硬上限，独立于 DSL 约束的防御性校验）
MAX_RESULT_ROWS: int = int(os.getenv("MAX_RESULT_ROWS", "20000"))
# SQL 执行自愈最大重试次数（把精确编译/引擎报错喂回 LLM 重写 DSL，至少 1 次；
# 审计修复 T01：1 次自愈对 order_by 别名类修正成功率不足，放宽为 3 次与编排器对齐）
SQL_SELF_HEAL_MAX_RETRIES: int = int(os.getenv("SQL_SELF_HEAL_MAX_RETRIES", "3"))
# EXPLAIN ANALYZE 扫描行预检缓存容量上限（整改指令3-3 降本项；原硬编码 512 改配置化）；
# 超出按"清空防膨胀"策略处理（扫描行数随数据变化，无 LRU 精度必要，只防无限增长）
MAX_SCAN_CACHE_SIZE: int = int(os.getenv("MAX_SCAN_CACHE_SIZE", "512"))
# 澄清槽位上下文 TTL（秒）：用户回答"最近30天"等短语的合并窗口（P0-5）
CLARIFY_SLOT_TTL: int = int(os.getenv("CLARIFY_SLOT_TTL", "1800"))
# 会话上下文记忆（Session Memory & Multi-turn Context）——见 agent/memory.py
# 会话状态 TTL（秒）：超过未交互自动失效，防止无限悬挂
SESSION_MEMORY_TTL: int = int(os.getenv("SESSION_MEMORY_TTL", "1800"))
# 进程内会话状态容量上限（超出按最久未访问 LRU 淘汰）
SESSION_MEMORY_MAX_SESSIONS: int = int(os.getenv("SESSION_MEMORY_MAX_SESSIONS", "1000"))
# 滚动保留的历史问答轮数（每轮 user + assistant 两条，控制 Token 消耗）
SESSION_MEMORY_HISTORY_TURNS: int = int(os.getenv("SESSION_MEMORY_HISTORY_TURNS", "5"))
# 执行层并发闸（P0-6）：只读连接池容量 + 全局并发信号量（"排队 + 熔断"双保险）
DB_POOL_SIZE: int = int(os.getenv("DB_POOL_SIZE", "4"))
MAX_CONCURRENT_QUERIES: int = int(os.getenv("MAX_CONCURRENT_QUERIES", "4"))

# 查询结果缓存（生产化）：同 (principal, SQL, 执行参数) 短期内重复查询免重复执行。
# 默认关闭；开启前确认数据时效可容忍 TTL 窗口（只读系统无写失效问题）。
QUERY_CACHE_ENABLED: bool = os.getenv("QUERY_CACHE_ENABLED", "0").lower() not in (
    "0",
    "false",
    "no",
)
QUERY_CACHE_TTL_SECONDS: float = float(os.getenv("QUERY_CACHE_TTL_SECONDS", "300"))
QUERY_CACHE_MAX_ENTRIES: int = int(os.getenv("QUERY_CACHE_MAX_ENTRIES", "256"))

# 异步查询任务（生产化）：POST /api/query/async 提交 -> 后台线程池执行 -> 轮询取回。
# 任务与结果保存在进程内存（重启丢失）；任务函数复用 run_query，护栏不旁路。
ASYNC_TASK_MAX_WORKERS: int = int(os.getenv("ASYNC_TASK_MAX_WORKERS", "2"))
ASYNC_TASK_HISTORY: int = int(os.getenv("ASYNC_TASK_HISTORY", "1000"))
ASYNC_TASK_TTL_SECONDS: float = float(os.getenv("ASYNC_TASK_TTL_SECONDS", "3600"))

# Multi-Tool Agent 调度上限（Max Steps：3~5 步，杜绝无限工具循环）
MAX_AGENT_STEPS: int = int(os.getenv("MAX_AGENT_STEPS", "5"))
# R3 反思层总开关：调度终止后自检结果充分性（确定性判定零成本；
# LLM 反思在预算有余时可追加一次受控查询，工具执行总数仍 <= MAX_AGENT_STEPS）
AGENT_REFLECTION_ENABLED: bool = os.getenv("AGENT_REFLECTION_ENABLED", "1") == "1"

# --------------------------------------------------------------------------- #
# 意图路由与决策中心（Intent Router & Decision Engine）—— 见 agent/router/
# --------------------------------------------------------------------------- #
# LLM 语义分类器判决置信度阈值：低于该值拒绝采纳，优雅降级到规则兜底
ROUTER_MIN_CONFIDENCE: float = float(os.getenv("ROUTER_MIN_CONFIDENCE", "0.6"))
# 意图语义分类单次 LLM 调用的超时（秒）：意图层耗时可控，绝不让路由拖垮主链路
ROUTER_LLM_TIMEOUT: int = int(os.getenv("ROUTER_LLM_TIMEOUT", "15"))
# 意图语义分类使用的模型名；留空则复用 LLM_MODEL（轻量模型优先，降低延迟与成本）
ROUTER_LLM_MODEL: str = os.getenv("ROUTER_LLM_MODEL", "")

# --------------------------------------------------------------------------- #
# Model Provider 网关层（多供应商接入）—— 见 providers/ 包
# --------------------------------------------------------------------------- #
# 供应商配置持久化文件（JSON；API Key 落盘加密存储，网络/展示不回传明文）
PROVIDERS_FILE: Path = PROJECT_ROOT / "config" / "providers.json"
# API Key 落盘加密主密钥（留空回退 AUTH_JWT_SECRET）；更换会使已存密钥无法解密
PROVIDERS_ENC_SECRET: str = os.getenv("PROVIDERS_ENC_SECRET", "")
# 供应商连通性探测与对话请求的默认超时（秒）
PROVIDER_TIMEOUT: int = int(os.getenv("PROVIDER_TIMEOUT", "60"))

# --------------------------------------------------------------------------- #
# 审计与结构化日志（P0）—— 见 audit/ 包
# --------------------------------------------------------------------------- #
# 是否开启审计写入（对象存储 JSONL + DuckDB 审计表）
AUDIT_ENABLED: bool = os.getenv("AUDIT_ENABLED", "1").lower() not in ("0", "false", "no")
# 审计产物目录（JSONL 对象存储 + DuckDB 审计表）
AUDIT_DIR: Path = PROJECT_ROOT / "logs"
AUDIT_LOG_PATH: Path = AUDIT_DIR / "audit.jsonl"
AUDIT_DB_PATH: Path = AUDIT_DIR / "audit.duckdb"
# 结构化日志级别（web.server 启动时 setup_logging 使用）
LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")

# --------------------------------------------------------------------------- #
# 统一身份认证（P0）—— 见 auth/ 包
# --------------------------------------------------------------------------- #
# 是否启用 HTTP 鉴权。开启后 /api/query 必须携带有效 JWT / 会话；
# 关闭时（本地开发/演示）仍不信任客户端：一律回退到 AUTH_DEFAULT_* 的服务端默认身份。
AUTH_ENABLED: bool = os.getenv("AUTH_ENABLED", "1").lower() not in ("0", "false", "no")
# 鉴权关闭时使用的服务端默认身份（principal 仍由服务端决定，客户端不可覆盖）
AUTH_DEFAULT_PRINCIPAL: str = os.getenv("AUTH_DEFAULT_PRINCIPAL", "admin")
AUTH_DEFAULT_USER: str = os.getenv("AUTH_DEFAULT_USER", "local-dev")
AUTH_DEFAULT_DISPLAY: str = os.getenv("AUTH_DEFAULT_DISPLAY", "本地开发者")
# 用户注册表 JSON（存在则加载；否则使用 auth.identity.DEFAULT_USERS）
AUTH_USERS_FILE: Path = PROJECT_ROOT / "auth" / "users.json"
# Web 绑定地址；非 localhost 绑定自动启用生产鉴权强校验
WEB_HOST: str = os.getenv("WEB_HOST", "127.0.0.1")
# 严格生产安全模式：拒绝弱 JWT 密钥与关闭鉴权
AUTH_STRICT: bool = os.getenv("AUTH_STRICT", "0").lower() not in ("0", "false", "no")
# 登录失败限流（P0-4）：按 用户名+IP 维度指数退避
AUTH_LOGIN_MAX_FAILURES: int = int(os.getenv("AUTH_LOGIN_MAX_FAILURES", "5"))
AUTH_LOGIN_BASE_SECONDS: float = float(os.getenv("AUTH_LOGIN_BASE_SECONDS", "2"))
AUTH_LOGIN_MAX_SECONDS: float = float(os.getenv("AUTH_LOGIN_MAX_SECONDS", "300"))
# JWT 密钥与令牌参数（生产环境务必通过环境变量注入强随机密钥）
AUTH_JWT_SECRET: str = os.getenv("AUTH_JWT_SECRET", "dev-insecure-jwt-secret-change-me")
WEAK_JWT_SECRETS: frozenset[str] = frozenset(
    {"", "dev-insecure-jwt-secret-change-me", "changeme", "secret", "password"}
)
AUTH_JWT_ISSUER: str = os.getenv("AUTH_JWT_ISSUER", "dataagent")
AUTH_JWT_AUDIENCE: str = os.getenv("AUTH_JWT_AUDIENCE", "dataagent-web")
# 令牌有效期（秒）：JWT 与 Session 各自独立
AUTH_JWT_TTL: int = int(os.getenv("AUTH_JWT_TTL", "3600"))
AUTH_SESSION_TTL: int = int(os.getenv("AUTH_SESSION_TTL", "86400"))
# 会话共享存储（P0-4）：配置为 SQLite 路径时启用持久化（重启不丢、多 worker 可共享）；
# 留空则使用进程内存储（本地开发/演示）。
AUTH_SESSION_DB: str | None = os.getenv("AUTH_SESSION_DB") or None

# --------------------------------------------------------------------------- #
# 会话记忆 / 澄清槽位 / 登录限流 外置化存储（整改指令3-2）
# --------------------------------------------------------------------------- #
# 配置为 SQLite 文件路径时，SessionStore / ClarifySlotStore / LoginRateLimiter
# 通过统一持久化后端落盘，使多 worker / 重启后状态一致；留空则进程内存储。
STATE_STORE_DB: str | None = os.getenv("STATE_STORE_DB") or None
