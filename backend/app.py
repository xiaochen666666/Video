import asyncio
import io
import math
import shutil
import uuid
import zipfile
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse
from typing import Literal
from PIL import Image, ImageDraw
from fastapi import FastAPI, HTTPException, UploadFile, File, Request
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from . import store, worker, media
from .cloud import get_client, CloudError, normalize_base, SECRET_FIELDS, KEY_FIELDS


@asynccontextmanager
async def lifespan(app):
    runner = asyncio.create_task(worker.loop())
    yield
    runner.cancel()
    with suppress(asyncio.CancelledError):
        await runner


app = FastAPI(title="拾帧 Video Notes", lifespan=lifespan)


@app.get("/api/health")
def health():
    return {"app": "video-notes", "version": "1.0.0"}


@app.middleware("http")
async def local_only(request: Request, call_next):
    host = request.url.hostname
    origin = request.headers.get("origin")
    if host not in ("127.0.0.1", "localhost", "testserver") or (
        origin and urlparse(origin).hostname not in ("127.0.0.1", "localhost")
    ):
        return Response("Only local requests are allowed", status_code=403)
    return await call_next(request)


class Settings(BaseModel):
    provider: str = "bailian"
    deepseek_model: str = Field(default="deepseek-flash", min_length=1, max_length=100)
    deepseek_api_key: str | None = None
    custom_base_url: str = ""
    custom_model: str = Field(default="", max_length=100)
    custom_vision_model: str = Field(default="", max_length=100)
    custom_api_key: str | None = None
    custom_json_mode: bool = True
    asr_provider: str = "bailian"
    custom_asr_base_url: str = ""
    custom_asr_model: str = Field(default="whisper-1", min_length=1, max_length=100)
    custom_asr_api_key: str | None = None
    region: str = "beijing"
    model: str = Field(default="qwen3.5-flash", min_length=1, max_length=100)
    asr_model: str = Field(default="qwen3-asr-flash", min_length=1, max_length=100)
    api_key: str | None = None
    asr_price_per_second: float | None = Field(default=None, ge=0)


@app.get("/api/settings")
def read_settings():
    config = store.settings()
    flags = {field: bool(config.pop(field, "")) for field in SECRET_FIELDS}
    return config | {
        "has_key": flags[KEY_FIELDS[config["provider"]]],
        "has_bailian_key": flags["api_key"],
        "has_deepseek_key": flags["deepseek_api_key"],
        "has_custom_key": flags["custom_api_key"],
        "has_custom_asr_key": flags["custom_asr_api_key"],
    }


@app.put("/api/settings")
def put_settings(body: Settings):
    if body.region not in ("beijing", "singapore"):
        raise HTTPException(400, "不支持的地域")
    if body.provider not in ("bailian", "deepseek", "custom"):
        raise HTTPException(400, "不支持的服务商")
    if body.asr_provider not in ("bailian", "none", "custom"):
        raise HTTPException(400, "不支持的语音接口类型")
    config = store.settings()
    changes = body.model_dump(exclude=set(SECRET_FIELDS), exclude_unset=True)
    for field in SECRET_FIELDS:
        value = getattr(body, field)
        if value is not None:
            changes[field] = value.strip()
    for base, key in [
        ("custom_base_url", "custom_api_key"),
        ("custom_asr_base_url", "custom_asr_api_key"),
    ]:
        if base in changes:
            try:
                changes[base] = normalize_base(changes[base]) if changes[base] else ""
            except ValueError as e:
                raise HTTPException(400, str(e)) from None
            if changes[base] != config[base] and key not in changes:
                changes[key] = (
                    ""  # A saved credential never follows an edited destination.
                )
    merged = config | changes
    if merged["provider"] == "custom" and (
        not merged["custom_base_url"] or not merged["custom_model"].strip()
    ):
        raise HTTPException(400, "请填写自定义 API 地址和文字模型。")
    if merged["asr_provider"] == "custom" and not merged["custom_asr_base_url"]:
        raise HTTPException(400, "请填写语音 API 地址。")
    store.write_json(store.ROOT / "settings.json", config | changes)
    return read_settings()


@app.post("/api/settings/test")
async def test_settings(kind: Literal["text", "vision"] = "text"):
    image_path = None
    try:
        config = store.settings()
        client = get_client(config)
        if kind == "vision":
            image_path = store.ROOT / f"connection-{uuid.uuid4().hex}.jpg"
            image = Image.new("RGB", (128, 128), "white")
            ImageDraw.Draw(image).rectangle((24, 24, 104, 104), fill="red")
            image.save(image_path)
        await client.analyze(
            '返回 JSON：{"ok":true}。如果提供了图片，请在 color 字段填写图片中方块的颜色。',
            [image_path] if image_path else [],
        )
        return {
            "ok": True,
            "message": f"{'画面' if kind == 'vision' else '文字'}接口调用成功。实际总结质量请在视频任务中验证。",
        }
    except CloudError as e:
        raise HTTPException(400, str(e)) from None
    finally:
        if image_path:
            image_path.unlink(missing_ok=True)


@app.post("/api/cookies")
async def cookies(file: UploadFile = File(...)):
    data = await file.read(2 * 1024 * 1024 + 1)
    if (
        len(data) > 2 * 1024 * 1024
        or b"Cookies" not in data[:300]
        and b"Cookie" not in data[:300]
    ):
        raise HTTPException(400, "请选择 Netscape 格式的 cookies.txt，最大 2 MB。")
    (store.ROOT / "cookies.txt").write_bytes(data)
    return {"ok": True}


@app.delete("/api/cookies")
def delete_cookies():
    (store.ROOT / "cookies.txt").unlink(missing_ok=True)
    return {"ok": True}


class NewTask(BaseModel):
    url: str = Field(max_length=2048)


def create_task(source, title):
    task_id = uuid.uuid4().hex
    with store.connect() as db:
        db.execute(
            "INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?)",
            (
                task_id,
                source,
                title,
                "queued",
                "等待处理",
                "",
                datetime.now(timezone.utc).isoformat(),
                "{}",
            ),
        )
    return store.get(task_id)


@app.post("/api/tasks")
def new_task(body: NewTask):
    url = body.url.strip()
    try:
        valid = media.valid_url(url)
    except ValueError:
        valid = False
    if not valid:
        raise HTTPException(400, "请输入 B站或 YouTube 的 HTTPS 视频链接。")
    existing = next(
        (
            t
            for t in store.tasks()
            if t["source"] == url and t["status"] in ("completed", "queued", "running")
        ),
        None,
    )
    return (
        (existing | {"cached": True}) if existing else create_task(url, "新的视频笔记")
    )


@app.post("/api/tasks/upload")
async def upload(file: UploadFile = File(...)):
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in (".mp4", ".mkv", ".webm", ".mov", ".avi", ".m4v"):
        raise HTTPException(400, "请选择 MP4、MKV、WebM、MOV、AVI 或 M4V 视频。")
    task_id = uuid.uuid4().hex
    folder = store.ROOT / task_id
    folder.mkdir()
    try:
        size = 0
        with (folder / f"upload{suffix}").open("wb") as f:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > 2 * 1024**3:
                    raise HTTPException(413, "本地视频最大 2 GB。")
                f.write(chunk)
        with store.connect() as db:
            db.execute(
                "INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?)",
                (
                    task_id,
                    "local",
                    Path(file.filename).name,
                    "queued",
                    "等待处理",
                    "",
                    datetime.now(timezone.utc).isoformat(),
                    "{}",
                ),
            )
        return store.get(task_id)
    except BaseException:
        shutil.rmtree(folder)
        raise


@app.get("/api/tasks")
def list_tasks():
    return store.tasks()


def require_task(task_id):
    task = store.get(task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    return task


@app.post("/api/tasks/{task_id}/subtitle")
async def import_subtitle(task_id: str, file: UploadFile = File(...)):
    task = require_task(task_id)
    if task["status"] not in ("paused", "cancelled"):
        raise HTTPException(409, "请先取消任务，再导入字幕；已完成的任务请重新创建。")
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in (".srt", ".vtt", ".json3", ".json"):
        raise HTTPException(400, "支持 SRT、VTT、JSON3 和 B站 JSON 字幕。")
    data = await file.read(2 * 1024 * 1024 + 1)
    if len(data) > 2 * 1024 * 1024:
        raise HTTPException(413, "字幕文件最大 2 MB。")
    # Recheck after awaiting upload; another request may have resumed the task.
    task = require_task(task_id)
    if task["status"] not in ("paused", "cancelled"):
        raise HTTPException(409, "任务已恢复，请先取消任务。")
    folder = store.ROOT / task_id
    folder.mkdir(exist_ok=True)
    path = folder / ("imported" + suffix)
    try:
        path.write_bytes(data)
        segments = media.parse_subtitle(path)
        valid = segments and all(
            isinstance(s["text"], str)
            and math.isfinite(s["start"])
            and math.isfinite(s["end"])
            and 0 <= s["start"] < s["end"]
            for s in segments
        )
        if not valid:
            raise ValueError()
    except (ValueError, KeyError, TypeError, UnicodeError):
        raise HTTPException(
            400, "字幕格式或时间戳无效，请使用 UTF-8 编码的字幕文件。"
        ) from None
    finally:
        path.unlink(missing_ok=True)
    old = worker.checkpoint(folder)
    result = {
        k: v
        for k, v in task["result"].items()
        if k in ("usage", "elapsed_seconds", "meta")
    }
    cp = {k: v for k, v in old.items() if k in ("meta", "frames", "subtitle_choice")}
    cp.update(segments=sorted(segments, key=lambda s: s["start"]), _result=result)
    store.write_json(folder / "checkpoint.json", cp)
    store.update(task_id, result=result, error="", stage="字幕已导入，请点击继续处理")
    return require_task(task_id)


@app.get("/api/tasks/{task_id}")
def get_task(task_id: str):
    return require_task(task_id)


@app.post("/api/tasks/{task_id}/cancel")
async def cancel(task_id: str):
    task = require_task(task_id)
    if task["status"] == "completed":
        return task
    store.update(task_id, status="cancelled", stage="已取消")
    if task_id in worker.ACTIVE:
        child = worker.ACTIVE[task_id]
        child.cancel()
        with suppress(asyncio.CancelledError):
            await child
    return require_task(task_id)


@app.post("/api/tasks/{task_id}/retry")
def retry(task_id: str):
    task = require_task(task_id)
    if task["status"] in ("paused", "cancelled"):
        store.update(task_id, status="queued", stage="等待恢复", error="")
    return require_task(task_id)


@app.delete("/api/tasks/{task_id}")
async def delete(task_id: str):
    await cancel(task_id)
    folder = store.ROOT / task_id
    if folder.exists():
        shutil.rmtree(folder)
    with store.connect() as db:
        db.execute("DELETE FROM tasks WHERE id=?", (task_id,))
    return {"ok": True}


@app.get("/api/tasks/{task_id}/frames/{filename}")
def frame(task_id: str, filename: str):
    require_task(task_id)
    if (
        not filename.startswith("frame-")
        or Path(filename).name != filename
        or not filename.endswith(".jpg")
    ):
        raise HTTPException(404)
    path = store.ROOT / task_id / filename
    if not path.exists():
        raise HTTPException(404)
    return FileResponse(path)


def markdown(task):
    r = task["result"]
    overview = r.get("overview", {})
    lines = [
        "# " + task["title"],
        "",
        f"状态：{task['stage']}",
        "",
        overview.get("summary", ""),
        "",
    ]
    lines += ["- " + str(x) for x in overview.get("points", [])]
    for c in r.get("chapters", []):
        lines += [
            "",
            f"## {media.timestamp(c['start'])} {c.get('title', '章节')}",
            "",
            c.get("summary", ""),
        ]
        if c.get("watch_url"):
            lines += ["", f"[回看原视频]({c['watch_url']})"]
        for key, label in [
            ("points", "内容要点"),
            ("visual_notes", "画面补充"),
            ("steps", "操作步骤"),
            ("uncertainties", "待确认"),
        ]:
            if c.get(key):
                lines += ["", f"### {label}", ""] + ["- " + str(x) for x in c[key]]
        lines += ["", f"时间定位：{r.get('timing', '字幕时间')}"]
        for f in c.get("frames", []):
            lines += [f"![{media.timestamp(f['time'])}](images/{f['file']})"]
    return "\n".join(lines) + "\n"


@app.get("/api/tasks/{task_id}/export")
def export(task_id: str, format: str = "md"):
    task = require_task(task_id)
    content = markdown(task)
    if format == "zip":
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("notes.md", content)
            for p in (store.ROOT / task_id).glob("frame-*.jpg"):
                archive.write(p, "images/" + p.name)
        return Response(
            buffer.getvalue(),
            media_type="application/zip",
            headers={"Content-Disposition": 'attachment; filename="video-notes.zip"'},
        )
    if format != "md":
        raise HTTPException(400, "不支持的导出格式")
    # Standalone Markdown uses local app links; ZIP contains portable image assets.
    content = content.replace(
        "](images/", f"](http://127.0.0.1:8765/api/tasks/{task_id}/frames/"
    )
    return Response(
        content,
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="video-notes.md"'},
    )


dist = Path(__file__).resolve().parents[1] / "dist"
if dist.exists():
    app.mount("/", StaticFiles(directory=dist, html=True), name="web")
