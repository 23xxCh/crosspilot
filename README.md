# Amazon JSON 核心处理器

> 第一次接触项目或准备改代码时，先看
> [可视化代码地图](项目代码地图.html)：业务流程、模块边界、模型回退和修改入口都在一页里。
>
> AI Agent 或开发者接手 Bug、排障和迭代时，先阅读
> [AGENTS.md](AGENTS.md) 和
> [Agent维护与排障指南](docs/Agent维护与排障指南.md)。

这个项目只做一件事：

```text
Amazon JSON 采集表
  → 图片安全初审与修复
  → 按产品站点本地化标题、副标题、描述、Bullet、关键词
  → 14 字段回填表
  → 离线终审包
```

## 给日常操作员

普通用户不进入项目目录，也不运行 BAT。服务器只共享下面这个目录：

```text
Amazon日常操作
├─ 1_把采集表放这里
├─ 2_到这里取结果
└─ 3_查看处理状态.html
```

使用方法只有三步：把采集表 JSON 放入第一个文件夹；双击状态页等待“已完成”；
到第二个文件夹领取 `最新回填表.json` 或对应任务的独立结果包。状态页每 10 秒
自动刷新，用户电脑不需要安装 Python、uv或其他程序。详见
[零基础操作员说明](docs/零基础操作员说明.md)。

## 本地开发或单次手工运行

1. 打开 `00_常用入口`，第一次使用时双击 `03_配置与模型.bat`，填写 API 密钥并检查配置。
2. 把 Amazon 采集表 JSON 拖到 `01_处理采集表.bat`。
3. 若运行状态为已发布，在 `02_处理结果\最新` 查看：
   - `跨境电商自动化回填表.json`
   - `终审包.html`
   - `审核数据.json`
   - `运行状态.json`
   - `图片\`
   若存在没有合格主图的商品，则结果会放在
   `02_处理结果\待人工审核\待定_<时间戳>`，此时不会覆盖“最新”。
4. 在终审包中导出 `审核决定.json` 后，把它拖到
   `02_应用审核结果.bat`。
5. 需要单独验证 Agnes 图生图和图片文字翻译时，双击
   `06_Agnes生图测试台.bat`。该测试台只保存候选图，不修改回填表。

Windows 空闲电脑作为服务器时，管理员只使用 `管理员工具`：

1. 第一次双击 `1_首次安装服务器.bat`。
2. 密钥或模型变化时双击 `2_配置模型密钥.bat`。
3. 出现异常时双击 `3_系统自检与修复.bat`。
4. 需要升级时双击 `5_更新系统.bat`。

安装入口会自动申请管理员权限、生成调用密钥并安装开机任务。状态入口只显示
正常、处理中、自动重试和需要处理，不显示程序日志。

运行环境会安装到
`%LOCALAPPDATA%\AmazonProcessor\venv`，项目目录不创建 `.venv`。

## 目录

```text
Amazon日常操作\          唯一向普通用户共享的目录
管理员工具\              安装、配置、自检、审核和更新
00_常用入口\             日常只需要打开这里
├─ 01_处理采集表.bat
├─ 02_应用审核结果.bat
├─ 03_配置与模型.bat
├─ 04_一键安装服务器.bat
├─ 05_查看系统状态.bat
├─ 06_Agnes生图测试台.bat
└─ 07_更新系统.bat
01_输入采集表\
├─ 待处理\               旧收件箱，仅用于升级迁移
└─ 已接收\YYYY-MM\       已受理原文件，内容和哈希保持不变
02_处理结果\
├─ 最新\                已发布回填表、终审包、状态和终审图片
├─ 待人工审核\          未发布的待定商品包
└─ 归档\                每次覆盖前的压缩备份
03_人工审核决定\         保存人工导出的审核决定
04_本地审图模型\         Ollama 运行环境和本地视觉模型
90_系统工具\服务器后台\  Worker、任务 API、安装脚本和看门狗
amazon_processor\        核心程序
config\                  模型、并发和 Prompt
docs\                    说明文档和格式模板
tests\                   离线测试
```

## 配置管理

- 推荐双击 `00_常用入口\03_配置与模型.bat`，在本机页面统一修改 API 密钥、
  模型线路、回退顺序、Prompt、并发和重试。
- 页面只绑定 `127.0.0.1`；旧密钥只显示配置状态和末四位，
  完整值不会返回浏览器。
- `.env` 仍是唯一密钥文件，并由 Git 忽略。现有
  `DEEPSEEK_KEY`、`AGNES_KEY`、`GPT_IMAGE_KEY` 无需迁移。
- `config/settings.json` 使用命名凭据配置模型和回退线路。
- `config/prompts/manifest.json` 注册全部 Prompt；
  Prompt 正文保存在对应 `.txt` 文件。
- 每次保存前会在 `.runtime/config_backups` 自动保留快照，
  最多 10 份。
- “测试线路”只检查已保存 Endpoint 和密钥是否可连接，不发送付费推理请求；
  “检查配置”负责在保存前验证模型、Prompt变量和运行参数。

修改模型或 Prompt 后缓存签名会自动变化，不会误用旧结果。
商品处理期间配置只读，避免同一批商品混用两套 Prompt。

## Agnes 生图测试台

- 双击 `00_常用入口\06_Agnes生图测试台.bat` 后，页面只在本机 `127.0.0.1`
  运行；Agnes 密钥不会发送到浏览器。
- 支持拖入 JPG、PNG、WebP，或粘贴公共 HTTPS 图片链接；默认使用
  `agnes-image-2.1-flash`，也可手动切换 2.0。
- 每次只生成一张候选图，不自动审图、不使用 GPT 回退、不修改正式回填表。
- 原图、生成图和不含密钥的生成记录保存在
  `02_处理结果\生图测试台\<时间戳>`。

## 业务规则

- 新采集表使用逐行字段 `产品站点`，支持 `US`、`UK`、`CA`、`MX`、
  `ES`、`BR`、`DE`、`FR`、`IT`；缺少该字段的旧采集表按 `US` 兼容。
- 文案按站点分别优化为 `en-US`、`en-GB`、`en-CA`、`es-MX`、
  `es-ES`、`pt-BR`、`de-DE`、`fr-FR`、`it-IT`。相同语言的不同站点
  也分别生成和缓存，图片则跨站点去重复用。
- 标题尽量接近且不超过 75 个字符；适配品牌标题使用
  对应语言的 `for/para/pour/für/per` 连接适配品牌或型号。
- 副标题只在标题少于 75 字符时填写，最多 125 字符；使用英文逗号
  分隔短语，补充标题未覆盖的材质、适配、规格、功能或场景。
- 描述最长 500 个字符，固定使用“目标语言简介 + 空行 + 逐行规格”；
  先删除交叉销售、价格、店铺和交易模板，再按标题相关性生成。
- 有效源描述的商品生成 5 条 Bullet 和 10 组关键词。源描述为空或清洗后
  没有产品内容的商品会自动删除，并把 ID 写入 `有问题的产品id`。
- 正式批处理固定为全量审图、只删不生图：主图、附图、变种图全部按 URL
  去重审查，`risk` 和持续 `unknown` 的图片删除，生图请求严格为 0。
- 所有普通审查安全的产品图都会继续检查主图资格。优先选择白底、单品清晰、
  无品牌名、Logo、车标、水印、人物和文字的原图作为主图。
- 没有合格主图，或选出主图后没有任何产品附图的商品，会从全部逐行字段删除
  并加入 `有问题的产品id`；变种图不能替代产品附图。
- 独立 Agnes 生图测试台仅供管理员手动试图，不连接正式回填流程。

## Module 与 Interface

- 外部 Interface：处理、应用审核决定、配置管理和异步任务 API。
- Python Interface：`amazon_processor.process_json(input_path) -> RunResult`。
- `amazon_processor/` 是唯一生产 Module；文本、图片、Provider、终审均为
  内部 Implementation，不保留历史 Adapter。
- `.runtime/cache/` 保存续跑缓存和唯一一份共享图片缓存。

## Windows 服务器全天运行

后台的 `启动全天处理.bat` 是无人值守入口，不打开浏览器。人工或 API 只把文件
放入 `Amazon日常操作\1_把采集表放这里`；Worker 跨轮询确认文件大小和修改时间稳定后，按
SHA256 幂等受理并原子移入 `01_输入采集表\已接收\YYYY-MM`。受理后的原文件
内容和哈希保持不变，不会重复创建或发布同一任务。状态、结构化子进程结果、
实时日志和心跳写入：

```text
.runtime/server/jobs/<sha256>.json
.runtime/server/logs/<sha256>_<attempt>.log
.runtime/server/outcomes/<sha256>/attempt_<次数>/任务结果.json
.runtime/server/heartbeat.json
```

管理员首次双击 `管理员工具\1_首次安装服务器.bat` 即可。需要命令行时，
管理员 PowerShell 也可以执行：

```powershell
Set-ExecutionPolicy -Scope Process Bypass
& ".\90_系统工具\服务器后台\安装后台服务.ps1"
```

任务会在开机后启动、异常退出后每分钟重启，并禁止重复实例。安装脚本会保存
当前插电睡眠时间并把插电睡眠设为“从不”，卸载时恢复原设置。看门狗每 5 分钟
检查计划任务、心跳和本机 API：进程消失立即重启，心跳连续两次过期才重启，
避免误杀长任务。管理员自检与修复：

```powershell
.\管理员工具\3_系统自检与修复.bat
```

上游不要直接覆盖正在写入的 JSON；先上传为 `.uploading`，完成后再原子改名
为 `.json`。异常断电后会修复“已移动但未写完状态”和失去子进程的 `running`
任务，并从已有字段级缓存续跑。

每个任务都有独立交付目录，不会只依赖不断覆盖的“最新”：

```text
02_处理结果/服务器交付/成功/<文件名_哈希_时间>/
02_处理结果/服务器交付/待处理/<文件名_哈希_时间>/
02_处理结果/服务器交付/阻塞/<文件名_哈希_时间>/
```

Worker 的状态含义：

- `published`：正式表已原子发布。
- `published_with_warnings`：不合格商品已独立隔离，其余商品已发布。
- `pending_review`：全部商品都无法自动放行，旧正式表不被空结果覆盖。
- `retry_wait`：临时网络或上游错误，按退避时间等待重试。
- `blocked`：鉴权或余额错误，每 6 小时低频自动复查。
- `invalid_input`：输入结构不可安全处理，不进行无意义的 API 重试。
- `failed`：未知程序错误达到保护性重试上限。

503、429、网络和超时按 30 秒、2 分钟、5 分钟、之后每 10 分钟（含随机抖动）
续跑；鉴权和余额每 6 小时低频探测，程序内部异常最多重试 3 次。Provider 整体
异常时当前任务会暂停后续队列，避免排队任务轮流放大 503。子进程连续 45 分钟
没有日志进展时也会被终止并从缓存续跑。修复
API Key 后无需删除缓存，最迟在下一次低频复查时继续；也可
立即执行：

```powershell
uv run python -m amazon_processor worker --retry-terminal --once
```

服务器正式任务固定使用 `select_existing` 规则，全天运行不会调用生图 API，
也不能通过环境变量切换为批量生图。独立 Agnes 生图测试台只保存管理员手动
生成的候选图，不会写回正式表。任务结束后不会写回密钥或原采集表。

系统每天在空闲时清理：缓存文件保留 2 天，已接收输入、独立交付和任务状态保留 90 天，日志保留
30 天，“最新”结果始终保留。磁盘低于 30 GB 时还会从最旧缓存开始清理到 50 GB；
低于 10 GB 时停止接收和启动新任务，并在状态入口显示原因。人工更新使用
`07_更新系统.bat`：进入维护模式、快进更新、同步外部环境、运行离线测试和冒烟
检查；失败会恢复更新前代码，不覆盖 `.env`、输入、结果、模型或运行缓存。

## 给其他系统调用

后台任务 API 提供无前端的异步接口。接口只负责验证并原子写入
Worker 收件箱；耗时的文本和审图不占用 HTTP 请求，503 和断点续跑仍由
全天 Worker 处理。可直接把 [任务 API v1 文档](docs/任务API.md) 交给调用方。

接口默认只监听 `127.0.0.1:8765`。安装后台服务时，如
`.env` 和 Windows 环境变量中都没有 `AMAZON_PROCESSOR_API_KEY`，安装脚本会在
`.env` 中生成独立调用密钥。这个密钥只给调用方使用，不能把 DeepSeek、Agnes
或 GPT Image 密钥交给调用方。

提交采集表：

```powershell
$headers = @{ "X-API-Key" = "从服务器 .env 获取的调用密钥" }
$job = Invoke-RestMethod `
  -Method Post `
  -Uri "http://127.0.0.1:8765/api/v1/jobs" `
  -Headers $headers `
  -ContentType "application/json" `
  -InFile ".\跨境电商自动化采集表.json"
$job.data.id
```

查询状态和下载结果：

```powershell
$id = $job.data.id
Invoke-RestMethod `
  -Uri "http://127.0.0.1:8765/api/v1/jobs/$id" `
  -Headers $headers
Invoke-WebRequest `
  -Uri "http://127.0.0.1:8765/api/v1/jobs/$id/result" `
  -Headers $headers `
  -OutFile ".\跨境电商自动化回填表.json"
```

接口一览：

- `POST /api/v1/jobs`：提交有效 Amazon 采集表；相同文件返回相同任务 ID。
- `GET /api/v1/jobs/{id}`：查询排队、运行、重试、阻塞或发布状态。
- `GET /api/v1/jobs/{id}/result`：下载独立交付包中的正式回填表。
- `GET /api/v1/jobs/{id}/review`：下载终审包。
- `GET /api/v1/health`：检查 API 和 Worker；Worker 异常时返回 HTTP 503。

接口限制单个文件 20 MB、最多 10,000 行、每个密钥每分钟最多提交 10 次。
不开放 CORS，也不允许调用方传模型、Prompt、Endpoint 或 Provider 密钥。需要从
其他电脑访问时，优先使用 Tailscale/VPN；公网使用必须在前面增加 HTTPS 反向
代理和 Windows 防火墙白名单，不能直接暴露 Python 端口。

## 测试

```powershell
uv sync --group dev
uv run ruff check amazon_processor tests
uv run pyright
uv run python -m pytest --cov=amazon_processor --cov-fail-under=75 -q
```

测试覆盖 JSON 契约、文本规则、图片安全、Provider 回退、完整管道、
终审包与审核决定。GitHub CI 同样执行 Ruff、Pyright 和最低 75% 覆盖率门禁。

无人值守队列可以在完全不调用 Provider 的情况下做故障注入压测：

```powershell
# 开发机短测
uv run python -m amazon_processor soak --cycles 1000

# 服务器 24 小时稳定性测试（报告不包含密钥）
uv run python -m amazon_processor soak --cycles 0 --duration-hours 24 `
  --interval-seconds 1 --report ".runtime\soak\24小时稳定性报告.json"
```

压测只使用系统临时目录，模拟重复投放、运行中断、状态损坏和受理状态未写完；
不会读取生产收件箱、覆盖正式结果或产生文本、审图、生图费用。
