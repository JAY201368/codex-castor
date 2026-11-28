using System;
using System.ComponentModel;
using System.IO;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Text;

static class WindowsFixture
{
    [StructLayout(LayoutKind.Sequential)]
    struct StartupInfo
    {
        public int Size;
        public IntPtr Reserved, Desktop, Title;
        public uint X, Y, Width, Height, TextColumns, TextRows, FillAttribute, Flags;
        public ushort ShowWindow, ReservedLength;
        public IntPtr ReservedBytes, StandardInput, StandardOutput, StandardError;
    }

    [StructLayout(LayoutKind.Sequential)]
    struct ProcessInformation
    {
        public IntPtr Process, Thread;
        public uint ProcessId, ThreadId;
    }

    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, ExactSpelling = true, SetLastError = true)]
    static extern bool CreateProcessW(string application, StringBuilder command, IntPtr processAttributes,
        IntPtr threadAttributes, bool inheritHandles, uint flags, IntPtr environment, string workingDirectory,
        ref StartupInfo startup, out ProcessInformation process);

    [DllImport("kernel32.dll")]
    static extern IntPtr GetStdHandle(int id);

    [DllImport("kernel32.dll")]
    static extern uint WaitForSingleObject(IntPtr handle, uint timeout);

    [DllImport("kernel32.dll", SetLastError = true)]
    static extern bool GetExitCodeProcess(IntPtr process, out int code);

    [DllImport("kernel32.dll")]
    static extern bool CloseHandle(IntPtr handle);

    static int Main()
    {
        string command = Environment.CommandLine;
        int end = command[0] == '"' ? command.IndexOf('"', 1) + 1 : command.IndexOf(' ');
        string arguments = end < 0 ? "" : command.Substring(end);
        string script = Path.ChangeExtension(Assembly.GetExecutingAssembly().Location, ".py");
        string python = Environment.GetEnvironmentVariable("CASTOR_TEST_PYTHON");
        var childCommand = new StringBuilder("\"" + python + "\" \"" + script + "\"" + arguments);
        var startup = new StartupInfo {
            Size = Marshal.SizeOf(typeof(StartupInfo)), Flags = 0x100,  // STARTF_USESTDHANDLES
            StandardInput = GetStdHandle(-10), StandardOutput = GetStdHandle(-11), StandardError = GetStdHandle(-12)
        };
        ProcessInformation child;
        if (!CreateProcessW(python, childCommand, IntPtr.Zero, IntPtr.Zero, true, 0x08000000, // CREATE_NO_WINDOW
            IntPtr.Zero, null, ref startup, out child))
            throw new Win32Exception(Marshal.GetLastWin32Error());
        CloseHandle(child.Thread);
        try
        {
            WaitForSingleObject(child.Process, 0xffffffff);
            int code;
            if (!GetExitCodeProcess(child.Process, out code))
                throw new Win32Exception(Marshal.GetLastWin32Error());
            return code;
        }
        finally
        {
            CloseHandle(child.Process);
        }
    }
}
