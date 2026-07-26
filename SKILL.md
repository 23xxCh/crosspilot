---
name: "ebay-tiktok-clean"
description: "eBay→TikTok Shop 越南站表格批量清洗：翻译越南语、去品牌/policy、图审去水印、图生图替换、图片注入描述"
---

# Skill: eBay→TikTok Shop Listing 清洗（越南站）

处理 eBay 来源的产品表格，输出符合 TikTok Shop 越南站要求的清洗后表格。
脚本：`scripts/process_ebay_tk.py`。

## 用法

**主入口（本地 Web 平台，推荐）**：双击 `run_web.bat` → 浏览器自动打开 `http://localhost:8765`。网页三 tab：
- **处理**：拖入 xlsx → 实时进度（阶段 x/10 + 百分比 + ETA，SSE 每秒推送）→ 完成下载
- **历史**：所有任务列表，可重下/删除，点详情看每行水印/生图明细
- **设置**：填 DMXAPI / Agnes key（掩码显示，保存写回 keys.json）

代码在 `web/`（FastAPI + 单页 HTML），管道 `scripts/process_ebay_tk._main` 零改动 import 复用。手动起服务：`uv run uvicorn web.app:app --port 8765`。

**纯命令行（fallback）**：
```bash
uv run python -u scripts/process_ebay_tk.py "<输入文件.xlsx>"   # 单文件
uv run python -u scripts/batch_process.py "<文件夹路径>"        # 批量队列（拖文件夹到 run_ebay_tk.bat）
```

**首次使用**：复制 `keys.example.json` 为 `keys.json`，填入 DMXAPI 和 Agnes 密钥。`keys.json` 已加入 .gitignore，不会泄漏。

输出：`<输入文件>_cleaned.xlsx`（原文件不改；已存在时自动加时间戳防覆盖）。

### 交付/部署注意

- **表头校验**：脚本启动时校验关键列（标题/描述/价格/展示价/库存/主图/视频/变种），TikTok 改导出格式会报警中止，不会删错列
- **断点缓存**：图审+生图结果存 `<文件名>_cache.json`（含文件 mtime 校验），崩溃/重跑时命中直接跳过。删除该文件可强制全量重跑
- **Agnes 配额**：Token Plan 日配额 4000 张，跑大批量前注意余额
- **水印 prompt 泛化**：94% 准确率是在卖家 ID 平铺水印（liazh-93 风格）上验证的；新卖家水印风格不同时，建议先抽查 10 张人工核对

### 架构：适配器模式（支持多来源表格）

系统不写死列号，启动时**自动识别表格格式**并注入列映射。eBay/Shopee/Amazon 等不同来源各配一个适配器，主流程完全不动。

```
[任意来源表格] → 适配器识别(detect) → 注入列映射(cols) → 10阶段处理 → 输出清洗表
```

- 适配器在 `scripts/adapters/`，现有 `ebay_tk.py`（eBay→TikTok 45列）
- **新增来源**：照 `scripts/adapters/README.md` 模板，复制一个适配器填 `detect`+`cols`，注册进 `__init__.py` 即可
- 不认识的格式会打印全部表头并中止，不会硬跑删错列

### 批量看板（可视化）

`batch_process.py` 处理时在文件夹根目录生成 `dashboard.html`（自刷新 5s）：
- 总进度条 + ✅/❌/⏳/⏸ 文件列表 + 失败可点看原因
- 当前文件的阶段进度（x/10、x/y、ETA），处理中按节流实时刷新
- 非技术用户双击就能看，无需服务器

### 进度监控（agent 必读）

脚本运行时在**输入文件同目录**写 `<输入文件名>_status.json`，实时更新：

```json
{
  "stage": "MiMo图审",       // 当前阶段名
  "stage_index": 2,          // 第几阶段
  "stage_total": 10,         // 共 10 阶段
  "current": 450, "total": 920,
  "percent": 49,
  "elapsed_s": 120,          // 本阶段已用秒
  "eta_s": 130,              // 预计剩余秒
  "updated_at": "..."
}
```

**agent 监控方式**：后台运行脚本后，定期 Read 该 status.json，向用户汇报"阶段 x/10 + 百分比 + 预计剩余时间"。任务完成时文件内容为 `{"stage": "完成", "output": "<输出路径>"}`。

10 个阶段：提取图片URL → MiMo图审 → Agnes图生图 → 附图清空 → 标题清洗+翻译 → 描述AI清洗 → 描述翻译 → 嵌入+注入图片 → 视频+模板图清理 → 价格列+保存。

---

## API 分工

| 任务 | 服务 | Base URL | 模型 |
|------|------|----------|------|
| 多模态图审（判定水印） | **DMXAPI** 中转站 | `https://www.dmxapi.cn/v1` | `mimo-v2.5` |
| 文本翻译（标题/描述→越南语） | **DMXAPI** 中转站 | 同上 | `deepseek-v4-flash` |
| 描述 AI 清洗（去品牌/policy） | **DMXAPI** 中转站 | 同上 | `deepseek-v4-flash` |
| 图生图去水印（img2img，返 URL） | **DMXAPI** 万相（主力） | `https://www.dmxapi.cn/v1/responses` | `wan2.7-image` |
| 图生图备 1 | **DMXAPI** 豆包 | 同上 | `doubao-seedream-5.0-lite` |
| 图生图备 2 | **Agnes** | `https://apihub.agnes-ai.com/v1` | `agnes-image-2.0-flash` |

> 生图三级 fallback：万相(~8s/张) → 豆包(~22s/张) → Agnes(~27s+排队)。并发 10，实测吞吐 63 张/分钟（Agnes 约 10 张/分钟）。都返 URL，`watermark:false` 关闭 AI 水印。

> DMXAPI key 在脚本 `DMX_KEY`（不限 RPM，图审并发 100 实测 73s/920 张；deepseek-v4-flash 翻译 ~1.3s/次，2500 并发）。
> Agnes key 在 `agnes_key.txt`（Token Plan，日配额 4000 张；队列限流自动等 30s 重试）。

---

## 表格特征

- 工作表：`tiktok_chanpin_`，45 列
- 平台：TikTok Shop Joyon Shop_VN（越南站，币种 VND，仓库广州仓）

### 关键列定位（列号 = 表头从 1 起）

| 列号 | 表头 | 处理 |
|:---:|------|------|
| 2 | 产品标题 | 规则去品牌 + **xfyun 翻译成越南语**（去重：相同标题只翻一次） |
| 3 | Tiktok产品描述 | **xfyun AI 清洗**（只留产品特性）+ **翻译越南语**（去重）+ 注入主图附图 URL（去重）+ 删 pushauction 模板图 |
| 15 | 价格(站点币种) | **改名** → 本地展示价 |
| 16 | 本地展示价 | **删除整列** |
| 17 | 库存 | **不动** |
| 18 | 主图(url)地址 | 有水印 → **Agnes 图生图替换** |
| 19-26 | 附图一~八 | 有水印 → **清空删除** |
| 27 | 视频连接 | 有内容 → **清空** |
| 28 | 尺码图 | 纳入附图审查（有水印则清空） |
| 29 | 变种主题1图片 | 像主图一样，有水印则图生图替换（不删除） |
| 36 | 来源Url | 原始 eBay Listing（未使用，仅留作参考） |

---

## 处理流程（SOP）

1. **提取图片 URL** — 主图+附图+变种图去重，记录每个 URL 出现在哪些单元格
2. **DMXAPI MiMo 图审** — 直接传 ebayimg URL（无需下载），并发 100，判定卖家水印/车标。**四级 fallback 链**：MiMo 快重试(3x) → MiMo 慢重试(2x, 5s) → `gemini-3.1-flash-lite-image` fallback(2x) → MiMo+base64 本地下载(1x)。四级全失败的图列入 `unreviewed` 清单（status.json + 日志），按保留原图处理；最终失败率 >10% 才中止
3. **Agnes 图生图** — 对"有水印且在主图/变种图列"的 URL 调 Agnes，并发 5，替换单元格。队列限流自动等 30s 重试
4. **附图清空** — 对"有水印且在附图列"的 URL，清空对应单元格
5. **标题规则清洗** — `BRANDS` 列表正则去品牌名（Toyota/BMW/Shopee/Lazada 等）
6. **标题翻译** — xfyun 英→越，去重（保留产品关键词/型号不译）
7. **描述 AI 清洗** — xfyun 去品牌+去 policy 模板+img→`__IMG__`占位符，去重
8. **描述翻译** — xfyun 翻译越南语（保留格式/`__IMG__`占位符），去重
9. **描述嵌入新 URL** — `__IMG__`占位符→真正的`<img>`标签（图生图新 URL 优先）
10. **注入主图附图 URL** — 删正文已有的主图附图 img 标签（去重）→ 顶部注入产品图区块
11. **删除模板图** — 删除所有 pushauction/ibay365 模板图标签
12. **视频清空** — col27 有内容置空
13. **价格列改名** — col15 表头改"本地展示价"；`ws.delete_cols(16)` 删原本地展示价列
14. **保存** — 输出 `<输入文件>_cleaned.xlsx`

---

## 图审 prompt（实测 94% 准确率，18 张人工验证集）

**只认**：卖家水印（半透明卖家 ID 如 `liazh-93`/`Constituen78` 重复平铺满图）、车标 logo（BMW/Toyota/Honda 叠加在图上）。

**明确排除**：
- 产品规格文字：`2PCS`/`1Set`/`1PC`/`48*30CM`（实心、单处、角落）
- 功能图标/营销横幅：`7 COLOR`、`Soft and Comfortable`、RFID 图标
- 产品自身的字：`SPORT`/`4x4`/`LIMITED EDITION` 徽章（印在产品上的）

> eBay 真水印几乎都是半透明卖家 ID 平铺文字。通用"有没有水印"的 prompt 误报率 ~60%，必须按此特征写。

### 图生图 payload

```python
{
    "model": "agnes-image-2.0-flash",
    "prompt": "Based on the reference image, keep the exact same product unchanged. Only remove watermarks, logos, and brand text. Preserve product appearance, color, shape, and composition exactly.",
    "size": "1024x1024",
    "extra_body": {"image": [image_url], "response_format": "url"}
}
```

---

## 配置项

| 参数 | 值 | 说明 |
|------|------|------|
| `MIMO_CONCURRENCY` | 100 | 图审并发（DMXAPI 不限流） |
| `GEN_CONCURRENCY` | 5 | 生图并发（Agnes 队列限流，自动等 30s 重试） |
| `TEXT_CONCURRENCY` | 10 | 翻译/清洗并发 |
| `MAX_RETRIES` | 8 | 最大重试 |

---

## 关键避坑

1. **curl.exe + payload 临时文件** — shell 转义特殊字符会截断 key；payload 写系统 temp 目录（`tempfile.gettempdir()`），不写工作目录
2. **Git Bash 冒号 key 坑** — Git Bash 里直接 curl 带 `:` 的 key（如 xfyun `apikey:apisecret`）会被 MSYS2 转义导致 401；Python `subprocess.run(shell=True)` 走 cmd.exe 则正常。API 401 时先用 Python 复测再断定 key 失效
3. **xfyun Coding Plan 独立 base URL** — （已弃用，现全部走 DMXAPI）`maas-coding-api` 与常规 `maas-api` 不通用
4. **Agnes 队列限流** — `image queue is full` 时等 30s 重试；`subscription` 限额则跳过保留原图
5. **去重逻辑** — 标题/描述翻译前先 `setdefault` 收集行号，相同原文只调一次 API（160 行→119 唯一文本，省 ~25%）
6. **pushauction 模板图** — eb 模板中大量 `image.pushauction.com` 装饰图，最后统一删除
7. **主图附图 URL 去重** — 注入前先用正则删除正文中已有的同 URL img 标签，避免同图出现两次
8. **列删除放最后** — `delete_cols(16)` 必须放在所有列操作之后，避免列号错位
9. **__IMG__ 占位符** — 描述清洗时替换 img 为占位符，翻译时必须保留占位符原文不动
10. **DMXAPI 图片模型只返 base64** — gpt-image/gemini 系列生图不给 URL；要 URL 用 Agnes

---

## 输入/输出

- **输入**：任意 eBay 来源 xlsx（45 列结构，工作表 `tiktok_chanpin_`）
- **输出**：同目录 `<输入文件>_cleaned.xlsx`

---

## 环境依赖

| 工具 | 用途 |
|------|------|
| Python 3.10+ | 脚本运行 |
| openpyxl | 表格读写（pyproject.toml 已声明，`uv run` 自动安装） |
| curl.exe | API 调用（Windows 10+ 自带） |
| keys.json | API 密钥（DMXAPI + Agnes，参考 keys.example.json） |

---

_最后更新：2026-07-17_
