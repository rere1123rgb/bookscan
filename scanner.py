"""복합기에 eSCL(네트워크 스캔 표준)로 직접 스캔을 요청한다. 드라이버·외부 프로그램 불필요."""
import re
import subprocess
import time
import urllib.error
import urllib.request

DEFAULT_URL = ''  # 비어 있으면 Windows 에 등록된 eSCL 스캐너를 자동으로 찾는다

JOB_XML = '''<?xml version="1.0" encoding="UTF-8"?>
<scan:ScanSettings xmlns:scan="http://schemas.hp.com/imaging/escl/2011/05/03" xmlns:pwg="http://www.pwg.org/schemas/2010/12/sm">
  <pwg:Version>2.63</pwg:Version>
  <pwg:ScanRegions pwg:MustHonor="false">
    <pwg:ScanRegion>
      <pwg:ContentRegionUnits>escl:ThreeHundredthsOfInches</pwg:ContentRegionUnits>
      <pwg:XOffset>0</pwg:XOffset>
      <pwg:YOffset>0</pwg:YOffset>
      <pwg:Width>{width}</pwg:Width>
      <pwg:Height>{height}</pwg:Height>
    </pwg:ScanRegion>
  </pwg:ScanRegions>
  <pwg:InputSource>{source}</pwg:InputSource>
  <scan:ColorMode>RGB24</scan:ColorMode>
  <pwg:DocumentFormat>image/jpeg</pwg:DocumentFormat>
  <scan:XResolution>{dpi}</scan:XResolution>
  <scan:YResolution>{dpi}</scan:YResolution>
  {duplex}
</scan:ScanSettings>'''

REJECTED_HELP = ('복합기가 스캔 요청을 거부했습니다.\n'
                 '1) 복합기 화면에 멈춘 작업(\'스캔 중\')이 있으면 취소하세요.\n'
                 '2) 그래도 안 되면 Windows 스캔 앱으로 한 장 스캔한 뒤 다시 시도하세요.\n'
                 '3) 계속 안 되면 복합기 전원을 껐다 켜세요.')


class ScanError(Exception):
    pass


def find_escl_url():
    """Windows 가 찾아 둔 eSCL 스캐너 주소를 읽는다. 없으면 None."""
    ps = ("Get-PnpDevice -PresentOnly | Where-Object { $_.InstanceId -like 'SWD\\ESCL\\*' } | "
          "ForEach-Object { (Get-PnpDeviceProperty -InstanceId $_.InstanceId "
          "-KeyName '{A35996AB-11CF-4935-8B61-A6761081ECDF} 12' -ErrorAction SilentlyContinue).Data }")
    try:
        out = subprocess.run(['powershell', '-NoProfile', '-Command', ps], capture_output=True, text=True,
                             timeout=30, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0)).stdout
    except Exception:
        return None
    urls = re.findall(r'https?://\S+', out)
    return urls[0].rstrip('/') if urls else None


def _get(url, timeout=15):
    return urllib.request.urlopen(url, timeout=timeout).read().decode('utf-8', 'replace')


def status(base):
    """(장치 상태, 피더 상태) 예: ('Idle', 'ScannerAdfLoaded')."""
    xml = _get(base + '/ScannerStatus')
    state = re.search(r'<pwg:State>(\w+)</pwg:State>', xml)
    adf = re.search(r'<scan:AdfState>(\w+)</scan:AdfState>', xml)
    return (state.group(1) if state else '?'), (adf.group(1) if adf else '?')


def capabilities(base):
    """{'Feeder': [해상도...], 'Platen': [...]} — 복합기가 실제로 받아 주는 값."""
    xml = _get(base.rstrip('/') + '/ScannerCapabilities')
    out = {}
    for name, tag in (('Feeder', 'Adf'), ('Platen', 'Platen')):
        m = re.search(rf'<scan:{tag}>(.*?)</scan:{tag}>', xml, re.S)
        if m:
            out[name] = sorted({int(v) for v in re.findall(r'<scan:XResolution>(\d+)', m.group(1))}, reverse=True)
    return out


def scan(base, on_page, duplex=True, dpi=300, should_stop=lambda: False, source='Feeder'):
    """source: 'Feeder'(피더, 여러 장) / 'Platen'(유리판, 한 장).
    피더 스캔. 페이지가 도착할 때마다 on_page(jpeg_bytes, 순번 0부터) 호출. 받은 장수 반환."""
    base = base.rstrip('/')
    try:
        _, adf = status(base)
    except Exception as e:
        raise ScanError(f'복합기에 연결할 수 없습니다: {base}\n({e})')
    if source == 'Feeder' and adf == 'ScannerAdfEmpty':
        raise ScanError('피더에 종이가 없습니다. 종이를 넣고 다시 시도하세요.')
    body = JOB_XML.format(width=2551 if source == 'Feeder' else 2550, height=3508, dpi=dpi, source=source,
                          duplex='<scan:Duplex>true</scan:Duplex>' if duplex and source == 'Feeder' else '').encode()
    req = urllib.request.Request(base + '/ScanJobs', data=body, method='POST',
                                 headers={'Content-Type': 'text/xml'})
    try:
        resp = urllib.request.urlopen(req, timeout=30)
    except urllib.error.HTTPError as e:
        raise ScanError(REJECTED_HELP if e.code >= 500 else f'스캔 요청 실패 (HTTP {e.code})')
    job = resp.headers['Location']
    if not job.startswith('http'):
        job = re.match(r'https?://[^/]+', base).group(0) + job
    n = 0
    while True:
        if should_stop():
            try:
                urllib.request.urlopen(urllib.request.Request(job, method='DELETE'), timeout=10)
            except Exception:
                pass
            break
        try:
            r = urllib.request.urlopen(job.rstrip('/') + '/NextDocument', timeout=180)
        except urllib.error.HTTPError as e:
            if e.code == 503:  # 아직 다음 페이지 준비 중
                time.sleep(0.5)
                continue
            if e.code == 404:  # 더 받을 페이지 없음 = 정상 종료
                break
            raise ScanError(f'스캔 중 오류 (HTTP {e.code}). 복합기 화면에 종이 걸림 등이 있는지 확인하세요.')
        on_page(r.read(), n)
        n += 1
    return n
