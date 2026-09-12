import asyncio
import json
import time
from . import store, media
from .cloud import Bailian, DeepSeek, Compatible, CloudError, KEY_FIELDS

ACTIVE = {}


def checkpoint(folder):
    p = folder / "checkpoint.json"
    return json.loads(p.read_text("utf-8")) if p.exists() else {}


def parse_json(raw):
    try:
        if raw.strip().startswith("```") and raw.strip().endswith("```"):
            raw = raw.strip().split("\n", 1)[1].rsplit("```", 1)[0].strip()
        obj = json.loads(raw)
        if not isinstance(obj, dict):
            raise ValueError()
        return obj
    except ValueError:
        raise CloudError("模型没有返回有效的 JSON 笔记，请重试当前阶段。") from None


async def process(task_id):
    task = store.get(task_id)
    folder = store.ROOT / task_id
    folder.mkdir(exist_ok=True)
    cp = checkpoint(folder)
    config = store.settings()
    client = {"deepseek": DeepSeek, "custom": Compatible, "bailian": Bailian}[
        config.get("provider", "bailian")
    ](config)
    result = (
        cp.get("_result")
        or task["result"]
        or {
            "usage": {
                "input_tokens": 0,
                "output_tokens": 0,
                "audio_seconds": 0,
                "estimated_cny": 0,
                "price_date": config["price_date"],
            }
        }
    )
    began = time.monotonic()
    elapsed_before = result.get("elapsed_seconds", 0)

    def save():
        result["elapsed_seconds"] = round(elapsed_before + time.monotonic() - began, 2)
        cp["_result"] = result
        store.write_json(folder / "checkpoint.json", cp)
        store.update(task_id, result=result)

    def stage(text):
        store.update(task_id, stage=text)

    async def call(key, prompt=None, images=(), audio=None, seconds=0):
        if key in cp:
            return cp[key]
        raw, usage = await (
            client.transcribe(audio) if audio else client.analyze(prompt, images)
        )
        value = raw if audio else parse_json(raw)
        if not audio:
            if key.startswith("visual-"):
                if not isinstance(value.get("observations"), list):
                    raise CloudError("画面识别返回格式不正确，请重试。")
            else:
                if (
                    not isinstance(value.get("summary"), str)
                    or not isinstance(value.get("points"), list)
                    or not all(isinstance(x, str) for x in value["points"])
                ):
                    raise CloudError("笔记返回格式不正确，请重试。")
                if key.startswith("chapter-"):
                    if not isinstance(value.get("title"), str):
                        raise CloudError("章节标题格式不正确，请重试。")
                    for field in ("visual_notes", "steps", "uncertainties"):
                        value.setdefault(field, [])
                        if not isinstance(value[field], list) or not all(
                            isinstance(x, str) for x in value[field]
                        ):
                            raise CloudError("章节内容格式不正确，请重试。")
        cp[key] = value
        u = result["usage"]
        inp, out = (
            usage.get("prompt_tokens", usage.get("input_tokens", 0)),
            usage.get("completion_tokens", usage.get("output_tokens", 0)),
        )
        u["input_tokens"] += inp
        u["output_tokens"] += out
        u["audio_seconds"] += seconds
        if audio:
            if config["asr_price_per_second"] is not None:
                u["estimated_cny"] += seconds * config["asr_price_per_second"]
            else:
                u["asr_cost_unknown"] = True
        elif (
            config.get("provider", "bailian") == "bailian"
            and config["region"] == "beijing"
            and config["model"] == "qwen3.5-flash"
        ):
            multiplier = 1 if inp <= 128000 else 4 if inp <= 256000 else 6
            u["estimated_cny"] += (
                (inp * config["input_price"] + out * config["output_price"])
                * multiplier
                / 1e6
            )
        else:
            u["model_cost_unknown"] = True
        save()
        return value

    store.update(task_id, status="running", error="")
    try:
        key_field = KEY_FIELDS[config.get("provider", "bailian")]
        if not config[key_field]:
            raise CloudError("请先在设置中填写当前服务商的 API Key，再重试任务。")
        cookies = store.ROOT / "cookies.txt"
        cookies = cookies if cookies.exists() else None
        source = task["source"]
        video = next(iter(folder.glob("upload.*")), None)
        if "meta" not in cp:
            stage("读取视频信息")
            if video:
                cp["meta"] = {
                    "title": task["title"],
                    "duration": await media.duration(video),
                }
            else:
                metadata = await media.info(source, cookies)
                cp["meta"] = {
                    "title": metadata.get("title", "视频"),
                    "duration": metadata.get("duration", 0),
                }
                cp["subtitle_choice"] = media.choose_subtitle(metadata)
            store.update(task_id, title=cp["meta"]["title"])
            save()
        result["meta"] = cp["meta"]
        total = cp["meta"]["duration"]
        if not total or total > 4 * 3600:
            raise RuntimeError("视频时长无效或超过第一版 4 小时上限。")
        if "segments" not in cp and cp.get("subtitle_choice"):
            stage("读取字幕")
            try:
                await media.download(
                    source, folder, subtitle=cp["subtitle_choice"], cookies=cookies
                )
                files = list(folder.glob("sub.*"))
                cp["segments"] = media.parse_subtitle(files[0]) if files else []
            except (RuntimeError, ValueError, KeyError):
                cp["segments"] = []
            save()

        async def ensure_video():
            existing = video or next(iter(folder.glob("video.*")), None)
            if existing and existing.suffix not in (".part", ".ytdl"):
                return existing
            stage("下载视频素材")
            await media.download(source, folder, cookies=cookies)
            return next(
                p for p in folder.glob("video.*") if p.suffix not in (".part", ".ytdl")
            )

        if not cp.get("segments"):
            video = await ensure_video()
            audio_present = await media.has_audio(video)
            stage(
                "提取音频并按静音切分" if audio_present else "无音轨，改用关键画面整理"
            )
            chunks = (
                await media.split_audio(video, folder, total) if audio_present else []
            )
            segments = []
            for i, (path, start, end) in enumerate(chunks):
                stage(f"语音转写 {i + 1}/{len(chunks)}")
                text = await call(f"asr-{i}", audio=path, seconds=end - start)
                segments.append(
                    {"start": start, "end": end, "text": text, "source": "audio_chunk"}
                )
            cp["segments"] = segments or [
                {
                    "start": 0,
                    "end": total,
                    "text": "视频没有音轨，请只依据画面总结。",
                    "source": "no_audio",
                }
            ]
            save()
        segments = cp["segments"]
        result["timing"] = (
            "片段时间（最长 30 秒）"
            if any(s["source"] == "audio_chunk" for s in segments)
            else "字幕时间"
        )
        if segments[0]["source"] == "no_audio":
            result["timing"] = "画面采样时间"
        # Bounded chunks keep long courses from overflowing a single request.
        groups, current, size = [], [], 0
        for s in segments:
            current.append(s)
            size += len(s["text"])
            if size >= 10000:
                groups.append(current)
                current = []
                size = 0
        if current:
            groups.append(current)
        if segments[0]["source"] == "no_audio":
            groups = []
        notes = []
        for i, group in enumerate(groups):
            stage(f"生成文字速览 {i + 1}/{len(groups)}")
            notes.append(
                await call(
                    f"text-{i}",
                    prompt='根据这些带时间戳的素材提炼笔记。返回 {"summary":"简要总结","points":["核心要点"]}。素材：'
                    + json.dumps(group, ensure_ascii=False),
                )
            )
        result["overview"] = (
            await call(
                "overview",
                prompt='整合为视频速览。返回 {"summary":"一句话结论","points":["5至8个核心要点"]}。资料：'
                + json.dumps(notes, ensure_ascii=False),
            )
            if groups
            else {"summary": "视频没有音轨，正在依据关键画面整理笔记。", "points": []}
        )
        result.setdefault(
            "overview_seconds", round(elapsed_before + time.monotonic() - began, 2)
        )
        save()
        if "frames" not in cp:
            video = await ensure_video()
            stage("检测场景变化并提取关键帧")
            cp["frames"] = await media.frames(video, folder, total)
            save()
        frames = cp["frames"]
        chapters = []
        # Five-minute chapters, with each visual batch bounded to six frames.
        for index, start in enumerate(range(0, int(total) + 1, 300)):
            if start >= total:
                break
            end = min(start + 300, total)
            local_frames = [f for f in frames if start <= f["time"] < end]
            local_segments = [
                s for s in segments if s["start"] < end and s["end"] > start
            ]
            visual = []
            for j in range(0, len(local_frames), 6):
                batch = local_frames[j : j + 6]
                stage(
                    f"画面分析 · 第 {index + 1} 章 · {j // 6 + 1}/{(len(local_frames) + 5) // 6}"
                )
                neighbors = [
                    s
                    for s in local_segments
                    if s["start"] <= batch[-1]["time"] + 15
                    and s["end"] >= batch[0]["time"] - 15
                ]
                visual.append(
                    await call(
                        f"visual-{index}-{j}",
                        prompt='按图片顺序识别 PPT、图表趋势、可见操作与不确定信息。返回 {"observations":[{"time":秒数,"detail":"内容","uncertainty":"不确定内容，没有则空字符串"}]}。图片时间：'
                        + json.dumps([f["time"] for f in batch])
                        + "。邻近字幕："
                        + json.dumps(neighbors, ensure_ascii=False),
                        images=[folder / f["file"] for f in batch],
                    )
                )
            stage(f"整理章节 {index + 1}")
            chapter = await call(
                f"chapter-{index}",
                prompt='整理本章笔记，区分字幕和画面证据。返回 {"title":"章节标题","summary":"章节总结","points":["重点"],"visual_notes":["画面补充"],"steps":["仅明确观察到的操作步骤"],"uncertainties":["疑问"]}。字幕：'
                + json.dumps(local_segments, ensure_ascii=False)
                + "。画面："
                + json.dumps(visual, ensure_ascii=False),
            )
            chapter.update(
                start=start,
                end=end,
                frames=local_frames,
                watch_url=media.watch_url(source, start),
            )
            chapters.append(chapter)
            result["chapters"] = chapters
            save()
        final_notes = [
            {
                k: v
                for k, v in c.items()
                if k in ("title", "summary", "points", "uncertainties")
            }
            for c in chapters
        ]
        result["overview"] = await call(
            "final",
            prompt='整合各章文本和画面信息形成最终速览，保留不确定性。返回 {"summary":"一句话结论","points":["5至8条核心要点"]}。资料：'
            + json.dumps(final_notes, ensure_ascii=False),
        )
        save()
        for pattern in ("video.*", "upload.*", "audio-*.wav", "sub.*"):
            for p in folder.glob(pattern):
                p.unlink(missing_ok=True)
        store.update(task_id, status="completed", stage="完整总结已完成")
    except asyncio.CancelledError:
        if store.get(task_id)["status"] != "queued":
            store.update(
                task_id, status="cancelled", stage="已取消，已完成的片段会保留"
            )
        raise
    except (CloudError, RuntimeError) as e:
        store.update(task_id, status="paused", error=str(e), stage="任务暂停")
    except OSError:
        store.update(
            task_id,
            status="paused",
            error="无法读写本地文件，请检查可用磁盘空间和目录权限后重试。",
            stage="任务暂停",
        )
    except Exception:
        store.update(
            task_id,
            status="paused",
            error="处理遇到异常，已保留检查点，请重试。",
            stage="任务暂停",
        )


async def loop():
    for t in store.tasks():
        if t["status"] == "running":
            store.update(t["id"], status="queued", stage="等待恢复")
    while True:
        pending = [t for t in reversed(store.tasks()) if t["status"] == "queued"]
        if pending:
            task_id = pending[0]["id"]
            child = asyncio.create_task(process(task_id))
            ACTIVE[task_id] = child
            try:
                await child
            except asyncio.CancelledError:
                if asyncio.current_task().cancelling():
                    store.update(task_id, status="queued", stage="等待恢复")
                    raise
            except Exception:
                store.update(
                    task_id,
                    status="paused",
                    stage="任务暂停",
                    error="任务检查点无法读取，请删除该任务后重新导入。",
                )
            finally:
                ACTIVE.pop(task_id, None)
        else:
            await asyncio.sleep(0.5)
