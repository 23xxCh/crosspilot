# CrossPilot — 跨境电商 Listing 清洗平台

eBay/Amazon → TikTok Shop / Amazon 回填表 全自动清洗。

```
采集表 .xlsx / Amazon .json → AI 图审 → 删除含人物的问题附图 → 主图/变种去水印和人物 → 翻译/优化 → 描述清洗 → 按原格式回填
```

## 快速开始

```bash
# 安装
pip install uv && uv sync

# 按 keys.example.json 创建 keys.json，配置 provider 和 key

# 启动 Web 平台
uv run uvicorn web.app:app --port 8765

# 或命令行
uv run python scripts/process_ebay_tk.py "表3.xlsx"          # eBay→TikTok
uv run python scripts/process_amazon.py "亚马逊表/采集表.xlsx"  # Amazon→回填表
uv run python scripts/process_amazon.py "亚马逊表/采集表.json"  # Amazon JSON→回填 JSON
```

## 配置模型提供商

在 `keys.json` 中配置：

```json
{
  "text_provider": "deepseek",
  "vision_provider": "agnes",
  "image_gen_provider": "agnes",
  "deepseek_key": "sk-...",
  "agnes_key": "cpk-..."
}
```

**换模型只需改配置，代码完全不用动！**

部署环境也可使用 `CROSSPILOT_DEEPSEEK_KEY` 与
`CROSSPILOT_AGNES_KEY`；环境变量优先于 `keys.json`。

### 支持的提供商

| 提供商 | text_provider | vision_provider | image_gen_provider |
|--------|--------------|-----------------|-------------------|
| DeepSeek | ✅ | ❌ | ❌ |
| Agnes | ✅ | ✅ | ✅ |

## 架构

```
scripts/
├── model_provider.py        # 统一模型提供商接口（配置驱动）
├── dmx_client.py            # 兼容层（包装 model_provider）
├── process_ebay_tk.py       # eBay 管道入口
├── process_amazon.py        # Amazon 管道
├── pipelines/               # eBay 阶段编排
├── services/                # 抽象层
├── adapters/                # 表格格式适配器
└── image_gen.py             # 独立图生图脚本

web/                       FastAPI + vanilla JS SPA
tests/                     单元、回归与 Web API 测试
```

## 管道阶段

### eBay→TikTok (10 阶段)
1. 读取数据到内存 2. Agnes 图审（水印/品牌/人物）3. 主图/变种清除人物 4. 问题附图清空
5. 标题翻译 6. 描述清洗 7. 描述翻译 8. 嵌入图片
9. 视频清空 10. 价格改名+保存

### Amazon→回填表 (6 阶段)
1. 读取表格 2. 审图+生图 3. 标题优化
4. 描述清洗 5. Bullet+关键词 6. 写回填表

如果存在未完成图审、生图失败或必填内容缺失，输出仍会保留用于人工修正，
但任务状态为“待复核”，不会计入成功任务。

## 配置

| 文件 | 说明 |
|------|------|
| `keys.json` | DeepSeek / Agnes key（.gitignore 保护） |
| `keys.example.json` | 参考模板 |
| `data/` | 上传、缓存、日志 |
| `.gitignore` | 排除敏感文件和运行时产物 |

### API 密钥配置

在 `keys.json` 中配置：

```json
{
  "deepseek_key": "sk-你的DeepSeek密钥",
  "agnes_key": "cpk-你的Agnes密钥"
}
```

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
uv run pyinstaller --onefile --name CrossPilot \
  --add-data "web/static:web/static" \
  --add-data "scripts:scripts" \
  --add-data "keys.example.json:." main_cli.py
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
