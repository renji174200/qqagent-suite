# 全智能QQagent · 引用（reply_target）

让回复有明确指向：引用原消息，或 @ 具体的人。

## 优势

- **群里默认引用**：一眼看出在回谁，减少误会
- **私聊可选 @**：需要时再开
- **模型可控**：提供 `reply_target` 工具，她能主动指定对象
- **概率化**：不必每条都引用，避免视觉噪音

## 配置

| 配置 | 默认 | 说明 |
|---|---|---|
| `auto_quote_group` | true | 群里自动引用 |
| `auto_quote_private` | false | 私聊自动引用 |
| `auto_quote_probability` | 0.4 | 自动引用概率 |
| `at_in_private` | false | 私聊用 @ 指向 |
| `cooldown` | 0 | 冷却（秒） |
