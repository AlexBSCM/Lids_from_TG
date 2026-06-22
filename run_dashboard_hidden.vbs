' BanditTour Dashboard Server - Hidden Launcher
Set WshShell = CreateObject("WScript.Shell")
WshShell.CurrentDirectory = "D:\BanditTour\Lids_from_TG"
pythonExe = "C:\Users\AVZ\AppData\Local\Python\pythoncore-3.12-64\python.exe"
WshShell.Run """" & pythonExe & """ -u dashboard_server.py", 0, False