# Domain Docs

## 布局

**单仓库模式** — 一个 `CONTEXT.md` + `docs/adr/` 在项目根目录。

## AI 技能读取规则

以下工程技能会读取 `CONTEXT.md` 了解项目领域语言，读取 `docs/adr/` 了解架构决策：

- `improve-codebase-architecture` — 基于领域语言找优化机会
- `diagnose` — 理解系统边界和依赖
- `tdd` — 测试策略对齐架构决策
