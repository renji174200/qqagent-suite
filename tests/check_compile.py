# -*- coding: utf-8 -*-
"""最小校验：语法编译 + schema 合法性 + 默认配置键一致。"""
import json
import pathlib
import py_compile
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
ok = True

for p in sorted((ROOT / "plugins").glob("*/main.py")):
    try:
        py_compile.compile(str(p), doraise=True)
        print(f"[OK] compile {p.parent.name}")
    except Exception as e:
        ok = False
        print(f"[FAIL] compile {p.parent.name}: {e}")

for d in sorted((ROOT / "plugins").iterdir()):
    if not d.is_dir():
        continue
    schema_path = d / "_conf_schema.json"
    cfg_path = d / "default_config.json"
    if not schema_path.exists():
        print(f"[SKIP] {d.name} 无 schema")
        continue
    try:
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
    except Exception as e:
        ok = False
        print(f"[FAIL] schema JSON {d.name}: {e}")
        continue
    missing = [k for k, v in schema.items() if "type" not in v]
    if missing:
        ok = False
        print(f"[FAIL] schema 缺 type {d.name}: {missing}")
    else:
        print(f"[OK] schema {d.name} ({len(schema)} keys)")

    if cfg_path.exists():
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        extra = set(cfg) - set(schema)
        if extra:
            print(f"[WARN] default_config 有 schema 外键 {d.name}: {sorted(extra)}")
        else:
            print(f"[OK] default_config {d.name}")

print("\n结果:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
