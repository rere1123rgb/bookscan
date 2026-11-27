# bookscan (책 스캔 도우미)

직접 자른 책을 복합기로 스캔하면, 기울기·크기·방향·빈 페이지·쪽 번호까지 정리해서 PDF 한 권으로 만들어 주는 윈도우 프로그램입니다.

> **시험판(베타)입니다.** 개인이 만들어 쓰던 도구를 공개한 것이고, 피더(양면) 달린 복합기 한 기종으로만 실물 시험을 했습니다. 다른 스캐너나 문서에서는 예상과 다르게 동작할 수 있습니다. 문제가 있으면 [Issues](https://github.com/rere1123rgb/bookscan/issues)에 알려 주세요(스캔 원본 이미지나 화면을 함께 올려 주시면 고치기 쉽습니다. 개인정보는 가려 주세요).

[English](#english) below.

## 다운로드

[Releases](https://github.com/rere1123rgb/bookscan/releases)에서 `bookscan-*.zip`을 받아 압축을 풀고 `bookscan.exe`를 실행합니다. 설치는 필요 없습니다. 윈도우 10/11(64비트)용입니다. 같은 폴더의 `바로가기 만들기.bat`을 실행하면 바탕화면과 시작 메뉴에 바로가기가 생깁니다.

코드 서명이 없어서 처음 실행할 때 파란 "Windows의 PC 보호" 창이 뜰 수 있습니다. **추가 정보 → 실행**을 누르면 됩니다.

## 쓸 수 있는 스캐너

프로그램을 켜면 스캐너를 자동으로 찾아 오른쪽 [스캔]의 목록에 넣어 둡니다.

- **공유기(와이파이·랜선)로 연결한 복합기:** 드라이버 없이 바로 됩니다. 요즘 복합기 대부분이 지원하는 네트워크 스캔(eSCL·AirScan·Mopria)을 씁니다.
- **USB로 연결한 복합기, 또는 제조사 드라이버만 설치한 경우:** Windows 드라이버(WIA)를 통해 스캔합니다. 양면 스캔 지원은 드라이버마다 다르고, 안 되면 한 면씩 스캔합니다.
- **목록에 안 보이면:** 복합기를 깨운 뒤 [찾기]를 누르거나, 목록의 "네트워크 주소 직접 입력…"에 복합기 IP를 넣으세요. 다른 프로그램으로 스캔해 둔 이미지는 [파일 불러오기]로 정리할 수 있습니다.

피더 달린 복합기 한 기종으로만 실물 시험을 했습니다. 다른 기종에서는 일부 쪽의 모서리를 손으로 조정([모서리 조정])해야 할 수 있습니다.

## 알려진 한계

- 흰 배경판 위의 흰 종이는 종이 끝이 화면에 보이지 않아, 인쇄된 내용을 다 감싸는 선으로 종이 영역을 정합니다. 그래서 몇몇 쪽은 내용이 한쪽으로 약간 치우칠 수 있습니다(내용이 잘리지는 않습니다). 양면 스캔이면 뒷면 정보로 바로잡습니다.
- 테두리가 인쇄된 문서는 그 테두리를 종이 끝으로 잡을 수 있습니다. 이런 쪽은 "확인 필요"(빨간 테두리)로 표시되니 [모서리 조정]으로 고쳐 주세요.
- 한 장만 피더에 넣으면 기종에 따라 종이가 걸릴 수 있습니다. 걸리면 앱이 알려 줍니다.

## 이런 게 편해요

- **넣고 누르면 정리까지 끝납니다.** 종이 부분만 찾아 기울기를 바로잡고 잘라 내고, 위아래 방향도 알아서 맞춥니다.
- **모든 쪽이 같은 크기가 됩니다.** 가장 큰 쪽에 맞춰 작은 쪽에 여백을 붙입니다.
- **빠진 쪽, 겹친 쪽을 알려 줍니다.** 쪽 번호를 읽어서 문제 있는 쪽만 표시하고, 그 쪽만 모아서 검토할 수 있습니다.
- **다시 스캔해도 제자리에 들어갑니다.** 문제 있는 쪽만 다시 넣으면 원래 자리를 찾아 바꿔 끼웁니다. 애매하면 바꾸지 않습니다.
- **빈 페이지는 알아서 뺍니다.** PDF 품질(무손실까지)이나 PNG 저장도 고를 수 있습니다.

스캐너하고만 통신합니다(같은 네트워크의 스캐너 찾기 포함). 업데이트 확인·통계·외부 전송은 없습니다.

---

## English

Windows desktop app that turns a book scanned through a network scanner's document feeder into one clean PDF: it scans, straightens and crops every page, makes all pages the same size, drops blank pages, checks page numbers, and saves PDF or PNG.

> The UI is available in Korean only.

> **Beta.** A personal tool made public. Tested on real hardware with one scanner (a multifunction printer with a duplex feeder) only; other scanners and documents may behave differently. Please report problems in [Issues](https://github.com/rere1123rgb/bookscan/issues), ideally with the raw scan image (blur any personal information).

### Features

- Scans from network scanners that support eSCL (AirScan / Mopria) with no driver, found automatically (Windows-registered devices and mDNS), or from any scanner with a Windows WIA driver (USB or vendor driver). Feeder (duplex where supported) or flatbed.
- Detects the paper area of each page, deskews and crops it, and pads smaller pages on the spine side so every page ends up the same size.
- Auto page orientation (ONNX model), blank-page removal, edge cleanup (per page), sharpening.
- Reads page numbers with OCR and reports missing and duplicate pages.
- Rescan selected pages: feed all or part of the stack again; a page is replaced only when the match is certain (undo available).
- Review problem pages one by one, reorder with drag and drop, cut/copy/paste, rotate, exclude.
- Saves PDF (JPEG quality presets or lossless) and/or PNG. Images already on disk can be loaded instead of scanning.

### Download

See [Releases](https://github.com/rere1123rgb/bookscan/releases). Windows 10/11 (64-bit). Unzip and run `bookscan.exe` (`바로가기 만들기.bat` adds desktop and Start menu shortcuts).

Page detection was developed and tested with one feeder-equipped multifunction printer. Other scanners should work for scanning, but if a scanner fills the area outside the paper differently, some pages may need their corners adjusted by hand (모서리 조정).

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
