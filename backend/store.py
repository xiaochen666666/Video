import json
import os
import sqlite3
import threading
from pathlib import Path

ROOT = Path(
    os.environ.get("VIDEO_DATA_DIR", Path(__file__).resolve().parents[1] / "data")
)
ROOT.mkdir(parents=True, exist_ok=True)
LOCK = threading.RLock()
DEFAULTS = dict(
    provider="bailian",
    deepseek_model="deepseek-flash",
    deepseek_api_key="",
    custom_base_url="",
    custom_model="",
    custom_vision_model="",
    custom_api_key="",
    custom_json_mode=True,
    asr_provider="bailian",
    custom_asr_base_url="",
    custom_asr_model="whisper-1",
    custom_asr_api_key="",
    region="beijing",
    model="qwen3.5-flash",
    asr_model="qwen3-asr-flash",
    api_key="",
    input_price=0.2,
    output_price=2.0,
    asr_price_per_second=None,
    price_date="2026-09-12",
)


def connect():
    db = sqlite3.connect(ROOT / "tasks.db")
    db.row_factory = sqlite3.Row
    return db


with connect() as db:
    db.execute(
        "CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY, source TEXT, title TEXT, status TEXT, stage TEXT, error TEXT, created TEXT, result TEXT)"
    )


def settings():
    path = ROOT / "settings.json"
    return DEFAULTS | (json.loads(path.read_text("utf-8")) if path.exists() else {})


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2), "utf-8")
    tmp.replace(path)


def tasks():
    with connect() as db:
        rows = db.execute("SELECT * FROM tasks ORDER BY created DESC").fetchall()
    return [decode(r) for r in rows]


def decode(row):
    if not row:
        return None
    item = dict(row)
    item["result"] = json.loads(item["result"] or "{}")
    return item


def get(task_id):
    with connect() as db:
        return decode(
            db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        )


def update(task_id, **fields):
    if "result" in fields:
        fields["result"] = json.dumps(fields["result"], ensure_ascii=False)
    assert set(fields) <= {"title", "status", "stage", "error", "result"}
    with LOCK, connect() as db:
        db.execute(
            "UPDATE tasks SET " + ",".join(f"{k}=?" for k in fields) + " WHERE id=?",
            [*fields.values(), task_id],
        )
