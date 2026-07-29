# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目概述

CrossPilot - 跨境电商 Listing 全自动清洗平台。从 eBay/Shopee/Amazon 导入产品表格，AI 自动翻译越南语、去水印/品牌/Logo、生图替换、图片注入描述，输出 TikTok Shop 越南站合规表格。

## 命令

```bash
# === 统一 CLI（推荐）===
uv run python -m crosspilot ebay "输入.xlsx"        # eBay→TikTok 清洗
uv run python -m crosspilot amazon "输入.xlsx"      # Amazon 采集表回填
uv run python -m crosspilot gen "输入.json" -c 20   # 纯图生图（Agnes）
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
├── model_provider.py        # 旧导入兼容门面
├── providers/               # Provider 客户端、路由、工厂和结构化错误
├── dmx_client.py            # 兼容层（包装 model_provider，保持旧接口）
├── process_ebay_tk.py       # eBay 管道入口
├── process_amazon.py        # Amazon 编排入口与兼容 API
├── pipelines/               # eBay/Amazon 阶段实现
│   ├── ebay_shared.py       # eBay 共享模块（使用 model_provider）
│   ├── ebay_stages.py       # eBay 阶段函数
│   ├── amazon_constants.py  # Amazon 清洗规则与质量校验
│   ├── amazon_io.py         # Amazon 输入、输出与交付校验
│   ├── amazon_stages.py     # Amazon 文本处理阶段
│   └── amazon_review_gen.py # Amazon 审图与生图阶段
├── services/                # 抽象层
│   ├── review.py            # 图审服务（使用 model_provider）
│   ├── translate.py         # 翻译服务（使用 model_provider）
│   └── constants.py         # 品牌词与共享规则
└── adapters/                # 表格格式适配器
```

## 核心管道 `process_ebay_tk.py`

**10 阶段**：提取URL → 图审 → 图生图 → 附图清空 → 标题翻译 → 描述AI清洗 → 描述翻译 → 嵌入注入图片 → 视频模板清理 → 价格列保存

**API 调用层**（使用 model_provider，配置驱动）：
```python
from model_provider import get_provider

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
