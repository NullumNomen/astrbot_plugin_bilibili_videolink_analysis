"""AstrBot entry point for Bilibili link previews and search."""

import asyncio
import re
from time import monotonic

import aiohttp
from astrbot.api import AstrBotConfig, logger
from astrbot.api import message_components as Comp
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star
from astrbot.core.star.filter.command import GreedyStr
from astrbot.core.utils.session_waiter import SessionController, SessionFilter, session_waiter

from .bili_client import BiliClient
from .bilibili import Cooldown, VideoTarget, card_targets, parse_targets, video_template_values

COMMAND = "B站点播"
TARGETS_EXTRA = "bilibili_videolink_analysis.targets"
SELECTION_EXTRA = "bilibili_videolink_analysis.selection"
TEMPLATE_FIELD = re.compile(r"\$\{([^{}]+)\}")
PART_OPTION = re.compile(r"(?:^|\s)-p\s+(\S+)\s*$")
NETWORK_ERRORS = (aiohttp.ClientError, TimeoutError, ValueError)


class BilibiliMessageFilter(filter.CustomFilter):
    def filter(self, event: AstrMessageEvent, cfg: AstrBotConfig) -> bool:
        if event.get_sender_id() == event.get_self_id():
            return False
        text = event.get_message_str()
        if text.strip().split(maxsplit=1)[:1] == [COMMAND]:
            return False
        targets = dict.fromkeys((target, "link") for target in parse_targets(text))
        for segment in event.get_messages():
            if isinstance(segment, Comp.Json):
                targets.update(dict.fromkeys((target, "card") for target in card_targets(segment.data)))
        if targets:
            event.set_extra(TARGETS_EXTRA, tuple(targets))
        return bool(targets)


class SelectionFilter(SessionFilter):
    @staticmethod
    def key(event: AstrMessageEvent) -> str:
        return f"bilibili_videolink_analysis:{event.unified_msg_origin}:{event.get_sender_id()}"

    def filter(self, event: AstrMessageEvent) -> str:
        text = event.get_message_str().strip()
        if event.get_extra(SELECTION_EXTRA) or text == "取消" or re.fullmatch(r"[0-9]{1,8}", text):
            return self.key(event)
        return ""


class BilibiliLinkAnalysis(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.cooldown = Cooldown(max(0, config["cooldown_seconds"]))
        self.in_flight = set()
        self.requests = asyncio.Semaphore(4)
        self.selection_filter = SelectionFilter()
        self.pending_selections = {}
        self.http = None

    async def initialize(self):
        self.http = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=8),
            cookie_jar=aiohttp.DummyCookieJar(),
        )
        self.bili = BiliClient(self.http)

    async def terminate(self):
        pending = list(self.pending_selections.values())
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
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
            await self._preview(event, target, source, automatic=True)

    @filter.command(COMMAND)
    async def search(self, event: AstrMessageEvent, keyword: GreedyStr):
        """搜索 B 站视频，根据回复的序号展示图文和播放链接。"""
        if not self.config["enable_search"]:
            return
        event.stop_event()
        keyword = keyword.strip()
        page = 1
        if option := PART_OPTION.search(keyword):
            if not re.fullmatch(r"[1-9][0-9]{0,5}", option[1]):
                await event.send(event.plain_result("请在 -p 后填写有效的分 P 序号。"))
                return
            page = int(option[1])
            keyword = keyword[:option.start()].strip()
        if not keyword or len(keyword) > 100:
            await event.send(event.plain_result("用法：/B站点播 关键词 [-p 分P]，关键词最多 100 字。"))
            return

        key = self.selection_filter.key(event)
        if key in self.pending_selections:
            await event.send(event.plain_result("你已有一组候选，请回复序号或发送“取消”。"))
            return
        task = asyncio.create_task(self._prompt_selection(event, keyword, page))
        self.pending_selections[key] = task
        try:
            await task
        finally:
            self.pending_selections.pop(key, None)

    async def _prompt_selection(self, event: AstrMessageEvent, keyword: str, page: int):
        try:
            async with self.requests:
                candidates = await asyncio.wait_for(self.bili.search(keyword), timeout=18)
        except NETWORK_ERRORS as error:
            logger.warning(f"B 站点播搜索失败：{error}")
            await event.send(event.plain_result("B 站搜索暂不可用，请稍后再试。"))
            return
        if not candidates:
            await event.send(event.plain_result("没有找到相关视频。"))
            return

        timeout = max(5, min(300, self.config["search_timeout_seconds"]))

        @session_waiter(timeout=timeout, record_history_chains=False)
        async def select(controller: SessionController, reply: AstrMessageEvent):
            choice = reply.get_message_str().strip()
            if choice == "取消":
                controller.stop()
                await reply.send(reply.plain_result("已取消点播。"))
                return
            if not re.fullmatch(r"[1-9][0-9]?", choice) or int(choice) > len(candidates):
                await reply.send(reply.plain_result(f"请输入 1 到 {len(candidates)}，或发送“取消”。"))
                return
            controller.stop()
            target = VideoTarget(candidates[int(choice) - 1][0], page)
            await self._preview(reply, target, "link", automatic=False)

        rows = [f"{index}. {title}" + (f" — {author}" if author else "")
                for index, (_, title, author) in enumerate(candidates, 1)]
        rows.append(f"请在 {timeout} 秒内回复序号，或发送“取消”。")
        event.set_extra(SELECTION_EXTRA, True)
        waiter = asyncio.create_task(select(event, session_filter=self.selection_filter))
        try:
            await event.send(event.plain_result("\n".join(rows)))
            await waiter
        except TimeoutError:
            await event.send(event.plain_result("点播选择已超时。"))
        finally:
            if not waiter.done():
                waiter.cancel()
            await asyncio.gather(waiter, return_exceptions=True)
            event.set_extra(SELECTION_EXTRA, False)

    async def _preview(self, event: AstrMessageEvent, target: VideoTarget, source: str, automatic: bool):
        session = event.unified_msg_origin
        source_key = (session, target)
        if automatic and (
            source_key in self.in_flight or not self.cooldown.reserve(session, target, monotonic())
        ):
            return
        reservations = {target} if automatic else set()
        active = {source_key} if automatic else set()
        self.in_flight.update(active)
        completed = False
        try:
            async with self.requests:
                resolved = await asyncio.wait_for(self.bili.resolve(target), timeout=12)
                if automatic and resolved != target:
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
            if automatic:
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
            if not automatic or self.config["show_error"]:
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
            len(chains) > 1 and self.config["merge_forward"]
            and event.get_platform_name() == "aiocqhttp" and event.get_group_id()
        ):
            nodes = [Comp.Node(uin=event.get_self_id(), name="B 站视频解析", content=chain)
                     for chain in chains]
            try:
                await event.send(MessageChain([Comp.Nodes(nodes)]))
                return
            except Exception:
                logger.warning("B 站解析合并转发失败，改为逐条发送。")
        for chain in chains:
            await event.send(MessageChain(chain))
