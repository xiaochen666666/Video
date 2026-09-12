"""Optional real short-video download smoke test; no cloud model requests."""

import asyncio
import json
from backend import media, store


async def main():
    folder = store.ROOT / "download-smoke"
    folder.mkdir(exist_ok=True)
    report = {}
    url = "https://www.youtube.com/watch?v=jNQXAC9IVRw"
    try:
        await asyncio.wait_for(media.download(url, folder, subtitle=("en", False)), 60)
        files = list(folder.glob("sub.*"))
        report["subtitle_segments"] = (
            len(media.parse_subtitle(files[0])) if files else 0
        )
        await asyncio.wait_for(media.download(url, folder), 90)
        path = next(
            p for p in folder.glob("video.*") if p.suffix not in (".part", ".ytdl")
        )
        total = await media.duration(path)
        report.update(
            download=True,
            duration=total,
            frames=len(await media.frames(path, folder, total)),
        )
    except (RuntimeError, asyncio.TimeoutError, StopIteration):
        report.update(
            download=False, error="短视频下载或字幕获取受站点限制，未完成完整下载验证。"
        )
    store.write_json(store.ROOT / "download-smoke.json", report)
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
