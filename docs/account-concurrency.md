# 单账号并发限制（account concurrency）

本项目原生**没有**按账号的并发控制：`/v1/chat/completions` 等主链路里没有按凭证的信号量、队列或 QPS 限制，每个请求独立挑选凭证后直接打上游（唯一的 `asyncio.Semaphore(5)` 只用于首次探测凭证的模型列表）。

这个补丁加了一个最小实现：**同一个凭证（账号 email）同时最多 N 个在途上游请求，超出的排队等待**。

## 配置

| 方式 | 说明 |
| --- | --- |
| 控制面板 → 设置 → 并发控制 → 单账号并发上限 | 推荐，保存后 5 秒内生效，无需重启 |
| 配置项 `account_concurrency` | 数据库配置键，等价于面板设置 |
| 环境变量 `ACCOUNT_CONCURRENCY` | 优先级最高，设置后覆盖面板配置（服务器部署常用） |

`0` 表示不限制（默认，保持原生行为）。取值校验范围 0–500。

示例：

```bash
# 通过面板接口设置（token 即面板密码）
curl -X POST http://127.0.0.1:7861/config/save \
  -H "Authorization: Bearer $PANEL_PASSWORD" \
  -H 'Content-Type: application/json' \
  -d '{"config":{"account_concurrency":3}}'

# 或环境变量
ACCOUNT_CONCURRENCY=3 python web.py
```

## 实现

| 文件 | 说明 |
| --- | --- |
| `src/acct_limit.py` | `slot(email)` 异步上下文管理器：按 `(email, limit)` 维护 `asyncio.Semaphore`；`limit<=0` 直接放行；带在飞计数与 `[ACCT LIMIT]` 日志 |
| `src/httpx_client.py` | `post_async()` / `stream_post_async()` 新增 `slot_key` 参数，把真实上游请求包进 `acct_limit.slot()` |
| `src/api/antigravity.py` | 流式 / 非流式两处上游调用传 `slot_key=current_file`（凭证 email） |
| `config.py` | 新增 `get_account_concurrency()`（ENV > DB > 默认 0） |
| `src/panel/config_routes.py` | `/config/get` 返回该配置，`/config/save` 校验 0–500 |
| `frontend/src/pages/Config.jsx` | 设置页新增「并发控制」分组 |

行为细节：

- **槽位覆盖整个上游请求**。流式请求从建立 SSE 连接一直占到流结束（`async with` 包住 `client.stream()` 的迭代），所以长回答也计入并发；
- 重试 / 换凭证时，先释放旧账号的槽位，再用新账号申请，不会串号；
- 限流值热改有 5 秒缓存（`_CACHE_TTL`），改完最迟 5 秒生效；limit 变化会新建信号量，旧的自然排空；
- 拿不到 email（例如凭证列表、配额查询等内部请求）时直接放行，不受影响。

## 实测（单账号，gemini-2.5-flash-lite，单请求约 2.4s）

| 设置 | 并发请求数 | 墙钟 | 波形 |
| --- | --- | --- | --- |
| 1 | 1 | 2.47s | 基准 |
| 2 | 6 | 7.01s | 2.2 / 4.2 / 7.0s，三波 |
| 3 | 6（非流式） | 5.33s | 2.6 / 5.3s，两波 |
| 3 | 6（流式 SSE） | 4.83s | 2.4 / 4.8s，两波，事件数一致 |
| 8 | 8 | 3.68s | 一波全并行 |
| 0（不限） | 40 | ~2.5s/个 | 全并行，无 429 |

日志示例：`[ACCT LIMIT] user@example.com 并发上限=3`。
