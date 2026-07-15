#!/usr/bin/env python3
"""
LCD 3.5" (USB, /dev/ttyACM0): gráficas en tiempo real de las GPUs.

Sustituye al dashboard de máquinas (proxmox_panel.py). Reutiliza la misma
librería del proyecto (LcdCommRevA) y el venv. Render PIL 320x480 portrait.

Muestra la RTX 3080 (nvidia-smi): utilización %, VRAM %, temperatura y potencia,
con histórico deslizante (~5 min). Intenta también la iGPU Intel; si está en
passthrough a una VM (vfio-pci) lo indica en vez de graficar.
"""
import os
import subprocess
import sys
import time
from collections import deque
from datetime import datetime

sys.path.insert(0, "/opt/lcd-panel")
from library.lcd.lcd_comm_rev_a import LcdCommRevA, Orientation  # noqa
from PIL import Image, ImageDraw, ImageFont

W, H = 320, 480
MARGIN = 6

FONT_SANSB = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
FONT_SANS  = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
FONT_MONOB = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf"

BG_TOP  = (13, 19, 36)
BG_BOT  = (6, 9, 18)
CARD    = (18, 26, 44)
CARD_BD = (34, 48, 73)
FG      = (229, 238, 247)
DIM     = (124, 138, 165)
FAINT   = (74, 86, 112)
SKY     = (56, 189, 248)
VIOLET  = (167, 139, 250)
GREEN   = (52, 211, 153)
AMBER   = (251, 191, 36)
RED     = (248, 113, 113)
TRACK   = (27, 39, 64)

f_brand = ImageFont.truetype(FONT_SANSB, 18)
f_clock = ImageFont.truetype(FONT_MONOB, 15)
f_big   = ImageFont.truetype(FONT_MONOB, 34)
f_lbl   = ImageFont.truetype(FONT_SANSB, 11)
f_val   = ImageFont.truetype(FONT_MONOB, 13)
f_small = ImageFont.truetype(FONT_SANS, 10)
f_foot  = ImageFont.truetype(FONT_MONOB, 11)

HIST = 300   # muestras de histórico (~5 min a 1 s)


def _lerp(a, b, t):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def color_for(pct):
    if pct >= 85: return RED
    if pct >= 55: return AMBER
    return GREEN


def get_gpu():
    """RTX 3080: util%, mem_used/total MB, temp C, power W. vfio=True si no accesible."""
    try:
        r = subprocess.run(
            ["nvidia-smi", "--query-gpu=utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=4)
        if r.returncode == 0 and r.stdout.strip():
            u, used, total, temp, pw = [x.strip() for x in r.stdout.strip().splitlines()[0].split(",")]
            return dict(util=int(float(u)), used=int(float(used)), total=int(float(total)),
                        temp=int(float(temp)), power=float(pw), vfio=False)
    except Exception:
        pass
    return dict(util=0, used=0, total=1, temp=0, power=0.0, vfio=True)


_igpu_state = {"driver": None, "checked": 0.0}


def get_igpu_status():
    """Texto de estado de la iGPU Intel. En passthrough (vfio-pci) no se puede medir."""
    now = time.time()
    if now - _igpu_state["checked"] > 10:
        _igpu_state["checked"] = now
        try:
            r = subprocess.run(["bash", "-c",
                                "lspci -nnk | grep -A3 -iE 'VGA.*Intel|Display.*Intel' | grep -i 'Kernel driver in use' | head -1"],
                               capture_output=True, text=True, timeout=4)
            drv = r.stdout.strip().split(":")[-1].strip() if r.stdout.strip() else "?"
            _igpu_state["driver"] = drv or "?"
        except Exception:
            _igpu_state["driver"] = "?"
    drv = _igpu_state["driver"]
    if drv and "vfio" in drv.lower():
        return "iGPU Intel: en VM (passthrough)", AMBER
    if drv == "i915":
        return "iGPU Intel: libre (i915)", GREEN
    return f"iGPU Intel: {drv or 'n/d'}", DIM


_CHROME = None


def _chrome():
    global _CHROME
    if _CHROME is None:
        img = Image.new("RGB", (W, H), BG_BOT)
        d = ImageDraw.Draw(img)
        for y in range(H):
            d.line([(0, y), (W, y)], fill=_lerp(BG_TOP, BG_BOT, y / (H - 1)))
        _CHROME = img
    return _CHROME.copy()


def draw_graph(d, x, y, w, h, series, color, maxval=100.0):
    """Gráfica de área/línea del histórico `series` (deque) en [x,y,w,h]."""
    d.rounded_rectangle([x, y, x + w, y + h], radius=8, fill=CARD, outline=CARD_BD, width=1)
    # rejilla horizontal (25/50/75%)
    for f in (0.25, 0.5, 0.75):
        gy = int(y + h - f * h)
        d.line([(x + 2, gy), (x + w - 2, gy)], fill=(28, 40, 66))
    n = len(series)
    if n < 2:
        return
    step = (w - 4) / (HIST - 1)
    x0 = x + w - 2 - (n - 1) * step   # alinear a la derecha (lo más reciente)
    pts = []
    for i, v in enumerate(series):
        vv = max(0.0, min(maxval, v))
        px = x0 + i * step
        py = y + h - 2 - (vv / maxval) * (h - 4)
        pts.append((px, py))
    # relleno bajo la curva
    poly = pts + [(pts[-1][0], y + h - 2), (pts[0][0], y + h - 2)]
    fill = tuple(int(c * 0.28) for c in color)
    d.polygon(poly, fill=fill)
    d.line(pts, fill=color, width=2, joint="curve")


def render(gpu, hist):
    img = _chrome()
    d = ImageDraw.Draw(img)

    # header
    d.rounded_rectangle([MARGIN, 3, W - MARGIN, 34], radius=10, fill=(16, 24, 42))
    d.ellipse([MARGIN + 8, 12, MARGIN + 18, 22], fill=GREEN if not gpu["vfio"] else RED)
    d.text((MARGIN + 24, 8), "RTX 3080", fill=FG, font=f_brand)
    now = datetime.now().strftime("%H:%M:%S")
    cw = d.textlength(now, font=f_clock)
    d.text((W - MARGIN - cw - 10, 11), now, fill=SKY, font=f_clock)
    ly = 36
    for xx in range(MARGIN, W - MARGIN):
        t = (xx - MARGIN) / (W - 2 * MARGIN)
        d.line([(xx, ly), (xx, ly + 1)], fill=_lerp(SKY, VIOLET, t))

    if gpu["vfio"]:
        d.text((W // 2, H // 2), "3080 en VFIO / no accesible", fill=AMBER,
               font=f_val, anchor="mm")
        return img

    # bloque grande: util actual
    util = gpu["util"]
    d.text((MARGIN + 12, 46), "UTILIZACIÓN", fill=DIM, font=f_lbl)
    d.text((MARGIN + 12, 58), f"{util}", fill=color_for(util), font=f_big)
    d.text((MARGIN + 12 + d.textlength(str(util), font=f_big) + 4, 78), "%", fill=DIM, font=f_val)
    # métricas a la derecha
    mem_pct = gpu["used"] * 100 // max(1, gpu["total"])
    rx = W - MARGIN - 128
    stats = [
        ("VRAM", f"{gpu['used']/1024:.1f}/{gpu['total']/1024:.0f}G", color_for(mem_pct)),
        ("TEMP", f"{gpu['temp']}°C", color_for(gpu['temp'] * 100 / 90)),
        ("POT.", f"{gpu['power']:.0f}W", SKY),
    ]
    for i, (lbl, val, col) in enumerate(stats):
        yy = 50 + i * 18
        d.text((rx, yy), lbl, fill=DIM, font=f_lbl)
        vw = d.textlength(val, font=f_val)
        d.text((W - MARGIN - 12 - vw, yy - 1), val, fill=col, font=f_val)

    # gráficas
    gx, gw = MARGIN, W - 2 * MARGIN
    # util %
    d.text((gx + 4, 108), "UTILIZACIÓN GPU  (5 min)", fill=DIM, font=f_lbl)
    draw_graph(d, gx, 122, gw, 96, hist["util"], SKY, 100.0)
    # vram %
    d.text((gx + 4, 226), "VRAM  (5 min)", fill=DIM, font=f_lbl)
    draw_graph(d, gx, 240, gw, 90, hist["mem"], VIOLET, 100.0)
    # temperatura
    d.text((gx + 4, 338), "TEMPERATURA °C  (5 min)", fill=DIM, font=f_lbl)
    draw_graph(d, gx, 352, gw, 78, hist["temp"], AMBER, 100.0)

    # footer: iGPU
    fy = H - 40
    d.rounded_rectangle([MARGIN, fy, W - MARGIN, H - 3], radius=10,
                        fill=(15, 22, 40), outline=CARD_BD, width=1)
    ig_txt, ig_col = get_igpu_status()
    d.text((MARGIN + 10, fy + 6), ig_txt, fill=ig_col, font=f_foot)
    d.text((MARGIN + 10, fy + 22),
           f"3080  {util}%   {gpu['temp']}°C   {gpu['power']:.0f}W   {gpu['used']/1024:.1f}GB",
           fill=FG, font=f_foot)
    return img


def main():
    com = os.environ.get("LCD_PORT", "/dev/ttyACM0")
    lcd = LcdCommRevA(com_port=com, display_width=W, display_height=H)
    lcd.Reset()
    lcd.InitializeComm()
    lcd.Clear()
    lcd.SetOrientation(Orientation.PORTRAIT)
    lcd.SetBrightness(int(os.environ.get("LCD_BRIGHTNESS", "80")))

    period = float(os.environ.get("LCD_PERIOD_S", "1.0"))
    hist = {k: deque(maxlen=HIST) for k in ("util", "mem", "temp")}
    while True:
        try:
            gpu = get_gpu()
            if not gpu["vfio"]:
                hist["util"].append(gpu["util"])
                hist["mem"].append(gpu["used"] * 100.0 / max(1, gpu["total"]))
                hist["temp"].append(gpu["temp"])
            img = render(gpu, hist)
            lcd.DisplayPILImage(img)
        except KeyboardInterrupt:
            break
        except Exception as e:
            print(f"[gpu-panel] error: {e!r}", file=sys.stderr, flush=True)
        time.sleep(period)


if __name__ == "__main__":
    main()
