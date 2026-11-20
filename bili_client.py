"""Async client for Bilibili's public video detail and search endpoints."""

import asyncio
from hashlib import md5
from pathlib import PurePosixPath
from time import monotonic, time
from urllib.parse import urlencode, urljoin, urlsplit

import aiohttp
from yarl import URL

from .bilibili import VideoTarget, parse_url, search_results

API = "https://api.bilibili.com"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.bilibili.com/",
}
WBI_ORDER = (
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
    27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
)
WBI_REMOVE = str.maketrans("", "", "!'()*")


class BiliError(ValueError):
    pass


class BiliClient:
    def __init__(self, session: aiohttp.ClientSession):
        self.session = session
        self._wbi_key = ""
        self._wbi_expires = 0.0
        self._wbi_lock = asyncio.Lock()
        self._guest_cookies = {}
        self._guest_expires = 0.0
        self._guest_lock = asyncio.Lock()

    async def _request(self, path: str, cookies=None) -> dict:
        async with self.session.get(
            URL(API + path, encoded=True), cookies=cookies, allow_redirects=False, headers=HEADERS,
        ) as response:
            if response.status != 200:
                raise BiliError(f"B 站请求失败（HTTP {response.status}）")
            result = await response.json(content_type=None)
        if not isinstance(result, dict):
            raise BiliError("B 站返回了无法识别的数据")
        return result

    @staticmethod
    def _data(response: dict) -> dict:
        if response.get("code") != 0 or not isinstance(response.get("data"), dict):
            raise BiliError(f"B 站接口暂不可用（{response.get('code', '未知错误')}）")
        return response["data"]

    async def _wbi(self) -> str:
        async with self._wbi_lock:
            if self._wbi_expires > monotonic():
                return self._wbi_key
            # The navigation endpoint also returns WBI images to signed-out clients.
            result = await self._request("/x/web-interface/nav")
            data = result.get("data")
            images = data.get("wbi_img") if isinstance(data, dict) else None
            if not isinstance(images, dict):
                raise BiliError("B 站未返回有效的网页签名参数")
            key = "".join(
                PurePosixPath(urlsplit(str(images.get(field) or "")).path).stem
                for field in ("img_url", "sub_url")
            )
            if len(key) != 64:
                raise BiliError("B 站未返回有效的网页签名参数")
            self._wbi_key = "".join(key[index] for index in WBI_ORDER)
            self._wbi_expires = monotonic() + 1800
            return self._wbi_key

    async def _signed_request(self, path: str, params: dict, cookies=None) -> dict:
        key = await self._wbi()
        params = {**params, "wts": int(time())}
        query = urlencode([
            (name, str(value).translate(WBI_REMOVE))
            for name, value in sorted(params.items())
        ])
        signature = md5((query + key).encode(), usedforsecurity=False).hexdigest()
        response = await self._request(f"{path}?{query}&w_rid={signature}", cookies=cookies)
        if response.get("code") in {-403, -352}:
            self._wbi_expires = 0
        return self._data(response)

    async def _search_cookies(self) -> dict:
        async with self._guest_lock:
            if self._guest_expires <= monotonic():
                result = self._data(await self._request("/x/frontend/finger/spi"))
                buvid3 = result.get("b_3")
                if not isinstance(buvid3, str) or not buvid3:
                    raise BiliError("B 站未返回匿名访问标识")
                self._guest_cookies = {"buvid3": buvid3}
                if isinstance(result.get("b_4"), str) and result["b_4"]:
                    self._guest_cookies["buvid4"] = result["b_4"]
                self._guest_expires = monotonic() + 1800
            return self._guest_cookies

    async def resolve(self, target: VideoTarget) -> VideoTarget:
        for _ in range(5):
            if not target.is_short:
                return target
            async with self.session.get(
                target.reference, allow_redirects=False, headers=HEADERS,
            ) as response:
                location = response.headers.get("Location", "")
                if response.status not in {301, 302, 303, 307, 308} or not location:
                    break
                resolved = parse_url(urljoin(target.reference, location), target.page)
                if not resolved:
                    break
                target = resolved
        if not target.is_short:
            return target
        raise BiliError("短链接未跳转到有效的 B 站视频")

    async def details(self, target: VideoTarget) -> dict:
        params = (
            {"bvid": target.reference} if target.reference.startswith("BV")
            else {"aid": target.reference[2:]}
        )
        return await self._signed_request("/x/web-interface/wbi/view", params)

    async def search(self, keyword: str) -> list[tuple[str, str, str]]:
        data = await self._signed_request(
            "/x/web-interface/wbi/search/type",
            {"keyword": keyword, "search_type": "video", "page": 1},
            cookies=await self._search_cookies(),
        )
        items = data.get("result")
        if items is None:
            return []
        if not isinstance(items, list):
            raise BiliError("B 站返回了无法识别的搜索结果")
        return search_results(items)
