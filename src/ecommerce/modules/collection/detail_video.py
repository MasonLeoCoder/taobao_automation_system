"""采集详情主图视频：无视频即跳过，下载实际播放器使用的普通 MP4。"""
import hashlib
import json
import shutil
from http.client import IncompleteRead
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
from uuid import uuid4

from ecommerce.infrastructure.storage.database import now
from ecommerce.modules.collection.detail_storage import product_directory, read_detail_document, save_detail_section
from ecommerce.platforms.taobao.detail import matches_detail_url, resolve_detail_input
from ecommerce.platforms.taobao.detail_video import VIDEO_SELECTOR, detect_main_video
from ecommerce.platforms.taobao.verification import active_guard
from ecommerce.shared.exceptions import NeedsAttention

MAX_VIDEO_BYTES = 512 * 1024 * 1024


def validate_media_url(url):
    try:
        parsed = urlsplit(url)
        host = parsed.hostname or ""
        valid = (parsed.scheme in {"https", "http"} and parsed.username is None and parsed.password is None
                 and parsed.port in {None, 443 if parsed.scheme == "https" else 80}
                 and any(host == domain or host.endswith("." + domain) for domain in ("taobao.com", "tmall.com", "alicdn.com")))
    except ValueError:
        valid = False
    if not valid:
        raise ValueError("视频地址不是当前支持的淘宝/天猫媒体 CDN 直链")
    return url


class _MediaRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, newurl):
        validate_media_url(newurl)
        return super().redirect_request(request, response, code, message, headers, newurl)


def download_video(url, destination, result, guard):
    """单次流式下载；校验响应、MP4 文件头及长度，不自动重试。"""
    validate_media_url(url)
    page, item_id = result.page, result.target.item_id
    temporary = destination.with_suffix(destination.suffix + ".part")
    opener = build_opener(ProxyHandler({}), _MediaRedirect())
    request = Request(url, headers={"Referer": resolve_detail_input(page.url).canonical_url,
                                   "User-Agent": page.evaluate("navigator.userAgent"), "Accept": "video/mp4,*/*;q=0.8"})
    size = 0
    digest = hashlib.sha256()
    try:
        guard.checkpoint("下载主图视频前检查")
        if not matches_detail_url(page, item_id):
            raise NeedsAttention("视频下载前商品页面发生变化")
        with opener.open(request, timeout=15) as response:
            validate_media_url(response.url)
            if response.status != 200:
                raise ValueError("视频响应未返回完整文件")
            content_type = response.headers.get_content_type()
            if content_type not in {"video/mp4", "application/octet-stream", "binary/octet-stream"}:
                raise ValueError("响应不是当前支持的 MP4 视频文件")
            length = response.headers.get("Content-Length")
            expected = int(length) if length and length.isdigit() else None
            if expected is not None and (expected <= 0 or expected > MAX_VIDEO_BYTES):
                raise ValueError("视频文件长度无效或超过下载保护上限")
            head = response.read(32)
            if len(head) < 12 or head[4:8] != b"ftyp":
                raise ValueError("视频响应未通过 MP4 文件头校验")
            destination.parent.mkdir(parents=True, exist_ok=True)
            with temporary.open("xb") as output:
                chunk = head
                while chunk:
                    guard.checkpoint("下载主图视频")
                    if not matches_detail_url(page, item_id):
                        raise NeedsAttention("视频下载期间商品页面发生变化")
                    size += len(chunk)
                    if size > MAX_VIDEO_BYTES:
                        raise ValueError("视频文件超过下载保护上限")
                    output.write(chunk)
                    digest.update(chunk)
                    if hasattr(guard,'note_module_progress'): guard.note_module_progress(counts={'received_bytes':size})
                    chunk = response.read(1024 * 1024)
            if expected is not None and size != expected:
                raise ValueError("视频文件未完整接收")
        guard.checkpoint("复核主图视频文件")
        if not matches_detail_url(page, item_id):
            raise NeedsAttention("视频保存前商品页面发生变化")
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
    return {"video_path": str(destination.resolve()), "video_size_bytes": size,
            "video_sha256": digest.hexdigest(), "content_type": content_type}


def save_video_result(directory, record):
    """保留批次原件，并在商品目录发布最新视频及统一详情 JSON。"""
    product_dir = directory.parent.parent
    item_id = record["product_id"]
    existing = read_detail_document(product_dir, item_id)
    video_path = product_dir / f"{item_id}.mp4"
    if video_path.exists():
        previous = existing.get("video", {})
        with video_path.open("rb") as previous_video:
            previous_sha256 = hashlib.file_digest(previous_video, "sha256").hexdigest()
        if previous.get("video_file") != video_path.name or previous.get("video_sha256") != previous_sha256:
            raise ValueError("现有商品视频与登记信息不一致，未覆盖")
    destination = directory / "video.json"
    directory.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(destination)

    latest = dict(record)
    temporary_video = video_path.with_suffix(f".{record['batch_id']}.mp4.tmp")
    backup_video = video_path.with_suffix(f".{record['batch_id']}.mp4.backup")
    if record["video_path"]:
        source = directory / f"{item_id}.mp4"
        latest.update(video_path=str(video_path.resolve()), video_file=video_path.name)
    else:
        latest["video_file"] = None
    latest["history_record_file"] = destination.relative_to(product_dir).as_posix()
    if record['video_status'] not in {'downloaded','no_video'}:
        record_path=save_detail_section(product_dir,item_id,'video_last_attempt',latest)
        return {'record_path':str(record_path.resolve()),'history_record_path':str(destination.resolve()),**latest}
    replaced_video = False
    try:
        if record["video_path"]:
            shutil.copy2(source, temporary_video)
        if video_path.exists():
            video_path.replace(backup_video)
        if record["video_path"]:
            temporary_video.replace(video_path)
            replaced_video = True
        record_path = save_detail_section(product_dir, item_id, "video", latest)
    except Exception:
        if replaced_video:
            video_path.unlink(missing_ok=True)
        if backup_video.exists():
            backup_video.replace(video_path)
        raise
    finally:
        temporary_video.unlink(missing_ok=True)
    backup_video.unlink(missing_ok=True)
    return {"record_path": str(record_path.resolve()), "history_record_path": str(destination.resolve()), **latest}


def collect_main_video(settings, result):
    """独立详情与后续循环共用此步骤，仅采集用户指定的主图视频。"""
    guard = active_guard()
    if guard is None:
        raise RuntimeError("视频采集必须在淘宝专用守卫中运行")
    value = detect_main_video(result, settings.timeout_ms)
    batch_id = guard.task_id + "-" + uuid4().hex[:8]
    from ecommerce.modules.collection.recovery import register_attempt
    register_attempt(settings,result.target.item_id,'video',batch_id,guard.task_id)
    directory = product_directory(settings, result.target.item_id) / "history" / batch_id
    record = {"schema_version": 1, "batch_id": batch_id, "task_id": guard.task_id,
              "product_id": result.target.item_id, "source_url": resolve_detail_input(result.page.url).canonical_url,
              "captured_at": now(), "scope": "main_gallery_video", "video_present": value["status"] != "no_video",
              "video_status": "no_video" if value["status"] == "no_video" else "detected",
              "video_url": value.get("url"), "duration_seconds": value.get("duration_seconds"),
              "video_path": None, "video_file": None, "video_size_bytes": 0, "video_sha256": None,
              "selector": VIDEO_SELECTOR, "skipped": value["status"] == "no_video", "error": None}
    saved_early=None
    if not record["skipped"]:
        try:
            validate_media_url(record["video_url"])
        except ValueError:
            record.update(video_status="unsupported_source", error="有视频，但当前地址不是可支持的媒体直链；未下载")
        else:
            try:
                record.update(download_video(record["video_url"], directory / f"{result.target.item_id}.mp4", result, guard))
                record["video_file"] = f"{result.target.item_id}.mp4"
                record["video_status"] = "downloaded"
            except HTTPError as error:
                record.update(video_status="download_failed", error=f"视频服务器返回 HTTP {error.code}；未自动重试")
                if error.code in {401,403,429,503}:
                    saved_early=guard.perform('保存视频访问失败记录',lambda:save_video_result(directory,record))
                    guard.request_network_attention(result.page,record['error'],step='检查视频访问限制',retry_after=error.headers.get('Retry-After') if error.headers else None)
            except (URLError, TimeoutError, OSError, ValueError, IncompleteRead) as error:
                record.update(video_status="download_failed", error=f"视频下载或完整性检查失败（{type(error).__name__}）；未自动重试")
    saved = saved_early or guard.perform("保存主图视频结果", lambda: save_video_result(directory, record))
    if saved["video_status"] == "no_video":
        message = "无视频，已跳过主图视频采集"
    elif saved["video_status"] == "downloaded":
        message = "主图视频已下载：" + saved["video_path"]
    else:
        message = saved["error"]
    return {"message": message, **saved}
