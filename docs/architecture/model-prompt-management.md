# 统一模型与 Prompt 管理

## 目标

让模型、端点、回退链和业务 Prompt 各有一个可信来源；修改后可热重载，
并自动使相关缓存失效，同时保持现有 CLI、Web 和流水线调用接口兼容。

## 接口设计

- `crosspilot.model_registry.ModelRegistry`
  - 输入：`crosspilot/model_profiles.json`、活动配置档名称。
  - 输出：文本、图审、生图模型目标和生图回退链。
- `crosspilot.prompt_registry.PromptRegistry`
  - 输入：Prompt ID、模板变量。
  - 输出：模板原文、渲染文本、内容签名。
- `crosspilot.config.save_env_values`
  - 输入：允许持久化的配置键值。
  - 输出：原子更新后的 `.env`，随后统一重载配置与 Provider。

## 数据流

```text
model_profiles.json ─┐
.env / 系统环境变量 ─┼─> effective config ─> ModelRegistry ─> Provider
keys.json（只读兼容）─┘

prompt files ─> PromptRegistry ─> pipeline/provider
                         └──────> cache signature
```

## 配置边界

- API Key 只进入 `.env` 或系统环境变量，不进入模型注册表。
- `model_profiles.json` 只存非敏感的模型、端点、参数和回退关系。
- Prompt 按业务操作命名，不按 Provider 复制；Provider 只负责 API 协议。
- 系统环境变量优先级最高，随后是 `.env`、旧 `keys.json`、默认配置档。
- Web 设置写入同一个 `.env`，并在保存后清理配置和 Provider 缓存。

## 缓存策略

缓存签名由以下内容自动计算：

- 策略版本；
- 当前业务 Prompt 内容；
- 当前模型配置档及模型路由。

修改 Prompt、模型或策略中的任一项都会自动使旧缓存失效。

## 兼容与影响范围

- 保留 `AGNES_MAIN_PROMPT`、`TITLE_OPTIMIZE_PROMPT` 等现有常量名。
- 保留 `CompositeProvider(config)` 和现有 `call_*` 方法签名。
- `keys.json` 继续可读，但不再是 Web 设置的写入目标。
- Amazon/eBay 输入、输出 JSON/XLSX 格式不变。
