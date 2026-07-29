# Amazon JSON 核心处理器

这个项目只做一件事：

```text
Amazon JSON 采集表
  → 图片安全初审与修复
  → 标题、描述、Bullet、关键词处理
  → 12 字段回填表
  → 离线终审包
```

## 使用

1. 复制 `.env.example` 为 `.env`，填写 API 密钥。
2. 把 Amazon 采集表 JSON 拖到 `处理Amazon采集表.bat`。
3. 在 `输出\最新` 查看：
   - `跨境电商自动化回填表.json`
   - `终审包.html`
   - `审核数据.json`
   - `图片\`
4. 在终审包中导出 `审核决定.json` 后，把它拖到
   `应用审核决定.bat`。

运行环境会安装到
`%LOCALAPPDATA%\AmazonProcessor\venv`，项目目录不创建 `.venv`。

## 配置

- `.env`：只保存 `DEEPSEEK_KEY`、`AGNES_KEY` 和
  `GPT_IMAGE_KEY`。
- `config/settings.json`：模型、端点、回退链、并发及重试参数。
- `config/prompts/*.txt`：文本、审图和生图 Prompt。

修改模型或 Prompt 后缓存签名会自动变化，不会误用旧结果。

## 业务规则

- 标题尽量接近且不超过 75 个字符；适配品牌标题使用
  `Generic [产品] for [适配品牌/型号]`。
- 有源描述的商品生成 5 条 Bullet 和 10 组关键词；缺失源描述不虚构。
- 所有图片必须通过结构化初审。风险附图删除；风险主图和变种图修复后
  再次复审；未知、冲突或修复失败的商品隔离。
- 生图顺序为 Agnes 2.1 → Agnes 2.0 → GPT Image，并支持
  503 快速重试、熔断、并发降级和缓存续跑。

## Module 与 Interface

- 外部 Interface：两个 BAT 文件。
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
