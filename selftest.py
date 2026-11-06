"""无 GUI 自检：验证预设、命令构造、ffprobe 与 FFmpeg 命令能否真正跑通。"""
from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

from config.paths import find_binary
from ffmpeg_core.command_builder import (
    build_cut_command,
    build_frame_png_command,
    build_two_pass_commands,
    format_command,
)
from ffmpeg_core.ffmpeg_engine import ffmpeg_binary, run_capture_bytes, run_command
from ffmpeg_core.ffprobe_engine import probe
from ffmpeg_core.presets import (
    EncodeParams,
    apply_strategy,
    estimate_size_mb,
    preset_library,
    target_bitrate_kbps,
)
from modules.converter import plan_conversion

_failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    mark = "[OK]  " if condition else "[FAIL]"
    print(f"  {mark} {name}" + (f"  -> {detail}" if detail else ""))
    if not condition:
        _failures.append(name)


def run() -> int:
    print("=" * 72)
    print("Video Toolkit V1.0 · 自检")
    print("=" * 72)

    print("\n[1] 预设库")
    library = preset_library()
    check("预设加载", len(library.presets) >= 20, f"{len(library.presets)} 条")
    for category in ("platform", "device", "quality"):
        check(f"分类 {category}", len(library.by_category(category)) > 0,
              f"{len(library.by_category(category))} 条")
    extreme = library.get("quality.extreme")
    check("极限压缩预设存在", extreme is not None and bool(extreme.strategy))
    if extreme:
        resolved = apply_strategy(extreme.params, extreme.strategy, 3840, 2160, 60.0)
        check("极限压缩降分辨率", resolved.width <= 1280 and resolved.height <= 720,
              f"{resolved.width}x{resolved.height}")
        check("极限压缩帧率上限", resolved.fps <= 24, f"{resolved.fps:g} fps")
        check("极限压缩 2-pass", resolved.two_pass is True)

    print("\n[2] 目标体积 / 码率反算")
    bitrate = target_bitrate_kbps(100, 600, 128)
    estimated = estimate_size_mb(bitrate, 128, 600)
    check("反算码率为正", bitrate > 0, f"{bitrate} kbps")
    check("预计体积接近目标", 90 <= estimated <= 100, f"{estimated:.1f} MB")

    print("\n[3] 命令构造")
    from ffmpeg_core.command_builder import build_video_command

    params = EncodeParams(rate_mode="crf", crf=23, scale_mode="none")
    argv_cpu = build_video_command("in.mp4", "out.mp4", params, vendor="cpu",
                                   src_width=1920, src_height=1080, src_fps=30)
    check("CPU 使用 libx264", "libx264" in argv_cpu)
    check("CPU 使用 CRF", "-crf" in argv_cpu and "23" in argv_cpu)
    argv_amd = build_video_command("in.mp4", "out.mp4", params, vendor="amd",
                                   src_width=1920, src_height=1080, src_fps=30)
    check("AMD 使用 h264_amf", "h264_amf" in argv_amd)
    check("AMD 改用 -b:v（CRF 换算）", "-b:v" in argv_amd and "-crf" not in argv_amd)

    vertical = library.get("platform.douyin.1080p")
    if vertical:
        argv_vertical = build_video_command(
            "in.mp4", "out.mp4", vertical.params, vendor="cpu",
            src_width=1920, src_height=1080, src_fps=30,
        )
        check("竖屏预设使用 filter_complex 模糊补边", "-filter_complex" in argv_vertical)

    target = library.get("quality.target_size")
    if target:
        first, second = build_two_pass_commands(
            "in.mp4", "out.mp4", target.params, duration_s=600.0,
            target_size_mb=100.0, src_width=1920, src_height=1080, src_fps=30,
        )
        check("目标体积生成 2-pass", "-pass" in first and "-pass" in second)

    cut = build_cut_command("in.mp4", "cut.mp4", 10, 25, lossless=True)
    check("无损剪切使用 -c copy", "-c" in cut and "copy" in cut)
    frame = build_frame_png_command("in.mp4", 15200, 30.0)
    check("逐帧命令输出到 stdout 管道", "pipe:1" in frame and "-ss" in frame)

    if extreme:
        resolved = apply_strategy(extreme.params, extreme.strategy, 1920, 1080, 30.0)
        pass1, pass2 = build_two_pass_commands(
            "in.mp4", "out.mp4", resolved, duration_s=600.0,
            src_width=1920, src_height=1080, src_fps=30.0,
        )
        check("极限压缩 2-pass 改用目标码率",
              "-b:v" in pass1 and "-crf" not in pass1,
              next((pass1[i + 1] for i, x in enumerate(pass1) if x == "-b:v"), ""))
        check("2-pass 第一遍跳过音频", "-an" in pass1)
        check("2-pass 第二遍保留音频", "-c:a" in pass2)
    print("  示例命令: " + format_command(argv_cpu)[:150] + " ...")

    print("\n[4] PySide6 播放能力")
    try:
        from PySide6.QtMultimedia import QMediaPlayer
        import PySide6

        check("PySide6 版本", True, PySide6.__version__)
        check("pitchCompensation API", hasattr(QMediaPlayer, "setPitchCompensation")
              and hasattr(QMediaPlayer, "pitchCompensationAvailability"))
    except Exception as exc:
        check("PySide6 可导入", False, str(exc))

    print("\n[5] FFmpeg 实际执行")
    ffmpeg = find_binary("ffmpeg")
    check("找到 ffmpeg", Path(ffmpeg).is_file() or subprocess.run(
        [ffmpeg, "-version"], capture_output=True).returncode == 0, ffmpeg)

    with tempfile.TemporaryDirectory(prefix="vtk_selftest_") as temp:
        folder = Path(temp)
        source = folder / "source.mp4"
        make = [
            ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "testsrc=size=320x240:rate=25:duration=2",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
            "-shortest", str(source),
        ]
        created = subprocess.run(make, capture_output=True)
        check("生成 2 秒测试视频", created.returncode == 0 and source.is_file(),
              "" if created.returncode == 0 else created.stderr.decode("utf-8", "replace")[:200])
        if not source.is_file():
            return _finish()

        info = probe(source)
        check("ffprobe 读取时长", 1.5 <= info.duration <= 2.5, f"{info.duration:.2f}s")
        check("ffprobe 读取分辨率", (info.width, info.height) == (320, 240),
              info.resolution_text())
        check("ffprobe 读取帧率", 24 <= info.fps <= 26, info.fps_text())
        check("ffprobe 读取分轨码率", info.vbitrate_kbps > 0 and info.abitrate_kbps > 0,
              f"视频 {info.vbitrate_text()} / 音频 {info.abitrate_text()}")

        output = folder / "converted.mp4"
        plan = plan_conversion(
            str(source), str(output),
            EncodeParams(container="mp4", rate_mode="crf", crf=28,
                         scale_mode="none", preset="ultrafast", faststart=True),
            media=info, vendor="cpu",
        )
        result = run_command(plan.commands[0], duration_s=info.duration)
        check("CPU 转码实际成功", result.returncode == 0 and output.is_file(),
              result.stderr.strip()[:200])
        if output.is_file():
            converted = probe(output)
            check("产物可被 ffprobe 读取", converted.duration > 0,
                  f"{converted.duration:.2f}s / {converted.size_text()}")

        png = run_capture_bytes(
            build_frame_png_command(str(source), 25, 25.0, ffmpeg_bin=ffmpeg)
        )
        check("逐帧抽帧返回 PNG", png.startswith(b"\x89PNG"), f"{len(png)} bytes")

        two_pass_out = folder / "twopass.mp4"
        target_params = EncodeParams(container="mp4", rate_mode="target_size",
                                     target_size_mb=0.2, two_pass=True,
                                     scale_mode="none", preset="ultrafast")
        two_pass_plan = plan_conversion(str(source), str(two_pass_out),
                                        target_params, media=info, vendor="cpu")
        check("目标体积生成两条命令", len(two_pass_plan.commands) == 2)
        r1 = run_command(two_pass_plan.commands[0], duration_s=info.duration)
        r2 = run_command(two_pass_plan.commands[1], duration_s=info.duration)
        check("2-pass 实际编码成功",
              r1.returncode == 0 and r2.returncode == 0 and two_pass_out.is_file(),
              (r1.stderr + r2.stderr).strip()[:200])

    print("\n[6] 界面资源 / 文案完整性")
    import json
    import re

    from config.paths import resource_path

    zh = json.loads(resource_path("language", "zh_CN.json").read_text(encoding="utf-8"))
    en = json.loads(resource_path("language", "en_US.json").read_text(encoding="utf-8"))
    check("中英文案键一一对应", set(zh) == set(en),
          f"zh={len(zh)} en={len(en)} 差异={sorted(set(zh) ^ set(en))[:6]}")

    static_keys: set[str] = set()
    for py_file in Path(".").rglob("*.py"):
        if "__pycache__" in py_file.parts:
            continue
        static_keys.update(re.findall(
            r'\btr\(\s*["\']([^"\']+)["\']', py_file.read_text(encoding="utf-8")
        ))
    missing = sorted(k for k in static_keys if k not in zh)
    check("静态 tr() 键均有译文", not missing, f"检查 {len(static_keys)} 个，缺失: {missing[:8]}")

    dynamic: set[str] = set()
    for key in ("compress", "cut", "queue", "list", "info"):
        dynamic.add(f"tool.{key}")
        dynamic.add(f"tool.{key}_hint")
    for value in ("none", "fit", "pad_black", "pad_blur", "fill_crop", "stretch"):
        dynamic.add(f"scale.{value}")
    for value in ("auto", "cpu", "nvidia", "intel", "amd"):
        dynamic.add(f"encoder.{value}")
    for value in ("crf", "bitrate", "target_size"):
        dynamic.add(f"output.mode.{value}")
    for value in ("all", "platform", "device", "quality", "custom"):
        dynamic.add(f"cat.{value}")
    from ffmpeg_core.media_options import (
        HDR_MODES, SUBTITLE_MODES, WATERMARK_MODES, WATERMARK_POSITIONS,
    )
    for value in SUBTITLE_MODES:
        dynamic.add(f"fx.sub.mode.{value}")
    for value in WATERMARK_MODES:
        dynamic.add(f"fx.wm.mode.{value}")
    for value in WATERMARK_POSITIONS:
        dynamic.add(f"fx.wm.pos.{value}")
    for value in HDR_MODES:
        dynamic.add(f"fx.hdr.mode.{value}")
    dynamic.update(("player.pitch_on", "player.pitch_off", "player.pitch_unknown",
                    "status.muted", "status.unmuted"))
    from ui.shortcuts import GROUPS
    for group, rows in GROUPS:
        dynamic.add(group)
        dynamic.update(action for _keys, action in rows)
    missing_dynamic = sorted(k for k in dynamic if k not in zh)
    check("动态拼接文案键均有译文", not missing_dynamic,
          f"检查 {len(dynamic)} 个，缺失: {missing_dynamic[:8]}")
    check("快捷键一览覆盖 5 个分组", len(GROUPS) == 5)

    print("\n[7] 字幕 / 水印 / AV1 / HDR")
    from ffmpeg_core.command_builder import build_video_command, resolve_vcodec
    from ffmpeg_core.encoder_registry import available_encoders, pick_cpu_encoder
    from ffmpeg_core.media_options import (
        HdrOptions, SubtitleOptions, WatermarkOptions, escape_filter_path,
    )
    from player.subtitle_track import SubtitleTrack

    encoders = available_encoders()
    cpu_av1 = pick_cpu_encoder("av1")
    check("AV1 CPU 编码器按实际编译结果挑选", cpu_av1 in encoders, cpu_av1)
    check("resolve_vcodec(av1, cpu) 返回可用编码器",
          resolve_vcodec("av1", "cpu")[0] == cpu_av1)
    check("resolve_vcodec(av1, amd) 返回 av1_amf",
          resolve_vcodec("av1", "amd") == ("av1_amf", True))
    try:
        from PySide6.QtMultimedia import QAudioOutput, QMediaDevices
        check("音频设备热切换 API 可用",
              hasattr(QAudioOutput, "setDevice") and hasattr(QMediaDevices, "audioOutputsChanged"))
    except Exception as exc:
        check("音频设备热切换 API 可用", False, str(exc))
    shortcut_keys = {keys for _group, rows in GROUPS for keys, _key in rows}
    check("+ / - 调速已列入快捷键表", "+ / -" in shortcut_keys)
    check("V 字幕开关已列入快捷键表", "V" in shortcut_keys)

    with tempfile.TemporaryDirectory(prefix="vtk_selftest_fx_") as temp:
        folder = Path(temp)
        sdr = folder / "sdr.mp4"
        hdr = folder / "hdr.mkv"
        srt = folder / "中 文.srt"
        logo = folder / "logo.png"
        ffmpeg = find_binary("ffmpeg")
        subprocess.run(
            [ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
             "-i", "testsrc=size=320x180:rate=25:duration=0.4", "-f", "lavfi",
             "-i", "sine=frequency=440:duration=0.4", "-c:v", "libx264",
             "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(sdr)], check=True,
        )
        subprocess.run(
            [ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
             "-i", "testsrc=size=320x180:rate=25:duration=0.4", "-c:v", "libx265",
             "-pix_fmt", "yuv420p10le", "-preset", "ultrafast", "-crf", "30",
             "-color_primaries", "bt2020", "-color_trc", "smpte2084",
             "-colorspace", "bt2020nc", "-x265-params", "hdr10=1:max-cll=1000,400",
             str(hdr)], check=True,
        )
        subprocess.run(
            [ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
             "-i", "color=c=red@0.5:s=60x24,format=rgba", "-frames:v", "1", str(logo)],
            check=True,
        )
        srt.write_text("1\n00:00:00,000 --> 00:00:00,300\n你好，世界\n", encoding="utf-8")

        track = SubtitleTrack()
        check("SRT 解析", track.load(srt) == 1 and track.cue_at(0.1) == "你好，世界")
        check("时间轴外的字幕不显示", track.cue_at(1.0) == "")
        check("滤镜路径转义保留中文与空格", "中 文" in escape_filter_path(srt)
              and r"\:" in escape_filter_path(srt))

        hdr_info = probe(hdr)
        sdr_info = probe(sdr)
        check("ffprobe 识别 HDR 源", hdr_info.is_hdr, hdr_info.hdr_type)
        check("ffprobe 识别 MaxCLL", bool(hdr_info.max_cll), hdr_info.max_cll)
        check("ffprobe 识别 SDR 源", not sdr_info.is_hdr)

        def params(**kwargs) -> EncodeParams:
            base = dict(container="mp4", codec_family="h264", rate_mode="crf", crf=30,
                        scale_mode="none", preset="ultrafast", faststart=False)
            base.update(kwargs)
            return EncodeParams(**base)

        burn = SubtitleOptions(mode="burn", path=str(srt)).to_dict()
        soft = SubtitleOptions(mode="embed", path=str(srt), language="chi").to_dict()
        image_wm = WatermarkOptions(mode="image", image_path=str(logo),
                                    scale_percent=20).to_dict()
        text_wm = WatermarkOptions(mode="text", text="VTK 100% A,B;c: it's").to_dict()
        tone = HdrOptions(mode="tonemap", tonemap="hable").to_dict()
        keep = HdrOptions(mode="keep", bit_depth=10).to_dict()

        cases = [
            ("字幕烧录", sdr, params(subtitle=burn), "mp4", sdr_info.color_info(), "cpu"),
            ("软字幕封装", sdr, params(subtitle=soft), "mp4", sdr_info.color_info(), "cpu"),
            ("图片水印", sdr, params(watermark=image_wm), "mp4", sdr_info.color_info(), "cpu"),
            ("文字水印", sdr, params(watermark=text_wm), "mp4", sdr_info.color_info(), "cpu"),
            ("HDR 转 SDR", hdr, params(hdr=tone), "mp4", hdr_info.color_info(), "cpu"),
            ("HDR10 保留", hdr, params(codec_family="hevc", container="mkv", hdr=keep),
             "mkv", hdr_info.color_info(), "cpu"),
            ("AV1 CPU", sdr, params(codec_family="av1", container="mkv"),
             "mkv", sdr_info.color_info(), "cpu"),
        ]
        for index, (name, source, encode_params, ext, color, vendor) in enumerate(cases):
            output = folder / f"case{index}.{ext}"
            argv = build_video_command(
                str(source), str(output), encode_params, ffmpeg_bin=ffmpeg,
                vendor=vendor, src_width=320, src_height=180, src_fps=25,
                duration_s=0.4, color_info=color,
            )
            result = run_command(argv, duration_s=0.4)
            check(f"实跑 · {name}", result.returncode == 0 and output.is_file(),
                  result.stderr.strip()[-160:])

        combined = build_video_command(
            str(hdr), str(folder / "all.mkv"),
            params(scale_mode="pad_blur", width=240, height=240,
                   watermark=image_wm, subtitle=burn, hdr=tone, container="mkv"),
            ffmpeg_bin=ffmpeg, vendor="cpu", src_width=320, src_height=180,
            src_fps=25, duration_s=0.4, color_info=hdr_info.color_info(),
        )
        check("组合滤镜走 -filter_complex", "-filter_complex" in combined)
        check("组合滤镜含 tonemap / overlay / subtitles",
              all(any(token in item for item in combined)
                  for token in ("tonemap=", "overlay=", "subtitles=")))
        check("组合滤镜含主视频 + 水印两路输入",
              sum(1 for item in combined if item == "-i") >= 2)
        result = run_command(combined, duration_s=0.4)
        check("实跑 · 缩放+水印+字幕+色调映射", result.returncode == 0,
              result.stderr.strip()[-200:])

        # 剪辑 + 字幕：输入侧 -ss 会归零时间戳，字幕按全片绝对时间匹配会整段错位
        clip_cmd = build_video_command(
            str(sdr), str(folder / "clip.mp4"), params(subtitle=burn),
            ffmpeg_bin=ffmpeg, vendor="cpu", src_width=320, src_height=180,
            src_fps=25, duration_s=0.3, seek_s=0.1, clip_duration=0.3,
        )
        check("剪辑+字幕改用输出侧 -ss 保留时间轴",
              clip_cmd.index(str(sdr)) < clip_cmd.index("-ss"))
        plain_cmd = build_video_command(
            str(sdr), str(folder / "plain_clip.mp4"), params(),
            ffmpeg_bin=ffmpeg, vendor="cpu", src_width=320, src_height=180,
            src_fps=25, duration_s=0.3, seek_s=0.1, clip_duration=0.3,
        )
        check("无字幕仍用输入侧快速 -ss",
              plain_cmd.index("-ss") < plain_cmd.index(str(sdr)))
        result = run_command(clip_cmd, duration_s=0.3)
        check("实跑 · 剪辑区间 + 字幕烧录", result.returncode == 0,
              result.stderr.strip()[-200:])

    bat_install = (Path(__file__).resolve().parent / "install.bat").read_text(
        encoding="ascii", errors="replace"
    )
    check("install.bat 一键装依赖并自检",
          "-r requirements.txt" in bat_install and "--selftest" in bat_install)
    check("install.bat 提示 ffmpeg 不是 pip 包", "NOT Python packages" in bat_install)

    print("\n[8] 播放列表 / 控制条布局")
    import inspect

    from player.playlist import Playlist

    pl = Playlist()
    check("去重加入", pl.add(["a.mp4", "b.mp4", "a.mp4"]) == 2, f"{len(pl)} 项")
    pl.select(0)
    check("下一个 / 上一个", (pl.next_index(), pl.prev_index()) == (1, None))
    pl.move(0, 1)
    check("下移后当前项跟随", pl.items[:2] == ["b.mp4", "a.mp4"] and pl.index == 1)
    pl.select(0)
    pl.remove([0])
    check("删除当前项后自动指向下一项", pl.current == "a.mp4" and pl.index == 0)
    check("末尾不再越界", pl.next_index() is None)
    pl.clear()
    check("清空后回到空状态", len(pl) == 0 and pl.current is None)

    from ui.main_window import MainWindow, _TOOL_KEYS
    from ui.playback_overlay import PlaybackOverlay
    from player.video_surface import VideoSurface

    check("播放列表已加入工具栏入口", "list" in _TOOL_KEYS, str(_TOOL_KEYS))
    check("打开视频走多选对话框",
          "getOpenFileNames" in inspect.getsource(MainWindow._open_file))
    from ui.info_panel import _FIELDS as INFO_FIELDS
    fields = [field for _, field in INFO_FIELDS]
    check("信息面板列出视频码率", "vbitrate" in fields, str(fields))
    check("信息面板列出音频码率", "abitrate" in fields)
    check("状态栏常驻视频码率", hasattr(MainWindow, "_update_media_brief"))
    for name in ("set_docked", "is_docked", "set_auto_hide", "poke"):
        check(f"控制条提供 {name}()", hasattr(PlaybackOverlay, name))
    check("控制条停靠变化会通知宿主重排",
          hasattr(PlaybackOverlay, "spaceChanged"))
    # QStackedLayout 会忽略 contentsMargins，画面必须自己扣掉底部条带
    check("画面支持底部预留（停靠不遮挡的真正实现）",
          hasattr(VideoSurface, "set_bottom_inset"))
    check("停靠不用 QStackedLayout 边距（它根本不生效）",
          "stack.setContentsMargins" not in inspect.getsource(MainWindow._position_overlay))
    hide_src = inspect.getsource(PlaybackOverlay._should_hide_later)
    check("隐藏判定不再依赖光标热区（否则鼠标停在控制条上永不隐藏）",
          "_in_hotzone" not in hide_src, hide_src.strip().splitlines()[-1][:60])
    # 第二层：热区方法本身必须彻底删除，只靠位移唤出；否则静止光标会在
    # 淡出后 200ms 立刻把它弹回来（用户看到的「怎么都隐藏不了」）
    check("控制条已移除底部热区自动唤出",
          not hasattr(PlaybackOverlay, "_in_hotzone"))
    check("唤出只由光标位移触发",
          "_in_hotzone" not in inspect.getsource(PlaybackOverlay._check_pointer))
    for name in ("hide_now", "set_docked", "set_auto_hide"):
        check(f"控制条提供 {name}()", hasattr(PlaybackOverlay, name))
    check("控制条上有「隐藏」按钮",
          "hide_btn" in inspect.getsource(MainWindow._build_overlay)
          and hasattr(MainWindow, "_hide_overlay_now"))

    # 控制条整体高度：按钮行 22 + 时间轴 46 + 边距，压到 100 以内
    from player.timeline import _HEIGHT as TIMELINE_HEIGHT
    check("时间轴高度已压窄（<=48px）", TIMELINE_HEIGHT <= 48, f"{TIMELINE_HEIGHT}px")
    pos_src = inspect.getsource(MainWindow._position_overlay)
    check("控制条整体高度上限已收紧", "max(78," in pos_src)

    # 控制台窗口：run.bat 启动时隐藏黑框，手动跑 / --console 保留
    import main as entry
    check("启动时可变暗控制台窗口", hasattr(entry, "_hide_console"))
    check("只对 run.bat 启动生效（不隐藏用户自己的终端）",
          "VTK_HIDE_CONSOLE" in inspect.getsource(entry._hide_console))
    bat = (Path(__file__).resolve().parent / "run.bat").read_text(
        encoding="utf-8", errors="replace"
    )
    check("run.bat 设置了 VTK_HIDE_CONSOLE", "VTK_HIDE_CONSOLE=1" in bat)
    check("run.bat 主流程不用 pause 挂住隐藏的窗口",
          "pause" not in bat.split("py -3 main.py")[-1].split(":nopyside")[0])
    # .bat 必须是 CRLF + 纯 ASCII：cmd 按字节读文件，LF 换行配多字节中文注释时
    # 行首会被错位吃掉（`rem 这里…` 变成 `里…` 当命令执行），报「'Windows' 不是内部或外部命令」
    for name in ("run.bat", "build_exe.bat", "install.bat"):
        raw = (Path(__file__).resolve().parent / name).read_bytes()
        crlf, lf = raw.count(b"\r\n"), raw.count(b"\n")
        check(f"{name} 使用 CRLF 换行", crlf == lf and lf > 0, f"CRLF={crlf} LF={lf}")
        check(f"{name} 是纯 ASCII 且无 BOM",
              all(b < 128 for b in raw) and not raw.startswith(b"\xef\xbb\xbf"))

    return _finish()


def _finish() -> int:
    print("\n" + "=" * 72)
    if _failures:
        print(f"自检未通过：{len(_failures)} 项 -> {', '.join(_failures)}")
        return 1
    print("自检全部通过 ✓")
    print("=" * 72)
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
