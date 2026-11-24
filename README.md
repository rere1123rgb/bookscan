# bookscan (책 스캔 도우미)

Windows desktop app that turns a book scanned through a network scanner's document feeder into one clean PDF: it scans, straightens and crops every page, makes all pages the same size, drops blank pages, checks page numbers, and saves PDF or PNG.

> The UI is available in Korean only. (화면은 한국어만 지원합니다.)

## Features

- Scans directly from scanners that support eSCL (AirScan / Mopria network scanning), feeder (duplex) or flatbed. No driver needed.
- Detects the paper area of each page, deskews and crops it, and pads smaller pages on the spine side so every page ends up the same size.
- Auto page orientation (ONNX model), blank-page removal, edge cleanup (per page), sharpening.
- Reads page numbers with OCR and reports missing and duplicate pages.
- Rescan selected pages: feed all or part of the stack again; a page is replaced only when the match is certain (undo available).
- Review problem pages one by one, reorder with drag and drop, cut/copy/paste, rotate, exclude.
- Saves PDF (JPEG quality presets or lossless) and/or PNG. Images already on disk can be loaded instead of scanning.

## Download

See [Releases](https://github.com/rere1123rgb/bookscan/releases). Windows 10/11 (64-bit). Unzip and run `bookscan.exe`.

Page detection was developed and tested with one feeder-equipped multifunction printer. Other eSCL scanners should work for scanning, but if a scanner fills the area outside the paper differently, some pages may need their corners adjusted by hand (모서리 조정).

The app talks only to the scanner on your local network. It does not check for updates or send any data.

## Build

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
