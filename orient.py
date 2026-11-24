"""페이지 방향 자동 판별: PaddleOCR 문서 방향 분류 모델(PP-LCNet_x1_0_doc_ori, Apache-2.0).

여백 많은 실제 책 페이지로 재 보니 모델 그대로는 4방향 정답률 73% 였다.
- 가운데 224px 만 잘라 보던 기본 전처리 대신 페이지 전체를 224×224 로 줄여 넣으면 92%,
- 페이지를 0/90/180/270° 로 돌린 네 장의 답을 서로 맞춰(합산) 보면 95%.
나머지는 앱이 '같은 면은 대부분 같은 방향'이라는 책의 성질로 보정한다(process.assign_auto_rotation).
"""
import os

import cv2
import numpy as np

MODEL = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'models', 'doc_ori.onnx')
_MEAN = np.array([0.485, 0.456, 0.406], np.float32)
_STD = np.array([0.229, 0.224, 0.225], np.float32)
_session = None


def available():
    return os.path.exists(MODEL)


def _sess():
    global _session
    if _session is None:
        import onnxruntime as ort
        ort.set_default_logger_severity(3)
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = 1  # 페이지마다 프로세스 하나씩 병렬로 도니 각자는 1스레드
        _session = ort.InferenceSession(MODEL, opts, providers=['CPUExecutionProvider'])
    return _session


def _prep(img):
    img = cv2.cvtColor(cv2.resize(img, (224, 224), interpolation=cv2.INTER_AREA), cv2.COLOR_BGR2RGB)
    return ((img.astype(np.float32) / 255 - _MEAN) / _STD).transpose(2, 0, 1)


def probs(img):
    """img: BGR 페이지(잘라낸 것, 회전 전). 반환: 길이 4 확률.
    k 번째 = 내용이 시계 방향으로 k×90° 돌아가 있다 → 바로 세우려면 반시계로 k×90° 돌려야 한다."""
    sess = _sess()
    batch = np.stack([_prep(np.ascontiguousarray(np.rot90(img, -j))) for j in range(4)])
    out = sess.run(None, {sess.get_inputs()[0].name: batch})[0].astype(np.float64)
    logp = out - out.max(1, keepdims=True)
    logp -= np.log(np.exp(logp).sum(1, keepdims=True))
    # j 만큼 더 돌린 입력의 정답은 (k + j) — 네 답을 합산
    score = np.array([sum(logp[j][(k + j) % 4] for j in range(4)) for k in range(4)])
    p = np.exp(score - score.max())
    return (p / p.sum()).tolist()


def fix_rotation(k):
    """방향 번호 k 를 바로 세우는 시계 방향 회전 각도."""
    return (360 - 90 * k) % 360
