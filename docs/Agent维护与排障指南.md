# Agent 维护与排障指南

这份文档面向接手 Bug、迭代功能或排查线上任务的 AI Agent。用户操作说明见根目录 `README.md`；不可破坏约束见根目录 `AGENTS.md`。

## 1. 两分钟理解项目

这是一个 Python 3.11+ 单机处理器，没有数据库、Redis、Docker或业务前端。核心公开接口是：

```python
from amazon_processor import process_json

result = process_json("采集表.json")
```

完整调用链：

```text
BAT / CLI / Worker / 任务 API
        ↓
amazon_processor.pipeline.process_json
        ↓
schema 读取与契约校验
        ↓
images.gate 图片去重、审查、选图或局部编辑
        ↓
text 标题、描述、副标题、Bullet、关键词、本地化修复
        ↓
delivery 行级隔离、14字段校验、终审包、原子发布
```

Windows 无人值守链路：

```text
Amazon日常操作/1_把采集表放这里 / POST /api/v1/jobs
        ↓
server_worker 按 SHA256 受理、FIFO、单任务运行
        ↓
python -m amazon_processor run --unattended
        ↓
任务结果.json → JobState → 成功/阻塞交付目录
```

## 2. 来源与产物

### 输入契约

源 JSON 是按列存储的数组对象。当前输入字段：

```text
商品id、产品站点、产品标题、产品描述、产品图片链接、变种图片链接
```

旧格式缺少 `产品站点` 时按 US 兼容。所有按行数组必须等长。读取和转换位于 `amazon_processor/schema.py`。

### 输出契约

正式回填表固定为 14 字段，字段顺序由 `AMAZON_JSON_OUTPUT_FIELDS` 定义。修改字段前必须同时更新 schema、fixture、交付校验、API校验、终审包和契约测试。

不要把 `有问题的产品id` 当通用警告列表。它通知上游删除四类商品：源描述为空、清理模板后没有产品内容、没有合格主图、图片清理后没有产品附图。普通文本模型失败仍进入 `异常商品.json` 或待审核包。

### 运行产物

```text
02_处理结果/最新/                 最近一次正式发布
02_处理结果/待人工审核/           未满足发布条件的审核包
02_处理结果/服务器交付/           每个 Worker 任务的独立交付
Amazon日常操作/                  操作员收件箱、静态状态页和友好交付
.runtime/cache/                   可续跑缓存
.runtime/server/jobs/             持久任务状态
.runtime/server/logs/             每次尝试日志
.runtime/server/outcomes/         子进程结构化结果
.runtime/server/heartbeat.json    Worker 心跳
```

缓存和输出不是源码。缓存默认保留 2 天，由 Worker 每日空闲时自动清理；活动任务期间不会清理。
正式输出、输入和交付包按独立保留策略处理，不要为了“重试干净”直接删除。

## 3. 模块地图

| 需求或故障 | 首先查看 | 对应测试 |
|---|---|---|
| 输入字段、行错位、商品 ID 数量 | `schema.py` | `test_amazon_json.py` |
| 阶段顺序、缓存恢复、整批运行 | `pipeline.py` | `test_amazon_pipeline.py` |
| 标题与品牌格式 | `text/titles.py`、标题 Prompt | `test_amazon_text.py` |
| 描述清洗与分段 | `text/descriptions.py`、描述 Prompt | `test_amazon_descriptions.py` |
| 副标题、Bullet、关键词 | `text/listing.py`、`text/subtitles.py` | `test_amazon_text.py` |
| 多站点语言错误 | `text/locale.py`、`text/localization.py` | `test_amazon_localization.py` |
| 图片误判、主图选择、附图删除 | `images/risk.py`、`images/gate.py` | `test_amazon_image_safety.py` |
| Agnes拥堵、回退、熔断 | `providers/agnes.py`、`providers/composite.py` | `test_agnes_congestion.py` |
| 配置、密钥、Prompt | `config/`、`config/manager.html` | `test_config_management.py` |
| 正式发布、异常隔离、终审包 | `delivery.py`、`review/` | `test_amazon_runtime.py`、`test_review_package.py` |
| Worker队列、尝试路径、幂等受理和状态恢复 | `server_jobs.py` | `test_server_jobs.py`、`test_server_worker.py`、`test_server_soak.py` |
| 正式结果验收、待审核重试判定、任务快照和操作员交付修复 | `server_delivery.py` | `test_server_delivery.py`、`test_server_worker.py`、`test_operator_workspace.py` |
| Worker启动预检、运行进度心跳和健康判定 | `server_health.py` | `test_server_health.py`、`test_server_worker.py`、`test_system_doctor.py` |
| Worker接单/暂停控制、状态转换、退避和终止决策 | `server_state.py` | `test_server_state.py`、`test_server_worker.py` |
| 子进程监督、超时、日志脱敏和故障分类 | `server_process.py` | `test_server_process.py`、`test_server_worker.py` |
| 缓存、日志、历史输入和交付保留策略 | `server_retention.py` | `test_server_retention.py`、`test_server_worker.py` |
| Worker执行、重试或磁盘治理 | `server_worker.py` | `test_server_worker.py` |
| 任务提交、契约校验、幂等状态和交付物定位 | `api_jobs.py` | `test_job_api.py` |
| HTTP鉴权、路由、限流响应和文件下载 | `api_server.py` | `test_job_api.py` |
| 系统状态汇总和中文展示 | `system_status.py` | `test_job_api.py`、`test_system_doctor.py` |
| 人工审核决定 | `review/decisions.py` | `test_amazon_review_decisions.py` |

## 4. 配置的唯一来源

| 内容 | 唯一来源 | 注意事项 |
|---|---|---|
| API 密钥 | `.env` | Git忽略；页面只显示末四位 |
| 模型、Endpoint、回退顺序 | `config/settings.json` | 每条线路引用命名凭据 |
| 并发、重试 | `config/settings.json` 的 `runtime` | 商品处理中配置只读 |
| Prompt 注册和变量 | `config/prompts/manifest.json` | 变量必须与模板完全一致 |
| AI 指令正文 | `config/prompts/**/*.txt` | Python不得拼接隐藏指令 |
| 站点语言与连接词 | `config/settings.json` 的 `markets` | 未知站点必须拒绝 |

管理员通过 `00_常用入口/03_配置与模型.bat` 修改。保存使用版本哈希、原子替换和最近10份备份。页面“测试线路”只探测 Endpoint和凭据，不发送付费推理请求。

新增 Prompt 的步骤：

1. 在 `config/prompts/` 新建 `.txt`。
2. 在 manifest 注册 ID、路径、变量和用途。
3. 代码用 `get_prompt_registry().render(...)` 传入事实变量。
4. 把 Prompt ID加入对应缓存签名。
5. 增加变量校验和行为测试，并扫描 Python 是否残留指令正文。

## 5. 图片规则

- 正式批处理只有 `select_existing`：全部源图按 URL 去重审查，风险和持续未知图片删除，生图调用必须为 0。
- 所有安全产品图均检查主图资格；白底清晰图优先，同等级按源顺序选择。
- 没有合格主图，或主图之外没有产品附图时，删除整行并加入 `有问题的产品id`；变种图不计为附图。
- 第一张产品图始终是主图；图片或 Prompt 策略变化必须失效对应缓存；文案 Prompt 变化不应触发重新审图。
- `06_Agnes生图测试台.bat` 是独立人工工具，只保存候选图，不能写回正式表。

## 6. Worker状态与故障分类

| 状态 | 含义 | Agent动作 |
|---|---|---|
| `queued` | 等待处理 | 检查队列位置，不重复提交 |
| `running` | 子进程处理中 | 只读心跳、状态和最新日志 |
| `retry_wait` | 临时故障退避 | 保留缓存，等待自动续跑 |
| `delivery_retry` | AI 已完成，本地交付目录整理重试 | 只修复文件交付，不得重跑付费流水线 |
| `blocked` | 鉴权、额度等人工问题 | 报告密钥/Endpoint/余额证据 |
| `published` | 全部发布 | 验证文件和契约 |
| `published_with_warnings` | 正常行已发布，部分隔离 | 核对正式行数和异常原因，不能只看退出码 |
| `pending_review` | 没有满足整批发布条件 | 保留上一版正式表 |
| `invalid_input` | 输入或确定性数据错误 | 不盲目重试 |
| `failed` | 内部错误达到上限 | 复现、写测试、修复后显式重排队 |

进程树中 `cmd → uv → venv shim → Python` 可能看起来像多个进程，但不代表多个 Worker。判断重复实例应检查命令行、父子 PID和 Worker锁。

Windows 短暂占用 `heartbeat.json` 时，Worker 会自动重试状态写入；即使一次心跳写入失败，也必须继续托管正在运行的处理子进程。看门狗以实际心跳和 API 健康检查为准，不因计划任务界面显示 `Ready` 就重复启动健康服务。

## 7. 标准 Bug 修复流程

1. 记录输入哈希、任务ID、状态文件、尝试次数和正式表修改时间。
2. 先读 `运行状态.json` 或 `任务结果.json`，再看日志；不要只解析乱码控制台文本。
3. 用最小 fixture 写失败测试，避免拿完整采集表反复调用付费API。
4. 只修改最靠近根因的模块，不顺便重构。
5. 跑相关测试，再跑完整离线测试。
6. 若需真实API冒烟，先说明请求数量、可能费用和验收目标，等用户明确授权。
7. 发布后核对：输入哈希、输出字段顺序、数组等长、ID顺序、主图存在、问题ID含义、异常清单。

常用命令：

```powershell
git status --short
codegraph explore "process_json 到 deliver 的调用链"
uv run python -m pytest tests/test_amazon_pipeline.py -q
uv run ruff check amazon_processor tests
uv run pyright
uv run python -m pytest --cov=amazon_processor --cov-fail-under=75 -q
uv run python -m amazon_processor soak --cycles 1000
python -m amazon_processor worker-status
python -m amazon_processor system-status
```

CI 采用渐进式门禁：Ruff 检查运行时错误、未定义/未使用符号和常见 bug；
Pyright 先覆盖已经加固的服务模块；测试覆盖率不得低于 75%。不要为了让门禁通过而
批量忽略错误，应在每次修改相关模块时逐步扩大类型检查范围。

`soak` 是隔离的离线故障注入工具，不导入业务流水线或 Provider。长时间测试使用
`--cycles 0 --duration-hours 24 --interval-seconds 1`；报告中
`provider_requests` 必须为 0，且 `invariant_failures` 必须为 0。

## 8. 不要做的事

- 不修改或覆盖原采集表。
- 不清空 `.runtime/cache` 来掩盖缓存签名错误。
- 不在已有 Worker 运行时启动第二个 Worker。
- 普通质检和 Provider 临时失败不写入 `有问题的产品id`；缺主图或缺产品附图按业务规则写入。
- 不把中间文件、待审核包或旧正式表误报成新交付。
- 不在文档、异常文本、浏览器API或Git里暴露完整密钥。
- 不恢复已删除的前端、eBay、XLSX、EXE、Docker或发布兼容层。

## 9. 完成定义

一次代码迭代只有同时满足以下条件才算完成：

- 根因有可复现测试；相关测试和完整离线测试通过。
- 新配置或 Prompt 在管理中心可见并可校验。
- 输入/输出契约和安全发布边界未破坏。
- README、AGENTS或本指南中受影响的说明同步更新。
- 最终报告明确说明修改范围、测试数量、是否调用付费API和仍存在的风险。
