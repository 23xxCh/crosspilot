# Amazon 处理器 Agent 工作约定

## 项目边界

本仓库只维护一条生产链：Amazon 列式 JSON 采集表 → 图片处理 → 多站点文案 → 14 字段回填表 → 离线终审包。不要恢复旧 Web 前端、eBay、XLSX、打包或目录监听兼容层。

## 开始修改前

1. 先运行 `git status --short`，保留用户现有修改，不清理无关文件。
2. 若根目录存在 `.codegraph/`，先用 `codegraph explore "问题或符号"` 理解调用链，再使用 `rg`。
3. 阅读 [docs/Agent维护与排障指南.md](docs/Agent维护与排障指南.md) 中与问题相关的章节。
4. 修改配置或 Prompt 前，确认没有正式任务持有 `.runtime/processor.lock`。

## 稳定接口与不可破坏契约

- Python 接口：`amazon_processor.process_json(input_path) -> RunResult`。
- CLI：`python -m amazon_processor run <input.json>`。
- 输入为 5/6 字段列式 JSON；旧表无 `产品站点` 时按 US 兼容。
- 输出字段顺序以 `amazon_processor.schema.AMAZON_JSON_OUTPUT_FIELDS` 为准，共 14 字段；各按行数组必须等长。
- `商品id`、`产品站点`、商品顺序和图片角色不得错位。
- 第一张 `产品图片链接` 是主图。
- `有问题的产品id` 记录源描述无内容、没有合格主图或图片清理后没有产品附图的商品；普通模型失败不得写入。
- 输入文件只读，正式输出必须通过 staging + 原子发布；失败时保留上一版正式表。
- 无人值守默认收件箱是 `Amazon日常操作/1_把采集表放这里`；旧
  `01_输入采集表/待处理` 只用于升级迁移。
- 密钥只存未跟踪的 `.env`，不得写入代码、日志、测试快照或文档。

## 配置与 Prompt

- 密钥：`.env`。
- 模型、Endpoint、回退、并发、重试：`config/settings.json`。正式批处理图片规则固定为只审图、只删图、不生图。
- Prompt 注册：`config/prompts/manifest.json`；正文：`config/prompts/**/*.txt`。
- Python 只能传事实变量，不得新增隐藏 AI 指令。Prompt/模型变化必须进入缓存签名。
- 管理员入口：`00_常用入口/03_配置与模型.bat`。

## 主要修改入口

- 流水线编排：`amazon_processor/pipeline.py`
- JSON 契约：`amazon_processor/schema.py`
- 标题/描述/Bullet/本地化：`amazon_processor/text/`
- 图片规则、缓存、生成：`amazon_processor/images/`
- Provider 与错误分类：`amazon_processor/providers/`
- 输出、隔离、终审包：`amazon_processor/delivery.py`、`amazon_processor/review/`
- 无人值守队列、尝试路径、幂等受理与状态恢复：`amazon_processor/server_jobs.py`
- 正式结果验收、任务快照、操作员交付与历史交付修复：`amazon_processor/server_delivery.py`
- Worker启动预检、运行进度心跳和健康判定：`amazon_processor/server_health.py`
- Worker循环控制、状态转换、退避与终止决策：`amazon_processor/server_state.py`
- 子进程监督、超时和故障分类：`amazon_processor/server_process.py`
- 缓存、日志和历史交付保留策略：`amazon_processor/server_retention.py`
- 无人值守 Worker执行、重试与磁盘治理：`amazon_processor/server_worker.py`
- 操作员静态状态页与友好交付：`amazon_processor/operator_workspace.py`
- 任务提交、状态与交付物服务：`amazon_processor/api_jobs.py`
- 任务 API HTTP适配：`amazon_processor/api_server.py`
- 系统状态汇总与中文展示：`amazon_processor/system_status.py`
- 配置中心：`amazon_processor/config/`、`config/manager.html`

## 测试与交付标准

- 先写或更新能复现问题的离线测试，再做最小修复。
- 针对性测试：`uv run python -m pytest tests/<相关文件>.py -q`。
- 最终测试：`uv run python -m pytest -q`。
- 默认测试禁止网络和付费 API；没有用户明确授权，不运行真实文本、审图或生图请求。
- 修改 BAT/PowerShell 后，在 Windows 上检查脚本解析；BAT/PS1 保持 CRLF。
- 不要仅因进程退出码为 0 就宣称成功；必须核对 `运行状态.json`、正式文件、行数、字段等长和异常清单。

## 故障处理原则

- `503/429/timeout/network`：保留缓存，按 Worker 退避续跑，不清缓存、不重复启动 Worker。
- `401/403/quota`：暂停并报告密钥、Endpoint 或余额问题。
- 单行文案或图片质量失败：无人值守模式隔离该行，正常行继续发布。
- 全批没有可发布商品：生成待审核包，不覆盖 `02_处理结果/最新`。
- 不熟悉的历史文件、缓存或用户输出不得擅自删除。

## Git

当前开发分支使用 `codex/` 前缀。除非用户明确要求，不提交 `.env`、`.runtime/`、输入采集表、正式输出或本地模型。提交前先展示并检查变更范围。
