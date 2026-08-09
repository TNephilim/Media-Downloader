import json
import os
import queue
import re
import shutil
import ssl
import subprocess
import sys
import threading
import tempfile
import time
from pathlib import Path
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen

APP_NAME = "Media Downloader"
APP_VERSION = "1.2.2"
TWITIGER_EXTRACT_URL = "https://twitiger.com/api/extract?url="
DOWNLOAD_DIR = Path.home() / "Downloads"
PLAYLIST_STATE_DIRECTORY = ".media-downloader"
PLAYLIST_MANIFEST_FILENAME = "playlist.json"
PLAYLIST_VIDEO_ARCHIVE_FILENAME = "completed-video-entries.txt"
RATE_LIMIT_BACKOFF_SECONDS = (15, 45)
MAX_VIDEO_FORMAT = "bv*[height<=1080][fps<=60]+ba/b[height<=1080][fps<=60]"
URL_PATTERN = re.compile(r"https?://[^\s<>\"]+")
_YT_DLP = None
_YT_DLP_UTILS = None
_IMAGEIO_FFMPEG = None
MEDIA_SOURCE_SUFFIXES = {
    ".aac",
    ".flac",
    ".m4a",
    ".mkv",
    ".mov",
    ".mp3",
    ".mp4",
    ".ogg",
    ".opus",
    ".wav",
    ".webm",
}
VIDEO_SOURCE_SUFFIXES = {".mp4", ".webm", ".mkv", ".mov"}
def extract_urls(text):
    urls = []
    for match in URL_PATTERN.findall(text):
        url = normalize_media_url(match.rstrip(").,;]'\""))
        if url not in urls:
            urls.append(url)
    return urls


def extract_local_files(text, urls):
    files = []
    url_set = set(urls)
    for line in text.splitlines():
        candidate = line.strip().strip("{}\"'")
        if not candidate or candidate in url_set or URL_PATTERN.fullmatch(candidate):
            continue
        path = Path(candidate)
        if path.is_file() and path not in files:
            files.append(path)
    return files


def inspect_output_name(input_text, collection_progress_callback=None):
    urls = extract_urls(input_text)
    local_files = extract_local_files(input_text, urls)
    if len(local_files) == 1 and not urls:
        return local_files[0].name, None, format_local_media_details(local_files[0]), []
    if len(urls) != 1 or local_files:
        return None, "Paste one video link or choose one media file to load a default filename.", None, []

    if is_x_or_twitter_url(urls[0]):
        fallback = extract_twitter_fallback(urls[0])
        if fallback:
            quality = fallback.get("quality") or probe_media_quality(fallback["video_url"])
            return fallback_output_name(fallback), None, quality or "No video detected", []

    try:
        info = extract_download_info(urls[0], collection_progress_callback)
    except Exception:
        fallback = extract_twitter_fallback(urls[0]) if is_x_or_twitter_url(urls[0]) else None
        if not fallback:
            raise
        quality = fallback.get("quality") or probe_media_quality(fallback["video_url"])
        return fallback_output_name(fallback), None, quality or "No video detected", []
    if collection_title(info):
        summary, entries = format_playlist_details(info)
        folder_name = safe_folder_name(str(collection_title(info)))
        if find_resumable_playlist_directory(urls[0], folder_name):
            summary = f"{summary} · Resume available"
        return folder_name, None, summary, entries
    title = safe_folder_name(str(info.get("title") or "Media Download"))
    return f"{title}.mp4", None, format_media_details(info), []


def format_duration(seconds):
    if seconds is None:
        return None
    try:
        total = max(0, round(float(seconds)))
    except (TypeError, ValueError):
        return None
    hours, remainder = divmod(total, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes}:{seconds:02d}"


def format_media_details(info):
    duration = format_duration(info.get("duration")) if isinstance(info, dict) else None
    quality = format_quality_preview(info)
    estimated_size = format_estimated_size(estimate_media_size_bytes(info))
    return " · ".join(part for part in (duration, quality, estimated_size) if part and part != "No video detected") or "No video detected"


def format_local_media_details(path):
    duration = format_duration(media_duration_seconds(path))
    quality = probe_media_quality(str(path))
    return " · ".join(part for part in (duration, quality) if part) or "No video detected"


def format_playlist_details(info):
    entries = [entry for entry in (info.get("entries") or []) if isinstance(entry, dict)]
    total_duration = sum(float(entry.get("duration") or 0) for entry in entries)
    declared_count = positive_int(info.get("playlist_count") or info.get("n_entries"))
    entry_count = declared_count or len(entries)
    summary_parts = [f"{entry_count} videos"]
    if total_duration:
        summary_parts.append(f"{format_duration(total_duration)} total")
    total_size = sum(filter(None, (estimate_media_size_bytes(entry) for entry in entries)))
    estimated_size = format_estimated_size(total_size)
    if estimated_size:
        summary_parts.append(estimated_size)
    summary_parts.append("Quality varies by entry")
    details = []
    for index, entry in enumerate(entries, start=1):
        title = safe_folder_name(str(entry.get("title") or f"Video {index}"))
        details.append(f"{index}. {title} · {format_media_details(entry)}")
    return " · ".join(summary_parts), details


def format_quality_preview(info):
    formats = info.get("formats") if isinstance(info, dict) else None
    if not isinstance(formats, list):
        height = info.get("height") if isinstance(info, dict) else None
        width = info.get("width") if isinstance(info, dict) else None
        fps = info.get("fps") if isinstance(info, dict) else None
        bitrate = info.get("tbr") or info.get("vbr") if isinstance(info, dict) else None
        resolution = f"{height}p" if height else (f"{width}px" if width else None)
        parts = [resolution]
        if fps:
            parts.append(f"{float(fps):g} fps")
        if bitrate:
            parts.append(f"{float(bitrate) / 1000:g} Mbps")
        return " · ".join(part for part in parts if part) or "No video detected"

    best = best_video_format(formats)
    if not best:
        return "Audio only"

    height = best.get("height")
    width = best.get("width")
    resolution = f"{height}p" if height else f"{width}px"
    fps = best.get("fps")
    bitrate = best.get("tbr") or best.get("vbr")
    parts = [resolution]
    if fps:
        parts.append(f"{fps:g} fps")
    if bitrate:
        parts.append(f"{bitrate / 1000:g} Mbps")
    return " - ".join(parts)


def best_video_format(formats):
    video_formats = [
        item for item in formats
        if isinstance(item, dict) and item.get("vcodec") not in (None, "none") and (item.get("height") or item.get("width"))
    ]
    return max(
        video_formats,
        key=lambda item: (
            item.get("height") or 0,
            item.get("width") or 0,
            item.get("fps") or 0,
            item.get("tbr") or item.get("vbr") or 0,
        ),
        default=None,
    )


def estimate_media_size_bytes(info):
    if not isinstance(info, dict):
        return None

    formats = info.get("formats")
    duration = info.get("duration")
    if not isinstance(formats, list):
        return estimate_format_size_bytes(info, duration)

    video = best_video_format(formats)
    if not video:
        return estimate_format_size_bytes(info, duration)

    selected_formats = [video]
    if video.get("acodec") in (None, "none"):
        audio_formats = [
            item for item in formats
            if isinstance(item, dict) and item.get("acodec") not in (None, "none") and item.get("vcodec") in (None, "none")
        ]
        best_audio = max(audio_formats, key=lambda item: item.get("abr") or item.get("tbr") or 0, default=None)
        if best_audio:
            selected_formats.append(best_audio)

    estimates = [estimate_format_size_bytes(media_format, duration) for media_format in selected_formats]
    if not any(estimates):
        return None
    return sum(size for size in estimates if size)


def estimate_format_size_bytes(media_format, duration):
    for key in ("filesize", "filesize_approx"):
        size = media_format.get(key)
        if isinstance(size, (int, float)) and size > 0:
            return int(size)
    try:
        seconds = float(duration)
        bitrate_kbps = float(media_format.get("tbr") or media_format.get("vbr") or media_format.get("abr") or 0)
    except (TypeError, ValueError):
        return None
    return int(seconds * bitrate_kbps * 1000 / 8) if seconds > 0 and bitrate_kbps > 0 else None


def format_estimated_size(size_bytes):
    if not isinstance(size_bytes, (int, float)) or size_bytes <= 0:
        return None
    units = ("B", "KB", "MB", "GB", "TB")
    value = float(size_bytes)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            precision = 0 if unit == "B" else (2 if value < 10 else 1)
            return f"~{value:.{precision}f} {unit}"
        value /= 1024


def probe_media_quality(source):
    try:
        completed = subprocess.run(
            [str(find_ffmpeg_exe()), "-hide_banner", "-i", source],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=8,
            startupinfo=hidden_startupinfo(),
            creationflags=hidden_creationflags(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return None

    output = completed.stderr
    video_line = next((line for line in output.splitlines() if "Video:" in line), "")
    dimensions = re.search(r"(\d{2,5})x(\d{2,5})", video_line)
    if not dimensions:
        return None
    width, height = dimensions.groups()
    fps_match = re.search(r"(\d+(?:\.\d+)?)\s*fps", video_line)
    parts = [f"{height}p"]
    if fps_match:
        parts.append(f"{float(fps_match.group(1)):g} fps")
    bitrate_match = re.search(r"bitrate:\s*(\d+(?:\.\d+)?)\s*kb/s", output)
    if bitrate_match:
        parts.append(f"{float(bitrate_match.group(1)) / 1000:g} Mbps")
    return " - ".join(parts)


def normalize_media_url(url):
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    if host == "x.com" or host.endswith(".x.com"):
        return url.replace(parsed.netloc, "twitter.com", 1)
    return url


def validate_inputs(urls, local_files):
    if not urls and not local_files:
        return "Paste one valid web link or choose a media file."
    if len(urls) > 1:
        return "Only one web link can be downloaded at a time. Use a playlist link for multiple videos."

    for url in urls:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return f"Invalid link: {url}"
        host = parsed.netloc.lower()
        if host == "spotify.com" or host.endswith(".spotify.com"):
            return (
                "Spotify downloads are not supported. Spotify Premium offline downloads "
                "must be done inside the Spotify app."
            )
    for file_path in local_files:
        if file_path.suffix.lower() not in LOCAL_FILE_SUFFIXES:
            return f"Unsupported file type: {file_path.name}"
    return None


def get_yt_dlp():
    global _YT_DLP
    configure_bundled_tools()
    if _YT_DLP is None:
        import yt_dlp

        _YT_DLP = yt_dlp
    return _YT_DLP


def configure_bundled_tools():
    if getattr(sys, "frozen", False):
        tools_directory = Path(getattr(sys, "_MEIPASS", ""))
    else:
        tools_directory = Path(__file__).resolve().parent.parent / "tools"

    phantomjs = tools_directory / "phantomjs.exe"
    if phantomjs.is_file():
        path_entries = os.environ.get("PATH", "").split(os.pathsep)
        if str(tools_directory) not in path_entries:
            os.environ["PATH"] = f"{tools_directory}{os.pathsep}{os.environ.get('PATH', '')}"


def get_ytdlp_utils():
    global _YT_DLP_UTILS
    if _YT_DLP_UTILS is None:
        import yt_dlp.utils

        _YT_DLP_UTILS = yt_dlp.utils
    return _YT_DLP_UTILS


def get_download_cancelled():
    return get_ytdlp_utils().DownloadCancelled


def get_imageio_ffmpeg():
    global _IMAGEIO_FFMPEG
    if _IMAGEIO_FFMPEG is None:
        import imageio_ffmpeg

        _IMAGEIO_FFMPEG = imageio_ffmpeg
    return _IMAGEIO_FFMPEG


def make_ydl_options(
    progress_hook,
    output_template,
    allow_playlists=True,
    format_selector=MAX_VIDEO_FORMAT,
    format_sort=None,
    merge_output_format="mp4",
    source_url=None,
    download_archive=None,
):
    ffmpeg_exe = find_ffmpeg_exe()
    options = {
        "format": format_selector,
        "outtmpl": str(output_template),
        "ffmpeg_location": str(ffmpeg_exe),
        "noplaylist": not allow_playlists,
        "quiet": True,
        "no_warnings": True,
        "progress_hooks": [progress_hook],
        "windowsfilenames": True,
    }
    if format_sort:
        options["format_sort"] = format_sort
    if merge_output_format:
        options["merge_output_format"] = merge_output_format
    if download_archive:
        options["download_archive"] = str(download_archive)
        options["continuedl"] = True
        options["nopart"] = False
    add_site_request_options(options, source_url)
    return options


def is_rate_limited_error(error):
    message = str(error).lower()
    return "429" in message or "too many requests" in message or "rate limit" in message


def wait_for_retry(seconds, cancel_event, status_callback, attempt):
    remaining = int(seconds)
    while remaining > 0:
        check_cancelled(cancel_event)
        status_callback(f"Rate limited. Waiting {remaining}s before retry {attempt}/{len(RATE_LIMIT_BACKOFF_SECONDS)}...")
        time.sleep(1)
        remaining -= 1


def download_with_rate_limit(options, url, cancel_event, status_callback):
    for attempt, delay in enumerate((*RATE_LIMIT_BACKOFF_SECONDS, None), start=1):
        check_cancelled(cancel_event)
        try:
            with get_yt_dlp().YoutubeDL(options) as ydl:
                code = ydl.download([url])
            if code:
                raise RuntimeError("One or more downloads failed.")
            return
        except Exception as exc:
            if delay is None or not is_rate_limited_error(exc):
                raise
            wait_for_retry(delay, cancel_event, status_callback, attempt)


def is_pornhub_url(url):
    host = (urlparse(url).hostname or "").lower()
    return host == "pornhub.com" or host.endswith(".pornhub.com")


def add_site_request_options(options, url):
    if not url or not is_pornhub_url(url):
        return

    # This site can return a JavaScript verification page to a plain Python
    # request. Use yt-dlp's supported browser profile instead of PhantomJS.
    from yt_dlp.networking.impersonate import ImpersonateTarget

    options["impersonate"] = ImpersonateTarget.from_str("chrome-110:windows-10")


def download_best_video(urls, progress_hook, status_callback, cancel_event, output_name=None):
    saved_locations = []
    for url in urls:
        check_cancelled(cancel_event)
        target = resolve_download_target(url, status_callback, "video", output_name)
        check_cancelled(cancel_event)
        output_template = target["directory"] / output_filename_template(target["is_collection"], output_name)
        options = make_ydl_options(
            progress_hook,
            output_template,
            format_selector=MAX_VIDEO_FORMAT,
            format_sort=["res", "fps", "br"],
            merge_output_format="mp4",
            source_url=target["url"],
            download_archive=target.get("archive"),
        )

        if target["is_collection"]:
            action = "Resuming" if target.get("is_resume") else "Downloading"
            status_callback(f"{action} {target['name']} into {target['directory'].name}...")
        else:
            status_callback("Downloading best available video...")

        try:
            download_with_rate_limit(options, target["url"], cancel_event, status_callback)
        except Exception:
            fallback = extract_twitter_fallback(target["url"]) if is_x_or_twitter_url(target["url"]) else None
            if not fallback:
                raise
            status_callback("Trying alternate video source...")
            fallback_name = custom_output_stem(output_name) if output_name else custom_output_stem(fallback_output_name(fallback))
            download_direct_media(fallback["video_url"], target["directory"] / f"{fallback_name}.mp4", progress_hook)
        saved_locations.append(target["directory"])
    return format_saved_locations(saved_locations)


def process_local_files(files, mode, status_callback, cancel_event, conversion_callback=None, output_name=None):
    outputs = []
    total = len(files)
    for index, file_path in enumerate(files, start=1):
        check_cancelled(cancel_event)
        suffix = file_path.suffix.lower()

        if mode == "fix-video":
            if suffix not in VIDEO_SOURCE_SUFFIXES:
                raise RuntimeError(f"{file_path.name} is not a supported video file to repair.")
            status_callback(f"Repairing video {index}/{total}...")
            outputs.append(repair_video_for_playback(file_path, replace_original=False, output_name=output_name))
        elif mode == "video":
            if suffix == ".gif":
                conversion_status = f"Converting GIF to video {index}/{total}..."
                status_callback(conversion_status)
                outputs.append(
                    convert_gif_to_video(
                        file_path,
                        file_path.parent,
                        progress_callback=(lambda percent, status=conversion_status: conversion_callback(percent, f"{status} {percent:.0f}%")) if conversion_callback else None,
                        cancel_event=cancel_event,
                    )
                )
            elif suffix in VIDEO_SOURCE_SUFFIXES:
                raise RuntimeError(f"{file_path.name} is already a video. Use Fix Video to repair it.")
            else:
                raise RuntimeError(f"{file_path.name} cannot be converted or repaired as video.")
        elif mode == "gif":
            if suffix not in VIDEO_SOURCE_SUFFIXES:
                raise RuntimeError(f"{file_path.name} is not a supported video file for GIF conversion.")
            conversion_status = f"Converting to GIF {index}/{total}...{gif_size_warning(file_path)}"
            status_callback(conversion_status)
            def report_local_progress(percent, stage=None, status=conversion_status):
                display_status = stage or status
                if percent is not None:
                    display_status = f"{display_status} {percent:.0f}%"
                conversion_callback(percent, display_status)

            outputs.append(
                convert_source_to_gif(
                    file_path,
                    file_path.parent,
                    delete_source=False,
                    progress_callback=report_local_progress if conversion_callback else None,
                    cancel_event=cancel_event,
                    output_name=output_name,
                )
            )
        elif mode in {"frames", "frame-first", "frames-similar"}:
            if suffix not in FRAME_SOURCE_SUFFIXES:
                raise RuntimeError(f"{file_path.name} is not a supported video or GIF file for frame extraction.")
            extraction_status = "Removing similar frames..." if mode == "frames-similar" else (f"Extracting first frame {index}/{total}..." if mode == "frame-first" else f"Extracting frames {index}/{total}...")
            status_callback(extraction_status)
            extract = extract_first_frame if mode == "frame-first" else (extract_similar_frames if mode == "frames-similar" else extract_unique_frames)
            outputs.append(
                extract(
                    file_path,
                    DOWNLOAD_DIR,
                    progress_callback=(lambda percent, status=extraction_status: conversion_callback(percent, f"{status} {percent:.0f}%", "extracting")) if conversion_callback else None,
                    cancel_event=cancel_event,
                    output_name=output_name,
                )
            )
        else:
            if suffix == ".mp3":
                raise RuntimeError(f"{file_path.name} is already an MP3 file.")
            if suffix not in MEDIA_SOURCE_SUFFIXES and suffix != ".gif":
                raise RuntimeError(f"{file_path.name} is not a supported media file for audio extraction.")
            conversion_status = f"Converting to MP3 {index}/{total}..."
            status_callback(conversion_status)
            outputs.append(
                convert_source_to_mp3(
                    file_path,
                    file_path.parent,
                    delete_source=False,
                    progress_callback=(lambda percent, status=conversion_status: conversion_callback(percent, f"{status} {percent:.0f}%")) if conversion_callback else None,
                    cancel_event=cancel_event,
                    output_name=output_name,
                )
            )

    return ", ".join(str(output) for output in outputs)


def download_best_gifs(urls, progress_hook, status_callback, cancel_event, conversion_callback=None, output_name=None):
    saved = 0
    saved_locations = []
    for url in urls:
        check_cancelled(cancel_event)
        target = resolve_download_target(url, status_callback, "gif", output_name)
        check_cancelled(cancel_event)
        with tempfile.TemporaryDirectory(prefix="media-downloader-") as temp_dir:
            temp_dir_path = Path(temp_dir)
            used_twitter_fallback = False
            temp_template = temp_dir_path / output_source_filename_template(target["is_collection"])
            options = make_ydl_options(
                progress_hook,
                temp_template,
                format_selector=MAX_VIDEO_FORMAT,
                format_sort=["res", "fps", "br"],
                merge_output_format=None,
                source_url=target["url"],
            )

            if target["is_collection"]:
                status_callback(f"Downloading {target['name']} source media into {target['directory'].name}...")
            else:
                status_callback("Downloading source media for GIF conversion...")

            try:
                download_with_rate_limit(options, target["url"], cancel_event, status_callback)
            except Exception:
                fallback = extract_twitter_fallback(target["url"]) if is_x_or_twitter_url(target["url"]) else None
                if not fallback:
                    raise
                status_callback("Trying alternate video source for GIF conversion...")
                fallback_stem = custom_output_stem(fallback_output_name(fallback))
                download_direct_media(fallback["video_url"], temp_dir_path / f"{fallback_stem}.source.mp4", progress_hook)
                used_twitter_fallback = True

            source_files = sorted(
                path
                for path in temp_dir_path.rglob("*")
                if path.is_file() and path.suffix.lower() in VIDEO_SOURCE_SUFFIXES | {".gif"}
            )
            if not source_files:
                raise RuntimeError("No downloaded media files were found for GIF saving or conversion.")

            if used_twitter_fallback:
                status_callback("Preparing video for GIF conversion...")
                source_files = [
                    remux_video_lossless(source, replace_original=True) if source.suffix.lower() != ".gif" else source
                    for source in source_files
                ]

            total = len(source_files)
            for index, source in enumerate(source_files, start=1):
                check_cancelled(cancel_event)
                if source.suffix.lower() == ".gif":
                    status_callback(f"Saving GIF {index}/{total}...")
                    save_existing_gif(
                        source,
                        target["directory"],
                        output_name if not target["is_collection"] else None,
                    )
                else:
                    conversion_status = f"Converting to GIF {index}/{total}...{gif_size_warning(source)}"
                    status_callback(conversion_status)
                    item_offset = (index - 1) / total * 100
                    item_scale = 100 / total
                    def report_item_progress(percent, stage=None, offset=item_offset, scale=item_scale, status=conversion_status):
                        overall_percent = None if percent is None else offset + percent * scale / 100
                        display_status = stage or status
                        if overall_percent is not None:
                            display_status = f"{display_status} {overall_percent:.0f}%"
                        conversion_callback(overall_percent, display_status)

                    convert_source_to_gif(
                        source,
                        target["directory"],
                        progress_callback=report_item_progress if conversion_callback else None,
                        cancel_event=cancel_event,
                        output_name=None if target["is_collection"] else output_name,
                    )
                saved += 1
            saved_locations.append(target["directory"])

    return f"{format_saved_locations(saved_locations)} ({saved} GIF file{'s' if saved != 1 else ''})"


def download_best_frames(urls, progress_hook, status_callback, cancel_event, conversion_callback=None, first_frame=False, similar_frames=False, output_name=None):
    extracted = 0
    saved_locations = []
    for url in urls:
        check_cancelled(cancel_event)
        target = resolve_download_target(url, status_callback, "frames", output_name)
        check_cancelled(cancel_event)
        with tempfile.TemporaryDirectory(prefix="media-downloader-") as temp_dir:
            temp_dir_path = Path(temp_dir)
            temp_template = temp_dir_path / output_source_filename_template(target["is_collection"])
            options = make_ydl_options(
                progress_hook,
                temp_template,
                format_selector=MAX_VIDEO_FORMAT,
                format_sort=["res", "fps", "br"],
                merge_output_format=None,
                source_url=target["url"],
            )

            if target["is_collection"]:
                status_callback(f"Downloading {target['name']} source media for frame extraction...")
            else:
                status_callback("Downloading source media for frame extraction...")

            try:
                download_with_rate_limit(options, target["url"], cancel_event, status_callback)
            except Exception:
                fallback = extract_twitter_fallback(target["url"]) if is_x_or_twitter_url(target["url"]) else None
                if not fallback:
                    raise
                status_callback("Trying alternate video source for frame extraction...")
                download_direct_media(fallback["video_url"], temp_dir_path / f"{custom_output_stem(fallback_output_name(fallback))}.source.mp4", progress_hook)

            source_files = sorted(
                path
                for path in temp_dir_path.rglob("*")
                if path.is_file() and path.suffix.lower() in FRAME_SOURCE_SUFFIXES
            )
            if not source_files:
                raise RuntimeError("No downloaded video files were found for frame extraction.")

            total = len(source_files)
            for index, source in enumerate(source_files, start=1):
                check_cancelled(cancel_event)
                extraction_status = "Removing similar frames..." if similar_frames else (f"Extracting first frame {index}/{total}..." if first_frame else f"Extracting frames {index}/{total}...")
                status_callback(extraction_status)
                extract = extract_first_frame if first_frame else (extract_similar_frames if similar_frames else extract_unique_frames)
                extract(
                    source,
                    target["directory"],
                    progress_callback=(lambda percent, status=extraction_status: conversion_callback(percent, f"{status} {percent:.0f}%", "extracting")) if conversion_callback else None,
                    cancel_event=cancel_event,
                    output_name=None if target["is_collection"] else output_name,
                )
                extracted += 1
            saved_locations.append(target["directory"])

    return f"{format_saved_locations(saved_locations)} ({extracted} frame folder{'s' if extracted != 1 else ''})"


def download_best_mp3s(urls, progress_hook, status_callback, cancel_event, conversion_callback=None, output_name=None):
    converted = 0
    saved_locations = []
    for url in urls:
        check_cancelled(cancel_event)
        target = resolve_download_target(url, status_callback, "audio", output_name)
        check_cancelled(cancel_event)
        with tempfile.TemporaryDirectory(prefix="media-downloader-") as temp_dir:
            temp_dir_path = Path(temp_dir)
            temp_template = temp_dir_path / output_source_filename_template(target["is_collection"])
            options = make_ydl_options(
                progress_hook,
                temp_template,
                format_selector="ba/bestaudio/b[height<=1080][fps<=60]",
                merge_output_format=None,
                source_url=target["url"],
            )

            if target["is_collection"]:
                status_callback(f"Downloading {target['name']} audio into {target['directory'].name}...")
            else:
                status_callback("Downloading audio for MP3 conversion...")

            try:
                download_with_rate_limit(options, target["url"], cancel_event, status_callback)
            except Exception:
                fallback = extract_twitter_fallback(target["url"]) if is_x_or_twitter_url(target["url"]) else None
                if not fallback:
                    raise
                status_callback("Trying alternate video source for audio conversion...")
                download_direct_media(fallback["video_url"], temp_dir_path / f"{custom_output_stem(fallback_output_name(fallback))}.source.mp4", progress_hook)

            source_files = sorted(
                path
                for path in temp_dir_path.rglob("*")
                if path.is_file() and path.suffix.lower() in MEDIA_SOURCE_SUFFIXES
            )
            if not source_files:
                raise RuntimeError("No downloaded media files were found to convert.")

            total = len(source_files)
            for index, source in enumerate(source_files, start=1):
                check_cancelled(cancel_event)
                conversion_status = f"Converting to MP3 {index}/{total}..."
                status_callback(conversion_status)
                convert_source_to_mp3(
                    source,
                    target["directory"],
                    progress_callback=(lambda percent, status=conversion_status: conversion_callback(percent, f"{status} {percent:.0f}%")) if conversion_callback else None,
                    cancel_event=cancel_event,
                    output_name=None if target["is_collection"] else output_name,
                )
                converted += 1
            saved_locations.append(target["directory"])

    return f"{format_saved_locations(saved_locations)} ({converted} MP3 file{'s' if converted != 1 else ''})"


def output_filename_template(is_collection, output_name=None):
    if is_collection:
        return "%(playlist_index)03d - %(title).120B.%(ext)s"
    if output_name:
        return f"{custom_output_stem(output_name)}.%(ext)s"
    return "%(title).120B.%(ext)s"


def output_source_filename_template(is_collection):
    if is_collection:
        return "%(playlist_index)03d - %(title).120B.source.%(ext)s"
    return "%(title).120B.source.%(ext)s"


def check_cancelled(cancel_event):
    if cancel_event.is_set():
        raise get_download_cancelled()("Download cancelled.")


def resolve_download_target(url, status_callback, mode, output_name=None):
    normalized_url = normalize_media_url(url)
    if not is_probable_collection_url(normalized_url):
        return fast_download_target(normalized_url)

    if is_youtube_playlist_url(normalized_url):
        return resolve_legacy_youtube_playlist_target(normalized_url, status_callback, mode, output_name)

    # Reuse the previewed collection name so the real download can go straight
    # through yt-dlp's playlist path without another full metadata scan.
    if output_name:
        folder_name = custom_folder_name(output_name)
        return prepare_playlist_target(normalized_url, folder_name, mode == "video")

    status_callback("Scanning link details...")
    try:
        info = extract_download_info(normalized_url)
    except Exception as exc:
        raise RuntimeError(media_detection_error(normalized_url, mode, exc)) from exc

    if not has_media_for_mode(info, mode):
        raise RuntimeError(no_media_message(normalized_url, mode))

    collection_name = collection_title(info)
    if collection_name:
        folder_name = custom_folder_name(output_name) if output_name else safe_folder_name(collection_name)
        target = prepare_playlist_target(normalized_url, folder_name, mode == "video")
        target["name"] = collection_name
        return target
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    return {"directory": DOWNLOAD_DIR, "is_collection": False, "name": None, "url": normalized_url}


def resolve_legacy_youtube_playlist_target(url, status_callback, mode, output_name=None):
    # Keep YouTube playlists on the original pre-Photino route: one playlist
    # read when Download is clicked, then yt-dlp performs the normal download.
    status_callback("Scanning link details...")
    try:
        info = extract_legacy_youtube_playlist_info(url)
    except Exception as exc:
        raise RuntimeError(media_detection_error(url, mode, exc)) from exc

    if not has_media_for_mode(info, mode):
        raise RuntimeError(no_media_message(url, mode))

    collection_name = collection_title(info)
    if collection_name:
        folder_name = custom_folder_name(output_name) if output_name else safe_folder_name(collection_name)
        target = prepare_playlist_target(url, folder_name, mode == "video")
        target["name"] = collection_name
        return target

    return fast_download_target(url)


def fast_download_target(url):
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    return {"directory": DOWNLOAD_DIR, "is_collection": False, "name": None, "url": url}


def playlist_state_path(directory):
    return Path(directory) / PLAYLIST_STATE_DIRECTORY


def playlist_manifest_path(directory):
    return playlist_state_path(directory) / PLAYLIST_MANIFEST_FILENAME


def hide_from_windows(path):
    if os.name != "nt":
        return
    try:
        import ctypes

        ctypes.windll.kernel32.SetFileAttributesW(str(path), 0x2)
    except Exception:
        pass


def read_playlist_manifest(directory):
    try:
        with playlist_manifest_path(directory).open("r", encoding="utf-8") as file:
            manifest = json.load(file)
    except (OSError, ValueError, TypeError):
        return None
    return manifest if isinstance(manifest, dict) else None


def has_legacy_playlist_files(directory):
    try:
        return any(re.match(r"^\d{3} - .+", child.name) for child in directory.iterdir())
    except OSError:
        return False


def find_resumable_playlist_directory(url, fallback_folder_name=None):
    normalized_url = normalize_media_url(url)
    if not DOWNLOAD_DIR.is_dir():
        return None
    try:
        directories = list(DOWNLOAD_DIR.iterdir())
    except OSError:
        return None
    for directory in directories:
        if not directory.is_dir():
            continue
        manifest = read_playlist_manifest(directory)
        if manifest and manifest.get("url") == normalized_url:
            return directory
    if fallback_folder_name:
        legacy_directory = DOWNLOAD_DIR / fallback_folder_name
        if legacy_directory.is_dir() and has_legacy_playlist_files(legacy_directory):
            return legacy_directory
    return None


def prepare_playlist_target(url, folder_name, allow_resume):
    normalized_url = normalize_media_url(url)
    existing_directory = find_resumable_playlist_directory(normalized_url, folder_name) if allow_resume else None
    is_resume = existing_directory is not None
    directory = existing_directory or unique_path(DOWNLOAD_DIR / folder_name)
    directory.mkdir(parents=True, exist_ok=True)

    state_directory = playlist_state_path(directory)
    state_directory.mkdir(exist_ok=True)
    manifest_path = playlist_manifest_path(directory)
    if not manifest_path.exists():
        manifest_path.write_text(json.dumps({"url": normalized_url}, ensure_ascii=True), encoding="utf-8")
    hide_from_windows(state_directory)

    target = {"directory": directory, "is_collection": True, "name": folder_name, "url": normalized_url, "is_resume": is_resume}
    if allow_resume:
        target["archive"] = state_directory / PLAYLIST_VIDEO_ARCHIVE_FILENAME
    return target


def is_probable_collection_url(url):
    parsed = urlparse(url)
    path = parsed.path.lower()
    query = parsed.query.lower()

    if "list=" in query:
        return True
    collection_markers = (
        "/playlist",
        "/playlists",
        "/sets/",
        "/album/",
        "/albums/",
        "/channel/",
        "/c/",
        "/user/",
        "/series/",
    )
    if any(marker in path for marker in collection_markers):
        return True

    # Creator and channel upload pages use different URL structures across
    # sites, but their final segment is usually a generic collection label.
    # We still verify the extracted result has multiple entries before making
    # a collection folder, so this does not hard-code any individual site.
    collection_endings = {"videos", "uploads", "media", "clips", "reels", "shorts"}
    path_segments = [segment for segment in path.split("/") if segment]
    return bool(path_segments and path_segments[-1] in collection_endings)


def is_youtube_playlist_url(url):
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    return (host == "youtube.com" or host.endswith(".youtube.com") or host == "youtu.be") and "list=" in parsed.query.lower()


def extract_download_info(url, collection_progress_callback=None):
    options = {
        "quiet": True,
        "no_warnings": True,
        "windowsfilenames": True,
        # A collection can contain unavailable, private, or temporarily gated
        # entries. Keep inspecting the remaining entries instead of treating
        # the entire collection as empty.
        "ignoreerrors": True,
    }
    # A preview needs the collection list, not a full format lookup for every
    # video. The actual download still uses the normal highest-quality path.
    if collection_progress_callback and is_probable_collection_url(url):
        options["extract_flat"] = "in_playlist"
    add_site_request_options(options, url)
    ydl_class = get_yt_dlp().YoutubeDL
    if collection_progress_callback:
        base_class = ydl_class

        class CollectionInspectionYoutubeDL(base_class):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self._found_entries = 0
                self._expected_entries = None

            def _report_collection_progress(self):
                try:
                    collection_progress_callback(self._found_entries, self._expected_entries)
                except Exception:
                    pass

            def to_screen(self, message, *args, **kwargs):
                # Observe yt-dlp's own discovery messages without changing its
                # playlist-processing path. Some extractors repeat lower item
                # numbers while resolving a source, so retain only the highest
                # valid position for the first detected collection.
                item_match = re.search(r"Downloading item\s+(\d+)\s+of\s+(\d+|N/A)", str(message))
                total_match = re.search(r"Playlist .+: Downloading\s+(\d+|N/A)\s+items", str(message))
                changed = False
                if total_match and self._expected_entries is None:
                    total_text = total_match.group(1)
                    self._expected_entries = int(total_text) if total_text.isdigit() else None
                    changed = True
                if item_match:
                    item_number = int(item_match.group(1))
                    total_text = item_match.group(2)
                    item_total = int(total_text) if total_text.isdigit() else None
                    if self._expected_entries is None and item_total:
                        self._expected_entries = item_total
                        changed = True
                    if not self._expected_entries or item_number <= self._expected_entries:
                        previous_count = self._found_entries
                        self._found_entries = max(self._found_entries, item_number)
                        changed = changed or self._found_entries != previous_count
                if changed:
                    self._report_collection_progress()
                return super().to_screen(message, *args, **kwargs)

        ydl_class = CollectionInspectionYoutubeDL

    with ydl_class(options) as ydl:
        info = ydl.extract_info(url, download=False)

    if is_probable_collection_url(url) and (not isinstance(info, dict) or is_incomplete_collection_info(info)):
        flat_options = dict(options)
        flat_options["extract_flat"] = "in_playlist"
        with ydl_class(flat_options) as ydl:
            flat_info = ydl.extract_info(url, download=False)
        if isinstance(flat_info, dict):
            return flat_info
    return info


def extract_legacy_youtube_playlist_info(url):
    options = {
        "quiet": True,
        "no_warnings": True,
        "windowsfilenames": True,
    }
    with get_yt_dlp().YoutubeDL(options) as ydl:
        return ydl.extract_info(url, download=False)


def is_incomplete_collection_info(info):
    if not isinstance(info, dict) or info.get("_type") not in {"playlist", "multi_video"}:
        return False

    expected_count = positive_int(info.get("playlist_count") or info.get("n_entries"))
    entries = [entry for entry in (info.get("entries") or []) if isinstance(entry, dict)]
    if isinstance(expected_count, int) and expected_count > len(entries):
        return True
    return not entries


def positive_int(value):
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def has_media_for_mode(info, mode):
    entries = info.get("entries") if isinstance(info, dict) else None
    if entries:
        return any(has_media_for_mode(entry, mode) for entry in entries if isinstance(entry, dict))

    formats = info.get("formats") if isinstance(info, dict) else None
    if not formats:
        return bool(info.get("url")) if isinstance(info, dict) else False

    if mode in {"video", "gif", "frames"}:
        return any(format_has_video(media_format) for media_format in formats)
    if mode == "audio":
        return any(format_has_audio(media_format) for media_format in formats)
    return bool(formats)


def format_has_video(media_format):
    video_codec = media_format.get("vcodec")
    return video_codec not in {None, "none"}


def format_has_audio(media_format):
    audio_codec = media_format.get("acodec")
    return audio_codec not in {None, "none"}


def media_detection_error(url, mode, exc):
    message = str(exc)
    if "Sign in to confirm you're not a bot" in message:
        return (
            f"YouTube temporarily blocked access while reading this {mode_label(mode)}. "
            "Wait a few minutes and try again."
        )
    if is_x_or_twitter_url(url):
        return (
            f"No downloadable {mode_label(mode)} was found in this X/Twitter post. "
            "It may be text-only, image-only, private, deleted, age-restricted, or require login. "
            "This app can only download media that yt-dlp can access without your browser session."
        )
    if "No video" in message or "Unsupported URL" in message:
        return (
            f"No downloadable {mode_label(mode)} was found at this link. "
            "The page may not contain supported media, or it may require login."
        )
    return clean_download_error(message)


def no_media_message(url, mode):
    if is_x_or_twitter_url(url):
        return (
            f"This X/Twitter post does not appear to contain downloadable {mode_label(mode)}. "
            "Posts with only images/text are not supported by the Video, GIF, or Audio buttons."
        )
    return f"This link does not appear to contain downloadable {mode_label(mode)}."


def mode_label(mode):
    return {"gif": "video for GIF conversion", "audio": "audio", "video": "video", "frames": "video for frame extraction"}.get(mode, "media")


def is_x_or_twitter_url(url):
    host = urlparse(url).netloc.lower()
    return host in {"x.com", "twitter.com"} or host.endswith(".x.com") or host.endswith(".twitter.com")


def extract_twitter_fallback(url):
    tweet_id = twitter_status_id(url)
    if not tweet_id:
        return None

    request = Request(
        TWITIGER_EXTRACT_URL + quote(url, safe=""),
        headers={
            "Accept": "application/json",
            "Referer": "https://twitiger.com/",
            "User-Agent": f"{APP_NAME}/{APP_VERSION}",
        },
    )
    # The alternate lookup occasionally returns an empty response even for a
    # valid post. Retry it briefly instead of surfacing the original yt-dlp error.
    for attempt in range(3):
        try:
            with urlopen(request, timeout=5) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (OSError, URLError, json.JSONDecodeError):
            if attempt < 2:
                time.sleep(0.35)
            continue

        data = payload.get("data") if payload.get("success") else None
        if not isinstance(data, dict):
            if attempt < 2:
                time.sleep(0.35)
            continue

        resolutions = data.get("resolutions") or []
        media_url = next((item.get("videoUrl") for item in resolutions if isinstance(item, dict) and item.get("videoUrl")), None)
        media_url = media_url or data.get("videoUrl") or data.get("downloadUrl")
        if media_url:
            return {
                "title": data.get("title") or f"Twitter media {tweet_id}",
                "video_url": media_url,
                "quality": format_fallback_quality(resolutions),
            }
        if attempt < 2:
            time.sleep(0.35)
    return None


def validate_output_name(output_name):
    if not output_name or not output_name.strip():
        return None

    name = output_name.strip()
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", name)
    if not name:
        return "File name cannot be empty."
    if name.endswith((".", " ")):
        return "File name cannot end with a period or space."

    stem = Path(name).stem.upper()
    reserved = {"CON", "PRN", "AUX", "NUL", *(f"COM{index}" for index in range(1, 10)), *(f"LPT{index}" for index in range(1, 10))}
    if stem in reserved:
        return f"{stem} is reserved by Windows and cannot be used as a file name."
    return None


def format_fallback_quality(resolutions):
    candidates = [item for item in resolutions if isinstance(item, dict) and item.get("videoUrl")]
    if not candidates:
        return None

    def numeric(value):
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0

    best = max(
        candidates,
        key=lambda item: (
            numeric(item.get("height")),
            numeric(item.get("width")),
            numeric(item.get("fps")),
            numeric(item.get("bitrate") or item.get("bitRate")),
        ),
    )
    resolution = str(best.get("resolution") or "").strip()
    if not resolution:
        height = best.get("height")
        width = best.get("width")
        resolution = f"{height}p" if height else (f"{width}px" if width else "")
    parts = [resolution] if resolution else []
    fps = best.get("fps")
    if numeric(fps):
        parts.append(f"{numeric(fps):g} fps")
    bitrate = best.get("bitrate") or best.get("bitRate")
    if numeric(bitrate):
        parts.append(f"{numeric(bitrate) / 1_000_000:g} Mbps")
    return " - ".join(parts) or None


def twitter_status_id(url):
    match = re.search(r"/(?:i/)?status(?:es)?/(\d+)", url)
    return match.group(1) if match else None


def fallback_output_name(fallback):
    title = safe_folder_name(str(fallback.get("title") or "Twitter media"))
    return f"{title}.mp4"


def clean_download_error(message):
    return re.sub(r"^ERROR:\s*", "", message).strip() or "The downloader could not read media from this link."


def collection_title(info):
    if not isinstance(info, dict):
        return None

    entries = info.get("entries")
    entry_count = len(entries) if isinstance(entries, list) else 0
    declared_count = positive_int(info.get("playlist_count") or info.get("n_entries"))
    is_collection = (
        info.get("_type") in {"playlist", "multi_video"}
        or entry_count > 1
        or (declared_count and declared_count > 1)
    )
    if not is_collection:
        return None

    return info.get("playlist_title") or info.get("title") or info.get("id") or "Media Download"


def safe_folder_name(name):
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")
    return cleaned or "Media Download"


def custom_output_stem(output_name):
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", output_name.strip()).rstrip(". ")
    return Path(cleaned).stem


def custom_folder_name(output_name):
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", output_name.strip()).rstrip(". ")
    return cleaned or "Media Download"


def format_saved_locations(locations):
    unique_locations = []
    for location in locations:
        if location not in unique_locations:
            unique_locations.append(location)
    if not unique_locations:
        return str(DOWNLOAD_DIR)
    if len(unique_locations) == 1:
        return str(unique_locations[0])
    return ", ".join(str(location) for location in unique_locations)


def format_download_size(size):
    try:
        size = float(size)
    except (TypeError, ValueError):
        return "0 B"
    units = ["B", "KB", "MB", "GB", "TB"]
    unit = 0
    while size >= 1024 and unit < len(units) - 1:
        size /= 1024
        unit += 1
    if unit == 0:
        return f"{int(size)} {units[unit]}"
    return f"{size:.1f} {units[unit]}"


def download_direct_media(url, output_path, progress_hook):
    output_path = unique_path(output_path)
    request = Request(
        url,
        headers={
            "Referer": "https://twitter.com/",
            "User-Agent": "Mozilla/5.0",
        },
    )
    downloaded = 0
    started_at = time.monotonic()
    # Older video.twimg.com hosts can expose an expired certificate chain. Do
    # not weaken certificate verification for any other direct-media source.
    host = (urlparse(url).hostname or "").lower()
    ssl_context = ssl._create_unverified_context() if host == "video.twimg.com" or host.endswith(".video.twimg.com") else ssl.create_default_context()
    with urlopen(request, timeout=60, context=ssl_context) as response, output_path.open("wb") as output:
        try:
            total = int(response.headers.get("Content-Length") or 0)
        except ValueError:
            total = 0
        while True:
            chunk = response.read(1024 * 256)
            if not chunk:
                break
            output.write(chunk)
            downloaded += len(chunk)
            elapsed = max(time.monotonic() - started_at, 0.001)
            progress_hook(
                {
                    "status": "downloading",
                    "downloaded_bytes": downloaded,
                    "total_bytes": total,
                    "_speed_str": f"{format_download_size(downloaded / elapsed)}/s",
                }
            )
    progress_hook({"status": "finished", "downloaded_bytes": downloaded, "total_bytes": total})
    return output_path


def repair_video_for_playback(path, replace_original=True, output_name=None):
    repaired = remux_video_lossless(path, replace_original=replace_original)
    rebuilt = rebuild_video_for_playback(repaired, replace_original=True)
    finished = rebuilt or repaired
    if not replace_original and output_name:
        named_output = unique_path(path.with_name(f"{custom_output_stem(output_name)}{path.suffix}"))
        if finished != named_output:
            finished.replace(named_output)
        return named_output
    return finished


def remux_video_lossless(path, replace_original=True):
    path = Path(path)
    if not path.exists() or path.suffix.lower() not in VIDEO_SOURCE_SUFFIXES:
        return path

    if replace_original:
        repaired = unique_path(path.with_name(f"{path.stem}.repaired{path.suffix}"))
    else:
        repaired = unique_path(path.with_name(f"{path.stem} fixed{path.suffix}"))
    args = [
        find_ffmpeg_exe(),
        "-y",
        "-fflags",
        "+genpts",
        "-i",
        str(path),
        "-map",
        "0",
        "-c",
        "copy",
        "-dn",
        "-avoid_negative_ts",
        "make_zero",
    ]
    if path.suffix.lower() in {".mp4", ".mov"}:
        args.extend(["-movflags", "+faststart"])
    args.append(str(repaired))

    try:
        run_ffmpeg(args)
        if replace_original:
            repaired.replace(path)
            return path
        return repaired
    except Exception:
        try:
            repaired.unlink(missing_ok=True)
        except OSError:
            pass
        if not replace_original:
            raise
    return path


def rebuild_video_for_playback(path, replace_original=True):
    path = Path(path)
    if not path.exists() or path.suffix.lower() not in VIDEO_SOURCE_SUFFIXES:
        return None

    if replace_original:
        repaired = unique_path(path.with_name(f"{path.stem}.rebuilt{path.suffix}"))
    else:
        repaired = unique_path(path.with_name(f"{path.stem} rebuilt{path.suffix}"))

    args = [
        find_ffmpeg_exe(),
        "-y",
        "-fflags",
        "+genpts",
        "-i",
        str(path),
        "-map",
        "0:v:0",
        "-map",
        "0:a?",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "18",
        "-pix_fmt",
        "yuv420p",
        "-vf",
        "setpts=PTS-STARTPTS",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-af",
        "asetpts=PTS-STARTPTS",
        "-avoid_negative_ts",
        "make_zero",
    ]
    if path.suffix.lower() in {".mp4", ".mov"}:
        args.extend(["-movflags", "+faststart"])
    args.append(str(repaired))

    try:
        run_ffmpeg(args)
        if replace_original:
            repaired.replace(path)
            return path
        return repaired
    except Exception:
        try:
            repaired.unlink(missing_ok=True)
        except OSError:
            pass
        return None


def convert_source_to_gif(source, output_dir, delete_source=True, progress_callback=None, cancel_event=None, output_name=None):
    output_dir.mkdir(parents=True, exist_ok=True)
    gif_name = f"{custom_output_stem(output_name)}.gif" if output_name else source.with_suffix(".gif").name.replace(".source", "")
    gif_path = unique_path(output_dir / gif_name)
    palette = source.with_suffix(".palette.png")
    ffmpeg_exe = find_ffmpeg_exe()
    duration_seconds = media_duration_seconds(source)

    def report_progress(start, end, stage):
        if not progress_callback:
            return None
        return lambda percent: progress_callback(None if percent is None else start + (end - start) * percent / 100, stage)

    completed = False
    try:
        if progress_callback:
            progress_callback(None, "Generating GIF color palette...")
        run_ffmpeg(
            [
                ffmpeg_exe,
                "-y",
                "-i",
                str(source),
                "-vf",
                "fps=30,scale=iw:ih:flags=lanczos,palettegen=stats_mode=full",
                str(palette),
            ],
            cancel_event=cancel_event,
        )
        if progress_callback:
            progress_callback(0, "Rendering GIF...")
        run_ffmpeg(
            [
                ffmpeg_exe,
                "-y",
                "-i",
                str(source),
                "-i",
                str(palette),
                "-filter_complex",
                "fps=30,scale=iw:ih:flags=lanczos[x];[x][1:v]paletteuse=dither=sierra2_4a",
                "-loop",
                "0",
                str(gif_path),
            ],
            progress_callback=report_progress(0, 100, "Rendering GIF..."),
            duration_seconds=duration_seconds,
            cancel_event=cancel_event,
        )
        completed = True
    finally:
        temp_files = [palette]
        if not completed:
            temp_files.append(gif_path)
        if delete_source:
            temp_files.append(source)
        for temp_file in temp_files:
            try:
                temp_file.unlink(missing_ok=True)
            except OSError:
                pass
    return gif_path


def save_existing_gif(source, output_dir, output_name=None):
    output_dir.mkdir(parents=True, exist_ok=True)
    filename = f"{custom_output_stem(output_name)}.gif" if output_name else source.name.replace(".source.gif", ".gif")
    gif_path = unique_path(output_dir / filename)
    shutil.copy2(source, gif_path)
    return gif_path


def convert_gif_to_video(source, output_dir, progress_callback=None, cancel_event=None):
    output_dir.mkdir(parents=True, exist_ok=True)
    video_path = unique_path(output_dir / f"{source.stem}.mp4")
    ffmpeg_exe = find_ffmpeg_exe()
    duration_seconds = media_duration_seconds(source)

    run_ffmpeg(
        [
            ffmpeg_exe,
            "-y",
            "-i",
            str(source),
            "-movflags",
            "+faststart",
            "-pix_fmt",
            "yuv420p",
            "-vf",
            "scale=trunc(iw/2)*2:trunc(ih/2)*2",
            str(video_path),
        ],
        progress_callback=progress_callback,
        duration_seconds=duration_seconds,
        cancel_event=cancel_event,
    )
    return video_path


def convert_source_to_mp3(source, output_dir, delete_source=True, progress_callback=None, cancel_event=None, output_name=None):
    output_dir.mkdir(parents=True, exist_ok=True)
    mp3_name = f"{custom_output_stem(output_name)}.mp3" if output_name else source.with_suffix(".mp3").name.replace(".source", "")
    mp3_path = unique_path(output_dir / mp3_name)
    ffmpeg_exe = find_ffmpeg_exe()
    duration_seconds = media_duration_seconds(source)

    try:
        run_ffmpeg(
            [
                ffmpeg_exe,
                "-y",
                "-i",
                str(source),
                "-vn",
                "-codec:a",
                "libmp3lame",
                "-q:a",
                "2",
                str(mp3_path),
            ],
            progress_callback=progress_callback,
            duration_seconds=duration_seconds,
            cancel_event=cancel_event,
        )
    finally:
        if delete_source:
            try:
                source.unlink(missing_ok=True)
            except OSError:
                pass
    return mp3_path


def extract_unique_frames(source, output_root, progress_callback=None, cancel_event=None, output_name=None):
    output_root.mkdir(parents=True, exist_ok=True)
    frame_dir = unique_path(output_root / f"{custom_output_stem(output_name) if output_name else safe_folder_name(source.stem)} Frames")
    frame_dir.mkdir(parents=True, exist_ok=True)
    output_pattern = frame_dir / "frame_%06d.png"
    duration_seconds = media_duration_seconds(source)

    run_ffmpeg(
        [
            find_ffmpeg_exe(),
            "-y",
            "-i",
            str(source),
            "-vf",
            "mpdecimate,setpts=N/FRAME_RATE/TB",
            "-vsync",
            "vfr",
            str(output_pattern),
        ],
        progress_callback=progress_callback,
        duration_seconds=duration_seconds,
        cancel_event=cancel_event,
    )
    frames_per_second = media_frame_rate(source) or 30
    for index, frame_path in enumerate(sorted(frame_dir.glob("frame_*.png")), start=1):
        second_dir = frame_dir / f"Second {(index - 1) // frames_per_second + 1:04d}"
        second_dir.mkdir(exist_ok=True)
        shutil.move(str(frame_path), str(second_dir / frame_path.name))
    return frame_dir


def media_frame_rate(source):
    completed = subprocess.run([str(find_ffmpeg_exe()), "-hide_banner", "-i", str(source)], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", startupinfo=hidden_startupinfo(), creationflags=hidden_creationflags())
    match = re.search(r"(\d+(?:\.\d+)?)\s*fps", completed.stderr)
    return max(1, round(float(match.group(1)))) if match else None


def extract_first_frame(source, output_root, progress_callback=None, cancel_event=None, output_name=None):
    output_root.mkdir(parents=True, exist_ok=True)
    frame_path = unique_path(output_root / f"{custom_output_stem(output_name) if output_name else safe_folder_name(source.stem)}.png")

    run_ffmpeg(
        [
            find_ffmpeg_exe(),
            "-y",
            "-i",
            str(source),
            "-frames:v",
            "1",
            str(frame_path),
        ],
        progress_callback=progress_callback,
        duration_seconds=media_duration_seconds(source),
        cancel_event=cancel_event,
    )
    return frame_path


def extract_similar_frames(source, output_root, progress_callback=None, cancel_event=None, output_name=None):
    from PIL import Image

    output_root.mkdir(parents=True, exist_ok=True)
    frame_dir = unique_path(output_root / f"{custom_output_stem(output_name) if output_name else safe_folder_name(source.stem)} Similar Frames")
    frame_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="media-downloader-frames-") as temp_dir:
        raw_pattern = Path(temp_dir) / "raw_%06d.png"
        run_ffmpeg([find_ffmpeg_exe(), "-y", "-i", str(source), str(raw_pattern)], progress_callback=progress_callback, duration_seconds=media_duration_seconds(source), cancel_event=cancel_event)
        previous = None
        saved = 0
        for raw_path in sorted(Path(temp_dir).glob("raw_*.png")):
            check_cancelled(cancel_event)
            with Image.open(raw_path) as image:
                sample = image.convert("L").resize((16, 16))
                pixels = list(sample.getdata())
            average = sum(pixels) / len(pixels)
            frame_hash = sum((1 << index) for index, value in enumerate(pixels) if value >= average)
            if previous is not None and (frame_hash ^ previous).bit_count() < 14:
                continue
            previous = frame_hash
            saved += 1
            shutil.move(str(raw_path), str(frame_dir / f"frame_{saved:06d}.png"))
    return frame_dir


def unique_path(path):
    if not path.exists():
        return path

    if path.is_dir():
        parent = path.parent
        counter = 2
        while True:
            candidate = parent / f"{path.name} ({counter})"
            if not candidate.exists():
                return candidate
            counter += 1

    stem = path.stem
    suffix = path.suffix
    parent = path.parent
    counter = 2
    while True:
        candidate = parent / f"{stem} ({counter}){suffix}"
        if not candidate.exists():
            return candidate
        counter += 1


def find_ffmpeg_exe():
    candidates = []

    try:
        candidates.append(Path(get_imageio_ffmpeg().get_ffmpeg_exe()))
    except Exception:
        pass

    for command in ("ffmpeg.exe", "ffmpeg"):
        path = shutil.which(command)
        if path:
            candidates.append(Path(path))

    winget_root = Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WinGet" / "Packages"
    if winget_root.exists():
        candidates.extend(winget_root.glob("Gyan.FFmpeg_*/*/bin/ffmpeg.exe"))

    for candidate in candidates:
        if candidate.exists():
            return candidate

    raise RuntimeError("FFmpeg could not be found. Install FFmpeg, then reopen the app.")


def media_duration_seconds(source):
    completed = subprocess.run(
        [str(find_ffmpeg_exe()), "-hide_banner", "-i", str(source)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        startupinfo=hidden_startupinfo(),
        creationflags=hidden_creationflags(),
    )
    match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", completed.stderr)
    if not match:
        return None
    hours, minutes, seconds = match.groups()
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def media_video_height(source):
    completed = subprocess.run(
        [str(find_ffmpeg_exe()), "-hide_banner", "-i", str(source)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        startupinfo=hidden_startupinfo(),
        creationflags=hidden_creationflags(),
    )
    match = re.search(r"Video:.*?(\d{2,5})x(\d{2,5})", completed.stderr)
    return int(match.group(2)) if match else None


def gif_size_warning(source):
    duration = media_duration_seconds(source) or 0
    height = media_video_height(source) or 0
    if duration >= 30 and height >= 1080:
        return " Large GIF warning: long and high-resolution."
    if duration >= 30:
        return " Large GIF warning: long video."
    if height >= 1080:
        return " Large GIF warning: high-resolution video."
    return ""


def hidden_startupinfo():
    startupinfo = None
    if os.name == "nt":
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    return startupinfo


def hidden_creationflags():
    return subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0


def run_ffmpeg(args, return_output=False, progress_callback=None, duration_seconds=None, cancel_event=None):
    startupinfo = hidden_startupinfo()
    creationflags = hidden_creationflags()

    if cancel_event is not None:
        command = list(args)
        progress_updates = queue.Queue()
        stderr_reader = None

        if progress_callback is not None and duration_seconds:
            command = [command[0], "-progress", "pipe:2", "-nostats", *command[1:]]

            def read_progress(stream):
                for raw_line in iter(stream.readline, b""):
                    line = raw_line.decode("utf-8", errors="replace").strip()
                    if line.startswith("out_time_us=") or line.startswith("out_time_ms="):
                        try:
                            progress_updates.put(float(line.partition("=")[2]) / 1_000_000)
                        except ValueError:
                            pass
                    elif line.startswith("out_time="):
                        match = re.fullmatch(r"out_time=(\d+):(\d+):(\d+(?:\.\d+)?)", line)
                        if match:
                            hours, minutes, seconds = match.groups()
                            progress_updates.put(int(hours) * 3600 + int(minutes) * 60 + float(seconds))

        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE if progress_callback is not None and duration_seconds else subprocess.DEVNULL,
            startupinfo=startupinfo,
            creationflags=creationflags,
        )
        if process.stderr is not None:
            stderr_reader = threading.Thread(target=read_progress, args=(process.stderr,), daemon=True)
            stderr_reader.start()
        cancelled = False
        last_percent = -1.0
        try:
            while process.poll() is None:
                if cancel_event.is_set():
                    cancelled = True
                    process.terminate()
                    try:
                        process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
                    break
                if progress_callback is None or not duration_seconds:
                    time.sleep(0.1)
                    continue
                try:
                    elapsed = progress_updates.get(timeout=0.1)
                except queue.Empty:
                    continue
                percent = min(100.0, elapsed / duration_seconds * 100)
                if percent - last_percent >= 0.25:
                    progress_callback(percent)
                    last_percent = percent
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            if stderr_reader is not None:
                stderr_reader.join(timeout=2)
            if process.stderr is not None:
                process.stderr.close()
        if cancelled:
            raise get_download_cancelled()("Download cancelled.")
        if process.returncode != 0:
            raise RuntimeError("FFmpeg failed while converting media.")
        if progress_callback is not None and duration_seconds:
            progress_callback(100)
        return None

    completed = subprocess.run(
        args,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        startupinfo=startupinfo,
        creationflags=creationflags,
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or "FFmpeg failed.")
    if return_output:
        return f"{completed.stdout}\n{completed.stderr}"
    return None


class DownloadBridge:
    def __init__(self):
        self.cancel_event = threading.Event()
        self.shutting_down = threading.Event()
        self.worker = None
        self.output_lock = threading.Lock()
        self.download_total = 0
        self.download_current = 0
        self.download_completed = 0

    def emit(self, event):
        with self.output_lock:
            if self.shutting_down.is_set():
                return
            message = json.dumps(event, ensure_ascii=True)
            if event.get("type") == "state":
                state_path = os.environ.get("MEDIA_DOWNLOADER_STATE_PATH")
                if state_path:
                    target = Path(state_path)
                    temporary = target.with_suffix(".tmp")
                    try:
                        target.parent.mkdir(parents=True, exist_ok=True)
                        temporary.write_text(message, encoding="utf-8")
                        os.replace(temporary, target)
                    except OSError:
                        pass
            print(message, flush=True)

    def run(self):
        self.emit({"type": "ready"})
        for line in sys.stdin:
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue

            action = message.get("action")
            if action == "start":
                self.start(message.get("input", ""), message.get("mode", "video"), message.get("outputName", ""))
            elif action == "inspect":
                self.inspect(message.get("input", ""), message.get("requestId"))
            elif action == "cancel":
                self.cancel_event.set()
                self.emit({"type": "state", "phase": "cancelling", "status": "Stopping conversion...", "indeterminate": True})
            elif action == "shutdown":
                self.shutting_down.set()
                # Wait for an in-flight emit to finish before the Python
                # runtime starts closing stdout under preview worker threads.
                with self.output_lock:
                    pass
                self.cancel_event.set()
                if self.worker and self.worker.is_alive():
                    self.worker.join(timeout=4)
                return

    def inspect(self, input_text, request_id):
        threading.Thread(target=self._inspect_worker, args=(input_text, request_id), daemon=True).start()

    def _inspect_worker(self, input_text, request_id):
        try:
            def report_collection_progress(found, total):
                status = f"Detecting videos: {found}/{total}" if total else f"Detecting videos: {found} found"
                self.emit({"type": "previewProgress", "requestId": request_id, "status": status})

            name, message, quality, entries = inspect_output_name(input_text, report_collection_progress)
        except Exception:
            name, message, quality, entries = None, "Default filename could not be read.", None, []
        self.emit({"type": "preview", "requestId": request_id, "name": name, "message": message, "quality": quality, "entries": entries})

    def start(self, input_text, mode, output_name=""):
        if self.worker and self.worker.is_alive():
            self.emit({"type": "error", "message": "A download is already running."})
            return

        urls = extract_urls(input_text)
        local_files = extract_local_files(input_text, urls)
        validation_error = validate_inputs(urls, local_files)
        validation_error = validation_error or validate_output_name(output_name)
        if mode == "fix-video" and (not local_files or urls):
            validation_error = validation_error or "Fix Video works with a selected or dropped video file."
        if validation_error:
            self.emit({"type": "error", "message": validation_error})
            return

        DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
        self.cancel_event.clear()
        self.download_total = len(urls) + len(local_files)
        self.download_current = 0
        self.download_completed = 0
        item_count = self.download_total
        item_text = "item" if item_count == 1 else "items"
        self.emit(
            {
                "type": "state",
                "phase": "downloading",
                "status": f"Starting {item_count} {item_text}...",
                "progress": 0,
                "indeterminate": False,
            }
        )
        self.worker = threading.Thread(target=self._download_worker, args=(urls, local_files, mode, output_name), daemon=True)
        self.worker.start()

    def _download_worker(self, urls, local_files, mode, output_name):
        try:
            outputs = []
            if local_files:
                outputs.append(process_local_files(local_files, mode, self._status, self.cancel_event, self._conversion_progress, output_name))
            if mode == "fix-video":
                output = None
            elif mode == "video":
                output = download_best_video(urls, self._progress_hook, self._status, self.cancel_event, output_name) if urls else None
            elif mode == "gif":
                output = download_best_gifs(urls, self._progress_hook, self._status, self.cancel_event, self._conversion_progress, output_name) if urls else None
            elif mode in {"frames", "frame-first", "frames-similar"}:
                output = download_best_frames(urls, self._progress_hook, self._status, self.cancel_event, self._conversion_progress, first_frame=mode == "frame-first", similar_frames=mode == "frames-similar", output_name=output_name) if urls else None
            else:
                output = download_best_mp3s(urls, self._progress_hook, self._status, self.cancel_event, self._conversion_progress, output_name) if urls else None
            if output:
                outputs.append(output)
            self.emit({"type": "state", "phase": "done", "status": f"Saved: {'; '.join(outputs)}", "progress": 100})
        except get_download_cancelled():
            self.emit({"type": "state", "phase": "cancelled", "status": "Download cancelled."})
        except Exception as exc:
            self.emit({"type": "error", "message": str(exc)})

    def _status(self, text):
        phase = "extracting" if "Extracting" in text else "converting" if any(marker in text for marker in ("Converting", "Generating GIF", "Rendering GIF", "Repairing")) else "downloading"
        event = {"type": "state", "phase": phase, "status": text}
        if phase == "converting" or text.startswith("Rate limited."):
            event["indeterminate"] = True
        self.emit(event)

    def _conversion_progress(self, percent, status, phase="converting"):
        event = {"type": "state", "phase": phase, "status": status}
        if percent is None:
            event["indeterminate"] = True
        else:
            event["progress"] = max(0, min(100, percent))
            event["indeterminate"] = False
        self.emit(event)

    def _progress_hook(self, data):
        if self.cancel_event.is_set():
            raise get_download_cancelled()("Download cancelled.")

        self._sync_download_position(data)
        status = data.get("status")
        count = self._download_count_prefix()
        if status == "downloading":
            total = data.get("total_bytes") or data.get("total_bytes_estimate") or 0
            downloaded = data.get("downloaded_bytes") or 0
            speed = data.get("_speed_str", "").strip()
            if total:
                item_percent = downloaded / total * 100
                overall_percent = self._overall_progress(item_percent)
                if self.download_total > 1:
                    message = f"{count}Overall {overall_percent:.1f}% - current video {item_percent:.1f}% {speed}".strip()
                else:
                    message = f"Downloading... {item_percent:.1f}% {speed}".strip()
                self.emit({"type": "state", "phase": "downloading", "status": message, "progress": overall_percent, "indeterminate": False})
            else:
                message = f"{count}Downloading... {format_download_size(downloaded)} received {speed}".strip()
                self.emit({"type": "state", "phase": "downloading", "status": message, "indeterminate": True})
        elif status == "finished":
            self.download_completed = max(self.download_completed, self.download_current)
            self.emit(
                {
                    "type": "state",
                    "phase": "downloading",
                    "status": f"{count}Processing downloaded media...",
                    "progress": self._overall_progress(100),
                    "indeterminate": False,
                }
            )

    def _sync_download_position(self, data):
        info = data.get("info_dict") or {}
        playlist_total = info.get("playlist_count") or info.get("n_entries") or data.get("playlist_count") or data.get("n_entries")
        playlist_index = info.get("playlist_index") or data.get("playlist_index")
        if isinstance(playlist_total, int) and playlist_total > self.download_total:
            self.download_total = playlist_total
        if isinstance(playlist_index, int) and playlist_index > 0:
            self.download_current = playlist_index
        elif self.download_total:
            self.download_current = min(self.download_completed + 1, self.download_total)

    def _download_count_prefix(self):
        if self.download_total <= 1:
            return ""
        current = self.download_current or min(self.download_completed + 1, self.download_total)
        return f"{current}/{self.download_total} "

    def _overall_progress(self, item_percent):
        if self.download_total <= 1:
            return item_percent
        current = self.download_current or min(self.download_completed + 1, self.download_total)
        return min(100, ((current - 1) + item_percent / 100) / self.download_total * 100)


if __name__ == "__main__":
    # The shipped app is Photino. Its host always launches this bridge.
    DownloadBridge().run()
