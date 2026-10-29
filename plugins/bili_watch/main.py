# -*- coding: utf-8 -*-
"""
B站视频"观看 + 记忆"插件

流程：
1. 识别 b23 短链 / BV 号 / QQ 小程序卡片
2. 拉标题、简介、字幕；下载低清视频并抽帧
3. 让多模态模型"看"画面，产出一份**内容笔记**（不是模板卡片）
4. 笔记写入会话记忆（带 TTL，落盘持久化）
5. 当前回合用它回一句自然的话；后续回合通过 on_llm_request 注入，
   所以她能接着跟你聊视频里的内容

配置：data/config/bili_watch_config.json
"""

import asyncio
import json
import os
import pathlib
import re
import tempfile
import time
from pathlib import Path

import aiohttp

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
from astrbot.core.message.message_event_result import MessageChain

_DATA_DIR = pathlib.Path(__file__).resolve().parents[2]  # .../AstrBot/data
CFG_PATH = _DATA_DIR / "config" / "bili_watch_config.json"
DATA_DIR = _DATA_DIR / "plugin_data" / "bili_watch"
NOTES_PATH = DATA_DIR / "notes.json"
DEFAULTS = {
    "enable": True,
    "provider_id": "deepseek",
    "sessdata": "",
    "max_duration_sec": 900,
    "frames": 8,
    "max_download_mb": 45,
    "require_at_in_group": True,
    "cooldown_sec": 60,
    "note_ttl_minutes": 120,
}

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
BV_RE = re.compile(r"(BV[0-9A-Za-z]{10})")
B23_RE = re.compile(r"https?://b23\.tv/[A-Za-z0-9]+")
HEADERS = {"User-Agent": UA, "Referer": "https://www.bilibili.com/"}


@register("bili_watch", "dsh", "看B站视频画面、记内容并与你讨论", "0.2.0")
class BiliWatch(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self.cfg = dict(DEFAULTS)
        try:
            self.cfg.update(json.loads(CFG_PATH.read_text(encoding="utf-8-sig")))
        except Exception:
            pass
        self._seen: dict[str, float] = {}
        self._buvid3 = ""
        self._notes: dict[str, list[dict]] = {}
        self._load_notes()
        logger.info(
            f"[bili_watch] 已加载 v0.2，帧数={self.cfg['frames']} "
            f"记忆={self.cfg['note_ttl_minutes']}分钟"
        )

    # ---------------------------------------------------------------- 记忆
    def _load_notes(self) -> None:
        try:
            raw = json.loads(NOTES_PATH.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                self._notes = raw
                logger.info(
                    f"[bili_watch] 恢复 {sum(len(v) for v in raw.values())} 条视频记忆"
                )
        except Exception:
            self._notes = {}

    def _save_notes(self) -> None:
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            notes = self._prune(self._notes)
            NOTES_PATH.write_text(json.dumps(notes, ensure_ascii=False), encoding="utf-8")
        except Exception as e:
            logger.warning(f"[bili_watch] 记忆保存失败: {e}")

    def _prune(self, notes: dict[str, list[dict]]) -> dict[str, list[dict]]:
        ttl = float(self.cfg.get("note_ttl_minutes", 120)) * 60
        now = time.time()
        out = {}
        for umo, items in notes.items():
            keep = [x for x in items if now - float(x.get("ts", 0)) < ttl]
            if keep:
                out[umo] = keep[-3:]
        return out

    def _remember(self, umo: str, title: str, bvid: str, note: str) -> None:
        self._notes.setdefault(umo, []).append(
            {"ts": time.time(), "title": title, "bvid": bvid, "note": note}
        )
        self._notes[umo] = self._notes[umo][-3:]
        self._save_notes()

    # ---------------------------------------------------------------- B站接口
    async def _buvid(self, session):
        if self._buvid3:
            return self._buvid3
        try:
            async with session.get("https://www.bilibili.com/", headers=HEADERS) as r:
                c = r.cookies.get("buvid3")
                if c:
                    self._buvid3 = c.value
        except Exception:
            pass
        return self._buvid3

    def _cookie(self):
        parts = []
        if self.cfg.get("sessdata"):
            parts.append(f"SESSDATA={self.cfg['sessdata']}")
        if self._buvid3:
            parts.append(f"buvid3={self._buvid3}")
        return "; ".join(parts)

    async def _resolve_b23(self, session, url: str) -> str:
        try:
            async with session.get(url, headers=HEADERS, allow_redirects=False) as r:
                return r.headers.get("Location", "")
        except Exception:
            return ""

    async def _view(self, session, bvid: str) -> dict:
        headers = dict(HEADERS)
        headers["Cookie"] = self._cookie()
        url = f"https://api.bilibili.com/x/web-interface/view?bvid={bvid}"
        async with session.get(url, headers=headers) as r:
            data = await r.json(content_type=None)
        if data.get("code") != 0:
            raise RuntimeError(f"B站接口错误 {data.get('code')}: {data.get('message')}")
        return data["data"]

    async def _subtitle(self, session, bvid: str, cid: int) -> str:
        try:
            headers = dict(HEADERS)
            headers["Cookie"] = self._cookie()
            url = f"https://api.bilibili.com/x/player/wbi/v2?bvid={bvid}&cid={cid}"
            async with session.get(url, headers=headers) as r:
                data = await r.json(content_type=None)
            subs = ((data.get("data") or {}).get("subtitle") or {}).get("subtitles") or []
            if not subs:
                return ""
            sub_url = subs[0].get("subtitle_url", "")
            if sub_url.startswith("//"):
                sub_url = "https:" + sub_url
            if not sub_url:
                return ""
            async with session.get(sub_url, headers=HEADERS) as r:
                j = await r.json(content_type=None)
            body = j.get("body") or []
            return " ".join(str(x.get("content", "")) for x in body)[:8000]
        except Exception:
            return ""

    async def _download(self, session, bvid: str, cid: int) -> str:
        headers = dict(HEADERS)
        headers["Cookie"] = self._cookie()
        url = (
            "https://api.bilibili.com/x/player/wbi/playurl"
            f"?bvid={bvid}&cid={cid}&qn=16&fnval=1"
        )
        async with session.get(url, headers=headers) as r:
            data = await r.json(content_type=None)
        durl = ((data.get("data") or {}).get("durl") or [])
        if not durl:
            raise RuntimeError("没有拿到视频流地址")
        media_url = durl[0]["url"]
        limit = int(self.cfg["max_download_mb"]) * 1024 * 1024
        tmp = tempfile.NamedTemporaryFile(suffix=".mp4", delete=False)
        tmp.close()
        got = 0
        async with session.get(media_url, headers=HEADERS) as r:
            with open(tmp.name, "wb") as f:
                async for chunk in r.content.iter_chunked(256 * 1024):
                    f.write(chunk)
                    got += len(chunk)
                    if got >= limit:
                        break
        return tmp.name

    async def _frames(self, video_path: str, count: int) -> list[str]:
        out_dir = tempfile.mkdtemp(prefix="bili_frames_")
        probe = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=nw=1:nk=1", video_path,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        out, _ = await probe.communicate()
        try:
            duration = float((out or b"0").decode().strip() or 0)
        except Exception:
            duration = 0
        if duration <= 1:
            duration = 60
        paths = []
        for i in range(count):
            t = duration * (i + 0.5) / count
            p = os.path.join(out_dir, f"f{i}.jpg")
            proc = await asyncio.create_subprocess_exec(
                "ffmpeg", "-v", "error", "-ss", f"{t:.2f}", "-i", video_path,
                "-frames:v", "1", "-vf", f"scale={int(self.cfg.get('frame_width', 960))}:-2", "-y", p,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            await proc.communicate()
            if os.path.exists(p) and os.path.getsize(p) > 1000:
                paths.append(p)
        return paths

    def _provider(self):
        pid = self.cfg.get("provider_id")
        provider = self.context.get_provider_by_id(pid) if pid else None
        return provider or self.context.get_using_provider()

    # ---------------------------------------------------------------- 主逻辑
    @filter.event_message_type(filter.EventMessageType.ALL, priority=5)
    async def on_message(self, event: AstrMessageEvent):
        frames: list[str] = []
        try:
            if not self.cfg.get("enable", True):
                return
            if event.get_platform_name() != "aiocqhttp":
                return
            if not event.is_private_chat() and self.cfg.get("require_at_in_group", True):
                if not event.is_at_or_wake_command:
                    return

            raw = self._raw_text(event)
            if not raw:
                return
            m_bv = BV_RE.search(raw)
            m_b23 = B23_RE.search(raw)
            if not (m_bv or m_b23):
                return

            key = (m_bv.group(1) if m_bv else m_b23.group(0))
            now = time.time()
            if now - self._seen.get(key, 0) < float(self.cfg.get("cooldown_sec", 60)):
                return
            self._seen[key] = now

            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=120), trust_env=True
            ) as session:
                await self._buvid(session)
                bvid = m_bv.group(1) if m_bv else ""
                if not bvid and m_b23:
                    loc = await self._resolve_b23(session, m_b23.group(0))
                    mb = BV_RE.search(loc)
                    if not mb:
                        return
                    bvid = mb.group(1)

                info = await self._view(session, bvid)
                duration = int(info.get("duration") or 0)
                note_prefix = ""
                if duration > int(self.cfg["max_duration_sec"]):
                    note_prefix = f"（视频较长，共 {duration // 60} 分钟，你只看了开头部分）"

                subs = await self._subtitle(session, bvid, int(info["cid"]))
                video_path = await self._download(session, bvid, int(info["cid"]))

            frames = await self._frames(video_path, int(self.cfg["frames"]))
            try:
                os.unlink(video_path)
            except Exception:
                pass

            provider = self._provider()
            if provider is None:
                logger.warning("[bili_watch] 没有可用的多模态模型")
                return

            # 1) 产出内容笔记（真正"看"画面）
            note_prompt = (
                f"{note_prefix}这是B站视频《{info.get('title')}》"
                f"（UP主：{(info.get('owner') or {}).get('name')}，"
                f"时长 {duration // 60}分{duration % 60}秒）的画面截图，按时间顺序排列。\n"
                f"简介：{str(info.get('desc') or '')[:400]}\n"
            )
            if subs:
                note_prompt += f"字幕节选：{subs[:3000]}\n"
            note_prompt += (
                "\n请仔细看这些画面，写一份中文内容笔记，要求：\n"
                "1. 视频主题是什么，在讲什么\n"
                "2. 画面里出现的具体文字、数据、图表、界面内容（尽量照抄关键信息）\n"
                "3. 按时间顺序列出主要段落说了什么\n"
                "4. 结论/观点/数据结果\n"
                "只写事实，不要评价，不超过 500 字。"
            )
            resp = await provider.text_chat(prompt=note_prompt, image_urls=frames)
            note = (resp.completion_text or "").strip()
            if not note:
                note = "（模型没看清画面内容）"
            self._remember(event.unified_msg_origin, str(info.get("title") or ""), bvid, note)
            logger.info(f"[bili_watch] 已看 {bvid}，帧数={len(frames)}，笔记 {len(note)} 字")

            # 不直接回复：笔记已注入本轮 LLM 请求，由主模型自然接话
            # （避免插件和主模型各回一条）

        except Exception as e:
            logger.warning(f"[bili_watch] 处理失败: {type(e).__name__}: {e}")
        finally:
            for p in frames:
                try:
                    os.unlink(p)
                except Exception:
                    pass

    # ---------------------------------------------------------------- 注入记忆
    @filter.on_llm_request()
    async def inject_notes(self, event: AstrMessageEvent, req) -> None:
        try:
            umo = event.unified_msg_origin
            items = self._prune(self._notes).get(umo) or []
            if not items:
                return
            block = "\n\n【你最近看过的B站视频（你亲眼看过画面，下面是你的笔记）】\n"
            for it in items[-2:]:
                block += f"- 《{it.get('title')}》：{it.get('note')}\n"
            latest = items[-1]
            if time.time() - float(latest.get("ts", 0)) < 300:
                block += (
                    "对方刚发来最新的这个视频，你已经看完了，用你平时的语气随口回应一两句，"
                    "可以提画面里的一个具体细节，不要列点、不要复述标题。\n"
                )
            block += "对方如果问到视频里的内容，直接根据笔记回答，不要说“我没看过”。"
            req.system_prompt = (req.system_prompt or "") + block
        except Exception as e:
            logger.warning(f"[bili_watch] 注入记忆失败: {e}")

    @staticmethod
    def _raw_text(event: AstrMessageEvent) -> str:
        parts = []
        try:
            for comp in event.get_messages():
                for attr in ("text", "data", "raw"):
                    v = getattr(comp, attr, None)
                    if isinstance(v, str) and v:
                        parts.append(v)
                    elif isinstance(v, dict):
                        parts.append(json.dumps(v, ensure_ascii=False))
        except Exception:
            pass
        raw = " ".join(parts)
        if not raw:
            raw = event.message_str or ""
        return raw
