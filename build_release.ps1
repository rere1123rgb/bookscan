# Builds dist\bookscan-<version>.zip (portable, no installer).
# Requires Python 3.10 (64-bit). Creates .venv-build next to this script on first run.
param([string]$Version = (Get-Date -Format 'yyyy.MM.dd'))
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

$venv = Join-Path $PSScriptRoot '.venv-build'
$py = Join-Path $venv 'Scripts\python.exe'
if (-not (Test-Path $py)) {
    py -3.10 -m venv $venv
    & $py -m pip install --upgrade pip
    & $py -m pip install -r requirements.txt pyinstaller
    & $py -m pip install --no-deps 'rapidocr_onnxruntime==1.4.4'
}

& $py -m PyInstaller --noconfirm --clean --windowed --name bookscan `
    --add-data 'models;models' `
    --collect-data rapidocr_onnxruntime `
    --hidden-import win32com.client --hidden-import pythoncom --hidden-import pywintypes --hidden-import win32timezone `
    --exclude-module tkinter --exclude-module sympy --exclude-module matplotlib `
    --exclude-module win32ui --exclude-module pythonwin `
    app.py
if ($LASTEXITCODE) { throw 'PyInstaller failed' }
# Not used by the app: OpenCV's video I/O (ffmpeg) and Qt's software OpenGL fallback
Get-ChildItem dist\bookscan\_internal -Recurse -Include 'opencv_videoio_ffmpeg*.dll', 'opengl32sw.dll' | Remove-Item
Remove-Item -Recurse -Force dist\bookscan\_internal\pythonwin -ErrorAction SilentlyContinue
# VC++ runtime next to everything that needs it: onnxruntime/pyclipper load in analysis processes that never import
# PySide6, so a PC without the VC++ redistributable would otherwise fail to load them
Copy-Item dist\bookscan\_internal\PySide6\msvcp140*.dll dist\bookscan\_internal\

Copy-Item README.md, THIRD_PARTY_NOTICES.md, '바로가기 만들기.bat' dist\bookscan\
$zip = "dist\bookscan-$Version.zip"
if (Test-Path $zip) { Remove-Item $zip }
Compress-Archive -Path dist\bookscan -DestinationPath $zip -CompressionLevel Optimal
Get-Item $zip | Select-Object Name, @{n = 'MB'; e = { [math]::Round($_.Length / 1MB, 1) } }
