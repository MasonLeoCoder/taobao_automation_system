"""只处理本地媒体，支持停止、合并与完整时长/轨道核验。"""
import os
import re
import subprocess
import time
from pathlib import Path
from uuid import uuid4


def run_ffmpeg(arguments, directory, checkpoint, *, progress=None, timeout=21600, inspect=False):
    """执行本地FFmpeg；参数：本地参数、停止和进度回调；返回：诊断文本，失败抛出可读异常。"""
    from imageio_ffmpeg import get_ffmpeg_exe
    error_path = directory / ('.media-' + uuid4().hex + '.log')
    progress_path = directory / ('.progress-' + uuid4().hex + '.log')
    process = None
    try:
        with error_path.open('wb') as error:
            command = [get_ffmpeg_exe(), '-hide_banner', '-nostdin', '-y']
            if not inspect:
                command += ['-loglevel', 'error', '-progress', str(progress_path)]
            command += arguments
            process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=error,
                                       creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
            deadline = time.monotonic() + timeout
            while process.poll() is None:
                checkpoint()
                if time.monotonic() > deadline:
                    raise ValueError('媒体处理超时，完整输入轨道已保留，可重试')
                if progress and progress_path.is_file():
                    # FFmpeg进度文件只含本地处理统计，不包含来源地址。
                    with progress_path.open('rb') as stream:
                        stream.seek(max(0, progress_path.stat().st_size - 4096))
                        values = re.findall(rb'out_time_us=(\d+)', stream.read())
                    if values:
                        progress(int(values[-1]) / 1_000_000)
                time.sleep(.15)
            checkpoint()
        text = error_path.read_text(encoding='utf-8', errors='replace')[:131072]
        if process.returncode:
            raise ValueError('媒体合并或校验失败：文件不完整或所选编码不兼容，请重试或选择兼容版本')
        return text
    finally:
        if process is not None and process.poll() is None:
            process.kill()
            process.wait()
        error_path.unlink(missing_ok=True)
        progress_path.unlink(missing_ok=True)


def inspect_media(path, checkpoint, *, duration, video=False, audio=False):
    """检查实际轨道及完整时长并解码开头；参数：文件与预期；返回：实际时长。"""
    maps = (['-map', '0:v:0'] if video else []) + (['-map', '0:a:0'] if audio else [])
    text = run_ffmpeg(['-i', str(path), *maps, '-t', '0.1', '-f', 'null', '-'], path.parent, checkpoint, timeout=90, inspect=True)
    match = re.search(r'Duration: (\d+):(\d+):(\d+(?:\.\d+)?)', text)
    if not match:
        raise ValueError('无法核对媒体的完整时长，未发布该文件')
    actual = int(match[1]) * 3600 + int(match[2]) * 60 + float(match[3])
    if not actual or duration and abs(actual - duration) > max(3, duration * .02):
        raise ValueError('实际文件时长与所选分集不一致，未将片段当作完整视频')
    return actual


def merge(video, audio, target, checkpoint, duration, progress=None):
    """复制编码合并音画；参数：已完整轨道、输出和回调；返回：无。"""
    args = ['-i', str(video)]
    if audio:
        args += ['-i', str(audio), '-map', '0:v:0', '-map', '1:a:0']
    else:
        args += ['-map', '0:v:0', '-map', '0:a:0']
    run_ffmpeg([*args, '-c', 'copy', '-movflags', '+faststart', '-f', 'mp4', str(target)], target.parent, checkpoint, progress=progress)
    inspect_media(target, checkpoint, duration=duration, video=True, audio=True)


def extract_audio(source, target, checkpoint, duration, *, copy=True, progress=None):
    """生成兼容M4A；参数：来源和是否可复制AAC；返回：无。"""
    codec = ['-c:a', 'copy'] if copy else ['-c:a', 'aac', '-b:a', '192k']
    run_ffmpeg(['-i', str(source), '-map', '0:a:0', '-vn', *codec, '-movflags', '+faststart', '-f', 'ipod', str(target)],
               target.parent, checkpoint, progress=progress)
    inspect_media(target, checkpoint, duration=duration, audio=True)
