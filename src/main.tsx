import React, { useEffect, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import {
  ArrowRight,
  ArrowUpRight,
  BookOpen,
  Check,
  ChevronRight,
  CirclePlay,
  Clock3,
  Copy,
  Download,
  FileVideo,
  Film,
  Layers3,
  Loader2,
  Plus,
  Search,
  Settings2,
  Sparkles,
  Trash2,
  Upload,
  X,
} from "lucide-react";
import "./style.css";

type Frame = { time: number; file: string };
type Chapter = {
  title: string;
  summary: string;
  start: number;
  end: number;
  watch_url: string;
  points: string[];
  visual_notes: string[];
  steps: string[];
  uncertainties: string[];
  frames: Frame[];
};
type Task = {
  id: string;
  title: string;
  source: string;
  status: string;
  stage: string;
  error: string;
  created: string;
  result: {
    overview?: { summary: string; points: string[] };
    chapters?: Chapter[];
    meta?: { duration: number };
    timing?: string;
    usage?: {
      input_tokens: number;
      output_tokens: number;
      audio_seconds: number;
      estimated_cny: number;
      asr_cost_unknown?: boolean;
      model_cost_unknown?: boolean;
    };
  };
};
type Config = {
  region: string;
  model: string;
  asr_model: string;
  has_key: boolean;
  asr_price_per_second: number | null;
};
async function api<T>(path: string, options: RequestInit = {}): Promise<T> {
  const r = await fetch("/api" + path, options);
  if (!r.ok) {
    const e = await r.json().catch(() => ({ detail: "请求失败" }));
    throw new Error(typeof e.detail === "string" ? e.detail : "请检查输入内容");
  }
  return r.json();
}
const json = (body: unknown) => ({
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify(body),
});
const time = (n: number) =>
  `${Math.floor(n / 60)
    .toString()
    .padStart(2, "0")}:${Math.floor(n % 60)
    .toString()
    .padStart(2, "0")}`;
const statusNames: Record<string, string> = {
  queued: "等待中",
  running: "整理中",
  completed: "已完成",
  paused: "已暂停",
  cancelled: "已取消",
};

function App() {
  const dialogRef = useRef<HTMLElement>(null);
  const [tasks, setTasks] = useState<Task[]>([]),
    [selected, setSelected] = useState<string | null>(null),
    [url, setUrl] = useState(""),
    [query, setQuery] = useState("");
  const [settings, setSettings] = useState(false),
    [config, setConfig] = useState<Config | null>(null),
    [key, setKey] = useState("");
  const [busy, setBusy] = useState(false),
    [notice, setNotice] = useState(""),
    [error, setError] = useState("");
  const task = tasks.find((t) => t.id === selected);
  const refresh = async () => {
    try {
      setTasks(await api<Task[]>("/tasks"));
    } catch (e) {
      setError((e as Error).message);
    }
  };
  useEffect(() => {
    refresh();
    api<Config>("/settings")
      .then(setConfig)
      .catch((e) => setError(e.message));
    const t = setInterval(refresh, 2000);
    return () => clearInterval(t);
  }, []);
  useEffect(() => {
    if (notice) {
      const t = setTimeout(() => setNotice(""), 4500);
      return () => clearTimeout(t);
    }
  }, [notice]);
  useEffect(() => {
    if (!settings) return;
    const previous = document.activeElement as HTMLElement | null;
    dialogRef.current?.querySelector<HTMLElement>("button")?.focus();
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") setSettings(false);
      if (event.key !== "Tab") return;
      const controls = Array.from(
        dialogRef.current?.querySelectorAll<HTMLElement>(
          "button:not(:disabled), input:not([type=file]), select",
        ) || [],
      );
      const first = controls[0],
        last = controls[controls.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last?.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first?.focus();
      }
    };
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("keydown", onKey);
      previous?.focus();
    };
  }, [settings]);
  async function action(fn: () => Promise<void>) {
    setBusy(true);
    setError("");
    try {
      await fn();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  async function submit(e: React.FormEvent) {
    e.preventDefault();
    await action(async () => {
      const t = await api<Task>("/tasks", { method: "POST", ...json({ url }) });
      setSelected(t.id);
      setUrl("");
      await refresh();
    });
  }
  async function upload(file: File, cookies = false) {
    await action(async () => {
      const data = new FormData();
      data.append("file", file);
      const r = await api<Task>(cookies ? "/cookies" : "/tasks/upload", {
        method: "POST",
        body: data,
      });
      if (!cookies) {
        setSelected(r.id);
        await refresh();
      }
      setNotice(cookies ? "Cookies 已保存在本机" : "视频已加入处理队列");
    });
  }
  async function operation(op: string) {
    if (!task) return;
    await action(async () => {
      await api(`/tasks/${task.id}${op === "delete" ? "" : "/" + op}`, {
        method: op === "delete" ? "DELETE" : "POST",
      });
      if (op === "delete") setSelected(null);
      await refresh();
    });
  }
  async function save() {
    if (!config) return;
    await action(async () => {
      const c = await api<Config>("/settings", {
        method: "PUT",
        ...json({ ...config, ...(key ? { api_key: key } : {}) }),
      });
      setConfig(c);
      setKey("");
      setNotice("设置已保存");
    });
  }
  return (
    <div className="shell">
      <aside className="sidebar">
        <a
          className="brand"
          href="#"
          onClick={(e) => {
            e.preventDefault();
            setSelected(null);
          }}
        >
          <span className="brand-icon">
            <Layers3 size={23} />
          </span>
          <span>
            拾帧<small>FRAME NOTES</small>
          </span>
        </a>
        <button className="new-button" onClick={() => setSelected(null)}>
          <Plus size={18} /> 新建视频笔记 <span>＋</span>
        </button>
        <div className="nav-caption">
          你的知识库 <span>{tasks.length}</span>
        </div>
        <div className="search">
          <Search size={15} />
          <input
            aria-label="搜索笔记"
            placeholder="搜索笔记…"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
          />
        </div>
        <div className="history">
          {tasks
            .filter((t) => t.title.toLowerCase().includes(query.toLowerCase()))
            .map((t) => (
              <button
                key={t.id}
                className={
                  "history-item " + (selected === t.id ? "active" : "")
                }
                onClick={() => setSelected(t.id)}
              >
                <span className={"history-icon " + t.status}>
                  <FileVideo size={17} />
                </span>
                <span>
                  <strong>{t.title}</strong>
                  <small>
                    {new Date(t.created).toLocaleDateString("zh-CN", {
                      month: "short",
                      day: "numeric",
                    })}{" "}
                    · {statusNames[t.status]}
                  </small>
                </span>
              </button>
            ))}
          {!tasks.length && (
            <div className="empty-history">
              <BookOpen size={23} />
              <p>好内容，值得留下来</p>
              <small>你的第一份笔记将出现在这里</small>
            </div>
          )}
        </div>
        <div className="sidebar-bottom">
          <div className="local-label">
            <span /> 本机工作空间 <small>LOCAL</small>
          </div>
          <button onClick={() => setSettings(true)}>
            <Settings2 size={17} /> 模型与设置 <ChevronRight size={15} />
          </button>
        </div>
      </aside>
      <main>
        <header>
          <div>
            <span>工作空间</span>
            <ChevronRight size={14} />
            <strong>{task ? "视频笔记" : "新建笔记"}</strong>
          </div>
          <button className="connection" onClick={() => setSettings(true)}>
            <span className={config?.has_key ? "dot" : "dot offline"} />
            {config?.has_key ? "百炼 API 已配置" : "配置云端模型"}
            <ArrowUpRight size={14} />
          </button>
        </header>
        {error && (
          <div role="alert" className="error-banner">
            {error}
            <button aria-label="关闭错误" onClick={() => setError("")}>
              <X size={16} />
            </button>
          </div>
        )}
        {!task ? (
          <div className="landing">
            <div className="eyebrow">
              <Sparkles size={14} /> 少一点观看，多一点收获
            </div>
            <h1>
              把长视频，
              <br />
              <em>变成你的知识。</em>
            </h1>
            <p className="intro">
              粘贴一个链接，留下真正重要的内容。
              <br />
              从讲解到 PPT、图表和操作画面，一起整理成清晰的笔记。
            </p>
            <form className="input-card" onSubmit={submit}>
              <label htmlFor="video-url">从一个视频链接开始</label>
              <div className="url-row">
                <CirclePlay size={23} />
                <input
                  id="video-url"
                  type="url"
                  required
                  placeholder="粘贴 B站或 YouTube 视频链接…"
                  value={url}
                  onChange={(e) => setUrl(e.target.value)}
                />
                <button className="primary" disabled={busy || !url.trim()}>
                  {busy ? (
                    <Loader2 className="spin" size={17} />
                  ) : (
                    <>
                      生成笔记 <ArrowRight size={17} />
                    </>
                  )}
                </button>
              </div>
              <div className="input-footer">
                <span>
                  <span className="platform bili">bilibili</span>
                  <span className="platform youtube">▶ YouTube</span>
                  <span className="recommended">适合 10–60 分钟视频</span>
                </span>
                <label className="upload-label">
                  <Upload size={14} /> 导入本地视频
                  <input
                    type="file"
                    accept=".mp4,.mkv,.webm,.mov,.avi,.m4v"
                    disabled={busy}
                    onChange={(e) => {
                      if (e.target.files?.[0]) upload(e.target.files[0]);
                      e.target.value = "";
                    }}
                  />
                </label>
              </div>
            </form>
            {!config?.has_key && (
              <button className="setup-hint" onClick={() => setSettings(true)}>
                <span>
                  <Settings2 size={15} /> 首次使用？配置百炼 API Key 即可开始
                </span>
                <ArrowRight size={15} />
              </button>
            )}
            <div className="section-heading">
              <span>不止是摘要，是可以回看的理解</span>
              <span>MADE FOR LEARNING</span>
            </div>
            <div className="features">
              <article>
                <div className="feature-icon">
                  <Sparkles size={20} />
                </div>
                <small>01 / 先抓重点</small>
                <h3>先读速览，再深入</h3>
                <p>
                  一句话结论与核心要点先到，
                  <br />
                  章节详解随后逐步补全。
                </p>
                <div className="mini-lines">
                  <i />
                  <i />
                  <i />
                </div>
              </article>
              <article>
                <div className="feature-icon blue">
                  <Film size={20} />
                </div>
                <small>02 / 看懂画面</small>
                <h3>让画面也成为笔记</h3>
                <p>
                  提取关键帧，理解 PPT 和图表，
                  <br />
                  留下操作过程中的重要步骤。
                </p>
                <div className="mini-chart">
                  <i />
                  <i />
                  <i />
                  <i />
                  <i />
                  <i />
                </div>
              </article>
              <article>
                <div className="feature-icon gold">
                  <Clock3 size={20} />
                </div>
                <small>03 / 随时回看</small>
                <h3>每个重点，都有出处</h3>
                <p>
                  沿时间戳回到原视频，
                  <br />
                  也能导出笔记，继续积累。
                </p>
                <div className="mini-timeline">
                  <span>02:14</span>
                  <i />
                  <span>08:36</span>
                </div>
              </article>
            </div>
            <footer>
              <span className="tiny-dot" /> 视频在本机处理 ·
              字幕与关键素材通过云端 API 分析
            </footer>
          </div>
        ) : (
          <div className="result-page">
            <div className="result-topline">
              <span className={"status " + task.status}>
                {task.status === "running" ? (
                  <Loader2 className="spin" size={14} />
                ) : (
                  <Check size={14} />
                )}{" "}
                {statusNames[task.status]}
              </span>
              <span>
                {task.result.meta?.duration
                  ? `${Math.ceil(task.result.meta.duration / 60)} 分钟`
                  : "视频笔记"}{" "}
                · {task.result.timing || "正在读取素材"}
              </span>
            </div>
            <h1>{task.title}</h1>
            <div className="result-toolbar">
              <p>{task.stage}</p>
              <div>
                {["running", "queued"].includes(task.status) && (
                  <button disabled={busy} onClick={() => operation("cancel")}>
                    取消
                  </button>
                )}
                {["paused", "cancelled"].includes(task.status) && (
                  <button disabled={busy} onClick={() => operation("retry")}>
                    继续处理
                  </button>
                )}
                <button
                  aria-label="删除笔记"
                  disabled={busy}
                  onClick={() => {
                    if (confirm("删除这份笔记及本地素材？"))
                      operation("delete");
                  }}
                >
                  <Trash2 size={16} />
                </button>
              </div>
            </div>
            {task.error && (
              <div className="task-error">
                {task.error}
                <button onClick={() => setSettings(true)}>
                  检查设置 <ArrowUpRight size={14} />
                </button>
              </div>
            )}
            {task.result.overview ? (
              <>
                <section className="overview">
                  <div className="overview-label">
                    <Sparkles size={17} />{" "}
                    {task.status === "completed"
                      ? "视频速览"
                      : "文字速览 · 画面内容仍在补充"}
                  </div>
                  <h2>{task.result.overview.summary}</h2>
                  <ul>
                    {task.result.overview.points?.map((p, i) => (
                      <li key={i}>{p}</li>
                    ))}
                  </ul>
                  <div className="export-row">
                    <button
                      onClick={() =>
                        action(async () => {
                          await navigator.clipboard.writeText(
                            await (
                              await fetch(`/api/tasks/${task.id}/export`)
                            ).text(),
                          );
                          setNotice("Markdown 已复制");
                        })
                      }
                    >
                      <Copy size={15} /> 复制笔记
                    </button>
                    <a href={`/api/tasks/${task.id}/export`}>
                      <Download size={15} /> Markdown
                    </a>
                    <a href={`/api/tasks/${task.id}/export?format=zip`}>
                      <Download size={15} /> 笔记与图片
                    </a>
                  </div>
                </section>
                <div className="chapter-heading">
                  <h2>章节详解</h2>
                  <span>{task.result.chapters?.length || 0} 个章节已整理</span>
                </div>
                {task.result.chapters?.map((c, i) => (
                  <section className="chapter" key={i}>
                    <div className="chapter-title">
                      <span>{String(i + 1).padStart(2, "0")}</span>
                      <h3>{c.title}</h3>
                      {c.watch_url ? (
                        <a href={c.watch_url} target="_blank" rel="noreferrer">
                          {time(c.start)} <ArrowUpRight size={13} />
                        </a>
                      ) : (
                        <small>{time(c.start)}</small>
                      )}
                    </div>
                    <p>{c.summary}</p>
                    {(
                      [
                        ["points", "内容要点"],
                        ["visual_notes", "画面补充"],
                        ["steps", "操作步骤"],
                        ["uncertainties", "待确认"],
                      ] as const
                    ).map(
                      ([k, label]) =>
                        c[k]?.length > 0 && (
                          <div className="chapter-detail" key={k}>
                            <h4>{label}</h4>
                            <ul>
                              {c[k].map((p, j) => (
                                <li key={j}>{p}</li>
                              ))}
                            </ul>
                          </div>
                        ),
                    )}
                    <div className="frames">
                      {c.frames?.map((f) => (
                        <a
                          key={f.file}
                          href={`/api/tasks/${task.id}/frames/${f.file}`}
                          target="_blank"
                          rel="noreferrer"
                        >
                          <img
                            loading="lazy"
                            src={`/api/tasks/${task.id}/frames/${f.file}`}
                            alt={`${time(f.time)} 关键画面`}
                          />
                          <span>{time(f.time)}</span>
                        </a>
                      ))}
                    </div>
                  </section>
                ))}
              </>
            ) : (
              <div className="processing">
                <div className="processing-icon">
                  {["paused", "cancelled"].includes(task.status) ? (
                    <FileVideo size={30} />
                  ) : (
                    <Loader2 className="spin" size={30} />
                  )}
                </div>
                <h2>
                  {task.status === "paused"
                    ? "准备好后，继续整理"
                    : "正在把视频变成笔记"}
                </h2>
                <p>
                  {task.stage}
                  {["running", "queued"].includes(task.status)
                    ? " · 可以切换页面，任务会在后台继续"
                    : " · 已完成内容会保留，点击继续处理可恢复"}
                </p>
              </div>
            )}
            {task.result.usage && (
              <div className="usage">
                API 用量 {task.result.usage.input_tokens.toLocaleString()} 输入
                / {task.result.usage.output_tokens.toLocaleString()} 输出 tokens
                · 估算 ¥{task.result.usage.estimated_cny.toFixed(4)}
                {(task.result.usage.asr_cost_unknown ||
                  task.result.usage.model_cost_unknown) &&
                  "（部分费用未计入）"}
                <small>
                  语音 {Math.round(task.result.usage.audio_seconds)} 秒 ·
                  估算供参考，以百炼账单为准
                </small>
              </div>
            )}
          </div>
        )}
      </main>
      {notice && (
        <div role="status" className="toast">
          <Check size={17} />
          {notice}
        </div>
      )}
      {settings && config && (
        <div className="modal-backdrop" onClick={() => setSettings(false)}>
          <section
            className="modal"
            ref={dialogRef}
            role="dialog"
            aria-modal="true"
            aria-label="模型与设置"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="modal-heading">
              <div>
                <span className="eyebrow">MODEL CONNECTION</span>
                <h2>模型与设置</h2>
              </div>
              <button aria-label="关闭设置" onClick={() => setSettings(false)}>
                <X size={20} />
              </button>
            </div>
            {error && (
              <div role="alert" className="task-error">
                {error}
              </div>
            )}
            <p>
              使用阿里云百炼分析字幕、关键画面和必要音频。密钥保存在本机后端，不会加入
              Git 仓库。
            </p>
            <label>
              服务地域
              <select
                value={config.region}
                onChange={(e) =>
                  setConfig({ ...config, region: e.target.value })
                }
              >
                <option value="beijing">中国内地 · 北京（默认）</option>
                <option value="singapore">国际 · 新加坡</option>
              </select>
            </label>
            <label>
              API Key{" "}
              <span>
                {config.has_key ? "已配置 · 留空保留现有密钥" : "尚未配置"}
              </span>
              <input
                type="password"
                autoComplete="new-password"
                placeholder="在此粘贴百炼 API Key"
                value={key}
                onChange={(e) => setKey(e.target.value)}
              />
            </label>
            <div className="form-grid">
              <label>
                文字与视觉模型
                <input
                  value={config.model}
                  onChange={(e) =>
                    setConfig({ ...config, model: e.target.value })
                  }
                />
              </label>
              <label>
                语音识别模型
                <input
                  value={config.asr_model}
                  onChange={(e) =>
                    setConfig({ ...config, asr_model: e.target.value })
                  }
                />
              </label>
            </div>
            <label>
              语音估算单价（元 / 秒，可选）
              <input
                type="number"
                min="0"
                step="0.0001"
                placeholder="留空则单独标记语音费用未计入"
                value={config.asr_price_per_second ?? ""}
                onChange={(e) =>
                  setConfig({
                    ...config,
                    asr_price_per_second:
                      e.target.value === "" ? null : Number(e.target.value),
                  })
                }
              />
            </label>
            <div className="cookies-row">
              <label className="upload-label">
                <Upload size={15} /> 导入 cookies.txt
                <input
                  type="file"
                  accept=".txt"
                  onChange={(e) => {
                    if (e.target.files?.[0]) upload(e.target.files[0], true);
                    e.target.value = "";
                  }}
                />
              </label>
              <button
                onClick={() =>
                  action(async () => {
                    await api("/cookies", { method: "DELETE" });
                    setNotice("Cookies 已清除");
                  })
                }
              >
                清除 cookies
              </button>
            </div>
            <small className="model-note">
              默认模型价格核对于 2026-09-12。连接测试会产生少量 API
              用量；请先保存设置。
            </small>
            <div className="modal-actions">
              <button
                disabled={busy || !config.has_key}
                onClick={() =>
                  action(async () => {
                    const r = await api<{ message: string }>("/settings/test", {
                      method: "POST",
                    });
                    setNotice(r.message);
                  })
                }
              >
                测试连接
              </button>
              <button className="primary" disabled={busy} onClick={save}>
                {busy ? (
                  <Loader2 className="spin" size={16} />
                ) : (
                  <Check size={16} />
                )}{" "}
                保存设置
              </button>
            </div>
          </section>
        </div>
      )}
    </div>
  );
}
createRoot(document.getElementById("root")!).render(<App />);
