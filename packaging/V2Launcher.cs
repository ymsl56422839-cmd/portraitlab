// SPDX-License-Identifier: AGPL-3.0-only
// Native portable V2 launcher; all mutable data is package-relative by default. Build with the Windows .NET Framework C# compiler.
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Net;
using System.Net.Sockets;
using System.Runtime.InteropServices;
using System.Text;
using System.Security.Cryptography;
using System.Security.AccessControl;
using System.Security.Principal;
using System.Threading;
using System.Threading.Tasks;
using Microsoft.Web.WebView2.Core;
using Microsoft.Web.WebView2.WinForms;
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

internal static class DesktopSecurity
{
    internal static bool LocalOrigin(string address, int port) {
        Uri uri;
        return Uri.TryCreate(address, UriKind.Absolute, out uri) && uri.Scheme == "http" &&
            uri.Host.Equals("127.0.0.1", StringComparison.OrdinalIgnoreCase) && uri.Port == port && String.IsNullOrEmpty(uri.UserInfo);
    }
    internal static bool ExternalHttps(string address) {
        Uri uri;
        return Uri.TryCreate(address, UriKind.Absolute, out uri) && uri.Scheme == "https" &&
            !uri.IsLoopback && String.IsNullOrEmpty(uri.UserInfo);
    }
}

internal sealed class Launcher : Form
{
    const string WebViewHelpUrl = "https://developer.microsoft.com/en-us/microsoft-edge/webview2/?form=MA13LH#download";
    [DllImport("user32.dll")] static extern bool SetForegroundWindow(IntPtr window);
    [DllImport("user32.dll")] static extern bool ShowWindow(IntPtr window, int command);
    readonly string root, appDir, dataDir, cacheDir, logDir;
    readonly bool headless, hiddenSmoke;
    readonly object logLock = new object();
    readonly Panel statusPanel = new Panel();
    readonly Panel contentPanel = new Panel();
    readonly MenuStrip menu = new MenuStrip();
    readonly Label status = new Label(), detail = new Label();
    readonly Button retry = new Button(), help = new Button();
    readonly ManualResetEventSlim cancelStart = new ManualResetEventSlim(false);
    WebView2 webView;
    CoreWebView2Environment webEnvironment;
    Process backend;
    ChildJob childJob;
    Task startTask;
    TaskCompletionSource<string> quitResult;
    int port;
    volatile bool ownBackend, stopping, ready, starting;
    bool closing, closeRequested, initializedBrowser, smokeStarted;
    string token = "", url = "", helpUrl = "", browserVersion = "", quitNonce = "";

    static Dictionary<string, object> Decode(string value) {
        return new JavaScriptSerializer { MaxJsonLength = 4 * 1024 * 1024 }.Deserialize<Dictionary<string, object>>(value);
    }
    static string Encode(object value) { return new JavaScriptSerializer().Serialize(value); }

    public Launcher(string[] args)
    {
        root = Path.GetFullPath(AppDomain.CurrentDomain.BaseDirectory);
        appDir = Path.Combine(root, "app");
        string data = Environment.GetEnvironmentVariable("PORTRAITLAB_DATA") ?? Path.Combine(root, "Data");
        string cache = Environment.GetEnvironmentVariable("PORTRAITLAB_CACHE") ?? Path.Combine(root, "Cache");
        port = 18910;
        for (int i = 0; i < args.Length; i++) {
            if (args[i] == "--headless" || args[i] == "--no-browser") headless = true;
            else if (args[i] == "--webview-smoke-test") hiddenSmoke = true;
            else if (args[i] == "--data" && i + 1 < args.Length) data = args[++i];
            else if (args[i] == "--cache" && i + 1 < args.Length) cache = args[++i];
            else if (args[i] == "--port" && i + 1 < args.Length) port = Int32.Parse(args[++i]);
        }
        if (port < 1024 || port > 65515) throw new ArgumentException("端口须在1024至65515之间。");
        dataDir = PortablePaths.DirectoryPath(data);
        cacheDir = PortablePaths.DirectoryPath(cache);
        logDir = PortablePaths.DirectoryPath(Path.Combine(dataDir, "logs"));
        string temp = PortablePaths.DirectoryPath(Path.Combine(cacheDir, "temp"));
        Environment.SetEnvironmentVariable("TEMP", temp);
        Environment.SetEnvironmentVariable("TMP", temp);
        Text = "照影 · PortraitLab";
        ClientSize = new Size(1360, 860);
        StartPosition = FormStartPosition.CenterScreen;
        Font = new Font("Microsoft YaHei UI", 10);
        BackColor = Color.FromArgb(16, 20, 24); ForeColor = Color.FromArgb(229, 232, 235);
        statusPanel.Dock = DockStyle.Fill; statusPanel.BackColor = BackColor;
        status.Text = "正在打开照影…"; status.Font = new Font("Microsoft YaHei UI", 19, FontStyle.Bold);
        status.AutoSize = false; status.TextAlign = ContentAlignment.MiddleCenter;
        detail.AutoSize = false; detail.TextAlign = ContentAlignment.TopCenter; detail.ForeColor = Color.FromArgb(170, 181, 192);
        retry.Text = "重试"; retry.FlatStyle = FlatStyle.Flat; retry.Visible = false;
        help.Text = "打开微软官方页面"; help.FlatStyle = FlatStyle.Flat; help.Visible = false;
        statusPanel.Controls.Add(status); statusPanel.Controls.Add(detail); statusPanel.Controls.Add(retry); statusPanel.Controls.Add(help);
        contentPanel.Dock = DockStyle.Fill; contentPanel.Controls.Add(statusPanel);
        var helpMenu = new ToolStripMenuItem("帮助");
        helpMenu.DropDownItems.Add("开源与运行许可…", null, delegate { try { RuntimeConsent(true); } catch (Exception exc) { MessageBox.Show(this, exc.Message, "照影"); } });
        helpMenu.DropDownItems.Add("查看运行日志…", null, delegate { ShowLogs(); });
        menu.Items.Add(helpMenu); menu.Dock = DockStyle.Top;
        menu.BackColor = Color.FromArgb(29, 33, 38); menu.ForeColor = ForeColor;
        Controls.Add(contentPanel); Controls.Add(menu); MainMenuStrip = menu;
        Resize += delegate { LayoutStatus(); }; LayoutStatus();
        retry.Click += delegate { BeginStart(); };
        help.Click += delegate { OpenExternal(helpUrl); };
        Load += delegate { FitWindowToScreen(); };
        Shown += delegate { if (headless) Hide(); BeginStart(); };
        FormClosing += HandleClosing;
        FormClosed += delegate {
            cancelStart.Set();
            if (webView != null) { webView.Dispose(); webView = null; }
            if (childJob != null) { childJob.Dispose(); childJob = null; }
        };
        if (headless || hiddenSmoke) { ShowInTaskbar = false; WindowState = FormWindowState.Minimized; Opacity = 0; }
    }

    void FitWindowToScreen() {
        if (headless || hiddenSmoke) return;
        Rectangle area = Screen.FromControl(this).WorkingArea;
        float scale;
        using (Graphics graphics = CreateGraphics()) scale = Math.Max(1f, graphics.DpiX / 96f);
        int margin = Math.Min(24, Math.Max(8, (int)(12 * scale)));
        int availableWidth = Math.Max(1, area.Width - margin * 2);
        int availableHeight = Math.Max(1, area.Height - margin * 2);
        Size preferred = SizeFromClientSize(new Size((int)(1360 * scale), (int)(860 * scale)));
        Size minimum = SizeFromClientSize(new Size((int)(960 * scale), (int)(600 * scale)));
        MinimumSize = new Size(Math.Min(minimum.Width, availableWidth), Math.Min(minimum.Height, availableHeight));
        Size = new Size(Math.Min(preferred.Width, availableWidth), Math.Min(preferred.Height, availableHeight));
        StartPosition = FormStartPosition.Manual;
        Location = new Point(area.Left + (area.Width - Width) / 2, area.Top + (area.Height - Height) / 2);
    }

    void LayoutStatus() {
        int y = Math.Max(55, ClientSize.Height / 2 - 100);
        status.SetBounds(40, y, Math.Max(300, ClientSize.Width - 80), 55);
        detail.SetBounds(65, y + 70, Math.Max(250, ClientSize.Width - 130), 90);
        retry.SetBounds(ClientSize.Width / 2 - 190, y + 175, 120, 38);
        help.SetBounds(ClientSize.Width / 2 - 40, y + 175, 230, 38);
    }
    void UI(Action action) { if (!IsDisposed && IsHandleCreated) try { BeginInvoke(action); } catch (InvalidOperationException) { } }
    void Log(string message) {
        if (String.IsNullOrEmpty(message)) return;
        string line = DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss") + " " + message;
        lock (logLock) {
            try { File.AppendAllText(PortablePaths.Check(Path.Combine(logDir, "launcher.log")), line + Environment.NewLine, new UTF8Encoding(false)); }
            catch (IOException) { }
        }
    }
    void ShowStatus(string title, string message, string officialHelp, bool mayRetry) {
        status.Text = title; detail.Text = message; helpUrl = officialHelp ?? "";
        help.Visible = !String.IsNullOrEmpty(helpUrl); retry.Visible = mayRetry; retry.Enabled = true;
        statusPanel.Visible = true; statusPanel.BringToFront();
    }
    void OpenExternal(string address) {
        if (!DesktopSecurity.ExternalHttps(address)) return;
        try { Process.Start(new ProcessStartInfo(new Uri(address).AbsoluteUri) { UseShellExecute = true }); }
        catch (Exception exc) { Log("无法打开外部链接：" + exc.Message); }
    }
    void ShowLogs() {
        string contents = "暂无运行日志。";
        string path = PortablePaths.Check(Path.Combine(logDir, "launcher.log"));
        try {
            using (FileStream stream = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.ReadWrite)) {
                if (stream.Length > 256 * 1024) stream.Seek(-256 * 1024, SeekOrigin.End);
                using (StreamReader reader = new StreamReader(stream, Encoding.UTF8)) contents = reader.ReadToEnd();
            }
        } catch (IOException) { }
        ShowText("照影 · 运行日志", contents, "日志位置：" + logDir);
    }
    void ShowText(string title, string contents, string source) {
        if (IsDisposed || closeRequested) return;
        using (Form dialog = new Form()) {
            dialog.Text = title; dialog.ClientSize = new Size(920, 560);
            dialog.StartPosition = FormStartPosition.CenterParent; dialog.Font = Font;
            TextBox text = new TextBox { Multiline = true, ReadOnly = true, WordWrap = false, ScrollBars = ScrollBars.Both,
                Dock = DockStyle.Fill, Text = contents, Font = new Font("Consolas", 9) };
            Label folder = new Label { Text = source, Dock = DockStyle.Bottom, Height = 36, AutoEllipsis = true };
            dialog.Controls.Add(text); dialog.Controls.Add(folder); dialog.ShowDialog(this);
        }
    }
    sealed class RuntimeTerm {
        public string title, version, file, text, sha256;
    }
    static string Hash(byte[] value) {
        using (SHA256 algorithm = SHA256.Create()) return BitConverter.ToString(algorithm.ComputeHash(value)).Replace("-", "").ToLowerInvariant();
    }
    bool RuntimeConsent(bool review) {
        string browser = Path.Combine(root, "WebView2", "msedgewebview2.exe");
        var terms = new [] {
            new RuntimeTerm { title = "WebView2", version = File.Exists(browser) ? FileVersionInfo.GetVersionInfo(browser).ProductVersion : "missing", file = "WebView2Runtime/LICENSE.txt" },
            new RuntimeTerm { title = "Microsoft VC++", version = "2015–2022", file = "MicrosoftVC/LICENSE.txt" },
            new RuntimeTerm { title = "NVIDIA CUDA", version = "12.8", file = "NVIDIA/CUDA-12.8-EULA.txt" },
            new RuntimeTerm { title = "NVIDIA cuDNN", version = "9.7", file = "NVIDIA/cuDNN-9.7-License.txt" },
        };
        StringBuilder identity = new StringBuilder("portraitlab-runtime-terms-v1\n");
        var records = new List<Dictionary<string, string>>();
        foreach (RuntimeTerm term in terms) {
            string path = Path.Combine(root, "Licenses", term.file.Replace('/', Path.DirectorySeparatorChar));
            if (!File.Exists(path)) throw new IOException("分享包的组件许可资料不完整，请重新完整解压软件包。缺少：Licenses/" + term.file);
            byte[] bytes = File.ReadAllBytes(path); term.sha256 = Hash(bytes); term.text = Encoding.UTF8.GetString(bytes).TrimStart('\uFEFF');
            identity.Append(term.file).Append('|').Append(term.version).Append('|').Append(term.sha256).Append('\n');
            records.Add(new Dictionary<string, string> { { "component", term.title }, { "version", term.version }, { "file", "Licenses/" + term.file }, { "sha256", term.sha256 } });
        }
        string fingerprint = Hash(Encoding.UTF8.GetBytes(identity.ToString()));
        string receipt = PortablePaths.Check(Path.Combine(cacheDir, "runtime-license-consent.json"));
        // Hidden QA validates that complete texts are present, without fabricating
        // an end-user acceptance record or opening an interactive consent dialog.
        if (hiddenSmoke) { Log("隐藏界面测试：组件许可资料已检查，未记录用户同意。"); return true; }
        if (!review) {
            try {
                var previous = Decode(File.ReadAllText(receipt, Encoding.UTF8));
                if ((bool)previous["accepted"] && (string)previous["fingerprint"] == fingerprint) return true;
            } catch { }
        }
        using (Form dialog = new Form()) {
            dialog.Text = review ? "照影 · 开源与运行许可" : "照影 · 首次使用";
            dialog.ClientSize = new Size(880, 640); dialog.MinimumSize = new Size(700, 500);
            dialog.StartPosition = FormStartPosition.CenterParent; dialog.Font = Font;
            var explanation = new Label { Dock = DockStyle.Top, Height = 100, Padding = new Padding(14),
                Text = "照影源代码按 AGPL-3.0 许可开源。随包的 Microsoft WebView2、VC++ 和 NVIDIA 运行组件分别适用以下条款。\r\n\r\nWebView2 的 SmartScreen 等安全服务可能连接微软并发送相关信息；本软件的照片处理在本机执行。" };
            var tabs = new TabControl { Dock = DockStyle.Fill };
            foreach (RuntimeTerm term in terms) {
                var page = new TabPage(term.title + " " + term.version);
                page.Controls.Add(new TextBox { Multiline = true, ReadOnly = true, ScrollBars = ScrollBars.Vertical, WordWrap = true,
                    Dock = DockStyle.Fill, Text = term.text, Font = new Font("Segoe UI", 9) }); tabs.TabPages.Add(page);
            }
            string notices = Path.Combine(root, "Licenses", "THIRD_PARTY_NOTICES.md");
            if (File.Exists(notices)) {
                var page = new TabPage("开源来源");
                page.Controls.Add(new TextBox { Multiline = true, ReadOnly = true, ScrollBars = ScrollBars.Vertical, WordWrap = true,
                    Dock = DockStyle.Fill, Text = File.ReadAllText(notices, Encoding.UTF8), Font = new Font("Microsoft YaHei UI", 9) }); tabs.TabPages.Add(page);
            }
            var footer = new Panel { Dock = DockStyle.Bottom, Height = review ? 58 : 100 };
            var checkbox = new CheckBox { Text = "我已阅读并同意以上随包运行组件的许可条款。", AutoSize = true, Location = new Point(14, 12), Visible = !review };
            var accept = new Button { Text = review ? "关闭" : "同意并打开照影", Width = 160, Height = 34, DialogResult = DialogResult.OK, Enabled = review };
            var cancel = new Button { Text = "退出", Width = 90, Height = 34, DialogResult = DialogResult.Cancel, Visible = !review };
            var folder = new Button { Text = "打开许可文件夹", Width = 160, Height = 34 };
            footer.Resize += delegate { int y = footer.Height - 46; folder.SetBounds(14, y, 160, 34); accept.SetBounds(footer.Width - 174, y, 160, 34); cancel.SetBounds(footer.Width - 276, y, 90, 34); };
            checkbox.CheckedChanged += delegate { accept.Enabled = checkbox.Checked; };
            folder.Click += delegate { Process.Start(new ProcessStartInfo("explorer.exe", Quote(Path.Combine(root, "Licenses"))) { UseShellExecute = true }); };
            footer.Controls.Add(checkbox); footer.Controls.Add(folder); footer.Controls.Add(cancel); footer.Controls.Add(accept);
            dialog.Controls.Add(tabs); dialog.Controls.Add(footer); dialog.Controls.Add(explanation);
            dialog.AcceptButton = accept; dialog.CancelButton = review ? accept : cancel;
            if (dialog.ShowDialog(this) != DialogResult.OK || (!review && !checkbox.Checked)) return false;
        }
        if (!review) {
            var consent = new Dictionary<string, object> { { "accepted", true }, { "fingerprint", fingerprint },
                { "accepted_at", DateTimeOffset.Now.ToString("o") }, { "application_version", "0.7.0" }, { "components", records } };
            File.WriteAllText(receipt, Encode(consent), new UTF8Encoding(false));
        }
        return true;
    }
    async void ShowLicense(string address) {
        Uri uri;
        if (hiddenSmoke || headless || !DesktopSecurity.LocalOrigin(address, port) ||
            !Uri.TryCreate(address, UriKind.Absolute, out uri) || !uri.AbsolutePath.StartsWith("/licenses/", StringComparison.Ordinal)) return;
        try {
            string contents = await Task.Run(delegate {
                var request = (HttpWebRequest)WebRequest.Create(uri); request.Proxy = null;
                request.Timeout = 3000; request.ReadWriteTimeout = 3000; request.AllowAutoRedirect = false;
                using (WebResponse response = request.GetResponse())
                using (StreamReader reader = new StreamReader(response.GetResponseStream(), Encoding.UTF8)) {
                    char[] buffer = new char[4096]; StringBuilder text = new StringBuilder(); int count;
                    while ((count = reader.Read(buffer, 0, buffer.Length)) > 0) {
                        if (text.Length + count > 1024 * 1024) throw new IOException("许可文本超过显示大小限制。");
                        text.Append(buffer, 0, count);
                    }
                    return text.ToString();
                }
            });
            ShowText("照影 · " + Path.GetFileName(uri.LocalPath), contents, "开源许可：" + uri.AbsolutePath);
        } catch (Exception exc) {
            Log("许可文本无法打开：" + exc.Message);
            if (!IsDisposed && !closeRequested) MessageBox.Show(this, "许可文本暂时无法打开，请稍后重试。", "照影", MessageBoxButtons.OK, MessageBoxIcon.Information);
        }
    }

    Dictionary<string, object> Request(string address, bool authenticated, int timeout) {
        HttpWebRequest request = (HttpWebRequest)WebRequest.Create(address);
        request.Proxy = null; request.Timeout = timeout; request.ReadWriteTimeout = timeout;
        if (authenticated) request.Headers["X-PortraitLab-Token"] = token;
        using (WebResponse response = request.GetResponse()) using (StreamReader reader = new StreamReader(response.GetResponseStream()))
            return Decode(reader.ReadToEnd());
    }
    Dictionary<string, object> Probe(int candidate) {
        try { return Request("http://127.0.0.1:" + candidate + "/api/bootstrap", false, 1200); }
        catch { return null; }
    }
    bool SameWorkspace(Dictionary<string, object> result) {
        try { if ((string)result["application"] != "portraitlab-v2") return false; var storage = (Dictionary<string, object>)result["storage"]; return Path.GetFullPath((string)storage["data_dir"]).TrimEnd('\\').Equals(dataDir.TrimEnd('\\'), StringComparison.OrdinalIgnoreCase); }
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
        if (String.IsNullOrWhiteSpace(info.EnvironmentVariables["PORTRAITLAB_AI_PYTHON"])) info.EnvironmentVariables["PORTRAITLAB_AI_PYTHON"] = Path.Combine(root, "AI", "python.exe");
        info.EnvironmentVariables["PORTRAITLAB_BUILD_VERSION"] = "0.7.0";
        var dirs = new Dictionary<string, string> { { "PIP_CACHE_DIR", "pip" }, { "MPLCONFIGDIR", "matplotlib" }, { "HF_HOME", "huggingface" }, { "TORCH_HOME", "torch" }, { "CUDA_CACHE_PATH", "cuda" }, { "TORCH_EXTENSIONS_DIR", "torch-extensions" }, { "TORCHINDUCTOR_CACHE_DIR", "torch-inductor" }, { "TRITON_CACHE_DIR", "triton" }, { "XDG_CACHE_HOME", "xdg" }, { "UV_CACHE_DIR", "uv" } };
        foreach (var pair in dirs) info.EnvironmentVariables[pair.Key] = PortablePaths.DirectoryPath(Path.Combine(cacheDir, pair.Value));
        info.EnvironmentVariables["HF_HUB_DISABLE_TELEMETRY"] = "1"; info.EnvironmentVariables["DO_NOT_TRACK"] = "1";
        info.EnvironmentVariables["FASTAPI_OTEL_AUTO_CONFIGURE"] = "false";
    }
    async void BeginStart() {
        if (starting || closeRequested) return;
        starting = true; retry.Enabled = false;
        ShowStatus("正在打开照影…", "", null, false);
        try {
            if (!headless && !RuntimeConsent(false)) { closing = true; Close(); return; }
            cancelStart.Reset();
            startTask = Task.Run((Action)StartBackend);
            await startTask;
            starting = false;
            if (closeRequested || IsDisposed) return;
            if (!headless) await InitializeBrowser();
        } catch (Exception exc) {
            starting = false; Log(exc.ToString());
            if (closeRequested || IsDisposed) return;
            if (headless || hiddenSmoke) { Environment.ExitCode = 1; Close(); return; }
            string official = exc is WebView2RuntimeNotFoundException ? WebViewHelpUrl : null;
            ShowStatus("暂时无法打开照影", exc.Message, official, true);
        }
    }
    void StartBackend() {
        if (ownBackend && backend != null && !backend.HasExited && ready) return;
        if (cancelStart.IsSet) throw new OperationCanceledException();
        string python = Path.Combine(root, "runtime", "python.exe");
        if (!File.Exists(python) || !Directory.Exists(appDir)) throw new FileNotFoundException("软件文件不完整，请完整解压软件包后再启动。");
        Dictionary<string, object> existing = null; bool found = false; int firstPort = port;
        for (int candidate = firstPort; candidate < firstPort + 20; candidate++) {
            if (cancelStart.IsSet) throw new OperationCanceledException();
            var result = Probe(candidate);
            if (SameWorkspace(result)) { port = candidate; existing = result; found = true; break; }
            if (PortFree(candidate)) { port = candidate; found = true; break; }
        }
        if (!found) throw new IOException("无法找到可用本地端口，请使用 --port 指定其他端口。");
        url = "http://127.0.0.1:" + port;
        if (existing != null) { ownBackend = false; MarkReady(existing); Log("连接已有服务；关闭本窗口不会停止该服务。"); return; }
        if (childJob != null) childJob.Dispose(); childJob = new ChildJob();
        string arguments = "-I -B -X utf8 -m experiments.portraitlab_v2.server --port " + port + " --data " + Quote(dataDir) +
            " --analysis-models " + Quote(Path.Combine(root, "Data", "models", "analysis")) +
            " --ai-python " + Quote(Path.Combine(root, "AI", "python.exe")) +
            " --neural-models " + Quote(Path.Combine(root, "Data", "models", "neural")) +
            " --vendor " + Quote(Path.Combine(root, "vendor", "ModelScope"));
        var info = new ProcessStartInfo(python, arguments);
        info.WorkingDirectory = appDir; info.UseShellExecute = false; info.CreateNoWindow = true;
        info.RedirectStandardOutput = true; info.RedirectStandardError = true;
        info.StandardOutputEncoding = Encoding.UTF8; info.StandardErrorEncoding = Encoding.UTF8;
        ChildEnvironment(info);
        backend = new Process { StartInfo = info, EnableRaisingEvents = true };
        backend.OutputDataReceived += delegate(object sender, DataReceivedEventArgs e) { Log(e.Data); };
        backend.ErrorDataReceived += delegate(object sender, DataReceivedEventArgs e) { Log(e.Data); };
        backend.Exited += delegate {
            Log("处理服务已退出。");
            if (stopping || closeRequested || starting) return;
            ready = false;
            UI(delegate { if (headless || hiddenSmoke || backend.ExitCode == 0) { closing = true; Close(); }
                else ShowStatus("处理服务已停止", "可以重试打开软件。", null, true); });
        };
        if (cancelStart.IsSet) throw new OperationCanceledException();
        backend.Start(); ownBackend = true;
        if (!childJob.Add(backend)) Log("未能启用Windows子进程组；停止时将仅回收本窗口拥有的服务进程树。");
        backend.BeginOutputReadLine(); backend.BeginErrorReadLine();
        Log("本地处理进程 PID " + backend.Id);
        for (int count = 0; count < 100; count++) {
            if (cancelStart.IsSet) throw new OperationCanceledException();
            if (backend.HasExited) throw new IOException("处理服务未能启动，退出码：" + backend.ExitCode);
            var result = Probe(port);
            if (SameWorkspace(result)) { MarkReady(result); return; }
            cancelStart.Wait(500);
        }
        throw new TimeoutException("打开软件超时，请重试。");
    }
    void MarkReady(Dictionary<string, object> result) {
        token = result.ContainsKey("token") ? (string)result["token"] : "";
        ready = true; WriteStatus(false); Log("本地服务已就绪：" + url);
    }
    void WriteStatus(bool desktopReady) {
        var record = new Dictionary<string, object> { { "ready", ready }, { "desktop_ready", desktopReady }, { "url", url }, { "port", port },
            { "launcher_pid", Process.GetCurrentProcess().Id }, { "server_pid", ownBackend && backend != null ? (object)backend.Id : null },
            { "owns_server", ownBackend }, { "data_dir", dataDir }, { "cache_dir", cacheDir }, { "runtime", Path.Combine(root, "runtime", "python.exe") },
            { "webview2_runtime", browserVersion }, { "mode", headless ? "headless" : "embedded" } };
        File.WriteAllText(PortablePaths.Check(Path.Combine(logDir, "launcher-status.json")), Encode(record), new UTF8Encoding(false));
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
    bool InCache(string path) {
        string value = PortablePaths.Check(path).TrimEnd('\\');
        return value.Equals(cacheDir.TrimEnd('\\'), StringComparison.OrdinalIgnoreCase) || value.StartsWith(cacheDir.TrimEnd('\\') + "\\", StringComparison.OrdinalIgnoreCase);
    }
    static void AllowBrowserContainers(string runtime) {
        // Fixed Version WebView2 120+ requires these read/execute grants for
        // unpackaged Win32 applications on Windows 10. Changes are restricted
        // to the included browser; user photos and other directories are untouched.
        DirectorySecurity security = Directory.GetAccessControl(runtime);
        bool changed = false;
        foreach (string value in new [] { "S-1-15-2-1", "S-1-15-2-2" }) {
            var sid = new SecurityIdentifier(value);
            bool present = false;
            foreach (FileSystemAccessRule rule in security.GetAccessRules(true, true, typeof(SecurityIdentifier))) {
                if (rule.IdentityReference.Equals(sid) && rule.AccessControlType == AccessControlType.Allow &&
                    (rule.FileSystemRights & FileSystemRights.ReadAndExecute) == FileSystemRights.ReadAndExecute &&
                    (rule.InheritanceFlags & (InheritanceFlags.ContainerInherit | InheritanceFlags.ObjectInherit)) ==
                        (InheritanceFlags.ContainerInherit | InheritanceFlags.ObjectInherit)) { present = true; break; }
            }
            if (!present) {
                security.AddAccessRule(new FileSystemAccessRule(sid, FileSystemRights.ReadAndExecute,
                    InheritanceFlags.ContainerInherit | InheritanceFlags.ObjectInherit, PropagationFlags.None, AccessControlType.Allow));
                changed = true;
            }
        }
        if (changed) Directory.SetAccessControl(runtime, security);
    }
    async Task InitializeBrowser() {
        if (webView != null) { webView.Dispose(); webView = null; }
        initializedBrowser = false;
        string profile = PortablePaths.DirectoryPath(Path.Combine(cacheDir, "webview2", "profile"));
        string disk = PortablePaths.DirectoryPath(Path.Combine(cacheDir, "webview2", "disk-cache"));
        string crashes = PortablePaths.DirectoryPath(Path.Combine(cacheDir, "webview2", "crashes"));
        string browserArgs = "--disk-cache-dir=" + Quote(disk) + " --crash-dumps-dir=" + Quote(crashes) + " --no-first-run --no-default-browser-check";
        Environment.SetEnvironmentVariable("WEBVIEW2_USER_DATA_FOLDER", profile);
        string fixedRuntime = PortablePaths.Check(Path.Combine(root, "WebView2"));
        if (fixedRuntime.StartsWith(@"\\", StringComparison.Ordinal))
            throw new IOException("内置界面组件不支持直接从网络共享目录运行，请先把完整软件包解压到本机磁盘。");
        if (!File.Exists(Path.Combine(fixedRuntime, "msedgewebview2.exe")))
            throw new IOException("软件包缺少内置界面运行组件。请重新完整解压包含模型的分享包。");
        try { AllowBrowserContainers(fixedRuntime); }
        catch (UnauthorizedAccessException) {
            throw new IOException("内置界面目录无法设置必要的读取权限，请把完整软件包解压到自己可写的本机文件夹。");
        }
        Environment.SetEnvironmentVariable("WEBVIEW2_BROWSER_EXECUTABLE_FOLDER", fixedRuntime);
        Environment.SetEnvironmentVariable("WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS", browserArgs);
        Environment.SetEnvironmentVariable("WEBVIEW2_PIPE_FOR_SCRIPT_DEBUGGER", null);
        browserVersion = CoreWebView2Environment.GetAvailableBrowserVersionString(fixedRuntime);
        if (String.IsNullOrEmpty(browserVersion)) throw new WebView2RuntimeNotFoundException();
        var options = new CoreWebView2EnvironmentOptions(); options.AdditionalBrowserArguments = browserArgs;
        webEnvironment = await CoreWebView2Environment.CreateAsync(fixedRuntime, profile, options);
        if (!InCache(webEnvironment.UserDataFolder) || !InCache(webEnvironment.FailureReportFolderPath))
            throw new IOException("WebView2 的配置或错误报告目录未采用指定缓存位置，请检查本机 WebView2 管理策略。");
        if (closeRequested || IsDisposed) return;
        webView = new WebView2 { Dock = DockStyle.Fill, DefaultBackgroundColor = BackColor };
        contentPanel.Controls.Add(webView); webView.SendToBack();
        IntPtr handle = webView.Handle;
        await webView.EnsureCoreWebView2Async(webEnvironment);
        if (closeRequested || IsDisposed) return;
        var core = webView.CoreWebView2;
        core.Settings.AreHostObjectsAllowed = false;
        core.Settings.IsWebMessageEnabled = true;
        core.Settings.AreDevToolsEnabled = false;
        core.Settings.AreDefaultContextMenusEnabled = false;
        core.Settings.IsStatusBarEnabled = false;
        core.Settings.IsPasswordAutosaveEnabled = false;
        core.Settings.IsGeneralAutofillEnabled = false;
        core.NavigationStarting += delegate(object sender, CoreWebView2NavigationStartingEventArgs e) {
            if (DesktopSecurity.LocalOrigin(e.Uri, port)) return;
            e.Cancel = true;
            if (e.IsUserInitiated && DesktopSecurity.ExternalHttps(e.Uri)) OpenExternal(e.Uri);
        };
        core.FrameNavigationStarting += delegate(object sender, CoreWebView2NavigationStartingEventArgs e) {
            if (!DesktopSecurity.LocalOrigin(e.Uri, port)) e.Cancel = true;
        };
        core.NewWindowRequested += delegate(object sender, CoreWebView2NewWindowRequestedEventArgs e) {
            e.Handled = true;
            if (e.IsUserInitiated && DesktopSecurity.LocalOrigin(e.Uri, port)) ShowLicense(e.Uri);
            else if (e.IsUserInitiated && DesktopSecurity.ExternalHttps(e.Uri)) OpenExternal(e.Uri);
        };
        core.PermissionRequested += delegate(object sender, CoreWebView2PermissionRequestedEventArgs e) { e.State = CoreWebView2PermissionState.Deny; };
        core.DownloadStarting += ExportDownload;
        core.AddWebResourceRequestedFilter("file:*", CoreWebView2WebResourceContext.All);
        core.WebResourceRequested += delegate(object sender, CoreWebView2WebResourceRequestedEventArgs e) {
            if (e.Request.Uri.StartsWith("file:", StringComparison.OrdinalIgnoreCase))
                e.Response = webEnvironment.CreateWebResourceResponse(new MemoryStream(new byte[0]), 403, "Forbidden", "Content-Type: text/plain");
        };
        core.WebMessageReceived += ReceiveMessage;
        core.NavigationCompleted += async delegate(object sender, CoreWebView2NavigationCompletedEventArgs e) {
            if (!e.IsSuccess || !DesktopSecurity.LocalOrigin(core.Source, port)) {
                Log("WebView2 navigation failed: " + e.WebErrorStatus + "; source=" + core.Source);
                if (hiddenSmoke) { Environment.ExitCode = 1; initializedBrowser = false; Close(); return; }
                if (!closeRequested) ShowStatus("界面未能加载", "请重试打开。", null, true);
                return;
            }
            initializedBrowser = true; statusPanel.Visible = false; webView.BringToFront(); WriteStatus(true);
            if (hiddenSmoke && !smokeStarted) { smokeStarted = true; await SmokeTest(); }
        };
        core.ProcessFailed += delegate(object sender, CoreWebView2ProcessFailedEventArgs e) {
            Log("WebView2 process failure: " + e.ProcessFailedKind + "; reason=" + e.Reason + "; exit=" + e.ExitCode);
            if (hiddenSmoke) { Environment.ExitCode = 1; initializedBrowser = false; Close(); return; }
            if (!closeRequested) ShowStatus("界面进程已停止", "本地数据已保留，可以重新打开界面。", null, true);
        };
        core.Navigate(url + "/");
    }
    async void ExportDownload(object sender, CoreWebView2DownloadStartingEventArgs e) {
        e.Cancel = true; e.Handled = true;
        Uri uri;
        if (headless || hiddenSmoke || !DesktopSecurity.LocalOrigin(e.DownloadOperation.Uri, port) ||
            !Uri.TryCreate(e.DownloadOperation.Uri, UriKind.Absolute, out uri) ||
            !uri.AbsolutePath.StartsWith("/api/exports/", StringComparison.Ordinal)) return;
        string filename = Uri.UnescapeDataString(uri.Segments[uri.Segments.Length - 1]);
        if (Path.GetFileName(filename) != filename || !filename.EndsWith(".png", StringComparison.OrdinalIgnoreCase)) return;
        using (CoreWebView2Deferral deferral = e.GetDeferral()) {
            try {
                // Leave the WebView2 callback before entering a modal dialog;
                // nested message loops inside its event callbacks are unsupported.
                await Task.Yield();
                if (closeRequested || IsDisposed) return;
                using (SaveFileDialog dialog = new SaveFileDialog()) {
                    dialog.Title = "保存处理结果"; dialog.Filter = "PNG 图像 (*.png)|*.png";
                    dialog.DefaultExt = "png"; dialog.AddExtension = true; dialog.OverwritePrompt = true;
                    dialog.FileName = filename; dialog.InitialDirectory = PortablePaths.DirectoryPath(Path.Combine(root, "Exports"));
                    if (dialog.ShowDialog(this) != DialogResult.OK) return;
                    string selected = PortablePaths.Check(dialog.FileName);
                    string backendExport = Path.GetFullPath(Path.Combine(dataDir, "exports", filename));
                    if (String.Equals(selected, backendExport, StringComparison.OrdinalIgnoreCase)) {
                        MessageBox.Show(this, "处理结果已经保存在此位置。", "照影", MessageBoxButtons.OK, MessageBoxIcon.Information);
                        return;
                    }
                    e.ResultFilePath = selected; e.Cancel = false; e.Handled = false;
                }
            } catch (Exception exc) {
                Log("导出另存为失败：" + exc.Message);
            } finally {
                deferral.Complete();
            }
        }
    }
    void ReceiveMessage(object sender, CoreWebView2WebMessageReceivedEventArgs e) {
        if (webView == null || !DesktopSecurity.LocalOrigin(e.Source, port) || !DesktopSecurity.LocalOrigin(webView.CoreWebView2.Source, port)) return;
        try {
            string raw = e.WebMessageAsJson;
            if (raw.Length > 8192) return;
            Dictionary<string, object> message = Decode(raw);
            object kind; if (message == null || !message.TryGetValue("type", out kind) || !(kind is string)) return;
            if ((string)kind == "openLogs") { if (!headless && !hiddenSmoke) ShowLogs(); }
            else if ((string)kind == "quit") Close();
            else if ((string)kind == "quitReady" && quitResult != null) {
                object nonce, ok, error;
                if (!message.TryGetValue("nonce", out nonce) || !(nonce is string) || (string)nonce != quitNonce) return;
                bool succeeded = message.TryGetValue("ok", out ok) && ok is bool && (bool)ok;
                string reason = message.TryGetValue("message", out error) && error is string ? (string)error : "参数尚未保存完成。";
                quitResult.TrySetResult(succeeded ? null : reason.Substring(0, Math.Min(reason.Length, 500)));
            }
        } catch (Exception exc) { Log("忽略不支持的界面消息：" + exc.GetType().Name); }
    }
    async Task<bool> FlushInterface() {
        if (!initializedBrowser || webView == null || webView.CoreWebView2 == null || !DesktopSecurity.LocalOrigin(webView.CoreWebView2.Source, port)) return true;
        quitNonce = Guid.NewGuid().ToString("N");
        quitResult = new TaskCompletionSource<string>();
        string nonce = Encode(quitNonce);
        string code = "(async function(){try{if(typeof window.portraitlabPrepareToQuit!=='function')throw new Error('界面尚未就绪，请稍后再退出。');await window.portraitlabPrepareToQuit();window.chrome.webview.postMessage({type:'quitReady',nonce:" + nonce + ",ok:true});}catch(e){window.chrome.webview.postMessage({type:'quitReady',nonce:" + nonce + ",ok:false,message:String(e&&e.message||e)});}})();";
        try {
            await webView.CoreWebView2.ExecuteScriptAsync(code);
            Task completed = await Task.WhenAny(quitResult.Task, Task.Delay(20000));
            string problem = completed == quitResult.Task ? await quitResult.Task : "保存参数尚未完成，请稍后重试退出。";
            if (problem == null) return true;
            Log("退出前保存未完成：" + problem);
            if (!hiddenSmoke) MessageBox.Show(this, problem, "尚未退出", MessageBoxButtons.OK, MessageBoxIcon.Information);
            return false;
        } catch (Exception exc) {
            Log("退出前保存失败：" + exc.Message);
            if (!hiddenSmoke) MessageBox.Show(this, "参数尚未确认保存。请重试保存后再退出。", "尚未退出", MessageBoxButtons.OK, MessageBoxIcon.Information);
            return false;
        } finally { quitResult = null; quitNonce = ""; }
    }
    async void HandleClosing(object sender, FormClosingEventArgs e) {
        if (closing) return;
        e.Cancel = true;
        if (closeRequested) return;
        closeRequested = true;
        try {
            // Freeze user input before checking jobs and taking the save snapshot.
            // Script execution and pending asynchronous saves continue normally.
            if (webView != null && !webView.IsDisposed) webView.Enabled = false;
            if (!headless && !await FlushInterface()) { closeRequested = false; return; }
            cancelStart.Set();
            if (startTask != null) try { await startTask; } catch { }
            await Task.Run((Action)StopOwnedBackend);
            closing = true; Close();
        } catch (Exception exc) { Log(exc.ToString()); closeRequested = false; }
        finally {
            if (!closing && !IsDisposed && webView != null && !webView.IsDisposed) webView.Enabled = true;
        }
    }
    void StopOwnedBackend() {
        stopping = true;
        try {
            if (!ownBackend || backend == null) return;
            if (!backend.HasExited) {
                try {
                    var request = (HttpWebRequest)WebRequest.Create(url + "/api/shutdown");
                    request.Proxy = null; request.Method = "POST"; request.Headers["X-PortraitLab-Token"] = token;
                    request.ContentLength = 0; request.Timeout = 3000;
                    using (request.GetResponse()) { }
                } catch (Exception exc) { Log("正常停止请求：" + exc.Message); }
                if (!backend.WaitForExit(15000)) {
                    Log("回收本窗口拥有的处理进程树。");
                    var kill = new ProcessStartInfo("taskkill.exe", "/PID " + backend.Id + " /T /F") { UseShellExecute = false, CreateNoWindow = true };
                    using (Process process = Process.Start(kill)) { if (process != null) process.WaitForExit(5000); }
                }
            }
        } finally {
            if (ownBackend && childJob != null) { childJob.Dispose(); childJob = null; }
            ownBackend = false; ready = false; stopping = false;
        }
    }
    async Task SmokeTest() {
        try {
            await Task.Delay(300);
            var state = Decode(await webView.CoreWebView2.ExecuteScriptAsync("({origin:location.origin,ready:document.readyState,bridge:!!window.chrome.webview,saveHook:typeof window.portraitlabPrepareToQuit==='function'})"));
            if ((string)state["origin"] != url || (string)state["ready"] != "complete" || !(bool)state["bridge"] || !(bool)state["saveHook"])
                throw new InvalidOperationException("Embedded UI did not initialize its native bridge and save hook");
            bool rejectedSave = false, awaitedSave = false;
            await webView.CoreWebView2.ExecuteScriptAsync("window.__portraitlabSmokeQuit=window.portraitlabPrepareToQuit;window.portraitlabPrepareToQuit=async()=>{await new Promise(r=>setTimeout(r,100));throw new Error('smoke-test expected save rejection');};");
            rejectedSave = !await FlushInterface();
            await webView.CoreWebView2.ExecuteScriptAsync("window.__portraitlabSmokeSaved=false;window.portraitlabPrepareToQuit=async()=>{await new Promise(r=>setTimeout(r,150));await window.__portraitlabSmokeQuit();window.__portraitlabSmokeSaved=true;};");
            awaitedSave = await FlushInterface() && await webView.CoreWebView2.ExecuteScriptAsync("window.__portraitlabSmokeSaved") == "true";
            await webView.CoreWebView2.ExecuteScriptAsync("window.portraitlabPrepareToQuit=window.__portraitlabSmokeQuit;delete window.__portraitlabSmokeQuit;delete window.__portraitlabSmokeSaved;");
            if (!rejectedSave || !awaitedSave) throw new InvalidOperationException("Asynchronous save-on-close regression");
            bool manualMarksBlocked = false, importBlocked = false;
            Exception quitGuardError = null;
            await webView.CoreWebView2.ExecuteScriptAsync("window.__portraitlabV2SmokeState={strokes:window.PortraitV2.state.draftStrokes,importing:window.PortraitV2.state.importing};window.PortraitV2.state.draftStrokes=[{points:[[10,10]],diameter:24}];");
            try {
                manualMarksBlocked = !await FlushInterface();
                await webView.CoreWebView2.ExecuteScriptAsync("window.PortraitV2.state.draftStrokes=window.__portraitlabV2SmokeState.strokes;window.PortraitV2.state.importing=true;");
                importBlocked = !await FlushInterface();
            } catch (Exception exc) { quitGuardError = exc; }
            await webView.CoreWebView2.ExecuteScriptAsync("window.PortraitV2.state.draftStrokes=window.__portraitlabV2SmokeState.strokes;window.PortraitV2.state.importing=window.__portraitlabV2SmokeState.importing;delete window.__portraitlabV2SmokeState;");
            if (quitGuardError != null) throw quitGuardError;
            if (!manualMarksBlocked || !importBlocked || !await FlushInterface())
                throw new InvalidOperationException("V2 pending marks/import quit guard regression");
            var security = new Dictionary<string, bool> {
                { "accept_local", DesktopSecurity.LocalOrigin(url + "/", port) },
                { "reject_other_port", !DesktopSecurity.LocalOrigin("http://127.0.0.1:" + (port + 1), port) },
                { "reject_prefix_host", !DesktopSecurity.LocalOrigin("http://127.0.0.1.evil.example:" + port, port) },
                { "reject_file", !DesktopSecurity.LocalOrigin("file:///C:/Windows/win.ini", port) && !DesktopSecurity.ExternalHttps("file:///C:/Windows/win.ini") },
                { "reject_command", !DesktopSecurity.ExternalHttps("cmd:/c calc") },
                { "reject_userinfo", !DesktopSecurity.LocalOrigin("http://fake@127.0.0.1:" + port, port) },
                { "accept_https", DesktopSecurity.ExternalHttps("https://github.com/ymsl56422839-cmd/portraitlab") },
                { "reject_external_http", !DesktopSecurity.ExternalHttps("http://example.com") },
            };
            foreach (bool passed in security.Values) if (!passed) throw new InvalidOperationException("Origin policy regression");
            var record = new Dictionary<string, object> { { "ok", true }, { "runtime", browserVersion }, { "origin", url }, { "dom", state },
                { "user_data_folder", webEnvironment.UserDataFolder }, { "failure_report_folder", webEnvironment.FailureReportFolderPath },
                { "cache_dir", cacheDir }, { "browser_pid", webView.CoreWebView2.BrowserProcessId }, { "security", security },
                { "hidden", true }, { "embedded", true }, { "owns_server", ownBackend },
                { "save_failure_prevents_close", rejectedSave }, { "async_save_waited", awaitedSave },
                { "manual_marks_prevent_close", manualMarksBlocked }, { "import_prevents_close", importBlocked } };
            File.WriteAllText(PortablePaths.Check(Path.Combine(logDir, "desktop-smoke.json")), Encode(record), new UTF8Encoding(false));
            await webView.CoreWebView2.ExecuteScriptAsync("window.chrome.webview.postMessage({type:'quit'});");
        } catch (Exception exc) { Log("WebView2 smoke test: " + exc); Environment.ExitCode = 1; initializedBrowser = false; Close(); }
    }
    string InstanceName() {
        using (SHA256 sha = SHA256.Create()) {
            byte[] digest = sha.ComputeHash(Encoding.UTF8.GetBytes(dataDir.TrimEnd('\\').ToUpperInvariant()));
            return "Local\\PortraitLabV2_" + BitConverter.ToString(digest).Replace("-", "");
        }
    }
    void ActivateExisting() {
        try {
            var record = Decode(File.ReadAllText(Path.Combine(logDir, "launcher-status.json"), Encoding.UTF8));
            using (Process process = Process.GetProcessById(Convert.ToInt32(record["launcher_pid"]))) {
                if (!String.Equals(Path.GetFullPath(process.MainModule.FileName), Path.Combine(root, "PortraitLab.exe"), StringComparison.OrdinalIgnoreCase)) return;
                IntPtr window = process.MainWindowHandle;
                if (window != IntPtr.Zero) { ShowWindow(window, 9); SetForegroundWindow(window); }
            }
        } catch { }
    }
    [STAThread]
    static void Main(string[] args) {
        Application.EnableVisualStyles(); Application.SetCompatibleTextRenderingDefault(false);
        try {
            using (Launcher window = new Launcher(args))
            using (Mutex instance = new Mutex(false, window.InstanceName())) {
                bool acquired;
                try { acquired = instance.WaitOne(0, false); } catch (AbandonedMutexException) { acquired = true; }
                if (!acquired) { window.ActivateExisting(); return; }
                try { Application.Run(window); } finally { instance.ReleaseMutex(); }
            }
        }
        catch (Exception exc) {
            bool quiet = Array.IndexOf(args, "--headless") >= 0 || Array.IndexOf(args, "--no-browser") >= 0 || Array.IndexOf(args, "--webview-smoke-test") >= 0;
            if (!quiet) MessageBox.Show(exc.Message + "\n\n可通过 --data 和 --cache 指定可写目录。", "照影无法启动", MessageBoxButtons.OK, MessageBoxIcon.Error);
            Environment.ExitCode = 1;
        }
    }
}
