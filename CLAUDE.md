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
uv run python -u scripts/gen_only.py "<输入.json>" 50

# Web 平台
uv run uvicorn web.app:app --port 8765

# 独立图生图脚本（发给别人测试用）
uv run python -u scripts/image_gen.py <图片> -o output -c 5

# 造迷你测试表（从全表截前 N 行，省 API 额度）
uv run python -u tests/make_sample.py <行数>

# 打包 exe
uv run pyinstaller --onefile --name CrossPilot --add-data "web/static:web/static" --add-data "scripts:scripts" --add-data "keys.example.json:." main_cli.py
```

## 架构

```
scripts/
├── model_provider.py        # 统一模型提供商接口（配置驱动，换模型只需改 keys.json）
├── dmx_client.py            # 兼容层（包装 model_provider，保持旧接口）
├── process_ebay_tk.py       # eBay 管道入口
├── process_amazon.py        # Amazon 管道
├── pipelines/               # eBay 阶段编排
│   ├── ebay_shared.py       # 共享模块（使用 model_provider）
│   └── ebay_stages.py       # 阶段函数
├── services/                # 抽象层
│   ├── review.py            # 图审服务（使用 model_provider）
│   ├── translate.py         # 翻译服务（使用 model_provider）
│   └── constants.py         # 品牌词与共享规则
├── adapters/                # 表格格式适配器
└── image_gen.py             # 独立图生图脚本（使用 model_provider）
```

## 核心管道 `process_ebay_tk.py`

**10 阶段**：提取URL → 图审 → 图生图 → 附图清空 → 标题翻译 → 描述AI清洗 → 描述翻译 → 嵌入注入图片 → 视频模板清理 → 价格列保存

**API 调用层**（使用 model_provider，配置驱动）：
```python
from model_provider import get_provider

provider = get_provider()  # 自动从 keys.json 加载配置

# 文本生成（由 text_provider 配置决定）
text = provider.call_text(prompt)

# 图审（由 vision_provider 配置决定）
needs_fix = provider.call_vision(image_url)

# 图生图（由 image_gen_provider 配置决定）
new_url = provider.call_image_gen(image_url)
```

**配置方式**（`keys.json`）：
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
- `CROSSPILOT_DATA_DIR` / `CROSSPILOT_KEYS_PATH` 环境变量控制数据/密钥路径
- `sys.stdout.reconfigure` 用 `hasattr` 守卫（frozen 模式下没有）
- `_load_keys()` 先查 env var 路径，再 fallback 到 `_ROOT/keys.json` 和 `keys.example.json`

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

Issues are tracked as markdown files under `.scratch/<feature>/` in this repo (local markdown convention — no GitHub/GitLab remote). See `docs/agents/issue-tracker.md`.

### Triage labels

Default triage vocabulary: `needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`. See `docs/agents/triage-labels.md`.

### Domain docs

Single-context: one `CONTEXT.md` + `docs/adr/` at the repo root. See `docs/agents/domain.md`.
