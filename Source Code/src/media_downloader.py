import base64
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import tkinter as tk
import tempfile
import html
from pathlib import Path
from tkinter import filedialog, ttk
from urllib.error import URLError
from urllib.parse import quote, unquote, urljoin, urlparse
from urllib.request import Request, urlopen

APP_NAME = "Media Downloader"
APP_VERSION = "1.1.4"
GITHUB_REPO = "TNephilim/media-downloader"
RELEASE_ASSET_NAME = "MediaDownloader.exe"
TWITIGER_EXTRACT_URL = "https://twitiger.com/api/extract?url="
DOWNLOAD_DIR = Path.home() / "Downloads"
URL_PATTERN = re.compile(r"https?://[^\s<>\"]+")
BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)
COMMON_HTTP_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "User-Agent": BROWSER_USER_AGENT,
}
MEDIA_URL_RE = re.compile(
    r"https?://[^\\\"'<>\s]+?\.(?:mp4|webm|mov|mkv|m3u8|mpd)(?:\?[^\\\"'<>\s]*)?",
    re.IGNORECASE,
)
IFRAME_URL_RE = re.compile(
    r"<iframe[^>]+src=[\"'](?P<src>[^\"']+)[\"']",
    re.IGNORECASE,
)
SOURCE_URL_RE = re.compile(
    r"<(?:source|video|audio)[^>]+src=[\"'](?P<src>[^\"']+)[\"']",
    re.IGNORECASE,
)
META_MEDIA_URL_RE = re.compile(
    r"<meta[^>]+(?:property|name)=[\"'](?:og:video|og:video:url|og:video:secure_url|twitter:player|twitter:player:stream)[\"'][^>]+content=[\"'](?P<src>[^\"']+)[\"']",
    re.IGNORECASE,
)
PAGE_SCAN_MAX_CANDIDATES = 10
BROWSER_DIRECT_CANDIDATE_LIMIT = 4
BROWSER_SCAN_SECONDS = 8
BROWSER_SCAN_TIMEOUT_MS = 20000
DIRECT_MEDIA_CONNECT_TIMEOUT_SECONDS = 15
DIRECT_MEDIA_DOWNLOAD_TIMEOUT_SECONDS = 20
DIRECT_MEDIA_PROBE_TIMEOUT_SECONDS = 12
HLS_PLAYLIST_PROBE_BYTES = 512_000
BROWSER_MEDIA_CONTENT_RE = re.compile(
    r"(video|audio|mpegurl|mpegURL|x-mpegURL|dash\+xml|mp4|webm|quicktime)",
    re.IGNORECASE,
)
BROWSER_STREAM_SEGMENT_RE = re.compile(r"(?:\.ts$|\.m4s$|/segment(?:s)?/|/chunk(?:s)?/|/frag(?:ment)?s?/)", re.IGNORECASE)
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
LOCAL_FILE_SUFFIXES = MEDIA_SOURCE_SUFFIXES | {".gif"}

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD

    APP_TK_CLASS = TkinterDnD.Tk
except Exception:
    DND_FILES = None
    APP_TK_CLASS = tk.Tk


class DownloadApp(APP_TK_CLASS):
    def __init__(self):
        super().__init__()
        self.title(APP_NAME)
        self.geometry("700x430")
        self.minsize(700, 430)
        self.resizable(False, False)
        self.configure(bg="#111827")

        self.events = queue.Queue()
        self.worker = None
        self.cancel_event = threading.Event()
        self.download_total = 0
        self.download_current = 0
        self.download_completed = 0
        self.progress_is_indeterminate = False

        self.phase_var = tk.StringVar(value="READY")
        self.status_var = tk.StringVar(value="No downloads currently")
        self.progress_var = tk.DoubleVar(value=0)

        self._configure_styles()
        self._build_ui()
        self.after(1000, self._check_for_updates)
        self.after(100, self._process_events)

    def _configure_styles(self):
        self.style = ttk.Style(self)
        try:
            self.style.theme_use("clam")
        except tk.TclError:
            pass

        self.style.configure(".", background="#111827", foreground="#e5e7eb", font=("Segoe UI", 10))
        self.style.configure("TFrame", background="#111827")
        self.style.configure("TLabel", background="#111827", foreground="#e5e7eb")
        self.style.configure("Muted.TLabel", background="#111827", foreground="#9ca3af")
        self.style.configure(
            "TButton",
            background="#1f2937",
            foreground="#f9fafb",
            bordercolor="#374151",
            focusthickness=1,
            focuscolor="#3b82f6",
            padding=(10, 6),
        )
        self.style.map(
            "TButton",
            background=[("active", "#374151"), ("disabled", "#111827")],
            foreground=[("disabled", "#6b7280")],
        )
        self.style.configure(
            "Download.Horizontal.TProgressbar",
            troughcolor="#1f2937",
            background="#16a34a",
            lightcolor="#16a34a",
            darkcolor="#16a34a",
            bordercolor="#374151",
        )
        self.style.configure(
            "Convert.Horizontal.TProgressbar",
            troughcolor="#1f2937",
            background="#16a34a",
            lightcolor="#16a34a",
            darkcolor="#16a34a",
            bordercolor="#374151",
        )
        self.style.configure(
            "Done.Horizontal.TProgressbar",
            troughcolor="#1f2937",
            background="#0f7ae5",
            lightcolor="#0f7ae5",
            darkcolor="#0f7ae5",
            bordercolor="#374151",
        )
        self.style.configure(
            "Error.Horizontal.TProgressbar",
            troughcolor="#1f2937",
            background="#dc2626",
            lightcolor="#dc2626",
            darkcolor="#dc2626",
            bordercolor="#374151",
        )

    def _build_ui(self):
        root = ttk.Frame(self, padding=(22, 18, 22, 24))
        root.pack(fill=tk.BOTH, expand=True)
        root.columnconfigure(0, weight=1)

        title = ttk.Label(root, text=APP_NAME, font=("Segoe UI", 18, "bold"))
        title.grid(row=0, column=0, sticky="w")

        subtitle = ttk.Label(
            root,
            text="Paste links, drag files here, or choose existing media files.",
            style="Muted.TLabel",
        )
        subtitle.grid(row=1, column=0, sticky="w", pady=(2, 7))

        entry_row = ttk.Frame(root)
        entry_row.grid(row=2, column=0, sticky="ew")
        entry_row.columnconfigure(0, weight=1)
        entry_row.rowconfigure((0, 1, 2), weight=1)

        self.url_text = tk.Text(
            entry_row,
            height=3,
            font=("Segoe UI", 10),
            bg="#1f2937",
            fg="#f9fafb",
            insertbackground="#f9fafb",
            relief=tk.FLAT,
            bd=0,
            padx=10,
            pady=8,
            wrap="word",
            highlightthickness=1,
            highlightbackground="#374151",
            highlightcolor="#3b82f6",
        )
        self.url_text.grid(row=0, column=0, rowspan=3, sticky="nsew")

        self.clear_button = ttk.Button(entry_row, text="Clear", command=self._clear_text_box)
        self.clear_button.grid(row=0, column=1, sticky="ew", padx=(8, 0), pady=(0, 3))

        paste_button = ttk.Button(entry_row, text="Paste", command=self._paste_clipboard)
        paste_button.grid(row=1, column=1, sticky="ew", padx=(8, 0), pady=3)

        self.choose_button = ttk.Button(entry_row, text="Choose File", command=self._choose_files)
        self.choose_button.grid(row=2, column=1, sticky="ew", padx=(8, 0), pady=(3, 0))

        button_row = ttk.Frame(root)
        button_row.grid(row=3, column=0, sticky="ew", pady=(8, 6))
        button_row.columnconfigure((0, 1, 2), weight=1)

        self.video_button = ttk.Button(
            button_row,
            text="Download Video",
            command=lambda: self._start_download("video"),
        )
        self.video_button.grid(row=0, column=0, sticky="ew", padx=(0, 4), ipady=8)

        self.mp3_button = ttk.Button(
            button_row,
            text="Download Audio",
            command=lambda: self._start_download("mp3"),
        )
        self.mp3_button.grid(row=0, column=1, sticky="ew", padx=4, ipady=8)

        self.gif_button = ttk.Button(
            button_row,
            text="Download GIF",
            command=lambda: self._start_download("gif"),
        )
        self.gif_button.grid(row=0, column=2, sticky="ew", padx=(4, 0), ipady=8)

        self.progress = ttk.Progressbar(
            root,
            variable=self.progress_var,
            maximum=100,
            mode="determinate",
            style="Download.Horizontal.TProgressbar",
        )
        self.progress.grid(row=4, column=0, sticky="ew", pady=(2, 6))

        self.phase_label = tk.Label(
            root,
            textvariable=self.phase_var,
            anchor="center",
            font=("Segoe UI", 12, "bold"),
            bg="#172033",
            fg="#93c5fd",
            padx=10,
            pady=8,
            bd=0,
            highlightthickness=1,
            highlightbackground="#374151",
        )
        self.phase_label.grid(row=5, column=0, sticky="ew")

        self.status_label = ttk.Label(root, textvariable=self.status_var, style="Muted.TLabel")
        self.status_label.grid(row=6, column=0, sticky="ew", pady=(6, 0))

        bottom_row = ttk.Frame(root)
        bottom_row.grid(row=7, column=0, sticky="ew", pady=(10, 0))
        bottom_row.columnconfigure(0, weight=1)

        open_button = ttk.Button(bottom_row, text="Open Downloads Folder", command=self._open_download_folder)
        open_button.grid(row=0, column=0, sticky="w")

        self.cancel_button = ttk.Button(bottom_row, text="Cancel Download", command=self._cancel_download)
        self.cancel_button.grid(row=0, column=1, sticky="e")
        self.cancel_button.configure(state=tk.DISABLED)

        if DND_FILES:
            self._register_drop_targets(root, entry_row, self.url_text, button_row, bottom_row)

    def _try_load_clipboard(self):
        try:
            text = self.clipboard_get().strip()
        except tk.TclError:
            return
        if "http://" in text or "https://" in text or Path(text).is_file():
            self.url_text.delete("1.0", tk.END)
            self.url_text.insert("1.0", text)

    def _paste_clipboard(self):
        self._try_load_clipboard()

    def _clear_text_box(self):
        self.url_text.delete("1.0", tk.END)

    def _choose_files(self):
        files = filedialog.askopenfilenames(
            title="Choose media files",
            filetypes=[
                ("Media files", "*.mp4 *.webm *.mkv *.mov *.gif *.mp3 *.m4a *.aac *.flac *.ogg *.opus *.wav"),
                ("All files", "*.*"),
            ],
        )
        if files:
            self._replace_input_items(files)

    def _drop_files(self, event):
        files = self.tk.splitlist(event.data)
        if files:
            self._replace_input_items(files)

    def _replace_input_items(self, items):
        self.url_text.delete("1.0", tk.END)
        self.url_text.insert("1.0", "\n".join(str(item) for item in items))

    def _register_drop_targets(self, *widgets):
        for widget in widgets:
            widget.drop_target_register(DND_FILES)
            widget.dnd_bind("<<Drop>>", self._drop_files)

    def _cancel_download(self):
        if self.worker and self.worker.is_alive():
            self.cancel_event.set()
            self.cancel_button.configure(state=tk.DISABLED)
            self._set_status("Cancelling download...")

    def _start_download(self, mode):
        if self.worker and self.worker.is_alive():
            self._show_dialog("Download Running", "A download is already running.")
            return

        input_text = self._get_input_text()
        if not input_text:
            self._try_load_clipboard()
            input_text = self._get_input_text()

        urls = extract_urls(input_text)
        local_files = extract_local_files(input_text, urls)
        validation_error = validate_inputs(urls, local_files)
        if validation_error:
            self._show_dialog("Cannot Download", validation_error, "error")
            return

        DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
        self._set_progress_activity(False)
        self.progress_var.set(0)
        self.cancel_event.clear()
        self.download_total = len(urls) + len(local_files)
        self.download_current = 0
        self.download_completed = 0
        self._set_phase("downloading")
        self._set_busy(True)
        item_count = len(urls) + len(local_files)
        item_text = "item" if item_count == 1 else "items"
        self._set_status(f"Starting {item_count} {item_text}...")

        self.worker = threading.Thread(target=self._download_worker, args=(urls, local_files, mode), daemon=True)
        self.worker.start()

    def _get_input_text(self):
        return self.url_text.get("1.0", tk.END).strip()

    def _download_worker(self, urls, local_files, mode):
        try:
            outputs = []
            if local_files:
                outputs.append(process_local_files(local_files, mode, self._status, self.cancel_event))
            if mode == "video":
                output = download_best_video(urls, self._progress_hook, self._status, self.cancel_event) if urls else None
            elif mode == "gif":
                output = download_best_gifs(urls, self._progress_hook, self._status, self.cancel_event) if urls else None
            elif mode == "frames":
                output = download_best_frames(urls, self._progress_hook, self._status, self.cancel_event) if urls else None
            else:
                output = download_best_mp3s(urls, self._progress_hook, self._status, self.cancel_event) if urls else None
            if output:
                outputs.append(output)
            self.events.put(("done", f"Saved: {'; '.join(outputs)}"))
        except get_download_cancelled():
            self.events.put(("cancelled", "Download cancelled."))
        except Exception as exc:
            self.events.put(("error", str(exc)))

    def _check_for_updates(self):
        if not getattr(sys, "frozen", False):
            return

        threading.Thread(target=self._update_worker, daemon=True).start()

    def _update_worker(self):
        try:
            release = fetch_latest_release()
            latest_version = release.get("tag_name", "")
            if not is_newer_version(latest_version, APP_VERSION):
                return

            asset = find_release_asset(release, RELEASE_ASSET_NAME)
            if not asset:
                return

            self.events.put(("status", f"Updating to {latest_version}..."))
            downloaded_exe = download_update_asset(asset["browser_download_url"])
            self.events.put(("update_ready", latest_version, str(downloaded_exe)))
        except (OSError, URLError, ValueError, KeyError, json.JSONDecodeError):
            return

    def _progress_hook(self, data):
        if self.cancel_event.is_set():
            raise get_download_cancelled()("Download cancelled.")

        if data.get("status") == "checking":
            self.events.put(("activity", "Checking media connection..."))
            return
        if data.get("status") == "preparing":
            self.events.put(("activity", "Connecting to media stream..."))
            return

        self._sync_download_position(data)

        if data.get("status") == "downloading":
            total = data.get("total_bytes") or data.get("total_bytes_estimate") or 0
            downloaded = data.get("downloaded_bytes") or 0
            speed = data.get("_speed_str", "").strip()
            count = self._download_count_prefix()
            fragment_index = data.get("fragment_index")
            fragment_count = data.get("fragment_count")

            if total:
                percent = downloaded / total * 100
                self.events.put(("progress", percent, f"{count}Downloading... {percent:.1f}% {speed}".strip()))
            elif isinstance(fragment_index, int) and isinstance(fragment_count, int) and fragment_count > 0:
                percent = fragment_index / fragment_count * 100
                self.events.put(
                    ("progress", percent, f"{count}Downloading stream... {fragment_index}/{fragment_count} parts {speed}".strip())
                )
            else:
                received = format_download_size(downloaded)
                message = f"{count}Downloading stream... {received} received {speed}".strip()
                self.events.put(("activity", message))
        elif data.get("status") == "finished":
            count = self._download_count_prefix()
            self.events.put(("progress", 100, f"{count}Processing downloaded media..."))
            self.download_completed = max(self.download_completed, self.download_current)

    def _status(self, text):
        self.events.put(("status", text))

    def _sync_download_position(self, data):
        info = data.get("info_dict") or {}
        playlist_total = (
            info.get("playlist_count")
            or info.get("n_entries")
            or data.get("playlist_count")
            or data.get("n_entries")
        )
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

    def _process_events(self):
        try:
            while True:
                event = self.events.get_nowait()
                kind = event[0]
                if kind == "progress":
                    self._set_progress_activity(False)
                    self.progress_var.set(event[1])
                    self._set_status(event[2])
                elif kind == "activity":
                    self._set_progress_activity(True)
                    self._set_status(event[1])
                elif kind == "status":
                    if "Converting" in event[1]:
                        self._set_phase("converting")
                    self._set_status(event[1])
                elif kind == "update_ready":
                    self._set_status(f"Installing update {event[1]}...")
                    install_update_and_restart(Path(event[2]))
                    self.destroy()
                    return
                elif kind == "done":
                    self._set_progress_activity(False)
                    self.progress_var.set(100)
                    self._set_phase("done")
                    self._set_status(event[1])
                    self._set_busy(False)
                elif kind == "cancelled":
                    self._set_progress_activity(False)
                    self._set_phase("cancelled")
                    self._set_status(event[1])
                    self._set_busy(False)
                elif kind == "error":
                    self._set_progress_activity(False)
                    self._set_phase("error")
                    self._set_status("Download failed.")
                    self._set_busy(False)
                    self._show_dialog("Download Failed", event[1], "error")
        except queue.Empty:
            pass
        self.after(100, self._process_events)

    def _set_busy(self, busy):
        state = tk.DISABLED if busy else tk.NORMAL
        self.video_button.configure(state=state)
        self.mp3_button.configure(state=state)
        self.gif_button.configure(state=state)
        self.choose_button.configure(state=state)
        self.cancel_button.configure(state=tk.NORMAL if busy else tk.DISABLED)

    def _set_progress_activity(self, active):
        if active == self.progress_is_indeterminate:
            return
        self.progress_is_indeterminate = active
        if active:
            self.progress.configure(mode="indeterminate")
            self.progress.start(12)
        else:
            self.progress.stop()
            self.progress.configure(mode="determinate")

    def _set_status(self, text):
        self.status_var.set(text or "No downloads currently")

    def _set_phase(self, phase):
        settings = {
            "downloading": {
                "text": "DOWNLOADING",
                "bg": "#052e16",
                "fg": "#86efac",
                "style": "Download.Horizontal.TProgressbar",
            },
            "converting": {
                "text": "CONVERTING",
                "bg": "#052e16",
                "fg": "#86efac",
                "style": "Convert.Horizontal.TProgressbar",
            },
            "done": {
                "text": "DOWNLOAD COMPLETE",
                "bg": "#082f49",
                "fg": "#93c5fd",
                "style": "Done.Horizontal.TProgressbar",
            },
            "cancelled": {
                "text": "DOWNLOAD CANCELLED",
                "bg": "#292524",
                "fg": "#d6d3d1",
                "style": "Error.Horizontal.TProgressbar",
            },
            "error": {
                "text": "DOWNLOAD FAILED",
                "bg": "#450a0a",
                "fg": "#fca5a5",
                "style": "Error.Horizontal.TProgressbar",
            },
        }[phase]
        self.phase_var.set(settings["text"])
        self.phase_label.configure(bg=settings["bg"], fg=settings["fg"], highlightbackground="#374151")
        self.progress.configure(style=settings["style"])

    def _open_download_folder(self):
        DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
        os.startfile(DOWNLOAD_DIR)

    def _show_dialog(self, title, message, kind="info"):
        colors = {
            "done": ("#082f49", "#93c5fd"),
            "error": ("#450a0a", "#fca5a5"),
            "info": ("#172033", "#e5e7eb"),
        }
        header_bg, header_fg = colors.get(kind, colors["info"])

        dialog = tk.Toplevel(self)
        dialog.title(title)
        dialog.configure(bg="#111827")
        dialog.transient(self)
        dialog.grab_set()
        dialog.resizable(False, False)

        frame = tk.Frame(dialog, bg="#111827", padx=20, pady=18)
        frame.grid(row=0, column=0, sticky="nsew")
        frame.columnconfigure(0, weight=1)

        header = tk.Label(
            frame,
            text=title.upper(),
            anchor="center",
            font=("Segoe UI", 12, "bold"),
            bg=header_bg,
            fg=header_fg,
            padx=12,
            pady=8,
            highlightthickness=1,
            highlightbackground="#374151",
        )
        header.grid(row=0, column=0, sticky="ew")

        body = tk.Label(
            frame,
            text=message,
            anchor="w",
            justify="left",
            wraplength=460,
            font=("Segoe UI", 10),
            bg="#111827",
            fg="#d1d5db",
            padx=2,
            pady=16,
        )
        body.grid(row=1, column=0, sticky="ew")

        ok_button = tk.Button(
            frame,
            text="OK",
            command=dialog.destroy,
            font=("Segoe UI", 10, "bold"),
            bg="#1f2937",
            fg="#f9fafb",
            activebackground="#374151",
            activeforeground="#f9fafb",
            relief=tk.FLAT,
            padx=24,
            pady=8,
            bd=0,
            highlightthickness=1,
            highlightbackground="#4b5563",
        )
        ok_button.grid(row=2, column=0, sticky="e")
        ok_button.focus_set()
        dialog.bind("<Return>", lambda _event: dialog.destroy())
        dialog.bind("<Escape>", lambda _event: dialog.destroy())

        self.update_idletasks()
        dialog.update_idletasks()
        x = self.winfo_rootx() + (self.winfo_width() - dialog.winfo_width()) // 2
        y = self.winfo_rooty() + (self.winfo_height() - dialog.winfo_height()) // 2
        dialog.geometry(f"+{max(x, 0)}+{max(y, 0)}")


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


def normalize_media_url(url):
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    if host == "x.com" or host.endswith(".x.com"):
        return url.replace(parsed.netloc, "twitter.com", 1)
    return url


def validate_inputs(urls, local_files):
    if not urls and not local_files:
        return "Paste at least one valid web link or choose a media file."

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
    if _YT_DLP is None:
        import yt_dlp

        _YT_DLP = yt_dlp
    return _YT_DLP


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


def fetch_latest_release():
    request = Request(
        f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest",
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": f"{APP_NAME}/{APP_VERSION}",
        },
    )
    with urlopen(request, timeout=10) as response:
        return json.loads(response.read().decode("utf-8"))


def is_newer_version(latest, current):
    return parse_version(latest) > parse_version(current)


def parse_version(version):
    cleaned = version.strip().lower().lstrip("v")
    parts = []
    for part in cleaned.split("."):
        digits = "".join(char for char in part if char.isdigit())
        parts.append(int(digits or 0))
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts[:3])


def find_release_asset(release, asset_name):
    for asset in release.get("assets", []):
        if asset.get("name") == asset_name and asset.get("browser_download_url"):
            return asset
    return None


def download_update_asset(url):
    update_path = Path(tempfile.gettempdir()) / RELEASE_ASSET_NAME
    request = Request(url, headers={"User-Agent": f"{APP_NAME}/{APP_VERSION}"})
    with urlopen(request, timeout=60) as response, update_path.open("wb") as output:
        shutil.copyfileobj(response, output)
    return update_path


def install_update_and_restart(downloaded_exe):
    current_exe = Path(sys.executable)
    script = Path(tempfile.gettempdir()) / "media_downloader_update.cmd"
    script.write_text(
        "\n".join(
            [
                "@echo off",
                "timeout /t 2 /nobreak >nul",
                f'copy /y "{downloaded_exe}" "{current_exe}" >nul',
                f'start "" "{current_exe}"',
                f'del "{downloaded_exe}" >nul 2>nul',
                'del "%~f0" >nul 2>nul',
            ]
        ),
        encoding="utf-8",
    )
    creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    subprocess.Popen([str(script)], shell=True, creationflags=creationflags)


def make_ydl_options(
    progress_hook,
    output_template,
    allow_playlists=True,
    format_selector="bv*+ba/b",
    format_sort=None,
    merge_output_format="mp4",
    http_headers=None,
    cookies_from_browser=None,
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
        "http_headers": http_headers or COMMON_HTTP_HEADERS,
        "retries": 5,
        "fragment_retries": 5,
        "extractor_retries": 3,
        "geo_bypass": True,
        "socket_timeout": 20,
    }
    if cookies_from_browser:
        options["cookiesfrombrowser"] = cookies_from_browser
    if format_sort:
        options["format_sort"] = format_sort
    if merge_output_format:
        options["merge_output_format"] = merge_output_format
    return options


def download_best_video(urls, progress_hook, status_callback, cancel_event):
    saved_locations = []
    for url in urls:
        check_cancelled(cancel_event)
        target = resolve_download_target(url, status_callback, "video")
        check_cancelled(cancel_event)
        output_template = target["directory"] / output_filename_template(target["is_collection"])
        options = make_ydl_options(
            progress_hook,
            output_template,
            merge_output_format="mp4",
            http_headers=target.get("http_headers"),
            cookies_from_browser=target.get("cookies_from_browser"),
        )

        if target["is_collection"]:
            status_callback(f"Downloading {target['name']} into {target['directory'].name}...")
        elif target.get("direct_media_url"):
            source = target.get("source") or "media scan"
            status_callback(f"Found media from {source}. Downloading stream...")
        else:
            status_callback("Downloading best available video...")

        started_at = time.time()
        if target.get("direct_media_url"):
            output_path = target["directory"] / direct_media_filename(target, ".mp4")
            download_direct_media(target["direct_media_url"], output_path, progress_hook, target.get("http_headers"))
            status_callback("Repairing video timestamps...")
            remux_video_lossless(output_path)
        else:
            with get_yt_dlp().YoutubeDL(options) as ydl:
                code = ydl.download([target["url"]])
            if code:
                raise RuntimeError("One or more downloads failed.")
            status_callback("Repairing video timestamps...")
            remux_new_videos(target["directory"], started_at)
        saved_locations.append(target["directory"])
    return format_saved_locations(saved_locations)


def process_local_files(files, mode, status_callback, cancel_event):
    outputs = []
    total = len(files)
    for index, file_path in enumerate(files, start=1):
        check_cancelled(cancel_event)
        suffix = file_path.suffix.lower()

        if mode == "frames":
            if suffix not in VIDEO_SOURCE_SUFFIXES and suffix != ".gif":
                raise RuntimeError(f"{file_path.name} is not a supported GIF or video file for frame extraction.")
            status_callback(f"Extracting frames {index}/{total}...")
            outputs.append(extract_unique_frames(file_path, DOWNLOAD_DIR))
        elif mode == "video":
            if suffix == ".gif":
                status_callback(f"Converting GIF to video {index}/{total}...")
                outputs.append(convert_gif_to_video(file_path, file_path.parent))
            elif suffix in VIDEO_SOURCE_SUFFIXES:
                status_callback(f"Repairing video {index}/{total}...")
                outputs.append(repair_video_for_playback(file_path, replace_original=False))
            else:
                raise RuntimeError(f"{file_path.name} cannot be converted or repaired as video.")
        elif mode == "gif":
            if suffix not in VIDEO_SOURCE_SUFFIXES:
                raise RuntimeError(f"{file_path.name} is not a supported video file for GIF conversion.")
            status_callback(f"Converting to GIF {index}/{total}...")
            outputs.append(convert_source_to_gif(file_path, file_path.parent, delete_source=False))
        else:
            if suffix == ".mp3":
                raise RuntimeError(f"{file_path.name} is already an MP3 file.")
            if suffix not in MEDIA_SOURCE_SUFFIXES and suffix != ".gif":
                raise RuntimeError(f"{file_path.name} is not a supported media file for audio extraction.")
            status_callback(f"Converting to MP3 {index}/{total}...")
            outputs.append(convert_source_to_mp3(file_path, file_path.parent, delete_source=False))

    return ", ".join(str(output) for output in outputs)


def download_best_gifs(urls, progress_hook, status_callback, cancel_event):
    converted = 0
    saved_locations = []
    for url in urls:
        check_cancelled(cancel_event)
        target = resolve_download_target(url, status_callback, "gif")
        check_cancelled(cancel_event)
        with tempfile.TemporaryDirectory(prefix="media-downloader-") as temp_dir:
            temp_dir_path = Path(temp_dir)
            temp_template = temp_dir_path / output_source_filename_template(target["is_collection"])
            options = make_ydl_options(
                progress_hook,
                temp_template,
                merge_output_format="mp4",
                http_headers=target.get("http_headers"),
                cookies_from_browser=target.get("cookies_from_browser"),
            )

            if target["is_collection"]:
                status_callback(f"Downloading {target['name']} source media into {target['directory'].name}...")
            else:
                status_callback("Downloading source media for GIF conversion...")

            if target.get("direct_media_url"):
                source = temp_dir_path / direct_media_filename(target, ".source.mp4")
                status_callback("Found media stream. Downloading source for GIF conversion...")
                download_direct_media(target["direct_media_url"], source, progress_hook, target.get("http_headers"))
            else:
                with get_yt_dlp().YoutubeDL(options) as ydl:
                    code = ydl.download([target["url"]])
                if code:
                    raise RuntimeError("One or more downloads failed before GIF conversion.")

            source_files = sorted(
                path
                for path in temp_dir_path.rglob("*")
                if path.is_file() and path.suffix.lower() in {".mp4", ".webm", ".mkv", ".mov"}
            )
            if not source_files:
                raise RuntimeError("No downloaded video files were found to convert.")

            total = len(source_files)
            for index, source in enumerate(source_files, start=1):
                check_cancelled(cancel_event)
                status_callback(f"Converting to GIF {index}/{total}...")
                convert_source_to_gif(source, target["directory"])
                converted += 1
            saved_locations.append(target["directory"])

    return f"{format_saved_locations(saved_locations)} ({converted} GIF file{'s' if converted != 1 else ''})"


def download_best_mp3s(urls, progress_hook, status_callback, cancel_event):
    converted = 0
    saved_locations = []
    for url in urls:
        check_cancelled(cancel_event)
        target = resolve_download_target(url, status_callback, "audio")
        check_cancelled(cancel_event)
        with tempfile.TemporaryDirectory(prefix="media-downloader-") as temp_dir:
            temp_dir_path = Path(temp_dir)
            temp_template = temp_dir_path / output_source_filename_template(target["is_collection"])
            options = make_ydl_options(
                progress_hook,
                temp_template,
                format_selector="ba/bestaudio/best",
                merge_output_format=None,
                http_headers=target.get("http_headers"),
                cookies_from_browser=target.get("cookies_from_browser"),
            )

            if target["is_collection"]:
                status_callback(f"Downloading {target['name']} audio into {target['directory'].name}...")
            else:
                status_callback("Downloading audio for MP3 conversion...")

            if target.get("direct_media_url"):
                source = temp_dir_path / direct_media_filename(target, ".source.mp4")
                status_callback("Found media stream. Downloading source for audio conversion...")
                download_direct_media(target["direct_media_url"], source, progress_hook, target.get("http_headers"))
            else:
                with get_yt_dlp().YoutubeDL(options) as ydl:
                    code = ydl.download([target["url"]])
                if code:
                    raise RuntimeError("One or more downloads failed before MP3 conversion.")

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
                status_callback(f"Converting to MP3 {index}/{total}...")
                convert_source_to_mp3(source, target["directory"])
                converted += 1
            saved_locations.append(target["directory"])

    return f"{format_saved_locations(saved_locations)} ({converted} MP3 file{'s' if converted != 1 else ''})"


def download_best_frames(urls, progress_hook, status_callback, cancel_event):
    frame_folders = []
    for url in urls:
        check_cancelled(cancel_event)
        target = resolve_download_target(url, status_callback, "video")
        check_cancelled(cancel_event)
        with tempfile.TemporaryDirectory(prefix="media-downloader-") as temp_dir:
            temp_dir_path = Path(temp_dir)
            temp_template = temp_dir_path / output_source_filename_template(target["is_collection"])
            options = make_ydl_options(
                progress_hook,
                temp_template,
                merge_output_format="mp4",
                http_headers=target.get("http_headers"),
                cookies_from_browser=target.get("cookies_from_browser"),
            )

            if target["is_collection"]:
                status_callback(f"Downloading {target['name']} source media for frame extraction...")
            else:
                status_callback("Downloading source media for frame extraction...")

            if target.get("direct_media_url"):
                source = temp_dir_path / direct_media_filename(target, ".source.mp4")
                status_callback("Found media stream. Downloading source for frame extraction...")
                download_direct_media(target["direct_media_url"], source, progress_hook, target.get("http_headers"))
            else:
                with get_yt_dlp().YoutubeDL(options) as ydl:
                    code = ydl.download([target["url"]])
                if code:
                    raise RuntimeError("One or more downloads failed before frame extraction.")

            source_files = sorted(
                path
                for path in temp_dir_path.rglob("*")
                if path.is_file() and path.suffix.lower() in (VIDEO_SOURCE_SUFFIXES | {".gif"})
            )
            if not source_files:
                raise RuntimeError("No downloaded GIF or video files were found for frame extraction.")

            total = len(source_files)
            for index, source in enumerate(source_files, start=1):
                check_cancelled(cancel_event)
                status_callback(f"Extracting unique frames {index}/{total}...")
                frame_folders.append(extract_unique_frames(source, target["directory"]))

    return f"{format_saved_locations(frame_folders)} ({len(frame_folders)} frame folder{'s' if len(frame_folders) != 1 else ''})"


def download_best_gif(url, progress_hook, status_callback):
    temp_template = DOWNLOAD_DIR / "%(title).120B [%(id)s].source.%(ext)s"
    options = make_ydl_options(progress_hook, temp_template, allow_playlists=False)
    with get_yt_dlp().YoutubeDL(options) as ydl:
        info = ydl.extract_info(url, download=True)
        source = Path(ydl.prepare_filename(info)).with_suffix(".mp4")
        if not source.exists():
            requested = info.get("requested_downloads") or []
            if requested:
                source = Path(requested[0].get("filepath", source))

    if not source.exists():
        raise RuntimeError("The video downloaded, but the saved file could not be found.")

    status_callback("Converting to GIF...")
    gif_path = convert_source_to_gif(source, DOWNLOAD_DIR)
    return gif_path


def output_filename_template(is_collection):
    if is_collection:
        return "%(playlist_index)03d - %(title).120B [%(id)s].%(ext)s"
    return "%(title).120B [%(id)s].%(ext)s"


def output_source_filename_template(is_collection):
    if is_collection:
        return "%(playlist_index)03d - %(title).120B [%(id)s].source.%(ext)s"
    return "%(title).120B [%(id)s].source.%(ext)s"


def check_cancelled(cancel_event):
    if cancel_event.is_set():
        raise get_download_cancelled()("Download cancelled.")


def resolve_download_target(url, status_callback, mode):
    normalized_url = normalize_media_url(url)
    status_callback("Scanning link details...")
    extraction_error = None
    try:
        info = extract_download_info(normalized_url)
    except Exception as exc:
        extraction_error = exc
        if is_x_or_twitter_url(normalized_url):
            fallback = extract_twitter_fallback(normalized_url)
            if fallback:
                return direct_download_target(normalized_url, fallback)
        status_callback("Scanning page for embedded media...")
        fallback_target = resolve_page_fallback_target(normalized_url, mode, status_callback)
        if fallback_target:
            return fallback_target
        raise RuntimeError(media_detection_error(normalized_url, mode, exc)) from exc

    if not has_media_for_mode(info, mode):
        if is_x_or_twitter_url(normalized_url):
            fallback = extract_twitter_fallback(normalized_url)
            if fallback:
                return direct_download_target(normalized_url, fallback)
        status_callback("Scanning page for embedded media...")
        fallback_target = resolve_page_fallback_target(normalized_url, mode, status_callback)
        if fallback_target:
            return fallback_target
        if extraction_error:
            raise RuntimeError(media_detection_error(normalized_url, mode, extraction_error)) from extraction_error
        raise RuntimeError(no_media_message(normalized_url, mode))

    return target_from_info(normalized_url, info)


def target_from_info(url, info, http_headers=None, cookies_from_browser=None):
    if cookies_from_browser is None and isinstance(info, dict):
        cookies_from_browser = info.get("_media_downloader_cookies_from_browser")
    collection_name = collection_title(info)
    if collection_name:
        directory = unique_path(DOWNLOAD_DIR / safe_folder_name(collection_name))
        directory.mkdir(parents=True, exist_ok=True)
        return {
            "directory": directory,
            "is_collection": True,
            "name": collection_name,
            "url": url,
            "http_headers": http_headers,
            "cookies_from_browser": cookies_from_browser,
        }
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    return {
        "directory": DOWNLOAD_DIR,
        "is_collection": False,
        "name": None,
        "url": url,
        "http_headers": http_headers,
        "cookies_from_browser": cookies_from_browser,
    }


def direct_download_target(url, fallback):
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    headers = media_request_headers(url)
    return {
        "directory": DOWNLOAD_DIR,
        "is_collection": False,
        "name": fallback.get("title") or twitter_status_id(url) or "Twitter media",
        "url": url,
        "direct_media_url": fallback["video_url"],
        "id": twitter_status_id(url) or "twitter",
        "http_headers": headers,
        "source": "X/Twitter fallback",
    }


def extract_download_info(url):
    errors = []
    for cookies_from_browser in [None, *available_browser_cookie_sources()]:
        try:
            info = extract_download_info_once(url, cookies_from_browser=cookies_from_browser)
            if isinstance(info, dict) and cookies_from_browser:
                info["_media_downloader_cookies_from_browser"] = cookies_from_browser
            return info
        except Exception as exc:
            errors.append(exc)
    raise errors[-1]


def extract_download_info_once(url, cookies_from_browser=None, quick=False, http_headers=None):
    retry_count = 1 if quick else 5
    options = {
        "quiet": True,
        "no_warnings": True,
        "windowsfilenames": True,
        "http_headers": http_headers or media_request_headers(url),
        "retries": retry_count,
        "fragment_retries": retry_count,
        "extractor_retries": 1 if quick else 3,
        "geo_bypass": True,
        "socket_timeout": 8 if quick else 20,
    }
    if cookies_from_browser:
        options["cookiesfrombrowser"] = cookies_from_browser
    with get_yt_dlp().YoutubeDL(options) as ydl:
        return ydl.extract_info(url, download=False)


def resolve_page_fallback_target(url, mode, status_callback=None):
    title, candidates = discover_page_candidate_urls(url)

    direct_candidates = [candidate for candidate in candidates if is_direct_media_url(candidate)]
    if direct_candidates:
        if status_callback:
            status_callback("Found embedded media URL...")
        return direct_media_target(url, direct_candidates[0], title, "page scan")

    for index, candidate in enumerate(candidates[:PAGE_SCAN_MAX_CANDIDATES], start=1):
        if status_callback:
            status_callback(f"Checking embedded player {index}/{min(len(candidates), PAGE_SCAN_MAX_CANDIDATES)}...")
        headers = media_request_headers(url)
        try:
            info = extract_download_info_once(candidate, quick=True)
            if has_media_for_mode(info, mode):
                return target_from_info(candidate, info, http_headers=headers)
        except Exception:
            pass

    if status_callback:
        status_callback("Opening browser page scanner...")
    browser_title, browser_candidates, browser_headers, browser_direct_candidates = discover_browser_media_urls(url, status_callback)
    title = browser_title or title
    if not browser_candidates:
        if status_callback:
            status_callback("No browser media stream found.")
        return None

    direct_candidate_limit = min(len(browser_direct_candidates), BROWSER_DIRECT_CANDIDATE_LIMIT)
    for index, media_url in enumerate(browser_direct_candidates[:direct_candidate_limit], start=1):
        if status_callback:
            status_callback(f"Testing browser stream {index}/{direct_candidate_limit}...")
        try:
            headers = browser_headers.get(media_url) or media_request_headers(url)
            verify_direct_media_access(media_url, headers)
            info = extract_download_info_once(media_url, quick=True, http_headers=headers)
            if not has_media_for_mode(info, mode):
                continue
            if status_callback:
                status_callback("Found usable browser media stream...")
            return direct_media_target(url, media_url, title, "browser scan", headers)
        except Exception:
            continue

    remaining_candidates = [candidate for candidate in browser_candidates if candidate not in browser_direct_candidates]
    for index, candidate in enumerate(remaining_candidates[:PAGE_SCAN_MAX_CANDIDATES], start=1):
        if status_callback:
            status_callback(
                f"Checking browser media candidate {index}/{min(len(remaining_candidates), PAGE_SCAN_MAX_CANDIDATES)}..."
            )
        try:
            info = extract_download_info_once(candidate, quick=True)
            if has_media_for_mode(info, mode):
                return target_from_info(candidate, info, http_headers=browser_headers.get(candidate) or media_request_headers(url))
        except Exception:
            pass

    if status_callback:
        status_callback("No downloadable browser media stream found.")
    return None


def discover_page_candidate_urls(url):
    try:
        page = fetch_page_html(url)
    except Exception:
        return None, []

    title = page_title(page)
    candidates = extract_candidate_urls_from_html(page, url)
    return title, prioritize_candidate_urls(candidates)[:PAGE_SCAN_MAX_CANDIDATES]


def discover_browser_media_urls(url, status_callback=None):
    browser_exe = find_system_browser_exe()
    if not browser_exe:
        return None, [], {}, []

    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        return None, [], {}, []

    found_urls = []
    request_headers = {}
    direct_stream_priorities = {}
    rendered_pages = []
    page_title_text = None
    browser = None

    def remember(candidate, headers=None, stream_priority=0):
        add_candidate_url(found_urls, url, candidate)
        absolute = urljoin(url, clean_candidate_url(candidate) or "")
        if absolute in found_urls and headers:
            request_headers[absolute] = browser_media_headers(headers, url)
        if absolute in found_urls and stream_priority:
            direct_stream_priorities[absolute] = max(direct_stream_priorities.get(absolute, 0), stream_priority)

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                executable_path=str(browser_exe),
                headless=True,
                args=[
                    "--headless=new",
                    "--disable-gpu",
                    "--disable-extensions",
                    "--disable-background-networking",
                    "--disable-popup-blocking",
                    "--disable-dev-shm-usage",
                    "--no-first-run",
                    "--no-default-browser-check",
                    "--mute-audio",
                    "--window-position=-32000,-32000",
                    "--window-size=1,1",
                    "--autoplay-policy=no-user-gesture-required",
                ],
            )
            context = browser.new_context(
                user_agent=BROWSER_USER_AGENT,
                extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
                ignore_https_errors=True,
            )
            page = context.new_page()

            def on_response(response):
                response_url = response.url
                content_type = response.headers.get("content-type", "")
                if is_direct_media_url(response_url) or BROWSER_MEDIA_CONTENT_RE.search(content_type):
                    try:
                        response_headers = response.request.all_headers()
                    except Exception:
                        response_headers = response.request.headers
                    try:
                        cookies = context.cookies([response_url])
                        if cookies and "cookie" not in {name.lower() for name in response_headers}:
                            response_headers["cookie"] = "; ".join(f"{cookie['name']}={cookie['value']}" for cookie in cookies)
                    except Exception:
                        pass
                    remember(response_url, response_headers, browser_stream_priority(response_url, content_type))

            page.on("response", on_response)
            page.goto(url, wait_until="domcontentloaded", timeout=BROWSER_SCAN_TIMEOUT_MS)
            if status_callback:
                status_callback("Activating browser media players...")
            activate_browser_media(page)
            page.wait_for_timeout(750)
            for frame in page.frames:
                activate_browser_media(frame)
            for offset in (0, 650, 1300, 1950):
                try:
                    page.evaluate(f"window.scrollTo(0, {offset});")
                except Exception:
                    pass
                for frame in page.frames:
                    activate_browser_media(frame)
                page.wait_for_timeout(max(500, int(BROWSER_SCAN_SECONDS * 250)))
            if status_callback:
                status_callback("Watching browser media requests...")
            page_title_text = page.title()
            for frame in page.frames:
                remember(frame.url)
                try:
                    rendered_pages.append(frame.content())
                except Exception:
                    pass
            context.close()
            browser.close()
    except Exception:
        try:
            if browser:
                browser.close()
        except Exception:
            pass
        return None, [], {}, []

    for rendered_html in rendered_pages:
        for candidate in extract_candidate_urls_from_html(rendered_html, url):
            remember(candidate)
    for candidate in found_urls:
        if is_direct_media_url(candidate):
            direct_stream_priorities.setdefault(candidate, 2)
    direct_candidates = sorted(direct_stream_priorities, key=lambda candidate: direct_stream_priorities[candidate], reverse=True)
    candidates = prioritize_browser_candidates(found_urls, direct_candidates)
    return page_title_text, candidates[:PAGE_SCAN_MAX_CANDIDATES], request_headers, direct_candidates


def activate_browser_media(frame):
    try:
        frame.evaluate(
            """
            () => {
                for (const video of document.querySelectorAll('video')) {
                    video.muted = true;
                    video.playsInline = true;
                    video.scrollIntoView({block: 'center', inline: 'center'});
                    const playPromise = video.play();
                    if (playPromise && playPromise.catch) playPromise.catch(() => {});
                }
                const playControls = [...document.querySelectorAll('button, [role="button"]')]
                    .filter(node => /play|watch|start/i.test(`${node.getAttribute('aria-label') || ''} ${node.title || ''} ${node.textContent || ''}`))
                    .slice(0, 3);
                for (const control of playControls) {
                    try { control.click(); } catch (_) {}
                }
            }
            """
        )
    except Exception:
        pass


def browser_media_headers(headers, page_url):
    safe_headers = media_request_headers(page_url)
    allowed_headers = {"accept", "accept-language", "cookie", "origin", "referer", "user-agent"}
    for name, value in (headers or {}).items():
        if name.lower() in allowed_headers and value:
            safe_headers[name.title()] = value
    return safe_headers


def browser_stream_priority(url, content_type):
    content_type = (content_type or "").lower()
    path = urlparse(url).path.lower()
    if BROWSER_STREAM_SEGMENT_RE.search(path):
        return 0
    if any(marker in content_type for marker in ("mpegurl", "dash+xml")) or path.endswith((".m3u8", ".mpd")):
        return 3
    if is_direct_media_url(url):
        return 2
    if "video/" in content_type or "audio/" in content_type:
        return 1
    return 0


def prioritize_browser_candidates(candidates, direct_candidates):
    direct_set = set(direct_candidates)
    return direct_candidates + [candidate for candidate in candidates if candidate not in direct_set]


def extract_candidate_urls_from_html(page, base_url):
    prepared = html.unescape(page or "").replace("\\/", "/")
    candidates = []
    for pattern in (MEDIA_URL_RE, IFRAME_URL_RE, SOURCE_URL_RE, META_MEDIA_URL_RE):
        for match in pattern.finditer(prepared):
            candidate = match.groupdict().get("src") or match.group(0)
            add_candidate_url(candidates, base_url, candidate)
    return candidates


def fetch_page_html(url):
    request = Request(url, headers=media_request_headers(url))
    with urlopen(request, timeout=30) as response:
        content_type = response.headers.get("Content-Type", "")
        if "text/" not in content_type and "html" not in content_type and "json" not in content_type:
            return ""
        raw = response.read(3_000_000)
        encoding = response.headers.get_content_charset() or "utf-8"
    return raw.decode(encoding, errors="replace")


def add_candidate_url(candidates, base_url, candidate):
    candidate = clean_candidate_url(candidate)
    if not candidate:
        return
    absolute = urljoin(base_url, candidate)
    if not absolute.startswith(("http://", "https://")):
        return
    if absolute not in candidates:
        candidates.append(absolute)


def prioritize_candidate_urls(candidates):
    direct = [candidate for candidate in candidates if is_direct_media_url(candidate)]
    other = [candidate for candidate in candidates if candidate not in direct]
    return direct + other


def clean_candidate_url(candidate):
    candidate = html.unescape(unquote(candidate)).strip().strip("\"'()[]{}")
    candidate = candidate.replace("\\u0026", "&").replace("\\/", "/")
    if not candidate or candidate.startswith(("data:", "blob:", "javascript:")):
        return None
    return candidate


def page_title(page):
    match = re.search(r"<title[^>]*>(?P<title>.*?)</title>", page, re.IGNORECASE | re.DOTALL)
    if not match:
        return None
    return re.sub(r"\s+", " ", html.unescape(match.group("title"))).strip()


def direct_media_target(page_url, media_url, title, source="page scan", http_headers=None):
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    parsed = urlparse(media_url)
    media_id = Path(parsed.path).stem or "media"
    return {
        "directory": DOWNLOAD_DIR,
        "is_collection": False,
        "name": title or media_id,
        "url": page_url,
        "direct_media_url": media_url,
        "id": media_id,
        "http_headers": http_headers or media_request_headers(page_url),
        "source": source,
    }


def is_direct_media_url(url):
    suffix = Path(urlparse(url).path).suffix.lower()
    return suffix in {".mp4", ".webm", ".mov", ".mkv", ".m3u8", ".mpd"}


def media_request_headers(referer=None):
    headers = dict(COMMON_HTTP_HEADERS)
    if referer:
        parsed = urlparse(referer)
        if parsed.scheme and parsed.netloc:
            headers["Referer"] = referer
            headers["Origin"] = f"{parsed.scheme}://{parsed.netloc}"
    return headers


def available_browser_cookie_sources():
    sources = []
    local_app_data = Path(os.environ.get("LOCALAPPDATA", ""))
    app_data = Path(os.environ.get("APPDATA", ""))
    if (local_app_data / "Google" / "Chrome" / "User Data").exists():
        sources.append(("chrome",))
    if (local_app_data / "Microsoft" / "Edge" / "User Data").exists():
        sources.append(("edge",))
    if (app_data / "Mozilla" / "Firefox" / "Profiles").exists():
        sources.append(("firefox",))
    return sources


def find_system_browser_exe():
    candidates = []
    for env_name, relative in (
        ("PROGRAMFILES", "Microsoft/Edge/Application/msedge.exe"),
        ("PROGRAMFILES(X86)", "Microsoft/Edge/Application/msedge.exe"),
        ("LOCALAPPDATA", "Microsoft/Edge/Application/msedge.exe"),
        ("PROGRAMFILES", "Google/Chrome/Application/chrome.exe"),
        ("PROGRAMFILES(X86)", "Google/Chrome/Application/chrome.exe"),
        ("LOCALAPPDATA", "Google/Chrome/Application/chrome.exe"),
    ):
        base = os.environ.get(env_name)
        if base:
            candidates.append(Path(base) / Path(relative))
    for command in ("msedge.exe", "chrome.exe"):
        path = shutil.which(command)
        if path:
            candidates.append(Path(path))
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


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
    try:
        with urlopen(request, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, URLError, json.JSONDecodeError):
        return None

    data = payload.get("data") if payload.get("success") else None
    if not isinstance(data, dict):
        return None

    media_url = best_twitter_fallback_url(data)
    if not media_url:
        return None

    return {
        "title": data.get("title") or f"Twitter media {tweet_id}",
        "video_url": media_url,
    }


def best_twitter_fallback_url(data):
    resolutions = data.get("resolutions") or []
    for item in resolutions:
        if isinstance(item, dict) and item.get("videoUrl"):
            return item["videoUrl"]
    return data.get("videoUrl") or data.get("downloadUrl")


def twitter_status_id(url):
    match = re.search(r"/(?:i/)?status(?:es)?/(\d+)", url)
    return match.group(1) if match else None


def has_media_for_mode(info, mode):
    entries = info.get("entries") if isinstance(info, dict) else None
    if entries:
        return any(has_media_for_mode(entry, mode) for entry in entries if isinstance(entry, dict))

    formats = info.get("formats") if isinstance(info, dict) else None
    if not formats:
        return bool(info.get("url")) if isinstance(info, dict) else False

    if mode in {"video", "gif"}:
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
    return {"gif": "video for GIF conversion", "audio": "audio", "video": "video", "frames": "video"}.get(mode, "media")


def is_x_or_twitter_url(url):
    host = urlparse(url).netloc.lower()
    return host in {"x.com", "twitter.com"} or host.endswith(".x.com") or host.endswith(".twitter.com")


def clean_download_error(message):
    return re.sub(r"^ERROR:\s*", "", message).strip() or "The downloader could not read media from this link."


def collection_title(info):
    if not isinstance(info, dict):
        return None

    entries = info.get("entries")
    entry_count = len(entries) if isinstance(entries, list) else 0
    is_collection = info.get("_type") in {"playlist", "multi_video"} or entry_count > 1
    if not is_collection:
        return None

    return info.get("playlist_title") or info.get("title") or info.get("id") or "Media Download"


def safe_folder_name(name):
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")
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


def direct_media_filename(target, suffix):
    title = safe_folder_name(target.get("name") or "Twitter media")
    media_id = target.get("id") or "twitter"
    return unique_path(Path(f"{title[:120]} [{media_id}]{suffix}")).name


def format_download_size(size):
    if not size:
        return "0 B"
    units = ("B", "KB", "MB", "GB", "TB")
    value = float(size)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} {unit}"
        value /= 1024
    return "0 B"


def media_preview(input_text):
    urls = extract_urls(input_text)
    local_files = extract_local_files(input_text, urls)
    if local_files:
        return local_media_preview(local_files[0])
    if urls:
        return link_media_preview(urls[0])
    return {"type": "preview", "message": "No media selected."}


def local_media_preview(path):
    path = Path(path)
    if path.suffix.lower() not in LOCAL_FILE_SUFFIXES:
        return {"type": "preview", "message": "Choose a supported media file to see a preview."}
    if path.suffix.lower() in {".mp3", ".m4a", ".aac", ".flac", ".ogg", ".opus", ".wav"}:
        return {"type": "preview", "title": path.name, "message": "Audio file selected."}

    preview_dir = Path(tempfile.gettempdir()) / "MediaDownloader" / "previews"
    preview_dir.mkdir(parents=True, exist_ok=True)
    preview_name = f"{abs(hash((str(path.resolve()), path.stat().st_mtime_ns))):x}.jpg"
    preview_path = preview_dir / preview_name
    if not preview_path.exists():
        run_ffmpeg(
            [
                str(find_ffmpeg_exe()),
                "-hide_banner",
                "-loglevel",
                "error",
                "-ss",
                "0.5",
                "-i",
                str(path),
                "-frames:v",
                "1",
                "-vf",
                "scale=min(320\\,iw):-2",
                "-q:v",
                "4",
                "-y",
                str(preview_path),
            ]
        )
    image = base64.b64encode(preview_path.read_bytes()).decode("ascii")
    return {
        "type": "preview",
        "title": path.name,
        "message": f"Local file | {format_download_size(path.stat().st_size)}",
        "src": f"data:image/jpeg;base64,{image}",
    }


def link_media_preview(url):
    options = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "skip_download": True,
        "http_headers": media_request_headers(url),
        "socket_timeout": 12,
        "retries": 1,
        "extractor_retries": 1,
    }
    with get_yt_dlp().YoutubeDL(options) as ydl:
        info = ydl.extract_info(url, download=False)
    if info.get("entries"):
        info = next((entry for entry in info["entries"] if entry), info)
    thumbnail = info.get("thumbnail")
    title = info.get("title") or "Media preview"
    details = []
    duration = info.get("duration")
    if duration:
        details.append(format_duration(duration))
    if info.get("width") and info.get("height"):
        details.append(f"{info['width']}x{info['height']}")
    filesize = info.get("filesize") or info.get("filesize_approx")
    if filesize:
        details.append(format_download_size(filesize))
    source = info.get("uploader") or info.get("channel") or info.get("extractor_key") or info.get("extractor")
    if source:
        details.append(str(source))
    message = " | ".join(details) or "Media link"
    if thumbnail:
        return {"type": "preview", "title": title, "message": message, "src": thumbnail}
    return {"type": "preview", "title": title, "message": "No preview image is available for this link."}


def format_duration(seconds):
    seconds = int(seconds)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}:{minutes:02}:{seconds:02}"
    return f"{minutes}:{seconds:02}"


def download_direct_media(url, output_path, progress_hook, http_headers=None):
    output_path = unique_path(output_path)
    headers = http_headers or media_request_headers(url)
    progress_hook({"status": "checking"})
    verify_direct_media_access(url, headers)
    progress_hook({"status": "preparing"})
    options = make_ydl_options(
        progress_hook,
        output_path,
        allow_playlists=False,
        format_selector="best",
        merge_output_format=None,
    )
    options["http_headers"] = headers
    options["socket_timeout"] = DIRECT_MEDIA_DOWNLOAD_TIMEOUT_SECONDS
    options["retries"] = 0
    options["fragment_retries"] = 0
    with get_yt_dlp().YoutubeDL(options) as ydl:
        code = ydl.download([url])
    if code:
        raise RuntimeError("Direct media download failed.")
    return output_path


def verify_direct_media_access(url, headers):
    deadline = time.monotonic() + DIRECT_MEDIA_PROBE_TIMEOUT_SECONDS
    is_hls = urlparse(url).path.lower().endswith(".m3u8")
    request_headers = dict(headers)
    if not is_hls:
        request_headers["Range"] = "bytes=0-1"
    request = Request(url, headers=request_headers)
    try:
        with urlopen(request, timeout=remaining_media_probe_timeout(deadline)) as response:
            if response.status >= 400:
                raise RuntimeError(f"Media server returned HTTP {response.status}.")
            content_type = response.headers.get("Content-Type", "")
            payload = response.read(HLS_PLAYLIST_PROBE_BYTES if is_hls or "mpegurl" in content_type.lower() else 1)
        if is_hls or "mpegurl" in content_type.lower():
            verify_hls_media_segment(url, payload, headers, deadline=deadline)
    except URLError as exc:
        reason = getattr(exc, "reason", None) or str(exc)
        raise RuntimeError(f"Could not connect to the captured media stream: {reason}") from exc
    except TimeoutError as exc:
        raise RuntimeError("The captured media stream did not respond within 15 seconds.") from exc


def remaining_media_probe_timeout(deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError
    return min(DIRECT_MEDIA_CONNECT_TIMEOUT_SECONDS, max(1, remaining))


def verify_hls_media_segment(playlist_url, playlist_payload, headers, depth=0, deadline=None):
    deadline = deadline or time.monotonic() + DIRECT_MEDIA_PROBE_TIMEOUT_SECONDS
    lines = [line.strip() for line in playlist_payload.decode("utf-8", errors="replace").splitlines()]
    media_lines = [line for line in lines if line and not line.startswith("#")]
    if not media_lines:
        raise RuntimeError("The captured HLS playlist does not contain media segments.")

    next_url = urljoin(playlist_url, media_lines[0])
    is_master_playlist = any(line.startswith("#EXT-X-STREAM-INF") for line in lines)
    if is_master_playlist and depth < 2:
        request = Request(next_url, headers=dict(headers))
        with urlopen(request, timeout=remaining_media_probe_timeout(deadline)) as response:
            if response.status >= 400:
                raise RuntimeError(f"Media server returned HTTP {response.status}.")
            payload = response.read(HLS_PLAYLIST_PROBE_BYTES)
        return verify_hls_media_segment(next_url, payload, headers, depth + 1, deadline)

    request_headers = dict(headers)
    request_headers["Range"] = "bytes=0-1"
    request = Request(next_url, headers=request_headers)
    with urlopen(request, timeout=remaining_media_probe_timeout(deadline)) as response:
        if response.status >= 400:
            raise RuntimeError(f"Media server returned HTTP {response.status}.")
        response.read(1)


def remux_new_videos(directory, started_at):
    candidates = sorted(
        path
        for path in directory.rglob("*")
        if path.is_file()
        and path.suffix.lower() in VIDEO_SOURCE_SUFFIXES
        and path.stat().st_mtime >= started_at - 2
    )
    for path in candidates:
        remux_video_lossless(path)


def repair_video_for_playback(path, replace_original=True):
    repaired = remux_video_lossless(path, replace_original=replace_original)
    rebuilt = rebuild_video_for_playback(repaired, replace_original=True)
    return rebuilt or repaired


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


def convert_source_to_gif(source, output_dir, delete_source=True):
    output_dir.mkdir(parents=True, exist_ok=True)
    gif_path = unique_path(output_dir / source.with_suffix(".gif").name.replace(".source", ""))
    palette = source.with_suffix(".palette.png")
    ffmpeg_exe = find_ffmpeg_exe()

    try:
        run_ffmpeg(
            [
                ffmpeg_exe,
                "-y",
                "-i",
                str(source),
                "-vf",
                "fps=30,scale=iw:ih:flags=lanczos,palettegen=stats_mode=full",
                str(palette),
            ]
        )
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
            ]
        )
    finally:
        temp_files = [palette]
        if delete_source:
            temp_files.append(source)
        for temp_file in temp_files:
            try:
                temp_file.unlink(missing_ok=True)
            except OSError:
                pass
    return gif_path


def extract_unique_frames(source, output_dir):
    source = Path(source)
    output_dir = Path(output_dir)
    folder_name = safe_folder_name(source.stem.replace(".source", ""))
    frame_dir = unique_path(output_dir / folder_name)
    frame_dir.mkdir(parents=True, exist_ok=False)
    try:
        run_ffmpeg(
            [
                find_ffmpeg_exe(),
                "-y",
                "-i",
                str(source),
                "-map",
                "0:v:0",
                "-vf",
                "mpdecimate",
                "-vsync",
                "vfr",
                str(frame_dir / "frame-%06d.png"),
            ]
        )
        if not any(frame_dir.glob("frame-*.png")):
            raise RuntimeError("No video frames could be extracted.")
        return frame_dir
    except Exception:
        try:
            shutil.rmtree(frame_dir)
        except OSError:
            pass
        raise


def convert_gif_to_video(source, output_dir):
    output_dir.mkdir(parents=True, exist_ok=True)
    video_path = unique_path(output_dir / f"{source.stem}.mp4")
    ffmpeg_exe = find_ffmpeg_exe()

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
        ]
    )
    return video_path


def convert_source_to_mp3(source, output_dir, delete_source=True):
    output_dir.mkdir(parents=True, exist_ok=True)
    mp3_path = unique_path(output_dir / source.with_suffix(".mp3").name.replace(".source", ""))
    ffmpeg_exe = find_ffmpeg_exe()

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
            ]
        )
    finally:
        if delete_source:
            try:
                source.unlink(missing_ok=True)
            except OSError:
                pass
    return mp3_path


def unique_path(path):
    if not path.exists():
        return path

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


def run_ffmpeg(args, return_output=False):
    startupinfo = None
    creationflags = 0
    if os.name == "nt":
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        creationflags = subprocess.CREATE_NO_WINDOW

    completed = subprocess.run(
        args,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        startupinfo=startupinfo,
        creationflags=creationflags,
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or "FFmpeg failed.")
    if return_output:
        return f"{completed.stdout}\n{completed.stderr}"
    return None


class DownloadBridge:
    """Line-delimited JSON bridge used by the Photino desktop shell."""

    def __init__(self):
        self.cancel_event = threading.Event()
        self.worker = None
        self.output_lock = threading.Lock()
        self.download_total = 0
        self.download_current = 0
        self.download_completed = 0

    def emit(self, event):
        with self.output_lock:
            print(json.dumps(event, ensure_ascii=True), flush=True)

    def run(self):
        self.emit({"type": "ready"})
        for line in sys.stdin:
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue

            action = message.get("action")
            if action == "start":
                self.start(message.get("input", ""), message.get("mode", "video"))
            elif action == "preview":
                request_id = message.get("requestId")
                threading.Thread(
                    target=self._preview_worker,
                    args=(message.get("input", ""), request_id),
                    daemon=True,
                ).start()
            elif action == "cancel":
                self.cancel_event.set()
                self.emit({"type": "state", "phase": "downloading", "status": "Cancelling download..."})
            elif action == "shutdown":
                self.cancel_event.set()
                return

    def _preview_worker(self, input_text, request_id):
        try:
            preview = media_preview(input_text)
        except Exception as exc:
            preview = {"type": "preview", "message": f"Preview unavailable: {str(exc).splitlines()[0]}"}
        preview["requestId"] = request_id
        self.emit(preview)

    def start(self, input_text, mode):
        if self.worker and self.worker.is_alive():
            self.emit({"type": "error", "message": "A download is already running."})
            return

        urls = extract_urls(input_text)
        local_files = extract_local_files(input_text, urls)
        validation_error = validate_inputs(urls, local_files)
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
                "phase": "scanning",
                "status": f"Scanning {item_count} {item_text} for media...",
                "progress": 0,
                "indeterminate": False,
            }
        )
        self.worker = threading.Thread(target=self._download_worker, args=(urls, local_files, mode), daemon=True)
        self.worker.start()

    def _download_worker(self, urls, local_files, mode):
        try:
            outputs = []
            if local_files:
                outputs.append(process_local_files(local_files, mode, self._status, self.cancel_event))
            if mode == "video":
                output = download_best_video(urls, self._progress_hook, self._status, self.cancel_event) if urls else None
            elif mode == "gif":
                output = download_best_gifs(urls, self._progress_hook, self._status, self.cancel_event) if urls else None
            elif mode == "frames":
                output = download_best_frames(urls, self._progress_hook, self._status, self.cancel_event) if urls else None
            else:
                output = download_best_mp3s(urls, self._progress_hook, self._status, self.cancel_event) if urls else None
            if output:
                outputs.append(output)
            self.emit({"type": "state", "phase": "done", "status": f"Saved: {'; '.join(outputs)}", "progress": 100})
        except get_download_cancelled():
            self.emit({"type": "state", "phase": "cancelled", "status": "Download cancelled."})
        except Exception as exc:
            self.emit({"type": "error", "message": str(exc)})

    def _status(self, text):
        scanning_prefixes = (
            "Scanning page",
            "Opening browser",
            "Activating browser",
            "Watching browser",
            "Testing browser",
            "Checking browser",
            "Checking embedded",
            "Found embedded",
            "Found usable browser",
        )
        if "Converting" in text or "Extracting" in text:
            phase = "converting"
        elif text.startswith(scanning_prefixes):
            phase = "scanning"
        else:
            phase = "downloading"
        self.emit({"type": "state", "phase": phase, "status": text})

    def _progress_hook(self, data):
        if self.cancel_event.is_set():
            raise get_download_cancelled()("Download cancelled.")

        self._sync_download_position(data)
        status = data.get("status")
        count = self._download_count_prefix()
        if status == "checking":
            self.emit({"type": "state", "phase": "downloading", "status": "Checking media connection...", "indeterminate": True})
            return
        if status == "preparing":
            self.emit({"type": "state", "phase": "downloading", "status": "Connecting to media stream...", "indeterminate": True})
            return
        if status == "downloading":
            total = data.get("total_bytes") or data.get("total_bytes_estimate") or 0
            downloaded = data.get("downloaded_bytes") or 0
            speed = data.get("_speed_str", "").strip()
            fragment_index = data.get("fragment_index")
            fragment_count = data.get("fragment_count")
            if total:
                item_percent = downloaded / total * 100
                message = f"{count}Downloading... {item_percent:.1f}% {speed}".strip()
                self.emit({"type": "state", "phase": "downloading", "status": message, "progress": self._overall_progress(item_percent), "indeterminate": False})
            elif isinstance(fragment_index, int) and isinstance(fragment_count, int) and fragment_count > 0:
                item_percent = fragment_index / fragment_count * 100
                message = f"{count}Downloading stream... {fragment_index}/{fragment_count} parts {speed}".strip()
                self.emit({"type": "state", "phase": "downloading", "status": message, "progress": self._overall_progress(item_percent), "indeterminate": False})
            else:
                message = f"{count}Downloading stream... {format_download_size(downloaded)} received {speed}".strip()
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
    if "--bridge" in sys.argv:
        DownloadBridge().run()
    else:
        app = DownloadApp()
        app.mainloop()
