Option Explicit
Dim fs, sh, base, pythonw, entry, code, launch
Set fs = CreateObject("Scripting.FileSystemObject")
Set sh = CreateObject("WScript.Shell")
base = fs.GetParentFolderName(WScript.ScriptFullName)
entry = Quote(fs.BuildPath(base, "room_audio.py"))
pythonw = sh.ExpandEnvironmentStrings("%LOCALAPPDATA%") & "\Programs\Python\Python312\pythonw.exe"
If fs.FileExists(pythonw) Then
    launch = Quote(pythonw) & " -B " & entry
Else
    launch = "pyw.exe -3.12 -B " & entry
End If
sh.CurrentDirectory = base
On Error Resume Next
code = sh.Run(launch, 1, False)
If Err.Number <> 0 Then
    MsgBox "Python 3.12 was not found. See README.md.", 16, "RoomAudio"
    WScript.Quit 1
End If

Function Quote(value)
    Quote = Chr(34) & value & Chr(34)
End Function
