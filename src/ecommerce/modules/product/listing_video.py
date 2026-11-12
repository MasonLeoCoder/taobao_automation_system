"""商品视频的本地比例检查及上传副本。"""
import re
from dataclasses import dataclass
from fractions import Fraction

from ecommerce.infrastructure.media.local import run_ffmpeg

VIDEO_LIMIT = 256 * 1024**2
ADAPTED_VIDEO = '上传视频.mp4'
SQUARE = 1280
ALLOWED_RATIOS = {Fraction(1), Fraction(3, 4), Fraction(9, 16)}


@dataclass(frozen=True)
class VideoInfo:
    width: int
    height: int
    sar: Fraction
    duration: float
    audio: str | None

    @property
    def ratio(self):
        return Fraction(self.width, self.height) * self.sar


def _input(path):
    return ['-xerror', '-protocol_whitelist', 'file', '-f', 'mov', '-i', str(path)]


def inspect_listing_video(path):
    """解码首帧并读取自动旋转后的画面尺寸、像素比例和源轨道。"""
    try:
        text = run_ffmpeg([*_input(path), '-map', '0:V:0', '-vf', 'showinfo', '-frames:v', '1',
                           '-f', 'null', '-'], path.parent, lambda: None, timeout=25, inspect=True)
    except (OSError, ValueError) as error:
        raise ValueError('视频无法读取或解码，请更换视频或将「视频」设为「不提供」') from error
    duration = re.search(r'Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)', text)
    frame = re.search(r'\bn:\s*0\b[^\n]*\bsar:(\d+)/(\d+)\s+s:(\d+)x(\d+)', text)
    if not duration or not frame:
        raise ValueError('无法核对视频的画面比例或时长，请更换视频')
    hours, minutes, seconds = map(float, duration.groups())
    sar_num, sar_den, width, height = map(int, frame.groups())
    audio = re.search(r'\bStream #[^\n]+Audio:\s*([\w-]+)', text.split('Stream mapping:', 1)[0])
    sar = Fraction(sar_num, sar_den) if sar_num and sar_den else Fraction(1)
    return VideoInfo(width, height, sar, hours * 3600 + minutes * 60 + seconds,
                     audio[1] if audio else None)


def _even(value):
    return max(2, 2 * round(value / 2))


def _convert(source, target, info):
    ratio = info.ratio
    width = SQUARE if ratio >= 1 else _even(SQUARE * ratio)
    height = SQUARE if ratio <= 1 else _even(SQUARE / ratio)
    crop_width = min(info.width, _even(info.height / info.sar))
    crop_height = min(info.height, _even(info.width * info.sar))
    filters = (
        '[0:V:0]split=2[original][background];'
        f'[original]scale={width}:{height},setsar=1[front];'
        f'[background]crop={crop_width}:{crop_height},scale=240:240,setsar=1,'
        f'gblur=sigma=9:steps=2,scale={SQUARE}:{SQUARE}:flags=bicubic,'
        'eq=brightness=-0.05:saturation=0.88[canvas];'
        '[canvas][front]overlay=(W-w)/2:(H-h)/2,setsar=1[video]'
    )
    audio_codec = ['-c:a', 'copy'] if info.audio == 'aac' else ['-c:a', 'aac', '-b:a', '192k']
    run_ffmpeg([*_input(source), '-filter_complex_threads', '2', '-filter_complex', filters,
                '-map', '[video]', '-map', '0:a:0?', '-c:v', 'libx264', '-preset', 'fast',
                '-crf', '21', '-maxrate', '6M', '-bufsize', '12M', '-threads', '4',
                '-pix_fmt', 'yuv420p', *audio_codec, '-movflags', '+faststart', str(target)],
               target.parent, lambda: None, timeout=300)


def prepare_listing_video(source, adapted):
    """保留合规视频或生成并核验 1:1 动态背景副本。"""
    info = inspect_listing_video(source)
    if not 5 <= info.duration <= 300:
        raise ValueError(f'视频时长为 {info.duration:g} 秒，千牛要求 5 秒至 5 分钟，请更换视频或选择不提供')
    converted = info.ratio not in ALLOWED_RATIOS
    output = info
    if converted:
        adapted.parent.mkdir()
        try:
            _convert(source, adapted, info)
            output = inspect_listing_video(adapted)
            if (output.ratio != 1 or abs(info.duration - output.duration) > .12
                    or bool(info.audio) != bool(output.audio) or adapted.stat().st_size > VIDEO_LIMIT):
                raise ValueError('副本比例、时长、音轨或大小未通过核验')
        except (OSError, ValueError) as error:
            raise ValueError('视频比例适配失败，请更换视频或将「视频」设为「不提供」') from error
    return {'source_ratio': f'{info.ratio.numerator}:{info.ratio.denominator}',
            'upload_ratio': f'{output.ratio.numerator}:{output.ratio.denominator}',
            'duration': output.duration, 'adapted': converted}
