"""Read-only network smoke checks; no API keys or paid model calls."""

import asyncio
import json
from backend import media, store


async def main():
    async def probe(name, url):
        try:
            data = await asyncio.wait_for(media.info(url), timeout=45)
            return {
                "platform": name,
                "url": url,
                "ok": True,
                "title": data.get("title"),
                "duration": data.get("duration"),
                "subtitle": media.choose_subtitle(data),
            }
        except (RuntimeError, asyncio.TimeoutError):
            return {
                "platform": name,
                "url": url,
                "ok": False,
                "reason": "当前网络或站点访问限制导致元数据探测失败；未提供 cookies。",
            }

    results = await asyncio.gather(
        probe("YouTube", "https://www.youtube.com/watch?v=jNQXAC9IVRw"),
        probe("B站", "https://www.bilibili.com/video/BV1xx411c7mD"),
    )
    store.write_json(store.ROOT / "source-probe.json", results)
    print(json.dumps(results, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
