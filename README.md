# Video
小工具

## 拾帧 · 视频笔记

Windows 本机网页，通过阿里云百炼 API 把 B站 / YouTube 视频整理成中文笔记。先给文字速览，再补全 PPT、图表和操作画面相关的章节说明。

### 启动

需要 Python 3.12 和 Node.js 20.14 或以上。在项目目录双击 **`start.bat`**，或运行：

```powershell
.\start.ps1
```

首次启动会安装 Python 环境、前端依赖及独立 Deno，并构建网页。FFmpeg 随 `imageio-ffmpeg` 安装，无需修改系统 PATH。打开 **http://127.0.0.1:8765**，关闭终端或按 Ctrl+C 停止服务。后续启动会检查依赖并重新构建前端。

### 配置百炼

1. 在 [阿里云百炼控制台](https://bailian.console.aliyun.com/) 开通模型服务、创建 API Key，并确保账户有可用额度。
2. 点击网页的“模型与设置”。默认地域为北京，文字和视觉模型为 `qwen3.5-flash`，语音模型为 `qwen3-asr-flash`。
3. 在本机设置页填写 Key，保存后测试连接。测试会进行一次真实文字请求并产生少量用量；图片和语音能力在视频任务中验证。
4. 粘贴视频链接，或导入本地 MP4、MKV、WebM、MOV、AVI、M4V 文件。无需把 Key 发给开发者。

北京与新加坡的 Key 不通用。默认使用百炼兼容接口公共域名；[官方文档](https://help.aliyun.com/zh/model-studio/qwen-asr-api-reference)说明现有域名仍可正常使用。密钥只存在被 Git 忽略的 `data/settings.json`，设置读取接口仅返回是否配置，不返回完整 Key。

### 使用方式

- 默认单条视频，支持指定 B站分P，例如 `?p=2`。YouTube 播放列表里的视频只处理该条。第一版不处理直播或批量播放列表。
- 优先取原语言人工字幕，其次自动字幕；没有字幕时按静音切成最多 30 秒音频，由云端转写。音频时间戳是片段边界，不是逐句对齐。
- 每分钟最多选 6 张关键帧，结合场景变化、定时覆盖和相邻去重；每 5 分钟整理为一章。短暂操作可能漏采，看不清的信息列入“待确认”。
- 文字速览先显示，画面和章节逐步补全。出现错误时保留已有结果；“继续处理”会复用已完成片段。关闭程序后，正在运行的任务在下次启动时恢复。
- 相同完整链接的已完成任务直接复用；已运行或排队任务不会重复创建。配置新模型后，原任务检查点仍保留旧结果；想全部重新分析可先删除旧任务。
- Markdown 可以复制或下载；单独 Markdown 的图片链接依赖本机服务。需要可携带的图片时下载“笔记与图片”ZIP，解压后打开其中 `notes.md`。
- 下载受登录或站点限制时，可在设置中主动导入 Netscape 格式 `cookies.txt`，或改用本地视频。程序不会自动读取浏览器凭据。

适合 10–60 分钟视频；上限 4 小时，本地上传最大 2 GB。视频处理在本机完成，文字、关键帧以及无字幕时的音频会发送至百炼。媒体成功处理后删除原视频和临时音频，保留笔记、关键帧和恢复记录；取消或失败时保留素材便于续跑。删除笔记会一并清理该任务文件。

### 费用

默认北京 `qwen3.5-flash` 价格核对日为 2026-09-12：单次输入不超过 128K tokens 时，输入 0.2 元 / 百万 tokens，输出 2 元 / 百万 tokens；更长输入使用对应阶梯价格。参见[官方模型说明与价格](https://help.aliyun.com/zh/model-studio/qwen3-5-flash)。

任务记录模型返回的 token 用量，并估算文字/图片请求价格。语音按秒显示；可在设置中填写账户对应的语音单价，否则明确提示语音费用未计入。切换其他模型或新加坡地域时，不套用默认价格，提示存在未估算费用。重试、取消后已经发出的请求、无响应请求及连接测试可能产生额外费用，最终以百炼账单为准。

### 开发与测试

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
npm.cmd ci
npm.cmd run build
.\.venv\Scripts\python.exe -m uvicorn backend.app:app --host 127.0.0.1 --port 8765
```

前端开发可另开终端执行 `npm.cmd run dev`，Vite 会把 `/api` 转发给后端。Windows 后端不要加 `--reload`，使用单进程以支持异步 FFmpeg 子进程和单任务队列。

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m scripts.probe_sources
.\.venv\Scripts\python.exe -m scripts.benchmark
```

`probe_sources` 只探测公开视频元数据，不调用模型。`benchmark` 生成 10 / 60 分钟合成视频，使用真实 FFmpeg 和模拟云端响应，写入独立的 `data/benchmark/`；不使用你的 Key、不产生真实 API 费用。其耗时不代表真实模型延迟或准确率。完整结果见 `docs/validation.md`。

### 结构与接口

- `src/`：React + TypeScript 网页。
- `backend/`：FastAPI、SQLite、视频处理队列和百炼适配器；默认单任务、全局最多 2 个模型请求并发。
- `data/`：本机配置、SQLite、检查点和媒体缓存，全部被 Git 忽略。
- `AGENTS.md`、`docs/agents/`：GitHub Issues 和单项目领域文档约定。

接口包括 `GET/PUT /api/settings`、`POST /api/settings/test`、`POST/DELETE /api/cookies`、`GET/POST /api/tasks`、`POST /api/tasks/upload`、`GET/DELETE /api/tasks/{id}`、`POST /api/tasks/{id}/cancel`、`POST /api/tasks/{id}/retry`、`GET /api/tasks/{id}/export?format=md|zip`。交互式接口文档位于 http://127.0.0.1:8765/docs 。服务仅监听回环地址并拒绝外站 Origin，不提供公网部署或多人账户功能。
