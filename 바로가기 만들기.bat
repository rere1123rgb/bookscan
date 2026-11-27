@echo off
setlocal
set "EXE=%~dp0bookscan.exe"
if not exist "%EXE%" (
  echo bookscan.exe 를 찾지 못했습니다. 압축을 푼 bookscan 폴더 안에서 실행하세요.
  pause
  exit /b 1
)
powershell -NoProfile -ExecutionPolicy Bypass -Command "$s = New-Object -ComObject WScript.Shell; foreach ($d in @([Environment]::GetFolderPath('Desktop'), [Environment]::GetFolderPath('Programs'))) { $l = $s.CreateShortcut((Join-Path $d ((-join [char[]](0xCC45,0x20,0xC2A4,0xCE94,0x20,0xB3C4,0xC6B0,0xBBF8)) + '.lnk'))); $l.TargetPath = $env:EXE; $l.WorkingDirectory = Split-Path $env:EXE; $l.Save() }"
echo.
echo 바탕화면과 시작 메뉴에 '책 스캔 도우미' 바로가기를 만들었습니다.
echo (이 폴더를 옮기면 바로가기를 다시 만들어 주세요)
pause