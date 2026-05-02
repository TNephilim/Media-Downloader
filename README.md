# Media Downloader

A small Windows media downloader GUI for saving videos, audio, GIFs, and playlists to the user's Downloads folder.

## Features

- Download video files.
- Download audio as MP3 files.
- Convert video links to GIF files.
- Batch download multiple links.
- Download playlists into named folders.
- Cancel active downloads.
- Blocks Spotify links because Spotify offline downloads must be handled inside Spotify's own app.

## Source

The Python source and PyInstaller spec are in `Source Files/`.

## Build

```powershell
pyinstaller --onefile --windowed --name MediaDownloader --specpath 'Source Files' --workpath 'Source Files\build' --distpath 'Source Files\dist' 'Source Files\src\media_downloader.py'
```

The built app is `MediaDownloader.exe`.
