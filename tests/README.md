# 测试

当前提供的是最小校验脚本（无需安装 AstrBot）：

```bash
python tests/check_compile.py
```

它会：

1. 对 `plugins/*/main.py` 做语法编译检查
2. 校验 `_conf_schema.json` 是合法 JSON 且每项都有 `type`
3. 校验 `default_config.json` 的键与 schema 一致

运行环境：Python 3.10+。
