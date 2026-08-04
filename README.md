# Amazon JSON 核心处理器

> 第一次接触项目或准备改代码时，先看
> [可视化代码地图](代码地图.html)：业务流程、模块边界、模型回退和修改入口都在一页里。

这个项目只做一件事：

```text
Amazon JSON 采集表
  → 图片安全初审与修复
  → 按产品站点本地化标题、副标题、描述、Bullet、关键词
  → 14 字段回填表
  → 离线终审包
```

## 使用

1. 第一次使用时双击 `03_配置管理.bat`，填写 API 密钥并检查配置。
2. 把 Amazon 采集表 JSON 拖到 `01_处理Amazon采集表.bat`。
3. 在 `02_处理结果\最新` 查看：
   - `跨境电商自动化回填表.json`
   - `终审包.html`
   - `审核数据.json`
   - `图片\`
4. 在终审包中导出 `审核决定.json` 后，把它拖到
   `02_应用审核决定.bat`。

运行环境会安装到
`%LOCALAPPDATA%\AmazonProcessor\venv`，项目目录不创建 `.venv`。

## 目录

```text
01_输入采集表\           保留的正式原始采集表
02_处理结果\
├─ 最新\                当前回填表、终审包和全部终审图片
└─ 归档\                每次覆盖前的压缩备份
01_处理Amazon采集表.bat  日常处理入口
02_应用审核决定.bat      应用人工终审结果
03_配置管理.bat          密钥、模型、Prompt 和并发管理
amazon_processor\        核心程序
config\                  模型、并发和 Prompt
tests\                   离线测试
```

## 配置管理

- 推荐双击 `03_配置管理.bat`，在本机页面统一修改 API 密钥、
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

修改模型或 Prompt 后缓存签名会自动变化，不会误用旧结果。
商品处理期间配置只读，避免同一批商品混用两套 Prompt。

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
- 有效源描述的商品生成 5 条 Bullet 和 10 组关键词；源描述为空、
  清洗后没有产品内容，或风险附图删除后没有任何可用附图时，自动删除
  整条商品数据，并把 ID 写入 `有问题的产品id`。
- `有问题的产品id` 只表示需要上游删除的商品；普通模型回退和文案
  质检提示只记录在终审数据中。
- 所有图片必须通过结构化初审。风险附图删除；风险主图和变种图修复后
  再次复审；未知、冲突或修复失败的商品隔离。
- 生图顺序为 Agnes 2.1 → Agnes 2.0 → GPT Image，并支持
  503 快速重试、熔断、并发降级和缓存续跑。

## Module 与 Interface

- 外部 Interface：处理、应用审核决定、配置管理三个 BAT 文件。
- Python Interface：`amazon_processor.process_json(input_path) -> RunResult`。
- `amazon_processor/` 是唯一生产 Module；文本、图片、Provider、终审均为
  内部 Implementation，不保留历史 Adapter。
- `.runtime/cache/` 保存续跑缓存和唯一一份共享图片缓存。

## 测试

```powershell
uv sync --group dev
uv run pytest -q
```

测试覆盖 JSON 契约、文本规则、图片安全、Provider 回退、完整管道、
终审包与审核决定。
