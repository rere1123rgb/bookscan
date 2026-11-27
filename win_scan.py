"""Windows 스캔 API(Windows.Devices.Scanners, Windows '스캔' 앱과 같은 방식)로 스캔한다.
USB 복합기나 제조사 드라이버만 설치된 PC 용. 피더·양면·평판을 Windows 가 드라이버에 맞게 처리한다.

scanner.py(eSCL) 와 같은 형태의 API: list_devices / capabilities / scan.
(예전 WIA Automation 방식은 일부 드라이버에서 피더 이미지를 넘겨주지 않아 이 방식으로 바꿨다 — 실제 기기에서 확인)

- 이 API 는 한 번의 스캔이 끝나야 파일을 한꺼번에 준다. 그래서 on_page 는 스캔이 끝난 뒤 순서대로 불린다.
- 작업 스레드에서 불러도 된다(호출마다 자체 이벤트 루프를 만든다).
"""
import asyncio
import os
import re
import shutil
import tempfile

from scanner import ScanError

STD_DPI = [1200, 600, 400, 300, 200, 150, 100, 75]

# 드라이버가 돌려주는 대표 오류(HRESULT) → 안내
_MESSAGES = {
    0x80210002: '종이가 걸렸습니다. 복합기 덮개를 열어 걸린 종이를 빼낸 뒤 다시 시도하세요.',
    0x80210003: '피더에 종이가 없습니다. 종이를 넣고 다시 시도하세요.',
    0x80210004: '종이 공급에 문제가 있습니다. 피더의 종이를 가지런히 다시 넣고 시도하세요.',
    0x80210020: '종이가 여러 장 한꺼번에 들어갔습니다. 종이를 털어 다시 넣고 시도하세요.',
    0x80210005: '복합기가 꺼져 있거나 연결되지 않았습니다. 전원과 USB/네트워크 연결을 확인하세요.',
    0x80210015: '복합기를 찾을 수 없습니다. 전원과 연결(USB/네트워크)을 확인하고 다시 시도하세요.',
    0x80210006: '복합기가 다른 작업 중입니다. 잠시 기다렸다가 다시 시도하세요.',
    0x8021000D: '복합기를 다른 프로그램이 쓰고 있습니다. 다른 스캔 프로그램을 닫고 다시 시도하세요.',
    0x80210007: '복합기가 준비 중입니다. 잠시 후 다시 시도하세요.',
    0x80210016: '복합기 덮개(피더 커버)가 열려 있습니다. 닫고 다시 시도하세요.',
    0x80210008: '복합기 화면에 확인할 내용이 있습니다. 복합기를 확인한 뒤 다시 시도하세요.',
    0x8021000A: '복합기와 통신이 끊겼습니다. 연결을 확인하고 다시 시도하세요.',
}


def _hr(e):
    for attr in ('winerror', 'hresult'):
        v = getattr(e, attr, None)
        if isinstance(v, int):
            return v & 0xFFFFFFFF
    if getattr(e, 'args', None) and isinstance(e.args[0], int):
        return e.args[0] & 0xFFFFFFFF
    return 0


def _explain(e, source):
    hr = _hr(e)
    if hr in _MESSAGES:
        return ScanError(_MESSAGES[hr])
    if hr >> 16 == 0x8019 and (hr & 0xFFFF) >= 500:   # 네트워크 스캐너가 HTTP 5xx 로 거절
        return ScanError('복합기가 스캔 요청을 거부했습니다. 복합기 화면에 멈춘 작업(\'스캔 중\')이 있으면 취소하고, '
                         '계속되면 복합기 전원을 껐다 켜세요.')
    first = '피더에 종이가 제대로 놓였는지 확인하고, ' if source == 'Feeder' else ''
    return ScanError(f'스캔 중 오류가 났습니다 (코드 0x{hr:08X}). {first}복합기 상태(종이 걸림·덮개)를 확인한 뒤 '
                     '다시 시도하세요. 계속되면 복합기를 껐다 켜세요.')


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


async def _scanners():
    from winrt.windows.devices.enumeration import DeviceInformation
    from winrt.windows.devices.scanners import ImageScanner
    return list(await DeviceInformation.find_all_async_aqs_filter(ImageScanner.get_device_selector()))


def list_devices():
    """[{'id','name','kind':'wia'}]. 오류 시 []."""
    try:
        return [{'id': d.id, 'name': d.name, 'kind': 'wia'} for d in _run(_scanners())]
    except Exception:
        return []


def _dpis(cfg):
    try:
        lo, hi = cfg.min_resolution.dpi_x, cfg.max_resolution.dpi_x
        vals = [v for v in STD_DPI if lo <= v <= hi]
        return vals or [int(hi)]
    except Exception:
        return [300, 150]


async def _caps(dev_id):
    from winrt.windows.devices.scanners import ImageScanner, ImageScannerScanSource as S
    sc = await ImageScanner.from_id_async(dev_id)
    out = {'duplex': False}
    if sc.is_scan_source_supported(S.FEEDER):
        out['Feeder'] = _dpis(sc.feeder_configuration)
        out['duplex'] = bool(sc.feeder_configuration.can_scan_duplex)
    if sc.is_scan_source_supported(S.FLATBED):
        out['Platen'] = _dpis(sc.flatbed_configuration)
    return out


def capabilities(dev_id):
    """{'Feeder': [dpi...], 'Platen': [dpi...], 'duplex': bool}. 오류 시 {}."""
    try:
        return _run(_caps(dev_id))
    except Exception:
        return {}


async def _scan(dev_id, folder, duplex, dpi, should_stop, source, notice):
    from winrt.windows.devices.scanners import (ImageScanner, ImageScannerColorMode, ImageScannerFormat,
                                                ImageScannerResolution, ImageScannerScanSource as S)
    from winrt.windows.storage import StorageFolder
    sc = await ImageScanner.from_id_async(dev_id)
    feeder = source == 'Feeder'
    src = S.FEEDER if feeder else S.FLATBED
    if not sc.is_scan_source_supported(src):
        raise ScanError('이 스캐너에는 자동 급지 장치(피더)가 없습니다. 스캔 방식을 "평판"으로 바꾸세요.' if feeder
                        else '이 스캐너에는 평판(유리판)이 없습니다. 스캔 방식을 "피더"로 바꾸세요.')
    cfg = sc.feeder_configuration if feeder else sc.flatbed_configuration
    used_duplex = False
    for name, value in (('format', ImageScannerFormat.JPEG), ('color_mode', ImageScannerColorMode.COLOR)):
        try:
            setattr(cfg, name, value)
        except Exception:
            pass
    try:
        opts = _dpis(cfg)
        use = dpi if dpi in opts else min(opts, key=lambda v: abs(v - dpi))
        cfg.desired_resolution = ImageScannerResolution(dpi_x=use, dpi_y=use)
    except Exception:
        pass
    if feeder:
        try:
            cfg.max_number_of_pages = 0          # 전부
        except Exception:
            pass
        if duplex:
            if cfg.can_scan_duplex:
                cfg.duplex = True
                used_duplex = True
            elif notice:
                notice('이 스캐너(드라이버)는 양면 스캔을 지원하지 않아 한 면씩 스캔합니다.')
        else:
            try:
                cfg.duplex = False
            except Exception:
                pass
    target = await StorageFolder.get_folder_from_path_async(folder)
    op = sc.scan_files_to_folder_async(src, target)
    task = asyncio.ensure_future(op)
    while not task.done():
        if should_stop():
            try:
                op.cancel()
            except Exception:
                pass
            try:
                await task
            except BaseException:
                pass
            return used_duplex
        await asyncio.sleep(0.3)
    task.result()   # 오류를 여기서 올린다
    return used_duplex


def scan(dev_id, on_page, duplex=True, dpi=300, should_stop=lambda: False, source='Feeder', notice=None):
    """source: 'Feeder'(피더, 여러 장) / 'Platen'(유리판, 한 장).
    스캔이 끝나면 받은 면마다 on_page(jpeg_bytes, 순번 0부터) 호출. 받은 장수 반환."""
    folder = tempfile.mkdtemp(prefix='bookscan_')
    try:
        try:
            _run(_scan(dev_id, folder, duplex, dpi, should_stop, source, notice))
        except ScanError:
            raise
        except Exception as e:
            if should_stop():
                pass
            else:
                raise _explain(e, source)
        files = sorted((f for f in os.listdir(folder) if os.path.isfile(os.path.join(folder, f))),
                       key=lambda f: (os.path.getctime(os.path.join(folder, f)), _num(f)))
        n = 0
        for f in files:
            with open(os.path.join(folder, f), 'rb') as fh:
                data = fh.read()
            on_page(_to_jpeg(data), n)
            n += 1
        if n == 0 and not should_stop():
            if source == 'Feeder':
                raise ScanError('스캔된 페이지가 없습니다. 피더에 종이가 제대로 놓였는지 확인하고 다시 시도하세요.')
            raise ScanError('스캔된 페이지가 없습니다. 원고를 유리판에 올리고 다시 시도하세요.')
        return n
    finally:
        shutil.rmtree(folder, ignore_errors=True)


def _num(name):
    """'Scan (12).jpg' → 12 (같은 시각에 만든 파일의 순서용)."""
    m = re.search(r'(\d+)\D*$', name)
    return int(m.group(1)) if m else 0


def _to_jpeg(data):
    if data[:2] == b'\xff\xd8':
        return data
    try:
        import cv2
        import numpy as np
        img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
        ok, buf = cv2.imencode('.jpg', img, [cv2.IMWRITE_JPEG_QUALITY, 95])
        if ok:
            return buf.tobytes()
    except Exception:
        pass
    raise ScanError('스캐너가 보낸 이미지를 읽을 수 없습니다. 복합기를 껐다 켠 뒤 다시 시도하세요.')
