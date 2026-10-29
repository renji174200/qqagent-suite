import pathlib
# -*- coding: utf-8 -*-
"""
reply_target —— 让模型自己决定这条回复要不要 @ 人 / 引用消息。

和 reply_flair 的区别：reply_flair 是随机加，这个是模型显式调用函数工具来加，
所以“想 @ 谁”“要不要引用”是可控的，不是抽卡。

用法（模型侧）：调用工具 reply_target(mode="at|quote|both|none", qq="123456")
- at    : 在回复开头 @ 对方
- quote : 引用对方那条消息
- both  : 两个都加
- none  : 本次不加（覆盖默认行为）

配置：data/config/reply_target_config.json
{
  "enable": true,
  "at_in_private": false,   # 私聊里 @ 很怪，默认只引用
  "cooldown": 0             # 同一个会话里两次自动加之间的最小秒数，0=不限制
}
"""

import json
import random
import time
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import At, Plain, Reply
from astrbot.api.star import Context, Star, register

_DATA_DIR = pathlib.Path(__file__).resolve().parents[2]  # .../AstrBot/data
CFG_PATH = _DATA_DIR / "config" / "reply_target_config.json"
DEFAULTS = {
    "enable": True,
    "auto_quote_group": True,
    "auto_quote_probability": 0.4,
    "auto_quote_private": False,
    "at_in_private": False,
    "cooldown": 0,
}

VALID_MODES = {"at", "quote", "both", "none"}


@register("reply_target", "dsh", "让模型自己决定 @ 人或引用消息", "0.2.0")
class ReplyTarget(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self.cfg = dict(DEFAULTS)
        try:
            self.cfg.update(json.loads(CFG_PATH.read_text(encoding="utf-8")))
        except Exception:
            pass
        self._last: dict[str, float] = {}
        try:
            self._cfg_mtime = CFG_PATH.stat().st_mtime
        except Exception:
            self._cfg_mtime = 0.0
        logger.info("[reply_target] 已加载，函数工具 reply_target 可用")

    # ---------- 给模型用 ----------
    @filter.llm_tool(name="reply_target")
    async def set_reply_target(
        self, event: AstrMessageEvent, mode: str, qq: str = ""
    ) -> str:
        """设置本次回复的指向方式：@ 某人 或 引用某条消息，让人知道你在跟谁说话。

        仅在需要明确指向时调用；不确定就调用 mode="none"。

        Args:
            mode(string): at=@对方, quote=引用对方消息, both=两者都要, none=都不要
            qq(string): 要 @ 的 QQ 号或群号。留空表示当前说话的人
        """
        mode = (mode or "").strip().lower()
        if mode not in VALID_MODES:
            return f"mode 只能是 {sorted(VALID_MODES)}，本次未设置"

        if mode == "none":
            event.set_extra("rt_mode", "")
            return "本次不加 @ 也不引用"

        event.set_extra("rt_mode", mode)
        event.set_extra("rt_qq", (qq or "").strip())
        return f"已设置：{mode}"

    def _reload_cfg_if_changed(self) -> None:
        try:
            mtime = CFG_PATH.stat().st_mtime
            if mtime == self._cfg_mtime:
                return
            cfg = dict(DEFAULTS)
            cfg.update(json.loads(CFG_PATH.read_text(encoding="utf-8")))
            self.cfg = cfg
            self._cfg_mtime = mtime
            logger.info("[reply_target] 配置已热重载")
        except Exception:
            pass

    # ---------- 给模型加一句提示 ----------
    @filter.on_llm_request()
    async def inject_hint(self, event: AstrMessageEvent, req) -> None:
        try:
            if event.get_platform_name() != "aiocqhttp":
                return
            hint = (
                "\n\n【发言指向】群聊里多人同时说话、你接的是某一条具体消息、"
                "或者对方问的是需要明确对象的问题时，先调用 reply_target 工具："
                "mode=\"quote\" 引用对方那条消息，mode=\"at\" @ 对方（qq 可留空表示当前说话的人），"
                "mode=\"both\" 两个都要。只是随手搭话、气氛闲聊就不用调用。"
            )
            if event.is_private_chat():
                hint = hint.replace("mode=\"at\" @ 对方（qq 可留空表示当前说话的人），", "")
            req.system_prompt = (req.system_prompt or "") + hint
        except Exception:
            pass

    # ---------- 实际往消息链里插 ----------
    @filter.on_decorating_result()
    async def decorate(self, event: AstrMessageEvent) -> None:
        try:
            self._reload_cfg_if_changed()
            if not self.cfg.get("enable", True):
                return
            if event.get_platform_name() != "aiocqhttp":
                return

            mode = str(event.get_extra("rt_mode", "") or "")
            if not mode:
                # 模型没显式调用工具 -> 按概率自动兜底
                if event.is_private_chat():
                    auto_on = bool(self.cfg.get("auto_quote_private", False))
                else:
                    auto_on = bool(self.cfg.get("auto_quote_group", True))
                try:
                    prob = float(self.cfg.get("auto_quote_probability", 0.4) or 0)
                except Exception:
                    prob = 0.4
                if auto_on and prob > 0 and random.random() < prob:
                    mode = "quote"
                else:
                    return
            if mode not in VALID_MODES or mode == "none":
                return

            result = event.get_result()
            if not result or not result.chain:
                return
            chain = result.chain
            # 已经有 @/引用 就别重复加
            if any(isinstance(c, (At, Reply)) for c in chain):
                return
            # 纯图片/表情包这类不加
            if not any(isinstance(c, Plain) and (c.text or "").strip() for c in chain):
                return

            is_private = event.is_private_chat()
            self_id = str(event.get_self_id() or "")
            sender = str(event.get_sender_id() or "")
            target = str(event.get_extra("rt_qq", "") or "") or sender
            if target == self_id:
                target = sender

            # 冷却，防止连续每条都 @
            cd = float(self.cfg.get("cooldown", 0) or 0)
            key = f"{event.get_platform_name()}:{event.get_group_id() or sender}"
            now = time.time()
            if cd > 0 and now - self._last.get(key, 0) < cd:
                return

            use_quote = mode in ("quote", "both")
            use_at = mode in ("at", "both")

            inserts = []
            if use_quote:
                mid = getattr(event.message_obj, "message_id", None)
                if mid:
                    inserts.append(Reply(id=str(mid)))

            if (
                use_at
                and target
                and not (is_private and not self.cfg.get("at_in_private", False))
            ):
                inserts.append(At(qq=target))

            if not inserts:
                return

            for comp in reversed(inserts):
                chain.insert(0, comp)

            # @ 完补个空格，看着自然点
            for i, comp in enumerate(chain):
                if isinstance(comp, At):
                    if i + 1 < len(chain) and isinstance(chain[i + 1], Plain):
                        t = chain[i + 1].text or ""
                        if not t.startswith(" "):
                            chain[i + 1].text = " " + t
                    break

            self._last[key] = now
        except Exception as e:  # 出错不影响正常回复
            logger.warning(f"[reply_target] 注入 @/引用失败：{e}")
