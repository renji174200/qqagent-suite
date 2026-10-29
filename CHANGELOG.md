# Changelog

## v1.0.0 (2026-10-29)

首个公开版本。

- `smart_reply`：打分制接话判断（0~100 + 阈值）、刚回复加成、重复消息去重、判断日志、三级重试与备用模型
- `bili_watch`：B站视频抽帧理解（默认 12 帧 / 960px）、标题简介字幕三路信息、笔记缓存
- `forward_reader`：嵌套转发递归展开、QQ 卡片解析、引用内容拉取
- `poke_tool`：模型主动戳人，带每日上限与冷却
- `reply_target`：群里自动引用、私聊可选 @
- `mimo_sing`：MiMo TTS 唱歌
- 全部插件提供 `_conf_schema.json`，可在面板内配置
