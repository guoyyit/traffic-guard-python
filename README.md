# traffic-guard-python

FastAPI 服务的分级限流与异常流量防护组件。在保持既有接口行为不变的前提下，
按 **来源身份 × 接口** 两个维度进行可配置的流量控制。

## 快速开始

```python
from fastapi import FastAPI
from traffic_guard import (
    ConfigReloader, InMemoryBackend, RateLimiter, RateLimitMiddleware,
)

reloader = ConfigReloader("traffic_guard_config.json")  # 配置变更无需重启
limiter = RateLimiter(InMemoryBackend(), reloader.get)

app = FastAPI()
app.add_middleware(RateLimitMiddleware, limiter=limiter)
```

`main.py` 已内置上述接线：通过环境变量 `TRAFFIC_GUARD_CONFIG` 指定配置文件
（默认 `traffic_guard_config.json`），文件不存在时使用内置默认配置。
参考 `traffic_guard_config.example.json`。

## 限流语义

### 维度与额度隔离

- 限流计数键为 `(identity, endpoint)` 二元组：同一来源访问不同接口互不影响，
  不同来源之间的额度相互独立。
- `identity` 默认取客户端 IP；构造 `RateLimitMiddleware` 时传入
  `identity_header="x-api-key"` 可改用指定请求头识别来源。

### 窗口边界约定（明确且可预期）

- **固定窗口**（`fixed_window`）：窗口按自然时间对齐，区间为
  `[k*window, (k+1)*window)`；请求时刻恰好等于右边界时计入下一个窗口。
  被限流时 `retry_after` 指向当前窗口的结束时刻。
- **滑动窗口**（`sliding_window`）：统计区间为 `(now - window, now]`，
  严格早于 `now - window` 的记录被剔除。被限流时 `retry_after` 为窗口内
  最早一条记录的过期等待时间。
- 两种策略下，**被拒绝的请求均不消耗额度**。

### 命中限流的响应

命中限流或封禁时返回稳定的 `429` 响应，不抛出未处理异常：

```json
{
  "error": "rate_limited",
  "message": "请求过于频繁，请稍后重试",
  "reason": "rate_limited",
  "retry_after": 42
}
```

- `reason`：`rate_limited`（超限）/ `banned`（封禁中）/ `degraded_closed`（降级拒绝）。
- 响应头携带 `Retry-After`（秒，整数），供客户端退避。
- 放行的请求携带 `X-RateLimit-Limit` / `X-RateLimit-Remaining` 响应头。

### 临时封禁与自动解封

- 同一来源在 `violation_window_seconds` 内累计 `violation_threshold` 次限流违规，
  即被封禁 `ban_seconds` 秒。
- 封禁期间的请求在进入计数逻辑前直接拒绝，**不消耗后端计数资源**。
- 封禁到期后自动解封（惰性：下次访问时判定）；封禁与解封均输出日志事件
  （logger 名称 `traffic_guard.ban`），便于观测与告警。
- 封禁后清空该来源的违规计数，避免解封后立即因历史违规再次被封。

### 存储后端不可用的降级

存储后端抛出的异常不会传播给请求方，按 `degrade_mode` 降级：

- `fail_open`（默认）：放行请求，优先保证服务可用；
- `fail_closed`：拒绝请求并返回 429（`reason=degraded_closed`，`retry_after=1`），
  优先保护下游。

限流组件自身出现任何未预期异常时，中间件一律放行并记录日志，
服务整体不因限流组件故障而不可用。

### 白名单与热更新

- `whitelisted_identities` / `whitelisted_endpoints` 中的来源或接口完全跳过限流。
- 所有配置（规则、阈值、策略、白名单、封禁、降级模式）均可通过配置文件修改，
  `ConfigReloader` 基于文件 mtime 检测变更，**无需重启服务即生效**；
  配置解析失败时沿用上一份可用配置。

## 配置项说明

| 字段 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `rules[]` | 数组 | `[]` | 限流规则，按声明顺序匹配，命中第一条 |
| `rules[].name` | 字符串 | 必填 | 规则名 |
| `rules[].identity` | 字符串 | `"*"` | 匹配的来源身份，`"*"` 匹配任意 |
| `rules[].endpoint` | 字符串 | `"*"` | 匹配的接口路径，`"*"` 匹配任意 |
| `rules[].strategy` | 字符串 | `"fixed_window"` | `fixed_window` / `sliding_window` |
| `rules[].limit` | 整数 | `100` | 窗口内允许的最大请求数 |
| `rules[].window_seconds` | 浮点 | `60` | 窗口长度（秒） |
| `default_rule` | 对象 | 同上默认 | 未命中任何规则时使用的兜底规则 |
| `ban.enabled` | 布尔 | `true` | 是否启用临时封禁 |
| `ban.violation_threshold` | 整数 | `5` | 触发封禁的违规次数 |
| `ban.violation_window_seconds` | 浮点 | `60` | 违规统计窗口（秒） |
| `ban.ban_seconds` | 浮点 | `300` | 封禁时长（秒） |
| `degrade_mode` | 字符串 | `"fail_open"` | `fail_open` / `fail_closed` |
| `whitelisted_identities` | 数组 | `[]` | 豁免限流的来源身份 |
| `whitelisted_endpoints` | 数组 | `[]` | 豁免限流的接口路径 |

## 并发一致性

计数通过存储后端的 `apply(key, fn)` 原子"读-改-写"完成；内置
`InMemoryBackend` 使用分条锁（striped lock），同一 key 的判定串行化、
不同 key 互不阻塞，高并发下不出现额度穿透或计数错乱，也不会形成全局锁瓶颈。
存储后端可替换（实现 `traffic_guard.storage.StorageBackend` 接口），
后端故障时按降级策略处理。

## 测试

```bash
python -m pytest tests/ -q
```

覆盖：窗口边界（固定/滑动）、并发竞争（额度不穿透）、二维额度隔离、
封禁与自动解封、存储降级（fail-open / fail-closed）、白名单与配置热更新、
429 响应形态与中间件异常兜底。
