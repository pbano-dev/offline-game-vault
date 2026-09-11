# Generated OGV native runtime. Requires Windows PowerShell 5.1.
[CmdletBinding()]
param([ValidateSet("Play", "Verify", "Recover")][string]$Action = "Verify")
Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$Root = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot "../.."))
$Utf8 = New-Object Text.UTF8Encoding($false)

function Hash-File([string]$Path) {
    # Keep integrity checks independent of PowerShell module discovery. A 5.1
    # process can inherit a PSModulePath from a newer PowerShell host.
    $algorithm = [Security.Cryptography.SHA256]::Create()
    try {
        $stream = [IO.File]::OpenRead($Path)
        try {
            return [BitConverter]::ToString($algorithm.ComputeHash($stream)).Replace("-", "").ToLowerInvariant()
        } finally {
            $stream.Dispose()
        }
    } finally {
        $algorithm.Dispose()
    }
}
function Check-Seal([string]$Path) {
    No-Links $Path
    No-Links ($Path + ".sha256")
    $expected = [IO.File]::ReadAllText($Path + ".sha256").Trim()
    if ($expected -notmatch '^[a-f0-9]{64}$' -or (Hash-File $Path) -ne $expected) {
        throw "Integrity check failed: $([IO.Path]::GetFileName($Path))"
    }
}
function Relative-Path([string]$Value) {
    if ([string]::IsNullOrWhiteSpace($Value) -or $Value.Contains("\") -or $Value.StartsWith("/")) {
        throw "Invalid relative path"
    }
    foreach ($part in $Value.Split("/")) {
        if ($part -eq "" -or $part -eq "." -or $part -eq ".." -or
            $part -match '[<>:"|?*\x00-\x1f]' -or $part -match '[. ]$' -or
            $part -match '^(?i:CON|PRN|AUX|NUL|COM[1-9\u00b9\u00b2\u00b3]|LPT[1-9\u00b9\u00b2\u00b3])(?:\.|$)') {
            throw "Non-portable path component: $part"
        }
    }
    return $Value.Replace("/", "\")
}
function No-Links([string]$Path) {
    $current = [IO.Path]::GetFullPath($Path)
    while ($current) {
        $item = Get-Item -LiteralPath $current -Force -ErrorAction SilentlyContinue
        if ($null -ne $item) {
            if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw "Refusing a reparse point: $current"
            }
        }
        $parent = [IO.Path]::GetDirectoryName($current)
        if ($parent -eq $current) { break }
        $current = $parent
    }
}
function Under([string]$Base, [string]$Relative) {
    $path = [IO.Path]::GetFullPath((Join-Path $Base (Relative-Path $Relative)))
    $prefix = [IO.Path]::GetFullPath($Base).TrimEnd("\") + "\"
    if (-not $path.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Path escaped its root"
    }
    No-Links $path
    return $path
}
function Check-Tree([string]$Path) {
    No-Links $Path
    if (-not (Test-Path -LiteralPath $Path)) { return }
    $item = Get-Item -LiteralPath $Path -Force
    if ($item.PSIsContainer) {
        foreach ($child in @(Get-ChildItem -LiteralPath $Path -Force)) {
            $null = Relative-Path $child.Name
            Check-Tree $child.FullName
        }
    }
}
function Copy-Tree([string]$Source, [string]$Destination) {
    Check-Tree $Source
    No-Links $Destination
    if (Test-Path -LiteralPath $Destination) { throw "Copy destination already exists" }
    if (-not (Test-Path -LiteralPath $Source)) { return }
    $item = Get-Item -LiteralPath $Source -Force
    if ($item.PSIsContainer) {
        $null = [IO.Directory]::CreateDirectory($Destination)
        foreach ($child in @(Get-ChildItem -LiteralPath $Source -Force)) {
            Copy-Tree $child.FullName (Join-Path $Destination $child.Name)
        }
    } else {
        [IO.File]::Copy($Source, $Destination, $false)
        if ((Hash-File $Source) -ne (Hash-File $Destination)) { throw "State copy did not verify" }
    }
}
function Remove-Tree([string]$Path) {
    Check-Tree $Path
    if (Test-Path -LiteralPath $Path) { Remove-Item -LiteralPath $Path -Recurse -Force }
}
function Move-Tree([string]$Source, [string]$Destination) {
    Check-Tree $Source
    No-Links $Destination
    if (Test-Path -LiteralPath $Destination) { throw "Move destination already exists" }
    if ((Get-Item -LiteralPath $Source -Force).PSIsContainer) {
        [IO.Directory]::Move($Source, $Destination)
    } else { [IO.File]::Move($Source, $Destination) }
}
function Write-Json([string]$Path, $Document) {
    No-Links $Path
    $temp = $Path + ".new"
    No-Links $temp
    $bytes = $Utf8.GetBytes(($Document | ConvertTo-Json -Depth 30))
    $stream = New-Object IO.FileStream($temp, [IO.FileMode]::Create, [IO.FileAccess]::Write, [IO.FileShare]::None)
    try { $stream.Write($bytes, 0, $bytes.Length); $stream.Flush($true) } finally { $stream.Dispose() }
    if (Test-Path -LiteralPath $Path) { [IO.File]::Replace($temp, $Path, $null) }
    else { [IO.File]::Move($temp, $Path) }
}
function Fingerprint([string]$Path) {
    Check-Tree $Path
    if (-not (Test-Path -LiteralPath $Path)) { return "missing" }
    if (-not (Get-Item -LiteralPath $Path -Force).PSIsContainer) { return "file:" + (Hash-File $Path) }
    $parts = New-Object 'Collections.Generic.List[string]'
    $parts.Add("directory")
    foreach ($child in @(Get-ChildItem -LiteralPath $Path -Force | Sort-Object Name)) {
        $parts.Add($child.Name + ":" + (Fingerprint $child.FullName))
    }
    $sha = [Security.Cryptography.SHA256]::Create()
    try { return [BitConverter]::ToString($sha.ComputeHash($Utf8.GetBytes(($parts -join "\n")))).Replace("-", "").ToLowerInvariant() }
    finally { $sha.Dispose() }
}
# A job owns the whole child process tree. KILL_ON_JOB_CLOSE makes an abrupt
# launcher exit terminate all descendants before the next recovery can run.
Add-Type -TypeDefinition @'
using System;
using System.ComponentModel;
using System.Collections;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;
public static class OgvNative {
    [DllImport("shell32.dll")] static extern int SHGetKnownFolderPath(ref Guid id, uint flags, IntPtr token, out IntPtr path);
    public static string Folder(string id) {
        Guid guid = new Guid(id); IntPtr p;
        int hr = SHGetKnownFolderPath(ref guid, 0, IntPtr.Zero, out p);
        if (hr != 0) Marshal.ThrowExceptionForHR(hr);
        try { return Marshal.PtrToStringUni(p); } finally { Marshal.FreeCoTaskMem(p); }
    }
    [StructLayout(LayoutKind.Sequential)] struct BasicLimit {
        public long ProcessTime, JobTime; public uint Flags;
        public UIntPtr MinWorking, MaxWorking; public uint ProcessLimit;
        public UIntPtr Affinity; public uint Priority, Scheduling;
    }
    [StructLayout(LayoutKind.Sequential)] struct IoCounters { public ulong A,B,C,D,E,F; }
    [StructLayout(LayoutKind.Sequential)] struct ExtendedLimit {
        public BasicLimit Basic; public IoCounters Io;
        public UIntPtr ProcessMemory, JobMemory, PeakProcessMemory, PeakJobMemory;
    }
    [StructLayout(LayoutKind.Sequential)] struct Accounting {
        public long A,B,C,D; public uint PageFaults, TotalProcesses, ActiveProcesses, TerminatedProcesses;
    }
    [StructLayout(LayoutKind.Sequential, CharSet=CharSet.Unicode)] struct Startup {
        public int Size; public string Reserved, Desktop, Title;
        public int X,Y,XSize,YSize,XChars,YChars,Fill,Flags;
        public short Show, Reserved2; public IntPtr ReservedPtr, StdIn, StdOut, StdErr;
    }
    [StructLayout(LayoutKind.Sequential)] struct ProcessInfo {
        public IntPtr Process, Thread; public uint ProcessId, ThreadId;
    }
    [DllImport("kernel32.dll", CharSet=CharSet.Unicode, SetLastError=true)]
    static extern IntPtr CreateJobObject(IntPtr attr, string name);
    [DllImport("kernel32.dll", SetLastError=true)]
    static extern bool SetInformationJobObject(IntPtr job, int kind, ref ExtendedLimit info, uint size);
    [DllImport("kernel32.dll", SetLastError=true)]
    static extern bool QueryInformationJobObject(IntPtr job, int kind, out Accounting info, uint size, IntPtr length);
    [DllImport("kernel32.dll", CharSet=CharSet.Unicode, SetLastError=true)]
    static extern bool CreateProcess(string app, StringBuilder cmd, IntPtr pa, IntPtr ta, bool inherit,
        uint flags, IntPtr env, string cwd, ref Startup si, out ProcessInfo pi);
    [DllImport("kernel32.dll", SetLastError=true)] static extern bool AssignProcessToJobObject(IntPtr job, IntPtr process);
    [DllImport("kernel32.dll", SetLastError=true)] static extern uint ResumeThread(IntPtr thread);
    [DllImport("kernel32.dll", SetLastError=true)] static extern uint WaitForSingleObject(IntPtr handle, uint ms);
    [DllImport("kernel32.dll", SetLastError=true)] static extern bool GetExitCodeProcess(IntPtr process, out uint code);
    [DllImport("kernel32.dll")] static extern bool TerminateProcess(IntPtr process, uint code);
    [DllImport("kernel32.dll")] static extern bool CloseHandle(IntPtr handle);
    public static string Quote(string value) {
        StringBuilder b = new StringBuilder("\""); int slashes = 0;
        foreach (char c in value) {
            if (c == '\\') { slashes++; continue; }
            if (c == '"') { b.Append('\\', slashes * 2 + 1); b.Append(c); slashes = 0; continue; }
            b.Append('\\', slashes); slashes = 0; b.Append(c);
        }
        b.Append('\\', slashes * 2); b.Append('"'); return b.ToString();
    }
    public static int Run(string exe, string cwd, string[] args, IDictionary overrides) {
        IntPtr job = CreateJobObject(IntPtr.Zero, null);
        if (job == IntPtr.Zero) throw new Win32Exception();
        IntPtr block = IntPtr.Zero; ProcessInfo pi = new ProcessInfo();
        try {
            ExtendedLimit limits = new ExtendedLimit(); limits.Basic.Flags = 0x2000;
            if (!SetInformationJobObject(job, 9, ref limits, (uint)Marshal.SizeOf(limits))) throw new Win32Exception();
            var env = new System.Collections.Generic.SortedDictionary<string,string>(StringComparer.OrdinalIgnoreCase);
            foreach (DictionaryEntry e in Environment.GetEnvironmentVariables()) env[(string)e.Key] = (string)e.Value;
            foreach (DictionaryEntry e in overrides) env[(string)e.Key] = (string)e.Value;
            StringBuilder data = new StringBuilder();
            foreach (var e in env) data.Append(e.Key).Append('=').Append(e.Value).Append('\0');
            data.Append('\0'); block = Marshal.StringToHGlobalUni(data.ToString());
            StringBuilder command = new StringBuilder(Quote(exe));
            foreach (string arg in args) command.Append(' ').Append(Quote(arg));
            Startup si = new Startup(); si.Size = Marshal.SizeOf(si);
            if (!CreateProcess(exe, command, IntPtr.Zero, IntPtr.Zero, false, 0x404, block, cwd, ref si, out pi))
                throw new Win32Exception();
            if (!AssignProcessToJobObject(job, pi.Process)) {
                int error = Marshal.GetLastWin32Error(); TerminateProcess(pi.Process, 1); throw new Win32Exception(error);
            }
            if (ResumeThread(pi.Thread) == 0xffffffff) throw new Win32Exception();
            if (WaitForSingleObject(pi.Process, 0xffffffff) == 0xffffffff) throw new Win32Exception();
            uint result; if (!GetExitCodeProcess(pi.Process, out result)) throw new Win32Exception();
            Accounting accounting;
            do {
                if (!QueryInformationJobObject(job, 1, out accounting, (uint)Marshal.SizeOf(typeof(Accounting)), IntPtr.Zero))
                    throw new Win32Exception();
                if (accounting.ActiveProcesses != 0) Thread.Sleep(100);
            } while (accounting.ActiveProcesses != 0);
            return unchecked((int)result);
        } finally {
            CloseHandle(job);
            if (pi.Thread != IntPtr.Zero) CloseHandle(pi.Thread);
            if (pi.Process != IntPtr.Zero) CloseHandle(pi.Process);
            if (block != IntPtr.Zero) Marshal.FreeHGlobal(block);
        }
    }
}
'@

$FolderIds = @{
    RoamingAppData = "3EB685DB-65F9-4CF6-A03A-E3EF65729F3D"
    LocalAppData = "F1B32785-6FBA-4FCF-9D55-7B8E7F157091"
    LocalAppDataLow = "A520A1A4-1780-4FF6-BD18-167343C5AF16"
    Documents = "FDD39AD0-238F-46AF-ADB4-6C85480369C7"
    SavedGames = "4C5C32FF-BB9D-43B0-B5B4-2D72E54EAAA4"
}
$SessionRoot = Join-Path $Root ".ogv-windows"
$Active = Join-Path $SessionRoot "active.json"
$Journal = $null
$Mutex = $null
$LockHeld = $false
$MaterializationLock = $null
$ExitCode = 1

function Tree-Bytes([string]$Path) {
    Check-Tree $Path
    if (-not (Test-Path -LiteralPath $Path)) { return [long]0 }
    $item = Get-Item -LiteralPath $Path -Force
    if (-not $item.PSIsContainer) { return [long]$item.Length }
    [long]$total = 0
    foreach ($child in @(Get-ChildItem -LiteralPath $Path -Force)) { $total += Tree-Bytes $child.FullName }
    return $total
}
function Check-State-Space {
    $budgets = @{}
    foreach ($state in @($Config.state)) {
        if ($state.folder -eq "Game") { continue }
        $mapping = Resolve-State $state
        [long]$size = Tree-Bytes $mapping.source
        # One host seed plus two verified staging copies on the portable
        # volume. Future game growth cannot be predicted from the baseline.
        foreach ($entry in @(@{path=$mapping.destination; bytes=$size}, @{path=$mapping.source; bytes=2*$size})) {
            $volume = [IO.Path]::GetPathRoot($entry.path)
            if (-not $budgets.ContainsKey($volume)) { $budgets[$volume] = [long](16MB) }
            $budgets[$volume] += $entry.bytes
        }
    }
    foreach ($volume in $budgets.Keys) {
        $drive = New-Object IO.DriveInfo($volume)
        if ($drive.AvailableFreeSpace -lt $budgets[$volume]) { throw "Insufficient space for verified state staging on $volume" }
    }
}
function Save-Journal { Write-Json $Active $script:Journal }
function Resolve-State($State) {
    $source = Under $Root $State.source
    if ($State.folder -eq "Game") {
        $destination = Under (Under $Root $Config.game_root) $State.path
        if ($source -ne $destination) { throw "Game-local state mapping differs" }
    } else {
        if (-not $FolderIds.ContainsKey($State.folder)) { throw "Unknown Windows folder" }
        $destination = Under ([OgvNative]::Folder($FolderIds[$State.folder])) $State.path
    }
    return @{source=$source; destination=$destination}
}
function Complete-Session {
    # Recovery uses only paths derived from the sealed launch contract.
    if ($Journal.phase -eq "running" -or $Journal.phase -eq "capturing") {
        $Journal.phase = "capturing"; Save-Journal
        foreach ($entry in @($Journal.items)) {
            $mapping = Resolve-State $Config.state[$entry.index]
            $after = Join-Path $SessionRoot ($Journal.id + "\after\" + $entry.index)
            if (Test-Path -LiteralPath $after) { Remove-Tree $after }
            $null = [IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($after))
            $fingerprint = Fingerprint $mapping.destination
            Copy-Tree $mapping.destination $after
            if ((Fingerprint $after) -ne $fingerprint -or (Fingerprint $mapping.destination) -ne $fingerprint) {
                throw "State changed during capture; close other applications before recovery"
            }
            $entry.captured_fingerprint = $fingerprint
        }
        $Journal.phase = "captured"; Save-Journal
    }
    if ($Journal.phase -eq "captured") {
        foreach ($entry in @($Journal.items)) {
            $mapping = Resolve-State $Config.state[$entry.index]
            $after = Join-Path $SessionRoot ($Journal.id + "\after\" + $entry.index)
            if ((Fingerprint $after) -ne $entry.captured_fingerprint) { throw "Captured state integrity failed" }
            $source = $mapping.source
            $oldSource = $source + ".ogv-" + $Journal.id + "-before"
            $newSource = $source + ".ogv-" + $Journal.id + "-new"
            No-Links $oldSource; No-Links $newSource
            $null = [IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($source))
            if (Test-Path -LiteralPath $newSource) { Remove-Tree $newSource }
            Copy-Tree $after $newSource
            if (Test-Path -LiteralPath $source) {
                if (-not (Test-Path -LiteralPath $oldSource)) { Move-Tree $source $oldSource }
                else { Remove-Tree $source }
            }
            if (Test-Path -LiteralPath $newSource) { Move-Tree $newSource $source }
            if ((Fingerprint $source) -ne $entry.captured_fingerprint) { throw "Preserved progress did not verify" }
        }
        $Journal.phase = "committed"; Save-Journal
    }
    foreach ($entry in @($Journal.items)) {
        if ($entry.restored) { continue }
        $mapping = Resolve-State $Config.state[$entry.index]
        $target = $mapping.destination
        $before = $target + ".ogv-" + $Journal.id + "-before"
        $seed = $target + ".ogv-" + $Journal.id + "-seed"
        $played = $target + ".ogv-" + $Journal.id + "-played"
        No-Links $before; No-Links $seed; No-Links $played
        if (Test-Path -LiteralPath $before) {
            if ((Fingerprint $before) -ne $entry.original_fingerprint) { throw "Host backup integrity failed" }
            if (Test-Path -LiteralPath $target) {
                if ($Journal.phase -eq "committed" -and (Fingerprint $target) -ne $entry.captured_fingerprint) {
                    throw "Host state changed after capture; recovery evidence retained"
                }
                Move-Tree $target $played
            }
            # Rename avoids a partially deleted host tree when interrupted.
            Move-Tree $before $target
        } elseif (-not $entry.original_present -and $entry.activated -and (Test-Path -LiteralPath $target)) {
            if ($Journal.phase -eq "committed" -and (Fingerprint $target) -ne $entry.captured_fingerprint) {
                throw "Host state changed after capture; recovery evidence retained"
            }
            Move-Tree $target $played
        }
        Remove-Tree $seed
        if ((Fingerprint $target) -ne $entry.original_fingerprint) {
            throw "Host state restoration did not verify; recovery evidence retained"
        }
        $entry.restored = $true; Save-Journal
    }
    $Journal.phase = "complete"; Save-Journal
    $history = Join-Path $SessionRoot ($Journal.id + "\receipt.json")
    $null = [IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($history))
    Write-Json $history $Journal
    # Only discard staging copies after progress and host restoration are
    # verified and the receipt is durable. Interrupted cleanup is repeatable.
    foreach ($entry in @($Journal.items)) {
        $mapping = Resolve-State $Config.state[$entry.index]
        Remove-Tree ($mapping.source + ".ogv-" + $Journal.id + "-before")
        Remove-Tree ($mapping.source + ".ogv-" + $Journal.id + "-new")
        Remove-Tree ($mapping.destination + ".ogv-" + $Journal.id + "-played")
    }
    Remove-Tree (Join-Path $SessionRoot ($Journal.id + "\after"))
    Remove-Item -LiteralPath $Active
    Write-Host "Windows session closed. Progress preserved; previous host state restored."
}

try {
    if ($env:OS -ne "Windows_NT") { throw "This runtime requires native Windows" }
    No-Links $Root
    Check-Seal $PSCommandPath
    $configPath = Join-Path $PSScriptRoot "launch.json"
    Check-Seal $configPath
    $Config = [IO.File]::ReadAllText($configPath) | ConvertFrom-Json
    if ($Config.schema -ne 0 -or $Config.contract -ne "ogv-windows-launch-v1") { throw "Unsupported Windows contract" }
    if ($Config.status -ne "prepared-unverified") { throw ("Windows preparation is blocked: " + ($Config.issues -join "; ")) }
    if ($Action -ne "Recover") {
        if ($Config.architecture -eq "x64" -and -not [Environment]::Is64BitOperatingSystem) {
            throw "This game requires 64-bit Windows"
        }
        $exe = Under $Root $Config.entrypoint
        $cwd = Under $Root $Config.working_directory
        if ((Hash-File $exe) -ne $Config.entrypoint_sha256) { throw "Game executable integrity failed" }
        if (-not (Test-Path -LiteralPath $cwd -PathType Container)) { throw "Working directory is missing" }
        foreach ($state in @($Config.state)) {
            $resolved = Resolve-State $state
            Check-Tree $resolved.source
        }
    }
    Write-Host "Native Windows preparation; this game has not been functionally validated on Windows."
    foreach ($warning in @($Config.warnings)) { Write-Host $warning }
    if ($Action -eq "Verify") {
        $filesPath = Join-Path $PSScriptRoot "files.json"
        Check-Seal $filesPath
        $files = [IO.File]::ReadAllText($filesPath) | ConvertFrom-Json
        if ($files.schema -ne 0 -or @($files.files).Count -eq 0) { throw "Empty or unsupported game integrity manifest" }
        foreach ($file in @($files.files)) {
            $candidate = Under $Root $file.path
            if ((Get-Item -LiteralPath $candidate).Length -ne $file.bytes -or (Hash-File $candidate) -ne $file.sha256) {
                throw "Game file integrity failed: $($file.path)"
            }
        }
        Write-Host "Integrity verified. This does not certify gameplay or installed native prerequisites."
        $ExitCode = 0
    } else {
        $sid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
        $Mutex = New-Object Threading.Mutex($false, ("Global\OGV.NativeState." + $sid))
        try { $LockHeld = $Mutex.WaitOne(0) } catch [Threading.AbandonedMutexException] { $LockHeld = $true }
        if (-not $LockHeld) { throw "Another OGV Windows session is active" }
        No-Links $SessionRoot
        $null = [IO.Directory]::CreateDirectory($SessionRoot)
        $lockPath = Join-Path $SessionRoot "session.lock"
        No-Links $lockPath
        $MaterializationLock = New-Object IO.FileStream($lockPath, [IO.FileMode]::OpenOrCreate, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None)
        $name = [IO.Path]::GetFileNameWithoutExtension($Config.entrypoint)
        if (@(Get-Process -Name $name -ErrorAction SilentlyContinue).Count -gt 0) { throw "A process with this game's name is already running" }
        if (Test-Path -LiteralPath $Active) {
            No-Links $Active
            if ($Action -ne "Recover") { throw "An interrupted session needs RECUPERAR_WINDOWS.bat before playing" }
            $Journal = [IO.File]::ReadAllText($Active) | ConvertFrom-Json
            if ($Journal.schema -ne 0 -or $Journal.sid -ne $sid -or $Journal.machine -ne $env:COMPUTERNAME -or
                $Journal.contract_sha256 -ne (Hash-File $configPath) -or
                $Journal.id -notmatch '^[a-f0-9]{32}$' -or
                $Journal.phase -notin @("preparing", "running", "capturing", "captured", "committed", "complete")) {
                throw "Session belongs to another user, host, contract, or unsupported recovery phase"
            }
            $seen = @{}
            foreach ($entry in @($Journal.items)) {
                if ($entry.index -lt 0 -or $entry.index -ge @($Config.state).Count -or $seen.ContainsKey($entry.index)) { throw "Invalid recovery state index" }
                $seen[$entry.index] = $true
                if ($Config.state[$entry.index].folder -eq "Game") { throw "Invalid external state journal" }
            }
            if ($seen.Count -ne @($Config.state | Where-Object { $_.folder -ne "Game" }).Count) {
                throw "Incomplete recovery state journal"
            }
            Complete-Session
            $ExitCode = 0
        } elseif ($Action -eq "Recover") {
            Write-Host "No interrupted session"; $ExitCode = 0
        } else {
            Check-State-Space
            $items = @()
            for ($i = 0; $i -lt @($Config.state).Count; $i++) {
                if ($Config.state[$i].folder -eq "Game") { continue }
                $mapping = Resolve-State $Config.state[$i]
                $items += @{index=$i; original_present=(Test-Path -LiteralPath $mapping.destination);
                            original_fingerprint=(Fingerprint $mapping.destination); activated=$false;
                            restored=$false; captured_fingerprint=$null}
            }
            $script:Journal = @{schema=0; id=[Guid]::NewGuid().ToString("N"); phase="preparing";
                sid=$sid; machine=$env:COMPUTERNAME; contract_sha256=(Hash-File $configPath);
                items=$items; exit_code=$null}
            Save-Journal
            foreach ($entry in @($Journal.items)) {
                $mapping = Resolve-State $Config.state[$entry.index]
                $target = $mapping.destination
                $seed = $target + ".ogv-" + $Journal.id + "-seed"
                $before = $target + ".ogv-" + $Journal.id + "-before"
                $null = [IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($target))
                Copy-Tree $mapping.source $seed
                if ((Fingerprint $target) -ne $entry.original_fingerprint) { throw "Host state changed while preparing" }
                # Record intent before either rename, allowing recovery from a
                # crash between moving the host state and installing the seed.
                $entry.activated = $true; Save-Journal
                if ($entry.original_present) { Move-Tree $target $before }
                if (Test-Path -LiteralPath $seed) { Move-Tree $seed $target }
            }
            $Journal.phase = "running"; Save-Journal
            $environment = @{}
            foreach ($property in $Config.environment.PSObject.Properties) { $environment[$property.Name] = [string]$property.Value }
            Write-Host "Starting native game. Close the game normally to preserve progress."
            $ExitCode = [OgvNative]::Run($exe, $cwd, [string[]]@($Config.arguments), $environment)
            $Journal.exit_code = $ExitCode; Save-Journal
            Complete-Session
            if ($ExitCode -ne 0) {
                Write-Host ("Game exit code: 0x{0:X8}. Check native dependencies and the game's own logs." -f $ExitCode)
            }
        }
    }
} catch {
    [Console]::Error.WriteLine($_.Exception.Message)
    if ($null -ne $Journal -and $Action -eq "Play" -and $Journal.phase -eq "preparing") {
        try { Complete-Session } catch { [Console]::Error.WriteLine("Recovery required: " + $_.Exception.Message) }
    }
    $ExitCode = 1
} finally {
    if ($null -ne $MaterializationLock) { $MaterializationLock.Dispose() }
    if ($LockHeld) { $Mutex.ReleaseMutex() }
    if ($null -ne $Mutex) { $Mutex.Dispose() }
}
exit $ExitCode
