# qqagent-suite

> 给 AstrBot 加一套「会看、会判断、会接梗」的 QQ 群聊增强插件。
> 当前版本 **v2.0.0**。A suite of QQ group-chat plugins for [AstrBot](https://github.com/AstrBotDevs/AstrBot): scored reply judgment, Bilibili video understanding, forward/card parsing, poke, quote targeting and singing.

## 优势 / Highlights

- **打分制接话** — 不再是「回 / 不回」，而是 0~100 分 + 可调阈值；刚说过话自动提高门槛，不会连珠炮。
- **真的会看 B 站** — 均匀抽 12 帧、每帧 960px 交给多模态模型，生成笔记后可记住 2 小时。
- **转发 / 卡片不再瞎** — 嵌套转发递归展开，QQ 小程序 / 网易云 / 公众号卡片解析成「标题 + 链接」。
- **配置全在面板** — 6 个插件全部有 `_conf_schema.json`；模型用下拉框选、数值带滑块，改完保存即生效。
- **成本可调** — 帧数、上下文条数、判断阈值都是显式参数。
- **误判可查** — 每次判断写入 `decisions.jsonl`，可按分数分布调阈值。
- **零依赖** — 只用 Python 标准库与 AstrBot 自带依赖。

## 插件列表

| 目录 | 面板显示名 | 作用 | 配置项 |
|---|---|---|---|
| `smart_reply` | 接话 | 打分判断该不该接话 | 21 |
| `bili_watch` | B站 | 抽帧看视频并记住内容 | 10 |
| `forward_reader` | 看转发增强 | 展开转发 / 解析卡片 | 4 |
| `poke_tool` | 戳 | 让模型主动戳一戳 | 11 |
| `reply_target` | 引用 | 回复时引用 / @ 指向 | 6 |
| `mimo_sing` | 唱歌 | MiMo TTS 唱歌 | 5 |

## 安装

### 方式一：脚本

```bash
git clone https://github.com/<you>/qqagent-suite.git
cd qqagent-suite
bash scripts/install.sh /path/to/astrbot/data/plugins
docker restart astrbot
```

### 方式二：手动

```bash
cp -r plugins/* /path/to/astrbot/data/plugins/
docker restart astrbot
```

插件首次启动会自动生成配置文件（`data/config/<插件目录名>_config.json`），随后在面板里逐项调整即可。

也可以把各插件目录下的 `default_config.json` 预先复制成对应配置文件名。

## 快速配置

装好后**只需要改一项就能跑**：

| 插件 | 配置 | 改成 |
|---|---|---|
| 接话 | `name_keywords` | 你的机器人名字 / 群名片 |
| B站 | `provider_id` | 留空则用当前对话模型（需支持图片） |
| 唱歌 | `api_key` | 你的 MiMo API Key（可选功能） |

其余参数默认值可直接使用。

## 配置文件位置

AstrBot 按**插件目录名**命名配置：

```
data/config/smart_reply_config.json
data/config/bili_watch_config.json
data/config/forward_reader_config.json
data/config/poke_tool_config.json
data/config/reply_target_config.json
data/config/mimo_sing_config.json
```

> 如果改了文件名，面板里的修改不会生效。

## 兼容性

- AstrBot **>= 4.25**（使用 `on_decorating_result`、`llm_tool`）
- Python **>= 3.10**
- 平台：`aiocqhttp`（OneBot v11，SnowLuma / NapCat 均可）

## 文档

- [配置说明](docs/配置说明.md)
- [常见问题](docs/常见问题.md)
- [更新日志](CHANGELOG.md)

## 免责声明

本项目仅供学习与自用。请遵守所在群规、平台规则与当地法律法规，**不要用于刷屏、骚扰或任何滥用场景**。默认配置已内置冷却与频率限制，请勿为了“更活跃”而盲目调高。使用本项目的风险由使用者自行承担。

## License

[MIT](LICENSE)
