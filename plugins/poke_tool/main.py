import pathlib
# -*- coding: utf-8 -*-
"""
让机器人会「戳一戳」

两种触发：
1. 聊天中自己决定 —— 提供 poke_user 工具，模型觉得合适就戳
2. 闲下来主动戳 —— 后台定时问模型"现在要不要戳一下", 由它自己判断

配置：data/config/poke_tool_config.json
"""

import asyncio
import json
import random
import time
from datetime import datetime
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

_DATA_DIR = pathlib.Path(__file__).resolve().parents[2]  # .../AstrBot/data
CFG_PATH = _DATA_DIR / "config" / "poke_tool_config.json"
DEFAULTS = {
    "enable": True,
    "tool_enable": True,
    "auto_enable": False,
    "auto_sessions": [],
    "idle_minutes": 20,
    "interval_min_minutes": 30,
    "interval_max_minutes": 80,
    "quiet_hours": "1-8",
    "max_per_day": 8,
    "cooldown_sec": 90,
    "provider_id": "deepseek",
}


class PokeTool(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self.cfg = dict(DEFAULTS)
        self._cfg_mtime = 0
        self._load_cfg()
        self._seen: dict[str, float] = {}       # umo -> last poke ts
        self._day_count: dict[str, int] = {}     # umo -> today count
        self._day: dict[str, str] = {}
        self._last_msg: dict[str, dict] = {}
        self._next_due: dict[str, float] = {}
        try:
            asyncio.create_task(self._loop())
        except Exception as e:
            logger.warning(f"[poke_tool] 启动后台任务失败: {e}")
        logger.info(
            f"[poke_tool] 已加载：工具={self.cfg['tool_enable']} 主动={self.cfg['auto_enable']}"
        )

    # ------------------------------------------------------------ 配置/工具
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

    def _in_quiet_hours(self) -> bool:
        try:
            spec = str(self.cfg.get("quiet_hours") or "").strip()
            if not spec or "-" not in spec:
                return False
            a, b = spec.split("-", 1)
            start, end = int(a), int(b)
            h = datetime.now().hour
            if start <= end:
                return start <= h < end
            return h >= start or h < end
        except Exception:
            return False

    def _daily_ok(self, umo: str) -> bool:
        today = datetime.now().strftime("%Y-%m-%d")
        if self._day.get(umo) != today:
            self._day[umo] = today
            self._day_count[umo] = 0
        return self._day_count.get(umo, 0) < int(self.cfg.get("max_per_day", 8))

    async def _client(self):
        for inst in self.context.platform_manager.get_insts():
            try:
                c = inst.get_client()
            except Exception:
                c = getattr(inst, "bot", None)
            if c is not None:
                return c
        return None

    async def _poke(self, user_id: int, group_id: int | None = None) -> None:
        client = await self._client()
        if client is None:
            raise RuntimeError("没有可用的 OneBot 客户端")
        payload = {"user_id": int(user_id)}
        if group_id:
            payload["group_id"] = int(group_id)
        await client.call_action("send_poke", **payload)

    # ------------------------------------------------------------ 记录活跃度
    @filter.event_message_type(filter.EventMessageType.ALL, priority=6)
    async def track(self, event: AstrMessageEvent):
        try:
            umo = event.unified_msg_origin
            self._last_msg[umo] = {
                "ts": time.time(),
                "sender": str(event.get_sender_id() or ""),
                "name": event.get_sender_name() or "",
                "text": (event.message_str or "")[:120],
                "group": str(event.get_group_id() or ""),
            }
        except Exception:
            pass

    # ------------------------------------------------------------ 聊天里主动戳
    @filter.llm_tool(name="poke_user")
    async def poke_user(self, event: AstrMessageEvent, qq: str = "", reason: str = "") -> str:
        """戳一戳对方（QQ 的"拍一拍"）。觉得对方欠揍、想提醒对方、或者气氛合适时可以戳一下，
        不要每条消息都戳，偶尔用一次就好。

        Args:
            qq(string): 要戳的 QQ 号，留空表示当前说话的人
            reason(string): 为什么想戳，用一句话说明（不会发给用户）
        """
        self._load_cfg()
        if not self.cfg.get("enable", True) or not self.cfg.get("tool_enable", True):
            return "戳一戳功能未启用。"

        umo = event.unified_msg_origin
        now = time.time()
        if now - self._seen.get(umo, 0) < float(self.cfg.get("cooldown_sec", 90)):
            return "刚戳过，别再戳了，等会儿再说。"
        if not self._daily_ok(umo):
            return "今天戳太多次了，歇会儿。"

        target = (qq or "").strip() or str(event.get_sender_id() or "")
        if not target.isdigit():
            return "找不到要戳的对象。"
        gid = event.get_group_id()
        try:
            await self._poke(int(target), int(gid) if gid else None)
        except Exception as e:
            logger.warning(f"[poke_tool] 戳失败: {e}")
            return "没戳成，可能对方设置了不允许。"

        self._seen[umo] = now
        self._day_count[umo] = self._day_count.get(umo, 0) + 1
        logger.info(f"[poke_tool] 聊天中戳了 {target}（{reason[:40]}）")
        return f"已经戳了 {target}。回一句简短的话就行，别提“戳”这个动作太多次。"

    # ------------------------------------------------------------ 闲时主动戳
    async def _loop(self) -> None:
        await asyncio.sleep(20)
        while True:
            try:
                await asyncio.sleep(60)
                self._load_cfg()
                if not self.cfg.get("enable", True) or not self.cfg.get("auto_enable", False):
                    continue
                if self._in_quiet_hours():
                    continue

                sessions = [str(x) for x in (self.cfg.get("auto_sessions") or []) if str(x).strip()]
                now = time.time()
                for umo in sessions:
                    info = self._last_msg.get(umo)
                    if not info:
                        continue
                    idle = (now - info["ts"]) / 60
                    if idle < float(self.cfg.get("idle_minutes", 20)):
                        continue
                    if now < self._next_due.get(umo, 0):
                        continue
                    # 安排下一次检查
                    lo = float(self.cfg.get("interval_min_minutes", 30))
                    hi = float(self.cfg.get("interval_max_minutes", 80))
                    self._next_due[umo] = now + random.uniform(lo, hi) * 60
                    if not self._daily_ok(umo):
                        continue
                    await self._decide_and_poke(umo, info)
            except asyncio.CancelledError:
                return
            except Exception as e:
                logger.warning(f"[poke_tool] 后台循环异常: {e}")

    async def _decide_and_poke(self, umo: str, info: dict) -> None:
        try:
            provider = None
            pid = self.cfg.get("provider_id")
            if pid:
                provider = self.context.get_provider_by_id(pid)
            provider = provider or self.context.get_using_provider()
            if provider is None:
                return

            prompt = (
                "你在 QQ 上聊天。现在有点闲，考虑要不要戳一下对方（戳一戳/拍一拍）。\n"
                f"当前时间：{datetime.now().strftime('%H:%M')}\n"
                f"对方昵称：{info.get('name')}\n"
                f"对方最后说的话：{info.get('text')}\n"
                f"已经安静了 {int((time.time()-info['ts'])/60)} 分钟。\n"
                "只在“想提醒他/想逗他/气氛合适”时才戳，别每次都戳。\n"
                "只回答一个词：戳 或 不戳"
            )
            resp = await provider.text_chat(prompt=prompt)
            ans = (resp.completion_text or "").strip()
            if not ans.startswith("戳"):
                return
            target = info.get("sender")
            if not target:
                return
            gid = info.get("group") or None
            await self._poke(int(target), int(gid) if gid else None)
            self._seen[umo] = time.time()
            self._day_count[umo] = self._day_count.get(umo, 0) + 1
            logger.info(f"[poke_tool] 主动戳了 {target}（{umo}）")
        except Exception as e:
            logger.warning(f"[poke_tool] 主动戳失败: {e}")
