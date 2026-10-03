# traffic-guard-python

面向 FastAPI 对外接口的分级限流与异常流量防护组件。在保持既有接口行为不变的前提下，提供按「来源身份 × 接口」两个维度的限流、固定/滑动两种窗口策略、确定性的拒绝响应、并发安全的计数、反复违规的临时封禁与自动解封、存储后端故障降级、白名单豁免与配置热加载。

## 快速开始

```bash
pip install -e .
python main.py
```

服务默认读取仓库根目录下的 `traffic_guard.json`（可用环境变量 `TRAFFIC_GUARD_CONFIG` 指定路径）；文件不存在时使用内置默认值（每 60 秒 100 次，固定窗口）。参考配置见 `traffic_guard.example.json`。

中间件已在 `main.py` 中接好：

```python
from traffic_guard import TrafficGuard, TrafficGuardMiddleware
from traffic_guard.config import ConfigLoader
from traffic_guard.storage import MemoryStorage

guard = TrafficGuard(config_loader=ConfigLoader("traffic_guard.json"), storage=MemoryStorage())
app.add_middleware(TrafficGuardMiddleware, guard=guard)
```

## 配置项说明

配置为 JSON 文件，支持热加载（服务运行期间修改文件即生效，无需重启）。

| 字段 | 默认值 | 说明 |
| --- | --- | --- |
| `default_limit.requests` | `100` | 每个窗口允许的最大请求数 |
| `default_limit.window_seconds` | `60` | 窗口长度（秒） |
| `default_limit.strategy` | `"fixed_window"` | 限流策略：`"fixed_window"` 或 `"sliding_window"` |
| `interface_limits` | `{}` | 按接口路径覆盖规则，key 为路径（如 `"/api/expensive"`），value 为同上的规则对象 |
| `whitelist_sources` | `[]` | 白名单来源（客户端 IP 或 `source_header` 指定的身份），命中后跳过一切限流 |
| `whitelist_interfaces` | `[]` | 白名单接口路径（如 `"/healthz"`），命中后跳过一切限流 |
| `ban.enabled` | `true` | 是否启用临时封禁 |
| `ban.threshold` | `5` | 在 `violation_window_seconds` 内累计多少次限流违规后触发封禁 |
| `ban.violation_window_seconds` | `60` | 违规计数窗口（秒） |
| `ban.ban_seconds` | `300` | 封禁时长（秒），到期自动解封 |
| `degradation.on_backend_failure` | `"fail_open"` | 存储后端不可用时的降级策略：`"fail_open"` / `"fail_close"` / `"local_fallback"` |
| `degradation.local_fallback_max_keys` | `10000` | 本地降级存储的最大键数 |

示例：

```json
{
  "default_limit": { "requests": 100, "window_seconds": 60, "strategy": "fixed_window" },
  "interface_limits": {
    "/api/expensive": { "requests": 10, "window_seconds": 60, "strategy": "sliding_window" }
  },
  "whitelist_sources": ["127.0.0.1"],
  "whitelist_interfaces": ["/healthz"],
  "ban": { "enabled": true, "threshold": 5, "violation_window_seconds": 60, "ban_seconds": 300 },
  "degradation": { "on_backend_failure": "fail_open", "local_fallback_max_keys": 10000 }
}
```

## 限流语义

### 两个维度

限流键为 `limit:{来源身份}:{接口路径}`，来源与接口两个维度组合：

- 同一来源访问不同接口 → 键不同，额度互不干扰；
- 不同来源访问同一接口 → 键不同，额度相互独立。

来源身份默认取客户端 IP（`scope["client"]`）；构造中间件时传入 `source_header`（如 `"X-API-Key"`）可改为按身份标识限流，header 缺失时回退为客户端 IP。

### 固定窗口（fixed_window）

- 时间按 `window_seconds` 对齐到 Unix 纪元切片：窗口起点 = `floor(now / window) * window`。
- 每个窗口一个计数器，窗口边界处计数器原子归零，归属明确：边界那一刻的请求属于新窗口。
- 特点是实现简单、边界清晰；但允许跨边界的突发流量（边界两侧各打满一个窗口）。

### 滑动窗口（sliding_window）

- 为每个键维护请求时间戳日志，仅保留最近 `window_seconds` 内的请求，计数随时间滑动。
- 没有对齐边界：请求不会在某个固定时刻集中重置，突发流量被平滑到整个窗口上。
- 拒绝时返回的 `Retry-After` = 最老请求离开窗口还需等待的时间，是精确的最小退避时长。

### 响应语义

命中限流时返回**确定性**响应，不抛异常：

| 状态码 | 含义 | 触发条件 |
| --- | --- | --- |
| `429` | Too Many Requests | 触发限流（含滑动/固定窗口额度用尽） |
| `403` | Forbidden | 来源处于临时封禁期 |
| `503` | Service Unavailable | 存储后端不可用且降级策略为 `fail_close` |

所有拒绝响应均为 JSON：

```json
{ "detail": "rate limit exceeded", "status_code": 429, "retry_after_seconds": "12" }
```

并携带响应头：

- `Retry-After`：建议客户端等待的秒数（向上取整，最小 1）；
- `X-RateLimit-Limit` / `X-RateLimit-Remaining` / `X-RateLimit-Reset`：额度总量、剩余量、窗口重置时间；
- `X-Traffic-Guard-Degraded: true`：本次判定发生在后端故障降级期间。

被拒绝的请求在中间件层直接返回，**不会到达业务逻辑**，因此不消耗后端处理资源；封禁中的请求连存储调用都不会发生。

## 临时封禁与自动解封

- 每次命中 429 记为一次违规；在 `violation_window_seconds` 内累计达到 `threshold` 次即触发封禁（403）。
- 封禁期间的请求直接拒绝、不访问存储、不计违规；到期后自动解封，违规历史清零。
- 封禁与解封均写入日志（logger `traffic_guard`）并计入指标（`bans_total`、`bans_unbanned_total`），可通过 `GET /traffic-guard/status` 查看当前封禁列表与最近事件。
- 配置热更新重建封禁管理器时会保留进行中的封禁，不会因重载配置提前解封。

## 存储后端与降级

限流器只依赖 `Storage` 协议，内置两种实现：

- `MemoryStorage`：进程内存储，按 key 加锁保证原子性，为默认实现，也是本地降级存储；
- `RedisStorage`：基于 Redis 的实现，使用 Lua 脚本保证计数原子性（需自行安装 `redis` 包：`pip install redis`，构造时传入连接 URL 即可）。

后端不可用时的行为由 `degradation.on_backend_failure` 配置，三种策略均可解释：

| 策略 | 行为 | 适用场景 |
| --- | --- | --- |
| `fail_open`（默认） | 记录错误并放行请求，响应带降级标记 | 限流是保护措施，可用性优先，后端故障时宁放勿拒 |
| `fail_close` | 返回 `503`，提示 `rate limiter unavailable` | 强管控场景，后端不可用时宁可拒绝也不放行 |
| `local_fallback` | 切换到进程内本地存储继续限流 | 希望限流不中断；注意本地存储是独立额度，故障切换瞬间额度重新计算 |

任何未预期的异常都会被中间件兜底捕获并放行（fail-open），保证限流组件自身的故障不会拖垮服务。

## 白名单与豁免

- `whitelist_sources`：命中的来源跳过一切限流与封禁判定；
- `whitelist_interfaces`：命中的接口对所有来源豁免（如健康检查 `/healthz`）。

白名单不消耗计数额度，且随配置热加载即时生效，无需重启服务。

## 可观测性

`GET /traffic-guard/status` 返回：

- `metrics`：`allowed_total` / `limited_total` / `banned_total` / `whitelisted_total` / `backend_errors_total` / `bans_total` / `bans_unbanned_total`；
- `active_bans`：当前生效的封禁（来源、到期时间、原因）；
- `recent_events`：最近的封禁/解封事件；
- `config`：当前生效的完整配置。

## 并发正确性

- 计数操作按 key 加锁（内存存储）或通过 Lua 脚本原子执行（Redis），高并发下额度不会穿透、计数不会错乱；
- 锁粒度为 key 级别，不同来源/接口的计数互不阻塞，计数不会成为明显性能瓶颈；
- 测试中以 25 线程 × 200 次请求的竞争场景验证：固定/滑动窗口的放行数都精确等于额度。

## 测试

```bash
python -m pytest tests/ -v
```

覆盖场景：

- 固定窗口：额度用尽、边界重置、边界归属、突发跨边界、退避时间收敛；
- 滑动窗口：滑出判定、最老请求决定的退避、与固定窗口的语义差异；
- 并发竞争：固定/滑动窗口额度精确性、多 key 独立性、违规竞争下只封禁一次；
- 封禁解封：累计违规触发、封禁期不触存储、到期自动解封、按来源隔离、违规窗口老化、事件可观测、配置热更新保留封禁；
- 白名单豁免：来源/接口豁免、不占额度、热加载生效、坏配置保留旧配置；
- 降级：fail-open 放行、fail-close 返回 503、local_fallback 仍限流、主存储恢复、异常不抛出；
- 中间件：429/403 响应头与体、503 降级标记、白名单绕过、意外异常兜底放行。
