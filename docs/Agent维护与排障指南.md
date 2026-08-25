# Agent 维护与排障指南

## 1. 边界

项目是单任务 Agent Skill，不是服务器系统。一个 Agent 任务处理一个明确的 Amazon JSON 文件；多个文件按顺序逐个调用确定性入口。

稳定接口：

```python
from amazon_processor import process_json
result = process_json("采集表.json")
```

```powershell
uv run python -m amazon_processor run "采集表.json"
uv run python scripts/process_amazon_json.py "采集表.json"
```

不要恢复 Worker、任务 API、收件箱监听、状态网页、Agnes、Ollama、GPT Image 或生图实现。

## 2. 调用链

```text
scripts/process_amazon_json.py
  → pipeline.process_json
  → schema 只读加载和契约校验
  → images.gate URL 去重、DeepSeek 审图、删图、原图选主图
  → text 标题、描述、副标题、Bullet、关键词和本地化
  → delivery 行隔离、14 字段验收、终审包、原子发布
  → result.json 机器摘要与二次验收
```

## 3. 主要修改入口

| 需求 | 模块 | 相关测试 |
|---|---|---|
| 输入字段、行错位、14字段 | `schema.py` | `test_amazon_json.py` |
| 阶段编排、缓存、发布 | `pipeline.py`、`delivery.py` | `test_amazon_pipeline.py`、`test_amazon_runtime.py` |
| 标题/描述/Bullet/关键词 | `text/`、对应 Prompt | `test_amazon_text.py`、`test_amazon_descriptions.py` |
| 多站点语言 | `markets.py`、`text/localization.py` | `test_amazon_localization.py` |
| 图片误判、主图选择 | `images/risk.py`、`images/gate.py` | `test_amazon_image_safety.py` |
| DeepSeek 请求、错误分类 | `providers/deepseek.py`、`providers/support.py` | `test_provider_errors.py` |
| Skill 发现与机器摘要 | `SKILL.md`、`scripts/process_amazon_json.py` | `test_skill_runner.py` |
| 终审包/审核决定 | `review/` | `test_review_package.py`、`test_amazon_review_decisions.py` |

## 4. 配置唯一来源

- `.env`：只保存 `DEEPSEEK_KEY`。
- `config/settings.json`：官方 DeepSeek 文案模型、视觉模型、Endpoint、并发和重试。
- `config/prompts/manifest.json`：Prompt ID、文件和变量。
- `config/prompts/**/*.txt`：全部 AI 指令正文。

Python 只传事实变量，不添加隐藏指令。Prompt 或模型变化必须进入缓存签名。

## 5. 图片规则

- 普通安全审查覆盖主图、附图和变种图，按标准化 URL 去重。
- `risk` 删除；Provider 临时故障形成的 `unknown` 必须中止本次运行，不能当成质量失败删图发布。
- 只有普通审查 `safe` 的产品图参加主图资格检查。
- 白底清晰图优先，同等级按源顺序选择。
- 主图和至少一张产品附图缺一不可；变种图不能代替产品附图。
- 正式代码没有图片生成操作，指标 `generation_requests` 必须为 0。

## 6. 故障处理

| 故障 | 行为 |
|---|---|
| 401/403 | 立即停止，检查 `.env` 密钥和官方 Endpoint |
| 余额/额度 | 立即停止，保留缓存和上一版正式表 |
| 429/5xx/网络/超时 | Provider 有限重试；Agent 入口再有限续跑一次 |
| 响应 JSON 不符合契约 | 停止并报告，不把未知图片放行 |
| 单行文案不合格 | `unattended=True` 时隔离该行，其余正常行继续 |
| 全批无可发布商品 | 生成待审核包，不覆盖 `02_处理结果/最新` |

不要清空 `.runtime/cache` 来解决临时 Provider 故障。已完成字段和图片结果用于断点续跑并避免重复付费。

## 7. 修改流程

1. `git status --short`，保留用户已有文件。
2. 若有 `.codegraph/`，先用 `codegraph explore` 定位调用链。
3. 写能复现问题的离线测试。
4. 做最小修改，运行相关测试。
5. 运行 Ruff、Pyright、完整 pytest 和 Skill quick validate。
6. 只有用户明确授权时才做真实 DeepSeek 冒烟。
7. 最终核对输入哈希、发布状态、14 字段顺序、数组等长、ID/站点顺序、问题 ID 互斥、主图和附图、请求统计。

## 8. 完成定义

- `result.json` 显示真实发布状态，而不是只看退出码。
- 输入 SHA256 不变。
- 正式输出通过 14 字段和图片业务规则验收。
- 失败时上一版正式表未被覆盖。
- 离线测试、Ruff、Pyright、Skill 校验全部通过。
- 文档明确说明是否产生付费请求。
