# Provider 模块化与结构化错误

## 目标

将单体 `scripts/model_provider.py` 拆分为可独立测试的客户端、组合路由和工厂，
同时保持现有导入与 eBay 使用的 `call_text` / `call_vision` /
`call_image_gen` 接口兼容。Amazon 图片安全门使用 `assess_image` 结构化接口。

## 接口设计

- `providers.errors`
  - `ProviderError`：包含 provider、operation、HTTP 状态、是否可重试。
  - `ProviderAuthError`、`ProviderQuotaError`、`ProviderRateLimitError`、
    `ProviderTimeoutError`、`ProviderUnavailableError`、
    `ProviderResponseError`。
- `providers.base`
  - `ModelProvider` 抽象接口、`assess_image` 结构化图审接口、HTTP 错误分类
    和脱敏尝试记录。
- `providers.deepseek`、`providers.agnes`、`providers.gpt_image`
  - 只负责各自 API 协议、解析和单 Provider 重试。
- `providers.composite`
  - 负责功能路由、跨 Provider 回退、指标和熔断。
- `providers.factory`
  - 负责生效配置映射和线程安全单例。
- `scripts.model_provider`
  - Provider 的稳定门面；内部实现只使用包相对导入。

## 数据流

```text
.env + ModelRegistry
        ↓
provider factory
        ↓
CompositeProvider ──> DeepSeek / Agnes / GPT Image
        │                       │
        └──── metrics <── typed ProviderError
```

## 兼容边界

- 唯一受支持导入为 `from scripts.model_provider import ...`，禁止把
  `scripts/` 目录单独加入 `sys.path` 后再导入顶层 `model_provider`。
- `ProviderQuotaError` 保留原名称和 `RuntimeError` 继承关系。
- 文本返回 `str`，生图返回 URL `str`。
- `call_vision -> bool | None` 只为 eBay 和外部旧调用方保留。
- Amazon 必须调用 `assess_image -> structured record | None`；缺少该接口或
  响应不合约时视为 `unknown`，不能回退到布尔接口放行。
- 图片路由遇到任一结构化 Provider 错误时继续尝试配置的下一个回退。
- 不在异常、日志或指标中记录 API Key、请求头、完整 Prompt 或图片 URL。

## 错误策略

- 鉴权、额度错误立即终止当前 Provider 的内部重试。
- 限流、超时和上游不可用按 Provider 既有策略重试；耗尽后抛出对应错误。
- 2xx 但缺少预期字段视为 `ProviderResponseError`。
- Composite 记录错误类型；是否降级为原值由上层业务阶段决定。
