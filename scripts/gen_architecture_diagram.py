#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Draw the DevOps Hub architecture / trust-boundary diagram as a 16:9 PNG.

    python scripts/gen_architecture_diagram.py [-o docs/architecture-diagram.png]

Palette is the portal's own light theme (Catppuccin Latte), converted from the
HSL custom properties in frontend/static/css/theme.css so the diagram and the
UI cannot drift apart. Labels are Latin only: the component names are English
anyway, and it keeps the image clear of the RTL shaping PIL does not do.

Two things this file is careful about, both learned the hard way:
  * shadows are composited BEFORE the cards are drawn, or the blur veils every
    card and the whole diagram comes out grey;
  * every label is measured and shrunk to its container, or the long ones spill
    across the box edge and over their neighbours.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

# ── Latte, converted from theme.css ────────────────────────────────────────
B1 = (249, 249, 251)    # card
B2 = (243, 244, 247)    # page behind the cards
B3 = (223, 225, 231)    # borders
BC = (71, 73, 98)       # body text
P = (77, 123, 213)      # primary
PF = (47, 99, 202)      # primary, darker
S = (150, 100, 216)     # secondary
A = (49, 150, 155)      # accent
N = (108, 111, 132)     # neutral
WA = (217, 142, 38)     # warning
ER = (217, 38, 77)      # error
BOUND = (237, 239, 244)  # the namespace ground, a shade under base-200

W, H = 2400, 1350       # 16:9

FONT_DIRS = [Path(r"C:\Windows\Fonts"), Path("/usr/share/fonts/truetype/dejavu")]
FACES = {
    "bold": ["segoeuib.ttf", "arialbd.ttf", "DejaVuSans-Bold.ttf"],
    "semi": ["segoeuisb.ttf", "segoeuib.ttf", "arialbd.ttf", "DejaVuSans-Bold.ttf"],
    "reg": ["segoeui.ttf", "arial.ttf", "DejaVuSans.ttf"],
}
_cache: dict[tuple[str, int], ImageFont.FreeTypeFont] = {}


def font(kind: str, size: int) -> ImageFont.FreeTypeFont:
    key = (kind, size)
    if key not in _cache:
        for name in FACES[kind]:
            for dirp in FONT_DIRS:
                p = dirp / name
                if p.exists():
                    _cache[key] = ImageFont.truetype(str(p), size)
                    return _cache[key]
        _cache[key] = ImageFont.load_default()
    return _cache[key]


# ── text helpers ───────────────────────────────────────────────────────────

def fit(d, text: str, kind: str, max_w: int, sizes: list[int]):
    """Largest of `sizes` at which `text` still fits `max_w`."""
    for s in sizes:
        f = font(kind, s)
        if d.textlength(text, font=f) <= max_w:
            return f
    return font(kind, sizes[-1])


def wrap(d, text: str, f, max_w: int) -> list[str]:
    lines, cur = [], ""
    for word in text.split():
        trial = f"{cur} {word}".strip()
        if d.textlength(trial, font=f) <= max_w or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = word
    if cur:
        lines.append(cur)
    return lines


def centre(d, cx, cy, text, f, fill):
    """Centre on a POINT using font metrics.

    PIL's default anchor is the ascender line, which differs per size, so a
    stack of mixed-size lines drifts off the card's midline however carefully
    the heights are added up. anchor="mm" centres on the real glyph box.
    """
    d.text((cx, cy), text, font=f, fill=fill, anchor="mm")


def uniform_title_size(d, cards: list[tuple[tuple, str]], pad: int = 64) -> int:
    """One title size for every card — the largest that fits them all.

    Sizing each card independently made "Route" and "PostgreSQL" render smaller
    than "nginx" beside them, which reads as a mistake rather than as fitting.
    """
    for size in range(44, 23, -2):
        f = font("semi", size)
        if all(d.textlength(t, font=f) <= (b[2] - b[0]) - pad for b, t in cards):
            return size
    return 24


# ── shapes ─────────────────────────────────────────────────────────────────

TITLE_LH, PORT_LH, DESC_LH = 56, 42, 36


def node(d, box, title, port=None, desc=None, accent=P, title_size=40):
    x0, y0, x1, y1 = box
    inner = (x1 - x0) - 64
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2

    d.rounded_rectangle(box, radius=22, fill=B1, outline=B3, width=3)
    d.rounded_rectangle([x0, y0 + 14, x0 + 11, y1 - 14], radius=6, fill=accent)

    lines = [(title, font("semi", title_size), BC, TITLE_LH)]
    if port:
        lines.append((port, font("reg", 28), N, PORT_LH))
    if desc:
        f = font("reg", 26)
        lines += [(ln, f, N, DESC_LH) for ln in wrap(d, desc, f, inner)]

    y = cy - sum(lh for *_, lh in lines) / 2
    for text, f, colour, lh in lines:
        centre(d, cx, y + lh / 2, text, f, colour)
        y += lh


def dashed(d, x0, y0, x1, y1, colour, width, on=22, off=15):
    total = ((x1 - x0) ** 2 + (y1 - y0) ** 2) ** 0.5
    if not total:
        return
    ux, uy = (x1 - x0) / total, (y1 - y0) / total
    t = 0.0
    while t < total:
        e = min(t + on, total)
        d.line([x0 + ux * t, y0 + uy * t, x0 + ux * e, y0 + uy * e],
               fill=colour, width=width)
        t = e + off


def arrow_h(d, x0, x1, y, colour=N, width=5, head=18):
    d.line([x0, y, x1 - head, y], fill=colour, width=width)
    s = head if x1 > x0 else -head
    d.polygon([(x1, y), (x1 - s, y - head * 0.7), (x1 - s, y + head * 0.7)],
              fill=colour)


def arrow_v(d, y0, y1, x, colour=N, width=5, head=18):
    d.line([x, y0, x, y1 - head], fill=colour, width=width)
    d.polygon([(x, y1), (x - head * 0.7, y1 - head), (x + head * 0.7, y1 - head)],
              fill=colour)


def hop_label(d, cx, y, text, colour, tick_to=None):
    """Short caption above its arrow, clear of every card.

    The gaps between cards are narrower than the pills, so the label sits above
    the row and a hairline drops onto the arrow it describes — without it the
    three captions read as floating and it is guesswork which hop each covers.
    """
    f = font("semi", 27)
    tw = d.textlength(text, font=f)
    if tick_to is not None:
        d.line([cx, y + 44, cx, tick_to], fill=colour, width=2)
    d.rounded_rectangle([cx - tw / 2 - 16, y, cx + tw / 2 + 16, y + 44],
                        radius=22, fill=B1, outline=colour, width=3)
    d.text((cx, y + 22), text, font=f, fill=colour, anchor="mm")


# ── the drawing ────────────────────────────────────────────────────────────

BROWSER = (90, 450, 420, 700)
ROUTE = (570, 450, 850, 700)
NGINX = (910, 450, 1190, 700)
API = (1250, 450, 1690, 700)
PG = (860, 860, 1200, 1050)
REDIS = (1360, 860, 1700, 1050)
EXT = (1810, 300, 2310, 1060)
BOUNDARY = (510, 260, 1750, 1100)


def build(out: Path) -> Path:
    img = Image.new("RGBA", (W, H), B2 + (255,))

    # Shadows go down first and get composited under everything, otherwise the
    # blur sits on top of the cards and greys the entire image out.
    shadow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    sd = ImageDraw.Draw(shadow)
    for box in (BROWSER, ROUTE, NGINX, API, PG, REDIS, EXT):
        sd.rounded_rectangle([box[0] + 3, box[1] + 9, box[2] + 3, box[3] + 11],
                             radius=22, fill=(120, 124, 145, 52))
    img = Image.alpha_composite(img, shadow.filter(ImageFilter.GaussianBlur(10)))

    img = img.convert("RGB")
    d = ImageDraw.Draw(img)

    # heading
    d.text((90, 62), "DevOps Hub", font=font("bold", 58), fill=BC)
    d.text((92, 136),
           "Architecture and trust boundaries",
           font=font("reg", 30), fill=N)
    d.line([90, 198, W - 90, 198], fill=B3, width=3)

    # the namespace, behind the cards
    d.rounded_rectangle(BOUNDARY, radius=26, fill=BOUND)
    x0, y0, x1, y1 = BOUNDARY
    for a, b in (((x0, y0), (x1, y0)), ((x1, y0), (x1, y1)),
                 ((x1, y1), (x0, y1)), ((x0, y1), (x0, y0))):
        dashed(d, *a, *b, N, 4)
    d.text((x0 + 34, y0 + 26),
           "OpenShift  ·  ONE namespace — production and test share it",
           font=font("semi", 28), fill=N)

    # nodes — descriptions kept to one or two lines so nothing crowds an edge
    cards = [
        (BROWSER, "Browser", None, "server-rendered HTML", N),
        (ROUTE, "Route", None, "the only way in", P),
        (NGINX, "nginx", ":3000", "reverse proxy · security headers", P),
        (API, "FastAPI", ":8000", "renders the UI · serves /api", PF),
        (PG, "PostgreSQL", ":5432", "users · audit · tokens", A),
        (REDIS, "Redis", ":6379", "60-second cache", A),
    ]
    tsize = uniform_title_size(d, [(b, t) for b, t, _, _, _ in cards])
    for box, title, port, desc, accent in cards:
        node(d, box, title, port, desc, accent=accent, title_size=tsize)

    # external systems
    d.rounded_rectangle(EXT, radius=22, fill=B1, outline=B3, width=3)
    d.rounded_rectangle([EXT[0], EXT[1] + 14, EXT[0] + 11, EXT[3] - 14],
                        radius=6, fill=S)
    ecx = (EXT[0] + EXT[2]) / 2
    centre(d, ecx, EXT[1] + 60, "Internal systems", font("semi", tsize), BC)
    centre(d, ecx, EXT[1] + 108, "reached by the backend only", font("reg", 26), N)
    for i, name in enumerate(["Azure DevOps Server", "ServiceNow", "SonarQube",
                              "Artifactory", "Confluence", "RedHat SSO"]):
        ty = EXT[1] + 160 + i * 96
        d.rounded_rectangle([EXT[0] + 42, ty, EXT[2] - 42, ty + 76],
                            radius=14, fill=B2, outline=B3, width=2)
        centre(d, ecx, ty + 38, name, font("reg", 30), BC)

    # flow along the request path
    mid = (BROWSER[1] + BROWSER[3]) / 2
    egress = mid - 60
    arrow_h(d, BROWSER[2] + 14, ROUTE[0] - 8, mid)
    arrow_h(d, ROUTE[2] + 14, NGINX[0] - 8, mid)
    arrow_h(d, NGINX[2] + 14, API[0] - 8, mid)
    arrow_h(d, API[2] + 14, EXT[0] - 8, egress)

    hop_label(d, (BROWSER[2] + ROUTE[0]) / 2, 348, "HTTPS", P, tick_to=mid - 12)
    hop_label(d, (NGINX[2] + API[0]) / 2, 348, "HTTP", WA, tick_to=mid - 12)
    hop_label(d, (API[2] + EXT[0]) / 2 + 4, 348, "HTTPS", ER, tick_to=egress - 12)

    # backend down to the two stores
    fan = (API[3] + PG[1]) / 2
    api_cx = (API[0] + API[2]) / 2
    pg_cx = (PG[0] + PG[2]) / 2
    rd_cx = (REDIS[0] + REDIS[2]) / 2
    d.line([api_cx, API[3], api_cx, fan], fill=N, width=5)
    d.line([pg_cx, fan, rd_cx, fan], fill=N, width=5)
    arrow_v(d, fan, PG[1] - 8, pg_cx)
    arrow_v(d, fan, REDIS[1] - 8, rd_cx)

    # notes
    notes = [
        (WA, "TLS terminates at the Route. The hop from nginx to the backend is "
             "plaintext HTTP, inside the namespace."),
        (ER, "Certificate verification is disabled on every outbound integration "
             "call (INTEGRATION_TLS_VERIFY=false)."),
        (ER, "Redis runs without a password, and one namespace holds both "
             "production and test."),
        (BC, "The browser never calls an internal system directly. Every "
             "integration call is server-side, and no credential reaches the client."),
    ]
    d.rounded_rectangle([90, 1120, W - 90, 1310], radius=18,
                        fill=B1, outline=B3, width=3)
    f_note = font("reg", 27)
    for i, (colour, text) in enumerate(notes):
        y = 1146 + i * 42
        d.ellipse([126, y + 9, 144, y + 27], fill=colour)
        d.text((166, y), text, font=f_note, fill=BC)

    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out, "PNG", optimize=True)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("-o", "--out", default="docs/architecture-diagram.png")
    args = ap.parse_args()
    p = build(Path(args.out))
    print(f"wrote {p} ({p.stat().st_size // 1024} KB, {W}x{H})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
