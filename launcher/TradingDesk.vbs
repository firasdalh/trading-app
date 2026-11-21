' Opens the AI Trading Desk launcher window (TradingDesk.ps1) without a console window flashing up.
' The desktop shortcut "AI Trading Desk" and "Start AI Trading Desk.bat" both run this.
Set shell = CreateObject("WScript.Shell")
here = CreateObject("Scripting.FileSystemObject").GetParentFolderName(WScript.ScriptFullName)
shell.Run "powershell.exe -NoProfile -ExecutionPolicy Bypass -STA -WindowStyle Hidden -File """ & here & "\TradingDesk.ps1""", 0, False
