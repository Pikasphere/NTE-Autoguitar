# 重新生成 docs/gtab_page.png（或手柄页 / 回放轮盘）的开发脚本（Windows）。
#
# 用法（在项目根目录）：
#   pwsh -File tests/capture_gtab_screenshot.ps1                 # GTAB 页 → docs/gtab_page.png
#   pwsh -File tests/capture_gtab_screenshot.ps1 -Page gamepad   # 手柄页 → docs/gamepad_page.png
#   pwsh -File tests/capture_gtab_screenshot.ps1 -Page wheel     # 回放轮盘 → docs/replay_wheel.png
#   $env:PREVIEW_SCORE='gct'; $env:PREVIEW_SCALE='1.6'  → docs/replay_wheel_gct.png（放大核对）
#
# 环境变量（预览脚本自己的开关，由这里继承给 pythonw）：
#   PREVIEW_WIDTH / PREVIEW_HEIGHT  窗口尺寸；App 真实内容宽度是 1144
#   PREVIEW_SCORE=gtab|gct          手柄页载入哪种谱面；gct 时默认存 docs/gamepad_gct.png
#   PREVIEW_STATE=play              手柄页摆成「和弦扫到一半」/ GTAB 页摆成「试听中」
#   PREVIEW_PAGE=4                  用第 4 页和弦（Cmaj9 / Am7/G，名字最长）当示例
#   PREVIEW_SCALE=1.6               轮盘预览整体放大（配合 -Page wheel 看和弦名布局）
#   -Out docs/xxx.png               自定义输出文件（不会覆盖默认那张）
#
# 注意：必须先 SetProcessDPIAware()。125% 缩放（DPI 120）下，非 DPI 感知的
# PowerShell 调 GetWindowRect/CopyFromScreen 拿到的是被虚拟化的逻辑坐标，
# 只会截到窗口左上角 80%，看起来像界面被裁掉（本会话踩过）。
# 另外用 pythonw.exe 启动，避免控制台窗口盖住 flet 窗口导致截图全黑。
# 取窗口矩形用 DWMWA_EXTENDED_FRAME_BOUNDS（不是 GetWindowRect：后者含投影阴影，
# 会把桌面图标/任务栏截进来）；Win11 的圆角会透明露底，所以截图前先用
# DwmSetWindowAttribute 关掉圆角，再内缩 2px 去掉 1px 透明边框。
param(
    [ValidateSet('gtab', 'gamepad', 'wheel')]
    [string]$Page = 'gtab',
    [string]$Out = ''
)
$ErrorActionPreference = 'Continue'
$project = Split-Path -Parent $PSScriptRoot
$pythonw = Join-Path $project '.venv\Scripts\pythonw.exe'
if ($Page -eq 'gamepad') {
    $title = '手柄测试预览'
    $previewScript = 'tests\preview_gamepad_page.py'
    # PREVIEW_SCORE=gct（预览脚本的开关）时不要覆盖 gtab 那张，另存 docs/gamepad_gct.png；
    # 再叠 PREVIEW_STATE=play 时存 docs/gamepad_gct_play.png（别把待机那张覆盖成回放态）
    if ($env:PREVIEW_SCORE -eq 'gct') {
        if ($env:PREVIEW_STATE -eq 'play') {
            $outPng = 'docs\gamepad_gct_play.png'
        } else {
            $outPng = 'docs\gamepad_gct.png'
        }
    } else {
        $outPng = 'docs\gamepad_page.png'
    }
} elseif ($Page -eq 'wheel') {
    $title = '回放轮盘预览'
    $previewScript = 'tests\preview_replay_wheel.py'
    if ($env:PREVIEW_SCORE -eq 'gct') {
        $outPng = 'docs\replay_wheel_gct.png'
    } else {
        $outPng = 'docs\replay_wheel.png'
    }
} else {
    $title = 'GTAB 页面预览'
    $previewScript = 'tests\preview_gtab_page.py'
    $outPng = 'docs\gtab_page.png'
}
if ($Out) { $outPng = $Out }

Add-Type @"
using System;
using System.Text;
using System.Runtime.InteropServices;
public class WinCapShot {
  public delegate bool EnumProc(IntPtr hWnd, IntPtr lParam);
  [DllImport("user32.dll")] public static extern bool SetProcessDPIAware();
  [DllImport("user32.dll")] public static extern bool EnumWindows(EnumProc cb, IntPtr lParam);
  [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr hWnd);
  [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr hWnd, out RECT r);
  [DllImport("user32.dll")] public static extern bool GetClientRect(IntPtr hWnd, out RECT r);
  [DllImport("user32.dll")] public static extern bool ClientToScreen(IntPtr hWnd, ref POINT p);
  [DllImport("dwmapi.dll")] public static extern int DwmGetWindowAttribute(IntPtr hWnd, int attr, out RECT r, int size);
  [DllImport("dwmapi.dll")] public static extern int DwmSetWindowAttribute(IntPtr hWnd, int attr, ref int value, int size);
  [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern int GetWindowText(IntPtr hWnd, StringBuilder s, int n);
  [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr hWnd, out uint processId);
  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hWnd);
  [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr hWnd, int n);
  [StructLayout(LayoutKind.Sequential)] public struct RECT { public int Left; public int Top; public int Right; public int Bottom; }
  [StructLayout(LayoutKind.Sequential)] public struct POINT { public int X; public int Y; }
}
"@
[void][WinCapShot]::SetProcessDPIAware()

function Get-PreviewHandles {
    # 只找标题为预览页的窗口；不要用 Get-Process flet 一锅端，否则会误杀用户自己开的 App
    $script:previewHandles = @()
    $callback = [WinCapShot+EnumProc]{
        param($hWnd, $lParam)
        if ([WinCapShot]::IsWindowVisible($hWnd)) {
            $text = New-Object System.Text.StringBuilder 300
            [void][WinCapShot]::GetWindowText($hWnd, $text, 300)
            if ($text.ToString() -eq $script:previewTitle) {
                $script:previewHandles += $hWnd
            }
        }
        return $true
    }
    [void][WinCapShot]::EnumWindows($callback, [IntPtr]::Zero)
    return @($script:previewHandles)
}

$script:previewTitle = $title

# 清掉上次遗留的预览窗口（只有同名窗口，不动用户正在用的 App）
foreach ($handle in @(Get-PreviewHandles)) {
    $windowPid = 0
    [void][WinCapShot]::GetWindowThreadProcessId($handle, [ref]$windowPid)
    if ($windowPid -gt 0) {
        Stop-Process -Id $windowPid -Force -ErrorAction SilentlyContinue
    }
}

$proc = Start-Process -FilePath $pythonw -ArgumentList $previewScript `
    -WorkingDirectory $project -PassThru

# 等预览窗口出现（最多 20 秒）
$target = [IntPtr]::Zero
for ($attempt = 0; $attempt -lt 40 -and $target -eq [IntPtr]::Zero; $attempt++) {
    Start-Sleep -Milliseconds 500
    $handles = @(Get-PreviewHandles)
    if ($handles.Count -gt 0) { $target = $handles[0] }
}

Add-Type -AssemblyName System.Drawing
if ($target -eq [IntPtr]::Zero) {
    Write-Output 'NO_TARGET: 预览窗口没出现（flet.exe 是否被遮挡/崩溃？）'
} else {
    [void][WinCapShot]::ShowWindow($target, 9)
    [void][WinCapShot]::SetForegroundWindow($target)
    Start-Sleep -Milliseconds 1500
    # Win11 的窗口四角是圆角，圆角处透明：直接截窗口矩形会把四个角连同桌面图标一起截进来。
    # 先关掉圆角（DWMWA_WINDOW_CORNER_PREFERENCE=33，DWMWCP_DONOTROUND=1）；Win10 上返回非 0，忽略。
    $corner = 1
    [void][WinCapShot]::DwmSetWindowAttribute($target, 33, [ref]$corner, 4)
    Start-Sleep -Milliseconds 300

    # 取真实窗口边界：GetWindowRect 会把 DWM 的投影阴影也算进来（左/下边缘会带进
    # 桌面图标、任务栏），DWMWA_EXTENDED_FRAME_BOUNDS(9) 不含阴影；失败再退回旧 API。
    $rect = New-Object WinCapShot+RECT
    $frame = New-Object WinCapShot+RECT
    $hr = [WinCapShot]::DwmGetWindowAttribute($target, 9, [ref]$frame, 16)
    if ($hr -eq 0 -and ($frame.Right - $frame.Left) -gt 0 -and ($frame.Bottom - $frame.Top) -gt 0) {
        $rect = $frame
    } else {
        Write-Output "DWM_FALLBACK hr=$hr（退回 GetWindowRect，截图可能带阴影）"
        [void][WinCapShot]::GetWindowRect($target, [ref]$rect)
    }
    # 窗口自身还有 1px 透明边框（DWM 的 resize 边框），内缩 2px 保证四条边全是窗口内容
    $inset = 2
    $rect.Left += $inset
    $rect.Top += $inset
    $rect.Right -= $inset
    $rect.Bottom -= $inset
    $width = $rect.Right - $rect.Left
    $height = $rect.Bottom - $rect.Top
    $bitmap = New-Object System.Drawing.Bitmap $width, $height
    $graphics = [System.Drawing.Graphics]::FromImage($bitmap)
    $graphics.CopyFromScreen($rect.Left, $rect.Top, 0, 0, $bitmap.Size)
    $out = Join-Path $project $outPng
    $bitmap.Save($out, [System.Drawing.Imaging.ImageFormat]::Png)
    $graphics.Dispose()
    $bitmap.Dispose()
    Write-Output "SAVED $out ($width x $height)"
}

# 只关掉本脚本启动的 pythonw 和它对应的预览窗口
if (-not $proc.HasExited) { Stop-Process -Id $proc.Id -Force }
if ($target -ne [IntPtr]::Zero) {
    $windowPid = 0
    [void][WinCapShot]::GetWindowThreadProcessId($target, [ref]$windowPid)
    if ($windowPid -gt 0) {
        Stop-Process -Id $windowPid -Force -ErrorAction SilentlyContinue
    }
}
