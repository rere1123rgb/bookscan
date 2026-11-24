"""별도 프로세스에서 도는 페이지 분석. 감지 코드는 Python 반복문이 많아 스레드로는 코어를 다 못 쓴다."""
import cv2
import numpy as np

import orient
import process as P
from detect import VERSION as DETECT_VERSION
from detect import detect_page


def imread(path):
    """한글 경로도 읽히도록 바이트로 읽어 디코딩."""
    return cv2.imdecode(np.fromfile(path, np.uint8), cv2.IMREAD_COLOR)


MATCH_SCALE = 0.25   # 같은 종이 찾기는 약 75dpi 로 충분
_orb = None


def _page_gray(path, dpi, feeder):
    raw = imread(path)
    quad = detect_page(raw, dpi, feeder)[0]
    small = cv2.resize(raw, None, fx=MATCH_SCALE, fy=MATCH_SCALE, interpolation=cv2.INTER_AREA)
    return cv2.cvtColor(P.warp(small, quad, MATCH_SCALE), cv2.COLOR_BGR2GRAY)


SAME_PAGE_MAX_DIFF = 0.17  # '한쪽에만 있는 잉크' 비율. 실제 두 번 스캔한 110면: 같은 종이 ≤0.09, 다른 페이지 ≥0.24


def _ink_difference(a, b):
    """b 를 a 에 특징점으로 겹쳐 맞춘 뒤, 두 페이지 잉크(글자·그림) 중 한쪽에만 있는 비율(0~1).
    같은 종이면 거의 다 겹쳐 0 에 가깝고, 틀만 같은 다른 페이지는 내용이 어긋나 커진다.
    특정 책에 맞춘 값이 아니라 '내용이 같은가'를 직접 보는 것. 놓인 방향은 상관없다."""
    global _orb
    _orb = _orb or cv2.ORB_create(1500)
    ka, da = _orb.detectAndCompute(a, None)
    if da is None:
        return 1.0
    best_inl, best_diff = 0, 1.0
    for k in range(4):
        bb = np.ascontiguousarray(np.rot90(b, -k))
        kb, db = _orb.detectAndCompute(bb, None)
        if db is None or len(kb) < 10:
            continue
        m = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=True).match(da, db)
        if len(m) < 10:
            continue
        pa = np.float32([ka[x.queryIdx].pt for x in m])
        pb = np.float32([kb[x.trainIdx].pt for x in m])
        h, mask = cv2.findHomography(pb, pa, cv2.RANSAC, 4.0)
        if h is None or mask.sum() <= best_inl:
            continue
        best_inl = int(mask.sum())
        w = cv2.warpPerspective(bb, h, (a.shape[1], a.shape[0]), borderValue=255)
        ia, ib = a < np.median(a) - 50, w < np.median(w) - 50
        ker = np.ones((5, 5), np.uint8)
        near_a = cv2.dilate(ia.astype(np.uint8), ker).astype(bool)
        near_b = cv2.dilate(ib.astype(np.uint8), ker).astype(bool)
        only = (ia & ~near_b).sum() + (ib & ~near_a).sum()
        best_diff = float(only / max(ia.sum() + ib.sum(), 1))
    return best_diff


def find_same_page(target, cands, dpi, feeder=True):
    """재스캔한 면들(cands) 중 target 과 같은 종이의 순번. 확실하지 않으면 None.
    반환: (순번 또는 None, 가장 비슷한 후보의 차이 비율)"""
    t = _page_gray(target, dpi, feeder)
    diffs = [_ink_difference(t, _page_gray(c, dpi, feeder)) for c in cands]
    if not diffs:
        return None, 1.0
    best = int(np.argmin(diffs))
    if diffs[best] > SAME_PAGE_MAX_DIFF:
        return None, diffs[best]
    # 같은 종이로 보이는 후보가 둘 이상이면(같은 종이를 두 번 넣는 등) 어느 쪽인지 애매하니 바꾸지 않는다
    if sum(d <= SAME_PAGE_MAX_DIFF for d in diffs) > 1:
        return None, diffs[best]
    return best, diffs[best]


def analyze(path, dpi, small_scale, quad=None, feeder=True, pno_band=None):
    """dict(quad, conf, top_ok, ink, small, ori). quad 를 주면(다시 연 작업) 감지는 건너뛴다."""
    raw = imread(path)
    small = cv2.resize(raw, None, fx=small_scale, fy=small_scale, interpolation=cv2.INTER_AREA)
    res = dict(small=small)
    if quad is None:
        quad, conf, top_ok, sides = detect_page(raw, dpi, feeder)
        res.update(quad=quad, conf=conf, top_ok=top_ok, side_conf=sides, det_v=DETECT_VERSION)
    page = P.warp(small, quad, small_scale)
    if quad is not None and 'conf' in res:
        res['ink'] = P.ink_ratio(page)
    if orient.available():
        res['ori'] = orient.probs(page)
    # 쪽 번호 후보: 페이지 자신의 방향 판단대로 세워서 위·아래 여백을 읽는다(150dpi 면 충분)
    try:
        import pagenum
        # 방향은 나중에 면별 다수결로 바뀔 수 있다(여백 많은 페이지는 자기 판단이 뒤집히기도 함).
        # 거꾸로 선 숫자를 읽으면 엉뚱한 값이 나오므로, 바로 선 방향과 180° 돌린 방향 둘 다 읽어 둔다.
        rot = orient.fix_rotation(int(np.argmax(res['ori']))) if res.get('ori') else 0
        mid = P.warp(cv2.resize(raw, None, fx=150 / dpi, fy=150 / dpi, interpolation=cv2.INTER_AREA), quad, 150 / dpi)
        bands = (pno_band,) if pno_band else ('top', 'bottom')  # 책의 쪽 번호 자리가 정해졌으면 그 띠만
        res['pno_cands'] = {str(r): pagenum.read_candidates(P.rotate(mid, r), 150, bands)
                            for r in (rot, (rot + 180) % 360)}
        res['pno_rot'] = rot
    except Exception:
        pass  # OCR 이 없거나 실패해도 나머지 분석은 그대로
    return res
