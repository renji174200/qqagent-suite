# -*- coding: utf-8 -*-
"""
MiMo V2.5 TTS 唱歌插件

MiMo TTS 原生支持 `<style>唱歌</style>` 标签，会把歌词真的唱出来（而不是念）。
接口：OpenAI 兼容 /v1/chat/completions，返回 base64 音频。

用法：
- /唱歌 我听见雨滴落在青青草地，我听见远方下课钟声响起
- 或者对它说"唱首歌"，模型调用 sing_song 工具
"""

import base64
import os
import subprocess
import tempfile
import uuid

import httpx

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Record
from astrbot.api.star import Context, Star, register

import pathlib
_DATA_DIR = pathlib.Path(__file__).resolve().parents[2]  # .../AstrBot/data
API_BASE = "https://api.xiaomimimo.com/v1"
MODEL = "mimo-v2.5-tts"
# 唱歌音色：mimo_default 是通用；如果有专门的歌声音色可以换
VOICE = "mimo_default"
STYLE = "唱歌"
USER_PROMPT = "Hello, MiMo, have you had lunch?"
TIMEOUT = 120


@register("mimo_sing", "dsh", "用 MiMo TTS 的唱歌风格演唱歌词", "0.1.0")
class MiMoSing(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self.api_key = ""
        cfg_path = str(_DATA_DIR / "config" / "mimo_sing_config.json")
        try:
            import json

            self.cfg = json.loads(open(cfg_path, encoding="utf-8-sig").read())
            self.api_key = self.cfg.get("api_key", "")
        except Exception:
            self.cfg = {}
        logger.info(
            f"[mimo_sing] 已加载，key={'已配置' if self.api_key else '缺失'}，音色={self.cfg.get('voice', VOICE)}"
        )

    async def _sing(self, lyrics: str) -> str:
        if not self.api_key:
            raise RuntimeError("MiMo API Key 未配置")
        text = (lyrics or "").strip()
        if not text:
            raise ValueError("歌词为空")
        # 太长会被截断，限制一下
        text = text[:400]
        # 按逗号/句号断句，给模型换气的位置，唱起来不容易糊成一团
        import re as _re

        text = _re.sub(r"[，,；;]", "，\n", text)

        payload = {
            "model": self.cfg.get("model", MODEL),
            "messages": [
                {"role": "user", "content": self.cfg.get("user_prompt", USER_PROMPT)},
                {"role": "assistant", "content": f"({self.cfg.get('style', STYLE)}){text}"},
            ],
            "audio": {
                "format": "wav",
                "voice": self.cfg.get("voice", VOICE),
            },
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            r = await client.post(f"{API_BASE}/chat/completions", json=payload, headers=headers)
            if r.status_code >= 400:
                raise RuntimeError(f"MiMo HTTP {r.status_code}: {r.text[:200]}")
            data = r.json()

        msg = ((data.get("choices") or [{}])[0].get("message") or {})
        b64 = (msg.get("audio") or {}).get("data") or ""
        if not b64:
            raise RuntimeError("MiMo 没有返回音频")

        raw = base64.b64decode(b64)
        tmpdir = tempfile.mkdtemp(prefix="mimo_sing_")
        src = os.path.join(tmpdir, "raw.wav")
        with open(src, "wb") as f:
            f.write(raw)

        out = os.path.join(tmpdir, f"sing_{uuid.uuid4().hex[:8]}.wav")
        subprocess.run(
            ["ffmpeg", "-v", "error", "-i", src, "-ar", "24000", "-ac", "1", "-y", out],
            capture_output=True,
        )
        return out if os.path.exists(out) else src

    @filter.command("唱歌")
    async def sing_cmd(self, event: AstrMessageEvent, lyrics: str = ""):
        """用法：/唱歌 歌词内容（用逗号或换行分隔）"""
        text = (lyrics or "").strip()
        if not text:
            yield event.plain_result("歌词呢？比如：\n/唱歌 我听见雨滴落在青青草地，我听见远方下课钟声响起")
            return
        yield event.plain_result("拿着麦呢，等我一下…")
        try:
            wav = await self._sing(text)
            yield event.chain_result([Record(file=wav)])
        except Exception as e:
            logger.warning(f"[mimo_sing] 合成失败: {e}")
            yield event.plain_result("没唱出来，稍后再试试")

    @filter.llm_tool(name="sing_song")
    async def sing_song(self, event: AstrMessageEvent, lyrics: str) -> str:
        """用 MiMo TTS 的唱歌风格把歌词唱出来，结果以语音消息发送。

        Args:
            lyrics(string): 要唱的歌词，一般 4~8 句，用逗号或换行分隔。用户没给时用你记得的副歌歌词。
        """
        try:
            wav = await self._sing(lyrics)
        except Exception as e:
            logger.warning(f"[mimo_sing] 工具合成失败: {e}")
            return "唱歌失败了，随便回用户一句，让他稍后再试。"
        await event.send(event.chain_result([Record(file=wav)]))
        return "已经唱完并发成语音了，简短回一句就行。"
