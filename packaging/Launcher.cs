// SPDX-License-Identifier: AGPL-3.0-only
// Native portable launcher. Build with the Windows .NET Framework C# compiler.
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Net;
using System.Net.Sockets;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;
using System.Web.Script.Serialization;
using System.Windows.Forms;
using Microsoft.Win32.SafeHandles;

internal static class PortablePaths
{
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    static extern SafeFileHandle CreateFile(string name, uint access, uint share, IntPtr security, uint disposition, uint flags, IntPtr template);
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    static extern uint GetFinalPathNameByHandle(SafeFileHandle handle, StringBuilder path, uint length, uint flags);
    static string Canonical(string path)
    {
        string full = Path.GetFullPath(path);
        if (!Directory.Exists(full) && !File.Exists(full))
            return Path.Combine(Canonical(Path.GetDirectoryName(full)), Path.GetFileName(full));
        using (SafeFileHandle handle = CreateFile(full, 0, 7, IntPtr.Zero, 3, 0x02000000, IntPtr.Zero)) {
            if (handle.IsInvalid) throw new IOException("无法核对目录位置：" + full);
            StringBuilder value = new StringBuilder(32768);
            uint length = GetFinalPathNameByHandle(handle, value, (uint)value.Capacity, 0);
            if (length == 0 || length >= value.Capacity) throw new IOException("无法解析目录：" + full);
            string result = value.ToString();
            if (result.StartsWith(@"\\?\UNC\")) return @"\\" + result.Substring(8);
            return result.StartsWith(@"\\?\") ? result.Substring(4) : result;
        }
    }
    public static string Check(string path)
    {
        string result = Canonical(path);
        string limit = Environment.GetEnvironmentVariable("PORTRAITLAB_STORAGE_ROOT");
        if (!String.IsNullOrWhiteSpace(limit)) {
            string root = Canonical(limit).TrimEnd(Path.DirectorySeparatorChar);
            if (!result.Equals(root, StringComparison.OrdinalIgnoreCase) &&
                !result.StartsWith(root + Path.DirectorySeparatorChar, StringComparison.OrdinalIgnoreCase))
                throw new IOException("目录超出 PORTRAITLAB_STORAGE_ROOT 设置的范围：" + result);
        }
        return result;
    }
    public static string DirectoryPath(string path) { string result = Check(path); Directory.CreateDirectory(result); return result; }
}

internal sealed class ChildJob : IDisposable
{
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode)] static extern IntPtr CreateJobObject(IntPtr attrs, string name);
    [DllImport("kernel32.dll")] static extern bool SetInformationJobObject(IntPtr job, int info, IntPtr buffer, uint size);
    [DllImport("kernel32.dll")] static extern bool AssignProcessToJobObject(IntPtr job, IntPtr process);
    [DllImport("kernel32.dll")] static extern bool CloseHandle(IntPtr handle);
    [StructLayout(LayoutKind.Sequential)] struct BasicLimits { public long processTime, jobTime; public uint flags; public UIntPtr minWorkingSet, maxWorkingSet; public uint activeLimit; public UIntPtr affinity; public uint priority, scheduling; }
    [StructLayout(LayoutKind.Sequential)] struct IoCounters { public ulong readOps, writeOps, otherOps, readBytes, writeBytes, otherBytes; }
    [StructLayout(LayoutKind.Sequential)] struct ExtendedLimits { public BasicLimits basic; public IoCounters io; public UIntPtr processMemory, jobMemory, peakProcessMemory, peakJobMemory; }
    IntPtr handle;
    public ChildJob() {
        handle = CreateJobObject(IntPtr.Zero, null);
        ExtendedLimits limits = new ExtendedLimits(); limits.basic.flags = 0x2000;
        int size = Marshal.SizeOf(typeof(ExtendedLimits)); IntPtr buffer = Marshal.AllocHGlobal(size);
        try { Marshal.StructureToPtr(limits, buffer, false); if (handle == IntPtr.Zero || !SetInformationJobObject(handle, 9, buffer, (uint)size)) throw new IOException("无法建立应用子进程组。"); }
        finally { Marshal.FreeHGlobal(buffer); }
    }
    public bool Add(Process process) { return handle != IntPtr.Zero && AssignProcessToJobObject(handle, process.Handle); }
    public void Dispose() { if (handle != IntPtr.Zero) { CloseHandle(handle); handle = IntPtr.Zero; } }
}

internal sealed class Launcher : Form
{
    const string RuntimeHelpUrl = "https://learn.microsoft.com/en-us/cpp/windows/latest-supported-vc-redist?view=msvc-170";
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)] static extern IntPtr LoadLibrary(string path);
    [DllImport("kernel32.dll")] static extern bool FreeLibrary(IntPtr module);
    readonly string root, appDir, dataDir, cacheDir, logDir;
    readonly bool noBrowser, headless;
    readonly object logLock = new object();
    readonly JavaScriptSerializer json = new JavaScriptSerializer();
    readonly TextBox logBox = new TextBox();
    readonly Label status = new Label();
    readonly Button start = new Button(), open = new Button(), stop = new Button(), logs = new Button(), runtimeHelp = new Button();
    Process backend; ChildJob childJob; int port; bool ownBackend, stopping, closing, ready, missingRuntime;
    string token = "", url = "";

    public Launcher(string[] args)
    {
        root = Path.GetFullPath(AppDomain.CurrentDomain.BaseDirectory);
        appDir = Path.Combine(root, "app");
        string data = Environment.GetEnvironmentVariable("PORTRAITLAB_DATA") ?? Path.Combine(root, "Data");
        string cache = Environment.GetEnvironmentVariable("PORTRAITLAB_CACHE") ?? Path.Combine(root, "Cache");
        port = 18765;
        for (int i = 0; i < args.Length; i++) {
            if (args[i] == "--no-browser") noBrowser = true;
            else if (args[i] == "--headless") { headless = true; noBrowser = true; }
            else if (args[i] == "--data" && i + 1 < args.Length) data = args[++i];
            else if (args[i] == "--cache" && i + 1 < args.Length) cache = args[++i];
            else if (args[i] == "--port" && i + 1 < args.Length) port = Int32.Parse(args[++i]);
        }
        if (port < 1024 || port > 65515) throw new ArgumentException("端口须在1024至65515之间。");
        dataDir = PortablePaths.DirectoryPath(data); cacheDir = PortablePaths.DirectoryPath(cache);
        logDir = PortablePaths.DirectoryPath(Path.Combine(dataDir, "logs"));
        Text = "PortraitLab 0.2 · 本地人像工作台"; Width = 840; Height = 520; MinimumSize = new Size(680, 420);
        StartPosition = FormStartPosition.CenterScreen; Font = new Font("Microsoft YaHei UI", 10);
        BackColor = Color.FromArgb(24, 27, 31); ForeColor = Color.FromArgb(229, 232, 235);
        Label title = new Label(); title.Text = "PortraitLab"; title.Font = new Font("Microsoft YaHei UI", 25, FontStyle.Bold);
        title.SetBounds(26, 20, 380, 50); Controls.Add(title);
        status.Text = "正在准备本地工作台…"; status.SetBounds(30, 78, 760, 28); status.Anchor = AnchorStyles.Top | AnchorStyles.Left | AnchorStyles.Right; Controls.Add(status);
        Label directory = new Label(); directory.Text = "数据目录：" + dataDir; directory.AutoEllipsis = true; directory.SetBounds(30, 110, 760, 25); directory.Anchor = status.Anchor; Controls.Add(directory);
        Configure(start, "启动", 30); Configure(open, "打开工作台", 150); Configure(stop, "停止处理", 310); Configure(logs, "查看日志目录", 450);
        Configure(runtimeHelp, "安装VC++运行库", 610); runtimeHelp.Visible = false;
        runtimeHelp.Click += delegate { Process.Start(new ProcessStartInfo(RuntimeHelpUrl) { UseShellExecute = true }); };
        open.Enabled = false; stop.Enabled = false; start.Enabled = false;
        start.Click += delegate { BeginStart(); }; open.Click += delegate { OpenBrowser(); }; stop.Click += delegate { BeginStop(false); };
        logs.Click += delegate { Process.Start(new ProcessStartInfo(PortablePaths.Check(logDir)) { UseShellExecute = true }); };
        logBox.Multiline = true; logBox.ReadOnly = true; logBox.ScrollBars = ScrollBars.Both; logBox.WordWrap = false;
        logBox.BackColor = Color.FromArgb(14, 17, 20); logBox.ForeColor = Color.FromArgb(190, 199, 207); logBox.Font = new Font("Consolas", 9);
        logBox.SetBounds(30, 205, 760, 245); logBox.Anchor = AnchorStyles.Top | AnchorStyles.Bottom | AnchorStyles.Left | AnchorStyles.Right; Controls.Add(logBox);
        Shown += delegate { if (headless) Hide(); BeginStart(); };
        FormClosing += HandleClosing;
        if (headless) { ShowInTaskbar = false; WindowState = FormWindowState.Minimized; Opacity = 0; }
    }
    void Configure(Button button, string label, int x) {
        button.Text = label; button.SetBounds(x, 151, label.Length > 3 ? 140 : 100, 36); button.FlatStyle = FlatStyle.Flat;
        button.BackColor = Color.FromArgb(42, 48, 54); button.ForeColor = ForeColor; Controls.Add(button);
    }
    void UI(Action action) { if (!IsDisposed && IsHandleCreated) try { BeginInvoke(action); } catch (InvalidOperationException) { } }
    void Log(string message) {
        if (String.IsNullOrEmpty(message)) return;
        string line = DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss") + " " + message;
        lock (logLock) File.AppendAllText(PortablePaths.Check(Path.Combine(logDir, "launcher.log")), line + Environment.NewLine, new UTF8Encoding(false));
        UI(delegate { if (logBox.TextLength > 35000) logBox.Text = logBox.Text.Substring(logBox.TextLength - 20000); logBox.AppendText(line + Environment.NewLine); });
    }
    Dictionary<string, object> Probe(int candidate) {
        try {
            HttpWebRequest request = (HttpWebRequest)WebRequest.Create("http://127.0.0.1:" + candidate + "/api/bootstrap");
            request.Proxy = null; request.Timeout = 1200; request.ReadWriteTimeout = 1200;
            using (WebResponse response = request.GetResponse()) using (StreamReader reader = new StreamReader(response.GetResponseStream()))
                return json.Deserialize<Dictionary<string, object>>(reader.ReadToEnd());
        } catch { return null; }
    }
    bool SameWorkspace(Dictionary<string, object> result) {
        try { var storage = (Dictionary<string, object>)result["storage"]; return Path.GetFullPath((string)storage["data_dir"]).TrimEnd('\\').Equals(dataDir.TrimEnd('\\'), StringComparison.OrdinalIgnoreCase); }
        catch { return false; }
    }
    bool PortFree(int candidate) { TcpListener listener = new TcpListener(IPAddress.Loopback, candidate); try { listener.Start(); return true; } catch { return false; } finally { listener.Stop(); } }
    void ChildEnvironment(ProcessStartInfo info) {
        info.EnvironmentVariables.Remove("PYTHONHOME"); info.EnvironmentVariables.Remove("PYTHONPATH");
        string temp = PortablePaths.DirectoryPath(Path.Combine(cacheDir, "temp"));
        foreach (string key in new [] { "TEMP", "TMP" }) info.EnvironmentVariables[key] = temp;
        info.EnvironmentVariables["PYTHONDONTWRITEBYTECODE"] = "1"; info.EnvironmentVariables["PYTHONNOUSERSITE"] = "1";
        info.EnvironmentVariables["PYTHONUTF8"] = "1"; info.EnvironmentVariables["PORTRAITLAB_DATA"] = dataDir;
        info.EnvironmentVariables["PORTRAITLAB_CACHE"] = cacheDir;
        info.EnvironmentVariables["PORTRAITLAB_WORK"] = cacheDir; info.EnvironmentVariables["PORTRAITLAB_PORT"] = port.ToString();
        if (String.IsNullOrWhiteSpace(info.EnvironmentVariables["PORTRAITLAB_AI_PYTHON"]))
            info.EnvironmentVariables["PORTRAITLAB_AI_PYTHON"] = Path.Combine(root, "AI", "python.exe");
        if (String.IsNullOrWhiteSpace(info.EnvironmentVariables["PORTRAITLAB_RELIGHT_MODELS"]))
            info.EnvironmentVariables["PORTRAITLAB_RELIGHT_MODELS"] = Path.Combine(dataDir, "models", "ic-light");
        var dirs = new Dictionary<string, string> { { "PIP_CACHE_DIR", "pip" }, { "MPLCONFIGDIR", "matplotlib" }, { "HF_HOME", "huggingface" }, { "TORCH_HOME", "torch" }, { "CUDA_CACHE_PATH", "cuda" }, { "TORCH_EXTENSIONS_DIR", "torch-extensions" }, { "TORCHINDUCTOR_CACHE_DIR", "torch-inductor" }, { "TRITON_CACHE_DIR", "triton" }, { "XDG_CACHE_HOME", "xdg" }, { "UV_CACHE_DIR", "uv" } };
        foreach (var pair in dirs) info.EnvironmentVariables[pair.Key] = PortablePaths.DirectoryPath(Path.Combine(cacheDir, pair.Value));
        info.EnvironmentVariables["HF_HUB_DISABLE_TELEMETRY"] = "1"; info.EnvironmentVariables["DO_NOT_TRACK"] = "1";
        info.EnvironmentVariables["FASTAPI_OTEL_AUTO_CONFIGURE"] = "false";
    }
    void BeginStart() {
        start.Enabled = false; stop.Enabled = false; status.Text = "正在启动…";
        ThreadPool.QueueUserWorkItem(delegate { try { StartBackend(); } catch (Exception exc) { Log(exc.ToString()); Environment.ExitCode = 1; UI(delegate { status.Text = missingRuntime ? "缺少 Microsoft Visual C++ x64 运行库，请点击安装按钮打开微软官方页面。" : "启动失败，请查看下方日志。"; runtimeHelp.Visible = missingRuntime; bool alive = ownBackend && backend != null && !backend.HasExited; start.Enabled = !alive; stop.Enabled = alive; if (headless) BeginStop(true); }); } });
    }
    void StartBackend() {
        // Test the system dependency by loading its x64 DLL, never by copying it.
        IntPtr runtime = LoadLibrary(Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.System), "msvcp140.dll"));
        missingRuntime = runtime == IntPtr.Zero;
        if (runtime != IntPtr.Zero) FreeLibrary(runtime);
        if (missingRuntime) throw new IOException("需要 Microsoft Visual C++ 2015–2022 x64 Redistributable。请从微软官方页面安装 x64 版本后重新启动：" + RuntimeHelpUrl);
        UI(delegate { runtimeHelp.Visible = false; });
        string python = Path.Combine(root, "runtime", "python.exe");
        if (!File.Exists(python) || !Directory.Exists(appDir)) throw new FileNotFoundException("便携运行时不完整，请完整解压软件包后再启动。");
        Dictionary<string, object> existing = null; bool found = false;
        for (int candidate = port; candidate < port + 20; candidate++) {
            var result = Probe(candidate);
            if (SameWorkspace(result)) { port = candidate; existing = result; found = true; break; }
            if (PortFree(candidate)) { port = candidate; found = true; break; }
        }
        if (!found) throw new IOException("无法找到可用本地端口，请使用 --port 指定其他端口。");
        url = "http://127.0.0.1:" + port;
        if (existing != null) { ownBackend = false; MarkReady(existing); Log("已连接现有工作台；本窗口不会停止其他窗口的服务。"); return; }
        if (childJob != null) childJob.Dispose(); childJob = new ChildJob();
        var info = new ProcessStartInfo(python, "-I -B -X utf8 -m portraitlab.server --port " + port + " --data " + Quote(dataDir));
        info.WorkingDirectory = appDir; info.UseShellExecute = false; info.CreateNoWindow = true;
        info.RedirectStandardOutput = true; info.RedirectStandardError = true; info.StandardOutputEncoding = Encoding.UTF8; info.StandardErrorEncoding = Encoding.UTF8;
        ChildEnvironment(info); backend = new Process(); backend.StartInfo = info; backend.EnableRaisingEvents = true;
        backend.OutputDataReceived += delegate(object sender, DataReceivedEventArgs e) { Log(e.Data); };
        backend.ErrorDataReceived += delegate(object sender, DataReceivedEventArgs e) { Log(e.Data); };
        backend.Exited += delegate { Log("处理服务已退出。"); ready = false; UI(delegate { open.Enabled = false; stop.Enabled = false; start.Enabled = true; status.Text = "处理已停止，可重新启动。"; if (headless && !stopping) { closing = true; Close(); } }); };
        backend.Start(); ownBackend = true;
        if (!childJob.Add(backend)) Log("Windows未启用子进程组；停止时将使用拥有的进程ID进行兜底。");
        backend.BeginOutputReadLine(); backend.BeginErrorReadLine();
        Log("正在启动本地处理进程 PID " + backend.Id + "；数据目录 " + dataDir);
        for (int count = 0; count < 100; count++) {
            if (backend.HasExited) throw new IOException("处理进程提前退出，请查看日志中的错误。退出码：" + backend.ExitCode);
            var result = Probe(port); if (SameWorkspace(result)) { MarkReady(result); return; }
            Thread.Sleep(500);
        }
        throw new TimeoutException("启动超时；请查看日志，或停止后重新启动。");
    }
    void MarkReady(Dictionary<string, object> result) {
        token = result.ContainsKey("token") ? (string)result["token"] : ""; ready = true;
        var record = new Dictionary<string, object> { { "ready", true }, { "url", url }, { "port", port }, { "launcher_pid", Process.GetCurrentProcess().Id }, { "server_pid", ownBackend ? (object)backend.Id : null }, { "data_dir", dataDir }, { "runtime", Path.Combine(root, "runtime", "python.exe") } };
        File.WriteAllText(PortablePaths.Check(Path.Combine(logDir, "launcher-status.json")), json.Serialize(record), new UTF8Encoding(false));
        UI(delegate { status.Text = "已就绪 · " + url; open.Enabled = true; stop.Enabled = ownBackend; start.Enabled = false; if (!noBrowser) OpenBrowser(); });
        Log("工作台已就绪：" + url);
    }
    static string Quote(string value) {
        StringBuilder result = new StringBuilder("\""); int slashes = 0;
        foreach (char ch in value) {
            if (ch == '\\') { slashes++; continue; }
            if (ch == '"') { result.Append('\\', slashes * 2 + 1); result.Append(ch); slashes = 0; continue; }
            result.Append('\\', slashes); slashes = 0; result.Append(ch);
        }
        result.Append('\\', slashes * 2); result.Append('"'); return result.ToString();
    }
    void OpenBrowser() {
        if (!ready) return;
        string browser = null;
        foreach (string variable in new [] { "ProgramFiles(x86)", "ProgramFiles", "LOCALAPPDATA" }) {
            string parent = Environment.GetEnvironmentVariable(variable); if (String.IsNullOrEmpty(parent)) continue;
            foreach (string relative in new [] { @"Microsoft\Edge\Application\msedge.exe", @"Google\Chrome\Application\chrome.exe" }) {
                string candidate = Path.Combine(parent, relative); if (File.Exists(candidate)) { browser = candidate; break; }
            }
            if (browser != null) break;
        }
        if (browser == null) { MessageBox.Show("未找到Edge或Chrome。请手动在浏览器打开：\n" + url, "PortraitLab"); return; }
        string profile = PortablePaths.DirectoryPath(Path.Combine(cacheDir, "browser-profile"));
        string disk = PortablePaths.DirectoryPath(Path.Combine(cacheDir, "browser-cache"));
        string crashes = PortablePaths.DirectoryPath(Path.Combine(cacheDir, "browser-crashes"));
        var info = new ProcessStartInfo(browser, "--app=" + Quote(url) + " --user-data-dir=" + Quote(profile) + " --disk-cache-dir=" + Quote(disk) + " --crash-dumps-dir=" + Quote(crashes) + " --no-first-run --no-default-browser-check --window-size=1440,960");
        info.UseShellExecute = false; info.CreateNoWindow = true; info.WorkingDirectory = root; ChildEnvironment(info);
        Process process = Process.Start(info); if (childJob != null && process != null) childJob.Add(process);
    }
    void BeginStop(bool exitAfter) {
        if (stopping) return; stopping = true; start.Enabled = false; stop.Enabled = false; status.Text = "正在停止当前处理…";
        ThreadPool.QueueUserWorkItem(delegate { try { StopOwnedBackend(); } catch (Exception exc) { Log(exc.ToString()); } finally { stopping = false; UI(delegate { open.Enabled = false; start.Enabled = true; status.Text = "处理已停止。"; if (exitAfter) { closing = true; Close(); } }); } });
    }
    void StopOwnedBackend() {
        if (!ownBackend || backend == null || backend.HasExited) return;
        try {
            var request = (HttpWebRequest)WebRequest.Create(url + "/api/shutdown"); request.Proxy = null; request.Method = "POST";
            request.Headers["X-PortraitLab-Token"] = token; request.ContentLength = 0; request.Timeout = 3000;
            using (request.GetResponse()) { }
        } catch (Exception exc) { Log("正常停止请求未完成：" + exc.Message); }
        if (!backend.WaitForExit(15000)) {
            Log("正在停止本启动器拥有的进程及其子进程。");
            var kill = new ProcessStartInfo("taskkill.exe", "/PID " + backend.Id + " /T /F") { UseShellExecute = false, CreateNoWindow = true };
            using (Process process = Process.Start(kill)) { if (process != null) process.WaitForExit(5000); }
        }
        if (childJob != null) { childJob.Dispose(); childJob = null; }
        ownBackend = false; ready = false;
    }
    void HandleClosing(object sender, FormClosingEventArgs e) {
        if (closing) return;
        if (ownBackend && backend != null && !backend.HasExited) {
            if (!headless && MessageBox.Show("关闭应用会停止当前处理。确定退出吗？", "PortraitLab", MessageBoxButtons.YesNo, MessageBoxIcon.Question) != DialogResult.Yes) { e.Cancel = true; return; }
            e.Cancel = true; BeginStop(true); return;
        }
        if (childJob != null) { childJob.Dispose(); childJob = null; }
    }
    [STAThread]
    static void Main(string[] args) {
        Application.EnableVisualStyles(); Application.SetCompatibleTextRenderingDefault(false);
        try { Application.Run(new Launcher(args)); }
        catch (Exception exc) { MessageBox.Show(exc.Message + "\n\n可通过 --data 和 --cache 指定可写目录。", "PortraitLab 无法启动", MessageBoxButtons.OK, MessageBoxIcon.Error); Environment.ExitCode = 1; }
    }
}
