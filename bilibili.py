"""Parse Bilibili video references and build display data."""

import heapq
import json
import re
from dataclasses import dataclass
from html import unescape
from urllib.parse import parse_qs, urlsplit

VIDEO_HOSTS = {"bilibili.com", "www.bilibili.com", "m.bilibili.com"}
SHORT_HOSTS = {"b23.tv", "bili22.cn", "bili23.cn", "bili33.cn", "bili2233.cn"}
ID_PATTERN = re.compile(r"(?i:BV)[0-9A-Za-z]{10}|(?i:av)[0-9]{1,20}")
SOURCE_PATTERN = re.compile(
    r"https?://[^\s<>\"'，。；！？、）】]+"
    r"|(?<![\w./@-])(?:(?:www\.|m\.)?bilibili\.com/video/"
    r"|b23\.tv/|bili(?:22|23|33|2233)\.cn/)[^\s<>\"'，。；！？、）】]+"
    r"|(?<![\w./@-])(?:(?i:BV)[0-9A-Za-z]{10}|(?i:av)[0-9]{1,20})(?![\w./@-])",
    re.IGNORECASE,
)
CARD_URL_FIELDS = {"qqdocurl", "jumpurl", "jump_url", "url", "targeturl", "target_url"}


@dataclass(frozen=True)
class VideoTarget:
    reference: str
    page: int = 1

    @property
    def is_short(self) -> bool:
        return self.reference.startswith("https://")


def normalize_id(value: str) -> str | None:
    if not ID_PATTERN.fullmatch(value):
        return None
    if value[:2].lower() == "bv":
        return "BV" + value[2:]
    aid = int(value[2:])
    return f"av{aid}" if aid else None


def parse_url(value: str, default_page: int = 1) -> VideoTarget | None:
    if "://" not in value:
        value = "https://" + value
    try:
        parts = urlsplit(value)
        host = parts.hostname
        if (
            parts.scheme not in {"https", "http"}
            or parts.username is not None
            or parts.port is not None
        ):
            return None
    except ValueError:
        return None

    page = parse_qs(parts.query).get("p", [""])[0]
    number = int(page) if re.fullmatch(r"[0-9]{1,6}", page) else 0
    if host in VIDEO_HOSTS:
        match = re.fullmatch(r"/video/([^/]+)/?", parts.path)
        video_id = normalize_id(match[1]) if match else None
        if video_id:
            return VideoTarget(video_id, number or default_page)
    if host in SHORT_HOSTS and re.fullmatch(r"/[A-Za-z0-9]+/?", parts.path):
        code = parts.path.strip("/")
        video_id = normalize_id(code)
        return VideoTarget(video_id or f"https://{host}/{code}", number or default_page)
    return None


def parse_targets(text: str, standalone: bool = True) -> list[VideoTarget]:
    matches = []
    for match in SOURCE_PATTERN.finditer(unescape(text).replace(r"\/", "/")):
        candidate = match[0].rstrip(".,;:!?)]}")
        video_id = normalize_id(candidate)
        if video_id:
            if standalone:
                matches.append((VideoTarget(video_id), True))
        elif target := parse_url(candidate):
            matches.append((target, False))
    linked_ids = {target.reference for target, bare in matches if not bare}
    return list(dict.fromkeys(
        target for target, bare in matches if not bare or target.reference not in linked_ids
    ))


def card_targets(card: object) -> list[VideoTarget]:
    if isinstance(card, str):
        try:
            card = json.loads(card)
        except (ValueError, RecursionError):
            return []
    pending = [card]
    targets = []
    while pending:
        value = pending.pop()
        if isinstance(value, dict):
            for key, item in value.items():
                if isinstance(item, str) and key.lower() in CARD_URL_FIELDS:
                    targets.extend(parse_targets(item, standalone=False))
                elif isinstance(item, (dict, list)):
                    pending.append(item)
        elif isinstance(value, list):
            pending.extend(value)
    return list(dict.fromkeys(targets))


class Cooldown:
    def __init__(self, seconds: int):
        self.seconds = seconds
        self.entries = {}
        self.expirations = []
        self.sequence = 0

    def reserve(self, session: str, target: VideoTarget, now: float) -> bool:
        if self.seconds == 0:
            return True
        while self.expirations and self.expirations[0][0] <= now:
            entry = heapq.heappop(self.expirations)
            key = entry[2]
            if self.entries.get(key) == entry:
                del self.entries[key]
        key = (session, target)
        if key in self.entries:
            return False
        self.sequence += 1
        entry = (now + self.seconds, self.sequence, key)
        self.entries[key] = entry
        heapq.heappush(self.expirations, entry)
        return True

    def release(self, session: str, target: VideoTarget) -> None:
        self.entries.pop((session, target), None)


def _number(value: object) -> str:
    if not isinstance(value, int) or value < 0:
        return "—"
    for factor, unit in ((100_000_000, "亿"), (10_000, "万")):
        if value >= factor:
            return f"{value / factor:.1f}".removesuffix(".0") + unit
    return str(value)


def _cover_url(value: str) -> str:
    if value.startswith("//"):
        value = "https:" + value
    try:
        parts = urlsplit(value)
        host = parts.hostname or ""
        if (
            parts.scheme in {"http", "https"}
            and parts.username is None
            and parts.port is None
            and (host == "hdslb.com" or host.endswith(".hdslb.com"))
        ):
            return parts._replace(scheme="https").geturl()
    except ValueError:
        pass
    return ""


def video_template_values(
    data: dict, page: int, id_preference: str = "bv", description_limit: int = 80
) -> dict[str, str]:
    bvid = normalize_id(str(data.get("bvid") or ""))
    aid = normalize_id(f"av{data.get('aid', '')}")
    if not bvid or not bvid.startswith("BV") or not data.get("title"):
        raise ValueError("B 站返回的视频资料不完整")
    raw_pages = data.get("pages")
    pages = [item for item in raw_pages if isinstance(item, dict)] if isinstance(raw_pages, list) else []
    part = next((item for item in pages if item.get("page") == page), None)
    if part is None and page != 1:
        raise ValueError(f"视频没有第 {page} 分 P")
    video_id = aid if id_preference == "av" and aid else bvid
    route = f"https://www.bilibili.com/video/{video_id}"
    if page > 1:
        route += f"?p={page}"
    description = str(data.get("desc") or "")
    if len(description) > description_limit:
        description = description[:description_limit] + "…" if description_limit else ""
    stat = data.get("stat")
    owner = data.get("owner")
    stat = stat if isinstance(stat, dict) else {}
    return {
        "标题": str(data["title"]),
        "UP主": str(owner.get("name") or "") if isinstance(owner, dict) else "",
        "简介": description,
        "分P": str(part.get("part") or "") if part and (len(pages) > 1 or page > 1) else "",
        "BV号": bvid,
        "AV号": aid or "",
        "封面": _cover_url(str(data.get("pic") or "")),
        "视频地址": route,
        "播放链接": f"https://player.bilibili.com/player.html?bvid={bvid}&p={page}",
        "点赞": _number(stat.get("like")),
        "投币": _number(stat.get("coin")),
        "收藏": _number(stat.get("favorite")),
        "转发": _number(stat.get("share")),
        "观看": _number(stat.get("view")),
        "弹幕": _number(stat.get("danmaku")),
        "tab": "\t",
    }
