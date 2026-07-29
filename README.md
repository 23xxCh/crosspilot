# CrossPilot — 跨境电商 Listing 清洗平台

eBay/Amazon → TikTok Shop / Amazon 回填表 全自动清洗。

```
采集表 .xlsx / Amazon .json → 结构化图片安全门 → 风险图删除/修复/隔离 → 翻译/优化 → 描述清洗 → 按原格式回填
```

## 快速开始

```bash
# 安装
pip install uv && uv sync

# 复制 .env.example 为 .env，填入 API Key

# 启动 Web 平台
uv run uvicorn web.app:app --port 8765

# 统一命令行
uv run python -m crosspilot run "表3.xlsx"                    # 自动识别平台
uv run python -m crosspilot run "亚马逊表/采集表.json"         # 正式处理
uv run python -m crosspilot audit "亚马逊表/回填表.json"       # 只读审图并生成终审包
uv run python -m crosspilot review "回填表.json" "终审包"      # 仅生成终审包
uv run python -m crosspilot apply "回填表.json" "审核决定.json" --dry-run
```

`scripts/audit_amazon_image_safety.py`、
`scripts/export_amazon_cn_review.py` 和
`scripts/apply_amazon_review_decisions.py` 仍可直接运行，但只作为统一 CLI
的兼容入口；新功能和参数统一添加到 `crosspilot.cli`。

内部导入统一使用完整包名，例如
`from scripts.model_provider import get_provider`。生产代码、Web 和测试不得
自行修改 `sys.path`；仅 `scripts/_bootstrap.py` 为旧
`python scripts/x.py` 启动方式提供集中兼容。

## 配置模型与 Prompt

敏感配置统一放在 `.env`：

```dotenv
DEEPSEEK_KEY=sk-...
AGNES_KEY=cpk-...
MODEL_PROFILE=production
```

模型、端点和回退链统一定义在
[`crosspilot/model_profiles.json`](crosspilot/model_profiles.json)。Web 设置页可修改
当前 Provider 和精确模型 ID，保存后会热重载。业务 Prompt 统一位于
[`crosspilot/prompts/`](crosspilot/prompts/)；修改 Prompt 或模型后，相关缓存签名会
自动变化，不会继续使用旧结果。

配置优先级为：`CROSSPILOT_*` 系统环境变量 > `.env` > 旧
`keys.json` > 配置档默认值。`keys.json` 仅保留只读兼容。

### 支持的提供商

| 提供商 | text_provider | vision_provider | image_gen_provider |
|--------|--------------|-----------------|-------------------|
| DeepSeek | ✅ | ❌ | ❌ |
| Agnes | ✅ | ✅ | ✅ |
| GPT Image | ❌ | ❌ | ✅ |

## 架构

```
scripts/
├── model_provider.py        # Provider 稳定门面
├── providers/               # 独立客户端、路由、工厂和结构化错误
├── dmx_client.py            # 兼容层（包装 model_provider）
├── process_ebay_tk.py       # eBay 管道入口
├── process_amazon.py        # Amazon 阶段顺序与兼容 Adapter
├── review_package/          # 终审包翻译、图片、数据、HTML 与导出编排
├── export_amazon_cn_review.py # 旧导出脚本兼容 Adapter
├── pipelines/
│   ├── amazon_image_safety/   # 审图、缓存、修复与安全门 Interface
│   ├── amazon_runtime.py      # 单次运行上下文、状态与阶段统计
│   ├── amazon_delivery.py     # 输出验收、指标、隔离与终审交付
│   ├── amazon_text/           # 标题、描述、Bullet/关键词深 Module
│   ├── amazon_quality/        # 审计、事实保护、规范与输出验收策略
│   ├── amazon_constants.py    # 旧质量规则名称兼容 Adapter
│   ├── amazon_stages.py       # 旧文本阶段名称兼容 Adapter
│   └── ...                    # 其余 eBay/Amazon 阶段
├── services/                # 抽象层
└── adapters/                # 表格格式适配器

web/                       FastAPI + vanilla JS SPA
tests/                     单元、回归与 Web API 测试

crosspilot/
├── cli.py                  # 运行、审计、终审和决定应用的统一命令接口
├── model_profiles.json    # 模型、端点和回退链
├── model_registry.py      # 模型配置校验与解析
├── prompt_registry.py     # Prompt 加载、渲染与签名
└── prompts/               # 可版本控制的业务 Prompt
```

## 管道阶段

### eBay→TikTok (10 阶段)
1. 读取数据到内存 2. Agnes 图审（水印/品牌/人物）3. 主图/变种清除人物 4. 问题附图清空
5. 标题翻译 6. 描述清洗 7. 描述翻译 8. 嵌入图片
9. 视频清空 10. 价格改名+保存

### Amazon→回填表 (6 阶段)
1. 读取表格 2. 结构化图片初审、风险修复与隔离 3. 标题优化
4. 描述清洗 5. Bullet+关键词 6. 写回填表

Amazon 附图风险或无法判断时直接删除；主图/变种图修复失败、复审仍有风险
或无法判断时隔离整个商品，不把风险原图写入正式回填表。每次任务另行生成
离线终审包，供人工确认、纠错和导出审核决定。

## 配置

| 文件 | 说明 |
|------|------|
| `.env` | API Key、当前配置档和临时模型覆盖（.gitignore 保护） |
| `.env.example` | 无敏感值的配置模板 |
| `crosspilot/model_profiles.json` | 模型、端点、参数和回退链 |
| `crosspilot/prompts/` | Amazon/eBay/图片业务 Prompt |
| `keys.json` | 旧版本配置，只读兼容 |
| `data/` | 上传、缓存、日志 |
| `.gitignore` | 排除敏感文件和运行时产物 |

### API 密钥配置

复制 `.env.example` 为 `.env` 后填写 `DEEPSEEK_KEY` 和
`AGNES_KEY`，或直接在 Web 设置页保存。

- **DeepSeek**: 用于文本翻译、描述清洗、Bullet/关键词生成
- **Agnes**: 用于图审（检测水印/人物）和图生图（去水印/去人物）

## Docker

```bash
docker compose up -d
# Web 平台: http://localhost:8765
```

Compose 默认只监听本机。如需额外访问控制，设置
`CROSSPILOT_AUTH_PASSWORD`，用户名默认为 `crosspilot`，也可通过
`CROSSPILOT_AUTH_USER` 修改。

可通过环境变量调整运行保护：

| 环境变量 | 默认值 | 说明 |
|------|------:|------|
| `CROSSPILOT_MAX_UPLOAD_MB` | `50` | 单文件上传上限 |
| `CROSSPILOT_MAX_BATCH_FILES` | `20` | 单次批量上传文件数上限，最大为 100 |
| `CROSSPILOT_MAX_ROWS` | `10000` | 单个表格商品行数上限 |
| `CROSSPILOT_MAX_WORKERS` | `2` | 并行管道数，最大为 4 |
| `CROSSPILOT_RETENTION_DAYS` | `7` | 启动时清理多少天前的终态任务；设为 `0` 禁用 |
| `CROSSPILOT_ALLOWED_HOSTS` | `127.0.0.1,localhost,[::1],::1,testserver` | 允许访问 Web 服务的 Host 列表 |
| `CROSSPILOT_ALLOWED_ORIGINS` | 空 | 反向代理或远程部署时额外允许的 Origin 列表 |

## 测试

```bash
uv run pytest tests/ -v                   # 默认离线测试
uv run pytest tests/ -v -m "not network"  # 显式运行离线测试
uv run pytest tests/test_pipeline.py -m network -k test_pipeline_with_sample
```

`.github/workflows/canary.yml` 每周运行一次真实 API 全链路探针；需在
GitHub Actions 配置 `CROSSPILOT_DEEPSEEK_KEY` 和 `CROSSPILOT_AGNES_KEY`。

## 打包

```bash
uv run pyinstaller --clean --noconfirm CrossPilot.spec
```

版本只在 `crosspilot/version.py` 中维护。推送 `v*` 标签前可运行：

```bash
uv run python scripts/release_preflight.py --tag v2.0.0 --require-tracked
```

正式发布还需要 GitHub Actions secrets `WINDOWS_CERT_BASE64` 与
`WINDOWS_CERT_PASSWORD`。发布产物会进行 Authenticode 签名，内置更新器只接受
与当前程序签名证书一致且 SHA-256 校验通过的新版本。

## License

Internal use.
