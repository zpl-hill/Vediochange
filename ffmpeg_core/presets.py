"""编码参数模型与三层预设库（平台 / 设备 / 质量档）。

设计要点：
- 预设库是 JSON，defaults + 每个预设的覆盖字段合并，便于用户自行编辑。
- 「质量模式(CRF)」与「体积模式(目标码率 / 2-pass / 目标体积)」是两套独立逻辑，
  因为 CRF 无法预测文件体积，"压到 xx MB" 必须反算码率。
- 极限压缩是组合策略(strategy)，不是把 CRF 调大一个数字。
"""
from __future__ import annotations

import json
from dataclasses import MISSING, asdict, dataclass, field, fields, replace
from pathlib import Path
from typing import Any

from config.errors import PresetError
from config.logging_setup import get_logger
from config.paths import resource_path

_log = get_logger("presets")


@dataclass
class EncodeParams:
    """一条完整的输出参数。0 / 空字符串表示「沿用源文件」。"""

    container: str = "mp4"
    codec_family: str = "h264"
    vcodec: str = "libx264"
    profile: str = "high"
    level: str = ""
    width: int = 0
    height: int = 0
    scale_mode: str = "fit"
    fps: float = 0.0
    rate_mode: str = "bitrate"          # crf | bitrate | target_size
    crf: int = 23
    bitrate_kbps: int = 8000
    maxrate_kbps: int = 0
    bufsize_kbps: int = 0
    preset: str = "medium"
    gop: int = 0
    pix_fmt: str = "yuv420p"
    acodec: str = "aac"
    abitrate_kbps: int = 128
    samplerate: int = 48000
    channels: int = 2
    faststart: bool = True
    two_pass: bool = False
    target_size_mb: float = 0.0
    # 任务级扩展选项（字幕 / 水印 / HDR），存成普通 dict，方便预设 JSON 编辑
    subtitle: dict[str, Any] = field(default_factory=dict)
    watermark: dict[str, Any] = field(default_factory=dict)
    hdr: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "EncodeParams":
        if not isinstance(data, dict) or not data:
            return cls()
        values: dict[str, Any] = {}
        for f in fields(cls):
            if f.name not in data or data[f.name] is None:
                continue
            raw = data[f.name]
            # default_factory 字段没有类属性，必须按 Field 定义取默认值
            fdef = next(x for x in fields(cls) if x.name == f.name)
            default = fdef.default if fdef.default is not MISSING else fdef.default_factory()
            try:
                if isinstance(default, bool):
                    value = raw if isinstance(raw, bool) else str(raw).strip().lower() in ("1", "true", "yes", "on")
                elif isinstance(default, dict):
                    value = dict(raw) if isinstance(raw, dict) else {}
                elif isinstance(default, float):
                    value = float(raw)
                elif isinstance(default, int):
                    value = int(float(raw))
                else:
                    value = str(raw)
            except (TypeError, ValueError) as exc:
                raise PresetError(f"参数 {f.name} 的值非法: {raw!r}") from exc
            values[f.name] = value
        return cls(**values)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def with_overrides(self, **kwargs) -> "EncodeParams":
        known = {f.name for f in fields(self)}
        clean = {k: v for k, v in kwargs.items() if k in known and v is not None}
        return replace(self, **clean) if clean else replace(self)


@dataclass
class Preset:
    id: str
    category: str
    name: dict[str, str] = field(default_factory=dict)
    note: dict[str, str] = field(default_factory=dict)
    params: EncodeParams = field(default_factory=EncodeParams)
    strategy: dict[str, Any] = field(default_factory=dict)

    def label(self, lang: str = "zh_CN") -> str:
        return self.name.get(lang) or self.name.get("zh_CN") or self.id

    def description(self, lang: str = "zh_CN") -> str:
        return self.note.get(lang) or self.note.get("zh_CN") or ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "category": self.category,
            "name": self.name,
            "note": self.note,
            "params": self.params.to_dict(),
            "strategy": self.strategy,
        }


def even(value: float | int) -> int:
    """H.264/H.265 要求宽高为偶数。"""
    n = max(2, int(round(value)))
    return n if n % 2 == 0 else n - 1


def fit_within(src_w: int, src_h: int, max_w: int, max_h: int) -> tuple[int, int]:
    """等比缩放到 max 框内，不放大。"""
    if src_w <= 0 or src_h <= 0 or max_w <= 0 or max_h <= 0:
        return 0, 0
    ratio = min(max_w / src_w, max_h / src_h, 1.0)
    return even(src_w * ratio), even(src_h * ratio)


def target_bitrate_kbps(
    target_mb: float, duration_s: float, audio_kbps: int = 128, safety: float = 0.96
) -> int:
    """按目标体积和时长反算视频码率。1 MB = 8192 kb。"""
    if target_mb <= 0 or duration_s <= 0:
        raise PresetError("目标体积与视频时长必须大于 0")
    total_kbps = target_mb * 8192.0 / duration_s
    video_kbps = total_kbps * safety - max(0, audio_kbps)
    if video_kbps < 64:
        raise PresetError("目标体积过小，即使只保留最低画质也无法达到，请调大目标体积")
    return int(video_kbps)


def estimate_size_mb(video_kbps: int, audio_kbps: int, duration_s: float) -> float:
    if duration_s <= 0:
        return 0.0
    return (max(0, video_kbps) + max(0, audio_kbps)) * duration_s / 8192.0


_CRF_POINTS = ((14, 1.35), (18, 0.90), (23, 0.55), (28, 0.35), (32, 0.25), (40, 0.12))
_BASE_KBPS_1080P30 = 12000.0


def crf_to_bitrate_kbps(crf: int, width: int, height: int, fps: float) -> int:
    """GPU 编码器没有 CRF 语义时，把 CRF 粗略换算成等效目标码率。"""
    if crf <= _CRF_POINTS[0][0]:
        factor = _CRF_POINTS[0][1]
    elif crf >= _CRF_POINTS[-1][0]:
        factor = _CRF_POINTS[-1][1]
    else:
        factor = _CRF_POINTS[-1][1]
        for (x0, y0), (x1, y1) in zip(_CRF_POINTS, _CRF_POINTS[1:]):
            if x0 <= crf <= x1:
                factor = y0 + (y1 - y0) * (crf - x0) / (x1 - x0)
                break
    w = width or 1920
    h = height or 1080
    f = fps or 30.0
    pixel_rate = (w * h * f) / (1920 * 1080 * 30.0)
    return max(200, int(_BASE_KBPS_1080P30 * factor * pixel_rate))


def apply_strategy(
    params: EncodeParams,
    strategy: dict[str, Any] | None,
    src_w: int = 0,
    src_h: int = 0,
    src_fps: float = 0.0,
) -> EncodeParams:
    """应用极限压缩等组合策略，返回新的参数对象（不修改入参）。"""
    if not strategy:
        return replace(params)
    result = replace(params)

    max_w = int(strategy.get("max_width") or 0)
    max_h = int(strategy.get("max_height") or 0)
    if max_w and max_h and src_w > 0 and src_h > 0:
        w, h = fit_within(src_w, src_h, max_w, max_h)
        result = result.with_overrides(width=w, height=h)
    else:
        result = result.with_overrides(width=even(src_w) if src_w else 0,
                                       height=even(src_h) if src_h else 0)

    fps_cap = float(strategy.get("fps_cap") or 0)
    if fps_cap and src_fps and src_fps > fps_cap:
        result = result.with_overrides(fps=fps_cap)

    for key in ("preset", "abitrate_kbps", "two_pass", "scale_mode", "crf"):
        if key in strategy:
            result = result.with_overrides(**{key: strategy[key]})

    if strategy.get("use_hevc"):
        result = result.with_overrides(codec_family="hevc", vcodec="libx265")
    return result


class PresetLibrary:
    CATEGORIES = ("platform", "device", "quality")

    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path else resource_path("config", "presets.json")
        self.version = ""
        self.updated = ""
        self.disclaimer: dict[str, str] = {}
        self.defaults: dict[str, Any] = {}
        self.presets: list[Preset] = []
        self.reload()

    def reload(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise PresetError(f"无法读取预设库 {self.path}: {exc}") from exc
        if not isinstance(raw, dict) or not isinstance(raw.get("presets"), list):
            raise PresetError("预设库格式非法：缺少 presets 数组")

        self.version = str(raw.get("version", ""))
        self.updated = str(raw.get("updated", ""))
        self.disclaimer = raw.get("disclaimer", {}) or {}
        self.defaults = raw.get("defaults", {}) or {}
        self.presets = []
        for item in raw["presets"]:
            if not isinstance(item, dict) or not item.get("id"):
                continue
            merged = {**self.defaults, **(item.get("params") or {})}
            self.presets.append(
                Preset(
                    id=str(item["id"]),
                    category=str(item.get("category", "custom")),
                    name=item.get("name") or {},
                    note=item.get("note") or {},
                    params=EncodeParams.from_dict(merged),
                    strategy=item.get("strategy") or {},
                )
            )
        _log.info("预设库已加载: %d 条 (v%s)", len(self.presets), self.version)

    def get(self, preset_id: str) -> Preset | None:
        return next((p for p in self.presets if p.id == preset_id), None)

    def by_category(self, category: str) -> list[Preset]:
        return [p for p in self.presets if p.category == category]

    def disclaimer_text(self, lang: str = "zh_CN") -> str:
        return self.disclaimer.get(lang) or self.disclaimer.get("zh_CN") or ""

    def export_to(self, path: str | Path) -> None:
        target = Path(path)
        payload = {
            "version": self.version,
            "updated": self.updated,
            "disclaimer": self.disclaimer,
            "defaults": self.defaults,
            "presets": [
                {**p.to_dict(), "params": {
                    k: v for k, v in p.params.to_dict().items()
                    if v != self.defaults.get(k, None)
                }}
                for p in self.presets
            ],
        }
        target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        _log.info("预设库已导出: %s", target)

    def import_from(self, path: str | Path, merge: bool = True) -> int:
        source = Path(path)
        raw = json.loads(source.read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or not isinstance(raw.get("presets"), list):
            raise PresetError("导入文件格式非法")
        defaults = {**self.defaults, **(raw.get("defaults") or {})}
        incoming: list[Preset] = []
        for item in raw["presets"]:
            if not isinstance(item, dict) or not item.get("id"):
                continue
            merged = {**defaults, **(item.get("params") or {})}
            incoming.append(Preset(
                id=str(item["id"]),
                category=str(item.get("category", "custom")),
                name=item.get("name") or {},
                note=item.get("note") or {},
                params=EncodeParams.from_dict(merged),
                strategy=item.get("strategy") or {},
            ))
        if merge:
            known = {p.id for p in self.presets}
            self.presets.extend(p for p in incoming if p.id not in known)
        else:
            self.presets = incoming
        _log.info("导入预设 %d 条 (merge=%s)", len(incoming), merge)
        return len(incoming)


_library: PresetLibrary | None = None


def preset_library() -> PresetLibrary:
    global _library
    if _library is None:
        _library = PresetLibrary()
    return _library
