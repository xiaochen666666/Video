import asyncio
import json
import httpx
import pytest
from fastapi.testclient import TestClient
from backend import store, worker
from backend.app import app, create_task
from backend.cloud import DeepSeek, Compatible, CloudError, normalize_base


def response(text='{"summary":"ok","points":[]}'):
    return httpx.Response(
        200,
        json={
            "choices": [{"message": {"content": text}}],
            "usage": {"prompt_tokens": 2, "completion_tokens": 3},
        },
    )


@pytest.mark.parametrize("provider", ["deepseek", "custom"])
def test_text_and_vision_routing(provider, tmp_path):
    calls = []

    def handler(req):
        calls.append(req)
        return response()

    config = store.DEFAULTS | {
        "provider": provider,
        "api_key": "bailian-only",
        "deepseek_api_key": "deepseek-only",
        "custom_api_key": "custom-only",
        "custom_base_url": "https://example.org/v1/",
        "custom_model": "text-model",
        "custom_vision_model": "vision-model",
    }
    client = (DeepSeek if provider == "deepseek" else Compatible)(
        config, httpx.MockTransport(handler)
    )
    img = tmp_path / "image.jpg"
    img.write_bytes(b"image")
    asyncio.run(client.analyze("test"))
    asyncio.run(client.analyze("test", [img]))
    first, second = [json.loads(r.content) for r in calls]
    assert calls[0].headers["authorization"] == f"Bearer {provider}-only"
    assert isinstance(first["messages"][1]["content"], str)
    assert second["messages"][1]["content"][1]["type"] == "image_url"
    assert "enable_thinking" not in first
    if provider == "deepseek":
        assert str(calls[0].url) == "https://api.deepseek.com/chat/completions"
        assert (
            first["thinking"] == {"type": "disabled"}
            and first["model"] == "deepseek-flash"
        )
    else:
        assert str(calls[0].url) == "https://example.org/v1/chat/completions"
        assert first["model"] == "text-model" and second["model"] == "vision-model"
        assert "thinking" not in first


def test_settings_secret_isolation_and_endpoint_change(isolated):
    client = TestClient(app)
    values = {
        "provider": "deepseek",
        "api_key": "bailian-only",
        "deepseek_api_key": "deepseek-only",
        "custom_api_key": "custom-only",
        "custom_asr_api_key": "asr-only",
        "custom_base_url": "https://one.example/v1",
        "custom_asr_base_url": "https://speech.example/v1",
    }
    r = client.put("/api/settings", json=values)
    assert r.status_code == 200
    assert all(
        value not in r.text
        for value in ("bailian-only", "deepseek-only", "custom-only", "asr-only")
    )
    r = client.put(
        "/api/settings", json={"provider": "custom", "custom_model": "model"}
    )
    assert r.json()["has_key"]
    r = client.put(
        "/api/settings",
        json={"custom_base_url": "https://two.example/v1/chat/completions"},
    )
    assert not r.json()["has_key"]
    c = store.settings()
    assert c["custom_base_url"] == "https://two.example/v1" and not c["custom_api_key"]
    assert c["deepseek_api_key"] == "deepseek-only" and c["api_key"] == "bailian-only"
    assert (
        client.put(
            "/api/settings",
            json={"custom_base_url": "https://user:secret@evil.example"},
        ).status_code
        == 400
    )
    assert "secret" not in client.get("/api/settings").text


@pytest.mark.parametrize("provider", ["bailian", "custom", "none"])
def test_independent_speech_routing(provider, tmp_path):
    calls = []

    def handler(req):
        calls.append(req)
        return (
            httpx.Response(200, json={"text": "转写"})
            if provider == "custom"
            else response("转写")
        )

    c = store.DEFAULTS | {
        "provider": "deepseek",
        "deepseek_api_key": "deepseek-only",
        "api_key": "bailian-only",
        "asr_provider": provider,
        "custom_asr_base_url": "https://speech.example/v1",
        "custom_asr_api_key": "asr-only",
    }
    p = tmp_path / "audio.wav"
    p.write_bytes(b"wave")
    cloud = DeepSeek(c, httpx.MockTransport(handler))
    if provider == "none":
        with pytest.raises(CloudError, match="字幕"):
            asyncio.run(cloud.transcribe(p))
        assert not calls
    else:
        assert asyncio.run(cloud.transcribe(p))[0] == "转写"
        assert "deepseek-only" not in calls[0].headers["authorization"]
        if provider == "custom":
            assert str(calls[0].url) == "https://speech.example/v1/audio/transcriptions"
            assert calls[0].headers["content-type"].startswith("multipart/form-data")
            assert b"wave" in calls[0].content
        else:
            assert calls[0].url.host == "dashscope.aliyuncs.com"


def test_redirect_does_not_forward_key():
    calls = []

    def handler(req):
        calls.append(req)
        return httpx.Response(307, headers={"Location": "https://another.example"})

    c = store.DEFAULTS | {"deepseek_api_key": "secret"}
    with pytest.raises(CloudError, match="重定向"):
        asyncio.run(DeepSeek(c, httpx.MockTransport(handler)).analyze("test"))
    assert len(calls) == 1


def test_custom_json_mode_can_be_disabled():
    def handler(req):
        assert "response_format" not in json.loads(req.content)
        return response('```json\n{"summary":"ok","points":[]}\n```')

    c = store.DEFAULTS | {
        "custom_api_key": "secret",
        "custom_base_url": "https://example.org/v1",
        "custom_model": "text",
        "custom_json_mode": False,
    }
    text, _ = asyncio.run(Compatible(c, httpx.MockTransport(handler)).analyze("test"))
    assert worker.parse_json(text)["summary"] == "ok"


def test_subtitle_import_invalidates_notes_retains_usage(isolated):
    client = TestClient(app)
    t = create_task("local", "example")
    folder = isolated / t["id"]
    folder.mkdir()
    store.update(
        t["id"],
        status="paused",
        result={"usage": {"input_tokens": 10}, "overview": {"summary": "old"}},
    )
    store.write_json(
        folder / "checkpoint.json",
        {
            "meta": {"duration": 60},
            "frames": [],
            "text-0": {"summary": "old"},
            "asr-0": "old",
        },
    )
    srt = "1\n00:00:01,000 --> 00:00:03,500\n新的字幕\n\n"
    r = client.post(
        f"/api/tasks/{t['id']}/subtitle",
        files={"file": ("captions.srt", srt.encode(), "text/plain")},
    )
    assert r.status_code == 200
    cp = worker.checkpoint(folder)
    assert cp["segments"][0]["end"] == 3.5 and cp["segments"][0]["text"] == "新的字幕"
    assert "text-0" not in cp and "asr-0" not in cp
    assert r.json()["result"] == {"usage": {"input_tokens": 10}}
    store.update(t["id"], status="running")
    assert (
        client.post(
            f"/api/tasks/{t['id']}/subtitle",
            files={"file": ("captions.srt", srt.encode())},
        ).status_code
        == 409
    )


@pytest.mark.parametrize(
    "bad",
    [
        "https://host/v1?key=secret",
        "file:///test",
        "http://public.example/v1",
        "https://user:pw@host/v1",
    ],
)
def test_base_url_validation(bad):
    with pytest.raises(ValueError):
        normalize_base(bad)


def test_vision_connection_test_cleans_image(isolated, monkeypatch):
    import importlib

    module = importlib.import_module("backend.app")
    seen = []

    class Fake:
        async def analyze(self, prompt, images):
            seen.extend(images)
            assert images[0].exists()
            return "{}", {}

    monkeypatch.setattr(module, "get_client", lambda _: Fake())
    r = TestClient(app).post("/api/settings/test?kind=vision")
    assert r.status_code == 200 and seen and not seen[0].exists()
