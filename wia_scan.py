"""Windows 드라이버(WIA)로 스캔한다. USB 복합기나 제조사 드라이버만 설치된 PC 용.

scanner.py(eSCL) 와 같은 형태의 API: list_devices / capabilities / scan.

알려진 한계 (드라이버마다 다름)
- 양면 스캔은 드라이버가 지원해야 한다. 지원 여부는 capabilities()['duplex'] 로 알 수 있다.
  양면을 지원하지 않거나 거부하면 자동으로 단면으로 다시 시도한다(앞면만 들어온다).
- 일부 드라이버는 앞/뒷면을 번갈아(앞,뒤,앞,뒤) 주고, 일부는 한 장씩만 준다.
  이 모듈은 도착한 순서 그대로 on_page 에 넘긴다.
- 피더/유리판별 해상도 목록은 WIA 로 따로 읽을 수 없어 같은 목록을 돌려준다.
- 스캔 중간에 멈추기(should_stop)는 한 페이지 전송이 끝난 뒤에 적용된다.
- 스캔은 작업 스레드에서 돌려도 된다(스레드마다 COM 을 초기화/해제한다).

PyInstaller: pywin32 의 pythoncom, pywintypes, win32com 이 필요하다.
  hiddenimports = ['win32com', 'win32com.client', 'pythoncom', 'pywintypes', 'win32timezone']
  (보통 pyinstaller-hooks-contrib 가 처리하지만 명시해 두면 안전하다.) JPEG 변환용 cv2/numpy 도 포함.
"""
import threading
from contextlib import contextmanager

from scanner import ScanError

WIA_JPEG = '{B96B3CAE-0728-11D3-9D7B-0000F81EF32E}'
WIA_BMP = '{B96B3CAB-0728-11D3-9D7B-0000F81EF32E}'
WIA_PNG = '{B96B3CAF-0728-11D3-9D7B-0000F81EF32E}'

P_HANDLING_CAPS = 3086
P_HANDLING_SELECT = 3088
P_PAGES = 3096
P_PAGE_SIZE = 3097
P_INTENT = 6146
P_XRES = 6147
P_YRES = 6148

FEED, FLAT, DUP = 1, 2, 4
STD_DPI = [1200, 600, 400, 300, 200, 150, 100, 75]

E_PAPER_JAM = 0x80210002
E_PAPER_EMPTY = 0x80210003
E_PAPER_PROBLEM = 0x80210004
E_OFFLINE = 0x80210005
E_BUSY = 0x80210006
E_WARMING_UP = 0x80210007
E_USER_INTERVENTION = 0x80210008
E_COMM = 0x8021000A
E_INVALID_COMMAND = 0x8021000B
E_BAD_HW_SETTING = 0x8021000C
E_LOCKED = 0x8021000D
E_NO_DEVICE = 0x80210015
E_COVER_OPEN = 0x80210016
E_MULTI_FEED = 0x80210020
E_INVALIDARG = 0x80070057
E_NOTIMPL = 0x80004001
DISP_E_EXCEPTION = 0x80020009

# 지원하지 않는 설정값 때문에 난 것으로 보이는 오류
_UNSUPPORTED = {E_INVALIDARG, E_NOTIMPL, E_INVALID_COMMAND, E_BAD_HW_SETTING}

_MESSAGES = {
    E_PAPER_JAM: '종이가 걸렸습니다. 복합기 덮개를 열어 걸린 종이를 빼낸 뒤 다시 시도하세요.',
    E_PAPER_PROBLEM: '종이 공급에 문제가 있습니다. 피더의 종이를 가지런히 다시 넣고 시도하세요.',
    E_MULTI_FEED: '종이가 여러 장 한꺼번에 들어갔습니다. 종이를 바람 넣듯 털어 다시 넣고 시도하세요.',
    E_BUSY: '복합기가 다른 작업 중입니다. 잠시 기다렸다가 다시 시도하세요. 계속되면 복합기 전원을 껐다 켜세요.',
    E_LOCKED: '복합기를 다른 프로그램이 쓰고 있습니다. 다른 스캔 프로그램을 닫고 다시 시도하세요.',
    E_WARMING_UP: '복합기가 준비 중입니다. 잠시 후 다시 시도하세요.',
    E_OFFLINE: '복합기가 꺼져 있거나 연결되지 않았습니다. 전원과 USB/네트워크 연결을 확인하세요.',
    E_NO_DEVICE: '복합기를 찾을 수 없습니다. 전원과 연결(USB/네트워크)을 확인하고 다시 시도하세요.',
    E_COMM: '복합기와 통신이 끊겼습니다. 연결을 확인하고 다시 시도하세요.',
    E_COVER_OPEN: '복합기 덮개(피더 커버)가 열려 있습니다. 닫고 다시 시도하세요.',
    E_USER_INTERVENTION: '복합기 화면에 확인할 내용이 있습니다. 복합기를 확인한 뒤 다시 시도하세요.',
}


@contextmanager
def _com():
    """스레드별 COM 초기화. 이미 다른 방식으로 초기화돼 있으면 그대로 쓴다."""
    import pythoncom
    ok = False
    try:
        pythoncom.CoInitialize()
        # 메인 스레드는 pythoncom 이 이미 COM 을 잡고 있어 중첩 해제하면 깨지므로 해제하지 않는다
        ok = threading.current_thread() is not threading.main_thread()
    except Exception:
        pass
    try:
        yield
    finally:
        import gc
        gc.collect()  # COM 개체를 CoUninitialize 전에 반드시 해제한다
        if ok:
            try:
                pythoncom.CoUninitialize()
            except Exception:
                pass


def _hr(e):
    """com_error 에서 실제 HRESULT(부호 없는 정수)를 꺼낸다."""
    hr = getattr(e, 'hresult', None)
    if hr is None and getattr(e, 'args', None):
        hr = e.args[0]
    if not isinstance(hr, int):
        return 0
    hr &= 0xFFFFFFFF
    if hr == DISP_E_EXCEPTION:
        info = getattr(e, 'excepinfo', None)
        try:
            sc = info[5] & 0xFFFFFFFF
            if sc:
                return sc
        except Exception:
            pass
    return hr


def _is_com_error(e):
    try:
        import pywintypes
        return isinstance(e, pywintypes.com_error)
    except Exception:
        return False


def _explain(e):
    """오류를 사용자용 한국어 ScanError 로 바꾼다."""
    if isinstance(e, ScanError):
        return e
    if _is_com_error(e):
        hr = _hr(e)
        msg = _MESSAGES.get(hr)
        if msg:
            return ScanError(msg)
        return ScanError(f'스캔 중 오류가 났습니다 (코드 0x{hr:08X}). 복합기 상태(종이 걸림·덮개)를 확인하고 '
                         f'다시 시도하세요. 계속되면 복합기를 껐다 켜세요.')
    return ScanError(f'스캔 중 오류가 났습니다: {e}')


# ---------- 속성 도우미 ----------

def _get_prop(obj, pid):
    try:  # 정수는 '순서'로 해석되므로 속성 ID 는 문자열로 넘겨야 한다
        return obj.Properties.Item(str(pid))
    except Exception:
        return None


def _get_val(obj, pid, default=None):
    p = _get_prop(obj, pid)
    if p is None:
        return default
    try:
        return p.Value
    except Exception:
        return default


def _set_prop(obj, pid, value):
    """속성을 쓴다. 안 되면 False (오류는 올리지 않는다)."""
    p = _get_prop(obj, pid)
    if p is None:
        return False
    try:
        p.Value = value
        return True
    except Exception:
        return False


def _dpi_options(prop):
    """해상도 속성의 허용값 목록(큰 값부터)."""
    if prop is None:
        return []
    try:
        st = prop.SubType
        if st == 2:  # 목록
            return sorted({int(v) for v in prop.SubTypeValues}, reverse=True)
        if st == 1:  # 범위
            lo, hi, step = int(prop.SubTypeMin), int(prop.SubTypeMax), int(prop.SubTypeStep) or 1
            vals = [v for v in STD_DPI if lo <= v <= hi and (v - lo) % step == 0]
            return vals or ([hi] if hi else [])
    except Exception:
        pass
    try:
        return [int(prop.Value)]
    except Exception:
        return []


def _nearest(options, dpi):
    return min(options, key=lambda v: (abs(v - dpi), -v)) if options else dpi


# ---------- 연결 ----------

def _manager():
    import win32com.client
    return win32com.client.Dispatch('WIA.DeviceManager')


def _connect(dev_id):
    """(device, item) 반환. 실패하면 ScanError. 호출 스레드에서 COM 이 초기화돼 있어야 한다."""
    try:
        mgr = _manager()
        infos = mgr.DeviceInfos
        info = None
        for i in range(1, infos.Count + 1):
            if infos.Item(i).DeviceID == dev_id:
                info = infos.Item(i)
                break
        if info is None:
            raise ScanError(_MESSAGES[E_NO_DEVICE])
        dev = info.Connect()
        if dev.Items.Count < 1:
            raise ScanError('스캐너에서 스캔할 수 있는 항목을 찾지 못했습니다. 드라이버를 다시 설치해 보세요.')
        return dev, dev.Items.Item(1)
    except ScanError:
        raise
    except Exception as e:
        raise _explain(e)


# ---------- 공개 API ----------

def list_devices():
    """[{'id','name','kind':'wia'}] 스캐너만. 오류 시 []."""
    try:
        with _com():
            return _list()
    except Exception:
        return []


def _list():
    if True:
        if True:
            infos = _manager().DeviceInfos
            out = []
            for i in range(1, infos.Count + 1):
                info = infos.Item(i)
                if info.Type != 1:
                    continue
                out.append({'id': info.DeviceID, 'name': str(_get_val(info, 'Name', info.DeviceID)), 'kind': 'wia'})
            return out


def capabilities(dev_id):
    """{'Feeder': [dpi...], 'Platen': [dpi...], 'duplex': bool}. 오류 시 {}."""
    try:
        with _com():
            return _caps(dev_id)
    except Exception:
        return {}


def _caps(dev_id):
    if True:
        if True:
            dev, item = _connect(dev_id)
            caps = _get_val(dev, P_HANDLING_CAPS, 0) or 0
            dpis = _dpi_options(_get_prop(item, P_XRES))
            out = {'duplex': bool(caps & DUP)}
            if caps & FEED:
                out['Feeder'] = list(dpis)
            if caps & FLAT:
                out['Platen'] = list(dpis)
            if not caps & (FEED | FLAT):  # 능력을 못 읽는 드라이버: 둘 다 있다고 본다
                out['Feeder'] = list(dpis)
                out['Platen'] = list(dpis)
            return out


def _to_jpeg(data):
    """이미지 바이트가 JPEG 이 아니면(BMP/PNG 등) JPEG(품질 95)로 바꾼다."""
    if data[:2] == b'\xff\xd8':
        return data
    try:
        import cv2
        import numpy as np
        img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
        ok, buf = cv2.imencode('.jpg', img, [cv2.IMWRITE_JPEG_QUALITY, 95])
        if not ok:
            raise ValueError('encode')
        return buf.tobytes()
    except Exception:
        raise ScanError('스캐너가 보낸 이미지를 읽을 수 없습니다. 복합기를 껐다 켠 뒤 다시 시도하세요.')


def _image_bytes(img_file):
    return bytes(img_file.FileData.BinaryData)


def _transfer(item):
    """JPEG 로 요청, 드라이버가 거부하면 BMP -> PNG 순으로. 성공한 형식을 기억하지 않는다(단순화)."""
    last = None
    for fmt in (WIA_JPEG, WIA_BMP, WIA_PNG):
        try:
            return item.Transfer(fmt)
        except Exception as e:
            if not _is_com_error(e) or _hr(e) not in _UNSUPPORTED:
                raise
            last = e
    raise last


def _run(dev, item, on_page, duplex, dpi, should_stop, source, notice=None, minimal=False):
    """한 번의 스캔 시도. 받은 장수 반환. 오류는 (원본 예외, 받은 장수) 와 함께 _Attempt 로 올린다."""
    feeder = source == 'Feeder'
    caps = _get_val(dev, P_HANDLING_CAPS, None)
    if feeder:
        mode = FEED
        if duplex and (caps is None or caps & DUP):
            mode |= DUP
        if not _set_prop(dev, P_HANDLING_SELECT, mode) and mode & DUP:
            _set_prop(dev, P_HANDLING_SELECT, FEED)  # 양면을 거부하면 단면
            mode = FEED
        if duplex and not mode & DUP and notice:
            notice('이 스캐너(드라이버)는 양면 스캔을 받지 않아 한 면씩 스캔합니다.')
        if not minimal:   # 일부 드라이버는 이 값들을 받으면 피더 스캔을 거부한다 → minimal 로 다시
            _set_prop(dev, P_PAGES, 0)
            _set_prop(dev, P_PAGE_SIZE, 0)  # A4. 지원하지 않는 드라이버는 무시된다
    else:
        _set_prop(dev, P_HANDLING_SELECT, FLAT)
        _set_prop(dev, P_PAGES, 1)

    _set_prop(item, P_INTENT, 1)  # 컬러
    opts = _dpi_options(_get_prop(item, P_XRES))
    use = _nearest(opts, dpi) if opts and dpi not in opts else dpi
    if not (_set_prop(item, P_XRES, use) and _set_prop(item, P_YRES, use)) and opts:
        use = _nearest(opts, dpi)
        _set_prop(item, P_XRES, use)
        _set_prop(item, P_YRES, use)

    n = 0
    while True:
        if should_stop():
            break
        try:
            img = _transfer(item)
        except Exception as e:
            if _is_com_error(e) and _hr(e) == E_PAPER_EMPTY:
                break  # 피더 끝(정상)
            raise _Attempt(e, n)
        on_page(_to_jpeg(_image_bytes(img)), n)
        n += 1
        if not feeder:
            break
    return n


class _Attempt(Exception):
    def __init__(self, err, n):
        super().__init__(str(err))
        self.err, self.n = err, n


def scan(dev_id, on_page, duplex=True, dpi=300, should_stop=lambda: False, source='Feeder', notice=None):
    """source: 'Feeder'(피더, 여러 장) / 'Platen'(유리판, 한 장).
    페이지가 도착할 때마다 on_page(jpeg_bytes, 순번 0부터) 호출. 받은 장수 반환.
    notice(문장): 양면을 못 해 단면으로 바꾸는 등 사용자에게 알릴 일이 있을 때."""
    with _com():
        return _scan(dev_id, on_page, duplex, dpi, should_stop, source, notice)


def _scan(dev_id, on_page, duplex, dpi, should_stop, source, notice=None):
    if True:
        dev, item = _connect(dev_id)
        want_duplex = bool(duplex and source == 'Feeder')
        # (양면, 최소 설정) 순서로 시도: 양면 → 단면 → 단면+최소 설정
        tries = ([(True, False)] if want_duplex else []) + [(False, False)]
        if source == 'Feeder':
            tries.append((False, True))
        total = 0
        for k, (dup, minimal) in enumerate(tries):
            try:
                n = _run(dev, item, lambda b, i: on_page(b, total + i), dup, dpi, should_stop, source,
                         notice if k == 0 else None, minimal)
            except _Attempt as a:
                total += a.n
                unsupported = _is_com_error(a.err) and _hr(a.err) in _UNSUPPORTED
                if unsupported and total == 0 and k + 1 < len(tries):
                    if dup and notice:
                        notice('이 스캐너(드라이버)가 양면 요청을 받지 않아 한 면씩 스캔합니다.')
                    continue  # 양면/추가 설정이 문제였을 수 있으니 더 단순하게 재시도
                if unsupported and total == 0:
                    # 드라이버에 따라 피더가 비었을 때도 '설정 오류'로 답한다(실제 기기에서 확인)
                    first = ('스캔을 시작하지 못했습니다. 피더에 종이가 제대로 놓였는지 먼저 확인하세요.\n'
                             if source == 'Feeder' else '스캔을 시작하지 못했습니다.\n')
                    raise ScanError(first + '종이가 있는데도 같은 메시지가 나오면 해상도를 낮추거나 '
                                    '양면을 끄고 다시 시도하세요.')
                if total > 0 and _is_com_error(a.err) and _hr(a.err) in (E_PAPER_JAM, E_COVER_OPEN, E_MULTI_FEED):
                    raise _explain(a.err)
                raise _explain(a.err)
            total += n
            break
        if total == 0 and not should_stop():
            if source == 'Feeder':
                raise ScanError('피더에 종이가 없습니다. 종이를 넣고 다시 시도하세요.')
            raise ScanError('스캔된 페이지가 없습니다. 원고를 유리판에 올리고 다시 시도하세요.')
        return total
