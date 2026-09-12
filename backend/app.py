import asyncio
import io
import shutil
import uuid
import zipfile
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse
from fastapi import FastAPI, HTTPException, UploadFile, File, Request
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from . import store, worker, media
from .cloud import Bailian, CloudError


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
    region: str = "beijing"
    model: str = Field(default="qwen3.5-flash", min_length=1, max_length=100)
    asr_model: str = Field(default="qwen3-asr-flash", min_length=1, max_length=100)
    api_key: str | None = None
    asr_price_per_second: float | None = Field(default=None, ge=0)


@app.get("/api/settings")
def read_settings():
    config = store.settings()
    configured = bool(config.pop("api_key"))
    return config | {"has_key": configured}


@app.put("/api/settings")
def put_settings(body: Settings):
    if body.region not in ("beijing", "singapore"):
        raise HTTPException(400, "不支持的地域")
    config = store.settings()
    changes = body.model_dump(exclude={"api_key"})
    if body.api_key is not None:
        changes["api_key"] = body.api_key.strip()
    store.write_json(store.ROOT / "settings.json", config | changes)
    return read_settings()


@app.post("/api/settings/test")
async def test_settings():
    try:
        await Bailian().analyze('返回 {"ok":true}')
        return {
            "ok": True,
            "message": "文字模型连接成功。画面和语音能力将在任务中验证。",
        }
    except CloudError as e:
        raise HTTPException(400, str(e)) from None


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
