# Prompt 生命周期与配置档

## 目标

在不修改随程序发布的默认 Prompt 的前提下，支持：

- `production` / `test` Prompt 配置档隔离；
- Web 中安全编辑已注册的 Prompt；
- 每次变更前自动创建不可变快照；
- 按快照回滚，或移除覆盖并恢复发布默认值；
- 模型配置档切换时清除旧的精确模型覆盖。

## 存储边界

```text
crosspilot/prompts/                 发布默认值（只读）
data/prompts/<profile>/             用户覆盖
data/prompt_history/<profile>/      变更前快照
.env                                当前 MODEL_PROFILE / PROMPT_PROFILE
```

目录可分别由 `CROSSPILOT_PROMPT_DIR`、
`CROSSPILOT_PROMPT_HISTORY_DIR` 和 `CROSSPILOT_DATA_DIR` 调整。

外部请求只能提交注册表中的 Prompt ID，不能提交文件路径。覆盖文件沿用注册表
预先定义的相对路径，历史目录使用经过约束的 Prompt ID。

## 写入与校验

保存、回滚和恢复默认均在进程锁内完成，并使用同目录临时文件加
`os.replace` 原子替换。写入前校验：

- 内容非空且不超过 64 KiB；
- Python format 模板语法有效；
- 模板变量集合与发布默认值完全一致。

这样可以允许调整措辞和结构，同时防止删除调用方必需变量或引入调用方不会提供的
新变量。

## 历史与回滚

每次有效变更前保存当前生效内容，快照包含：

- revision ID、UTC 时间、配置档、Prompt ID；
- 变更原因、内容签名和完整内容。

回滚本身也是一次变更，因此回滚前的内容仍会生成快照，可以再次撤销。

## 配置档切换

`PROMPT_PROFILE` 决定覆盖目录；没有覆盖时自动使用发布默认值。
`MODEL_PROFILE` 决定模型路由。Web 切换模型配置档时清空 `.env` 中旧的精确
模型 ID 覆盖，使新配置档立即按注册表值生效。

系统环境变量仍具有最高优先级；被系统环境变量锁定的配置不会被 Web 实际覆盖。
