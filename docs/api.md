# Web UI 与 API

## 启动服务

```bash
python -m web.server 8000
```

默认地址：`http://127.0.0.1:8000`。

## 端点

| 端点 | 方法 | 说明 |
| --- | --- | --- |
| `/api/health` | GET | 健康检查 |
| `/api/metrics` | GET | QPS、分位数、意图/动作分布等进程内指标 |
| `/api/auth/login` | POST | 用户名/口令换取 JWT 与 Session |
| `/api/auth/logout` | POST | 吊销服务端 Session |
| `/api/auth/me` | GET | 返回当前身份 |
| `/api/query` | POST | 执行受保护的数据查询 |
| `/api/agent/run` | POST | Data Agent 同步编排（多步分析 + 沙箱 + HITL 恢复） |
| `/api/v1/agent/chat/stream` | GET | Data Agent SSE 流式编排（AgentStreamEvent 事件流） |
| `/api/settings/providers` | GET / POST | 模型供应商列表（不含 api_key）与创建自定义供应商 |
| `/api/settings/providers/<id>` | PUT / DELETE | 更新（空 Key 保留原值）与删除（预置供应商拒绝） |
| `/api/settings/providers/test` | POST | 连通性探测（极小 ping 请求，返回 HTTP 200 + 延时） |
| `/api/settings/providers/<id>/reveal` | POST | 查看已保存的真实 API Key（显式动作，记审计日志） |
| `/api/schema/summary` | GET | 语义目录摘要：按物理表分组的可查询字段清单（知识上下文） |
| `/static/` | GET | Agent 对话流工作台前端（侧边栏 + 单列对话） |

## 查询示例

登录：

```bash
curl -X POST http://127.0.0.1:8000/api/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username":"analyst","password":"analyst123"}'
```

查询：

```bash
curl -X POST http://127.0.0.1:8000/api/query \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <token>" \
  -d '{"query":"各品类成功订单的GMV分布？"}'
```

`/api/query` 的响应包含 DSL、SQL、列、行、解释与可视化建议；principal 不接受客户端传入值，由服务端身份映射决定。

## SSE 流式 Data Agent 会话

`GET /api/v1/agent/chat/stream` 以 `text/event-stream` 推送编排全过程，每帧为
`data: <AgentStreamEvent JSON>\n\n`。事件类型（与前端 `web/static/js/protocol.js` 契约一致）：

| 事件 | 载荷要点 | 前端消费 |
| --- | --- | --- |
| `plan_created` | `plan: [{id,title,kind,status}]` | 任务 DAG 时间线 |
| `step_start` | `step_id / step_title` | 图节点进度指示 |
| `tool_start` / `tool_end` | `tool: {name, input, output, duration_ms, error}` | 工具手风琴（DSL/沙箱代码展开） |
| `reflection` | `reflection: {observation, decision, reason}` | 反思/自愈节点 |
| `hitl_request` | `hitl: {question, resume_token}` | 澄清交互卡（可点击答复） |
| `artifact_emit` | `artifact: {type, title, content}` | 产物入账（报告/图表/代码/数据表，侧边栏徽标计数） |
| `done` / `error` | `report` / `error` | 终态收尾（状态灯复位） |

查询参数：`query`（必填）、`human_reply` + `resume_token`（HITL 恢复）、
`provider_id` + `model_id`（请求级模型切换）。鉴权与 `/api/query` 一致
（Bearer JWT / 会话 Cookie）；编排异常收敛为 `error` 事件，不中断 HTTP 流。

## 模型供应商管理

`/api/settings/providers` 系列端点用于管理工作台的多模型供应商（预置智谱 / OpenAI /
Anthropic / Gemini，支持 OpenAI Chat、OpenAI Responses、Anthropic、Gemini 四种协议适配）。
设计约束：

- API Key 落盘加密存储（`providers/crypto.py`，Encrypt-then-MAC），列表/详情响应**完全不含
  `api_key` 字段**；编辑时留空即保留服务端原 Key；
- 连通性探测的业务失败（401/429/超时等）以 HTTP 200 + `success=false` 返回，与传输层错误
  （404 供应商不存在 / 400 参数非法）严格区分；
- SSE 编排请求可通过 `provider_id` + `model_id` 查询参数进行请求级模型切换，不指定时使用
  工作台当前选中的启用供应商。

供应商配置持久化于服务端 `config/providers.json`，详见[环境配置](configuration.md)。
