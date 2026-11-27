"""감지한 종이 영역을 펴서 잘라내고, 회전·크기 통일·여백·빈 페이지 판정을 한다."""
import re

import cv2
import numpy as np


def quad_size(quad):
    """모서리 4점으로 종이의 (가로, 세로) 픽셀 크기."""
    q = np.array(quad, np.float32)
    w = (np.linalg.norm(q[1] - q[0]) + np.linalg.norm(q[2] - q[3])) / 2
    h = (np.linalg.norm(q[3] - q[0]) + np.linalg.norm(q[2] - q[1])) / 2
    return float(w), float(h)


def warp(img, quad, scale=1.0):
    """기울기 보정 + 잘라내기: 사각형으로 펴낸다. img 는 이미 scale 배로 줄인 원본."""
    q = np.array(quad, np.float32) * scale
    w, h = quad_size(q)
    w, h = max(int(round(w)), 1), max(int(round(h)), 1)
    dst = np.array([[0, 0], [w - 1, 0], [w - 1, h - 1], [0, h - 1]], np.float32)
    m = cv2.getPerspectiveTransform(q, dst)
    # 축소 미리보기는 빠르게, 최종 저장(원본 크기)은 화질 우선
    out = cv2.warpPerspective(img, m, (w, h), flags=cv2.INTER_AREA if scale < 1 else cv2.INTER_CUBIC,
                              borderMode=cv2.BORDER_CONSTANT, borderValue=0)
    valid = cv2.warpPerspective(np.full(img.shape[:2], 255, np.uint8), m, (w, h), flags=cv2.INTER_NEAREST,
                                borderMode=cv2.BORDER_CONSTANT, borderValue=0)
    # 경계 2px 은 보간 때 바깥 검정이 섞이므로 같이 채운다
    missing = cv2.dilate((valid < 255).astype(np.uint8), np.ones((5, 5), np.uint8)).astype(bool)
    if missing.any() and not missing.all():
        # 스캔 범위 밖(기울어 들어와 잘린 모서리 등)은 바로 옆 바탕색으로 채운다.
        # 가장자리 픽셀을 늘여 붙이면 무늬 있는 컬러 페이지에 줄무늬가 생긴다.
        k = max(int(min(w, h) * 0.02), 3)
        near = cv2.dilate(missing.astype(np.uint8), np.ones((k, k), np.uint8)).astype(bool) & ~missing
        out[missing] = _dominant_color(out[near]) if near.any() else 255
    return out


def _dominant_color(pixels):
    """가장 많은 색. 회색 바탕에 노란 글자가 섞여 있으면 중간값은 탁한 색이 되므로 무리 짓기로 고른다."""
    px = pixels.reshape(-1, 3).astype(np.float32)
    if len(px) > 4000:
        px = px[np.random.default_rng(0).choice(len(px), 4000, replace=False)]
    if len(px) < 10:
        return np.median(px, 0)
    _, labels, centers = cv2.kmeans(px, 3, None, (cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_MAX_ITER, 20, 1.0),
                                    2, cv2.KMEANS_PP_CENTERS)
    return centers[np.bincount(labels.ravel()).argmax()]


def effective_quad(page, opt, target):
    """윗변(종이가 들어오는 쪽)을 못 찾은 페이지는 아랫변에서 표준 세로 길이만큼으로 윗변을 정한다.
    앞면은 흰 배경판 위 흰 종이라 그 변이 안 보이고, 기울어 들어오면 모서리가 스캔 범위 밖으로
    잘리기도 해서, 실제 크기는 짧게도 길게도 잘못 잴 수 있다 → 늘리기·줄이기 모두 한다.
    스캔 범위 밖으로 나간 부분은 warp 가 바탕색으로 채운다."""
    q = page['quad']
    if page.get('top_ok', True) or not target:
        return q
    deg = page_rotation(page, opt)
    th = target[0] if (deg // 90) % 2 else target[1]  # 회전 전(스캔 방향) 기준 세로
    tl, tr, br, bl = (np.array(p, float) for p in q)
    out = []
    for top, bottom in ((tl, bl), (tr, br)):
        d = top - bottom
        length = np.linalg.norm(d)
        out.append(bottom + d * (th / length) if length else top)
    return [out[0].tolist(), out[1].tolist(), br.tolist(), bl.tolist()]


def rotate(img, deg):
    k = (deg // 90) % 4
    return img if k == 0 else np.ascontiguousarray(np.rot90(img, -k))  # 시계 방향 양수


def rotated_size(size, deg):
    w, h = size
    return (h, w) if (deg // 90) % 2 else (w, h)


def paper_color(img):
    """가장자리 안쪽 띠의 중간값을 종이색으로 본다(그림자 선은 피함)."""
    h, w = img.shape[:2]
    a, b = max(int(h * .01), 1), max(int(h * .04), 2)
    c, d = max(int(w * .01), 1), max(int(w * .04), 2)
    ring = np.concatenate([img[a:b].reshape(-1, 3), img[h - b:h - a].reshape(-1, 3),
                           img[:, c:d].reshape(-1, 3), img[:, w - d:w - c].reshape(-1, 3)])
    return tuple(int(v) for v in np.median(ring, 0))


def fit_size(img, target, mode, fill, spine='center'):
    """모든 페이지를 정확히 target 크기로.
    mode 'pad': 내용 크기는 그대로 두고 제본 쪽(spine: 'left'/'right'/'center')에 종이색을 덧대거나
                덜어 낸다. 바깥쪽 재단면에 붙여 정렬하므로 페이지를 넘겨도 내용 위치가 흔들리지 않는다.
    mode 'stretch': 이미지를 늘려 맞춘다(내용도 그만큼 늘어난다)."""
    tw, th = int(round(target[0])), int(round(target[1]))
    h, w = img.shape[:2]
    if mode == 'stretch':
        return cv2.resize(img, (tw, th), interpolation=cv2.INTER_CUBIC if tw * th > w * h else cv2.INTER_AREA)
    out = np.empty((th, tw, 3), np.uint8)
    out[:] = fill
    # 가로: 제본 쪽에서 덧대거나 덜어 냄. 세로(위·아래는 원래 재단면): 가운데 기준.
    if spine == 'left':      # 내용은 오른쪽(바깥쪽) 끝에 붙인다
        sx, dx = max(w - tw, 0), max(tw - w, 0)
    elif spine == 'right':   # 내용은 왼쪽(바깥쪽) 끝에 붙인다
        sx, dx = 0, 0
    else:
        sx, dx = max((w - tw) // 2, 0), max((tw - w) // 2, 0)
    sy, dy = max((h - th) // 2, 0), max((th - h) // 2, 0)
    cw, ch = min(w, tw), min(h, th)
    out[dy:dy + ch, dx:dx + cw] = img[sy:sy + ch, sx:sx + cw]
    return out


def spine_side(page, opt):
    """제본 쪽(직접 잘라서 크기가 들쭉날쭉한 쪽). 책의 오른쪽 페이지(앞면)는 제본이 왼쪽에 있다."""
    mode = opt.get('spine', 'auto')
    if mode == 'center':
        return 'center'
    if page.get('cover') in ('front', 'back'):
        left = page['cover'] == 'front'     # 표지는 책의 오른쪽 면, 뒷표지는 왼쪽 면
    elif page.get('pno') is not None:
        # 쪽 번호를 읽었으면 그게 가장 확실하다: 홀수 쪽 = 책의 오른쪽 페이지 = 제본이 왼쪽.
        # (종이가 뒤집혀 들어가 앞·뒷면이 바뀐 경우에도 맞게 된다)
        left = page['pno'] % 2 == 1
    elif page.get('side') in ('front', 'back'):
        left = page['side'] == 'front'
    else:
        return 'center'
    if mode == 'swap':
        left = not left
    return 'left' if left else 'right'


SHARPEN_AMOUNT = (0.0, 0.4, 0.8, 1.3)  # 끔 / 약하게 / 보통 / 강하게


def sharpen(img, level, dpi):
    """언샤프 마스크. 복합기 JPEG 은 300dpi 에서 글자 가장자리가 살짝 무르다.
    번짐 반경은 해상도에 맞춰(300dpi 에서 약 1px) 미리보기와 저장본이 같은 느낌이 되게 한다."""
    amount = SHARPEN_AMOUNT[max(0, min(int(level), 3))]
    if amount <= 0:
        return img
    sigma = max(dpi / 300, 0.3)
    blur = cv2.GaussianBlur(img, (0, 0), sigma)
    return cv2.addWeighted(img, 1 + amount, blur, -amount, 0)


def clean_edges(img, px):
    """가장자리 px 띠를 바로 안쪽 바탕색으로 덮는다. 감지가 1mm 안팎 어긋나 남는 배경판 띠·그림자 선을
    가리기 위한 것(실제 110면에서 뒷면 몇 장에 하늘색 띠가 약 1mm 남았다). 크기는 그대로."""
    h, w = img.shape[:2]
    if px <= 0 or h <= 4 * px or w <= 4 * px:
        return img
    img = img.copy()
    inner = np.concatenate([img[px:2 * px, px:w - px].reshape(-1, 3), img[h - 2 * px:h - px, px:w - px].reshape(-1, 3),
                            img[px:h - px, px:2 * px].reshape(-1, 3), img[px:h - px, w - 2 * px:w - px].reshape(-1, 3)])
    c = _dominant_color(inner)
    img[:px], img[h - px:], img[:, :px], img[:, w - px:] = c, c, c, c
    return img


def add_margin(img, px, fill):
    if px <= 0:
        return img
    return cv2.copyMakeBorder(img, px, px, px, px, cv2.BORDER_CONSTANT, value=fill)


def ink_ratio(img):
    """빈 페이지 판정용: 종이색과 확실히 다른 점의 비율. 가장자리 5% 와 뒷면 비침(옅은 점)은 무시.
    색 거리(Lab) 25 기준: 실제 86면에서 회색 바탕+노란 무늬 면지가 예전 기준(60)으론 0% 로 나와
    빈 페이지로 빠졌다. 25 면 그 면지 44%, 가장 내용 적은 페이지 2.6%, 뒷면 비침만 있는 흰 종이 0%."""
    h, w = img.shape[:2]
    img = cv2.medianBlur(img[int(h * .05):int(h * .95), int(w * .05):int(w * .95)], 5)
    lab = cv2.cvtColor(img, cv2.COLOR_BGR2LAB).reshape(-1, 3).astype(np.float32)
    dist = np.linalg.norm(lab - np.median(lab, 0), axis=1)
    return float((dist > 25).mean())


def render(raw, page, opt, target, scale=1.0):
    """한 페이지 결과 이미지. raw 는 scale 배로 줄인 원본, target 은 원본 해상도 기준 통일 크기."""
    deg = page_rotation(page, opt)
    if opt['crop']:
        img = warp(raw, effective_quad(page, opt, target), scale)
        edge = page.get('edge_mm', opt.get('edge_mm', 0.5))  # 페이지별로 따로 정했으면 그 값
        img = clean_edges(img, int(round(edge / 25.4 * opt['dpi'] * scale)))
    else:
        img = raw
    img = rotate(img, deg)
    img = sharpen(img, opt.get('sharpen', 0), opt['dpi'] * scale)
    fill = paper_color(img)
    if opt['crop'] and opt['uniform'] and target:
        img = fit_size(img, (target[0] * scale, target[1] * scale), opt['uniform_mode'], fill,
                       spine_side(page, opt))
    return add_margin(img, int(round(opt['margin_mm'] / 25.4 * opt['dpi'] * scale)), fill)


def page_rotation(page, opt):
    """자동 판별 값(있으면) 또는 앞/뒷면 설정값 + 사용자가 직접 돌린 만큼."""
    if opt.get('auto_orient') and page.get('auto_rot') is not None:
        base = page['auto_rot']
    else:
        base = {'front': opt['rot_front'], 'back': opt['rot_back']}.get(page.get('side'), opt['rot_front'])
    return (base + page.get('rot', 0)) % 360


def rescan_window(idx, old_len, new_len, radius=2):
    """같은 묶음을 같은 순서로 다시 넣었을 때 원래 idx 번째 면이 있을 만한 재스캔 순번들.
    장수가 같으면 그 자리 하나(비교 불필요), 다르면 비례 위치 앞뒤 radius 장."""
    if new_len == old_len:
        return [idx]
    expected = round(idx * (new_len - 1) / max(old_len - 1, 1))
    return [j for j in range(expected - radius, expected + radius + 1) if 0 <= j < new_len]


RAW_NAME = re.compile(r'^(.+)-(?:imp)?(\d{4})\.\w+$')


def _sheet_key(page):
    """양면 스캔에서 같은 종이의 앞·뒷면을 묶는 열쇠 (묶음 이름, 종이 번호). 알 수 없으면 None."""
    m = RAW_NAME.match(page.get('raw', ''))
    if not m or page.get('side') not in ('front', 'back'):
        return None
    return m.group(1), (int(m.group(2)) - 1) // 2


def assign_auto_rotation(pages):
    """방향 모델 결과(page['ori'])를 면(앞면·뒷면·기타)별 다수결로 다듬어 page['auto_rot'] 에 넣는다.

    같은 면은 피더에 같은 방향으로 들어가므로 면 전체의 합산 결과(다수결)를 기본으로 쓴다.
    다수결과 다르게 돌리는 건 종이를 정말 거꾸로 넣었을 때뿐인데, 그러면 그 종이의 앞·뒷면이
    둘 다 같은 만큼 틀어져 있어야 한다. 그래서
    - 양면: 앞·뒷면이 모두 '확실히 다르다(자기 방향 0.7↑, 다수결 방향 0.15↓)'고 하고 틀어진 정도가
      같을 때만 따른다. 한 면만 그렇다고 하면 모델 착각으로 본다.
      (실제 86면에서 한 면만 0.76 으로 '거꾸로'라고 착각한 사례가 있었다)
    - 짝이 없는 단면: 더 엄격하게(0.8↑, 0.1↓) 확실할 때만 따른다."""
    import orient
    groups = {}
    for p in pages:
        # 거의 빈 면(내용 1.5% 미만: 접힌 자국·비침 정도)의 방향 판단은 무작위에 가깝다: 다수결에 넣지 않는다
        if p.get('ori') and p.get('ink', 1.0) >= 0.015:
            groups.setdefault(p.get('side'), []).append(p)
    consensus = {side: int(np.argmax(np.log(np.array([p['ori'] for p in g]) + 1e-9).sum(0)))
                 for side, g in groups.items()}

    def deviation(p, own_min, group_max):
        """다수결 대비 확실히 틀어져 있으면 그 차이(1~3), 아니면 0."""
        k_all = consensus[p.get('side')]
        k_own = int(np.argmax(p['ori']))
        if k_own != k_all and p['ori'][k_own] >= own_min and p['ori'][k_all] < group_max:
            return (k_own - k_all) % 4
        return 0

    sheets = {}
    for p in pages:
        if p.get('ori') and _sheet_key(p):
            sheets.setdefault(_sheet_key(p), []).append(p)
    for p in pages:
        if not p.get('ori'):
            continue
        if p.get('side') not in consensus:
            p['auto_rot'] = None      # 이 면에 내용 있는 페이지가 없다: 앞/뒷면 설정값을 따른다
            continue
        k_all = consensus[p.get('side')]
        if p.get('ink', 1.0) < 0.015:
            p['auto_rot'] = orient.fix_rotation(k_all)   # 빈 면은 자기 판단 없이 다수결
            continue
        mates = [q for q in sheets.get(_sheet_key(p), []) if q is not p] if _sheet_key(p) else []
        if mates:
            dev = deviation(p, 0.7, 0.15)
            if dev and any(deviation(q, 0.7, 0.15) != dev for q in mates):
                dev = 0
        elif p.get('side') == 'flat':
            # 평판은 한 장씩 손으로 올려놓아 방향이 장마다 다를 수 있다: 페이지 자신의 판단이
            # 웬만큼(0.5↑) 확실하면 그걸 쓰고, 애매할 때만 다수결을 따른다
            k_own = int(np.argmax(p['ori']))
            dev = (k_own - k_all) % 4 if p['ori'][k_own] >= 0.5 else 0
        else:
            dev = deviation(p, 0.8, 0.1)
        p['auto_rot'] = orient.fix_rotation((k_all + dev) % 4)


def reconcile_sheets(pages, mm_tol=5.0):
    """양면 스캔: 같은 종이의 앞·뒷면은 크기가 같다. 한 면은 확실히 잡혔는데(신뢰도 0.8↑) 다른 면이
    그보다 5mm 넘게 다르거나 불확실하면, 확실한 면의 영역을 좌우로 뒤집어 다른 면에 쓴다.
    (양면 피더는 종이를 뒤집어 읽으므로 뒷면 이미지는 앞면의 좌우 반전. 앞·뒤 센서의 위치 차이는
     같은 묶음에서 양쪽 다 확실한 종이들로 잰다 — 특정 기종 값을 넣지 않는다)
    흰 배경판 위 흰 종이처럼 한 면만으로는 가장자리를 알 수 없는 경우를 위한 것. 손으로 고친 면은 건드리지 않는다."""
    sheets, batches = {}, {}
    for p in pages:
        k = _sheet_key(p)
        if k and p.get('auto_quad') and p.get('img_w') and not p.get('manual_quad'):
            sheets.setdefault(k, {})[p['side']] = p
    for k, pair in sheets.items():
        f, b = pair.get('front'), pair.get('back')
        if f and b and min(f.get('auto_conf', 0), b.get('auto_conf', 0)) >= 0.8:
            fq, bq = np.array(f['auto_quad']), np.array(b['auto_quad'])
            batches.setdefault(k[0], []).append(fq[:, 0].min() - (b['img_w'] - 1 - bq[:, 0].max()))
    for k, pair in sheets.items():
        f, b = pair.get('front'), pair.get('back')
        for p in (f, b):     # 지난번에 짝에서 빌려 온 값은 먼저 원래대로
            if p and p.get('from_mate'):
                p.update(quad=p['auto_quad'], conf=p.get('auto_conf', 0), side_conf=p.get('auto_side_conf'),
                         top_ok=p.get('auto_top_ok', True), from_mate=False)
        if not (f and b):
            continue
        good, weak = (f, b) if f.get('auto_conf', 0) >= b.get('auto_conf', 0) else (b, f)
        if good.get('auto_conf', 0) < 0.8:
            continue
        gs, ws = quad_size(good['auto_quad']), quad_size(weak['auto_quad'])
        mm = 25.4 / good.get('dpi', 300)
        differs = max(abs(gs[0] - ws[0]), abs(gs[1] - ws[1])) * mm > mm_tol
        if not (differs or weak.get('auto_conf', 0) < 0.5):
            continue
        offs = batches.get(k[0])
        off = float(np.median(offs)) if offs else 0.0
        W = good['img_w'] - 1
        g = np.array(good['auto_quad'], float)
        # 좌우 반전: 왼위↔오른위, 오른아래↔왼아래. 앞면 x = W - 뒷면 x + off (반대도 같은 식)
        m = [[W - g[i][0] + off, g[i][1]] for i in (1, 0, 3, 2)]
        weak.update(quad=[[float(x), float(y)] for x, y in m], conf=good['auto_conf'] * 0.9,
                    side_conf=good.get('auto_side_conf'), top_ok=good.get('auto_top_ok', True), from_mate=True)


def target_size(pages, opt):
    """통일 크기 = 큰 쪽 기준(가로·세로 각각 90번째 백분위). 작은 페이지는 여기에 맞춰 키운다.
    최댓값을 그대로 쓰지 않는 건 감지가 크게 틀린 한두 장에 책 전체가 끌려가지 않게 하려는 것.
    위·아래 끝을 모두 확인한 페이지(top_ok)만 쓴다: 윗변을 추정한 페이지는 크기가 부정확하다.
    (그런 페이지가 5장 미만이면 전부 쓴다)"""
    ready = [p for p in pages if not is_excluded(p, opt) and p.get('quad')]
    sure = [p for p in ready if p.get('top_ok', True)]
    use = sure if len(sure) >= 5 else ready
    if not use:
        return None
    trusted = [p for p in use if p.get('conf', 1.0) >= 0.5]    # 가장자리를 못 찾은 페이지는 크기도 믿기 어렵다
    if len(trusted) >= 3:
        use = trusted
    sizes = np.array([rotated_size(quad_size(p['quad']), page_rotation(p, opt)) for p in use])
    # 중간값에서 6% 넘게 벗어난 크기(비뚤게 들어간 종이·감지 실패)는 빼고 계산한다.
    # 장수가 적으면 그런 한두 장이 90번째 백분위를 그대로 끌어올린다(실제 9장 묶음에서 확인)
    med = np.median(sizes, axis=0)
    near = sizes[(np.abs(sizes - med) / med <= 0.06).all(axis=1)]
    if len(near) >= max(3, len(sizes) // 2):
        sizes = near
    return tuple(float(v) for v in np.percentile(sizes, 90, axis=0))


def is_excluded(page, opt):
    if page.get('excluded') is not None:  # 사용자가 직접 정한 값이 우선
        return page['excluded']
    return opt['skip_blank'] and page.get('ink', 1.0) < opt['blank_threshold'] / 100


def warn_reasons(page, opt, target):
    """'확인 필요' 표시의 이유(짧은 문장들). 없으면 빈 목록. 사용자가 '문제없음'을 누른 페이지는 빈 목록."""
    out = []
    if not page.get('quad') or page.get('warn_ok'):
        return out
    if page.get('conf', 1.0) < 0.5:
        # 변 이름은 결과 화면(회전 후) 기준으로: 스캔 방향 위·오른쪽·아래·왼쪽이 시계 방향으로 k칸 돈다
        k = page_rotation(page, opt) // 90
        names = ['위', '오른쪽', '아래', '왼쪽']
        weak = [names[(i + k) % 4] for i, s in enumerate('TRBL')
                if (page.get('side_conf') or {}).get(s, 1.0) < 0.5
                and (s != 'T' or page.get('side') == 'flat')]  # 피더 윗변은 원래 안 보여서 뺀다
        out.append('가장자리를 확실히 못 찾음' + (f' ({"·".join(weak)})' if weak else ''))
    if size_outlier(page, target, opt):
        w, h = rotated_size(quad_size(effective_quad(page, opt, target)), page_rotation(page, opt))
        mm = 25.4 / page.get('dpi', opt['dpi'])
        parts = [f'{name} {abs(v - t) * mm:.0f}mm {"작음" if v < t else "큼"}'
                 for name, v, t in (('가로', w, target[0]), ('세로', h, target[1])) if abs(v - t) / t > 0.06]
        out.append('다른 페이지보다 ' + ', '.join(parts))
    return out


def size_outlier(page, target, opt, tol=0.06):
    """통일 크기와 6% 넘게 다르면 감지 실패 의심. 직접 자른 쪽의 몇 mm 차이는 정상 범위."""
    if not target or not page.get('quad'):
        return False
    w, h = rotated_size(quad_size(effective_quad(page, opt, target)), page_rotation(page, opt))
    return abs(w - target[0]) / target[0] > tol or abs(h - target[1]) / target[1] > tol
