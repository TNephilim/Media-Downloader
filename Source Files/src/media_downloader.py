import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import tkinter as tk
import tempfile
from pathlib import Path
from tkinter import ttk
from urllib.error import URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

import imageio_ffmpeg
import yt_dlp
from yt_dlp.utils import DownloadCancelled, sanitize_filename


APP_NAME = "Media Downloader"
APP_VERSION = "1.0.0"
GITHUB_REPO = "TNephilim/media-downloader"
RELEASE_ASSET_NAME = "MediaDownloader.exe"
DOWNLOAD_DIR = Path.home() / "Downloads"
URL_PATTERN = re.compile(r"https?://[^\s<>\"]+")
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


class DownloadApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_NAME)
        self.geometry("700x385")
        self.minsize(700, 385)
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
            text="Paste a media link, playlist, page, or multiple links. Pages are scanned by the downloader.",
            style="Muted.TLabel",
        )
        subtitle.grid(row=1, column=0, sticky="w", pady=(2, 7))

        entry_row = ttk.Frame(root)
        entry_row.grid(row=2, column=0, sticky="ew")
        entry_row.columnconfigure(0, weight=1)
        entry_row.rowconfigure((0, 1), weight=1)

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
            pady=8,
            wrap="word",
            highlightthickness=1,
            highlightbackground="#374151",
            highlightcolor="#3b82f6",
        )
        self.url_text.grid(row=0, column=0, rowspan=2, sticky="nsew")

        self.clear_button = ttk.Button(entry_row, text="Clear", command=self._clear_text_box)
        self.clear_button.grid(row=0, column=1, sticky="ew", padx=(8, 0), pady=(0, 3))

        paste_button = ttk.Button(entry_row, text="Paste", command=self._paste_clipboard)
        paste_button.grid(row=1, column=1, sticky="ew", padx=(8, 0), pady=(3, 0))

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

    def _try_load_clipboard(self):
        try:
            text = self.clipboard_get().strip()
        except tk.TclError:
            return
        if "http://" in text or "https://" in text:
            self.url_text.delete("1.0", tk.END)
            self.url_text.insert("1.0", text)

    def _paste_clipboard(self):
        self._try_load_clipboard()

    def _clear_text_box(self):
        self.url_text.delete("1.0", tk.END)

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
        validation_error = validate_urls(urls)
        if validation_error:
            self._show_dialog("Cannot Download", validation_error, "error")
            return

        DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
        self.progress_var.set(0)
        self.cancel_event.clear()
        self.download_total = len(urls)
        self.download_current = 0
        self.download_completed = 0
        self._set_phase("downloading")
        self._set_busy(True)
        item_text = "link" if len(urls) == 1 else "links"
        self._set_status(f"Starting download for {len(urls)} {item_text}...")

        self.worker = threading.Thread(target=self._download_worker, args=(urls, mode), daemon=True)
        self.worker.start()

    def _get_input_text(self):
        return self.url_text.get("1.0", tk.END).strip()

    def _download_worker(self, urls, mode):
        try:
            if mode == "video":
                output = download_best_video(urls, self._progress_hook, self._status, self.cancel_event)
            elif mode == "gif":
                output = download_best_gifs(urls, self._progress_hook, self._status, self.cancel_event)
            else:
                output = download_best_mp3s(urls, self._progress_hook, self._status, self.cancel_event)
            self.events.put(("done", f"Saved: {output}"))
        except DownloadCancelled:
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
            raise DownloadCancelled("Download cancelled.")

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
        url = match.rstrip(").,;]'\"")
        if url not in urls:
            urls.append(url)
    return urls


def validate_urls(urls):
    if not urls:
        return "Paste at least one valid web link."

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
    return None


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
        target = resolve_download_target(url, status_callback)
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

        with yt_dlp.YoutubeDL(options) as ydl:
            code = ydl.download([url])
        if code:
            raise RuntimeError("One or more downloads failed.")
        saved_locations.append(target["directory"])
    return format_saved_locations(saved_locations)


def download_best_gifs(urls, progress_hook, status_callback, cancel_event):
    converted = 0
    saved_locations = []
    for url in urls:
        check_cancelled(cancel_event)
        target = resolve_download_target(url, status_callback)
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

            with yt_dlp.YoutubeDL(options) as ydl:
                code = ydl.download([url])
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
        target = resolve_download_target(url, status_callback)
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

            with yt_dlp.YoutubeDL(options) as ydl:
                code = ydl.download([url])
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
    with yt_dlp.YoutubeDL(options) as ydl:
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
        raise DownloadCancelled("Download cancelled.")


def resolve_download_target(url, status_callback):
    status_callback("Scanning link details...")
    info = extract_download_info(url)
    collection_name = collection_title(info)
    if collection_name:
        directory = unique_path(DOWNLOAD_DIR / safe_folder_name(collection_name))
        directory.mkdir(parents=True, exist_ok=True)
        return {"directory": directory, "is_collection": True, "name": collection_name}
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    return {"directory": DOWNLOAD_DIR, "is_collection": False, "name": None}


def extract_download_info(url):
    options = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": "in_playlist",
        "windowsfilenames": True,
    }
    with yt_dlp.YoutubeDL(options) as ydl:
        return ydl.extract_info(url, download=False)


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
    cleaned = sanitize_filename(name, restricted=False).strip(" .")
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


def convert_source_to_gif(source, output_dir):
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
        for temp_file in (palette, source):
            try:
                temp_file.unlink(missing_ok=True)
            except OSError:
                pass
    return gif_path


def convert_source_to_mp3(source, output_dir):
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
        candidates.append(Path(imageio_ffmpeg.get_ffmpeg_exe()))
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


def run_ffmpeg(args):
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


if __name__ == "__main__":
    app = DownloadApp()
    app.mainloop()
