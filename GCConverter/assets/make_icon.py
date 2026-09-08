# -*- coding: utf-8 -*-
"""GC Converter 아이콘 생성기.

K-Petro 그레이 원 위에 GC 크로마토그램 파형을 그려 assets/icon.ico 와
assets/icon_1024.png 를 만든다. OilScope 아이콘과 같은 바탕색/초록을 써서
같은 계열로 보이되, 그림은 이 프로그램이 다루는 크로마토그램으로 구분한다.
아이콘을 손보고 싶으면 아래 peaks 값을 바꾸고 이 파일을 실행하면 된다:

    python assets/make_icon.py
"""
import math, os
from PIL import Image, ImageDraw

SS = 4                      # 슈퍼샘플링 배율
N = 1024
W = N * SS

GRAY = (82, 82, 88, 255)          # K-Petro GRAY (OilScope 아이콘과 같은 바탕)
GREEN = (140, 198, 63, 255)       # K-Petro LIGHT GREEN
GREEN_FILL = (140, 198, 63, 140)
BASELINE = (216, 217, 219, 255)   # K-Petro LIGHT GRAY

img = Image.new("RGBA", (W, W), (0, 0, 0, 0))
d = ImageDraw.Draw(img)
d.ellipse([0, 0, W - 1, W - 1], fill=GRAY)

# ── 크로마토그램 파형 ─────────────────────────────────────────────────
left, right = 0.16 * W, 0.84 * W
base_y = 0.735 * W
top_y = 0.235 * W
span = base_y - top_y

# (중심 위치, 폭, 높이) - 솔벤트 피크 하나 + 주 피크 + 뒤따르는 작은 피크들
peaks = [
    (0.10, 0.015, 0.36),
    (0.38, 0.019, 1.00),
    (0.57, 0.016, 0.48),
    (0.78, 0.018, 0.68),
]

TAIL = 0.020   # 실제 GC 피크처럼 오른쪽으로 살짝 끌리는 꼬리

def signal(u: float) -> float:
    y = 0.0
    for c, w, h in peaks:
        if u >= c:                       # 봉우리 오른쪽은 조금 더 넓게 끌린다
            y += h * math.exp(-0.5 * ((u - c) / (w + TAIL * 0.5)) ** 2)
        else:
            y += h * math.exp(-0.5 * ((u - c) / w) ** 2)
    return min(y, 1.0)

pts = []
steps = 1400
for i in range(steps + 1):
    u = i / steps
    x = left + u * (right - left)
    pts.append((x, base_y - signal(u) * span))

# 파형 아래를 옅은 초록으로 채워 작은 크기에서도 덩어리로 읽히게 한다
d.polygon(pts + [(right, base_y), (left, base_y)], fill=GREEN_FILL)
d.line(pts, fill=GREEN, width=int(0.026 * W), joint="curve")

# 베이스라인(시간축)
axis_y = base_y + 0.016 * W
d.line([(left - 0.04 * W, axis_y), (right + 0.04 * W, axis_y)],
       fill=BASELINE, width=int(0.026 * W))


img = img.resize((N, N), Image.LANCZOS)
OUT = os.path.dirname(os.path.abspath(__file__))
img.save(os.path.join(OUT, "icon_1024.png"))
img.save(os.path.join(OUT, "icon.ico"),
         sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
print("생성 완료:", os.path.join(OUT, "icon.ico"))
