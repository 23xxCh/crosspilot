# Agnes 503 快速拥塞控制

## 目标

Agnes 健康时维持现有高并发吞吐；上游返回 503 时，不再让每张图片执行最长
22 分钟的同步重试，而是在一次短暂随机退避后快速切换回退模型。

## 接口设计

- 输入：Agnes HTTP 结果、业务操作、模型 ID、当前时间。
- 输出：允许请求、短暂重试、快速失败进入回退链，或半开探测。
- 配置：
  - `AGNES_503_RETRY_LIMIT`：单次调用最多快速重试次数，默认 1；
  - `AGNES_503_BACKOFF_MIN_S` / `MAX_S`：随机等待区间，默认 3–8 秒；
  - `AGNES_503_CIRCUIT_THRESHOLD`：连续拥堵阈值，默认 3；
  - `AGNES_503_CIRCUIT_COOLDOWN_S`：熔断冷却，默认 120 秒。

## 数据流

```text
请求 Agnes 主模型
  ├─ 2xx ─> 清除拥堵状态 ─> 返回结果
  └─ 503 ─> 记录模型拥堵
             ├─ 首次且未熔断 ─> 随机等待 3–8 秒 ─> 快速重试一次
             └─ 再次 503/已熔断 ─> ProviderUnavailableError
                                      └─ CompositeProvider 立即走下一模型

冷却结束 ─> 只允许一个半开探测请求
  ├─ 成功 ─> 恢复正常
  └─ 503/网络失败 ─> 重新冷却
```

## 与现有代码的关系

- `scripts/providers/congestion.py`：与 HTTP 客户端解耦的线程安全状态机。
- `scripts/providers/agnes.py`：在图审和生图请求前后接入拥塞门。
- `scripts/providers/composite.py`：继续使用既有 Agnes 2.1 → 2.0 → GPT 回退链。
- `scripts/concurrency.py`：整批失败时继续负责并发减半和缓慢恢复。

429 仍按限流处理，503 才进入快速拥塞策略。鉴权、余额不足等终止错误不进入
重试或回退等待。
