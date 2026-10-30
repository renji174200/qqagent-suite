# -*- coding: utf-8 -*-
"""
打分制接话判断：让机器人自己决定要不要开口

规则优先级：
1. 被 @ / 唤醒          -> 照常回复（本插件不介入）
2. 消息里叫她的名字      -> 直接回复
3. 刚回过话后的冷却期内  -> 只对"模糊消息"沉默，叫名字仍然回复
4. 其他群消息           -> 交给便宜的判断模型决定 回 / 不回

配置：data/config/smart_reply_config.json
"""

import asyncio
import json
import re
import time
from datetime import datetime
from pathlib import Path

from astrbot.api import logger
from astrbot.api.web import error_response, json_response, request
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
from astrbot.core.message.message_event_result import MessageEventResult

_DATA_DIR = Path(__file__).resolve().parents[2]  # data dir
CFG_PATH = _DATA_DIR / "config" / "smart_reply_config.json"
DEFAULTS = {
    "enable": True,
    "judge_provider_id": "glm-4-flash-250414",
    "cooldown_sec": 120,
    "context_size": 10,
    "min_text_len": 3,
    "name_keywords": [],
    "force_keywords": [
        "说话来", "说句话", "回我", "回句话", "别装死", "装什么死",
        "你说话", "理我", "出来", "狂什么", "你倒是说", "吭声",
    ],
    "max_judge_per_minute": 8,
    "quiet_hours": "1-8",
    "force_question_marks": True,
    "judge_threshold": 60,
    "recent_reply_bonus": 15,
    "recent_reply_window_sec": 300,
    "dedupe_window_sec": 30,
    "decision_log": True,
    "require_signal": True,
    "signal_keywords": ["你", "鱼", "肥", "臭", "?", "？", "吗", "呢"],
    "ignore_senders": [],
    "ignore_sender_ids": [],
    "ignore_bot_names": ["机器人", "bot", "GPT", "Claude"],
    "group_overrides": {},
}

SELF_LABEL = "机器人（你）"

PUNCT_ONLY = re.compile(r"^[\s\W_]+$", re.UNICODE)

DEFAULT_JUDGE_PROMPT = """你是QQ群聊分析器，判断最新一条消息是不是在跟机器人说话。
机器人也叫：{aliases}。

给「这条消息在跟机器人说话」的程度打 0-100 分：
- 80~100：叫了它的名字，或在接它刚说的话，或明确在问它
- 40~79：话题明显指向它但没点名（例如“那个只会吃白饭的”）
- 0~39：群友互聊、聊别人、聊游戏/价格/日常、感叹、复读、发图片

以下情况一律 0~20 分：
- @ 的是别人，或消息里带着别人的 @
- 群友之间在互相说话
- 单纯感叹、哈哈哈、发图片、发链接
- 拿不准是不是在跟它说话

宁可漏接，也不要乱接。

示例：
最近：张三：机器人在吗 / 最新：张三：说话啊 → 95
最近：李四：你胖了 @张三 / 最新：李四：真的胖了 → 5
最近：机器人：打呗，别通宵 / 最新：张三：我赢了 → 88
最近：A：这游戏真难 / 最新：B：换个鼠标试试 → 3
最近：张三：这个 AI 现在不乱回消息了 / 最新：李四：那挺好 → 8"""


@register("smart_reply", "dsh", "由模型判断是否接话，替代概率主动回复", "0.1.0")
class SmartReply(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self.cfg = dict(DEFAULTS)
        self._cfg_mtime = 0
        self._load_cfg()
        self._ctx: dict[str, list[dict]] = {}
        self._last_reply: dict[str, float] = {}
        self._judge_log: dict[str, list[float]] = {}
        try:
            context.register_web_api(
                "/smart_reply/replay", self.page_replay, ["GET"], "判断回放数据"
            )
            context.register_web_api(
                "/smart_reply/label", self.page_label, ["POST"], "标注判断结果"
            )
        except Exception as e:
            logger.warning(f"[smart_reply] 注册 Web API 失败: {e}")
        logger.info(
            f"[smart_reply] 已加载：判断模型={self.cfg['judge_provider_id']} "
            f"冷却={self.cfg['cooldown_sec']}s 关键词={self.cfg['name_keywords']}"
        )

    def _load_cfg(self) -> None:
        try:
            mtime = CFG_PATH.stat().st_mtime
            if mtime == self._cfg_mtime:
                return
            cfg = dict(DEFAULTS)
            cfg.update(json.loads(CFG_PATH.read_text(encoding="utf-8-sig")))
            self.cfg = cfg
            self._cfg_mtime = mtime
        except Exception:
            pass

    def _group_override(self, umo: str) -> dict:
        """按群读取覆盖配置；支持 dict 或 JSON 字符串。"""
        raw = self.cfg.get("group_overrides") or {}
        if isinstance(raw, str):
            try:
                raw = json.loads(raw or "{}")
            except Exception:
                raw = {}
        if not isinstance(raw, dict):
            return {}
        gid = str(umo).rsplit(":", 1)[-1]
        ov = raw.get(gid) or raw.get(str(gid)) or {}
        return ov if isinstance(ov, dict) else {}

    def _in_quiet_hours(self, ov: dict | None = None) -> bool:
        try:
            spec = str((ov or {}).get("quiet_hours", self.cfg.get("quiet_hours")) or "").strip()
            if not spec or "-" not in spec:
                return False
            a, b = spec.split("-", 1)
            start, end = int(a), int(b)
            h = datetime.now().hour
            return (start <= h < end) if start <= end else (h >= start or h < end)
        except Exception:
            return False

    def _judge_quota(self, umo: str) -> bool:
        now = time.time()
        arr = [t for t in self._judge_log.get(umo, []) if now - t < 60]
        self._judge_log[umo] = arr
        limit = int(self.cfg.get("max_judge_per_minute", 8))
        if limit > 0 and len(arr) >= limit:
            return False
        arr.append(now)
        return True

    def _track(self, umo: str, name: str, text: str) -> None:
        buf = self._ctx.setdefault(umo, [])
        buf.append({"name": name, "text": text, "ts": time.time()})
        if len(buf) > int(self.cfg.get("context_size", 6)):
            del buf[: len(buf) - int(self.cfg.get("context_size", 6))]

    async def _ask_judge(self, umo: str, sender_name: str, text: str, ctx_n: int | None = None):
        """返回 (分数 0-100, 原始回答)。失败返回 (0, "")"""
        try:
            provider = self.context.get_provider_by_id(self.cfg["judge_provider_id"])
            if provider is None:
                provider = self.context.get_using_provider()
            if provider is None:
                return 0, ""
            if not hasattr(self, "_judge_lock"):
                self._judge_lock = asyncio.Lock()
            async with self._judge_lock:
                for attempt in range(3):
                    try:
                        return await self._call_judge(provider, umo, sender_name, text, ctx_n)
                    except Exception as e:
                        msg = str(e)
                        if "429" in msg or "1302" in msg or "1305" in msg:
                            await asyncio.sleep(1.5 + attempt)
                            continue
                        raise
                fb_id = str(self.cfg.get("judge_fallback_provider_id") or "").strip()
                if fb_id and fb_id != self.cfg.get("judge_provider_id"):
                    fb = self.context.get_provider_by_id(fb_id)
                    if fb is not None:
                        logger.warning(f"[smart_reply] 主判断模型限流，改用备用 {fb_id}")
                        try:
                            return await self._call_judge(fb, umo, sender_name, text, ctx_n)
                        except Exception as e:
                            logger.warning(f"[smart_reply] 备用判断也失败: {e}")
            return 0, ""
        except Exception as e:
            logger.warning(f"[smart_reply] 判断失败: {e}")
            return 0, ""

    async def _call_judge(self, provider, umo: str, sender_name: str, text: str, ctx_n: int | None = None):
        try:
            lines = []
            ctx_n = int(ctx_n or self.cfg.get("context_size", 10))
            for i, m in enumerate(self._ctx.get(umo, [])[-ctx_n:], 1):
                lines.append(f"{i}. {m['name']}：{m['text']}")
            convo = "\n".join(lines)
            aliases = "、".join(self.cfg.get("name_keywords") or ["机器人"])
            template = str(self.cfg.get("judge_prompt") or "").strip() or DEFAULT_JUDGE_PROMPT
            system_prompt = template.replace("{aliases}", aliases)
            user_prompt = (
                "最近消息：\n" + convo +
                "\n最新消息：" + sender_name + "：" + text +
                "\n\n【打分任务】请忽略前面关于「只回答一个词」的要求，"
                "给「这条消息是在跟它说话」的程度打一个 0-100 的整数分："
                "100=明显在叫它或接它的话，50=有点像但拿不准，0=完全与它无关。"
                "只输出这个整数，不要回答肥鱼/不回，也不要解释。"
            )
            resp = await provider.text_chat(prompt=user_prompt, system_prompt=system_prompt)
            raw = (resp.completion_text or "").strip()
            m = re.search(r"\d{1,3}", raw)
            score = int(m.group(0)) if m else 0
            score = max(0, min(100, score))
            if not m and raw.startswith("肥鱼"):
                score = 100
            self._log_decision(umo, sender_name, text, score, raw)
            return score, raw
        except Exception as e:
            msg = str(e)
            if "429" in msg or "1302" in msg or "1305" in msg:
                raise
            logger.warning(f"[smart_reply] 判断失败: {e}")
            return 0, ""

    def _log_decision(self, umo: str, sender: str, text: str, score: int, raw: str):
        if not self.cfg.get("decision_log", True):
            return
        try:
            import os, json as _json, pathlib
            d = _DATA_DIR / "plugin_data" / "smart_reply"
            d.mkdir(parents=True, exist_ok=True)
            rec = {"ts": time.time(), "umo": umo, "sender": sender,
                   "text": text[:200], "score": score, "raw": raw[:60]}
            with open(d / "decisions.jsonl", "a", encoding="utf-8") as f:
                f.write(_json.dumps(rec, ensure_ascii=False) + "\n")
        except Exception:
            pass

    @staticmethod
    def _force_reply(event: AstrMessageEvent) -> None:
        """让这条没被 @ 的群消息走默认 LLM 链路。
        注意：should_call_llm(True) 是"禁止默认 LLM"，别用。
        """
        try:
            event.call_llm = False          # 允许默认 LLM
            event.is_at_or_wake_command = True
            event.is_wake = True
            event.set_extra("smart_reply_forced", True)
            event.set_result(MessageEventResult().continue_event())  # 解除 waking 的 stop
        except Exception as e:
            logger.warning(f"[smart_reply] 强制回复失败: {e}")

    @filter.on_llm_request()
    async def hint_forced(self, event: AstrMessageEvent, req) -> None:
        """提醒主模型：这条消息其实没有 @ 你。"""
        try:
            if not event.get_extra("smart_reply_forced"):
                return
            req.system_prompt = (req.system_prompt or "") + (
                "\n\n【注】这条群消息没有 @ 你，是你自己判断该接话才被唤醒的。"
                "如果消息里的 @ 指向别人，不要当成在叫你，也不要问“艾特我干嘛”。"
            )
        except Exception:
            pass

    @filter.on_decorating_result(priority=-100)
    async def capture_bot_reply(self, event: AstrMessageEvent):
        """把机器人自己刚发的话也记进上下文，否则判断层看不到它在接谁的话。"""
        try:
            if event.get_platform_name() != "aiocqhttp" or event.is_private_chat():
                return
            result = event.get_result()
            if not result or not result.chain:
                return
            text = "".join(
                getattr(c, "text", "") or ""
                for c in result.chain
                if type(c).__name__ == "Plain"
            ).strip()
            if not text:
                return
            self._track(event.unified_msg_origin, SELF_LABEL, text[:200])
        except Exception:
            pass

    def _is_bot_sender(self, event, name: str) -> bool:
        """识别其它机器人 / 指定忽略的账号，避免 bot 之间互相对话。"""
        try:
            sid = str(event.get_sender_id() or "")
            ids = [str(x).strip() for x in (self.cfg.get("ignore_sender_ids") or []) if str(x).strip()]
            if sid and sid in ids:
                return True
            low = (name or "").lower()
            for kw in (self.cfg.get("ignore_bot_names") or []):
                k = str(kw).strip()
                if k and (k in (name or "") or k.lower() in low):
                    return True
            raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
            if isinstance(raw, dict):
                sender = raw.get("sender") or {}
                if sender.get("is_robot") is True:
                    return True
        except Exception:
            pass
        return False

    @filter.event_message_type(filter.EventMessageType.ALL, priority=10)
    async def on_message(self, event: AstrMessageEvent):
        try:
            self._load_cfg()
            if not self.cfg.get("enable", True):
                return
            if event.get_platform_name() != "aiocqhttp":
                return
            if event.is_private_chat():
                return  # 私聊不参与判断，照常回

            text = (event.message_str or "").strip()
            name = event.get_sender_name() or str(event.get_sender_id() or "")
            umo = event.unified_msg_origin
            ov = self._group_override(umo)
            if ov.get("enable") is False:
                return

            # 别的机器人 / 指定忽略的人，不参与判断
            ign = [str(x) for x in (self.cfg.get("ignore_senders") or []) if str(x).strip()]
            if any(x in name for x in ign):
                return
            if self._is_bot_sender(event, name):
                logger.info(f"[smart_reply] 跳过其它机器人/忽略账号：{name}")
                return

            # @ / 唤醒的消息交给正常流程，本插件不介入
            if event.is_at_or_wake_command:
                self._track(umo, name, text)
                return

            # @ 的是别人（不是她）→ 不要接，否则会把别人的 @ 当成叫她
            at_targets = []
            try:
                for comp in event.get_messages():
                    if type(comp).__name__ == "At":
                        at_targets.append(str(getattr(comp, "qq", "") or ""))
            except Exception:
                pass
            if at_targets:
                self_id = str(event.get_self_id() or "")
                if self_id and self_id not in at_targets:
                    self._track(umo, name, text or "[@别人]")
                    return

            if not text or len(text) < int(self.cfg.get("min_text_len", 3)):
                self._track(umo, name, text)
                return
            if self.cfg.get("skip_commands", True) and text.startswith("/"):
                return
            if PUNCT_ONLY.match(text):
                self._track(umo, name, text)
                return

            self._track(umo, name, text)

            # 同一条消息短时间重复出现，直接跳过
            now_ts = time.time()
            dk = (umo, text)
            if not hasattr(self, "_recent_msgs"):
                self._recent_msgs = {}
            last_ts = self._recent_msgs.get(dk, 0)
            if now_ts - last_ts < float(self.cfg.get("dedupe_window_sec", 30)):
                return
            self._recent_msgs[dk] = now_ts
            if len(self._recent_msgs) > 500:
                cutoff = now_ts - 600
                self._recent_msgs = {k: v for k, v in self._recent_msgs.items() if v > cutoff}

            # 硬规则：叫名字直接回（无视冷却）
            keywords = [str(k) for k in (self.cfg.get("name_keywords") or []) if str(k).strip()]
            if any(k in text for k in keywords):
                logger.info(f"[smart_reply] 命中名字，直接接话：{text[:40]}")
                self._force_reply(event)
                self._last_reply[umo] = time.time()
                return

            # 硬规则：引用了她的消息（引用消息里出现她的名字/群名片）
            _qm = re.search(r"引用消息[（(](.{0,60}?)[)）]", text)
            if _qm and any(k in _qm.group(1) for k in keywords):
                logger.info(f"[smart_reply] 引用她的话，直接接话：{text[:40]}")
                self._force_reply(event)
                self._last_reply[umo] = time.time()
                return

            # 硬规则：明显在催她说话/质问她，也直接回
            force_kw = [str(k) for k in (self.cfg.get("force_keywords") or []) if str(k).strip()]
            if any(k in text for k in force_kw):
                logger.info(f"[smart_reply] 命中催促词，直接接话：{text[:40]}")
                self._force_reply(event)
                self._last_reply[umo] = time.time()
                return

            # 她刚说过话，且这条是紧接着她说的第一句 —— 才算承接（默认关闭）
            try:
                hist = self._ctx.get(umo, [])[:-1] if self.cfg.get("enable_continuation", False) else []
                if hist and str(hist[-1].get("name", "")).startswith("机器人"):
                    if time.time() - float(hist[-1].get("ts", 0)) < 90 and len(text) <= 12:
                        logger.info(f"[smart_reply] 承接她上一句，直接接话：{text[:30]}")
                        self._force_reply(event)
                        self._last_reply[umo] = time.time()
                        return
            except Exception:
                pass

            if self._in_quiet_hours(ov):
                return

            # 粗筛：没有指向性词的消息基本是群友闲聊，不值得花判断成本
            if self.cfg.get("require_signal", True):
                sig = self.cfg.get("signal_keywords") or ["你", "鱼", "肥", "?", "？", "吗", "呢"]
                if not any(str(s) in text for s in sig):
                    return

            # 冷却期内，模糊消息不判断
            cd = float(self.cfg.get("cooldown_sec", 30))
            if time.time() - self._last_reply.get(umo, 0) < cd:
                logger.info(f"[smart_reply] 冷却中（{cd:.0f}s），跳过判断：{text[:30]}")
                return

            if not self._judge_quota(umo):
                return

            ctx_n = int(ov.get("context_size") or 0) or None
            score, raw = await self._ask_judge(umo, name, text, ctx_n=ctx_n)
            threshold = float(ov.get("judge_threshold", self.cfg.get("judge_threshold", 60)))
            window = float(self.cfg.get("recent_reply_window_sec", 300))
            if time.time() - float(self._last_reply.get(umo, 0)) < window:
                threshold += float(ov.get("recent_reply_bonus", self.cfg.get("recent_reply_bonus", 15)))
            logger.info(f"[smart_reply] 判断 {name}「{text[:30]}」-> {score}分 (阈值{threshold:.0f}) => {'回' if score >= threshold else '不回'}")
            if score >= threshold:
                self._force_reply(event)
                self._last_reply[umo] = time.time()
        except Exception as e:
            logger.warning(f"[smart_reply] 处理失败: {e}")

    # ---------------- 判断回放面板 ----------------
    def _replay_dir(self):
        return _DATA_DIR / "plugin_data" / "smart_reply"

    def _read_jsonl(self, path, limit: int = 500):
        rows = []
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rows.append(json.loads(line))
                    except Exception:
                        continue
        except Exception:
            return []
        return rows[-limit:]

    def _label_map(self):
        rows = self._read_jsonl(self._replay_dir() / "labels.jsonl", 5000)
        return {str(r.get("key")): r for r in rows if r.get("key")}

    async def page_replay(self):
        try:
            limit = request.query.get("limit", 200, type=int)
            d = self._replay_dir()
            decisions = self._read_jsonl(d / "decisions.jsonl", max(1, min(limit, 1000)))
            labels = self._label_map()
            for r in decisions:
                r["key"] = f"{float(r.get('ts', 0)):.3f}"
                r["label"] = labels.get(r["key"], {}).get("label", "")
                r["group"] = str(r.get("umo", "")).rsplit(":", 1)[-1]
            buckets = {"0-19": 0, "20-39": 0, "40-59": 0, "60-79": 0, "80-100": 0}
            for r in decisions:
                sc = int(r.get("score") or 0)
                if sc < 20:
                    buckets["0-19"] += 1
                elif sc < 40:
                    buckets["20-39"] += 1
                elif sc < 60:
                    buckets["40-59"] += 1
                elif sc < 80:
                    buckets["60-79"] += 1
                else:
                    buckets["80-100"] += 1
            return json_response(
                {
                    "threshold": self.cfg.get("judge_threshold", 60),
                    "bonus": self.cfg.get("recent_reply_bonus", 15),
                    "group_overrides": self.cfg.get("group_overrides", {}),
                    "buckets": buckets,
                    "total": len(decisions),
                    "items": decisions[::-1],
                }
            )
        except Exception as e:
            return error_response(f"读取判断日志失败: {e}", status_code=500)

    async def page_label(self):
        try:
            payload = await request.json(default={})
            key = str(payload.get("key") or "").strip()
            label = str(payload.get("label") or "").strip()
            if label not in ("ok", "miss", "spam"):
                return error_response("label 必须是 ok / miss / spam")
            if not key:
                return error_response("缺少 key")
            rec = {
                "key": key,
                "label": label,
                "ts": time.time(),
                "text": str(payload.get("text") or "")[:300],
                "score": int(payload.get("score") or 0),
                "group": str(payload.get("group") or ""),
            }
            d = self._replay_dir()
            d.mkdir(parents=True, exist_ok=True)
            with open(d / "labels.jsonl", "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            return json_response({"saved": True})
        except Exception as e:
            return error_response(f"保存标注失败: {e}", status_code=500)
