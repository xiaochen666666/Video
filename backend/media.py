import asyncio
import json
import re
import sys
from pathlib import Path
from urllib.parse import urlparse, parse_qsl, urlencode, urlunparse
import imageio_ffmpeg
from PIL import Image

FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()


def runtime_args():
    local = (
        Path(__file__).resolve().parents[1]
        / "node_modules"
        / "deno-bin"
        / "bin"
        / "deno.exe"
    )
    return (
        ["--js-runtimes", f"deno:{local}"]
        if local.exists()
        else ["--js-runtimes", "deno"]
    )


def valid_url(url):
    p = urlparse(url)
    trusted = (
        p.scheme == "https"
        and p.hostname
        in {
            "www.youtube.com",
            "youtube.com",
            "youtu.be",
            "www.bilibili.com",
            "bilibili.com",
            "b23.tv",
            "m.bilibili.com",
        }
        and not p.username
        and p.port in (None, 443)
    )
    if not trusted:
        return False
    if p.hostname in ("youtube.com", "www.youtube.com"):
        return (p.path == "/watch" and bool(dict(parse_qsl(p.query)).get("v"))) or bool(
            re.fullmatch(r"/(shorts|live)/[\w-]+/?", p.path)
        )
    if p.hostname in ("youtu.be", "b23.tv"):
        return bool(re.fullmatch(r"/[\w-]+/?", p.path))
    return bool(re.fullmatch(r"/video/(?:BV[\w]+|av\d+)/?", p.path))


async def run(*args):
    proc = await asyncio.create_subprocess_exec(
        *map(str, args), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        out, err = await proc.communicate()
    except asyncio.CancelledError:
        if proc.returncode is None:
            proc.kill()
        await proc.wait()
        raise
    if proc.returncode:
        # Raw extractor output can contain cookies, signed URLs and local paths.
        raise RuntimeError(
            "视频处理失败：请检查链接可访问性、登录 cookies 和网络；也可导入本地视频。"
        )
    return out.decode("utf-8", errors="replace"), err.decode("utf-8", errors="replace")


async def info(url, cookies=None):
    cmd = [
        sys.executable,
        "-m",
        "yt_dlp",
        "--ignore-config",
        *runtime_args(),
        "--no-playlist",
        "--dump-single-json",
        "--skip-download",
        "--no-warnings",
        "--socket-timeout",
        "30",
    ]
    if cookies:
        cmd += ["--cookies", cookies]
    text, _ = await run(*cmd, url)
    obj = json.loads(text)
    if obj.get("entries"):
        obj = next((x for x in obj["entries"] if x), {})
    if obj.get("is_live") or obj.get("live_status") == "is_live":
        raise RuntimeError("第一版暂不支持直播，请使用已结束的视频。")
    return obj


def choose_subtitle(meta):
    for automatic in (False, True):
        pool = meta.get("automatic_captions" if automatic else "subtitles") or {}
        preferred = [meta.get("language"), "zh-Hans", "zh-CN", "zh", "en"]
        for lang in [*preferred, *pool.keys()]:
            if lang and lang in pool and lang != "live_chat":
                return lang, automatic
    return None


async def download(url, folder, *, subtitle=None, cookies=None):
    cmd = [
        sys.executable,
        "-m",
        "yt_dlp",
        "--ignore-config",
        *runtime_args(),
        "--no-playlist",
        "--no-warnings",
        "--socket-timeout",
        "30",
        "--ffmpeg-location",
        FFMPEG,
    ]
    if cookies:
        cmd += ["--cookies", cookies]
    if subtitle:
        lang, auto = subtitle
        cmd += [
            "--skip-download",
            "--write-auto-subs" if auto else "--write-subs",
            "--sub-langs",
            lang,
            "--sub-format",
            "json3/vtt/best",
            "-o",
            str(folder / "sub.%(ext)s"),
        ]
    else:
        cmd += [
            "-f",
            "bestvideo[height<=720]+bestaudio/best[height<=720]/best",
            "--merge-output-format",
            "mp4",
            "-o",
            str(folder / "video.%(ext)s"),
        ]
    await run(*cmd, url)


def parse_subtitle(path):
    if path.suffix == ".json3":
        obj = json.loads(path.read_text("utf-8"))
        return [
            {
                "start": e["tStartMs"] / 1000,
                "end": (e["tStartMs"] + e.get("dDurationMs", 0)) / 1000,
                "text": "".join(s.get("utf8", "") for s in e.get("segs", [])).strip(),
                "source": "subtitle",
            }
            for e in obj.get("events", [])
            if e.get("segs")
        ]
    if path.suffix == ".json":
        obj = json.loads(path.read_text("utf-8"))
        return [
            {
                "start": e["from"],
                "end": e["to"],
                "text": e["content"],
                "source": "subtitle",
            }
            for e in obj.get("body", [])
        ]
    text = path.read_text("utf-8-sig")
    segments = []

    def seconds(s):
        nums = list(map(float, s.replace(",", ".").split(":")))
        return sum(v * 60**i for i, v in enumerate(reversed(nums)))

    for block in re.split(r"\n\s*\n", text):
        m = re.search(r"([\d:.,]+) --> ([\d:.,]+)[^\n]*\n([\s\S]+)", block)
        if m:
            segments.append(
                dict(
                    start=seconds(m[1]),
                    end=seconds(m[2]),
                    text=re.sub("<[^>]+>", "", m[3]).strip(),
                    source="subtitle",
                )
            )
    return segments


async def duration(path):
    _, err = await run(FFMPEG, "-i", path, "-t", "0", "-f", "null", "-")
    m = re.search(r"Duration: (\d+):(\d+):(\d+\.\d+)", err)
    if not m:
        raise RuntimeError("无法读取视频时长。")
    return int(m[1]) * 3600 + int(m[2]) * 60 + float(m[3])


async def has_audio(path):
    _, log = await run(FFMPEG, "-i", path, "-t", "0", "-f", "null", "-")
    return "Audio:" in log


async def split_audio(video, folder, total):
    _, log = await run(
        FFMPEG,
        "-i",
        video,
        "-vn",
        "-af",
        "silencedetect=noise=-35dB:d=0.35",
        "-f",
        "null",
        "-",
    )
    silences = [float(x) for x in re.findall(r"silence_end: ([\d.]+)", log)]
    chunks = []
    start = 0.0
    while start < total - 0.05:
        candidates = [t for t in silences if start + 15 < t <= start + 30]
        end = min(max(candidates) if candidates else start + 30, total)
        path = folder / f"audio-{len(chunks):04}.wav"
        if not path.exists():
            await run(
                FFMPEG,
                "-y",
                "-ss",
                start,
                "-i",
                video,
                "-t",
                end - start,
                "-vn",
                "-ac",
                "1",
                "-ar",
                "16000",
                path,
            )
        chunks.append((path, start, end))
        start = end
    return chunks


async def frames(video, folder, total):
    _, log = await run(
        FFMPEG,
        "-i",
        video,
        "-an",
        "-vf",
        "select='gt(scene,0.25)',showinfo",
        "-vsync",
        "vfr",
        "-f",
        "null",
        "-",
    )
    scenes = [float(x) for x in re.findall(r"pts_time:([\d.]+)", log)]
    chosen = []
    for minute in range(int(total // 60) + 1):
        a, b = minute * 60, min((minute + 1) * 60, total)
        # Reserve coverage across the minute, then fill remaining slots with changes.
        times = [t for t in (a + 1, a + 21, a + 41) if t < b]
        times += [
            t for t in scenes if a <= t < b and all(abs(t - x) > 3 for x in times)
        ][:3]
        chosen.extend(sorted(set(times))[:6])
    output = []
    previous = None
    for t in chosen:
        p = folder / f"frame-{round(t * 1000):09}.jpg"
        if not p.exists():
            await run(
                FFMPEG,
                "-y",
                "-ss",
                t,
                "-i",
                video,
                "-frames:v",
                "1",
                "-vf",
                "scale=1280:-2",
                "-q:v",
                "3",
                p,
            )
        if not p.exists():
            continue
        with Image.open(p) as img:
            thumb = list(img.convert("L").resize((32, 32)).getdata())
        if previous and sum(abs(a - b) for a, b in zip(thumb, previous)) / 1024 < 2:
            p.unlink(missing_ok=True)
            continue
        previous = thumb
        output.append({"time": t, "file": p.name})
    return output


def timestamp(seconds):
    s = int(seconds)
    return f"{s // 3600:02}:{s // 60 % 60:02}:{s % 60:02}"


def watch_url(url, seconds):
    if not valid_url(url):
        return ""
    p = urlparse(url)
    query = dict(parse_qsl(p.query))
    query["t"] = str(int(seconds))
    return urlunparse(p._replace(query=urlencode(query), fragment=""))
