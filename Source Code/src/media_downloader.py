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
from pathlib import Path
from tkinter import filedialog, ttk
from urllib.error import URLError
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen

APP_NAME = "Media Downloader"
APP_VERSION = "1.1.5"
GITHUB_REPO = "TNephilim/media-downloader"
RELEASE_ASSET_NAME = "MediaDownloader.exe"
TWITIGER_EXTRACT_URL = "https://twitiger.com/api/extract?url="
DOWNLOAD_DIR = Path.home() / "Downloads"
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
FRAME_SOURCE_SUFFIXES = VIDEO_SOURCE_SUFFIXES | {".gif"}
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
        self.geometry("700x320")
        self.minsize(700, 320)
        self.resizable(False, False)
        self.configure(bg="#111827")

        self.events = queue.Queue()
        self.worker = None
        self.cancel_event = threading.Event()
        self.download_total = 0
        self.download_current = 0
        self.download_completed = 0

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
        self.style.configure(
            "Primary.TButton",
            background="#2563eb",
            foreground="#ffffff",
            bordercolor="#3b82f6",
            focusthickness=1,
            focuscolor="#60a5fa",
            padding=(10, 7),
            font=("Segoe UI", 10, "bold"),
        )
        self.style.map(
            "TButton",
            background=[("active", "#374151"), ("disabled", "#111827")],
            foreground=[("disabled", "#6b7280")],
        )
        self.style.map(
            "Primary.TButton",
            background=[("active", "#1d4ed8"), ("disabled", "#1e3a8a")],
            foreground=[("disabled", "#93a4bf")],
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
        root = ttk.Frame(self, padding=(18, 14, 18, 16))
        root.pack(fill=tk.BOTH, expand=True)
        root.columnconfigure(0, weight=1)

        title_row = ttk.Frame(root)
        title_row.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        title_row.columnconfigure(0, weight=1)

        title = ttk.Label(title_row, text=APP_NAME, font=("Segoe UI", 17, "bold"))
        title.grid(row=0, column=0, sticky="w")

        version = ttk.Label(title_row, text=f"v{APP_VERSION}", style="Muted.TLabel")
        version.grid(row=0, column=1, sticky="e")

        entry_row = ttk.Frame(root)
        entry_row.grid(row=1, column=0, sticky="ew")
        entry_row.columnconfigure(0, weight=1)
        entry_row.rowconfigure((0, 1, 2), weight=1)

        self.url_text = tk.Text(
            entry_row,
            height=2,
            font=("Segoe UI", 10),
            bg="#1f2937",
            fg="#f9fafb",
            insertbackground="#f9fafb",
            relief=tk.FLAT,
            bd=0,
            padx=10,
            pady=7,
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
        button_row.grid(row=2, column=0, sticky="ew", pady=(8, 6))
        button_row.columnconfigure((0, 1, 2, 3), weight=1)

        self.video_button = ttk.Button(
            button_row,
            text="Download Video",
            style="Primary.TButton",
            command=lambda: self._start_download("video"),
        )
        self.video_button.grid(row=0, column=0, sticky="ew", padx=(0, 4), ipady=6)

        self.mp3_button = ttk.Button(
            button_row,
            text="Download Audio",
            style="Primary.TButton",
            command=lambda: self._start_download("mp3"),
        )
        self.mp3_button.grid(row=0, column=1, sticky="ew", padx=4, ipady=6)

        self.gif_button = ttk.Button(
            button_row,
            text="Download GIF",
            style="Primary.TButton",
            command=lambda: self._start_download("gif"),
        )
        self.gif_button.grid(row=0, column=2, sticky="ew", padx=4, ipady=6)

        self.frames_button = ttk.Button(
            button_row,
            text="Extract Frames",
            style="Primary.TButton",
            command=lambda: self._start_download("frames"),
        )
        self.frames_button.grid(row=0, column=3, sticky="ew", padx=(4, 0), ipady=6)

        self.progress = ttk.Progressbar(
            root,
            variable=self.progress_var,
            maximum=100,
            mode="determinate",
            style="Download.Horizontal.TProgressbar",
        )
        self.progress.grid(row=3, column=0, sticky="ew", pady=(2, 6))

        self.phase_label = tk.Label(
            root,
            textvariable=self.phase_var,
            anchor="center",
            font=("Segoe UI", 11, "bold"),
            bg="#172033",
            fg="#93c5fd",
            padx=10,
            pady=7,
            bd=0,
            highlightthickness=1,
            highlightbackground="#374151",
        )
        self.phase_label.grid(row=4, column=0, sticky="ew")

        self.status_label = ttk.Label(root, textvariable=self.status_var, style="Muted.TLabel")
        self.status_label.grid(row=5, column=0, sticky="ew", pady=(6, 0))

        bottom_row = ttk.Frame(root)
        bottom_row.grid(row=6, column=0, sticky="ew", pady=(12, 0))
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

        self._sync_download_position(data)

        if data.get("status") == "downloading":
            total = data.get("total_bytes") or data.get("total_bytes_estimate") or 0
            downloaded = data.get("downloaded_bytes") or 0
            percent = (downloaded / total * 100) if total else 0
            speed = data.get("_speed_str", "").strip()
            count = self._download_count_prefix()
            self.events.put(("progress", percent, f"{count}Downloading... {percent:.1f}% {speed}".strip()))
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
                    self.progress_var.set(event[1])
                    self._set_status(event[2])
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
                    self.progress_var.set(100)
                    self._set_phase("done")
                    self._set_status(event[1])
                    self._set_busy(False)
                elif kind == "cancelled":
                    self._set_phase("cancelled")
                    self._set_status(event[1])
                    self._set_busy(False)
                elif kind == "error":
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
        self.frames_button.configure(state=state)
        self.choose_button.configure(state=state)
        self.cancel_button.configure(state=tk.NORMAL if busy else tk.DISABLED)

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
    format_selector="bv*+ba/best",
    format_sort=None,
    merge_output_format="mp4",
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
            format_sort=["res", "fps", "br"],
            merge_output_format="mp4",
        )

        if target["is_collection"]:
            status_callback(f"Downloading {target['name']} into {target['directory'].name}...")
        else:
            status_callback("Downloading best available video...")

        started_at = time.time()
        if target.get("direct_media_url"):
            output_path = target["directory"] / direct_media_filename(target, ".mp4")
            download_direct_media(target["direct_media_url"], output_path, progress_hook)
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

        if mode == "video":
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
        elif mode == "frames":
            if suffix not in FRAME_SOURCE_SUFFIXES:
                raise RuntimeError(f"{file_path.name} is not a supported video or GIF file for frame extraction.")
            status_callback(f"Extracting frames {index}/{total}...")
            outputs.append(extract_unique_frames(file_path, DOWNLOAD_DIR))
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
                format_sort=["res", "fps", "br"],
                merge_output_format="mp4",
            )

            if target["is_collection"]:
                status_callback(f"Downloading {target['name']} source media into {target['directory'].name}...")
            else:
                status_callback("Downloading source media for GIF conversion...")

            if target.get("direct_media_url"):
                source = temp_dir_path / direct_media_filename(target, ".source.mp4")
                download_direct_media(target["direct_media_url"], source, progress_hook)
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


def download_best_frames(urls, progress_hook, status_callback, cancel_event):
    extracted = 0
    saved_locations = []
    for url in urls:
        check_cancelled(cancel_event)
        target = resolve_download_target(url, status_callback, "frames")
        check_cancelled(cancel_event)
        with tempfile.TemporaryDirectory(prefix="media-downloader-") as temp_dir:
            temp_dir_path = Path(temp_dir)
            temp_template = temp_dir_path / output_source_filename_template(target["is_collection"])
            options = make_ydl_options(
                progress_hook,
                temp_template,
                format_sort=["res", "fps", "br"],
                merge_output_format="mp4",
            )

            if target["is_collection"]:
                status_callback(f"Downloading {target['name']} source media for frame extraction...")
            else:
                status_callback("Downloading source media for frame extraction...")

            if target.get("direct_media_url"):
                source = temp_dir_path / direct_media_filename(target, ".source.mp4")
                download_direct_media(target["direct_media_url"], source, progress_hook)
            else:
                with get_yt_dlp().YoutubeDL(options) as ydl:
                    code = ydl.download([target["url"]])
                if code:
                    raise RuntimeError("One or more downloads failed before frame extraction.")

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
                status_callback(f"Extracting frames {index}/{total}...")
                extract_unique_frames(source, target["directory"])
                extracted += 1
            saved_locations.append(target["directory"])

    return f"{format_saved_locations(saved_locations)} ({extracted} frame folder{'s' if extracted != 1 else ''})"


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
            )

            if target["is_collection"]:
                status_callback(f"Downloading {target['name']} audio into {target['directory'].name}...")
            else:
                status_callback("Downloading audio for MP3 conversion...")

            if target.get("direct_media_url"):
                source = temp_dir_path / direct_media_filename(target, ".source.mp4")
                download_direct_media(target["direct_media_url"], source, progress_hook)
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
    if not is_probable_collection_url(normalized_url):
        if is_x_or_twitter_url(normalized_url):
            fallback = extract_twitter_fallback(normalized_url)
            if fallback:
                return direct_download_target(normalized_url, fallback)
        return fast_download_target(normalized_url)

    status_callback("Scanning link details...")
    try:
        info = extract_download_info(normalized_url)
    except Exception as exc:
        if is_x_or_twitter_url(normalized_url):
            fallback = extract_twitter_fallback(normalized_url)
            if fallback:
                return direct_download_target(normalized_url, fallback)
        raise RuntimeError(media_detection_error(normalized_url, mode, exc)) from exc

    if not has_media_for_mode(info, mode):
        if is_x_or_twitter_url(normalized_url):
            fallback = extract_twitter_fallback(normalized_url)
            if fallback:
                return direct_download_target(normalized_url, fallback)
        raise RuntimeError(no_media_message(normalized_url, mode))

    collection_name = collection_title(info)
    if collection_name:
        directory = unique_path(DOWNLOAD_DIR / safe_folder_name(collection_name))
        directory.mkdir(parents=True, exist_ok=True)
        return {"directory": directory, "is_collection": True, "name": collection_name, "url": normalized_url}
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    return {"directory": DOWNLOAD_DIR, "is_collection": False, "name": None, "url": normalized_url}


def fast_download_target(url):
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    return {"directory": DOWNLOAD_DIR, "is_collection": False, "name": None, "url": url}


def direct_download_target(url, fallback):
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    return {
        "directory": DOWNLOAD_DIR,
        "is_collection": False,
        "name": fallback.get("title") or twitter_status_id(url) or "Twitter media",
        "url": url,
        "direct_media_url": fallback["video_url"],
        "id": twitter_status_id(url) or "twitter",
    }


def is_probable_collection_url(url):
    parsed = urlparse(url)
    host = parsed.netloc.lower()
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
    return "soundcloud.com" in host and "/sets/" in path


def extract_download_info(url):
    options = {
        "quiet": True,
        "no_warnings": True,
        "windowsfilenames": True,
    }
    with get_yt_dlp().YoutubeDL(options) as ydl:
        return ydl.extract_info(url, download=False)


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
        with urlopen(request, timeout=5) as response:
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


def direct_media_filename(target, suffix):
    title = safe_folder_name(target.get("name") or "Twitter media")
    media_id = target.get("id") or "twitter"
    return unique_path(Path(f"{title[:120]} [{media_id}]{suffix}")).name


def download_direct_media(url, output_path, progress_hook):
    output_path = unique_path(output_path)
    options = make_ydl_options(
        progress_hook,
        output_path,
        allow_playlists=False,
        format_selector="best",
        merge_output_format=None,
    )
    options["http_headers"] = {
        "Referer": "https://twitter.com/",
        "User-Agent": "Mozilla/5.0",
    }
    with get_yt_dlp().YoutubeDL(options) as ydl:
        code = ydl.download([url])
    if code:
        raise RuntimeError("Direct media download failed.")
    return output_path


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


def extract_unique_frames(source, output_root):
    output_root.mkdir(parents=True, exist_ok=True)
    frame_dir = unique_path(output_root / f"{safe_folder_name(source.stem)} Frames")
    frame_dir.mkdir(parents=True, exist_ok=True)
    output_pattern = frame_dir / "frame_%06d.png"

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
        ]
    )
    return frame_dir


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
            elif action == "cancel":
                self.cancel_event.set()
                self.emit({"type": "state", "phase": "downloading", "status": "Cancelling download..."})
            elif action == "shutdown":
                self.cancel_event.set()
                return

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
                "phase": "downloading",
                "status": f"Starting {item_count} {item_text}...",
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
        phase = "extracting" if "Extracting" in text else "converting" if "Converting" in text else "downloading"
        self.emit({"type": "state", "phase": phase, "status": text})

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
                message = f"{count}Downloading... {item_percent:.1f}% {speed}".strip()
                self.emit({"type": "state", "phase": "downloading", "status": message, "progress": self._overall_progress(item_percent), "indeterminate": False})
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
    if "--bridge" in sys.argv:
        DownloadBridge().run()
    else:
        app = DownloadApp()
        app.mainloop()
