"""把 gtab 文档合成成 WAV，供 GTAB 页试听（只用标准库）。

这是**调试用**的试听，不是最终音色：目的是核对音高、节奏、音长、BPM
是不是按写入的内容在走。合成方式很朴素——标准定弦的频率 + 几个谐波 +
指数衰减包络，混音后归一化成 16-bit 单声道 WAV。

播放用 Windows 自带的 ``winsound.PlaySound``（``SND_MEMORY`` 直接放内存里的
WAV，不需要临时文件）。模块本身不依赖 Flet，可以直接单测。
"""

from __future__ import annotations

import io
import math
import os
import tempfile
import wave
from array import array

from ui.gtab_doc import GtabDocument, tick_to_seconds

__all__ = [
    "LEAD_IN_SECONDS",
    "SAMPLE_RATE",
    "STRING_BASE_FREQS",
    "TAIL_SECONDS",
    "fret_frequency",
    "note_timeline",
    "play_wav",
    "preview_seconds",
    "render_notes",
    "render_wav",
    "stop_playback",
    "wav_duration",
]

SAMPLE_RATE = 22050
LEAD_IN_SECONDS = 0.15  # 开头留一点空白，PlaySound 启动时不会吞掉第一个音
TAIL_SECONDS = 0.35  # 结尾留一点余音，避免最后一个音被截断
MAX_SECONDS = 120.0  # 合成上限，防止一张超长谱把内存吃光

# 正在播放的临时 WAV 路径（winsound 不支持内存 + 异步，只能落文件）
_TEMP_PATH: str | None = None

# 标准定弦：1 弦（最细）= E4，6 弦（最粗）= E2。
# 0 品是空弦，每加一品升高一个半音。
STRING_BASE_FREQS: dict[int, float] = {
    1: 329.63,  # E4
    2: 246.94,  # B3
    3: 196.00,  # G3
    4: 146.83,  # D3
    5: 110.00,  # A2
    6: 82.41,  # E2
}

# (谐波倍数, 权重)：基频 + 一点二次/三次谐波，听起来比纯正弦更像拨弦
_HARMONICS = ((1, 1.0), (2, 0.35), (3, 0.15))
_ATTACK_SECONDS = 0.006
_RELEASE_SECONDS = 0.02
_DECAY = 2.2  # 指数衰减时间常数（1/秒）


def fret_frequency(string: int, fret: int) -> float:
    """弦号 + 品 → 频率（Hz）。未知弦号按 6 弦处理，负品按 0 品处理。"""
    base = STRING_BASE_FREQS.get(int(string), STRING_BASE_FREQS[6])
    fret = max(0, int(fret))
    return base * (2.0 ** (fret / 12.0))


def note_timeline(doc: GtabDocument) -> list[tuple[float, float, int, int]]:
    """音符的播放时间轴：``(start_s, end_s, string, fret)``，已含 BPM/Tempo。"""
    return [
        (
            tick_to_seconds(doc, entry.start_tick),
            tick_to_seconds(doc, entry.end_tick),
            entry.string,
            entry.fret,
        )
        for entry in doc.note_pairs()
    ]


def preview_seconds(doc: GtabDocument) -> float:
    """整段试听的长度（秒），已含前后留白，并与合成上限取小。"""
    last = max((end for _, end, _, _ in note_timeline(doc)), default=0.0)
    return min(MAX_SECONDS, LEAD_IN_SECONDS + last + TAIL_SECONDS)


def _write_wav(samples: array, sample_rate: int) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(samples.tobytes())
    return buffer.getvalue()


def render_notes(
    notes: list[tuple[float, float, int, int]],
    seconds: float,
    sample_rate: int = SAMPLE_RATE,
) -> bytes:
    """把 ``(start_s, end_s, 弦, 品)`` 列表渲染成 16-bit 单声道 WAV。

    gtab 页（音符）和 GCT 页（和弦扫弦/拨奏）共用这一个合成器，
    两个页面只是喂进来的时间轴不同。
    """
    sample_rate = max(8000, int(sample_rate))
    total = max(1, int(max(0.0, float(seconds)) * sample_rate))

    # 用 float 混音，最后统一归一化，避免多个音叠加时溢出
    mix = array("f", [0.0]) * total

    for start_s, end_s, string, fret in notes:
        start = int((start_s + LEAD_IN_SECONDS) * sample_rate)
        if start >= total:
            continue
        duration = max(0.03, end_s - start_s)
        length = min(total - start, int(duration * sample_rate))
        if length <= 0:
            continue

        frequency = fret_frequency(string, fret)
        for multiple, weight in _HARMONICS:
            harmonic = frequency * multiple
            if harmonic >= sample_rate / 2:  # 超过奈奎斯特频率就不合成
                continue
            omega = 2.0 * math.pi * harmonic / sample_rate
            phase = 0.0
            for index in range(length):
                t = index / sample_rate
                envelope = math.exp(-_DECAY * t)
                if t < _ATTACK_SECONDS:
                    envelope *= t / _ATTACK_SECONDS
                release_start = duration - _RELEASE_SECONDS
                if t > release_start > 0:
                    envelope *= max(0.0, (duration - t) / _RELEASE_SECONDS)
                mix[start + index] += weight * envelope * math.sin(phase)
                phase += omega

    peak = max((abs(value) for value in mix), default=0.0)
    scale = (0.85 / peak) if peak > 0.85 else 1.0

    pcm = array("h")
    for value in mix:
        pcm.append(int(max(-1.0, min(1.0, value * scale)) * 32767))
    return _write_wav(pcm, sample_rate)


def render_wav(doc: GtabDocument, sample_rate: int = SAMPLE_RATE) -> bytes:
    """把 gtab 文档渲染成 16-bit 单声道 WAV 字节。没有音符时返回一小段静音。"""
    return render_notes(note_timeline(doc), preview_seconds(doc), sample_rate)


def wav_duration(blob: bytes) -> float:
    """读 WAV 头算时长（秒），页面用它驱动播放进度。"""
    with wave.open(io.BytesIO(blob), "rb") as wav:
        rate = wav.getframerate() or SAMPLE_RATE
        return wav.getnframes() / float(rate)


def play_wav(blob: bytes) -> None:
    """异步播放一段 WAV（Windows ``winsound``）。

    注意：``winsound`` **不允许** ``SND_MEMORY`` 与 ``SND_ASYNC`` 同时使用
    （会报 ``Cannot play asynchronously from memory``），所以这里先落到临时
    文件，再用 ``SND_FILENAME | SND_ASYNC`` 播放；停止时删除临时文件。
    """
    try:
        import winsound
    except ImportError as exc:  # pragma: no cover - 非 Windows 才会走到
        raise RuntimeError("当前平台不支持 winsound，无法试听") from exc

    global _TEMP_PATH
    # 先停掉上一段（并清掉它的临时文件），避免两段同时响
    stop_playback()
    fd, path = tempfile.mkstemp(prefix="nte_autoguitar_preview_", suffix=".wav")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(blob)
        winsound.PlaySound(
            path,
            winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT,
        )
    except Exception:
        try:
            os.remove(path)
        except OSError:
            pass
        raise
    _TEMP_PATH = path


def stop_playback() -> None:
    """停止试听、清空播放队列并删除临时文件（幂等，非 Windows 下什么也不做）。"""
    try:
        import winsound
    except ImportError:  # pragma: no cover - 非 Windows
        return
    try:
        winsound.PlaySound(None, winsound.SND_PURGE)
    except RuntimeError:  # pragma: no cover - 没有声音在播时可能抛
        pass
    _cleanup_temp()


def _cleanup_temp() -> None:
    global _TEMP_PATH
    path, _TEMP_PATH = _TEMP_PATH, None
    if not path:
        return
    try:
        os.remove(path)
    except OSError:  # 还被系统占用时留给下一次 play_wav/stop_playback 再清
        pass
