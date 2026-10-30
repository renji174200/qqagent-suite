# 全智能QQagent · 接话（smart_reply）

判断「这条消息该不该回」，让机器人自己决定什么时候开口。

## 优势

- **打分制**：模型给 0~100 分，≥ 阈值才回，比二分类更可控
- **刚说过话会更安静**：回复后 N 秒内阈值自动 +15，避免刷屏
- **三级兜底**：429 重试 3 次 → 备用模型 → 放弃判断（判不回）
- **决策可回溯**：每次判断写 JSONL，能直接统计误判
- **硬规则保底**：叫名字、引用她的话、催促词仍然直通，不依赖模型

## 配置要点

| 配置 | 默认 | 说明 |
|---|---|---|
| `judge_threshold` | 60 | 越高越安静 |
| `recent_reply_bonus` | 15 | 刚说完话时的加分门槛 |
| `context_size` | 30 | 判断看多少条历史 |
| `name_keywords` | 空 | **必填**：机器人名字/外号 |
| `judge_provider_id` | 空 | **下拉选择**判断模型；留空用当前对话模型 |
| `decision_log` | true | 写判断日志 |

## 用法

装好即生效。调阈值即可控制活跃度：

```
太吵 → judge_threshold 70，recent_reply_bonus 25
太静 → judge_threshold 50
```

## 判断日志

```
data/plugin_data/smart_reply/decisions.jsonl
```

每行含 `score`、`text`、`sender`，可直接统计分数分布。

## 判断回放面板

插件详情页 →「判断回放」：

- 查看最近 300 条判断（群、发送者、内容、分数）
- 分数分布柱状图，一眼看出阈值是否合适
- 逐条标注：`对` / `该回没回` / `不该回回了`
- 点「导出 few-shot」生成提示词样例，粘贴回 `judge_prompt` 即可提升准确率

## 分群配置

`group_overrides` 支持按群覆盖参数：

```json
{
  "751997685": {"judge_threshold": 70, "recent_reply_bonus": 20},
  "111627212": {"judge_threshold": 50, "quiet_hours": ""},
  "722324569": {"enable": false}
}
```

可覆盖：`judge_threshold`、`recent_reply_bonus`、`context_size`、`quiet_hours`、`enable`。
