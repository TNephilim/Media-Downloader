using System.Diagnostics;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Runtime.InteropServices.ComTypes;
using System.Text;
using System.Text.Json;
using Photino.NET;
using System.Windows.Forms;

internal static class Program
{
    private const int WindowWidth = 700;
    private const int WindowHeight = 357;
    private const int DetailsWindowHeight = 512;
    private static readonly JsonSerializerOptions JsonOptions = new() { PropertyNamingPolicy = JsonNamingPolicy.CamelCase };
    private static readonly Dictionary<string, FileImport> activeImports = [];
    private static PhotinoWindow? window;
    private static BackendBridge? backend;

    [STAThread]
    private static void Main()
    {
        window = new PhotinoWindow()
            .SetTitle("Media Downloader")
            .SetTemporaryFilesPath(GetPhotinoDataPath())
            .SetUseOsDefaultSize(false)
            .SetSize(WindowWidth, WindowHeight)
            .SetMinSize(WindowWidth, WindowHeight)
            .SetMaxSize(WindowWidth, DetailsWindowHeight)
            .SetResizable(false)
            .SetDevToolsEnabled(false)
            .SetContextMenuEnabled(false)
            .SetFileSystemAccessEnabled(true);

        window.Centered = true;
        window.WebMessageReceived += (_, message) => HandlePageMessage(message);
        window.StartUrl = ExtractUserInterface();
        window.WaitForClose();
        backend?.Dispose();
    }

    private static string GetPhotinoDataPath()
    {
        var path = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "MediaDownloader", "Photino");
        Directory.CreateDirectory(path);
        return path;
    }

    private static void HandlePageMessage(string message)
    {
        try
        {
            using var document = JsonDocument.Parse(message);
            var root = document.RootElement;
            var action = root.GetProperty("action").GetString();

            switch (action)
            {
                case "start":
                    EnsureBackend();
                    backend?.Send(new
                    {
                        action,
                        mode = root.GetProperty("mode").GetString(),
                        input = root.GetProperty("input").GetString(),
                        outputName = root.TryGetProperty("outputName", out var outputName) ? outputName.GetString() : string.Empty,
                    });
                    break;
                case "inspect":
                    EnsureBackend();
                    backend?.Send(new
                    {
                        action,
                        requestId = root.GetProperty("requestId").GetInt32(),
                        input = root.GetProperty("input").GetString(),
                    });
                    break;
                case "cancel":
                    EnsureBackend();
                    backend?.Send(new { action });
                    break;
                case "poll":
                    backend?.FlushLatestState();
                    break;
                case "detailsMenu":
                    var detailsOpen = root.TryGetProperty("open", out var open) && open.GetBoolean();
                    window?.SetSize(WindowWidth, detailsOpen ? DetailsWindowHeight : WindowHeight);
                    break;
                case "choose":
                    ChooseFiles();
                    break;
                case "openDownloads":
                    OpenDownloadsFolder();
                    break;
                case "files":
                    if (root.TryGetProperty("files", out var files))
                    {
                        SendToPage(new { type = "files", files = files.EnumerateArray().Select(file => file.GetString()).Where(path => !string.IsNullOrWhiteSpace(path)) });
                    }
                    break;
                case "importStart":
                    StartFileImport(root);
                    break;
                case "importChunk":
                    AppendFileImportChunk(root);
                    break;
                case "importComplete":
                    CompleteFileImport(root);
                    break;
                case "importCancel":
                    CancelFileImport(root);
                    break;
            }
        }
        catch (Exception exception)
        {
            SendToPage(new { type = "error", message = exception.Message });
        }
    }

    private static void EnsureBackend()
    {
        if (backend is not null)
        {
            return;
        }

        backend = new BackendBridge(SendToPage);
        backend.Start();
    }

    private static void ChooseFiles()
    {
        using var dialog = new OpenFileDialog
        {
            Title = "Choose media files",
            Filter = "Media files|*.mp4;*.webm;*.mkv;*.mov;*.gif;*.mp3;*.m4a;*.aac;*.flac;*.ogg;*.opus;*.wav|All files|*.*",
            Multiselect = true,
        };

        if (dialog.ShowDialog() == DialogResult.OK)
        {
            SendToPage(new { type = "files", files = dialog.FileNames });
        }
    }

    private static void OpenDownloadsFolder()
    {
        var downloads = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.UserProfile), "Downloads");
        Directory.CreateDirectory(downloads);
        Process.Start(new ProcessStartInfo { FileName = downloads, UseShellExecute = true });
    }

    private static void StartFileImport(JsonElement root)
    {
        var id = root.GetProperty("id").GetString() ?? throw new InvalidOperationException("Missing file import id.");
        var fileName = Path.GetFileName(root.GetProperty("name").GetString() ?? "dropped-media");
        if (string.IsNullOrWhiteSpace(fileName))
        {
            fileName = "dropped-media";
        }
        CancelFileImport(id);
        var directory = Path.Combine(Path.GetTempPath(), "MediaDownloader", "Dropped Files");
        Directory.CreateDirectory(directory);
        var path = Path.Combine(directory, $"{id}-{fileName}");
        activeImports[id] = new FileImport(path);
    }

    private static void AppendFileImportChunk(JsonElement root)
    {
        var id = root.GetProperty("id").GetString() ?? string.Empty;
        if (!activeImports.TryGetValue(id, out var import))
        {
            throw new InvalidOperationException("The dropped file import is no longer available.");
        }
        var data = root.GetProperty("data").GetString() ?? string.Empty;
        import.Stream.Write(Convert.FromBase64String(data));
    }

    private static void CompleteFileImport(JsonElement root)
    {
        var id = root.GetProperty("id").GetString() ?? string.Empty;
        if (!activeImports.Remove(id, out var import))
        {
            throw new InvalidOperationException("The dropped file import is no longer available.");
        }
        import.Dispose();
        // This handler is called from the browser message callback. Dispatch
        // the response separately so Photino does not wait on its own UI thread.
        _ = Task.Run(() => SendToPage(new { type = "files", files = new[] { import.Path } }));
    }

    private static void CancelFileImport(JsonElement root)
    {
        CancelFileImport(root.GetProperty("id").GetString() ?? string.Empty);
    }

    private static void CancelFileImport(string id)
    {
        if (!activeImports.Remove(id, out var import))
        {
            return;
        }
        import.Dispose();
        try { File.Delete(import.Path); } catch (IOException) { }
    }

    private static void SendToPage(object message)
    {
        var payload = JsonSerializer.Serialize(message, JsonOptions);
        window?.Invoke(() => window.SendWebMessage(payload));
    }

    private static string ExtractUserInterface()
    {
        const string resourceName = "MediaDownloaderUi.index.html";
        using var stream = Assembly.GetExecutingAssembly().GetManifestResourceStream(resourceName)
            ?? throw new InvalidOperationException("The bundled interface could not be loaded.");
        using var memory = new MemoryStream();
        stream.CopyTo(memory);
        var bytes = memory.ToArray();
        var hash = Convert.ToHexString(System.Security.Cryptography.SHA256.HashData(bytes)).Substring(0, 12).ToLowerInvariant();
        var directory = Path.Combine(Path.GetTempPath(), "MediaDownloader", "ui", hash);
        var path = Path.Combine(directory, "index.html");
        Directory.CreateDirectory(directory);
        if (!File.Exists(path) || new FileInfo(path).Length != bytes.Length)
        {
            File.WriteAllBytes(path, bytes);
        }
        return path;
    }
}

internal sealed class FileImport : IDisposable
{
    public FileImport(string path)
    {
        Path = path;
        Stream = new FileStream(path, FileMode.Create, FileAccess.Write, FileShare.None);
    }

    public string Path { get; }
    public FileStream Stream { get; }

    public void Dispose() => Stream.Dispose();
}

internal sealed class FileDropForwarder : IDisposable
{
    private readonly Action<string[]> sendFiles;
    private readonly HashSet<IntPtr> attachedHandles = [];
    private readonly List<RegisteredDropTarget> targets = [];
    private readonly IntPtr parentHandle;
    private readonly bool oleInitialized;

    public FileDropForwarder(IntPtr handle, Action<string[]> sendFiles)
    {
        parentHandle = handle;
        this.sendFiles = sendFiles;
        var oleResult = OleInitialize(IntPtr.Zero);
        oleInitialized = oleResult >= 0;
    }

    public bool HasBrowserTarget { get; private set; }

    public void AttachBrowserChildren()
    {
        var browserWindows = new List<IntPtr>();
        EnumChildWindows(parentHandle, (child, _) =>
        {
            var className = new StringBuilder(256);
            GetClassName(child, className, className.Capacity);
            if (className.ToString().Contains("Chrome_RenderWidgetHostHWND", StringComparison.OrdinalIgnoreCase))
            {
                browserWindows.Add(child);
            }
            return true;
        }, IntPtr.Zero);
        foreach (var browserWindow in browserWindows)
        {
            Attach(browserWindow);
        }
        HasBrowserTarget = browserWindows.Count > 0;
    }

    private void Attach(IntPtr handle)
    {
        if (handle == IntPtr.Zero || !attachedHandles.Add(handle))
        {
            return;
        }

        // WebView2 already owns an OLE drop target. Replace it with one that
        // forwards real Explorer paths into the app bridge.
        RevokeDragDrop(handle);
        var target = new ExplorerFileDropTarget(sendFiles);
        var registerResult = RegisterDragDrop(handle, target);
        if (registerResult >= 0)
        {
            targets.Add(new RegisteredDropTarget(handle, target));
        }
        else
        {
            attachedHandles.Remove(handle);
        }
    }

    public void Dispose()
    {
        foreach (var target in targets)
        {
            RevokeDragDrop(target.Handle);
        }
        targets.Clear();
        attachedHandles.Clear();
        if (oleInitialized)
        {
            OleUninitialize();
        }
    }

    private sealed class RegisteredDropTarget
    {
        public RegisteredDropTarget(IntPtr handle, ExplorerFileDropTarget target)
        {
            Handle = handle;
            Target = target;
        }

        public IntPtr Handle { get; }
        public ExplorerFileDropTarget Target { get; }
    }

    [ComVisible(true)]
    private sealed class ExplorerFileDropTarget : IOleDropTarget
    {
        private const uint DropEffectNone = 0;
        private const uint DropEffectCopy = 1;
        private readonly Action<string[]> sendFiles;

        public ExplorerFileDropTarget(Action<string[]> sendFiles)
        {
            this.sendFiles = sendFiles;
        }

        public int DragEnter(System.Runtime.InteropServices.ComTypes.IDataObject dataObject, uint keyState, PointL point, ref uint effect)
        {
            // Explorer supplies the final file list on Drop. Some WebView2
            // drag proxies do not answer QueryGetData during hover, so do not
            // reject a valid file before it is released.
            effect = DropEffectCopy;
            return 0;
        }

        public int DragOver(uint keyState, PointL point, ref uint effect)
        {
            effect = DropEffectCopy;
            return 0;
        }

        public int DragLeave() => 0;

        public int Drop(System.Runtime.InteropServices.ComTypes.IDataObject dataObject, uint keyState, PointL point, ref uint effect)
        {
            var files = GetFiles(dataObject);
            effect = files.Length > 0 ? DropEffectCopy : DropEffectNone;
            if (files.Length > 0)
            {
                sendFiles(files);
            }
            return 0;
        }

        private static bool HasFiles(System.Runtime.InteropServices.ComTypes.IDataObject dataObject)
        {
            var format = FileDropFormat();
            return dataObject.QueryGetData(ref format) == 0;
        }

        private static string[] GetFiles(System.Runtime.InteropServices.ComTypes.IDataObject dataObject)
        {
            var format = FileDropFormat();
            STGMEDIUM medium;
            try
            {
                dataObject.GetData(ref format, out medium);
            }
            catch (COMException)
            {
                return [];
            }
            try
            {
                var count = DragQueryFile(medium.unionmember, 0xFFFFFFFF, null, 0);
                var files = new List<string>((int)count);
                for (uint index = 0; index < count; index++)
                {
                    var length = DragQueryFile(medium.unionmember, index, null, 0);
                    if (length == 0)
                    {
                        continue;
                    }
                    var path = new StringBuilder((int)length + 1);
                    DragQueryFile(medium.unionmember, index, path, (uint)path.Capacity);
                    files.Add(path.ToString());
                }
                return files.ToArray();
            }
            finally
            {
                ReleaseStgMedium(ref medium);
            }
        }

        private static FORMATETC FileDropFormat() => new()
        {
            cfFormat = 15,
            dwAspect = DVASPECT.DVASPECT_CONTENT,
            lindex = -1,
            tymed = TYMED.TYMED_HGLOBAL,
        };
    }

    private delegate bool EnumWindowsCallback(IntPtr windowHandle, IntPtr parameter);

    [DllImport("user32.dll")]
    private static extern bool EnumChildWindows(IntPtr parentHandle, EnumWindowsCallback callback, IntPtr parameter);

    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    private static extern int GetClassName(IntPtr windowHandle, StringBuilder className, int maximumCount);

    [DllImport("shell32.dll", CharSet = CharSet.Unicode)]
    private static extern uint DragQueryFile(IntPtr dropHandle, uint index, StringBuilder? path, uint pathLength);

    [DllImport("ole32.dll")]
    private static extern int RegisterDragDrop(IntPtr windowHandle, [MarshalAs(UnmanagedType.Interface)] IOleDropTarget target);

    [DllImport("ole32.dll")]
    private static extern int RevokeDragDrop(IntPtr windowHandle);

    [DllImport("ole32.dll")]
    private static extern void ReleaseStgMedium(ref STGMEDIUM medium);

    [DllImport("ole32.dll")]
    private static extern int OleInitialize(IntPtr reserved);

    [DllImport("ole32.dll")]
    private static extern void OleUninitialize();
}

[ComVisible(true)]
[Guid("00000122-0000-0000-C000-000000000046")]
[InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
internal interface IOleDropTarget
{
    int DragEnter([MarshalAs(UnmanagedType.Interface)] System.Runtime.InteropServices.ComTypes.IDataObject dataObject, uint keyState, PointL point, ref uint effect);
    int DragOver(uint keyState, PointL point, ref uint effect);
    int DragLeave();
    int Drop([MarshalAs(UnmanagedType.Interface)] System.Runtime.InteropServices.ComTypes.IDataObject dataObject, uint keyState, PointL point, ref uint effect);
}

[StructLayout(LayoutKind.Sequential)]
internal struct PointL
{
    public int X;
    public int Y;
}

internal sealed class BackendBridge : IDisposable
{
    private readonly Action<object> sendToPage;
    private readonly object inputLock = new();
    private readonly object stateLock = new();
    private readonly string statePath = Path.Combine(Path.GetTempPath(), "MediaDownloader", $"state-{Guid.NewGuid():N}.json");
    private Process? process;
    private StreamWriter? input;
    private string? latestStateMessage;

    public BackendBridge(Action<object> sendToPage)
    {
        this.sendToPage = sendToPage;
    }

    public void Start()
    {
        var backendPath = ExtractBackend();
        process = new Process
        {
            StartInfo = new ProcessStartInfo
            {
                FileName = backendPath,
                Arguments = "--bridge",
                UseShellExecute = false,
                RedirectStandardInput = true,
                RedirectStandardOutput = true,
                RedirectStandardError = true,
                CreateNoWindow = true,
                StandardOutputEncoding = Encoding.UTF8,
            },
            EnableRaisingEvents = true,
        };
        Directory.CreateDirectory(Path.GetDirectoryName(statePath)!);
        process.StartInfo.Environment["MEDIA_DOWNLOADER_STATE_PATH"] = statePath;
        process.OutputDataReceived += (_, args) => ForwardBackendMessage(args.Data);
        process.ErrorDataReceived += (_, _) => { };
        process.Start();
        input = process.StandardInput;
        process.BeginOutputReadLine();
        process.BeginErrorReadLine();
    }

    public void Send(object message)
    {
        lock (inputLock)
        {
            input?.WriteLine(JsonSerializer.Serialize(message));
            input?.Flush();
        }
    }

    private void ForwardBackendMessage(string? line)
    {
        if (string.IsNullOrWhiteSpace(line))
        {
            return;
        }
        try
        {
            using var document = JsonDocument.Parse(line);
            if (document.RootElement.TryGetProperty("type", out var type) && type.GetString() == "ready")
            {
                return;
            }
            if (document.RootElement.TryGetProperty("type", out type) && type.GetString() == "state")
            {
                lock (stateLock)
                {
                    latestStateMessage = line;
                }
                return;
            }
            sendToPage(document.RootElement.Clone());
        }
        catch (JsonException)
        {
            // The bundled engine's normal output is intentionally ignored; only JSON bridge events reach the page.
        }
    }

    public void FlushLatestState()
    {
        // Standard output is the live bridge. Prefer it over the optional
        // state file, which can briefly retain the previous progress event on Windows.
        string? message;
        lock (stateLock)
        {
            message = latestStateMessage;
        }
        if (string.IsNullOrWhiteSpace(message))
        {
            try
            {
                message = File.Exists(statePath) ? File.ReadAllText(statePath, Encoding.UTF8) : null;
            }
            catch (IOException) { }
        }
        if (string.IsNullOrWhiteSpace(message))
        {
            return;
        }
        try
        {
            using var document = JsonDocument.Parse(message);
            sendToPage(document.RootElement.Clone());
        }
        catch (JsonException)
        {
            // Ignore an incomplete state message and wait for the next backend update.
        }
    }

    private static string ExtractBackend()
    {
        const string resourceName = "MediaDownloaderBackend.exe";
        var bytes = ReadResource(resourceName);
        var hash = Convert.ToHexString(System.Security.Cryptography.SHA256.HashData(bytes)).Substring(0, 12).ToLowerInvariant();
        var directory = Path.Combine(Path.GetTempPath(), "MediaDownloader", hash);
        var path = Path.Combine(directory, "MediaDownloaderBackend.exe");
        Directory.CreateDirectory(directory);
        if (!File.Exists(path) || new FileInfo(path).Length != bytes.Length)
        {
            File.WriteAllBytes(path, bytes);
        }
        return path;
    }

    private static byte[] ReadResource(string resourceName)
    {
        using var stream = Assembly.GetExecutingAssembly().GetManifestResourceStream(resourceName)
            ?? throw new InvalidOperationException("The bundled download engine could not be loaded.");
        using var memory = new MemoryStream();
        stream.CopyTo(memory);
        return memory.ToArray();
    }

    public void Dispose()
    {
        try { Send(new { action = "shutdown" }); } catch { }
        if (process is { HasExited: false })
        {
            process.WaitForExit(5000);
            if (!process.HasExited)
            {
                process.Kill(true);
                process.WaitForExit(2000);
            }
        }
        process?.Dispose();
    }
}
