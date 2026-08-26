# Amazon JSON Processor Skill

这个项目只做一件事：Agent 接收一份 Amazon 列式 JSON 采集表，完成图片审查、多站点文案处理、14 字段验收，并返回正式回填表和离线终审包。

```text
用户给 Agent 一个采集表路径
  → DeepSeek 全量审图（URL 去重）
  → 删除风险图，从安全原图重选主图
  → DeepSeek 多站点文案
  → 14 字段回填表
  → 离线终审包与机器摘要
```

项目不再包含 Windows 服务器、Worker、任务 API、定时任务、状态网页、Agnes、Ollama、GPT Image 或图片生成流程。

## 一键安装（Windows）

打开 PowerShell，复制并执行这一条命令：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -Command "irm 'https://raw.githubusercontent.com/23xxCh/crosspilot/main/scripts/install_from_github.ps1' | iex"
```

该命令会把项目安装或更新到 `%LOCALAPPDATA%\AmazonJsonProcessor`，自动准备 `uv` 和 Python 依赖，并注册为 `%USERPROFILE%\.codex\skills\amazon-json-processor`。要求电脑已安装 [Git for Windows](https://git-scm.com/download/win)；已有安装存在未提交修改时会安全停止，不会覆盖。

安装完成后，只需打开 `%LOCALAPPDATA%\AmazonJsonProcessor\.env`，填写：

```dotenv
DEEPSEEK_KEY=你的官方DeepSeek密钥
```

然后重启 Codex，向 Agent 提供采集表绝对路径，并要求“处理这个 Amazon 采集表”。

### 已下载项目的安装方式

如果别人拿到的是完整项目文件夹，在该目录打开 PowerShell 后执行：

```powershell
uv sync --frozen; powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\install_skill.ps1
```

也可以直接双击 `安装Amazon处理Skill.bat` 注册 Skill，但仍需自行执行 `uv sync --frozen` 准备依赖。

Agent 实际执行：

```powershell
uv run python scripts/process_amazon_json.py "E:\data\跨境电商自动化采集表.json"
```

也可以直接使用稳定 CLI：

```powershell
uv run python -m amazon_processor run "E:\data\跨境电商自动化采集表.json" --unattended
```

## 成功交付

正式发布位于 `02_处理结果\最新`：

- `跨境电商自动化回填表.json`
- `终审包.html`
- `审核数据.json`
- `运行状态.json`
- `异常商品.json`（存在隔离商品时）

Agent 入口另写入 `.runtime\agent_runs\<时间戳_输入哈希>\result.json`。只有该文件同时包含 `status: published` 和 `published: true`，才能宣称正式表已更新。

## 固定业务规则

- 输入为 5/6 字段列式 JSON；旧表缺少 `产品站点` 时按 US 处理。
- 输出字段和顺序固定为 `amazon_processor.schema.AMAZON_JSON_OUTPUT_FIELDS` 的 14 字段。
- 支持 US、UK、CA、MX、ES、BR、DE、FR、IT 多站点本地化。
- 所有唯一图片 URL 都用 `deepseek-v4-flash-vision-exp` 审查；默认 15 批并发、每批 3 张。
- 风险图删除；持续未知视为运行失败，不覆盖上一版正式表。
- 主图必须来自原采集表。白底、单品清晰、安全且无文字的原图优先。
- 没有合格主图，或清理后主图之外没有产品附图的商品，会从逐行字段删除并加入 `有问题的产品id`。
- 图片生成请求始终为 0。
- 输入文件只读；正式结果使用 staging + 原子发布。

## 配置

- 密钥：`.env`，只允许 `DEEPSEEK_KEY`。
- 模型、Endpoint、并发、批量和重试：`config/settings.json`。
- Prompt 注册：`config/prompts/manifest.json`。
- Prompt 正文：`config/prompts/**/*.txt`。

模型或 Prompt 修改后会改变缓存签名；旧 Agnes 缓存不会被新 DeepSeek 配置误用。

## 更新

再次执行上方“一键安装”命令即可从 GitHub `main` 快进更新。更新脚本不会覆盖 `.env`；如果本地代码有修改，它会停止并提示先保存修改。

## 代码入口

- Skill 工作流：[SKILL.md](SKILL.md)
- 确定性 Agent 入口：`scripts/process_amazon_json.py`
- 流水线：`amazon_processor/pipeline.py`
- 输入/输出契约：`amazon_processor/schema.py`
- DeepSeek Provider：`amazon_processor/providers/deepseek.py`
- 图片安全门：`amazon_processor/images/gate.py`
- 文案：`amazon_processor/text/`
- 原子发布与终审包：`amazon_processor/delivery.py`、`amazon_processor/review/`
- 维护说明：[docs/Agent维护与排障指南.md](docs/Agent维护与排障指南.md)

## 离线验证

```powershell
uv sync --group dev
uv run ruff check amazon_processor scripts tests
uv run pyright
uv run python -m pytest --cov=amazon_processor --cov-fail-under=75 -q
```

默认测试不会访问网络或产生付费请求。真实 DeepSeek 冒烟测试必须由用户明确授权。
