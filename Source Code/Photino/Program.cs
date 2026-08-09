using System.Diagnostics;
using System.Reflection;
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
            .SetContextMenuEnabled(false);

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
        string? message;
        try
        {
            message = File.Exists(statePath) ? File.ReadAllText(statePath, Encoding.UTF8) : null;
        }
        catch (IOException)
        {
            message = null;
        }
        if (string.IsNullOrWhiteSpace(message))
        {
            lock (stateLock)
            {
                message = latestStateMessage;
            }
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
