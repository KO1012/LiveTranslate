' LiveTranslate silent launcher (no console window).
'
' Double-click this file to start the app without a flashing/lingering cmd
' window. It uses the venv's pythonw.exe (the windowed Python interpreter,
' which has no attached console). Logs still go to logs\*.log as usual.
'
' Use start.bat instead if you want to watch live console output while
' developing.

Option Explicit

Dim fso, shell, scriptDir, pyw, mainPy

Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")

scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
pyw = scriptDir & "\.venv\Scripts\pythonw.exe"
mainPy = scriptDir & "\main.py"

If Not fso.FileExists(pyw) Then
    MsgBox "Virtual environment not found (.venv\Scripts\pythonw.exe)." & vbCrLf & "Please run install.bat first.", vbCritical, "LiveTranslate"
    WScript.Quit 1
End If

' Run pythonw in the project directory; intWindowStyle=0 (hidden), bWaitOnReturn=False.
shell.CurrentDirectory = scriptDir
shell.Run """" & pyw & """ """ & mainPy & """", 0, False
