"""Telemaxxing - Telegram profile-battle bot.

Render-safe layout:
  * Flask health server runs in a daemon thread (binds to $PORT)
  * python-telegram-bot polling runs on the MAIN thread
  * every startup problem prints a clear message instead of dying silently

Env vars:  BOT_TOKEN (required), PORT (set by Render), FONT_PATH (optional)
Single file: images + bot together (no images.py needed).
requirements.txt:
    python-telegram-bot==21.6
    Flask==3.1.3
    Pillow
"""
import asyncio
import hashlib
import io
import logging
import math
import os
import random
import sys
import threading
import time

from flask import Flask
from pathlib import Path
from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps, ImageStat
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, User
from telegram.constants import ChatType
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)


# ======================================================================
# IMAGE GENERATION
# ======================================================================
W, H = 1280, 720
GOLD = (255, 200, 40)
CYAN = (0, 210, 255)
PINK = (255, 60, 180)
GREEN = (120, 255, 60)
RED = (255, 50, 50)
WHITE = (255, 255, 255)
GREY = (170, 178, 195)

AURA_MAX = 15
MAXES = {
    "photos": 50,
    "username": 15,
    "premium": 25,
    "bio": 10,
    "age": 30,
    "quality": 30,
    "aura": AURA_MAX,
}
ROWS = [
    ("photos", "PROFILE PICS"),
    ("username", "USERNAME"),
    ("premium", "PREMIUM"),
    ("bio", "BIO"),
    ("age", "ACCOUNT AGE"),
    ("quality", "PIC QUALITY"),
    ("aura", "AURA"),
]

TIERS = [
    (50, "NPC", (150, 158, 178)),
    (80, "SIDE CHARACTER", (110, 230, 140)),
    (110, "MAIN CHARACTER", CYAN),
    (140, "CERTIFIED MOGGER", PINK),
]
FINAL_BOSS_COLOR = GOLD

BASE_DIR = Path(__file__).resolve().parent
# Drop a bold .ttf next to this file named font.ttf (Anton / Bebas Neue /
# Montserrat ExtraBold from Google Fonts look great) or set FONT_PATH.
FONT_CANDIDATES = [
    os.environ.get("FONT_PATH", ""),
    str(BASE_DIR / "font.ttf"),
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
    "DejaVuSans-Bold.ttf",
    "arialbd.ttf",
]
_FONTS = {}


def font(size):
    size = int(size)
    if size in _FONTS:
        return _FONTS[size]
    f = None
    for p in FONT_CANDIDATES:
        if not p:
            continue
        try:
            f = ImageFont.truetype(p, size)
            break
        except OSError:
            continue
    if f is None:
        try:
            f = ImageFont.load_default(size)  # Pillow >= 10.1
        except TypeError:
            f = ImageFont.load_default()
    _FONTS[size] = f
    return f


def fit_font(text, max_w, start, minimum=16):
    """Largest font (<= start) whose rendered text fits in max_w pixels."""
    size = start
    while size > minimum:
        f = font(size)
        if f.getlength(text) <= max_w:
            return f
        size -= 2
    return font(minimum)


def img_safe(text, fallback="Player"):
    """Drop emoji / characters outside the BMP that fonts can't draw."""
    cleaned = "".join(ch for ch in (text or "") if ord(ch) <= 0xFFFF).strip()
    return cleaned or fallback


def tier_for(score):
    for limit, name, _ in TIERS:
        if score < limit:
            return name
    return "FINAL BOSS"


def tier_color(name):
    for _, n, c in TIERS:
        if n == name:
            return c
    return FINAL_BOSS_COLOR


# ------------------------------------------------------------ pfp analysis
def pic_quality(img):
    """0-30 points from how sharp / well lit / colourful the picture is."""
    if img is None:
        return 0
    try:
        small = ImageOps.fit(img.convert("RGB"), (256, 256))
        gray = small.convert("L")
        sharp = ImageStat.Stat(gray.filter(ImageFilter.FIND_EDGES)).stddev[0]
        bright = ImageStat.Stat(gray).mean[0]
        sat = ImageStat.Stat(small.convert("HSV")).mean[1]
    except Exception:
        return 0
    pts = (
        min(sharp / 35, 1) * 12
        + max(0, 1 - abs(bright - 135) / 135) * 8
        + min(sat / 140, 1) * 10
    )
    return int(round(pts))


# ------------------------------------------------------------ primitives
def gradient(size, top, bottom):
    w, h = size
    mask = Image.linear_gradient("L").resize((w, h))
    return Image.composite(Image.new("RGB", size, bottom), Image.new("RGB", size, top), mask)


def hgradient(size, left, right):
    w, h = size
    mask = Image.linear_gradient("L").rotate(90).resize((w, h))
    # rotate(90) makes white on the left; flip so white is on the right
    mask = ImageOps.mirror(mask)
    return Image.composite(Image.new("RGB", size, right), Image.new("RGB", size, left), mask)


def lighten(color, k):
    return tuple(min(255, int(c + (255 - c) * k)) for c in color)


def darken(color, k):
    return tuple(int(c * k) for c in color)


team_dim = darken


def blob(img, center, radius, color, alpha):
    layer = Image.new("RGBA", img.size, color + (0,))
    cx, cy = center
    ImageDraw.Draw(layer).ellipse(
        (cx - radius, cy - radius, cx + radius, cy + radius), fill=color + (alpha,)
    )
    img.alpha_composite(layer.filter(ImageFilter.GaussianBlur(radius // 2)))


def aa_shape(img, box, painter, ss=4, pad=6):
    """Draw anti-aliased vector shapes: painter(draw, scale, ox, oy) draws on a
    supersampled layer covering `box`, then it's downsampled and composited."""
    x0, y0, x1, y1 = [int(v) for v in box]
    x0 -= pad; y0 -= pad; x1 += pad; y1 += pad
    w, h = x1 - x0, y1 - y0
    layer = Image.new("RGBA", (w * ss, h * ss), (0, 0, 0, 0))
    painter(ImageDraw.Draw(layer), ss, x0, y0)
    layer = layer.resize((w, h), Image.LANCZOS)
    img.alpha_composite(layer, (x0, y0))


def aa_ellipse(img, box, fill=None, outline=None, width=1):
    def paint(d, s, ox, oy):
        b = [(box[0] - ox) * s, (box[1] - oy) * s, (box[2] - ox) * s, (box[3] - oy) * s]
        d.ellipse(b, fill=fill, outline=outline, width=int(width * s))
    aa_shape(img, box, paint)


def aa_rrect(img, box, radius, fill=None, outline=None, width=1):
    def paint(d, s, ox, oy):
        b = [(box[0] - ox) * s, (box[1] - oy) * s, (box[2] - ox) * s, (box[3] - oy) * s]
        d.rounded_rectangle(b, radius * s, fill=fill, outline=outline, width=int(width * s))
    aa_shape(img, box, paint)


def glow_ring(img, box, color, width, blur=16):
    layer = Image.new("RGBA", img.size, color + (0,))
    ImageDraw.Draw(layer).ellipse(box, outline=color + (255,), width=width)
    img.alpha_composite(layer.filter(ImageFilter.GaussianBlur(blur)))


def glow_text(img, xy, text, f, color, blur=12, fill=WHITE, stroke=0, anchor="mm"):
    layer = Image.new("RGBA", img.size, color + (0,))
    ImageDraw.Draw(layer).text(xy, text, font=f, fill=color + (255,), anchor=anchor, stroke_width=6)
    img.alpha_composite(layer.filter(ImageFilter.GaussianBlur(blur)))
    ImageDraw.Draw(img).text(
        xy, text, font=f, fill=fill, anchor=anchor, stroke_width=stroke, stroke_fill=(0, 0, 0)
    )


def panel(img, box, radius=28, fill=(8, 10, 24, 170), outline=(255, 255, 255, 50)):
    # soft drop shadow
    sh = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ImageDraw.Draw(sh).rounded_rectangle(
        (box[0], box[1] + 10, box[2], box[3] + 10), radius, fill=(0, 0, 0, 120)
    )
    img.alpha_composite(sh.filter(ImageFilter.GaussianBlur(14)))
    aa_rrect(img, box, radius, fill=fill, outline=outline, width=2)
    # top highlight
    hl = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ImageDraw.Draw(hl).rounded_rectangle(
        (box[0] + 2, box[1] + 2, box[2] - 2, box[1] + 46), radius - 2, fill=(255, 255, 255, 14)
    )
    img.alpha_composite(hl)


def sparkle(d, x, y, r, color):
    pts = [(x, y - r), (x + r * 0.22, y - r * 0.22), (x + r, y), (x + r * 0.22, y + r * 0.22),
           (x, y + r), (x - r * 0.22, y + r * 0.22), (x - r, y), (x - r * 0.22, y - r * 0.22)]
    d.polygon(pts, fill=color)


# ------------------------------------------------------------ background
_BG_CACHE = {}


def background():
    if "bg" in _BG_CACHE:
        return _BG_CACHE["bg"].copy()
    bg = gradient((W, H), (8, 10, 26), (46, 12, 66)).convert("RGBA")

    # diagonal light streaks
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ld = ImageDraw.Draw(layer)
    for x in range(-H, W, 90):
        ld.line([(x, H), (x + H, 0)], fill=(255, 255, 255, 9), width=28)
    bg.alpha_composite(layer)

    # dot grid
    dots = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    dd = ImageDraw.Draw(dots)
    for gx in range(20, W, 40):
        for gy in range(20, H, 40):
            dd.ellipse((gx - 1, gy - 1, gx + 1, gy + 1), fill=(255, 255, 255, 22))
    bg.alpha_composite(dots)

    blob(bg, (210, 360), 400, CYAN, 85)
    blob(bg, (1070, 360), 400, PINK, 85)
    blob(bg, (640, 700), 360, (90, 40, 200), 70)

    # sparkles
    rnd = random.Random(7)
    sp = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    sd = ImageDraw.Draw(sp)
    for _ in range(26):
        sparkle(sd, rnd.randint(20, W - 20), rnd.randint(20, H - 20), rnd.randint(4, 10),
                rnd.choice([WHITE, CYAN, PINK, GOLD]) + (rnd.randint(70, 170),))
    bg.alpha_composite(sp)

    # vignette
    vig = Image.radial_gradient("L").resize((W, H))
    dark = Image.new("RGBA", (W, H), (0, 0, 0, 255))
    dark.putalpha(vig.point(lambda v: int(max(0, v - 110) * 1.1)))
    bg.alpha_composite(dark)

    _BG_CACHE["bg"] = bg
    return bg.copy()


# ------------------------------------------------------------ avatar bits
def circle_crop(img, size):
    img = ImageOps.fit(img.convert("RGB"), (size, size))
    mask = Image.new("L", (size * 4, size * 4), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, size * 4 - 1, size * 4 - 1), fill=255)
    mask = mask.resize((size, size), Image.LANCZOS)
    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    out.paste(img.convert("RGBA"), (0, 0), mask)
    return out


def placeholder(size, letter):
    base = gradient((size, size), (78, 92, 140), (28, 32, 58))
    ImageDraw.Draw(base).text(
        (size / 2, size / 2), letter, font=font(int(size * 0.5)), fill=(215, 222, 240), anchor="mm"
    )
    return base


def crown(img, cx, y, w=120, h=58):
    def paint(d, s, ox, oy):
        def P(px, py):
            return ((px - ox) * s, (py - oy) * s)
        x0, x1 = cx - w / 2, cx + w / 2
        pts = [P(x0, y + h), P(x0, y + h * 0.25), P(cx - w / 4, y + h * 0.62), P(cx, y),
               P(cx + w / 4, y + h * 0.62), P(x1, y + h * 0.25), P(x1, y + h)]
        d.polygon(pts, fill=GOLD, outline=(160, 110, 0), width=3 * s)
        d.rectangle([*P(x0, y + h - 11), *P(x1, y + h)], fill=(225, 150, 20))
        for px, py, col in ((x0, y + h * 0.25, PINK), (cx, y, CYAN), (x1, y + h * 0.25, PINK)):
            a, b = P(px - 8, py - 8), P(px + 8, py + 8)
            d.ellipse([*a, *b], fill=col, outline=(255, 255, 255), width=2 * s)
    aa_shape(img, (cx - w / 2, y, cx + w / 2, y + h), paint, pad=14)


def rays(img, cx, cy, seed):
    """Golden god-rays behind the winner."""
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ld = ImageDraw.Draw(layer)
    n = 18
    for i in range(n):
        a0 = (i / n) * 2 * math.pi
        a1 = a0 + math.pi / n * 0.8
        r = 330
        ld.polygon([(cx, cy), (cx + math.cos(a0) * r, cy + math.sin(a0) * r),
                    (cx + math.cos(a1) * r, cy + math.sin(a1) * r)], fill=GOLD + (30,))
    img.alpha_composite(layer.filter(ImageFilter.GaussianBlur(3)))


def confetti(img, cx, cy, seed):
    rnd = random.Random(seed)
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    for _ in range(60):
        ang = rnd.uniform(0, 2 * math.pi)
        r = rnd.uniform(170, 245)
        x, y = cx + math.cos(ang) * r, cy + math.sin(ang) * r * 0.92
        col = rnd.choice([GOLD, CYAN, PINK, GREEN, WHITE])
        s = rnd.randint(7, 15)
        piece = Image.new("RGBA", (s * 2, s * 2), (0, 0, 0, 0))
        ImageDraw.Draw(piece).rectangle((s * 0.4, s * 0.7, s * 1.6, s * 1.3), fill=col + (240,))
        piece = piece.rotate(rnd.uniform(0, 360), resample=Image.BICUBIC)
        layer.alpha_composite(piece, (int(x - s), int(y - s)))
    sd = ImageDraw.Draw(layer)
    for _ in range(10):
        ang = rnd.uniform(0, 2 * math.pi)
        r = rnd.uniform(150, 260)
        sparkle(sd, cx + math.cos(ang) * r, cy + math.sin(ang) * r, rnd.randint(8, 15), WHITE + (230,))
    img.alpha_composite(layer)


def mogged_banner(width):
    bw, bh = width, 104
    ss = 2
    layer = Image.new("RGBA", (bw * ss, bh * ss), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    d.rectangle([0, 0, bw * ss, bh * ss], fill=GREEN + (255,))
    d.rectangle([0, 0, bw * ss, 8 * ss], fill=(20, 20, 20, 255))
    d.rectangle([0, (bh - 8) * ss, bw * ss, bh * ss], fill=(20, 20, 20, 255))
    f = fit_font("MOGGED!", (bw - 40) * ss, 72 * ss, 20)
    d.text((bw * ss / 2, bh * ss / 2), "MOGGED!", font=f, fill=RED, anchor="mm",
           stroke_width=4 * ss, stroke_fill=(0, 0, 0))
    layer = layer.resize((bw, bh), Image.LANCZOS)
    return layer.rotate(12, expand=True, resample=Image.BICUBIC)


def pill(img, cx, cy, text, color, f):
    tw = f.getlength(text)
    w, h = int(tw + 40), int(f.size + 18)
    box = (cx - w // 2, cy - h // 2, cx + w // 2, cy + h // 2)
    aa_rrect(img, box, h // 2, fill=darken(color, 0.25) + (235,), outline=color + (255,), width=2)
    ImageDraw.Draw(img).text((cx, cy), text, font=f, fill=lighten(color, 0.25), anchor="mm")


# ------------------------------------------------------------ battle card
def draw_fighter(img, cx, color, p, is_winner, seed):
    S, y = 300, 170
    x = cx - S // 2
    if is_winner:
        rays(img, cx, y + S // 2, seed)
        confetti(img, cx, y + S // 2, seed)
        glow_ring(img, (x - 14, y - 14, x + S + 14, y + S + 14), GOLD, 16, blur=22)
    glow_ring(img, (x - 8, y - 8, x + S + 8, y + S + 8), color, 10)

    name = p.get("name") or "Player"
    pic = p.get("pfp") or placeholder(S, (name[:1] or "?").upper())
    pic = ImageOps.fit(pic.convert("RGB"), (S, S))
    if not is_winner:
        pic = Image.blend(ImageOps.grayscale(pic).convert("RGB"),
                          Image.new("RGB", pic.size, (0, 0, 0)), 0.38)
    img.alpha_composite(circle_crop(pic, S), (x, y))

    ring = GOLD if is_winner else color
    aa_ellipse(img, (x - 7, y - 7, x + S + 7, y + S + 7), outline=ring + (255,), width=7)
    aa_ellipse(img, (x + 2, y + 2, x + S - 2, y + S - 2), outline=(255, 255, 255, 40), width=2)

    if is_winner:
        crown(img, cx, y - 70)
    else:
        banner = mogged_banner(S + 30)
        img.alpha_composite(banner, (cx - banner.width // 2, y + S // 2 - banner.height // 2))

    d = ImageDraw.Draw(img)
    shown = name[:16]
    d.text((cx, y + S + 44), shown, font=fit_font(shown, 340, 42, 22), fill=WHITE, anchor="mm",
           stroke_width=2, stroke_fill=(0, 0, 0))
    tcol = tier_color(p["tier"])
    pill(img, cx, y + S + 92, p["tier"], tcol, font(22))
    d = ImageDraw.Draw(img)
    d.text((cx, y + S + 138), f"{p['score']} PTS", font=font(48),
           fill=GOLD if is_winner else GREY, anchor="mm", stroke_width=2, stroke_fill=(0, 0, 0))


def draw_stats(img, a, b):
    panel(img, (392, 160, 888, 625))
    d = ImageDraw.Draw(img)
    cx, bar_max = 640, 190
    for i, (key, label) in enumerate(ROWS):
        y0 = 184 + i * 61
        va, vb = a["parts"].get(key, 0), b["parts"].get(key, 0)
        d.text((cx, y0 + 4), label, font=font(18), fill=(190, 198, 218), anchor="mm")
        mx = MAXES[key]
        # faint tracks
        aa_rrect(img, (cx - 10 - bar_max, y0 + 18, cx - 10, y0 + 38), 10, fill=(255, 255, 255, 14))
        aa_rrect(img, (cx + 10, y0 + 18, cx + 10 + bar_max, y0 + 38), 10, fill=(255, 255, 255, 14))
        for val, other, color, side in ((va, vb, CYAN, -1), (vb, va, PINK, 1)):
            length = max(14, int(bar_max * min(val, mx) / mx)) if mx else 14
            win = val >= other
            c1 = color if win else darken(color, 0.45)
            c2 = lighten(color, 0.35) if win else darken(color, 0.6)
            bar = hgradient((length, 20), c1, c2) if side > 0 else hgradient((length, 20), c2, c1)
            mask = Image.new("L", (length * 4, 80), 0)
            ImageDraw.Draw(mask).rounded_rectangle((0, 0, length * 4 - 1, 79), 40, fill=255)
            mask = mask.resize((length, 20), Image.LANCZOS)
            if side < 0:
                bx = cx - 10 - length
                tx, anchor = bx - 10, "rm"
            else:
                bx = cx + 10
                tx, anchor = bx + length + 10, "lm"
            if win and val != other:
                gl = Image.new("RGBA", img.size, (0, 0, 0, 0))
                ImageDraw.Draw(gl).rounded_rectangle((bx, y0 + 18, bx + length, y0 + 38), 10,
                                                     fill=color + (150,))
                img.alpha_composite(gl.filter(ImageFilter.GaussianBlur(7)))
            img.paste(bar, (bx, y0 + 18), mask)
            ImageDraw.Draw(img).text((tx, y0 + 28), str(val), font=font(20),
                                     fill=WHITE if win else (140, 148, 168), anchor=anchor)


def make_battle_image(a, b, winner_is_a):
    """a / b: dicts with name, score, tier, parts{}, pfp (PIL image or None)."""
    img = background()
    glow_text(img, (640, 40), "PROFILE BATTLE", font(50), CYAN, blur=14)

    # VS badge
    glow_ring(img, (600, 76, 680, 156), GREEN, 8, blur=12)
    aa_ellipse(img, (600, 76, 680, 156), fill=(10, 14, 30, 255), outline=GREEN + (255,), width=4)
    ImageDraw.Draw(img).text((640, 117), "VS", font=font(38), fill=GREEN, anchor="mm")

    seed = a["score"] * 31 + b["score"]
    draw_fighter(img, 210, CYAN, a, winner_is_a, seed)
    draw_fighter(img, 1070, PINK, b, not winner_is_a, seed + 1)
    draw_stats(img, a, b)

    win, lose = (a, b) if winner_is_a else (b, a)
    margin = abs(a["score"] - b["score"])
    line = f"{win['name'][:12].upper()} MOGGED {lose['name'][:12].upper()}"
    glow_text(img, (640, 664), line, fit_font(line, 900, 36, 22), GOLD, blur=10, fill=GOLD, stroke=3)
    if margin:
        pill(img, 640, 701, f"WON BY {margin} PTS", GREEN, font(16))

    buf = io.BytesIO()
    img.convert("RGB").save(buf, "JPEG", quality=94, subsampling=0)
    buf.seek(0)
    return buf


# ------------------------------------------------------------ challenge banner
def _sword(img, hilt, tip):
    hx, hy = hilt
    tx, ty = tip
    dx, dy = tx - hx, ty - hy
    length = math.hypot(dx, dy)
    ux, uy = dx / length, dy / length
    px, py = -uy, ux
    gx, gy = hx + ux * length * 0.22, hy + uy * length * 0.22
    x0, y0 = min(hx, tx) - 70, min(hy, ty) - 70
    x1, y1 = max(hx, tx) + 70, max(hy, ty) + 70

    def paint(d, s, ox, oy):
        def P(x, y):
            return ((x - ox) * s, (y - oy) * s)
        d.line([P(hx, hy), P(gx, gy)], fill=(150, 90, 40), width=16 * s)
        # blade with a bright core + tapered tip
        d.line([P(gx, gy), P(tx, ty)], fill=(190, 200, 222), width=22 * s)
        d.line([P(gx, gy), P(tx, ty)], fill=WHITE, width=8 * s)
        d.polygon([P(tx + px * 11, ty + py * 11), P(tx - px * 11, ty - py * 11),
                   P(tx + ux * 34, ty + uy * 34)], fill=(190, 200, 222))
        d.line([P(gx - px * 62, gy - py * 62), P(gx + px * 62, gy + py * 62)], fill=GOLD, width=18 * s)
        a, b = P(hx - 17, hy - 17), P(hx + 17, hy + 17)
        d.ellipse([*a, *b], fill=GOLD, outline=(160, 110, 0), width=3 * s)
    aa_shape(img, (x0, y0, x1, y1), paint)


def make_challenge_banner():
    img = background()
    blob(img, (640, 360), 320, GREEN, 60)

    glow = Image.new("RGBA", img.size, (0, 0, 0, 0))
    _sword(glow, (430, 560), (850, 190))
    _sword(glow, (850, 560), (430, 190))
    img.alpha_composite(glow.filter(ImageFilter.GaussianBlur(16)))
    _sword(img, (430, 560), (850, 190))
    _sword(img, (850, 560), (430, 190))

    # clash burst where the blades cross
    burst = Image.new("RGBA", img.size, (0, 0, 0, 0))
    bd = ImageDraw.Draw(burst)
    for k in range(12):
        a = k * math.pi / 6
        r = 90 if k % 2 == 0 else 55
        bd.line([(640, 375), (640 + math.cos(a) * r, 375 + math.sin(a) * r)],
                fill=(255, 255, 255, 220), width=4)
    sparkle(bd, 640, 375, 46, (255, 255, 255, 255))
    img.alpha_composite(burst.filter(ImageFilter.GaussianBlur(1)))

    glow_text(img, (640, 92), "TELEMAXXING", font(116), GREEN, blur=20, stroke=3)
    d = ImageDraw.Draw(img)
    d.text((640, 626), "Challenge someone to a profile battle", font=font(40),
           fill=WHITE, anchor="mm", stroke_width=2, stroke_fill=(0, 0, 0))
    d.text((640, 680), "tap to send the challenge  -  who mogs who?", font=font(24),
           fill=(165, 175, 198), anchor="mm")
    buf = io.BytesIO()
    img.convert("RGB").save(buf, "JPEG", quality=94, subsampling=0)
    buf.seek(0)
    return buf


# ======================================================================
# BOT
# ======================================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=sys.stdout,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("telemaxxing")

TOKEN = os.environ.get("BOT_TOKEN", "").strip()
PORT = int(os.environ.get("PORT", "10000"))

# ------------------------------------------------------------------ health web
web = Flask(__name__)


@web.get("/")
def health():
    return "telemaxxing ok"


@web.get("/healthz")
def healthz():
    return "ok"


def run_web():
    # werkzeug dev server is fine for a health endpoint
    logging.getLogger("werkzeug").setLevel(logging.WARNING)
    web.run(host="0.0.0.0", port=PORT, threaded=True, use_reloader=False)


# ------------------------------------------------------------------ scoring
# (user id, approx. creation year) - rough public estimates, used for ACCOUNT AGE.
_ID_YEARS = [
    (0, 2013.5),
    (2_000_000, 2013.6),
    (100_000_000, 2016.0),
    (300_000_000, 2018.2),
    (500_000_000, 2019.2),
    (1_000_000_000, 2020.4),
    (1_500_000_000, 2021.0),
    (2_000_000_000, 2021.6),
    (5_000_000_000, 2022.2),
    (6_000_000_000, 2023.2),
    (7_000_000_000, 2024.2),
    (8_000_000_000, 2025.2),
]


def account_year(uid):
    prev = _ID_YEARS[0]
    for cur in _ID_YEARS[1:]:
        if uid <= cur[0]:
            span = cur[0] - prev[0] or 1
            return prev[1] + (cur[1] - prev[1]) * (uid - prev[0]) / span
        prev = cur
    return _ID_YEARS[-1][1]


def current_year():
    t = time.gmtime()
    return t.tm_year + (t.tm_yday - 1) / 365.0


def username_points(username):
    if not username:
        return 0
    n = len(username)
    if n <= 5:
        return 15
    if n <= 8:
        return 12
    if n <= 12:
        return 8
    return 5


def aura_points(uid):
    """Deterministic 'vibes' roll so the same person always gets the same aura."""
    h = hashlib.sha256(f"aura:{uid}".encode()).digest()
    return h[0] % (AURA_MAX + 1)


async def fetch_pfp(bot, uid):
    """Returns (photo_count, PIL image or None)."""
    try:
        photos = await bot.get_user_profile_photos(uid, limit=1)
    except TelegramError as e:
        log.info("no profile photos for %s: %s", uid, e)
        return 0, None
    count = photos.total_count
    if not count:
        return 0, None
    try:
        tg_file = await photos.photos[0][-1].get_file()
        raw = await tg_file.download_as_bytearray()
        pic = Image.open(io.BytesIO(bytes(raw)))
        pic.load()
        return count, pic
    except Exception as e:  # corrupt / unsupported image etc.
        log.warning("pfp download failed for %s: %s", uid, e)
        return count, None


async def fetch_bio(bot, uid):
    try:
        chat = await bot.get_chat(uid)
        return (getattr(chat, "bio", None) or "").strip()
    except TelegramError:
        return ""


async def build_profile(bot, user: User):
    count, pic = await fetch_pfp(bot, user.id)
    bio = await fetch_bio(bot, user.id)
    quality = await asyncio.to_thread(pic_quality, pic)

    age_years = max(0.0, current_year() - account_year(user.id))
    parts = {
        "photos": min(count, 10) * 5,
        "username": username_points(user.username),
        "premium": 25 if getattr(user, "is_premium", False) else 0,
        "bio": 10 if bio else 0,
        "age": int(round(min(age_years / 12.0, 1.0) * MAXES["age"])),
        "quality": quality,
        "aura": aura_points(user.id),
    }
    for k, v in parts.items():
        parts[k] = max(0, min(int(v), MAXES[k]))
    score = sum(parts.values())
    return {
        "id": user.id,
        "name": img_safe(user.first_name or user.full_name or "", "Player"),
        "score": score,
        "tier": tier_for(score),
        "parts": parts,
        "pfp": pic,
    }


def decide_winner(a, b):
    """True if A wins. Ties are broken deterministically."""
    if a["score"] != b["score"]:
        return a["score"] > b["score"]
    h = hashlib.sha256(f"{a['id']}:{b['id']}".encode()).digest()[0]
    return h % 2 == 0


async def run_battle(bot, chat_id, user_a: User, user_b: User, reply_to=None):
    a, b = await asyncio.gather(build_profile(bot, user_a), build_profile(bot, user_b))
    winner_is_a = decide_winner(a, b)
    buf = await asyncio.to_thread(make_battle_image, a, b, winner_is_a)
    win, lose = (a, b) if winner_is_a else (b, a)
    caption = f"{win['name']} mogged {lose['name']}  ({win['score']} - {lose['score']})"
    await bot.send_photo(
        chat_id, photo=buf, caption=caption, reply_to_message_id=reply_to,
        allow_sending_without_reply=True,
    )


# ------------------------------------------------------------------ handlers
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "TELEMAXXING - profile battles.\n\n"
        "/challenge - post a challenge anyone can accept\n"
        "/battle - reply to someone's message to battle them\n"
        "/score - see your own score breakdown"
    )


async def cmd_challenge(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    try:
        banner = await asyncio.to_thread(make_challenge_banner)
        kb = InlineKeyboardMarkup(
            [[InlineKeyboardButton("Accept challenge", callback_data=f"acc:{user.id}")]]
        )
        sent = await update.effective_chat.send_photo(
            photo=banner,
            caption=f"{img_safe(user.first_name)} wants a profile battle. Who's brave enough?",
            reply_markup=kb,
        )
        context.bot_data.setdefault("challengers", {})[(sent.chat_id, sent.message_id)] = user
    except Exception:
        log.exception("challenge failed")
        await update.effective_message.reply_text("Couldn't create the challenge, try again.")


async def cmd_battle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    target = msg.reply_to_message.from_user if msg.reply_to_message else None
    if target is None:
        await msg.reply_text("Reply to someone's message with /battle to fight them, or use /challenge.")
        return
    if target.is_bot:
        await msg.reply_text("Bots have no aura. Pick a human.")
        return
    if target.id == update.effective_user.id:
        await msg.reply_text("You can't battle yourself.")
        return
    try:
        await context.bot.send_chat_action(msg.chat_id, "upload_photo")
        await run_battle(context.bot, msg.chat_id, update.effective_user, target, msg.message_id)
    except Exception:
        log.exception("battle failed")
        await msg.reply_text("Battle crashed, try again in a sec.")


async def cb_accept(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    try:
        challenger_id = int(q.data.split(":", 1)[1])
    except (IndexError, ValueError):
        await q.answer()
        return
    if q.from_user.id == challenger_id:
        await q.answer("You can't accept your own challenge.", show_alert=True)
        return

    msg = q.message
    key = (msg.chat_id, msg.message_id)
    store = context.bot_data.setdefault("challengers", {})
    challenger = store.pop(key, None)
    if challenger is None:  # bot restarted, look the user up instead
        try:
            challenger = (await context.bot.get_chat_member(msg.chat_id, challenger_id)).user
        except TelegramError:
            await q.answer("Challenge expired, post a new one.", show_alert=True)
            return

    await q.answer("Battle starting...")
    try:
        await msg.edit_reply_markup(reply_markup=None)  # one accept only
    except TelegramError:
        pass
    try:
        await run_battle(context.bot, msg.chat_id, challenger, q.from_user, msg.message_id)
    except Exception:
        log.exception("accepted battle failed")
        await context.bot.send_message(msg.chat_id, "Battle crashed, try again in a sec.")


async def cmd_score(update: Update, context: ContextTypes.DEFAULT_TYPE):
    p = await build_profile(context.bot, update.effective_user)
    lines = [f"{p['name']} - {p['score']} pts - {p['tier']}", ""]
    for key, label in ROWS:
        lines.append(f"{label}: {p['parts'][key]}/{MAXES[key]}")
    await update.effective_message.reply_text("\n".join(lines))


async def on_error(update, context: ContextTypes.DEFAULT_TYPE):
    log.error("unhandled error", exc_info=context.error)


# ------------------------------------------------------------------ main
def main():
    if not TOKEN:
        log.error("BOT_TOKEN is not set. Add it in Render -> Environment, then redeploy.")
        sys.exit(1)

    threading.Thread(target=run_web, name="web", daemon=True).start()
    log.info("health server on port %s", PORT)

    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("challenge", cmd_challenge))
    app.add_handler(CommandHandler("battle", cmd_battle))
    app.add_handler(CommandHandler("score", cmd_score))
    app.add_handler(CallbackQueryHandler(cb_accept, pattern=r"^acc:\d+$"))
    app.add_error_handler(on_error)

    log.info("starting polling")
    # main thread, blocks until SIGTERM/SIGINT
    app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception:
        log.exception("fatal error on startup")
        raise
