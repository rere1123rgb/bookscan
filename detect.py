"""종이 영역 감지.

피더 스캔은 배경(복합기 안쪽 흰 판)과 흰 종이가 둘 다 255 로 날아가므로 색 차이로는
종이를 구분할 수 없다. 대신 종이 가장자리에 생기는 얇은 그림자 선을 변마다 찾아
직선을 맞추고, 네 직선의 교점을 종이 모서리로 쓴다.

- 피더는 종이를 위쪽에 붙여 넣으므로 윗변은 이미지 맨 위 몇 mm 안에서만 찾는다.
  (넓게 찾으면 페이지 안의 테두리 박스 선을 종이 끝으로 착각한다.)
- 회색·색지처럼 배경보다 확실히 어두운 종이는 밝기 계단으로 먼저 찾는다.
- 감지는 150dpi 상당으로 줄여서 하고, 결과 좌표만 원본 해상도로 되돌린다.
"""
import cv2
import numpy as np

WORK_DPI = 150
# 감지 방식이 바뀌면 올린다. 저장된 작업을 다시 열 때 이 값이 다르면 다시 감지한다.
# 2: 뒷면(하늘색 배경판)·색지용 색 기반 감지 추가
# 3: 빈 페이지 판정 기준 변경(색지 무늬 페이지가 빈 페이지로 빠지던 문제) — 내용 비율을 다시 재야 함
# 4: 두 방식이 같은 가장자리를 찾으면 더 안쪽 선 사용(뒷면 배경판 띠 잔여 문제)
# 5·6: 기울기는 좌우(원래 재단면) 기준, 위·아래 변은 수직(2° 이내)일 때만 인정하고 가운데 값 사용
# 7: 변마다 신뢰도도 돌려준다(확인 표시 이유 안내용)
# 8: (분석 단계) 쪽 번호 후보 읽기 추가 — 다시 분석해야 채워진다
# 9: 종이가 스캔 영역을 넘거나 꽉 채우는 경우(A4 등)·말이 안 되는 모양 처리
VERSION = 9


def _edge_in_profile(p):
    """바깥→안쪽 방향 1차원 밝기 프로파일에서 종이 가장자리 위치. 못 찾으면 None."""
    p = p.astype(float)
    n = len(p)
    # 배경보다 확실히 어두운 종이(회색 표지 등): 밝은 배경에서 어두운 평탄 구간으로 넘어가는 계단
    for i in range(n - 20):
        if p[i] > 240 and np.median(p[i + 3:i + 20]) < 225 and p[i + 3:i + 20].std() < 12:
            return i + 1 + int(np.argmin(np.diff(p[i:i + 5])))
    # 흰 종이: 가장 바깥쪽 그림자 골 다음의 가장 가파른 상승, 그 안쪽이 평탄해야 종이로 인정
    for i in range(1, n - 12):
        ahead = p[i + 1:i + 12].max()
        if p[i] <= p[i - 1] and p[i] <= p[i + 1] and ahead - p[i] >= 6:
            j = i + int(np.argmax(np.diff(p[i:i + 8])))
            run = p[j + 2:j + 20]
            if len(run) and run.std() < 4:
                return j + 1
    # (예전엔 여기서 '흰색이 아닌 첫 픽셀'을 돌려줬는데, 뒷면처럼 배경 자체가 흰색이 아니면
    #  이미지 테두리를 종이 끝으로 잡아 버렸다. 그런 경우는 _color_edge_in_profile 이 맡는다.)
    return None


def _color_edge_in_profile(p, thr=3.0, run=10):
    """바깥→안쪽 Lab 프로파일(n×3)에서 배경색이 끝나고 다른 색이 안정적으로 이어지는 지점.
    뒷면 센서의 배경판은 연한 하늘색이라 밝기는 종이와 비슷하고 색만 조금 다르다
    (Lab 로 3~4 차이). 픽셀 하나로는 잡음에 묻히지만 띠 중간값끼리 비교하면 갈린다."""
    n = len(p)
    if n < run + 6:
        return None
    start = 0
    # 드라이버가 채운 순백(255) 구간은 배경이 아니라 '종이 밖 빈칸'이라 건너뛴다
    while start < n - run - 6 and p[start, 0] >= 254.5 and abs(p[start, 1] - 128) < 1 and abs(p[start, 2] - 128) < 1:
        start += 1
    ref = np.median(p[start:start + 4], 0)
    d = np.linalg.norm(p - ref, axis=1)
    far = d > thr
    for i in range(start + 4, n - run):
        if far[i:i + run].all():
            return i
    return None


def _fit_line(pts):
    """RANSAC 으로 이상치를 걸러낸 뒤 직선 맞춤. (vx, vy, x0, y0), 인라이어 비율."""
    pts = np.array(pts, np.float32)
    if len(pts) < 5:
        return None, 0.0
    rng = np.random.default_rng(0)
    best = None
    for _ in range(200):
        a, b = pts[rng.choice(len(pts), 2, replace=False)]
        d = b - a
        length = np.hypot(*d)
        if length < 1:
            continue
        nrm = np.array([-d[1], d[0]]) / length
        inl = np.abs((pts - a) @ nrm) < 3
        if best is None or inl.sum() > best.sum():
            best = inl
    if best is None:
        return None, 0.0
    line = tuple(float(v) for v in cv2.fitLine(pts[best], cv2.DIST_HUBER, 0, .01, .01).ravel())
    return line, float(best.mean())


def _dist_to_center(line, center, side):
    """변 방향으로 잰 페이지 중심까지 거리(작을수록 안쪽)."""
    vx, vy, x0, y0 = line
    if side in 'LR':   # 세로선: 중심 높이에서의 x
        x = x0 + (center[1] - y0) / vy * vx if abs(vy) > 1e-6 else x0
        return abs(x - center[0])
    y = y0 + (center[0] - x0) / vx * vy if abs(vx) > 1e-6 else y0
    return abs(y - center[1])


def _intersect(l1, l2):
    vx1, vy1, x1, y1 = l1
    vx2, vy2, x2, y2 = l2
    t = np.linalg.solve(np.array([[vx1, -vx2], [vy1, -vy2]]), np.array([x2 - x1, y2 - y1]))[0]
    return x1 + t * vx1, y1 + t * vy1


def detect_page(img, dpi, feeder=True):
    """img: BGR 원본. 반환: (모서리 4점 [좌상, 우상, 우하, 좌하] 원본 좌표, 신뢰도 0~1, 윗변을 찾았는지,
    변마다 신뢰도 {L,R,T,B})."""
    s = WORK_DPI / dpi
    small = cv2.resize(img, None, fx=s, fy=s, interpolation=cv2.INTER_AREA)
    g = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
    h, w = g.shape
    # 종이가 끝난 뒤 드라이버가 흰색으로 채운 아래쪽은 잘라내고 본다
    rows = np.where((g < 254).mean(1) > 0.02)[0]
    if len(rows) == 0:
        return ([[0, 0], [img.shape[1] - 1, 0], [img.shape[1] - 1, img.shape[0] - 1], [0, img.shape[0] - 1]], 0.0, True,
                dict(L=0.0, R=0.0, T=0.0, B=0.0))
    H = int(rows.max()) + 1
    if H < 90 / 25.4 * WORK_DPI:
        # 책 한 쪽이 9cm 보다 짧을 리 없다: 흰 종이 위 작은 점 몇 개만 위쪽에 있는 경우라
        # 그 아래도 '빈칸'이 아니라 종이다(예전엔 높이 몇 mm 짜리 영역을 잡고 분석이 멈췄다)
        H = h
    g = cv2.GaussianBlur(g[:H], (3, 3), 0)
    band_x, band_y, band_top = int(w * .12), int(H * .12), max(int(H * .03), 25)
    if not feeder:
        # 평판: 책 한 쪽(약 148×200mm)을 A4 유리판 아무 데나 놓으므로 가장자리가 안쪽 깊이 있을 수 있다.
        # 바깥에서 안으로 처음 만나는 변을 찾으니 범위를 넓혀도 페이지 안쪽 선을 잡을 일은 적다.
        band_x, band_y = int(w * .6), int(H * .6)
        band_top = band_y

    lab = cv2.cvtColor(cv2.GaussianBlur(small[:H], (3, 3), 0), cv2.COLOR_BGR2LAB).astype(np.float32)

    # 변마다 두 방식으로 점을 모은다: 밝기(그림자 선·어두운 종이) / 색(배경색과 다른 지점)
    shade = {k: [] for k in 'LRTB'}
    color = {k: [] for k in 'LRTB'}

    def collect(prof_g, prof_lab, band, store_pt):
        for pts, fn, prof in ((shade, _edge_in_profile, prof_g), (color, _color_edge_in_profile, prof_lab)):
            e = fn(prof[:band])
            if e is not None:
                store_pt(pts, e)

    for y in range(int(H * .1), int(H * .9), 8):
        pg, pl = np.median(g[y:y + 8], 0), np.median(lab[y:y + 8], 0)
        collect(pg, pl, band_x, lambda pts, e: pts['L'].append((e, y + 4)))
        collect(pg[::-1], pl[::-1], band_x, lambda pts, e: pts['R'].append((w - 1 - e, y + 4)))
    for x in range(int(w * .1), int(w * .9), 8):
        pg, pl = np.median(g[:, x:x + 8], 1), np.median(lab[:, x:x + 8], 1)
        collect(pg, pl, band_top, lambda pts, e: pts['T'].append((x + 4, e)))
        collect(pg[::-1], pl[::-1], band_y, lambda pts, e: pts['B'].append((x + 4, H - 1 - e)))

    fallback = {'L': (0., 1., 0., 0.), 'R': (0., 1., w - 1., 0.),
                'T': (1., 0., 0., 0.), 'B': (1., 0., 0., H - 1.)}
    lines, conf = {}, {}
    center = (w / 2, H / 2)
    for k in 'LRTB':
        # 두 방식 중 점들이 더 한 직선에 잘 모이는 쪽을 쓴다.
        # 단 둘 다 잘 맞고 서로 4mm 안쪽으로 가까우면 같은 가장자리를 본 것이니 '더 안쪽' 선을 쓴다:
        # 뒷면은 하늘색 배경판 바깥쪽 그림자 선이 먼저 잡혀 배경 띠가 1~2mm 남는 일이 있었다.
        cands = [(*_fit_line(pts), len(pts)) for pts in (shade[k], color[k])]
        cands = [c for c in cands if c[0] is not None]
        line, ratio, npts = max(cands, key=lambda c: c[1]) if cands else (None, 0.0, 0)
        good = [c for c in cands if c[1] >= 0.6]
        if len(good) == 2:
            d = [_dist_to_center(c[0], center, k) for c in good]
            if abs(d[0] - d[1]) < 4 / 25.4 * WORK_DPI:
                line, ratio, npts = good[int(np.argmin(d))]
        if line is None or ratio < 0.3:
            line, ratio = fallback[k], 0.0
        lines[k], conf[k] = line, ratio
    # 기울기의 기준은 왼쪽·오른쪽 변(스캔 방향과 나란한 변 = 책의 원래 재단면, 길이가 일정하다).
    # 위·아래 변 중 하나는 직접 자른 쪽이라 비스듬할 수 있어 기울기 계산에 쓰지 않고,
    # 잘라낼 위치만 정한다: 비스듬하면 종이 안쪽으로 맞춰 배경이 들어오지 않게 한다.
    # (예전엔 '종이는 직사각형'이라 보고 윗변을 아랫변과 평행하게 그었다가, 손 재단 선이
    #  비스듬한 페이지에서 윗변까지 기울어져 배경 띠가 남았다)
    dirs = []
    for k in 'LR':
        vx, vy = lines[k][0], lines[k][1]
        if vy < 0:
            vx, vy = -vx, -vy
        dirs.append((np.array([vx, vy]), max(conf[k], 1e-3)))
    d = sum(v * c for v, c in dirs)
    d /= np.linalg.norm(d)                       # 아래로 향하는 세로 방향
    n = np.array([d[1], -d[0]])                  # 오른쪽으로 향하는 가로 방향
    across = (1., 0., 0., 0.)                    # 이미지 0행

    def along(p):
        return float(np.dot(p, d))

    def side_pos(k):
        """세로변의 가로 위치(중심 높이에서)."""
        p = _intersect(lines[k], (1., 0., 0., H / 2))
        return float(np.dot(p, n))

    x_left, x_right = side_pos('L'), side_pos('R')
    def tilt(k):
        """위·아래 변이 좌우 변에 수직인 방향에서 틀어진 각도."""
        vx, vy = lines[k][0], lines[k][1]
        return abs((np.degrees(np.arctan2(vy, vx) - np.arctan2(n[1], n[0])) + 90) % 180 - 90)

    def mid(k):
        """위·아래 변의 위치: 좌우 변과 만나는 두 점의 가운데."""
        return (along(_intersect(lines[k], lines['L'])) + along(_intersect(lines[k], lines['R']))) / 2

    # 종이는 직사각형이므로 위·아래 변은 좌우 변에 거의 수직이어야 한다(2° 넘게 틀어지면 잘못 잡은 선)
    top_ok = conf['T'] >= 0.3 and tilt('T') < 2
    if top_ok:
        t = mid('T')
    else:
        # 윗변(종이가 들어오는 쪽)은 앞면에선 흰 배경판 위 흰 종이라 안 보인다. 일단 0행까지로 두고
        # (top_ok=False), process.effective_quad 가 아랫변에서 표준 세로 길이로 다시 정한다.
        t = min(along(_intersect(lines['L'], across)), along(_intersect(lines['R'], across)))
    b_tilted = conf['B'] >= 0.3 and tilt('B') >= 2
    if b_tilted:
        conf['B'] = min(conf['B'], 0.29)  # 수직이 아닌 아랫변은 믿지 않는다(신뢰도 낮춰 확인 표시)
    runs_off = H >= h - 2   # 아래에 드라이버가 채운 빈칸이 없다 = 종이가 스캔 범위 밖까지 이어진다(A4 종이 등)
    if conf['B'] < 0.3 and (runs_off or not b_tilted):
        # 종이가 스캔 끝까지 이어지면 비스듬히 잡힌 선은 글자 줄·얼룩이다 → 스캔(내용) 끝까지.
        # (스캔 안에서 끝나는 손 재단 종이의 비스듬한 아랫변은 그 선이 더 정확해서 그대로 둔다)
        lines['B'] = fallback['B']
        if runs_off and min(conf['L'], conf['R']) >= 0.6:
            conf['B'] = min(conf['L'], conf['R'])   # 아랫변 = 스캔 끝. 정상
        elif not b_tilted and min(conf['L'], conf['R']) >= 0.8:
            # 좌우에 배경판이 보이면(바깥 열이 종이·빈칸보다 어둡다) 그 배경이 끝나는 줄 = 종이 아래 끝.
            # 흰 종이와 드라이버가 채운 흰 빈칸이 맞닿아 아랫변이 안 보여도 높이는 정확하다
            mid_rows = g[int(H * .3):int(H * .7)]
            if np.median(mid_rows[:, :3]) < 250 and np.median(mid_rows[:, -3:]) < 250:
                conf['B'] = min(conf['L'], conf['R']) * 0.95
    b = mid('B')
    quad = [x * n + y * d for x, y in ((x_left, t), (x_right, t), (x_right, b), (x_left, b))]
    quad = [[float(px / s), float(py / s)] for px, py in quad]
    ih, iw = img.shape[:2]
    full = [[0.0, 0.0], [iw - 1.0, 0.0], [iw - 1.0, ih - 1.0], [0.0, ih - 1.0]]
    qa = np.array(quad)
    qw = (np.linalg.norm(qa[1] - qa[0]) + np.linalg.norm(qa[2] - qa[3])) / 2
    qh = (np.linalg.norm(qa[3] - qa[0]) + np.linalg.norm(qa[2] - qa[1])) / 2
    if min(qw, qh) < 60 / 25.4 * dpi or max(qw, qh) > 3 * min(qw, qh):
        # 말이 안 되는 모양(아주 작거나 길쭉함) = 감지 실패. 스캔 전체를 쓰고 확인 표시
        return full, 0.0, False, dict(L=0.0, R=0.0, T=0.0, B=0.0)
    if feeder and max(conf['L'], conf['R']) < 0.3 and H >= h - 2:
        # 좌우 어디에도 배경이 안 보이고 아래 빈칸도 없다 = 종이가 스캔 영역을 꽉 채운다
        # (스캐너가 용지 크기로 잘라 보내는 경우). 스캔 전체가 종이다
        return full, 0.5, True, dict(L=0.5, R=0.5, T=0.5, B=0.5)
    # 윗변은 그림자가 없는 게 정상(피더가 위쪽에 붙임)이라 신뢰도 계산에서 뺀다.
    # top_ok=False 면 윗변을 추정한 것이고, 기울어 들어온 종이는 윗모서리가 스캔 범위 밖으로
    # 잘렸을 수 있다 → process.effective_quad 가 다른 페이지 세로 길이로 복원한다.
    sides = {k: round(float(v), 2) for k, v in conf.items()}  # 스캔 방향 기준 L·R·T·B
    if not feeder:  # 평판은 네 변 모두 보여야 정상
        return quad, min(conf['L'], conf['R'], conf['B'], conf['T']), bool(top_ok), sides
    return quad, min(conf['L'], conf['R'], conf['B']), bool(top_ok), sides
