# CrossPilot V2 系统架构

## 1. 系统概述

**定位：** 跨境电商 Listing 数据清洗中台，接收 RPA 爬取的原始产品数据，自动清洗文本、审核/重生成图片，输出合规表格。

**核心用户：**
- RPA 系统（主要调用方）：通过 API 或文件夹提交任务、获取结果
- 运维人员（你）：通过 Web 面板监控状态、管理配置

**架构驱动力：**
- 必须 7×24 稳定运行，RPA 不能断
- 文本处理 < 30s（361行），不能阻塞 RPA 流水线
- 单人维护，代码必须简单可调试
- 双形态交付：EXE 双击运行 + Docker 云部署

## 2. 架构模式

**模块化单体（Modular Monolith）**

```
┌─────────────────────────────────────────────┐
│                 CLI / EXE 入口                │
├─────────────────────────────────────────────┤
│  Web Layer         │  API Server (FastAPI)    │
│  监控面板           │  POST /api/v1/process    │
│  配置页             │  GET  /api/v1/process/id │
│  任务列表           │  GET  /api/v1/health     │
├─────────────────────────────────────────────┤
│              Pipeline Engine                 │
│  ┌───────────┐  ┌───────────────────────┐    │
│  │ Image Pipe │  │      Text Pipe        │    │
│  │ 审图+生图   │  │ 标题优化+描述清洗+Bullet│   │
│  └───────────┘  └───────────────────────┘    │
│           ↑ 并行执行 ↑                        │
├─────────────────────────────────────────────┤
│              Service Layer                   │
│  ┌──────────┐ ┌──────────┐ ┌────────────┐    │
│  │ Provider │ │  Adapter │ │  Reporter  │    │
│  │ API调用   │ │  格式适配 │ │  审核报告   │    │
│  └──────────┘ └──────────┘ └────────────┘    │
├─────────────────────────────────────────────┤
│              Data Layer                      │
│  ┌──────────┐ ┌──────────┐ ┌────────────┐    │
│  │  SQLite  │ │   Cache  │ │ File Watch │    │
│  │  任务记录 │ │  生图缓存 │ │  文件夹监听 │    │
│  └──────────┘ └──────────┘ └────────────┘    │
└─────────────────────────────────────────────┘
```

**选型理由：** 单体部署，无分布式复杂度。模块边界清晰，后续可拆微服务。EXE 和 Docker 共用同一代码。

## 3. 组件设计

### 3.1 API Server (`web/api.py`)

**职责：** 对外 REST API，RPA 系统的唯一入口

| 端点 | 方法 | 说明 |
|------|------|------|
| `/api/v1/process` | POST | 提交文件，返回 task_id |
| `/api/v1/process/{id}` | GET | 查询任务状态和进度 |
| `/api/v1/process/{id}/download` | GET | 下载清洗后文件 |
| `/api/v1/process/{id}/report` | GET | 下载审核报告 |
| `/api/v1/health` | GET | API 预检状态 |

**错误处理：**
- 4xx：文件格式错误、任务不存在 → RPA 可据此重试或跳过
- 5xx：内部错误 → RPA 应重试
- 所有错误返回 JSON：`{"error": {"code": "INVALID_FORMAT", "message": "..."}}`

### 3.2 File Watcher (`crosspilot/watcher.py`)

**职责：** 监控 `watch/input/` 文件夹，自动处理新文件

```
watch/input/xxx.json 出现
  → 移到 watch/processing/
  → 启动 Pipeline
  → 成功 → 输出到 watch/output/{name}/
  → 失败 → 移到 watch/failed/ + 写入错误日志
```

**实现：** `watchdog` 库监听文件创建事件，防抖 2 秒（等文件写完）。处理中忽略重复事件。

### 3.3 Pipeline Engine (`crosspilot/pipeline.py`)

**职责：** 数据处理核心，已在 Phase 1 实现

关键能力：
- 自动检测输入格式（JSON/XLSX, eBay/Amazon）
- 文本管道 + 图像管道可独立执行
- `--text-only` 跳过生图（12s 完成 361 行）
- API 挂了自动降级（规则清洗兜底、原图兜底）

### 3.4 Provider Layer (`scripts/model_provider.py`)

**当前配置（经验证最优）：**

| 功能 | 提供商 | 模型 | 要点 |
|------|--------|------|------|
| 文本 | DeepSeek | `deepseek-v4-flash` | `thinking: disabled`, 2500 并发 |
| 图审 | Agnes | `agnes-2.5-flash` | 1000 RPM |
| 生图 | Agnes | `2.0-flash` + `2.1-flash` 分流 | 3-5 并发防 503 |

**容错机制：**
- 重试 3 次 + 指数退避
- Circuit breaker（8 次连续失败 → 冷却 60s）
- 生图双模型自动切换（503 → 换备用模型）
- 文本 API 挂了 → 规则清洗兜底
- 任何降级不静默，报告明确标注

### 3.5 Web Dashboard (`web/app.py`)

**职责：** 运维人员监控界面

三个页面：总览仪表盘、任务详情、设置。已在设计阶段定义。

**前端：** 保持现有 vanilla JS SPA，不引入框架（降低维护成本）。

### 3.6 Reporter (`crosspilot/report.py`)

**职责：** 自动生成审核报告，已在 Phase 1 实现

输出到 `{任务名}_review/`：
- `review_report.txt` — 中英对照
- `quality_summary.txt` — 异常行统计
- `images/` — 下载的生图

## 4. 数据模型

### 4.1 任务记录（SQLite）

```sql
CREATE TABLE tasks (
    id          TEXT PRIMARY KEY,
    filename    TEXT NOT NULL,
    platform    TEXT,           -- ebay | amazon
    input_path  TEXT NOT NULL,
    output_path TEXT,
    status      TEXT DEFAULT 'queued',  -- queued|running|done|failed
    progress    REAL DEFAULT 0,         -- 0-100
    stage       TEXT,
    total_rows  INTEGER,
    clean_rows  INTEGER,
    quality_json TEXT,          -- JSON: issues list
    error_msg   TEXT,
    elapsed_s   REAL,
    created_at  TEXT,
    updated_at  TEXT
);
```

### 4.2 缓存（JSON 文件）

```
{输入文件名}_amz_cache.json
├── review_results: {url: bool}     # 图审结果
├── gen_results: {key: url}         # 生图结果（已通过门禁）
└── gen_meta: {key: {...}}          # 生图元数据
```

### 4.3 配置（.env）

优先级：环境变量 > .env 文件 > 默认值。已在 `crosspilot/config.py` 实现。

## 5. NFR 映射

| NFR | 决策 | 实现方式 |
|-----|------|---------|
| **可靠性** | 永不静默降级 | 任何 API 失败都在输出报告标注；circuit breaker；图片失败保留原图 |
| **可靠性** | 任务不丢失 | SQLite WAL 模式；处理前先固化任务记录 |
| **性能** | 文本 < 30s | DeepSeek 2500 并发，100 workers；361 行 12s 实测 |
| **性能** | 生图不阻塞文本 | 文本和生图可独立执行，`--text-only` 模式 |
| **可维护性** | 单人可维 | 模块化单体；vanilla JS 前端；Python 全套，无多语言 |
| **可部署性** | 单文件 EXE | PyInstaller + 内嵌 uvicorn；双击即用 |
| **可部署性** | Docker 一行起 | `docker compose up` |
| **可观测性** | 进度可见 | API 轮询进度 + Web 实时 SSE + status.json |
| **安全性** | Key 保护 | .env 文件存储，不提交 git；localhost 绑定 |
| **兼容性** | 零依赖安装 | EXE 内嵌 Python + 所有依赖 |

## 6. 技术栈

| 层 | 技术 | 理由 |
|----|------|------|
| API Server | FastAPI + uvicorn | 已有，异步支持，自动 OpenAPI 文档 |
| Pipeline | Python 3.13 | 已有，AI/ML 生态最好 |
| 前端 | Vanilla JS + SSE | 已有 1454 行，无框架依赖 |
| 数据库 | SQLite (WAL) | 零配置，单机够用 |
| 文件监听 | watchdog | Python 标准文件监听库 |
| 打包 | PyInstaller | 已有 spec 文件，成熟方案 |
| 容器化 | Docker + docker-compose | 已有 Dockerfile |
| 配置 | python-dotenv | 轻量，.env 加载 |

## 7. 部署架构

### 桌面 EXE 模式
```
客户电脑
├── CrossPilot.exe (双击启动)
├── .env (API Keys)
├── watch/
│   ├── input/    ← RPA 丢文件
│   └── output/   → RPA 取文件
└── 浏览器自动打开 → http://localhost:8765
```

### 云服务器模式
```
VPS (2C4G)
├── Docker: crosspilot
│   ├── API: http://ip:8765/api/v1/
│   └── Web: http://ip:8765/
├── Volume: ./data (持久化)
└── Volume: ./watch (文件夹监听)
```

## 8. Phase 2 待实现

1. **API Server** (`web/api.py`) — REST 接口给 RPA
2. **File Watcher** (`crosspilot/watcher.py`) — 文件夹监听
3. **Web Dashboard 改造** — 任务列表 + 详情页 + 设置页
4. **PyInstaller 一键打包** — 更新 spec，确保 EXE 内嵌所有文件
5. **Docker Compose 一行部署** — 完善 compose 文件
