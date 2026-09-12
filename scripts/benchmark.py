"""Synthetic 10/60 minute integration benchmark. NEVER claims cloud accuracy/latency.

Uses a separate data directory; performs real FFmpeg media work with a fake cloud
adapter. Results are reproducible without a key and incur no API charges.
"""

import asyncio
import json
import os
import time
from pathlib import Path

os.environ["VIDEO_DATA_DIR"] = str(
    Path(__file__).resolve().parents[1] / "data" / "benchmark"
)
from PIL import Image, ImageDraw, ImageFont
from backend import store, worker, media
from backend.app import create_task


class FakeCloud:
    calls = 0

    def __init__(self, *args):
        pass

    async def analyze(self, prompt, images=()):
        FakeCloud.calls += 1
        if images:
            result = {
                "observations": [
                    {
                        "time": 0,
                        "detail": "模拟响应：幻灯片、图表或操作画面",
                        "uncertainty": "此测试不验证模型识别准确率",
                    }
                ]
            }
        else:
            result = {
                "title": "模拟章节",
                "summary": "模拟 API 输出，用于验证处理链路。",
                "points": ["字幕内容"],
                "visual_notes": ["模拟画面说明"],
                "steps": ["模拟步骤"],
                "uncertainties": ["未调用真实模型"],
            }
        return json.dumps(result, ensure_ascii=False), {
            "prompt_tokens": 100,
            "completion_tokens": 30,
        }

    async def transcribe(self, path):
        FakeCloud.calls += 1
        return "模拟语音内容，用于验证分片转写。", {}


async def main():
    worker.Bailian = FakeCloud
    store.write_json(
        store.ROOT / "settings.json",
        store.DEFAULTS | {"api_key": "fake-benchmark-not-a-real-key"},
    )
    rows = []
    font = ImageFont.truetype("C:/Windows/Fonts/msyh.ttc", 28)
    for duration, subtitles in [(600, True), (3600, False)]:
        task = create_task("local", f"模拟 {duration // 60} 分钟：PPT、图表与操作")
        folder = store.ROOT / task["id"]
        folder.mkdir()
        for i in range(3):
            image = Image.new("RGB", (640, 360), ["#eef4e8", "#e7eef5", "#f6f1e6"][i])
            d = ImageDraw.Draw(image)
            d.text(
                (30, 25),
                [
                    "PPT：视频总结的三个步骤",
                    "图表：学习时间对比",
                    "操作：粘贴链接后生成笔记",
                ][i],
                font=font,
                fill="#254032",
            )
            if i == 0:
                for j, text in enumerate(
                    ["1. 获取字幕", "2. 分析关键帧", "3. 整理章节"]
                ):
                    d.text((50, 110 + j * 65), text, font=font, fill="#527347")
            elif i == 1:
                for j, h in enumerate([70, 140, 210]):
                    d.rectangle(
                        (65 + j * 150, 310 - h, 145 + j * 150, 310), fill="#658b77"
                    )
            else:
                d.rounded_rectangle(
                    (30, 120, 600, 190), radius=10, outline="#66816a", width=3
                )
                d.text(
                    (50, 140), "https://video.example/lesson", font=font, fill="#567050"
                )
                d.rounded_rectangle((380, 230, 600, 290), radius=10, fill="#315d49")
                d.text((425, 240), "生成笔记", font=font, fill="white")
            image.save(folder / f"slide-{i}.png")
        # Each image lasts 20 seconds; repeat the sequence for the requested duration.
        concat = folder / "slides.txt"
        concat.write_text(
            "".join(f"file 'slide-{i}.png'\nduration 20\n" for i in range(3))
            + "file 'slide-0.png'\n",
            "utf-8",
        )
        await media.run(
            media.FFMPEG,
            "-y",
            "-stream_loop",
            "-1",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            concat,
            "-f",
            "lavfi",
            "-i",
            "anullsrc=r=16000:cl=mono",
            "-t",
            duration,
            "-r",
            "1",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            folder / "upload.mp4",
        )
        if subtitles:
            store.write_json(
                folder / "checkpoint.json",
                {
                    "meta": {"title": task["title"], "duration": duration},
                    "segments": [
                        {
                            "start": i,
                            "end": i + 30,
                            "text": "视频讲解：获取字幕，分析 PPT 图表，整理操作步骤。",
                            "source": "subtitle",
                        }
                        for i in range(0, duration, 30)
                    ],
                },
            )
        count = FakeCloud.calls
        began = time.perf_counter()
        await worker.process(task["id"])
        t = store.get(task["id"])
        r = t["result"]
        rows.append(
            {
                "duration_minutes": duration // 60,
                "subtitles": subtitles,
                "status": t["status"],
                "overview_seconds": r.get("overview_seconds"),
                "total_seconds": round(time.perf_counter() - began, 2),
                "chapters": len(r.get("chapters", [])),
                "frames": sum(len(c["frames"]) for c in r.get("chapters", [])),
                "mock_api_calls": FakeCloud.calls - count,
                "real_api_cost": 0,
                "error": t["error"],
            }
        )
        print(json.dumps(rows[-1], ensure_ascii=False), flush=True)
    store.write_json(store.ROOT / "results.json", rows)


if __name__ == "__main__":
    asyncio.run(main())
