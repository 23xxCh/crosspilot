# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目概述

CrossPilot - 跨境电商 Listing 全自动清洗平台。从 eBay/Shopee/Amazon 导入产品表格，AI 自动翻译越南语、去水印/品牌/Logo、生图替换、图片注入描述，输出 TikTok Shop 越南站合规表格。

## 命令

```bash
# === 统一 CLI（推荐）===
uv run python -m crosspilot ebay "输入.xlsx"        # eBay→TikTok 清洗
uv run python -m crosspilot amazon "输入.xlsx"      # Amazon 采集表回填
uv run python -m crosspilot audit "回填表.json"      # 只读图片审计+终审包
uv run python -m crosspilot review "回填表.json" "终审包"
uv run python -m crosspilot apply "回填表.json" "审核决定.json" --dry-run
uv run python -m crosspilot web -p 8765             # Web 管理平台

# === 旧式直接调用（兼容）===
uv run python -u scripts/process_ebay_tk.py "<输入.xlsx>"
uv run python -u scripts/process_amazon.py "<输入.xlsx>"

# Web 平台
uv run uvicorn web.app:app --port 8765

# 造迷你测试表（从全表截前 N 行，省 API 额度）
uv run python -u tests/make_sample.py <行数>

# 打包 exe
uv run pyinstaller --clean --noconfirm CrossPilot.spec
```

## 架构

```
scripts/
├── model_provider.py        # Provider 稳定门面
├── providers/               # Provider 客户端、路由、工厂和结构化错误
├── dmx_client.py            # 兼容层（包装 model_provider，保持旧接口）
├── process_ebay_tk.py       # eBay 管道入口
├── process_amazon.py        # Amazon 阶段顺序与兼容 Adapter
├── review_package/          # 终审包深 Module
│   ├── translation.py       # 中文翻译与可续跑缓存
│   ├── assets.py            # 图片下载、解码与共享缓存
│   ├── rows.py              # 正式/隔离商品终审数据模型
│   ├── html_renderer.py     # 离线审核交互页面
│   └── exporter.py          # 导出编排
├── export_amazon_cn_review.py # 旧脚本兼容 Adapter
├── pipelines/               # eBay/Amazon 阶段实现
│   ├── ebay_shared.py       # eBay 共享模块（使用 model_provider）
│   ├── ebay_stages.py       # eBay 阶段函数
│   ├── amazon_quality/      # Amazon 质量策略深 Module
│   │   ├── rules.py         # 清洗正则、事实规格与文本指纹
│   │   ├── audit.py         # 逐行审计、降级问题与复核摘要
│   │   ├── listing.py       # Bullet/关键词确定性规范
│   │   └── validation.py    # 正式 Amazon 行验收
│   ├── amazon_constants.py  # 旧质量规则名称兼容 Adapter
│   ├── amazon_io.py         # Amazon 输入、输出与交付校验
│   ├── amazon_text/         # Amazon 文本阶段深 Module
│   │   ├── titles.py        # 标题规范、流量优化与事实保护
│   │   ├── descriptions.py  # 描述清理、事实保护与脏数据隔离
│   │   └── listing_content.py # Bullet、关键词生成与规则补全
│   ├── amazon_stages.py     # 旧文本阶段名称兼容 Adapter
│   ├── amazon_image_safety/   # Amazon 图片安全深 Module
│   │   ├── assessment.py      # 结构化审图与 URL/图片解码验证
│   │   ├── cache.py           # 策略签名缓存与人工覆盖
│   │   ├── remediation.py     # 生图、下载验证和生成图复审
│   │   └── gate.py            # fail-closed 隔离决策与指标编排
│   ├── amazon_runtime.py    # 运行上下文、状态与阶段统计
│   └── amazon_delivery.py   # 正式输出、隔离、指标与终审交付
├── services/                # 抽象层
│   ├── review.py            # 图审服务（使用 model_provider）
│   ├── translate.py         # 翻译服务（使用 model_provider）
│   └── constants.py         # 品牌词与共享规则
└── adapters/                # 表格格式适配器
```

`crosspilot/cli.py` 是用户命令的唯一参数解析入口。
Amazon 生产调用统一使用 `process_amazon.run_amazon_pipeline()`；
`_main()` 和 `_main_impl()` 仅为旧调用方保留。
Amazon 文本生产调用统一使用 `scripts.pipelines.amazon_text` Interface；
不要在 `amazon_stages.py` 兼容 Adapter 中添加新逻辑。
Amazon 质量规则生产调用统一使用 `scripts.pipelines.amazon_quality`
Interface；不要在 `amazon_constants.py` 兼容 Adapter 中添加新逻辑。
Amazon 图片生产调用统一使用
`scripts.pipelines.amazon_image_safety` Interface；调用方不得绕过
`gate.py` 的 fail-closed 隔离决策。
`audit_amazon_image_safety.py`、`export_amazon_cn_review.py` 和
`apply_amazon_review_decisions.py` 的 `main()` 只是兼容 Adapter；不要在这些
脚本中新增命令参数，应修改 `crosspilot/cli.py`。

所有内部导入必须使用 `scripts.*` 完整包名或包内相对导入。不要修改
`sys.path`，也不要恢复顶层 `model_provider`、`pipelines`、`services`
等别名。仅 `scripts/_bootstrap.py` 可以为旧文件式入口调整导入路径。

## 核心管道 `process_ebay_tk.py`

**10 阶段**：提取URL → 图审 → 图生图 → 附图清空 → 标题翻译 → 描述AI清洗 → 描述翻译 → 嵌入注入图片 → 视频模板清理 → 价格列保存

**API 调用层**（使用 model_provider，配置驱动）：
```python
from scripts.model_provider import get_provider

provider = get_provider()  # 自动加载生效配置和模型路由

# 文本生成（由 text_provider 配置决定）
text = provider.call_text(prompt)

# 图审（由 vision_provider 配置决定）
needs_fix = provider.call_vision(image_url)

# 图生图（由 image_gen_provider 配置决定）
new_url = provider.call_image_gen(image_url)
```

**配置方式**：
- 密钥与临时覆盖：`.env`（参考 `.env.example`）
- 模型、端点和回退链：`crosspilot/model_profiles.json`
- 业务 Prompt：`crosspilot/prompts/`
- 优先级：`CROSSPILOT_*` 系统环境变量 > `.env` > 旧 `keys.json` > 配置档

**并发配置**：
- `REVIEW_CONCURRENCY = 100` - 图审
- `TEXT_CONCURRENCY = 20` - 文本批次并发
- `GEN_CONCURRENCY = 15` - 生图
- `MAX_RETRIES = 8` - API 调用内部重试

**图审**：配置驱动的 vision_provider

**生图**：配置驱动的 image_gen_provider

**文本**：配置驱动的 text_provider

## Web 层

**API 端点**：`/api/dashboard` `/api/upload/batch` `/api/tasks` `/api/tasks/{id}` `/api/tasks/{id}/rows` `/api/tasks/{id}/events`(SSE) `/api/tasks/{id}/download` `/api/settings` `/api/templates` `/api/version`

**任务执行**：
- Dev 模式：`subprocess.Popen` 调 `web/runner.py`（进程隔离，真并发），`_pending` 等待队列
- Exe 模式：当前 exe 以 `--run-job` 参数启动独立子进程
- Monitor 线程：每秒轮询 `_status.json` 推进度 + 探活，支持 frozen/dev 双模式

**SSE 推送**：每 3 秒无条件推送进度（心跳）+ 30 分钟最大连接时间

**PyInstaller 兼容**：
- `CROSSPILOT_DATA_DIR` / `CROSSPILOT_ENV` 环境变量控制数据/配置路径
- `sys.stdout.reconfigure` 用 `hasattr` 守卫（frozen 模式下没有）
- `keys.json` 仅作为旧版本只读兼容；Web 设置写入 `.env` 并热重载

## 表结构

`store.py` 的 tasks 表有 SQLite WAL 模式。每任务对应 `data/uploads/<job_id>/` 目录，包含：输入 xlsx + `_cache.json`（图审/生图结果，含 mtime）+ `_status.json`（管道进度）+ `_cleaned.xlsx`（输出）

## 关键设计决策

- **requests Session 而不是 curl subprocess**：省掉每次 ~0.25s 进程启动开销 + TCP 连接复用，2.3x 加速
- **多进程而不是多线程**：`_DASHBOARD_HOOK` 是模块级全局，多线程下任务进度串台
- **SSE 不是 WebSocket**：单向推送，EventSource 原生支持，更简单
- **SQLite WAL 模式**：读写并发不互锁
- **去重翻译**：相同原文只调一次 API，节省 ~40% 文本调用

## Agent skills

### Issue tracker

GitHub Issues: `https://github.com/23xxCh/crosspilot`（通过 `gh` CLI 操作）。See `docs/agents/issue-tracker.md`.

### Triage labels

中文标签：`待评估`、`待补充`、`可自动处理`、`需人工`、`不处理`。See `docs/agents/triage-labels.md`.

### Domain docs

单仓库模式：一个 `CONTEXT.md` + `docs/adr/` 在项目根目录。See `docs/agents/domain.md`.
