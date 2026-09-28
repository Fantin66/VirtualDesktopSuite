using System;
using System.IO;
using System.Runtime.InteropServices;
using Microsoft.Win32;

// Waits without polling while the suite has physically hidden the taskbar.
// Normal exit signals stop; an unexpected parent exit restores Shell state.
internal static class TrayWatchdog
{
    [StructLayout(LayoutKind.Sequential)]
    private struct Rect
    {
        public int Left, Top, Right, Bottom;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct AppBarData
    {
        public uint Size;
        public IntPtr Window;
        public uint CallbackMessage;
        public uint Edge;
        public Rect Rectangle;
        public IntPtr State;
    }

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern IntPtr OpenProcess(uint access, bool inherit, uint pid);

    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern IntPtr OpenEvent(uint access, bool inherit, string name);

    [DllImport("kernel32.dll")]
    private static extern bool SetEvent(IntPtr handle);

    [DllImport("kernel32.dll")]
    private static extern uint WaitForMultipleObjects(
        uint count, [In] IntPtr[] handles, bool waitAll, uint timeout);

    [DllImport("kernel32.dll")]
    private static extern bool CloseHandle(IntPtr handle);

    [DllImport("user32.dll")]
    private static extern bool IsWindow(IntPtr window);

    [DllImport("user32.dll")]
    private static extern bool IsWindowVisible(IntPtr window);

    [DllImport("user32.dll")]
    private static extern bool ShowWindow(IntPtr window, int command);

    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    private static extern IntPtr FindWindow(string className, string title);

    [DllImport("shell32.dll")]
    private static extern IntPtr SHAppBarMessage(uint message, ref AppBarData data);

    private static int Main(string[] args)
    {
        if (args.Length != 4) return 2;
        uint parentPid;
        if (!uint.TryParse(args[0], out parentPid)) return 2;
        IntPtr parent = OpenProcess(0x00100000, false, parentPid);
        IntPtr ready = OpenEvent(0x0002, false, args[1]);
        IntPtr stop = OpenEvent(0x00100000, false, args[2]);
        if (parent == IntPtr.Zero || ready == IntPtr.Zero || stop == IntPtr.Zero)
        {
            if (parent != IntPtr.Zero) CloseHandle(parent);
            if (ready != IntPtr.Zero) CloseHandle(ready);
            if (stop != IntPtr.Zero) CloseHandle(stop);
            return 2;
        }
        try
        {
            SetEvent(ready);
            uint outcome = WaitForMultipleObjects(
                2, new[] { parent, stop }, false, 0xFFFFFFFF);
            if (outcome != 0) return 0;
            try { Recover(args[3], parentPid); }
            catch { return 1; } // Preserve the marker for next-start recovery.
            return 0;
        }
        finally
        {
            CloseHandle(parent);
            CloseHandle(ready);
            CloseHandle(stop);
        }
    }

    private static void Recover(string marker, uint parentPid)
    {
        string[] parts = File.ReadAllText(marker).Split(
            (char[])null, StringSplitOptions.RemoveEmptyEntries);
        uint markerPid;
        long windowValue;
        if (parts.Length < 3 || !uint.TryParse(parts[0], out markerPid)
            || markerPid != parentPid || !long.TryParse(parts[1], out windowValue))
            return;

        IntPtr tray = new IntPtr(windowValue);
        if (!IsWindow(tray)) tray = FindWindow("Shell_TrayWnd", null);
        if (tray != IntPtr.Zero && !IsWindowVisible(tray)) ShowWindow(tray, 5);
        if (tray == IntPtr.Zero || !IsWindowVisible(tray))
            throw new InvalidOperationException("Taskbar could not be restored");

        bool previousAutoHide = parts[2] == "1";
        using (RegistryKey key = Registry.CurrentUser.OpenSubKey(
            @"Software\Microsoft\Windows\CurrentVersion\Explorer\StuckRects3", true))
        {
            if (key != null)
            {
                byte[] settings = key.GetValue("Settings") as byte[];
                if (settings != null && settings.Length > 8)
                {
                    settings[8] = previousAutoHide
                        ? (byte)(settings[8] | 1) : (byte)(settings[8] & 0xFE);
                    key.SetValue("Settings", settings, RegistryValueKind.Binary);
                }
            }
        }
        AppBarData data = new AppBarData();
        data.Size = (uint)Marshal.SizeOf(typeof(AppBarData));
        data.Window = tray;
        data.State = new IntPtr(previousAutoHide ? 3 : 2);
        SHAppBarMessage(10, ref data);
        File.Delete(marker);
    }
}
