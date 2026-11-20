"""AstrBot entry point for Bilibili link previews."""

import asyncio
import json
import re
from pathlib import Path
from time import monotonic

import aiohttp
from astrbot.api import AstrBotConfig, logger
from astrbot.api import message_components as Comp
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star

from .bili_client import BiliClient
from .bilibili import Cooldown, VideoTarget, card_targets, parse_targets, video_template_values

TARGETS_EXTRA = "bilibili_videolink_analysis.targets"
TEMPLATE_FIELD = re.compile(r"\$\{([^{}]+)\}")
NETWORK_ERRORS = (aiohttp.ClientError, TimeoutError, ValueError)
PREVIOUS_DEFAULT_TEMPLATE = (
    "${标题}\n${分P}\nUP 主：${UP主}\n播放 ${观看} · 点赞 ${点赞} · 弹幕 ${弹幕}\n"
    "投币 ${投币} · 收藏 ${收藏} · 转发 ${转发}\n${简介}\n${视频地址}\n${封面}"
)


class BilibiliMessageFilter(filter.CustomFilter):
    def filter(self, event: AstrMessageEvent, cfg: AstrBotConfig) -> bool:
        if event.get_sender_id() == event.get_self_id():
            return False
        text = event.get_message_str()
        targets = dict.fromkeys((target, "link") for target in parse_targets(text))
        for segment in event.get_messages():
            if isinstance(segment, Comp.Json):
                targets.update(dict.fromkeys((target, "card") for target in card_targets(segment.data)))
        if targets:
            event.set_extra(TARGETS_EXTRA, tuple(targets))
        return bool(targets)


class BilibiliLinkAnalysis(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.cooldown = Cooldown(max(0, config["cooldown_seconds"]))
        self.in_flight = set()
        self.requests = asyncio.Semaphore(4)
        self.http = None

    async def initialize(self):
        if self.config["link_template"] == PREVIOUS_DEFAULT_TEMPLATE:
            schema_path = Path(__file__).with_name("_conf_schema.json")
            schema = json.loads(schema_path.read_text(encoding="utf-8"))
            self.config["link_template"] = schema["link_template"]["default"]
            self.config.save_config()
        self.http = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=8),
            cookie_jar=aiohttp.DummyCookieJar(),
        )
        self.bili = BiliClient(self.http)

    async def terminate(self):
        if self.http:
            await self.http.close()

    @filter.custom_filter(BilibiliMessageFilter)
    async def parse_message(self, event: AstrMessageEvent):
        """自动预览 B 站视频链接、BV/AV 号和 QQ 分享卡片。"""
        if not self.config["enable_auto_parse"]:
            return
        candidates = event.get_extra(TARGETS_EXTRA, ())
        targets = {}
        links_only = None
        if not self.config["parse_standalone_ids"]:
            links_only = set(parse_targets(event.get_message_str(), standalone=False))
        for target, source in candidates:
            if source == "card" and not self.config["parse_qq_cards"]:
                continue
            if source == "link" and links_only is not None and target not in links_only:
                continue
            targets[target] = source
        limit = max(1, min(10, self.config["parse_limit"]))
        for target, source in list(targets.items())[:limit]:
            await self._preview(event, target, source)

    async def _preview(self, event: AstrMessageEvent, target: VideoTarget, source: str):
        session = event.unified_msg_origin
        source_key = (session, target)
        if source_key in self.in_flight or not self.cooldown.reserve(session, target, monotonic()):
            return
        reservations = {target}
        active = {source_key}
        self.in_flight.update(active)
        completed = False
        try:
            async with self.requests:
                resolved = await asyncio.wait_for(self.bili.resolve(target), timeout=12)
                if resolved != target:
                    resolved_key = (session, resolved)
                    if resolved_key in self.in_flight or not self.cooldown.reserve(
                        session, resolved, monotonic()
                    ):
                        completed = True
                        return
                    self.in_flight.add(resolved_key)
                    active.add(resolved_key)
                    reservations.add(resolved)
                data = await asyncio.wait_for(self.bili.details(resolved), timeout=18)

            values = video_template_values(
                data, resolved.page, self.config["id_preference"],
                max(0, min(1000, self.config["description_limit"])),
            )
            canonical = VideoTarget(values["BV号"], resolved.page)
            if canonical not in reservations:
                if (session, canonical) in self.in_flight or not self.cooldown.reserve(
                    session, canonical, monotonic()
                ):
                    completed = True
                    return
                reservations.add(canonical)
                active.add((session, canonical))
                self.in_flight.add((session, canonical))
            template = self.config["link_template"]
            if source == "card":
                template = self.config["card_template"] or template
            await self._send_preview(event, template, values)
            completed = True
        except NETWORK_ERRORS as error:
            logger.warning(f"B 站视频解析失败：{error}")
            if self.config["show_error"]:
                message = str(error) if isinstance(error, ValueError) else "视频解析失败，请稍后再试。"
                await event.send(event.plain_result(message))
        finally:
            self.in_flight.difference_update(active)
            if not completed:
                for reserved in reservations:
                    self.cooldown.release(session, reserved)

    async def _send_preview(self, event: AstrMessageEvent, template: str, values: dict[str, str]):
        chains = []
        for part in template.split("${~~~}"):
            chain = []
            for index, text in enumerate(part.split("${封面}")):
                if index and values["封面"]:
                    chain.append(Comp.Image.fromURL(values["封面"]))
                text = TEMPLATE_FIELD.sub(
                    lambda field: "" if field[1] == "视频" else values.get(field[1], field[0]),
                    text,
                )
                text = re.sub(r"\n[ \t]*\n+", "\n", text)
                if text.strip():
                    chain.append(Comp.Plain(text))
            if chain:
                chains.append(chain)

        if (
            chains and self.config["merge_forward"]
            and event.get_platform_name() == "aiocqhttp" and event.get_group_id()
        ):
            try:
                nodes = [
                    Comp.Node(uin=event.get_self_id(), name="B 站视频解析", content=chain)
                    for chain in chains
                ]
                await event.send(MessageChain([Comp.Nodes(nodes)]))
                return
            except Exception:
                logger.warning("B 站解析合并转发失败，改为逐条发送。")
        for chain in chains:
            await event.send(MessageChain(chain))
