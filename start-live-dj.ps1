$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$pyw = "C:\Users\c\AppData\Local\Programs\Python\Python312\pythonw.exe"
Start-Process -FilePath $pyw -ArgumentList @((Join-Path $here "live_dj.py"), "gui") -WorkingDirectory $here
