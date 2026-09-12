import asyncio
import io
import json
import zipfile
import pytest
import httpx
from fastapi.testclient import TestClient
from backend import store, worker, media
from backend.app import app, create_task
from backend.cloud import Bailian, CloudError


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "ROOT", tmp_path)
    with store.connect() as db:
        db.execute(
            "CREATE TABLE tasks (id TEXT PRIMARY KEY, source TEXT, title TEXT, status TEXT, stage TEXT, error TEXT, created TEXT, result TEXT)"
        )
    return tmp_path


def test_settings_redaction_and_origin(isolated):
    client = TestClient(app)
    result = client.put("/api/settings", json={"api_key": "secret-test-key"}).json()
    assert result["has_key"] and "secret-test-key" not in json.dumps(result)
    assert "api_key" not in client.get("/api/settings").json()
    client.put("/api/settings", json={"model": "qwen3.5-flash"})
    assert store.settings()["api_key"] == "secret-test-key"
    assert (
        client.put(
            "/api/settings", json={}, headers={"Origin": "https://example.org"}
        ).status_code
        == 403
    )
    assert client.get("/api/tasks", headers={"Host": "evil.example"}).status_code == 403


def test_tasks_cache_and_exports(isolated):
    client = TestClient(app)
    assert (
        client.post("/api/tasks", json={"url": "https://example.com/video"}).status_code
        == 400
    )
    t = client.post(
        "/api/tasks", json={"url": "https://www.youtube.com/watch?v=test"}
    ).json()
    folder = isolated / t["id"]
    folder.mkdir()
    (folder / "frame-000001000.jpg").write_bytes(b"image-fixture")
    result = {
        "overview": {"summary": "摘要", "points": ["重点"]},
        "chapters": [
            {
                "title": "第一章",
                "summary": "说明",
                "start": 0,
                "frames": [{"file": "frame-000001000.jpg", "time": 1}],
            }
        ],
    }
    store.update(t["id"], status="completed", result=result)
    reused = client.post("/api/tasks", json={"url": t["source"]}).json()
    assert reused["cached"] and reused["id"] == t["id"]
    text = client.get(f"/api/tasks/{t['id']}/export").text
    assert "摘要" in text and "http://127.0.0.1:8765/api/tasks/" in text
    z = zipfile.ZipFile(
        io.BytesIO(client.get(f"/api/tasks/{t['id']}/export?format=zip").content)
    )
    assert set(z.namelist()) == {"notes.md", "images/frame-000001000.jpg"}
    assert client.delete(f"/api/tasks/{t['id']}").status_code == 200
    assert not folder.exists()


@pytest.mark.parametrize(
    "code,message", [(401, "Key"), (403, "Key"), (402, "余额"), (400, "HTTP")]
)
def test_cloud_terminal_errors(code, message):
    transport = httpx.MockTransport(
        lambda req: httpx.Response(
            code, json={"error": {"message": "private-provider-detail secret"}}
        )
    )
    client = Bailian(store.DEFAULTS | {"api_key": "secret"}, transport)
    with pytest.raises(CloudError, match=message) as e:
        asyncio.run(client.analyze("test"))
    assert "secret" not in str(e.value) and "private-provider" not in str(e.value)


@pytest.mark.parametrize("mode", ["429", "503", "timeout", "network"])
def test_cloud_retries_bounded(monkeypatch, mode):
    calls = []

    def handler(req):
        calls.append(req)
        if mode == "timeout":
            raise httpx.ReadTimeout("secret")
        if mode == "network":
            raise httpx.ConnectError("secret")
        return httpx.Response(int(mode))

    async def no_sleep(_):
        pass

    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    with pytest.raises(CloudError):
        asyncio.run(
            Bailian(
                store.DEFAULTS | {"api_key": "secret"}, httpx.MockTransport(handler)
            ).analyze("test")
        )
    assert len(calls) == 4


def test_cloud_payloads(tmp_path):
    requests = []

    def handler(req):
        requests.append(json.loads(req.content))
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": '{"ok":true}'}}],
                "usage": {"prompt_tokens": 10},
            },
        )

    client = Bailian(
        store.DEFAULTS | {"api_key": "secret"}, httpx.MockTransport(handler)
    )
    img = tmp_path / "frame.jpg"
    img.write_bytes(b"frame")
    asyncio.run(client.analyze("test", [img]))
    assert requests[0]["enable_thinking"] is False
    assert requests[0]["messages"][1]["content"][1]["image_url"]["url"].startswith(
        "data:image/jpeg;base64,"
    )
    asyncio.run(client.transcribe(img))
    assert "enable_thinking" not in requests[1]
    assert requests[1]["messages"][0]["content"][0]["input_audio"]["data"].startswith(
        "data:audio/wav;base64,"
    )


def test_subtitles(tmp_path):
    vtt = tmp_path / "a.vtt"
    vtt.write_text("WEBVTT\n\n00:00:01.000 --> 00:00:04.000\n<b>hello</b>\n", "utf-8")
    assert media.parse_subtitle(vtt) == [
        dict(start=1, end=4, text="hello", source="subtitle")
    ]
    js = tmp_path / "a.json3"
    js.write_text(
        json.dumps(
            {
                "events": [
                    {"tStartMs": 500, "dDurationMs": 1000, "segs": [{"utf8": "hi"}]}
                ]
            }
        )
    )
    assert media.parse_subtitle(js)[0]["start"] == 0.5
    assert media.choose_subtitle(
        {"language": "en", "subtitles": {"en": []}, "automatic_captions": {"zh": []}}
    ) == ("en", False)


def test_real_media(tmp_path):
    async def scenario():
        p = tmp_path / "sample.mp4"
        await media.run(
            media.FFMPEG,
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=320x180:rate=2",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=16000",
            "-t",
            "32",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            p,
        )
        total = await media.duration(p)
        assert 31 <= total <= 33
        chunks = await media.split_audio(p, tmp_path, total)
        assert len(chunks) == 2 and all(end - start <= 30 for _, start, end in chunks)
        assert all(path.stat().st_size < 10 * 1024 * 1024 for path, _, _ in chunks)
        frames = await media.frames(p, tmp_path, total)
        assert 1 <= len(frames) <= 6 and all(
            (tmp_path / f["file"]).exists() for f in frames
        )

    asyncio.run(scenario())


def test_worker_resume_and_partial_overview(isolated, monkeypatch):
    store.write_json(isolated / "settings.json", store.DEFAULTS | {"api_key": "test"})
    t = create_task("local", "演示视频")
    folder = isolated / t["id"]
    folder.mkdir()
    store.write_json(
        folder / "checkpoint.json",
        {
            "meta": {"title": "演示视频", "duration": 600},
            "segments": [
                {"start": 0, "end": 600, "text": "学习操作", "source": "subtitle"}
            ],
            "frames": [],
        },
    )
    calls = []
    fail = [True]

    class Fake:
        async def analyze(self, prompt, images=()):
            calls.append(prompt)
            if "整理本章" in prompt and fail[0]:
                fail[0] = False
                assert store.get(t["id"])["result"]["overview"]
                raise CloudError("模拟限流")
            return json.dumps(
                {
                    "summary": "总结",
                    "points": ["要点"],
                    "title": "示例章节",
                    "visual_notes": [],
                    "steps": [],
                    "uncertainties": [],
                }
            ), {"prompt_tokens": 100, "completion_tokens": 10}

    monkeypatch.setattr(worker, "Bailian", lambda _: Fake())
    asyncio.run(worker.process(t["id"]))
    assert store.get(t["id"])["status"] == "paused"
    first_count = len(calls)
    asyncio.run(worker.process(t["id"]))
    result = store.get(t["id"])
    assert result["status"] == "completed" and len(result["result"]["chapters"]) == 2
    assert (
        len(calls) - first_count == 3
    )  # two chapters + final; successful text calls cached
    assert result["result"]["usage"]["input_tokens"] == 500


def test_worker_cancel(isolated, monkeypatch):
    async def scenario():
        store.write_json(
            isolated / "settings.json", store.DEFAULTS | {"api_key": "test"}
        )
        t = create_task("https://youtu.be/test", "test")
        started = asyncio.Event()

        async def wait(*args):
            started.set()
            await asyncio.sleep(100)

        monkeypatch.setattr(media, "info", wait)
        running = asyncio.create_task(worker.process(t["id"]))
        await started.wait()
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running
        assert store.get(t["id"])["status"] == "cancelled"

    asyncio.run(scenario())


def test_worker_startup_resume(isolated, monkeypatch):
    async def scenario():
        t = create_task("local", "recover")
        store.update(t["id"], status="running")
        seen = asyncio.Event()

        async def process(task_id):
            assert store.get(task_id)["status"] == "queued"
            store.update(task_id, status="completed")
            seen.set()

        monkeypatch.setattr(worker, "process", process)
        runner = asyncio.create_task(worker.loop())
        await asyncio.wait_for(seen.wait(), 2)
        runner.cancel()
        with pytest.raises(asyncio.CancelledError):
            await runner

    asyncio.run(scenario())


def test_balance_error_403():
    transport = httpx.MockTransport(
        lambda req: httpx.Response(403, json={"code": "Arrearage"})
    )
    with pytest.raises(CloudError, match="余额"):
        asyncio.run(
            Bailian(store.DEFAULTS | {"api_key": "secret"}, transport).analyze("test")
        )


def test_video_only_urls_and_timestamp():
    assert not media.valid_url("https://www.youtube.com/playlist?list=123")
    assert not media.valid_url("https://www.bilibili.com/")
    assert media.valid_url("https://www.bilibili.com/video/BV1xx411c7mD?p=2")
    assert media.watch_url(
        "https://www.bilibili.com/video/BV1xx411c7mD?p=2&t=50#reply", 10
    ).endswith("?p=2&t=10")


def test_corrupted_checkpoint_does_not_stop_queue(isolated, monkeypatch):
    async def scenario():
        bad = create_task("local", "bad")
        folder = isolated / bad["id"]
        folder.mkdir()
        (folder / "checkpoint.json").write_text("{broken")
        create_task("local", "good")
        original = worker.process
        seen = asyncio.Event()

        async def process(task_id):
            if task_id == bad["id"]:
                return await original(task_id)
            store.update(task_id, status="completed")
            seen.set()

        monkeypatch.setattr(worker, "process", process)
        runner = asyncio.create_task(worker.loop())
        await asyncio.wait_for(seen.wait(), 2)
        assert store.get(bad["id"])["status"] == "paused"
        runner.cancel()
        with pytest.raises(asyncio.CancelledError):
            await runner

    asyncio.run(scenario())


def test_silent_video_pipeline(isolated, monkeypatch):
    class Fake:
        async def analyze(self, prompt, images=()):
            assert "根据这些带时间戳的素材提炼笔记" not in prompt
            if images:
                return '{"observations":[]}', {}
            return '{"title":"画面笔记","summary":"仅根据画面","points":[]}', {}

        async def transcribe(self, path):
            pytest.fail("silent video must not call ASR")

    monkeypatch.setattr(worker, "Bailian", lambda _: Fake())

    async def scenario():
        store.write_json(
            isolated / "settings.json", store.DEFAULTS | {"api_key": "test"}
        )
        task = create_task("local", "silent")
        folder = isolated / task["id"]
        folder.mkdir()
        await media.run(
            media.FFMPEG,
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=blue:s=320x180:r=1",
            "-t",
            "3",
            folder / "upload.mp4",
        )
        assert not await media.has_audio(folder / "upload.mp4")
        await worker.process(task["id"])
        result = store.get(task["id"])
        assert (
            result["status"] == "completed"
            and result["result"]["timing"] == "画面采样时间"
        )

    asyncio.run(scenario())
