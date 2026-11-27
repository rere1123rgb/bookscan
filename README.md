# bookscan (책 스캔 도우미)

직접 자른 책을 복합기로 스캔하면, 기울기·크기·방향·빈 페이지·쪽 번호까지 정리해서 PDF 한 권으로 만들어 주는 윈도우 프로그램입니다.

[English](#english) below.

## 다운로드

[Releases](https://github.com/rere1123rgb/bookscan/releases)에서 `bookscan-*.zip`을 받아 압축을 풀고 `bookscan.exe`를 실행합니다. 설치는 필요 없습니다. 윈도우 10/11(64비트)용입니다.

코드 서명이 없어서 처음 실행할 때 파란 "Windows의 PC 보호" 창이 뜰 수 있습니다. **추가 정보 → 실행**을 누르면 됩니다.

네트워크 스캔(eSCL, AirScan, Mopria)을 지원하는 복합기가 필요하고, 드라이버는 필요 없습니다. 피더 달린 복합기 한 기종으로만 시험해서, 다른 기종에서는 일부 쪽의 모서리를 손으로 조정해야 할 수 있습니다. 이미 스캔해 둔 이미지 파일을 불러와 정리할 수도 있습니다.

## 이런 게 편해요

- **넣고 누르면 정리까지 끝납니다.** 종이 부분만 찾아 기울기를 바로잡고 잘라 내고, 위아래 방향도 알아서 맞춥니다.
- **모든 쪽이 같은 크기가 됩니다.** 가장 큰 쪽에 맞춰 작은 쪽에 여백을 붙입니다.
- **빠진 쪽, 겹친 쪽을 알려 줍니다.** 쪽 번호를 읽어서 문제 있는 쪽만 표시하고, 그 쪽만 모아서 검토할 수 있습니다.
- **다시 스캔해도 제자리에 들어갑니다.** 문제 있는 쪽만 다시 넣으면 원래 자리를 찾아 바꿔 끼웁니다. 애매하면 바꾸지 않습니다.
- **빈 페이지는 알아서 뺍니다.** PDF 품질(무손실까지)이나 PNG 저장도 고를 수 있습니다.

같은 네트워크의 스캐너하고만 통신하고, 업데이트 확인·통계·외부 전송은 없습니다.

---

## English

Windows desktop app that turns a book scanned through a network scanner's document feeder into one clean PDF: it scans, straightens and crops every page, makes all pages the same size, drops blank pages, checks page numbers, and saves PDF or PNG.

> The UI is available in Korean only.

### Features

- Scans directly from scanners that support eSCL (AirScan / Mopria network scanning), feeder (duplex) or flatbed. No driver needed.
- Detects the paper area of each page, deskews and crops it, and pads smaller pages on the spine side so every page ends up the same size.
- Auto page orientation (ONNX model), blank-page removal, edge cleanup (per page), sharpening.
- Reads page numbers with OCR and reports missing and duplicate pages.
- Rescan selected pages: feed all or part of the stack again; a page is replaced only when the match is certain (undo available).
- Review problem pages one by one, reorder with drag and drop, cut/copy/paste, rotate, exclude.
- Saves PDF (JPEG quality presets or lossless) and/or PNG. Images already on disk can be loaded instead of scanning.

### Download

See [Releases](https://github.com/rere1123rgb/bookscan/releases). Windows 10/11 (64-bit). Unzip and run `bookscan.exe`.

Page detection was developed and tested with one feeder-equipped multifunction printer. Other eSCL scanners should work for scanning, but if a scanner fills the area outside the paper differently, some pages may need their corners adjusted by hand (모서리 조정).

The app talks only to the scanner on your local network. It does not check for updates or send any data.

### Build

Requirements: Python 3.10 (64-bit).

```powershell
powershell -ExecutionPolicy Bypass -File build_release.ps1
```

Output: `dist\bookscan-<version>.zip`. To run from source instead:

```powershell
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\pip install --no-deps rapidocr_onnxruntime==1.4.4
.venv\Scripts\python app.py
```

Third-party licenses: [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
