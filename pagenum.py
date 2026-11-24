"""쪽 번호 읽기.

책마다 쪽 번호 위치·모양이 다르고, 쪽 번호가 없는 페이지(표지·간지·빈 쪽)도 있다. 그래서
1) 페이지 위·아래 여백 띠에서 숫자가 든 글자 덩어리를 모두 후보로 읽어 두고(read_candidates),
2) 책 전체를 보고 '스캔 순서에 따라 1씩 늘어나는' 후보가 가장 많이 나오는 띠를 그 책의 쪽 번호 자리로
   고른 뒤 페이지마다 번호를 정한다(assign). 본문 속 번호·연도처럼 다른 숫자는 이 규칙에 덜 맞아 걸러진다.
글자 위치 찾기는 OCR 엔진의 검출 단계가 느려서(띠 하나 1초 이상) 잉크 덩어리로 직접 찾고,
OCR 은 잘라낸 조각을 읽는 데만 쓴다.
"""
import re
from collections import Counter

import cv2
import numpy as np

BAND = 0.10         # 위·아래 여백 띠 높이(페이지 높이 대비)
_ocr = None


def _reader():
    global _ocr
    if _ocr is None:
        from rapidocr_onnxruntime import RapidOCR
        _ocr = RapidOCR(intra_op_num_threads=1, use_cls=False)
    return _ocr


def _text_boxes(band, dpi):
    """여백 띠에서 글자 줄(단어) 덩어리 상자들."""
    g = cv2.cvtColor(band, cv2.COLOR_BGR2GRAY)
    ink = (g < np.median(g) - 60).astype(np.uint8)
    px = dpi / 25.4                                   # 1mm 당 픽셀
    k = cv2.getStructuringElement(cv2.MORPH_RECT, (max(int(2.5 * px), 3), max(int(0.6 * px), 1)))
    merged = cv2.morphologyEx(ink, cv2.MORPH_CLOSE, k)  # 글자끼리 이어 단어로
    n, _, stats, _ = cv2.connectedComponentsWithStats(merged)
    boxes = []
    for x, y, w, h, area in stats[1:]:
        # 글자 높이 1.2~8mm, 너비는 띠의 절반 미만(긴 선·그림 제외)
        if 1.2 * px <= h <= 8 * px and w < band.shape[1] * 0.5 and area > 0.15 * w * h * 0 + 4:
            boxes.append((x, y, w, h))
    boxes.sort(key=lambda b: -b[2] * b[3])
    return boxes[:8]


def read_candidates(page_img, dpi, bands=('top', 'bottom')):
    """바로 세운 페이지 이미지에서 여백의 숫자 후보 [(띠, 가로위치 0~1, 숫자)].
    bands: 읽을 띠. 책의 쪽 번호 자리가 이미 정해졌으면 그 띠만 읽어 시간을 아낀다."""
    h, w = page_img.shape[:2]
    out = []
    ocr = _reader()
    for name, y0, y1 in (('top', 0, int(h * BAND)), ('bottom', int(h * (1 - BAND)), h)):
        if name not in bands:
            continue
        band = page_img[y0:y1]
        for x, y, bw, bh in _text_boxes(band, dpi):
            m = max(int(bh * 0.4), 4)
            crop = band[max(y - m, 0):y + bh + m, max(x - m, 0):x + bw + m]
            res, _ = ocr(crop, use_det=False, use_cls=False, use_rec=True)
            if not res:
                continue
            text = res[0][0] if isinstance(res[0], (list, tuple)) else str(res[0])
            for num in re.findall(r'(?<!\d)(\d{1,4})(?!\d)', text):
                out.append((name, round((x + bw / 2) / w, 3), int(num)))
    return out


def _cands(p, rot):
    """실제 결과 방향(rot)으로 세웠을 때 읽은 후보. 분석 때 두 방향(자기 판단·180°)으로 읽어 둔다.
    90° 어긋난 방향이면 읽은 게 없다고 본다."""
    c = p.get('pno_cands') or {}
    return c.get(str(rot % 360), []) if isinstance(c, dict) else []


def assign(pages, rots=None):
    """자동으로 읽은 번호를 정한 뒤, 사용자가 정한 것을 우선 적용한다:
    표지(cover='front') = 0쪽, 뒷표지(cover='back') = 마지막 번호 + 1, 직접 입력한 번호(pno_manual),
    '번호 없는 쪽'(no_pno). 반환은 _assign_auto 와 같다."""
    res = _assign_auto(pages, rots)
    for p in pages:
        if p.get('cover') == 'front':
            p['pno'] = 0                      # 표지 = 0쪽
        elif p.get('no_pno'):
            p['pno'] = None
        elif p.get('pno_manual') is not None:
            p['pno'] = p['pno_manual']
    # 뒷표지 = 책의 마지막 번호 + 1 (페이지가 늘어도 항상 맨 끝을 따라간다)
    backs = [p for p in pages if p.get('cover') == 'back']
    if backs:
        last = max((p['pno'] for p in pages if p.get('pno') is not None and p.get('cover') != 'back'), default=0)
        for p in backs:
            p['pno'] = last + 1
    return res


def suggest(pages, i):
    """i 번째 페이지의 쪽 번호 추측: 가장 가까운 앞(없으면 뒤) 번호 페이지에서 위치 차이만큼."""
    for j in range(i - 1, -1, -1):
        if pages[j].get('pno') is not None:
            return pages[j]['pno'] + (i - j)
    for j in range(i + 1, len(pages)):
        if pages[j].get('pno') is not None:
            return max(pages[j]['pno'] - (j - i), 1)
    return i + 1


def _assign_auto(pages, rots=None):
    """pages(목록 순서)의 'pno_cands' 로 쪽 번호 자리를 정하고 page['pno'] 를 채운다(못 읽으면 None).
    rots: 페이지마다 실제 결과 방향(없으면 OCR 때 방향 그대로).
    반환: (고른 띠 이름 또는 None(쪽 번호가 있는 책으로 보기 어려울 때), 규칙에 맞은 페이지 수)."""
    rots = rots or [p.get('pno_rot', 0) for p in pages]
    cands = [_cands(p, r) for p, r in zip(pages, rots)]
    votes = Counter()
    for i, p in enumerate(pages):
        for band, _, v in cands[i]:
            votes[(band, v - i)] += 1               # 같은 띠·같은 차이(쪽 번호 - 순번)가 많을수록 쪽 번호
    if not votes:
        for p in pages:
            p['pno'] = None
        return None, 0
    (band, offset), n = votes.most_common(1)[0]
    if n < max(3, len(pages) * 0.2):                # 규칙에 맞는 페이지가 너무 적으면 쪽 번호 없는 책
        for p in pages:
            p['pno'] = None
        return None, n
    for i, p in enumerate(pages):
        vals = [v for b, _, v in cands[i] if b == band]
        # 띠 안에 숫자가 여럿이면 순서상 기대값에 가장 가까운 것
        p['pno'] = min(vals, key=lambda v: abs(v - (i + offset))) if vals else None
    return band, n


def check(pages):
    """목록 순서에서 쪽 번호로 본 문제: (빠진 구간 [(앞 번호, 뒤 번호, 빠진 수)], 중복 번호 [..]).
    번호 없는 페이지가 사이에 있으면 그만큼은 빠진 게 아니라고 본다(표지·간지 등)."""
    nums = [(i, p['pno']) for i, p in enumerate(pages) if p.get('pno') is not None]
    missing = []
    for (i, a), (j, b) in zip(nums, nums[1:]):
        lost = (b - a - 1) - (j - i - 1)
        if b > a and lost > 0:
            missing.append((a, b, lost))
    dup = sorted(v for v, c in Counter(v for _, v in nums).items() if c > 1)
    return missing, dup


def _runs(nums):
    """번호 있는 페이지마다, 앞뒤로 번호가 1씩 이어지는 길이(본문 속에 있는지 가늠).
    nums: [(목록 위치, 번호)] 목록 순서."""
    n = len(nums)
    back, fwd = [0] * n, [0] * n
    for k in range(1, n):
        if nums[k][1] == nums[k - 1][1] + 1:
            back[k] = back[k - 1] + 1
    for k in range(n - 2, -1, -1):
        if nums[k + 1][1] == nums[k][1] + 1:
            fwd[k] = fwd[k + 1] + 1
    return [b + f for b, f in zip(back, fwd)]


def place_index(pages, pno):
    """쪽 번호 pno 인 페이지가 들어갈 자리(pages 순서 기준 0..len).
    번호가 가장 가까운 앞 번호(없으면 뒤 번호) 페이지 옆. 같은 번호가 여러 장이면(복사본·재스캔본이
    끝에 따로 있는 등) 앞뒤 번호가 이어지는 쪽, 즉 본문 속에 있는 페이지를 기준으로 삼는다.
    (예전엔 '작은 번호 중 목록에서 가장 뒤에 있는 페이지 뒤'라, 끝에 몰려 있는 복사본 옆으로 가서
    옮겼다고 하고도 그 자리에 머무는 일이 있었다)"""
    nums = [(i, p['pno']) for i, p in enumerate(pages) if p.get('pno') is not None]
    if not nums:
        return len(pages)
    run = _runs(nums)
    lower = [k for k, (_, v) in enumerate(nums) if v < pno]
    upper = [k for k, (_, v) in enumerate(nums) if v > pno]
    pred = succ = None
    if lower:
        best = max(nums[k][1] for k in lower)
        pred = max((k for k in lower if nums[k][1] == best), key=lambda k: run[k])
    if upper:
        best = min(nums[k][1] for k in upper)
        succ = max((k for k in upper if nums[k][1] == best), key=lambda k: run[k])
    if pred is not None and succ is not None:
        if nums[pred][0] < nums[succ][0]:      # 앞뒤 번호가 순서대로 있다: 앞 번호 바로 뒤
            return nums[pred][0] + 1
        # 앞뒤 번호가 서로 엇갈린 곳에 있다: 본문 속에 있는(이어지는 길이가 긴) 쪽 옆
        return nums[pred][0] + 1 if run[pred] >= run[succ] else nums[succ][0]
    if pred is not None:
        return nums[pred][0] + 1
    return nums[succ][0]

