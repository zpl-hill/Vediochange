"""FFmpeg 命令构造器 —— 全工程唯一的命令出口。

原则：
- 所有视频处理都生成 argv 列表交给子进程，不自己实现任何编码逻辑。
- 界面上的每一个参数最终都落到这里；命令预览直接展示 argv，便于排查与复制。
- CPU 编码器用 CRF；GPU 编码器(NVENC/QSV/AMF)没有统一的 CRF 语义，
  统一改用 -b:v 目标码率，CRF 档位会换算成等效码率。

滤镜图：缩放/画幅 → 文字水印 → 图片水印 → 字幕烧录 → 字幕封装，
统一由 compose_video_graph() 拼装。只要出现多输入（图片水印、模糊补边），
就改用 -filter_complex 并显式 -map，避免不同功能互相覆盖 -vf。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from .encoder_registry import pick_cpu_encoder
from .media_options import (
    HdrOptions,
    SubtitleOptions,
    WatermarkOptions,
    default_font,
    escape_drawtext,
    escape_filter_path,
    overlay_position,
)
from .presets import (
    EncodeParams,
    crf_to_bitrate_kbps,
    estimate_size_mb,
    even,
    target_bitrate_kbps,
)

NULL_DEVICE = "NUL" if os.name == "nt" else "/dev/null"

ENCODER_TABLE: dict[str, dict[str, str]] = {
    "cpu": {"h264": "libx264", "hevc": "libx265", "av1": "libaom-av1"},
    "nvidia": {"h264": "h264_nvenc", "hevc": "hevc_nvenc", "av1": "av1_nvenc"},
    "intel": {"h264": "h264_qsv", "hevc": "hevc_qsv", "av1": "av1_qsv"},
    "amd": {"h264": "h264_amf", "hevc": "hevc_amf", "av1": "av1_amf"},
}

_CPU_PRESETS = ("ultrafast", "veryfast", "faster", "fast", "medium", "slow", "slower", "veryslow")
_NVENC_PRESET = {"ultrafast": "p1", "veryfast": "p2", "faster": "p3", "fast": "p3",
                 "medium": "p4", "slow": "p5", "slower": "p6", "veryslow": "p7"}
_AOM_CPU_USED = {"ultrafast": 8, "veryfast": 7, "faster": 6, "fast": 5,
                 "medium": 4, "slow": 3, "slower": 2, "veryslow": 0}
_SVT_PRESET = {"ultrafast": 12, "veryfast": 10, "faster": 8, "fast": 7,
               "medium": 6, "slow": 4, "slower": 2, "veryslow": 0}

_SUBTITLE_CONTAINER_CODEC = {
    "mp4": "mov_text", "mov": "mov_text", "m4v": "mov_text",
    "mkv": "srt", "webm": "webvtt", "ts": "srt", "avi": "srt",
}
_ZSCALE_TRANSFERS = {"smpte2084", "smpte2086", "arib-std-b67", "bt2020-10", "bt2020-12"}
_ZSCALE_PRIMARIES = {"bt2020", "bt2020m", "bt2100"}
_ZSCALE_MATRICES = {"bt2020nc", "bt2020ncl", "bt2020c"}


def resolve_vcodec(codec_family: str, vendor: str) -> tuple[str, bool]:
    """返回 (编码器名, 是否硬件)。未知族/vendor 一律回退 CPU。

    CPU AV1 按当前 FFmpeg 实际编入的库挑选（本机有 libaom-av1、无 libsvtav1）。
    """
    family = codec_family if codec_family in ("h264", "hevc", "av1") else "h264"
    table = ENCODER_TABLE.get(vendor, ENCODER_TABLE["cpu"])
    if vendor == "cpu" and family == "av1":
        return pick_cpu_encoder("av1"), False
    cpu_encoder = ENCODER_TABLE["cpu"][family]
    vcodec = table.get(family) or cpu_encoder
    return vcodec, vendor != "cpu" and vcodec != cpu_encoder


def effective_bitrate_kbps(
    params: EncodeParams,
    *,
    is_hardware: bool,
    duration_s: float = 0.0,
    target_size_mb: float = 0.0,
    src_width: int = 0,
    src_height: int = 0,
    src_fps: float = 0.0,
    force_bitrate: bool = False,
) -> int:
    """把三种码率模式统一折算成一个视频目标码率。"""
    mode = params.rate_mode
    if mode == "target_size":
        desired = target_size_mb or params.target_size_mb
        return target_bitrate_kbps(desired, duration_s, params.abitrate_kbps)
    if mode == "bitrate":
        return max(100, int(params.bitrate_kbps or 8000))
    # CRF 模式：GPU 编码器与 2-pass 都没有可用的 CRF 语义，换算成等效码率
    if is_hardware or force_bitrate:
        return crf_to_bitrate_kbps(params.crf, src_width, src_height, src_fps)
    return max(100, int(params.bitrate_kbps or 8000))  # 仅用于预计体积展示


def estimate_output_size_mb(
    params: EncodeParams,
    *,
    duration_s: float,
    is_hardware: bool,
    target_size_mb: float = 0.0,
    src_width: int = 0,
    src_height: int = 0,
    src_fps: float = 0.0,
) -> float:
    if duration_s <= 0:
        return 0.0
    if params.rate_mode == "crf" and not is_hardware:
        video = crf_to_bitrate_kbps(params.crf, src_width or 1920, src_height or 1080, src_fps or 30.0)
    else:
        video = effective_bitrate_kbps(
            params, is_hardware=is_hardware, duration_s=duration_s,
            target_size_mb=target_size_mb, src_width=src_width,
            src_height=src_height, src_fps=src_fps,
        )
    return estimate_size_mb(video, params.abitrate_kbps, duration_s)


def compute_scale(params: EncodeParams, src_width: int, src_height: int) -> tuple[int, int]:
    """返回实际目标宽高；(0, 0) 表示不需要缩放滤镜。"""
    if params.scale_mode == "none":
        return 0, 0
    w = even(params.width or src_width or 0)
    h = even(params.height or src_height or 0)
    if not w or not h:
        return 0, 0
    if w == even(src_width or 0) and h == even(src_height or 0) and params.scale_mode in ("fit", "pad_black"):
        return 0, 0
    return w, h


def build_filter(params: EncodeParams, src_width: int, src_height: int) -> tuple[str, bool]:
    """兼容旧调用的画幅滤镜入口（不含字幕/水印/HDR）。"""
    w, h = compute_scale(params, src_width, src_height)
    if not w or not h:
        return "", False
    mode = params.scale_mode
    if mode == "pad_blur":
        chain = (
            "[0:v]split=2[bg][fg];"
            f"[bg]scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},gblur=sigma=20[bgb];"
            f"[fg]scale={w}:{h}:force_original_aspect_ratio=decrease[fgf];"
            "[bgb][fgf]overlay=(W-w)/2:(H-h)/2,setsar=1[vout]"
        )
        return chain, True
    if mode == "stretch":
        return f"scale={w}:{h}:flags=lanczos,setsar=1", False
    if mode == "fill_crop":
        return f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},setsar=1", False
    return (
        f"scale={w}:{h}:force_original_aspect_ratio=decrease,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1",
        False,
    )


# ---------------- 滤镜图 ----------------
@dataclass
class FilterGraph:
    chain: str = ""
    is_complex: bool = False
    extra_inputs: list[str] = field(default_factory=list)
    map_args: list[str] = field(default_factory=list)
    stream_args: list[str] = field(default_factory=list)
    color_args: list[str] = field(default_factory=list)
    encoder_args: list[str] = field(default_factory=list)
    pix_fmt: str = "yuv420p"
    notes: list[str] = field(default_factory=list)


def _scale_chain(params: EncodeParams, w: int, h: int) -> list[str]:
    mode = params.scale_mode
    if mode == "stretch":
        return [f"scale={w}:{h}:flags=lanczos", "setsar=1"]
    if mode == "fill_crop":
        return [f"scale={w}:{h}:force_original_aspect_ratio=increase", f"crop={w}:{h}", "setsar=1"]
    return [
        f"scale={w}:{h}:force_original_aspect_ratio=decrease",
        f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=black",
        "setsar=1",
    ]


def _tonemap_chain(hdr: HdrOptions, color: dict[str, Any]) -> list[str]:
    """HDR → SDR：FFmpeg 9 的 zscale 需要先用 setparams 给帧打色彩标记。"""
    primaries = str(color.get("color_primaries") or "bt2020").lower()
    transfer = str(color.get("color_transfer") or "smpte2084").lower()
    matrix = str(color.get("color_space") or "bt2020nc").lower()
    if primaries not in _ZSCALE_PRIMARIES:
        primaries = "bt2020"
    if transfer not in _ZSCALE_TRANSFERS:
        transfer = "smpte2084"
    if matrix not in _ZSCALE_MATRICES:
        matrix = "bt2020nc"
    algorithm = hdr.tonemap if hdr.tonemap in (
        "hable", "mobius", "reinhard", "linear", "gamma", "clip") else "hable"
    npl = max(10, min(1000, int(hdr.npl or 100)))
    desat = max(0.0, min(2.0, float(hdr.desat or 0.0)))
    return [
        f"setparams=color_primaries={primaries}:color_trc={transfer}:colorspace={matrix}",
        f"zscale=t=linear:npl={npl}",
        "format=gbrpf32le",
        f"tonemap=tonemap={algorithm}:desat={desat:g}",
        "zscale=t=bt709:m=bt709:r=tv:p=bt709",
        "format=yuv420p",
    ]


def _subtitle_filter(sub: SubtitleOptions) -> str:
    path = escape_filter_path(sub.path)
    parts = [f"subtitles=filename='{path}'"]
    if sub.encoding and sub.encoding.lower() not in ("", "auto", "utf-8", "utf8"):
        parts.append(f"charenc={sub.encoding}")
    if sub.force_style:
        style = (
            f"FontName={sub.font_name or 'Arial'},"
            f"FontSize={max(6, int(sub.font_size))},"
            f"Outline={max(0, int(sub.outline))},"
            "PrimaryColour=&H00FFFFFF&,OutlineColour=&H00000000&,BorderStyle=1"
        )
        parts.append(f"force_style='{style}'")
    return ":".join(parts)


def _text_watermark_filter(wm: WatermarkOptions) -> str:
    x, y = overlay_position(wm.position, wm.margin, for_drawtext=True)
    opacity = max(0.0, min(1.0, float(wm.opacity)))
    parts = []
    font = wm.font_path or default_font()
    if font:
        parts.append(f"fontfile='{escape_filter_path(font)}'")
    parts += [
        # 禁用 %/{} 展开，否则 FFmpeg 9 会把用户输入的 "100%" 当成时间格式
        "expansion=none",
        f"text='{escape_drawtext(wm.text)}'",
        f"fontsize={max(6, int(wm.font_size))}",
        f"fontcolor={wm.color or 'white'}@{opacity:g}",
        f"x={x}", f"y={y}",
    ]
    if wm.box:
        parts += ["box=1", "boxcolor=black@0.45", "boxborderw=6"]
    return "drawtext=" + ":".join(parts)


def _hdr_encoder_args(hdr: HdrOptions, vcodec: str, color: dict[str, Any]) -> list[str]:
    """HDR10 保留：写入色彩标记；libx265 额外写 SEI 静态元数据。"""
    args = [
        "-color_primaries", "bt2020",
        "-color_trc", "smpte2084",
        "-colorspace", "bt2020nc",
    ]
    if vcodec != "libx265":
        return args
    params = ["hdr10=1", "repeat-headers=1", "colorprim=bt2020",
              "transfer=smpte2084", "colormatrix=bt2020nc"]
    master = (hdr.master_display or str(color.get("master_display") or "")).strip()
    cll = (hdr.max_cll or str(color.get("max_cll") or "")).strip()
    if master:
        params.append(f"master-display={master}")
    if cll:
        params.append(f"max-cll={cll}")
    return args + ["-x265-params", ":".join(params)]


def compose_video_graph(
    params: EncodeParams,
    src_width: int,
    src_height: int,
    *,
    color_info: dict[str, Any] | None = None,
    pass_number: int = 0,
    container: str = "",
) -> FilterGraph:
    """把画幅、字幕、水印、HDR 组合成一条完整滤镜链 + 输出流参数。"""
    color = dict(color_info or {})
    source_hdr = bool(color.get("is_hdr"))
    sub = SubtitleOptions.from_dict(params.subtitle)
    wm = WatermarkOptions.from_dict(params.watermark)
    hdr = HdrOptions.from_dict(params.hdr)
    graph = FilterGraph()

    mode = hdr.mode if hdr.mode in ("auto", "keep", "tonemap", "sdr") else "auto"
    if mode == "auto":
        mode = "keep" if source_hdr else "sdr"
    if mode == "tonemap" and not source_hdr:
        # 对 SDR 源做 PQ 反变换会严重偏色，直接退化成普通 SDR 输出
        mode = "sdr"
        graph.notes.append("源视频不是 HDR，色调映射已跳过")
    if mode == "keep":
        graph.pix_fmt = "yuv420p10le" if int(hdr.bit_depth or 10) >= 10 else "yuv420p"
        graph.color_args = _hdr_encoder_args(hdr, "libx265", color)[:-2]
        # 实际编码器在 build_video_command 里才解析；只有 libx265 才写 SEI 元数据
        graph.encoder_args = _hdr_encoder_args(hdr, "libx265", color)[-2:]
    elif mode == "tonemap":
        graph.pix_fmt = "yuv420p"
        graph.color_args = ["-color_primaries", "bt709", "-color_trc", "bt709",
                            "-colorspace", "bt709"]
    else:
        graph.pix_fmt = params.pix_fmt or "yuv420p"

    prefix: list[str] = _tonemap_chain(hdr, color) if mode == "tonemap" else []

    w, h = compute_scale(params, src_width, src_height)
    scale_filters = _scale_chain(params, w, h) if w and h else []
    text_filters = [_text_watermark_filter(wm)] if wm.mode == "text" and wm.active() else []
    burn_filters = [_subtitle_filter(sub)] if sub.mode == "burn" and sub.active() else []

    need_complex = params.scale_mode == "pad_blur" or (wm.mode == "image" and wm.active())
    segments: list[str] = []
    label = "0:v"

    if not need_complex:
        chain = prefix + scale_filters + text_filters + burn_filters
        # 统一像素格式，避免 10bit 源直接喂给 8bit 编码器；
        # tonemap 链自带 format=yuv420p 时不重复添加
        fmt = f"format={graph.pix_fmt}"
        if chain:
            if chain[-1] != fmt:
                chain = chain + [fmt]
        elif graph.pix_fmt != (params.pix_fmt or "yuv420p"):
            chain = [fmt]
        graph.chain = ",".join(chain)
        graph.is_complex = False
    else:
        first = prefix + text_filters + burn_filters
        if params.scale_mode == "pad_blur" and w and h:
            if first:
                segments.append(f"[0:v]{','.join(first)}[vpre]")
                base = "vpre"
            else:
                base = "0:v"
            segments += [
                f"[{base}]split=2[bg][fg]",
                f"[bg]scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},gblur=sigma=20[bgb]",
                f"[fg]scale={w}:{h}:force_original_aspect_ratio=decrease[fgf]",
                "[bgb][fgf]overlay=(W-w)/2:(H-h)/2,setsar=1[vscaled]",
            ]
            label = "vscaled"
        else:
            combined = first + scale_filters
            if combined:
                segments.append(f"[0:v]{','.join(combined)}[vbase]")
                label = "vbase"

        if wm.mode == "image" and wm.active():
            graph.extra_inputs.append(wm.image_path)
            index = len(graph.extra_inputs)  # 0 号输入是主视频
            out_w = w or src_width or 1920
            logo_w = max(16, even(out_w * max(1, int(wm.scale_percent or 15)) / 100))
            opacity = max(0.0, min(1.0, float(wm.opacity)))
            x, y = overlay_position(wm.position, wm.margin)
            segments.append(
                f"[{index}:v]format=rgba,colorchannelmixer=aa={opacity:g},scale={logo_w}:-1[wm]"
            )
            segments.append(
                f"[{label}][wm]overlay={x}:{y}:format=auto,format={graph.pix_fmt}[vout]"
            )
        else:
            segments.append(f"[{label}]null,format={graph.pix_fmt}[vout]")
        graph.chain = ";".join(segments)
        graph.is_complex = True

    # 软字幕：额外输入 + 显式映射；2-pass 第一遍输出到 null，不封装字幕轨
    soft = sub.mode == "embed" and sub.active() and pass_number != 1
    if graph.is_complex:
        graph.map_args = ["-map", "[vout]", "-map", "0:a?"]
    elif soft:
        graph.map_args = ["-map", "0:v", "-map", "0:a?"]
    if soft:
        graph.extra_inputs.append(sub.path)
        index = len(graph.extra_inputs)
        suffix = Path(sub.path).suffix.lower()
        # FFmpeg 按输出文件扩展名选 muxer，所以以实际 dst 为准，
        # 否则「预设写 mkv、输出却是 .mp4」时会选到容器不支持的字幕编码。
        actual_container = (container or params.container or "mp4").lower().lstrip(".")
        codec = _SUBTITLE_CONTAINER_CODEC.get(actual_container, "mov_text")
        if actual_container == "mkv":
            codec = "ass" if suffix in (".ass", ".ssa") else ("webvtt" if suffix == ".vtt" else "srt")
        if actual_container == "webm":
            codec = "webvtt"
        graph.map_args += ["-map", f"{index}:0"]
        language = (sub.language or "und").strip() or "und"
        graph.stream_args = [
            "-c:s", codec,
            "-metadata:s:s:0", f"language={language}",
            "-disposition:s:0", "default",
        ]
    return graph


# ---------------- 编码器参数 ----------------
def _aom_crf(crf: int) -> int:
    """libaom/libsvt AV1 的 CRF 是 0-63，与 x264 的 0-51 做线性映射。"""
    return max(0, min(63, round(int(crf) * 63 / 51)))


def _encoder_args(
    vcodec: str, params: EncodeParams, bitrate_kbps: int, *, force_bitrate: bool = False,
    hdr_args: Sequence[str] | None = None,
) -> list[str]:
    args: list[str] = []
    use_crf = params.rate_mode == "crf" and not force_bitrate
    if vcodec in ("libx264", "libx265"):
        preset = params.preset if params.preset in _CPU_PRESETS else "medium"
        args += ["-preset", preset]
        args += ["-crf", str(params.crf)] if use_crf else ["-b:v", f"{bitrate_kbps}k"]
        # -profile:v high / -level:v 是 H.264 语义，喂给 libx265 会直接导致
        # 编码器打不开（HDR10 10-bit 尤其明显），x265 要用 -x265-params。
        if vcodec == "libx264":
            if params.profile:
                args += ["-profile:v", params.profile]
            if params.level:
                args += ["-level:v", str(params.level)]
        if hdr_args:
            args += list(hdr_args)
    elif vcodec in ("libsvtav1", "libaom-av1", "librav1e"):
        if vcodec == "libsvtav1":
            args += ["-preset", str(_SVT_PRESET.get(params.preset, 6))]
        elif vcodec == "libaom-av1":
            args += ["-cpu-used", str(_AOM_CPU_USED.get(params.preset, 4)), "-row-mt", "1"]
        if use_crf:
            args += ["-crf", str(_aom_crf(params.crf))]
            if vcodec == "libaom-av1":
                args += ["-b:v", "0"]  # libaom 的恒定质量模式必须显式关掉目标码率
        else:
            args += ["-b:v", f"{bitrate_kbps}k"]
    elif vcodec.endswith("_nvenc"):
        args += ["-preset", _NVENC_PRESET.get(params.preset, "p4"), "-rc", "vbr",
                 "-b:v", f"{bitrate_kbps}k"]
        if params.maxrate_kbps:
            args += ["-maxrate", f"{params.maxrate_kbps}k"]
        if params.bufsize_kbps:
            args += ["-bufsize", f"{params.bufsize_kbps}k"]
    elif vcodec.endswith("_qsv"):
        args += ["-b:v", f"{bitrate_kbps}k"]
    elif vcodec.endswith("_amf"):
        args += ["-b:v", f"{bitrate_kbps}k", "-quality", "balanced"]
    else:
        args += ["-b:v", f"{bitrate_kbps}k"]

    if params.maxrate_kbps and vcodec in ("libx264", "libx265"):
        args += ["-maxrate", f"{params.maxrate_kbps}k"]
        if params.bufsize_kbps:
            args += ["-bufsize", f"{params.bufsize_kbps}k"]
    return args


def _audio_args(params: EncodeParams) -> list[str]:
    codec = params.acodec or "aac"
    if codec == "none":
        return ["-an"]
    if codec == "copy":
        return ["-c:a", "copy"]
    args = ["-c:a", codec]
    if codec not in ("flac", "pcm_s16le", "pcm_s24le"):
        args += ["-b:a", f"{max(32, int(params.abitrate_kbps))}k"]
    if params.samplerate:
        args += ["-ar", str(params.samplerate)]
    if params.channels:
        args += ["-ac", str(params.channels)]
    return args


def _base(ffmpeg_bin: str, *, progress: bool = True) -> list[str]:
    cmd = [ffmpeg_bin, "-hide_banner", "-loglevel", "error", "-y"]
    if progress:
        cmd += ["-progress", "pipe:1", "-nostats"]
    return cmd


def build_video_command(
    src: str,
    dst: str,
    params: EncodeParams,
    *,
    ffmpeg_bin: str = "ffmpeg",
    vendor: str = "cpu",
    src_width: int = 0,
    src_height: int = 0,
    src_fps: float = 0.0,
    duration_s: float = 0.0,
    target_size_mb: float = 0.0,
    seek_s: float | None = None,
    clip_duration: float | None = None,
    input_args: list[str] | None = None,
    pass_number: int = 0,
    passlog_prefix: str = "",
    color_info: dict[str, Any] | None = None,
) -> list[str]:
    """构造一条完整的转码命令（含 ffmpeg 可执行文件本身）。"""
    vcodec, is_hardware = resolve_vcodec(params.codec_family, vendor)
    # 2-pass 与 GPU 编码器必须用目标码率（CRF 会被忽略）；码率按实际输出尺寸估算
    force_bitrate = is_hardware or pass_number in (1, 2)
    target_w, target_h = compute_scale(params, src_width, src_height)
    effective_w = target_w or src_width
    effective_h = target_h or src_height
    effective_fps = params.fps or src_fps
    bitrate = effective_bitrate_kbps(
        params, is_hardware=is_hardware, duration_s=duration_s,
        target_size_mb=target_size_mb, src_width=effective_w,
        src_height=effective_h, src_fps=effective_fps,
        force_bitrate=force_bitrate,
    )

    # 字幕时间轴是「全片绝对时间」。输入侧 -ss 会把帧时间戳归零，
    # 导致字幕整段错位；这时改用输出侧 -ss（解码后丢弃），
    # 帧与字幕流都保持原始时间轴，输出仍会从 0 开始。
    active_subtitle = SubtitleOptions.from_dict(params.subtitle).active()
    absolute_ts = active_subtitle and seek_s is not None and seek_s > 0
    cmd = _base(ffmpeg_bin)
    if seek_s is not None and seek_s > 0 and not absolute_ts:
        cmd += ["-ss", f"{seek_s:.3f}"]
    cmd += input_args if input_args is not None else ["-i", src]
    graph = compose_video_graph(
        params, src_width, src_height, color_info=color_info, pass_number=pass_number,
        container=Path(dst).suffix.lstrip(".") if pass_number != 1 else "",
    )
    for extra in graph.extra_inputs:
        cmd += ["-i", extra]
    if absolute_ts:
        cmd += ["-ss", f"{seek_s:.3f}"]
    if clip_duration is not None and clip_duration > 0:
        cmd += ["-t", f"{clip_duration:.3f}"]

    if graph.chain:
        cmd += ["-filter_complex", graph.chain] if graph.is_complex else ["-vf", graph.chain]
    cmd += graph.map_args

    cmd += ["-c:v", vcodec]
    # HDR10 的 -x265-params 只对 libx265 有意义
    hdr_extra = graph.encoder_args if vcodec == "libx265" else []
    cmd += _encoder_args(vcodec, params, bitrate, force_bitrate=force_bitrate,
                         hdr_args=hdr_extra)
    cmd += ["-pix_fmt", graph.pix_fmt]
    cmd += graph.color_args
    # 2-pass 第一遍只统计视频，跳过音频编码可明显缩短耗时
    cmd += ["-an"] if pass_number == 1 else _audio_args(params)
    cmd += graph.stream_args

    if params.fps and params.fps > 0:
        cmd += ["-r", f"{params.fps:g}"]
    if params.gop and params.gop > 0:
        cmd += ["-g", str(params.gop)]
    if pass_number != 1 and params.faststart and params.container in ("mp4", "mov", "m4v"):
        cmd += ["-movflags", "+faststart"]
    if pass_number in (1, 2):
        cmd += ["-pass", str(pass_number)]
        if passlog_prefix:
            cmd += ["-passlogfile", passlog_prefix]

    if pass_number == 1:
        cmd += ["-f", "null", NULL_DEVICE]
    else:
        cmd += [dst]
    return cmd


def build_two_pass_commands(
    src: str, dst: str, params: EncodeParams, **kwargs: Any
) -> tuple[list[str], list[str]]:
    """2-pass：第一遍统计，第二遍输出。passlog 放在输出目录旁边。"""
    prefix = str(Path(dst).with_suffix("")) + "_vtkpass"
    common = dict(kwargs)
    first = build_video_command(src, dst, params, pass_number=1, passlog_prefix=prefix, **common)
    second = build_video_command(src, dst, params, pass_number=2, passlog_prefix=prefix, **common)
    return first, second


def build_cut_command(
    src: str,
    dst: str,
    start_s: float,
    end_s: float,
    *,
    lossless: bool = True,
    ffmpeg_bin: str = "ffmpeg",
    params: EncodeParams | None = None,
    vendor: str = "cpu",
    src_width: int = 0,
    src_height: int = 0,
    src_fps: float = 0.0,
    duration_s: float = 0.0,
    color_info: dict[str, Any] | None = None,
) -> list[str]:
    """剪切。无损用 -c copy（快但受关键帧限制），重编码可精确到帧。"""
    if end_s <= start_s:
        raise ValueError("剪切结束时间必须大于开始时间")
    clip = end_s - start_s
    if lossless or params is None:
        cmd = _base(ffmpeg_bin)
        if start_s > 0:
            cmd += ["-ss", f"{start_s:.3f}"]
        cmd += ["-i", src, "-t", f"{clip:.3f}", "-c", "copy",
                "-avoid_negative_ts", "make_zero"]
        if Path(dst).suffix.lower() in (".mp4", ".mov", ".m4v"):
            cmd += ["-movflags", "+faststart"]
        cmd += [dst]
        return cmd
    return build_video_command(
        src, dst, params, ffmpeg_bin=ffmpeg_bin, vendor=vendor,
        src_width=src_width, src_height=src_height, src_fps=src_fps,
        duration_s=duration_s, seek_s=start_s, clip_duration=clip,
        color_info=color_info,
    )


def write_concat_list(files: Sequence[str], path: str | Path) -> str:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    for item in files:
        clean = Path(item).resolve().as_posix().replace("'", "'\\''")
        lines.append(f"file '{clean}'")
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(target)


def build_merge_command(
    list_path: str,
    dst: str,
    *,
    ffmpeg_bin: str = "ffmpeg",
    reencode: bool = False,
    params: EncodeParams | None = None,
    vendor: str = "cpu",
    src_width: int = 0,
    src_height: int = 0,
    src_fps: float = 0.0,
    duration_s: float = 0.0,
) -> list[str]:
    input_args = ["-f", "concat", "-safe", "0", "-i", list_path]
    if reencode and params is not None:
        return build_video_command(
            "", dst, params, ffmpeg_bin=ffmpeg_bin, vendor=vendor,
            src_width=src_width, src_height=src_height, src_fps=src_fps,
            duration_s=duration_s, input_args=input_args,
        )
    cmd = _base(ffmpeg_bin) + input_args + ["-c", "copy", "-avoid_negative_ts", "make_zero"]
    if Path(dst).suffix.lower() in (".mp4", ".mov", ".m4v"):
        cmd += ["-movflags", "+faststart"]
    return cmd + [dst]


_AUDIO_CODEC = {
    "mp3": "libmp3lame", "aac": "aac", "m4a": "aac",
    "wav": "pcm_s16le", "flac": "flac", "ogg": "libvorbis",
}


def build_audio_command(
    src: str,
    dst: str,
    *,
    ffmpeg_bin: str = "ffmpeg",
    fmt: str | None = None,
    bitrate_kbps: int = 192,
    samplerate: int = 48000,
    channels: int = 2,
) -> list[str]:
    ext = (fmt or Path(dst).suffix.lstrip(".")).lower()
    codec = _AUDIO_CODEC.get(ext, "aac")
    cmd = _base(ffmpeg_bin) + ["-i", src, "-vn", "-c:a", codec]
    if codec not in ("flac", "pcm_s16le"):
        cmd += ["-b:a", f"{bitrate_kbps}k"]
    cmd += ["-ar", str(samplerate), "-ac", str(channels), dst]
    return cmd


def build_snapshot_command(
    src: str, dst: str, time_s: float, *, ffmpeg_bin: str = "ffmpeg", quality: int = 2
) -> list[str]:
    cmd = _base(ffmpeg_bin, progress=False)
    if time_s > 0:
        cmd += ["-ss", f"{time_s:.6f}"]
    cmd += ["-i", src, "-frames:v", "1"]
    if Path(dst).suffix.lower() in (".jpg", ".jpeg"):
        cmd += ["-q:v", str(quality)]
    return cmd + [dst]


def build_frame_png_command(
    src: str, frame_no: int, fps: float, *, ffmpeg_bin: str = "ffmpeg"
) -> list[str]:
    """逐帧浏览：按帧号计算时间戳，用 -ss 精确 seek 后输出单帧 PNG 到 stdout。

    -ss 前置时 FFmpeg 会 seek 到最近关键帧再解码丢弃到目标时间，
    既快又精确到帧；对 24 小时长视频也不会从头解码。
    """
    if fps <= 0:
        raise ValueError("帧率必须大于 0 才能逐帧定位")
    t = max(0, int(frame_no)) / fps
    return _base(ffmpeg_bin, progress=False) + [
        "-ss", f"{t:.6f}", "-i", src,
        "-frames:v", "1", "-f", "image2pipe", "-vcodec", "png", "pipe:1",
    ]


def build_gif_command(
    src: str,
    dst: str,
    *,
    ffmpeg_bin: str = "ffmpeg",
    start_s: float = 0.0,
    duration_s: float = 5.0,
    fps: int = 12,
    width: int = 480,
) -> list[str]:
    chain = (
        f"[0:v]fps={fps},scale={width}:-1:flags=lanczos,split[a][b];"
        "[a]palettegen[p];[b][p]paletteuse"
    )
    cmd = _base(ffmpeg_bin)
    if start_s > 0:
        cmd += ["-ss", f"{start_s:.3f}"]
    cmd += ["-i", src, "-t", f"{duration_s:.3f}",
            "-filter_complex", chain, "-loop", "0", dst]
    return cmd


def format_command(argv: Sequence[str]) -> str:
    """把 argv 转成可复制的命令行文本（仅用于展示，不用于执行）。"""
    parts: list[str] = []
    for item in argv:
        text = str(item)
        if any(ch.isspace() for ch in text) or ";" in text or text.startswith("["):
            parts.append('"' + text.replace('"', '\\"') + '"')
        else:
            parts.append(text)
    return " ".join(parts)
