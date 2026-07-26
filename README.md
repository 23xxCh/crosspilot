# CrossPilot — 跨境电商 Listing 清洗平台

eBay/Amazon → TikTok Shop / Amazon 回填表 全自动清洗。

```
采集表 .xlsx → AI 图审 → AI 生图去水印 → 翻译越南语/优化标题 → 描述清洗 → Bullet Point → 关键词 → 回填表
```

## 快速开始

```bash
# 安装
pip install uv && uv sync

# 配置 API key（注册 https://www.dmxapi.cn 获取）
echo '{"dmx_key": "sk-..."}' > keys.json

# 启动 Web 平台
uv run uvicorn web.app:app --port 8765

# 或命令行
uv run python scripts/process_ebay_tk.py "表3.xlsx"          # eBay→TikTok
uv run python scripts/process_amazon.py "亚马逊表/采集表.xlsx"  # Amazon→回填表
```

## 架构

```
scripts/
├── process_ebay_tk.py     eBay 管道 (12 阶段函数)
├── process_amazon.py      Amazon 管道 (7 阶段函数)
├── services/              抽象层
│   ├── review.py          ImageReviewService (水印检测)
│   ├── translate.py       TranslationService (翻译/清洗)
│   └── generate.py        ImageGenService (图生图)
├── adapters/              表格格式适配器
│   ├── ebay_tk.py         eBay 45 列模板
│   └── amazon_tk.py       Amazon 7 列采集表
├── dmx_client.py          DMXAPI 底层
└── pipeline_log.py        结构化日志

web/                       FastAPI + vanilla JS SPA
tests/                     26+ 单元测试
```

## 管道阶段

### eBay→TikTok (10 阶段)
1. 读取数据到内存 2. MiMo图审 3. 图生图 4. 附图清空
5. 标题翻译 6. 描述清洗 7. 描述翻译 8. 嵌入图片
9. 视频清空 10. 价格改名+保存

### Amazon→回填表 (7 阶段)
1. 读取数据 2. 图审 3. 图生图 4. 标题优化
5. 描述清洗 6. Bullet+关键词 7. 写回填表

## 配置

| 文件 | 说明 |
|------|------|
| `keys.json` | DMXAPI key（.gitignore 保护） |
| `keys.example.json` | 参考模板 |
| `data/` | 上传、缓存、日志 |
| `.gitignore` | 排除敏感文件和运行时产物 |

## Docker

```bash
docker compose up -d
# Web 平台: http://localhost:8765
```

## 测试

```bash
uv run pytest tests/ -v                  # 全部
uv run pytest tests/ -v -m "not network" # 不需网络
```

## 打包

```bash
uv run pyinstaller --onefile --name CrossPilot \
  --add-data "web/static:web/static" \
  --add-data "scripts:scripts" \
  --add-data "keys.example.json:." main_cli.py
```

## License

Internal use.
