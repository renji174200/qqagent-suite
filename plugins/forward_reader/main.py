import pathlib
# -*- coding: utf-8 -*-
"""
展开 QQ 合并转发消息（含嵌套转发）

AstrBot 只会展开"引用消息"里的转发，普通转发的正文看不到。
这个插件把转发消息递归拉取出来，拼成文字写回消息，模型就能读到内容。
"""

import html
import json
import re
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

_DATA_DIR = pathlib.Path(__file__).resolve().parents[2]  # .../AstrBot/data
CFG_PATH = _DATA_DIR / "config" / "forward_reader_config.json"
DEFAULTS = {
    "enable": True,
    "max_depth": 3,
    "max_nodes": 60,
    "max_chars": 4000,
}


@register("forward_reader", "dsh", "展开QQ转发消息（含嵌套）", "0.1.0")
class ForwardReader(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self.cfg = dict(DEFAULTS)
        self._load_cfg()
        logger.info(f"[forward_reader] 已加载：深度={self.cfg['max_depth']}")

    def _load_cfg(self) -> None:
        try:
            self.cfg.update(json.loads(CFG_PATH.read_text(encoding="utf-8-sig")))
        except Exception:
            pass

    @staticmethod
    def _xml_text(raw: str) -> str:
        raw = str(raw or "")
        if not raw:
            return ""
        if "viewMultiMsg" in raw or "聊天记录" in raw:
            titles = re.findall(r"<title[^>]*>(.*?)</title>", raw, re.S)
            titles = [html.unescape(x).strip() for x in titles]
            titles = [x for x in titles if x and "聊天记录" not in x]
            if titles:
                return "[嵌套聊天记录] " + "；".join(titles[:20])
        # 公众号/链接卡片：取 title + digest + url
        parts = []
        for tag in ("title", "digest", "des"):
            m = re.search(rf"<{tag}[^>]*>(.*?)</{tag}>", raw, re.S)
            if m:
                v = html.unescape(re.sub(r"<[^>]+>", "", m.group(1))).strip()
                if v:
                    parts.append(v)
        m = re.search(r'url="([^"]+)"', raw)
        if m:
            parts.append(m.group(1))
        return ("[卡片] " + " | ".join(parts)) if parts else ""

    @staticmethod
    def _card_text(data) -> str:
        """把 QQ 小程序/结构化卡片压成一行可读文字"""
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except Exception:
                return ""
        if not isinstance(data, dict):
            return ""
        fields = {"title": "", "desc": "", "prompt": "", "url": "", "app": ""}
        def walk(o):
            if isinstance(o, dict):
                for k, v in o.items():
                    kl = str(k).lower()
                    if isinstance(v, str) and v.strip() and len(v) < 300:
                        if kl in ("title", "desc", "prompt") and not fields[kl if kl != "desc" else "desc"]:
                            fields[kl if kl != "desc" else "desc"] = v.strip()
                        elif kl in ("qqdocurl", "jumpurl", "url") and not fields["url"]:
                            fields["url"] = v.strip()
                        elif kl in ("app", "appname") and not fields["app"]:
                            fields["app"] = v.strip()
                    walk(v)
            elif isinstance(o, list):
                for v in o:
                    walk(v)
        walk(data)
        bits = []
        if fields["app"]:
            bits.append(f"来源={fields['app']}")
        if fields["title"]:
            bits.append(f"标题={fields['title']}")
        if fields["desc"] and fields["desc"] != fields["title"]:
            bits.append(f"描述={fields['desc']}")
        if fields["prompt"]:
            bits.append(f"提示={fields['prompt']}")
        if fields["url"]:
            bits.append(f"链接={fields['url']}")
        return ("[卡片] " + " | ".join(bits)) if bits else ""

    @staticmethod
    def _seg_text(seg) -> str:
        if isinstance(seg, str):
            return seg
        if not isinstance(seg, dict):
            return ""
        t = seg.get("type")
        d = seg.get("data") or {}
        if t == "text":
            return str(d.get("text") or "")
        if t == "at":
            qq = d.get("qq")
            return f"@{qq} " if qq else ""
        if t == "face":
            return "[表情]"
        if t == "image":
            return "[图片]"
        if t == "record":
            return "[语音]"
        if t == "video":
            return "[视频]"
        if t in ("forward", "node"):
            return "[嵌套转发]"
        if t == "xml":
            raw = str(d.get("data") or "")
            txt = ForwardReader._xml_text(raw)
            return txt if txt else "[卡片]"
        if t == "json":
            raw = str(d.get("data") or "")
            if "聊天记录" in raw:
                return "[嵌套聊天记录]"
            return "[卡片]"
        return ""

    def _render_nodes(self, nodes: list, depth: int, counter: list, lines: list) -> None:
        for node in nodes:
            if counter[0] >= int(self.cfg.get("max_nodes", 60)):
                return
            counter[0] += 1
            if not isinstance(node, dict):
                continue
            sender = node.get("sender") or {}
            name = (
                sender.get("card")
                or sender.get("nickname")
                or sender.get("name")
                or str(sender.get("user_id") or "")
                or "群友"
            )
            content = node.get("message") or node.get("content") or []
            if isinstance(content, str):
                text = content
            else:
                text = "".join(self._seg_text(s) for s in content)
            text = (text or "").strip()
            indent = "  " * depth
            if text:
                lines.append(f"{indent}{name}：{text}")

    async def _fetch(self, client, fid: str, depth: int, counter: list, lines: list) -> None:
        if depth > int(self.cfg.get("max_depth", 3)):
            return
        try:
            resp = await client.call_action("get_forward_msg", id=str(fid))
        except Exception as e:
            logger.warning(f"[forward_reader] 拉取转发失败 id={fid}: {e}")
            return
        if not isinstance(resp, dict):
            return
        if isinstance(resp.get("data"), dict):
            data = resp["data"]
        elif "messages" in resp or "message" in resp or "nodes" in resp:
            data = resp
        else:
            logger.warning(f"[forward_reader] 转发内容结构异常 id={fid} keys={list(resp)[:8]}")
            return
        nodes = data.get("messages") or data.get("message") or data.get("nodes") or []
        if not isinstance(nodes, list):
            return
        if nodes and isinstance(nodes[0], dict):
            logger.info(f"[forward_reader] 节点结构示例 keys={list(nodes[0])[:12]}")
        self._render_nodes(nodes, depth, counter, lines)
        # 递归嵌套
        for node in nodes:
            if not isinstance(node, dict):
                continue
            content = node.get("message") or node.get("content") or []
            if isinstance(content, list):
                for seg in content:
                    if isinstance(seg, dict) and seg.get("type") in ("forward", "node"):
                        child = (seg.get("data") or {}).get("id") or (seg.get("data") or {}).get("resid")
                        if child and counter[0] < int(self.cfg.get("max_nodes", 60)):
                            await self._fetch(client, str(child), depth + 1, counter, lines)

    @filter.event_message_type(filter.EventMessageType.ALL, priority=20)
    async def on_message(self, event: AstrMessageEvent):
        try:
            self._load_cfg()
            if not self.cfg.get("enable", True):
                return
            if event.get_platform_name() != "aiocqhttp":
                return
            # 引用了卡片/转发/etc 时，把被引用的内容也拉下来
            reply_id = None
            for comp in event.get_messages():
                if type(comp).__name__ == "Reply":
                    reply_id = getattr(comp, "id", None)
                    break
            if reply_id:
                client0 = getattr(event, "bot", None)
                if client0 is not None:
                    try:
                        resp = await client0.call_action("get_msg", message_id=str(reply_id))
                        data = resp.get("data") if isinstance(resp, dict) and isinstance(resp.get("data"), dict) else resp
                        segs = (data or {}).get("message") or []
                        parts = []
                        for seg in segs if isinstance(segs, list) else []:
                            if not isinstance(seg, dict):
                                continue
                            t = seg.get("type"); d = seg.get("data") or {}
                            if t == "text":
                                parts.append(str(d.get("text") or ""))
                            elif t == "json":
                                parts.append(self._card_text(d.get("data")))
                            elif t == "xml":
                                parts.append(self._xml_text(d.get("data")))
                            elif t == "image":
                                parts.append("[图片]")
                            elif t in ("forward", "node"):
                                parts.append("[转发消息]")
                        quoted = " ".join(p for p in parts if p and p.strip()).strip()
                        if quoted:
                            original = (event.message_str or "").strip()
                            merged = (original + "\n" if original else "") + "【引用内容】" + quoted[:600]
                            event.message_str = merged
                            try:
                                if event.message_obj is not None:
                                    event.message_obj.message_str = merged
                            except Exception:
                                pass
                            logger.info(f"[forward_reader] 引用内容已解析: {quoted[:120]}")
                    except Exception as e:
                        logger.warning(f"[forward_reader] 拉取引用消息失败: {e}")

            # 先处理卡片（小程序/链接卡片）
            cards = []
            for comp in event.get_messages():
                if type(comp).__name__ == "Json":
                    try:
                        text_card = self._card_text(getattr(comp, "data", None))
                    except Exception:
                        text_card = ""
                    if text_card:
                        cards.append(text_card)
            if cards:
                original = (event.message_str or "").strip()
                if original in ("", "[ComponentType.Json]", "[Json]"):
                    original = ""
                merged = (original + "\n" if original else "") + "\n".join(cards)
                event.message_str = merged
                try:
                    if event.message_obj is not None:
                        event.message_obj.message_str = merged
                except Exception:
                    pass
                logger.info(f"[forward_reader] 卡片已解析: {cards[0][:120]}")

            fwd_id = None
            for comp in event.get_messages():
                if type(comp).__name__ == "Forward":
                    fwd_id = getattr(comp, "id", None) or getattr(comp, "resid", None)
                    break
            if not fwd_id:
                return
            client = getattr(event, "bot", None)
            if client is None:
                logger.warning("[forward_reader] 没有 OneBot 客户端")
                return
            lines: list[str] = []
            counter = [0]
            await self._fetch(client, str(fwd_id), 0, counter, lines)
            if not lines:
                return
            body = "\n".join(lines)
            limit = int(self.cfg.get("max_chars", 4000))
            if len(body) > limit:
                body = body[:limit] + "\n……（转发内容过长，已截断）"
            original = (event.message_str or "").strip()
            merged = (original + "\n" if original else "") + "【转发消息内容】\n" + body
            event.message_str = merged
            try:
                if event.message_obj is not None:
                    event.message_obj.message_str = merged
            except Exception:
                pass
            logger.info(f"[forward_reader] 已展开转发 id={fwd_id} 节点={counter[0]} 字数={len(body)}")
        except Exception as e:
            logger.warning(f"[forward_reader] 处理失败: {type(e).__name__}: {e}")
