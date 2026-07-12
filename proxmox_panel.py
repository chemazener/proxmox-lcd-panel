#!/usr/bin/env python3
"""
LCD 3.5" (USB, /dev/ttyACM0): dashboard de máquinas Proxmox activas.

Pensado como reemplazo de un tema de turing-smart-screen-python: este script debe
colocarse DENTRO de un checkout de ese proyecto (usa su `library/` y su venv).
Render PIL completo 320x480 portrait, refresco cada 2 s.

Config por variables de entorno: LCD_PORT (def /dev/ttyACM0), LCD_BRIGHTNESS,
LCD_PERIOD_S, PVE_NODE (nombre del nodo Proxmox, def "pve").

Por máquina (CT o VM 'running'):
  - %CPU      : `cpu` de pvesh status/current (ya viene 0-1, normalizado a cores).
  - %MEM      : mem/maxmem (clamp 0-100).
  - %NET      : (delta(netin+netout)/Δt) sobre 1 Gbps (vmbr0 nominal).
  - %GPU(SM)  : suma de sm% de los procesos asociados al cgroup de la maquina
                en `nvidia-smi pmon -s u`. 0 si la 3080 esta en VFIO o si la
                maquina no toca la GPU NVIDIA.

Interacción: rueda del ratón del host (evdev, hot-plug) para cambiar de página
manualmente; el auto-paginado se pausa unos segundos tras usarla.
"""
import json
import os
import re
import select
import subprocess
import sys
import time
from collections import defaultdict
from datetime import datetime

# Este script vive dentro de un checkout de turing-smart-screen-python y usa su `library/`.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from library.lcd.lcd_comm_rev_a import LcdCommRevA, Orientation  # noqa
from PIL import Image, ImageDraw, ImageFont

try:
    import evdev
    from evdev import ecodes as _ec
except Exception:
    evdev = None
    _ec = None

PVE_NODE = os.environ.get("PVE_NODE", "pve")   # nombre del nodo Proxmox

W, H = 320, 480
HEADER_H = 38
FOOTER_H = 42
MARGIN = 6
GRID_Y = HEADER_H + 4
GRID_H = H - HEADER_H - FOOTER_H - 8
COLS = 2
CELL_GAP = 6
CELL_W = (W - 2 * MARGIN - CELL_GAP) // COLS   # ~151
PAGE_SIZE = 8                              # 2x4: tope antes de paginar
PAGE_DWELL_S = 5.0                         # tiempo por página cuando hay paginación
MANUAL_PAUSE_S = 15.0                      # pausa del auto-paginado tras tocar la rueda
NET_REF_MBPS = 1000                        # vmbr0 ~1 Gbps

FONT_SANS  = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
FONT_SANSB = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
FONT_MONO  = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"
FONT_MONOB = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf"

# ---- paleta ----
BG_TOP  = (13, 19, 36)
BG_BOT  = (6, 9, 18)
CARD    = (18, 26, 44)
CARD_BD = (34, 48, 73)
FG      = (229, 238, 247)
DIM     = (124, 138, 165)
FAINT   = (74, 86, 112)
SKY     = (56, 189, 248)     # CT
VIOLET  = (167, 139, 250)    # VM
GREEN   = (52, 211, 153)
AMBER   = (251, 191, 36)
RED     = (248, 113, 113)
TRACK   = (27, 39, 64)
INK     = (9, 14, 26)        # texto sobre badge claro


def run_json(cmd, timeout=8):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if r.returncode != 0:
            return None
        return json.loads(r.stdout)
    except Exception:
        return None


def get_machines():
    """Lista ordenada por VMID de las maquinas en 'running'.

    Para VMs, hacemos una llamada adicional a /status/current y, si el agent
    reporta `ballooninfo`, usamos `total_mem - free_mem` (RAM realmente usada
    dentro del guest) en lugar de `mem` (que es el balloon target y suele
    coincidir con maxmem -> falso 100%)."""
    out = []
    # CTs: el endpoint colectivo ya da mem/maxmem reales.
    cts = run_json(["pvesh", "get", f"/nodes/{PVE_NODE}/lxc", "--output-format", "json"]) or []
    for m in cts:
        if m.get("status") != "running":
            continue
        out.append({
            "kind":   "CT",
            "vmid":   int(m["vmid"]),
            "name":   str(m.get("name", "?")),
            "cpu":    float(m.get("cpu") or 0.0),
            "mem":    int(m.get("mem") or 0),
            "maxmem": int(m.get("maxmem") or 1),
            "netin":  int(m.get("netin") or 0),
            "netout": int(m.get("netout") or 0),
        })

    # VMs: detalle individual por status/current para leer ballooninfo.
    vms = run_json(["pvesh", "get", f"/nodes/{PVE_NODE}/qemu", "--output-format", "json"]) or []
    for v in vms:
        if v.get("status") != "running":
            continue
        vmid = int(v["vmid"])
        det = run_json(["pvesh", "get", f"/nodes/{PVE_NODE}/qemu/{vmid}/status/current",
                        "--output-format", "json"], timeout=5) or v
        mem = int(det.get("mem") or 0)
        maxmem = int(det.get("maxmem") or 1)
        b = det.get("ballooninfo") or {}
        total_mem = int(b.get("total_mem") or 0)
        free_mem  = int(b.get("free_mem") or 0)
        if total_mem > 0 and free_mem >= 0:
            # uso real visto desde el guest via balloon driver
            mem = max(0, total_mem - free_mem)
            maxmem = total_mem
        out.append({
            "kind":   "VM",
            "vmid":   vmid,
            "name":   str(det.get("name") or v.get("name") or "?"),
            "cpu":    float(det.get("cpu") or 0.0),
            "mem":    mem,
            "maxmem": maxmem,
            "netin":  int(det.get("netin") or 0),
            "netout": int(det.get("netout") or 0),
        })
    out.sort(key=lambda x: x["vmid"])
    return out


# regex para localizar VMID en /proc/<pid>/cgroup
_CG_LXC  = re.compile(r"/lxc(?:\.payload)?[./](\d+)")
_CG_QEMU = re.compile(r"/(\d+)\.scope")


def _pid_to_vmid(pid):
    try:
        with open(f"/proc/{pid}/cgroup") as f:
            cg = f.read()
    except Exception:
        return None
    m = _CG_LXC.search(cg)
    if m:
        return int(m.group(1))
    m = _CG_QEMU.search(cg)
    if m:
        return int(m.group(1))
    return None


def get_gpu_per_vmid():
    """{vmid: sm_percent} sumando sm% por proceso. (None, msg) si nvidia-smi falla."""
    try:
        r = subprocess.run(["nvidia-smi", "pmon", "-c", "1", "-s", "u"],
                           capture_output=True, text=True, timeout=4)
    except Exception as e:
        return {}, f"GPU?: {e.__class__.__name__}"
    if r.returncode != 0:
        # Tipico cuando la 3080 esta bound a vfio-pci (VM Game running)
        return {}, "GPU VFIO"
    per = defaultdict(int)
    for line in r.stdout.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) < 4:
            continue
        try:
            pid = int(parts[1])
        except Exception:
            continue
        sm_raw = parts[3]
        try:
            sm = int(sm_raw) if sm_raw != "-" else 0
        except Exception:
            sm = 0
        vmid = _pid_to_vmid(pid)
        if vmid is not None:
            per[vmid] += sm
    return dict(per), None


def get_host():
    import psutil
    try:
        with open("/proc/uptime") as f:
            uptime_s = float(f.read().split()[0])
    except Exception:
        uptime_s = 0
    try:
        load = os.getloadavg()[0]
    except Exception:
        load = 0.0
    gpu_pct, gpu_used, gpu_total = -1, 0, 0
    try:
        r = subprocess.run(["nvidia-smi",
                            "--query-gpu=utilization.gpu,memory.used,memory.total",
                            "--format=csv,noheader,nounits"],
                           capture_output=True, text=True, timeout=4)
        if r.returncode == 0:
            u, used, total = (int(x.strip()) for x in r.stdout.strip().split(","))
            gpu_pct, gpu_used, gpu_total = u, used, total
    except Exception:
        pass
    return dict(
        cpu=psutil.cpu_percent(interval=None),
        mem=psutil.virtual_memory().percent,
        load=load,
        uptime=uptime_s,
        gpu=gpu_pct,
        gpu_used=gpu_used,
        gpu_total=gpu_total,
    )


# ---------- fuentes ----------
f_brand = ImageFont.truetype(FONT_SANSB, 19)
f_clock = ImageFont.truetype(FONT_MONOB, 17)
f_up    = ImageFont.truetype(FONT_SANS, 11)
f_badge = ImageFont.truetype(FONT_MONOB, 11)
f_name  = ImageFont.truetype(FONT_SANSB, 12)
f_lbl   = ImageFont.truetype(FONT_SANSB, 10)
f_val   = ImageFont.truetype(FONT_MONOB, 10)
f_foot  = ImageFont.truetype(FONT_MONOB, 11)
f_footb = ImageFont.truetype(FONT_SANSB, 11)


def color_for(pct):
    if pct >= 80: return RED
    if pct >= 50: return AMBER
    return GREEN


def _lerp(a, b, t):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


# ---- fondo/chrome cacheado (se construye una vez) ----
_CHROME = None


def _build_chrome():
    img = Image.new("RGB", (W, H), BG_BOT)
    d = ImageDraw.Draw(img)
    # degradado vertical
    for y in range(H):
        d.line([(0, y), (W, y)], fill=_lerp(BG_TOP, BG_BOT, y / (H - 1)))
    # banda de cabecera translúcida
    d.rounded_rectangle([MARGIN, 3, W - MARGIN, HEADER_H], radius=10, fill=(16, 24, 42))
    # línea de acento bajo la cabecera (degradado sky->violet)
    ly = HEADER_H + 1
    for x in range(MARGIN, W - MARGIN):
        t = (x - MARGIN) / (W - 2 * MARGIN)
        d.line([(x, ly), (x, ly + 1)], fill=_lerp(SKY, VIOLET, t))
    # pastilla de footer
    d.rounded_rectangle([MARGIN, H - FOOTER_H, W - MARGIN, H - 3],
                        radius=10, fill=(15, 22, 40), outline=CARD_BD, width=1)
    return img


def _chrome():
    global _CHROME
    if _CHROME is None:
        _CHROME = _build_chrome()
    return _CHROME.copy()


def _layout_grid(n_visible):
    if n_visible <= 2:
        rows = 1
    elif n_visible <= 4:
        rows = 2
    elif n_visible <= 6:
        rows = 3
    else:
        rows = 4
    return rows, GRID_H // rows


def _draw_stat(d, x, y, w, rowh, label, pct):
    """Una fila: etiqueta | pista redondeada con relleno | valor."""
    pct = max(0.0, min(100.0, pct))
    lblw, valw = 30, 34
    bh = min(rowh - 3, 11)
    vy = y + (rowh - bh) // 2
    tx = x + lblw
    tw = w - lblw - valw
    col = color_for(pct)
    # etiqueta
    d.text((x, y + (rowh - 10) // 2 - 1), label, fill=DIM, font=f_lbl)
    # pista
    d.rounded_rectangle([tx, vy, tx + tw, vy + bh], radius=bh // 2, fill=TRACK)
    fw = int((tw - 2) * pct / 100)
    if fw >= 3:
        d.rounded_rectangle([tx + 1, vy + 1, tx + 1 + fw, vy + bh - 1],
                            radius=(bh - 2) // 2, fill=col)
    elif fw > 0:
        d.rectangle([tx + 1, vy + 1, tx + 1 + fw, vy + bh - 1], fill=col)
    # valor
    vtxt = f"{int(round(pct))}%"
    vw = d.textlength(vtxt, font=f_val)
    d.text((x + w - vw, y + (rowh - 10) // 2 - 1), vtxt, fill=FG, font=f_val)


def _draw_cell(d, x, y, w, h, m, net_rate_mb, gpu_pct):
    accent = SKY if m["kind"] == "CT" else VIOLET
    # tarjeta
    d.rounded_rectangle([x, y, x + w - 1, y + h - 1], radius=9,
                        fill=CARD, outline=CARD_BD, width=1)
    # franja de acento a la izquierda
    d.rounded_rectangle([x + 2, y + 3, x + 5, y + h - 4], radius=2, fill=accent)

    cpu_pct = m["cpu"] * 100
    mem_pct = (m["mem"] / m["maxmem"] * 100) if m["maxmem"] else 0
    net_pct = max(0.0, net_rate_mb * 8 / NET_REF_MBPS * 100)
    gpu_p   = gpu_pct

    # punto de estado (peor de cpu/mem)
    worst = max(cpu_pct, mem_pct)
    dot = color_for(worst)
    dr = 4
    d.ellipse([x + w - 12, y + 6, x + w - 12 + dr * 2, y + 6 + dr * 2], fill=dot)

    # badge [CT 101]
    tag = f"{m['kind']} {m['vmid']}"
    tw = d.textlength(tag, font=f_badge)
    bx0, by0 = x + 9, y + 5
    d.rounded_rectangle([bx0, by0, bx0 + tw + 10, by0 + 15], radius=5, fill=accent)
    d.text((bx0 + 5, by0 + 2), tag, fill=INK, font=f_badge)

    # nombre truncado
    name = (m["name"] or "?")
    nx = bx0 + tw + 16
    navail = (x + w - 16) - nx
    while name and d.textlength(name, font=f_name) > navail:
        name = name[:-1]
    d.text((nx, by0 + 2), name, fill=FG, font=f_name)

    # barras
    by = y + 24
    bh_area = h - 28
    rowh = max(11, bh_area // 4)
    bars = [("CPU", cpu_pct), ("MEM", mem_pct), ("NET", net_pct), ("GPU", gpu_p)]
    for i, (lbl, pct) in enumerate(bars):
        _draw_stat(d, x + 8, by + i * rowh, w - 16, rowh, lbl, pct)


def _page_dots(d, cx, y, n, cur):
    if n <= 1:
        return
    gap = 9
    total = (n - 1) * gap
    x0 = cx - total / 2
    for i in range(n):
        c = FG if i == cur else FAINT
        d.ellipse([x0 + i * gap - 2, y - 2, x0 + i * gap + 2, y + 2], fill=c)


def render(machines, host, net_rates, gpu_warning, page_idx=0):
    img = _chrome()
    d = ImageDraw.Draw(img)

    # ---- header ----
    now = datetime.now().strftime("%H:%M:%S")
    up_d = int(host["uptime"] // 86400)
    up_h = int((host["uptime"] % 86400) // 3600)
    d.ellipse([MARGIN + 8, 14, MARGIN + 18, 24], fill=SKY)       # punto marca
    d.text((MARGIN + 24, 9), "PROXMOX", fill=FG, font=f_brand)
    cw = d.textlength(now, font=f_clock)
    d.text((W - MARGIN - cw - 10, 11), now, fill=SKY, font=f_clock)
    up = f"{up_d}d {up_h}h"
    d.text((W - MARGIN - d.textlength(up, font=f_up) - 10, 26), up, fill=DIM, font=f_up)
    # movemos el reloj para no chocar con uptime
    # (uptime va bajo el reloj; ambos alineados a la derecha)

    # ---- mosaico ----
    total = len(machines)
    n_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    page_idx = page_idx % n_pages
    start = page_idx * PAGE_SIZE
    page = machines[start:start + PAGE_SIZE]

    if not page:
        d.text((W // 2, H // 2), "sin máquinas activas", fill=DIM,
               font=f_name, anchor="mm")
    else:
        rows, cell_h = _layout_grid(len(page))
        for i, m in enumerate(page):
            r = i // COLS
            c = i % COLS
            x = MARGIN + c * (CELL_W + CELL_GAP)
            y = GRID_Y + r * cell_h
            _draw_cell(d, x, y, CELL_W, cell_h - CELL_GAP, m,
                       net_rates.get(m["vmid"], 0), m.get("gpu_sm", 0))

    # ---- footer ----
    fy = H - FOOTER_H
    host_line = f"CPU {int(host['cpu']):3d}%   MEM {int(host['mem']):3d}%   L {host['load']:.2f}"
    d.text((MARGIN + 10, fy + 6), host_line, fill=FG, font=f_foot)
    if host["gpu"] >= 0:
        gpu_line = f"GPU {int(host['gpu']):3d}%   {host['gpu_used']/1024:.1f}/{host['gpu_total']/1024:.1f} GB"
        gcol = FG
    else:
        gpu_line = gpu_warning or "GPU --"
        gcol = AMBER
    d.text((MARGIN + 10, fy + 22), gpu_line, fill=gcol, font=f_foot)
    _page_dots(d, W - MARGIN - 34, fy + 27, n_pages, page_idx)
    if n_pages > 1:
        pn = f"{page_idx+1}/{n_pages}"
        d.text((W - MARGIN - d.textlength(pn, font=f_footb) - 12, fy + 6), pn,
               fill=DIM, font=f_footb)
    return img


# ---------- rueda del ratón (evdev, hot-plug) ----------
class MouseScroller:
    """Lee REL_WHEEL del ratón del host. Sin ratón (o sin evdev), no hace nada."""
    REFRESH_S = 30.0

    def __init__(self):
        self.devs = []
        self.fds = {}
        self.last_refresh = 0.0
        self._refresh()

    def _is_wheel(self, d):
        try:
            return _ec.REL_WHEEL in d.capabilities().get(_ec.EV_REL, [])
        except Exception:
            return False

    def _close(self):
        for d in self.devs:
            try:
                d.close()
            except Exception:
                pass

    def _refresh(self):
        self._close()
        self.devs = []
        if evdev is not None:
            for path in evdev.list_devices():
                try:
                    dev = evdev.InputDevice(path)
                except Exception:
                    continue
                if self._is_wheel(dev):
                    self.devs.append(dev)
                else:
                    try:
                        dev.close()
                    except Exception:
                        pass
        self.fds = {d.fd: d for d in self.devs}
        self.last_refresh = time.time()

    def wait(self, deadline):
        """Bloquea hasta `deadline`; devuelve el desplazamiento neto de rueda (int).
        Reacciona en cuanto llega un evento de rueda."""
        if evdev is None:
            t = deadline - time.time()
            if t > 0:
                time.sleep(t)
            return 0
        if time.time() - self.last_refresh > self.REFRESH_S:
            self._refresh()
        net = 0
        while True:
            timeout = deadline - time.time()
            if timeout <= 0:
                return net
            try:
                r, _, _ = select.select(list(self.fds.keys()), [], [], timeout)
            except (OSError, ValueError):
                self._refresh()
                return net
            if not r:
                return net
            for fd in r:
                dev = self.fds.get(fd)
                if dev is None:
                    continue
                try:
                    for ev in dev.read():
                        if ev.type == _ec.EV_REL and ev.code == _ec.REL_WHEEL:
                            net += ev.value
                except (OSError, BlockingIOError):
                    self.last_refresh = 0.0
            if net != 0:
                return net


def main():
    com = os.environ.get("LCD_PORT", "/dev/ttyACM0")
    lcd = LcdCommRevA(com_port=com, display_width=W, display_height=H)
    lcd.Reset()
    lcd.InitializeComm()
    lcd.Clear()
    lcd.SetOrientation(Orientation.PORTRAIT)
    lcd.SetBrightness(int(os.environ.get("LCD_BRIGHTNESS", "80")))

    period = float(os.environ.get("LCD_PERIOD_S", "2.0"))
    prev_net = {}        # vmid -> (bytes_total, ts)
    page_idx = 0
    page_since = time.time()
    manual_until = 0.0
    scroller = MouseScroller()
    last = None          # cache: (machines, host, net_rates, gpu_warning)
    next_fetch = 0.0

    while True:
        now = time.time()
        # ---- recogida de datos a ritmo `period` ----
        if last is None or now >= next_fetch:
            try:
                machines = get_machines()
                gpu_per_vmid, gpu_warning = get_gpu_per_vmid()
                host = get_host()

                tnow = time.time()
                net_rates = {}
                seen = set()
                for m in machines:
                    seen.add(m["vmid"])
                    tot = m["netin"] + m["netout"]
                    prev = prev_net.get(m["vmid"])
                    if prev:
                        dt = tnow - prev[1]
                        if dt > 0:
                            net_rates[m["vmid"]] = max(0.0, (tot - prev[0]) / dt / 1e6)
                    prev_net[m["vmid"]] = (tot, tnow)
                    m["gpu_sm"] = gpu_per_vmid.get(m["vmid"], 0)
                for vmid in list(prev_net):
                    if vmid not in seen:
                        del prev_net[vmid]
                last = (machines, host, net_rates, gpu_warning)
            except KeyboardInterrupt:
                break
            except Exception as e:
                print(f"[panel] tick error: {e!r}", file=sys.stderr, flush=True)
            next_fetch = time.time() + period

        if last is None:
            time.sleep(period)
            continue

        machines, host, net_rates, gpu_warning = last
        n_pages = max(1, (len(machines) + PAGE_SIZE - 1) // PAGE_SIZE)

        # ---- auto-paginado (salvo override manual reciente) ----
        if (n_pages > 1 and time.time() >= manual_until
                and (time.time() - page_since) >= PAGE_DWELL_S):
            page_idx = (page_idx + 1) % n_pages
            page_since = time.time()
        page_idx %= n_pages

        try:
            img = render(machines, host, net_rates, gpu_warning, page_idx=page_idx)
            lcd.DisplayPILImage(img)
        except KeyboardInterrupt:
            break
        except Exception as e:
            print(f"[panel] render error: {e!r}", file=sys.stderr, flush=True)

        # ---- esperar al próximo refresco reaccionando a la rueda ----
        moved = scroller.wait(next_fetch)
        if moved and n_pages > 1:
            # rueda arriba (+1) -> página anterior ; abajo (-1) -> siguiente
            page_idx = (page_idx - moved) % n_pages
            manual_until = time.time() + MANUAL_PAUSE_S
            page_since = time.time()


if __name__ == "__main__":
    main()
