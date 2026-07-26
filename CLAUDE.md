# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目概述

CrossPilot - 跨境电商 Listing 全自动清洗平台。从 eBay/Shopee/Amazon 导入产品表格，AI 自动翻译越南语、去水印/品牌/Logo、生图替换、图片注入描述，输出 TikTok Shop 越南站合规表格。

## 命令

```bash
# Web 平台（主入口）
uv run uvicorn web.app:app --port 8765

# 纯命令行单文件
uv run python -u scripts/process_ebay_tk.py "<输入.xlsx>"

# 独立图生图脚本（发给别人测试用）
uv run python -u scripts/image_gen.py <图片> -o output -c 5

# 造迷你测试表（从全表截前 N 行，省 API 额度）
uv run python -u web/make_sample.py <行数>

# 打包 exe
uv run pyinstaller --onefile --name CrossPilot --add-data "web/static:web/static" --add-data "scripts:scripts" --add-data "keys.example.json:." main_cli.py
```

## 架构

```
web/static/index.html + app.js     SPA 前端（hash 路由，vanilla JS + SSE）
web/app.py                         FastAPI 路由（9 个端点）
web/jobs.py                        任务队列（dev 模式子进程 runner.py，exe 模式线程）
web/store.py                       SQLite tasks 表（WAL 模式 + busy_timeout）
web/runner.py                      子进程入口（调 _main）
web/updater.py                     本地文件更新检测（_update/ 目录）
main_cli.py                        PyInstaller 打包入口
scripts/process_ebay_tk.py         核心管道（10 阶段，997 行）
scripts/adapters/                  表格格式适配器（eBay 已实现，Shopee/Amazon 模板预留）
scripts/image_gen.py               独立图生图脚本（三级 fallback + 并发）
```

## 核心管道 `process_ebay_tk.py`

**10 阶段**：提取URL → MiMo图审(2685张/100并发) → 万相生图(10并发) → 附图清空 → 标题翻译 → 描述AI清洗 → 描述翻译 → 嵌入注入图片 → 视频模板清理 → 价格列保存

**API 调用层**（最近从 curl subprocess 重构为 requests.Session）：
```python
_http = requests.Session()
_adapter = HTTPAdapter(pool_connections=100, pool_maxsize=100, max_retries=0)
_http.mount('https://', _adapter)
```
- `dmx_call(payload)`: 调用 DMXAPI chat completions，返回 content 字符串。40s 总超时，主模型 8 次重试 + 3 个 fallback 模型自动切换
- `_vision_call(model, image_url)`: 图审单次调用，返回 True/False/None
- `_post_json(endpoint, payload)`: POST JSON 到 DMXAPI，返回 dict

**并发配置**：
- `MIMO_CONCURRENCY = 100` - 图审
- `TEXT_CONCURRENCY = 20` - 文本批次并发
- `GEN_CONCURRENCY = 10` - 生图（万相限流）
- `MAX_RETRIES = 8` - dmx_call 内部重试（主模型），fallback 模型 2 次

**图审**：三级 fallback（MiMo快重试3x → MiMo慢重试2x → gemini 2x），单张 45s 总超时

**生图**：二级 fallback（万相 wan2.7-image → 豆包 doubao-seedream-5.0-lite），URL→URL，不下载图片

**文本**：去重+断点缓存（`{unique_text: [row_indices]}` → 全局哈希缓存 → 并发批量 API）。主模型 mimo-v2.5，fallback deepseek-v4-flash/hy3/step-3.5-flash

## Web 层

**API 端点**：`/api/dashboard` `/api/upload/batch` `/api/tasks` `/api/tasks/{id}` `/api/tasks/{id}/rows` `/api/tasks/{id}/events`(SSE) `/api/tasks/{id}/download` `/api/settings` `/api/templates` `/api/version`

**任务执行**：
- Dev 模式：`subprocess.Popen` 调 `web/runner.py`（进程隔离，真并发），`_pending` 等待队列
- Exe 模式：线程直接调 `_main`（PyInstaller 打包后无法 spawn 子进程）
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
