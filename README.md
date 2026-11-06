# Video Toolkit（VTK）

Windows 本地桌面视频处理工具，定位介于 VLC / HandBrake / 格式工厂之间。
主打「能压、能转、能剪、能批量」，同时带一个不挡画面的播放预览器。

作者：Fred Zhao ｜ 许可：自用为主，仓库当前为公开示例

---

## 功能一览

### 压缩 / 转换
- 内置主流平台与设备预设（标注为公开资料参考值，可编辑 / 导入导出）：
  - 平台：抖音、快手、视频号、小红书、B 站、西瓜、YouTube、微信发送
  - 设备：iPhone、iPad、Android、4K 电视、投影、归档
- 质量档含「极限压缩」——**组合策略**而非单一 CRF 值；支持「压到指定体积」（按时长反算码率 + 2-pass）。
- 输出可手动设置分辨率、码率、帧率、编码、画幅策略等。

### 剪辑 / 队列
- 时间轴裁剪、片段预览。
- 队列批量处理：可把多个文件 / 多组参数排队，后台线程逐个跑，UI 不卡顿。

### 播放预览
- 外挂字幕：自动查找同名字幕，支持 SRT / VTT / ASS，可拖入字幕文件，`V` 显隐。
- **音频输出设备热切换**：系统切换扬声器 / 耳机时自动跟随重建输出，保留播放位置与速率。
- 倍速（`+` / `-`，带音调补偿开关）、逐帧、时间轴拖动。
- 播放列表：`Ctrl+O` 多选、`PgUp/PgDn` 切文件、播完自动下一个。

### 效果（字幕烧录 / 水印 / AV1 / HDR）
- 字幕烧录、图片 / 文字水印：作为任务级选项，随预设 JSON 编辑 / 导入导出；命令统一在滤镜图里拼装。
- **AV1**：运行时按 `ffmpeg -encoders` 动态探测可用编码器（CPU `libaom-av1` / 硬件 `av1_amf`），无硬件时回退 CPU。
- **HDR**：保留走 x265 10bit BT.2020 / PQ（`-x265-params hdr10=1:...`）；转 SDR 走 `zscale` + `tonemap` 色调映射。

### 媒体信息（Ctrl+4）
- ffprobe 读取：分辨率、帧率、**分轨码率**（总码率 / 视频码率 / 音频码率，MKV/WebM 无分轨字段时按「总码率 − 实测音频码率」估算并标 `≈`）。
- 打开文件后，主窗口状态栏常驻一行：`分辨率 · 帧率 · 视频码率`，悬停 tooltip 给三档完整值。

---

## 技术约束（不可动摇）

1. 所有视频处理**只通过外部 `ffmpeg.exe`**；所有媒体信息**只通过 `ffprobe.exe`**；禁止自实现编码逻辑。
2. GUI 用 **PySide6（>= 6.10）**，不用 tkinter。
3. 命令必须在 `ffmpeg_core/command_builder.py` 统一构造为 argv，界面提供预览与一键复制。
4. 耗时任务放后台线程，UI 不卡顿；所有功能记日志，异常弹友好框并写 `logs/error.log`。
5. 配置存 JSON（`config/settings.json` / `config/presets.json`），中英切换走 `language/*.json`。
6. 依赖精简：不用 `opencv-python` / `numpy` / `ffmpeg-python` / `reportlab`；`psutil`、`darkdetect` 为可选。

### 播放渲染的一个硬坑（已解决）
Qt 6.12 在 Windows 用 D3D11 / DirectComposition，打开视频后会创建 `WS_EX_LAYERED` 原生子 HWND，
盖住所有普通子控件（控制条、字幕）。本工具改用 **`QVideoSink` + `QVideoFrame.toImage()` 自绘**（`player/video_surface.py`），
规避遮挡，代价是每帧像素转换（源 24fps 实测满帧；4K / 高帧率可能吃 CPU）。

---

## 快速开始

### 1. 准备环境
- 安装 **Python 3.10+**（建议 3.14，安装时勾选「Add to PATH」/ py launcher）。
- 把 **`ffmpeg.exe` / `ffprobe.exe`** 放进项目 `ffmpeg/` 目录（或确保它们在系统 `PATH`）。
  > ffmpeg / ffprobe 不是 pip 包，需单独下载（如 https://www.gyan.dev/ffmpeg/builds/ 的 essentials 版）。
  > 查找优先级：`settings.json` 路径 → 项目 `ffmpeg/` 目录 → PATH。

### 2. 安装依赖
- **推荐**：双击 **`install.bat`** —— 自动检查 `py`、安装依赖、检查 ffmpeg、跑自检。
  - CI / 脚本模式：`cmd /c install.bat --silent`（跳过所有 pause）。
- 或手动：`py -3 -m pip install -r requirements.txt`

### 3. 启动
- 双击 **`run.bat`**（会隐藏控制台黑框，启动更快）。
- 或保留控制台调试：`py -3 main.py`。
- 自检：`py -3 main.py --selftest`（自检通过标准：rc=0 + 中英文案键一致 + `logs/error.log` 为空）。

---

## 打包为 exe（可选）

双击 **`build_exe.bat`**（会自动确保 pyinstaller 已装），产物在 `dist/VideoToolkit/`。
记得把 `ffmpeg.exe` / `ffprobe.exe` 复制到 `dist/VideoToolkit/ffmpeg/`。

体积参考（onedir 目录版）：约 95–120 MB；若只保留中英文翻译、删 `opengl32sw.dll` 可压到约 70–80 MB；
onefile 压缩后约 45–65 MB（启动略慢）。换机器想精简可只装 `PySide6-Essentials + PySide6-Addons`（跳过 WebEngine / Pdf）。

---

## 快捷键

| 快捷键 | 作用 |
|---|---|
| `Ctrl+1` ~ `Ctrl+4` | 压缩/转换、剪辑、队列、视频信息 |
| `H` | 控制条：常驻 ↔ 播放时自动隐藏 |
| `Ctrl+H` | 切换控制条悬浮 / 停靠下方 |
| `▾`（控制条右侧） | 主动收起控制条 |
| `V` | 字幕显隐 |
| `+` / `-` | 调速（覆盖小键盘与 Shift） |
| `PgUp` / `PgDn` | 播放列表切上一个 / 下一个 |
| `Ctrl+O` | 多选打开文件 |

> 说明：方向键 / 单键快捷键在输入框聚焦时会自动让位，避免调分辨率 / 码率时误跳 5 秒。

---

## 项目结构

```
VideoToolkit/
├─ main.py                 # 入口：初始化、自检、隐藏控制台、恢复上次文件
├─ run.bat / install.bat / build_exe.bat   # 启动 / 一键装依赖 / 打包
├─ requirements.txt        # 仅 PySide6>=6.10（+ 可选 psutil / darkdetect）
├─ selftest.py             # 自检脚本（8 章，含真实 ffmpeg 实跑）
├─ VideoToolkit.spec       # PyInstaller 配置
├─ config/                 # settings.json、presets.json、语言配置
├─ language/               # zh_CN.json、en_US.json
├─ ffmpeg_core/            # command_builder、ffprobe_engine、ffmpeg_engine、encoder_registry、media_options
├─ ui/                     # main_window、info_panel、playback_overlay、effects_panel、playlist_panel、theme、shortcuts
├─ player/                 # video_player、video_surface、timeline、subtitle_track、playlist
├─ modules/                # 队列 / 后台任务等
├─ resources/              # 资源（当前为空）
├─ ffmpeg/                 # 放 ffmpeg.exe / ffprobe.exe（当前为空，靠 PATH）
├─ logs/                   # error.log（应始终保持 0 字节）
├─ reports/                # 自检 / 截图产物
└─ .gitignore              # 已排除 settings.json / .workbuddy / logs / reports / ffmpeg 二进制等
```

---

## FFmpeg 9.0.2 实测参数约束（改命令构造前必看）

- 本机**无 `libsvtav1`**，CPU AV1 必须按 `ffmpeg -encoders` 动态挑（现为 `libaom-av1`）；`av1_amf` 可用，AV1 探测最小尺寸 640×360。
- `libx265` 不能带 H.264 的 `-profile:v` / `-level:v`，否则编码器打不开；HDR10 用 `-x265-params hdr10=1:...:max-cll=...`。
- `zscale` 色调映射前必须 `setparams=color_primaries=...:color_trc=...:colorspace=...`，否则 `no path between colorspaces`。
- `drawtext` 必须 `expansion=none`；文本里的单引号用 `''` 而非 `\'`。路径用 `escape_filter_path()` 转义。
- **剪辑 + 字幕**必须把 `-ss` 放输出侧（`-i src` 之后），输入侧 seek 会归零时间戳导致字幕整段错位；无字幕时仍用输入侧快速 seek。
- 软字幕编码按实际输出扩展名选 `mov_text` / `srt` / `ass` / `webvtt`，不用预设 container。

---

## 本机环境（开发用）

- `py -3` → Python 3.14 + PySide6 6.12.0；FFmpeg / ffprobe 9.0.2 在 `PATH`。
- 显卡 AMD Radeon RX 9060 XT → Auto 编码器选 `h264_amf` / `hevc_amf`；AV1 硬件 `av1_amf` 可用；nvenc / qsv 实测不可用（编入但无硬件）。
- 自检通过标准：`--selftest` rc=0 + 中英文案键一致 + `error.log` 为空。

---

## 移植 / 同步到新电脑

1. 拷贝整个项目（可排除 `__pycache__/` 、`logs/` 、`Screenshots/` 、`reports/` 、`.workbuddy/` 、旧 `settings.json`）。
2. 装 Python 3.10+。
3. 把 `ffmpeg.exe` / `ffprobe.exe` 放进 `ffmpeg/`。
4. 双击 `install.bat` → 看到「自检全部通过」。
5. 双击 `run.bat` 启动。

> 注意：`config/settings.json` 存个人状态（播放列表、上次位置、窗口布局），移植时**别拷旧的那份**，让程序自己生成新的。

---

## 常见问题

- **`run.bat` 一闪而过 / 运行不了**：bat 必须 CRLF + 纯 ASCII + 无 BOM。`run.bat --console` 可保留控制台看报错。
- **播放器不跟随系统声音输出切换**：已支持热切换；如仍不跟，检查设置里 `follow_default_audio_device` 是否开启。
- **控制条挡画面**：默认停靠在画面下方（不挤视频）；`Ctrl+H` 切悬浮，`H` 切换常驻 / 自动隐藏。
- **某些格式预览不了（MKV / HEVC / AV1 / HDR）**：预览依赖系统解码器；**转码输出**不受影响（转码走 ffmpeg）。
