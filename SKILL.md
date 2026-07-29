---
name: "crosspilot-listing-clean"
description: "CrossPilot 的 eBay→TikTok 与 Amazon 回填表清洗、图审、生图、翻译和质检工作流"
---

# CrossPilot Listing 清洗

CrossPilot 处理两类输入：

- eBay 来源 XLSX → TikTok Shop 越南站清洗表；
- Amazon 采集 XLSX/列式 JSON → Amazon 回填表或回填 JSON。

原文件不修改。运行产物包括清洗输出、状态、指标和缓存；存在质量风险时任务
进入“待人工复核”而不是静默标记成功。

## 推荐入口

```powershell
# Web
uv run uvicorn web.app:app --port 8765

# 统一 CLI
uv run python -m crosspilot run "<输入文件>"

# 兼容脚本
uv run python scripts/process_ebay_tk.py "<eBay.xlsx>"
uv run python scripts/process_amazon.py "<Amazon.xlsx|json>"
```

## 配置

- API Key、活动配置档和临时模型覆盖：`.env`
- 配置模板：`.env.example`
- 模型、端点、参数和回退链：`crosspilot/model_profiles.json`
- 业务 Prompt：`crosspilot/prompts/`
- 旧 `keys.json`：只读兼容，不再作为 Web 设置写入目标

优先级：

```text
CROSSPILOT_* 系统环境变量 > .env > 旧 keys.json > model profile / 静态默认值
```

Web `/api/settings` 保存到 `.env`，随后清理配置和 Provider 单例。不要把密钥
写入模型注册表、Prompt、日志、测试夹具或提交记录。

## 修改模型

常规切换可在 Web 设置页修改精确模型 ID。需要调整端点、参数或完整回退链时，
修改 `crosspilot/model_profiles.json`：

```json
{
  "active_profile": "production",
  "profiles": {
    "production": {
      "text": {"provider": "deepseek", "base_url": "...", "model": "..."},
      "vision": {"provider": "agnes", "base_url": "...", "model": "..."},
      "image": {
        "provider": "agnes",
        "base_url": "...",
        "model": "...",
        "fallbacks": [
          {"provider": "agnes", "base_url": "...", "model": "..."},
          {"provider": "gpt", "base_url": "...", "model": "..."}
        ]
      }
    }
  }
}
```

不要在 Provider 类、健康检查或 CLI 中再次硬编码模型 ID。

## 修改 Prompt

按业务操作修改 `crosspilot/prompts/` 中的模板。模板由
`crosspilot.prompt_registry` 统一加载、变量校验和签名：

- `amazon/`：标题、描述、Bullet/关键词；
- `ebay/`：eBay 描述清洗；
- `translation/`：单条和批量翻译；
- `images/`：图审、主图、变种图和人物移除；
- `system/`：Provider 系统指令。

Prompt、模型路由或图片策略变化时，Amazon/eBay 相关缓存签名自动变化。
不要手工复制 Prompt 到 Provider 或管道模块。

## 关键实现边界

- `scripts/model_provider.py`：旧导入兼容门面；
- `scripts/providers/`：API 客户端、结构化错误、指标、熔断和回退；
- `crosspilot/model_registry.py`：模型配置校验与解析；
- `crosspilot/prompt_registry.py`：Prompt 加载、渲染和运行时签名；
- `scripts/pipelines/amazon_*`：Amazon 阶段；
- `scripts/pipelines/ebay_*`：eBay 阶段；
- `scripts/services/`：翻译、图审和共享规则；
- `web/`：FastAPI 与前端；
- `tests/`：离线回归测试。

Provider 不应持有业务 Prompt 的唯一副本，管道不应拼接 Provider 端点。

## 安全与交付

- `.env`、`keys.json`、缓存、状态、日志和生成图片必须保持忽略；
- 不输出或回显密钥明文；
- 不覆盖输入文件；
- 修改配置、Prompt 或缓存规则后运行对应定向测试，再运行完整离线测试；
- 真实 API 试跑必须使用小样本，并保留原图作为失败回退。

## 验证

```powershell
python -m pytest tests -q
python -m crosspilot config
python -m crosspilot health
```

真实 API 健康检查和试跑会产生费用或消耗配额，只有用户明确要求时才执行。
