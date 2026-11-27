"""복합기에 eSCL(네트워크 스캔 표준, AirScan/Mopria)로 직접 스캔을 요청한다. 드라이버·외부 프로그램 불필요.

스캐너 찾기는 두 가지를 합친다:
- Windows 가 이미 등록해 둔 eSCL 스캐너(설정 > 프린터 및 스캐너)
- 같은 네트워크에서 mDNS(_uscan._tcp / _uscans._tcp)로 광고하는 스캐너 (Windows 등록·드라이버 없어도 됨)
"""
import re
import socket
import ssl
import struct
import subprocess
import time
import urllib.error
import urllib.request

DEFAULT_URL = ''  # 비어 있으면 찾은 스캐너를 쓴다

A4 = (2550, 3508)  # 요청 영역 상한, 300분의 1인치 (레터 폭 8.5in × A4 높이). 복합기 최대 크기가 더 작으면 그에 맞춘다

JOB_XML = '''<?xml version="1.0" encoding="UTF-8"?>
<scan:ScanSettings xmlns:scan="http://schemas.hp.com/imaging/escl/2011/05/03" xmlns:pwg="http://www.pwg.org/schemas/2010/12/sm">
  <pwg:Version>{version}</pwg:Version>
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
  <scan:DocumentFormatExt>image/jpeg</scan:DocumentFormatExt>
  <scan:XResolution>{dpi}</scan:XResolution>
  <scan:YResolution>{dpi}</scan:YResolution>
  {duplex}
</scan:ScanSettings>'''

REJECTED_HELP = ('복합기가 스캔 요청을 거부했습니다.\n'
                 '1) 복합기 화면에 멈춘 작업(\'스캔 중\')이 있으면 취소하세요.\n'
                 '2) 그래도 안 되면 Windows 스캔 앱으로 한 장 스캔한 뒤 다시 시도하세요.\n'
                 '3) 계속 안 되면 복합기 전원을 껐다 켜세요.')
BUSY_HELP = '복합기가 다른 작업 중입니다. 복합기 화면의 작업이 끝나면 다시 시도하세요.'
UNREACHABLE_HELP = ('복합기에 연결할 수 없습니다.\n'
                    '· 복합기가 켜져 있고 절전 모드가 아닌지 확인하세요(화면을 한 번 눌러 깨우기).\n'
                    '· 이 PC와 같은 공유기(와이파이)에 연결돼 있는지 확인하세요.\n'
                    '· 오른쪽 [스캔]의 [찾기]로 복합기를 다시 고르세요.')


class ScanError(Exception):
    pass


# ---------- 통신 ----------
_SSL = ssl.create_default_context()
_SSL.check_hostname = False
_SSL.verify_mode = ssl.CERT_NONE   # 복합기는 자체 서명 인증서를 쓴다


def _open(req, timeout):
    return urllib.request.urlopen(req, timeout=timeout, context=_SSL)


def _get(url, timeout=15):
    return _open(url, timeout).read().decode('utf-8', 'replace')


# ---------- 스캐너 찾기 ----------
def find_escl_url():
    """Windows 가 찾아 둔 eSCL 스캐너 주소를 읽는다. 없으면 None."""
    found = _windows_escl()
    return found[0]['url'] if found else None


def _windows_escl():
    ps = ("Get-PnpDevice -PresentOnly | Where-Object { $_.InstanceId -like 'SWD\\ESCL\\*' } | "
          "ForEach-Object { $_.FriendlyName + '|' + (Get-PnpDeviceProperty -InstanceId $_.InstanceId "
          "-KeyName '{A35996AB-11CF-4935-8B61-A6761081ECDF} 12' -ErrorAction SilentlyContinue).Data }")
    try:
        out = subprocess.run(['powershell', '-NoProfile', '-Command', ps], capture_output=True, text=True,
                             timeout=30, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0)).stdout
    except Exception:
        return []
    res = []
    for line in out.splitlines():
        name, _, rest = line.partition('|')
        m = re.search(r'https?://\S+', rest)
        if m:
            res.append(dict(url=m.group(0).rstrip('/'), name=name.strip() or '네트워크 스캐너', kind='escl'))
    return res


def _dns_name(buf, off):
    """DNS 이름 읽기(압축 포인터 포함). (이름, 다음 위치)."""
    labels, jumped, end = [], False, off
    for _ in range(64):
        n = buf[off]
        if n == 0:
            off += 1
            break
        if n & 0xC0 == 0xC0:
            if not jumped:
                end = off + 2
            off, jumped = ((n & 0x3F) << 8) | buf[off + 1], True
            continue
        labels.append(buf[off + 1:off + 1 + n].decode('utf-8', 'replace'))
        off += 1 + n
    return '.'.join(labels), (end if jumped else off)


def _parse_mdns(buf):
    """응답 패킷 → 레코드 목록 (이름, 타입, 데이터)."""
    _, _, qd, an, ns, ar = struct.unpack('>6H', buf[:12])
    off = 12
    for _ in range(qd):
        _, off = _dns_name(buf, off)
        off += 4
    recs = []
    for _ in range(an + ns + ar):
        name, off = _dns_name(buf, off)
        typ, _, _, ln = struct.unpack('>HHIH', buf[off:off + 10])
        off += 10
        data = buf[off:off + ln]
        if typ == 12:      # PTR
            val = _dns_name(buf, off)[0]
        elif typ == 33:    # SRV
            port = struct.unpack('>H', data[4:6])[0]
            val = (port, _dns_name(buf, off + 6)[0])
        elif typ == 16:    # TXT
            val, i = {}, 0
            while i < len(data):
                s = data[i + 1:i + 1 + data[i]].decode('utf-8', 'replace')
                k, _, v = s.partition('=')
                val[k.lower()] = v
                i += 1 + data[i]
        elif typ == 1 and ln == 4:  # A
            val = socket.inet_ntoa(data)
        else:
            val = None
        recs.append((name.lower(), typ, val))
        off += ln
    return recs


MDNS_SERVICES = ('_uscan._tcp.local', '_uscans._tcp.local')


def _mdns_query():
    q = struct.pack('>6H', 0, 0, len(MDNS_SERVICES), 0, 0, 0)
    for s in MDNS_SERVICES:
        q += b''.join(bytes([len(p)]) + p.encode() for p in s.split('.')) + b'\x00' + struct.pack('>HH', 12, 1)
    return q


def _mdns_collect(recs):
    """(이름, 타입, 값, 보낸 IP) 레코드들 → 스캐너 목록."""
    ptr = [(v, n) for n, t, v, _ in recs if t == 12 and n in MDNS_SERVICES and v]
    srv = {n: v for n, t, v, _ in recs if t == 33}
    txt = {n: v for n, t, v, _ in recs if t == 16}
    host_ip = {n: v for n, t, v, _ in recs if t == 1}
    src_ip = {n: ip for n, t, v, ip in recs}
    res, seen = [], set()
    for inst, svc in ptr:
        inst_l = inst.lower()
        secure = svc.startswith('_uscans')
        port, host = srv.get(inst_l, (443 if secure else 80, ''))
        ip = host_ip.get(host.lower()) or src_ip.get(inst_l)
        if not ip:
            continue
        t = txt.get(inst_l, {})
        rs = (t.get('rs') or 'eSCL').strip('/')
        scheme = 'https' if secure else 'http'
        if (ip, scheme) in seen:
            continue
        seen.add((ip, scheme))
        res.append(dict(url=f'{scheme}://{ip}:{port}/{rs}', name=t.get('ty') or inst.split('._uscan')[0],
                        kind='escl', ip=ip))
    # 같은 기기가 http 와 https 둘 다 광고하면 http 하나만
    http_ips = {r['ip'] for r in res if r['url'].startswith('http:')}
    return [r for r in res if r['url'].startswith('http:') or r['ip'] not in http_ips]


def _mdns_escl(timeout=2.5):
    """같은 네트워크에서 eSCL 스캐너 광고(_uscan/_uscans)를 찾는다."""
    q = _mdns_query()
    recs = []
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 255)
        sock.settimeout(0.3)
        sock.bind(('', 0))
        start = time.time()
        sent = 0
        while time.time() - start < timeout:
            if sent < 3 and time.time() - start >= sent * 0.7:
                sock.sendto(q, ('224.0.0.251', 5353))
                sent += 1
            try:
                data, addr = sock.recvfrom(9000)
            except socket.timeout:
                continue
            try:
                recs += [(n, t, v, addr[0]) for n, t, v in _parse_mdns(data)]
            except Exception:
                continue
        sock.close()
    except OSError:
        return []
    return _mdns_collect(recs)


def discover(timeout=2.5):
    """찾을 수 있는 eSCL 스캐너 전부: [{'url','name','kind':'escl'}]. Windows 등록 + 네트워크 광고."""
    out, seen = [], set()
    for d in _windows_escl() + _mdns_escl(timeout):
        host = re.sub(r'^https?://([^/:]+).*$', r'\1', d['url'])
        if host in seen:
            continue
        seen.add(host)
        out.append(dict(url=d['url'], name=d['name'], kind='escl'))
    return out


# ---------- 상태·기능 ----------
def status(base):
    """(장치 상태, 피더 상태) 예: ('Idle', 'ScannerAdfLoaded')."""
    xml = _get(base + '/ScannerStatus')
    state = re.search(r'<pwg:State>(\w+)</pwg:State>', xml)
    adf = re.search(r'<scan:AdfState>(\w+)</scan:AdfState>', xml)
    return (state.group(1) if state else '?'), (adf.group(1) if adf else '?')


def _resolutions(block):
    vals = {int(v) for v in re.findall(r'<scan:XResolution>(\d+)', block)}
    rng = re.search(r'<scan:XResolutionRange>.*?<scan:Min>(\d+)</scan:Min>.*?<scan:Max>(\d+)</scan:Max>', block, re.S)
    if rng:
        lo, hi = int(rng.group(1)), int(rng.group(2))
        vals |= {v for v in (600, 400, 300, 200, 150, 100, 75) if lo <= v <= hi}
    return sorted(vals, reverse=True)


def capabilities(base):
    """{'Feeder': [해상도...], 'Platen': [...], 'duplex': bool, 'size': {'Feeder': (w, h)}, 'version', 'model'}
    — 복합기가 실제로 받아 주는 값. 크기는 300분의 1인치."""
    xml = _get(base.rstrip('/') + '/ScannerCapabilities')
    out, size = {}, {}
    for name, tag in (('Feeder', 'Adf'), ('Platen', 'Platen')):
        m = re.search(rf'<scan:{tag}>(.*?)</scan:{tag}>', xml, re.S)
        if not m:
            continue
        block = m.group(1)
        out[name] = _resolutions(block)
        mw = re.search(r'<scan:MaxWidth>(\d+)', block)
        mh = re.search(r'<scan:MaxHeight>(\d+)', block)
        if mw and mh:
            size[name] = (int(mw.group(1)), int(mh.group(1)))
        if name == 'Feeder':
            out['duplex'] = bool(re.search(r'AdfDuplexInputCaps|<scan:AdfOption>Duplex', block))
    out['size'] = size
    v = re.search(r'<pwg:Version>([\d.]+)</pwg:Version>', xml)
    out['version'] = v.group(1) if v else '2.0'
    mm = re.search(r'<pwg:MakeAndModel>([^<]+)</pwg:MakeAndModel>', xml)
    if mm:
        out['model'] = mm.group(1).strip()
    return out


# ---------- 스캔 ----------
def _post_job(base, body):
    req = urllib.request.Request(base + '/ScanJobs', data=body, method='POST', headers={'Content-Type': 'text/xml'})
    return _open(req, 30)


def _job_error(code):
    if code == 503:
        return ScanError(BUSY_HELP)
    if code >= 500:
        return ScanError(REJECTED_HELP)
    if code in (400, 409):
        return ScanError('복합기가 이 스캔 설정을 받지 않았습니다. 해상도를 낮추거나 양면을 끄고 다시 시도하세요.'
                         f'\n(HTTP {code})')
    return ScanError(f'스캔 요청이 실패했습니다(HTTP {code}).')


def scan(base, on_page, duplex=True, dpi=300, should_stop=lambda: False, source='Feeder', notice=None):
    """source: 'Feeder'(피더, 여러 장) / 'Platen'(유리판, 한 장).
    페이지가 도착할 때마다 on_page(jpeg_bytes, 순번 0부터) 호출. 받은 장수 반환.
    notice(문장): 설정을 바꿔 진행하는 등 사용자에게 알릴 일이 있을 때."""
    base = base.rstrip('/')
    try:
        _, adf = status(base)
    except Exception:
        raise ScanError(UNREACHABLE_HELP + f'\n(주소: {base})')
    try:
        caps = capabilities(base)
    except Exception:
        caps = {}
    if source == 'Feeder' and 'Platen' in caps and 'Feeder' not in caps:
        raise ScanError('이 복합기에는 자동 급지 장치(피더)가 없습니다. 스캔 방식을 "평판"으로 바꾸세요.')
    if source == 'Feeder' and adf == 'ScannerAdfEmpty':
        raise ScanError('피더에 종이가 없습니다. 종이를 넣고 다시 시도하세요.')
    if caps.get(source) and dpi not in caps[source]:
        dpi = min(caps[source], key=lambda v: abs(v - dpi))   # 지원하는 가장 가까운 해상도
    dup = duplex and source == 'Feeder'
    if dup and caps.get('duplex') is False:
        dup = False
        if notice:
            notice('이 복합기는 양면 스캔을 지원하지 않아 한 면씩 스캔합니다.')
    mw, mh = caps.get('size', {}).get(source, A4)
    width, height = min(A4[0], mw), min(A4[1], mh)

    def body(d):
        return JOB_XML.format(version=caps.get('version', '2.0'), width=width, height=height, dpi=dpi, source=source,
                              duplex='<scan:Duplex>true</scan:Duplex>' if d else '').encode()

    try:
        try:
            resp = _post_job(base, body(dup))
        except urllib.error.HTTPError as e:
            if not (e.code in (400, 409) and dup):
                raise _job_error(e.code)
            dup = False                      # 양면 요청을 거절 → 단면으로 다시
            if notice:
                notice('복합기가 양면 요청을 받지 않아 한 면씩 스캔합니다.')
            try:
                resp = _post_job(base, body(False))
            except urllib.error.HTTPError as e2:
                raise _job_error(e2.code)
    except (urllib.error.URLError, OSError) as e:
        if isinstance(e, urllib.error.HTTPError):
            raise _job_error(e.code)
        raise ScanError(UNREACHABLE_HELP + f'\n(주소: {base})')
    job = resp.headers['Location']
    if not job.startswith('http'):
        job = re.match(r'https?://[^/]+', base).group(0) + job
    n = 0
    while True:
        if should_stop():
            try:
                _open(urllib.request.Request(job, method='DELETE'), 10)
            except Exception:
                pass
            break
        try:
            r = _open(job.rstrip('/') + '/NextDocument', 180)
        except urllib.error.HTTPError as e:
            if e.code == 503:  # 아직 다음 페이지 준비 중
                time.sleep(0.5)
                continue
            if e.code == 404:  # 더 받을 페이지 없음 = 정상 종료
                break
            raise ScanError(f'스캔 중 오류가 났습니다(HTTP {e.code}). 복합기 화면에 종이 걸림 등이 있는지 확인하세요.')
        except (urllib.error.URLError, OSError):
            raise ScanError(f'스캔 중 복합기와 연결이 끊겼습니다. 지금까지 {n}면을 받았습니다.\n'
                            '복합기 상태를 확인한 뒤, 남은 종이만 다시 넣고 스캔하세요.')
        on_page(r.read(), n)
        n += 1
    return n
