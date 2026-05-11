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
- Checks GitHub Releases on startup and replaces itself when a newer release is available.

## Source

The Python source and PyInstaller spec are in `Source Code/`.

## Build

```powershell
pyinstaller --onefile --windowed --name MediaDownloader --specpath 'Source Code' --workpath 'Source Code\build' --distpath 'Source Code\dist' 'Source Code\src\media_downloader.py'
```

The built app is `MediaDownloader.exe`.
