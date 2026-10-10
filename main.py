"""Telemaxxing - Telegram profile-battle bot.

Render-safe layout:
  * Flask health server runs in a daemon thread (binds to $PORT)
  * python-telegram-bot polling runs on the MAIN thread
  * every startup problem prints a clear message instead of dying silently

INLINE-ONLY bot: type @yourbot in any chat -> pick the challenge, someone accepts, battle appears
with Leaderboard / Refresh profile buttons under it.
Images are served by the Flask server (Telegram fetches them by URL).

BotFather: /setinline  (enable inline mode, add a placeholder text)
Env vars:  BOT_TOKEN (required), PORT + RENDER_EXTERNAL_URL (Render sets both),
           PUBLIC_URL (only if you use a custom domain), FONT_PATH (optional)
Single file: images + bot together (no images.py needed).
requirements.txt:
    python-telegram-bot==21.6
    Flask==3.1.3
    Pillow
"""
import asyncio
import hashlib
import io
import json
import logging
import math
import os
import random
import sys
import threading
import time

from flask import Flask, Response, abort
from pathlib import Path
from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps, ImageStat
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InlineQueryResultPhoto,
    InputMediaPhoto,
    Update,
    User,
)
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    ContextTypes,
    InlineQueryHandler,
    MessageHandler,
    filters,
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
        (box[0] + 2, box[1] + 2, box[2] - 2, box[1] + 46), radius - 2, fill=(255, 255, 255, 6)
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


def rays(img, cx, cy, seed, scale=1.0):
    """Golden god-rays behind the winner."""
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ld = ImageDraw.Draw(layer)
    n = 18
    for i in range(n):
        a0 = (i / n) * 2 * math.pi
        a1 = a0 + math.pi / n * 0.8
        r = 330 * scale
        ld.polygon([(cx, cy), (cx + math.cos(a0) * r, cy + math.sin(a0) * r),
                    (cx + math.cos(a1) * r, cy + math.sin(a1) * r)], fill=GOLD + (30,))
    img.alpha_composite(layer.filter(ImageFilter.GaussianBlur(3)))


def confetti(img, cx, cy, seed, scale=1.0):
    rnd = random.Random(seed)
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    for _ in range(60):
        ang = rnd.uniform(0, 2 * math.pi)
        r = rnd.uniform(170, 245) * scale
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
        r = rnd.uniform(150, 260) * scale
        sparkle(sd, cx + math.cos(ang) * r, cy + math.sin(ang) * r, rnd.randint(8, 15), WHITE + (230,))
    img.alpha_composite(layer)


def mogged_banner(width, bh=104):
    bw = width
    ss = 2
    layer = Image.new("RGBA", (bw * ss, bh * ss), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    d.rectangle([0, 0, bw * ss, bh * ss], fill=GREEN + (255,))
    d.rectangle([0, 0, bw * ss, 8 * ss], fill=(20, 20, 20, 255))
    d.rectangle([0, (bh - 8) * ss, bw * ss, bh * ss], fill=(20, 20, 20, 255))
    f = fit_font("MOGGED!", (bw - 30) * ss, int(bh * 0.69) * ss, 20)
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


# ------------------------------------------------------------ shared pieces
def gbar(img, x, y, w, h, c1, c2, glow=None):
    """Anti-aliased gradient bar (c1 on the left -> c2 on the right)."""
    w = max(int(w), int(h))
    if glow:
        gl = Image.new("RGBA", img.size, (0, 0, 0, 0))
        ImageDraw.Draw(gl).rounded_rectangle((x, y, x + w, y + h), h // 2, fill=glow + (150,))
        img.alpha_composite(gl.filter(ImageFilter.GaussianBlur(7)))
    bar = hgradient((w, h), c1, c2)
    mask = Image.new("L", (w * 4, h * 4), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, w * 4 - 1, h * 4 - 1), h * 2, fill=255)
    mask = mask.resize((w, h), Image.LANCZOS)
    img.paste(bar, (int(x), int(y)), mask)


def star_badge(img, cx, cy, r=20):
    """Telegram-premium style star badge."""
    aa_ellipse(img, (cx - r - 7, cy - r - 7, cx + r + 7, cy + r + 7),
               fill=(16, 20, 44, 255), outline=CYAN + (255,), width=3)

    def paint(d, s, ox, oy):
        pts = []
        for i in range(10):
            ang = -math.pi / 2 + i * math.pi / 5
            rr = r if i % 2 == 0 else r * 0.46
            pts.append(((cx + math.cos(ang) * rr - ox) * s, (cy + math.sin(ang) * rr - oy) * s))
        d.polygon(pts, fill=GOLD)
    aa_shape(img, (cx - r, cy - r, cx + r, cy + r), paint)


def avatar(img, cx, y, S, pic, name, ring, winner=False, gray=False):
    x = cx - S // 2
    if winner:
        glow_ring(img, (x - 14, y - 14, x + S + 14, y + S + 14), GOLD, 16, blur=22)
    glow_ring(img, (x - 8, y - 8, x + S + 8, y + S + 8), ring, 10)
    pic = pic or placeholder(S, ((name or "?")[:1] or "?").upper())
    pic = ImageOps.fit(pic.convert("RGB"), (S, S))
    if gray:
        pic = Image.blend(ImageOps.grayscale(pic).convert("RGB"),
                          Image.new("RGB", pic.size, (0, 0, 0)), 0.38)
    img.alpha_composite(circle_crop(pic, S), (x, y))
    aa_ellipse(img, (x - 7, y - 7, x + S + 7, y + S + 7),
               outline=(GOLD if winner else ring) + (255,), width=7)
    aa_ellipse(img, (x + 2, y + 2, x + S - 2, y + S - 2), outline=(255, 255, 255, 40), width=2)


def info_lines(img, cx, y, p, color, score_color, score_font=48):
    """Name / handle+record / tier pill / score, centred under an avatar."""
    d = ImageDraw.Draw(img)
    name = (p.get("name") or "Player")[:16]
    d.text((cx, y), name, font=fit_font(name, 340, 42, 22), fill=WHITE, anchor="mm",
           stroke_width=2, stroke_fill=(0, 0, 0))
    sub = "   |   ".join(s for s in (p.get("handle", ""), p.get("rec", "")) if s)
    if sub:
        d.text((cx, y + 30), sub, font=fit_font(sub, 340, 20, 12), fill=(215, 222, 240), anchor="mm",
               stroke_width=2, stroke_fill=(0, 0, 0))
    pill(img, cx, y + 68, p["tier"], tier_color(p["tier"]), font(22))
    ImageDraw.Draw(img).text((cx, y + 118), f"{p['score']} PTS", font=font(score_font),
                             fill=score_color, anchor="mm", stroke_width=2, stroke_fill=(0, 0, 0))


# ------------------------------------------------------------ card layout
CW, CH = 1080, 1240
_BG2 = {}


def _jpeg(img):
    buf = io.BytesIO()
    img.convert("RGB").save(buf, "JPEG", quality=94, subsampling=0)
    buf.seek(0)
    return buf


def background2():
    """Dark, clean backdrop for the tall cards."""
    if "bg" in _BG2:
        return _BG2["bg"].copy()
    bg = gradient((CW, CH), (7, 9, 20), (22, 9, 36)).convert("RGBA")
    blob(bg, (290, 390), 430, CYAN, 44)
    blob(bg, (790, 390), 430, PINK, 44)
    blob(bg, (540, 1160), 430, (90, 40, 200), 38)
    dots = Image.new("RGBA", (CW, CH), (0, 0, 0, 0))
    dd = ImageDraw.Draw(dots)
    for gx in range(20, CW, 40):
        for gy in range(20, CH, 40):
            dd.ellipse((gx - 1, gy - 1, gx + 1, gy + 1), fill=(255, 255, 255, 16))
    bg.alpha_composite(dots)
    rnd = random.Random(11)
    sp = Image.new("RGBA", (CW, CH), (0, 0, 0, 0))
    sd = ImageDraw.Draw(sp)
    for _ in range(22):
        sparkle(sd, rnd.randint(20, CW - 20), rnd.randint(20, CH - 20), rnd.randint(4, 9),
                rnd.choice([WHITE, CYAN, PINK, GOLD]) + (rnd.randint(50, 130),))
    bg.alpha_composite(sp)
    vig = Image.radial_gradient("L").resize((CW, CH))
    dark = Image.new("RGBA", (CW, CH), (0, 0, 0, 255))
    dark.putalpha(vig.point(lambda v: int(max(0, v - 120) * 1.1)))
    bg.alpha_composite(dark)
    _BG2["bg"] = bg
    return bg.copy()


def header(img, tag, tag_color):
    glow_text(img, (60, 62), "TELEMAXXING", font(44), GREEN, blur=12, anchor="lm")
    f = font(20)
    w = f.getlength(tag) + 40
    pill(img, int(1020 - w / 2), 62, tag, tag_color, f)


def footer(img, text="who mogs who?   -   telemaxxing"):
    ImageDraw.Draw(img).text((CW // 2, 1210), text, font=font(18), fill=(120, 128, 152), anchor="mm")


# ------------------------------------------------------------ battle card
def fighter_card(img, x0, x1, color, p, winner, seed):
    y0, y1, S, ay = 118, 640, 226, 192
    cx = (x0 + x1) // 2
    ring = GOLD if winner else darken(color, 0.6)
    if winner:
        gl = Image.new("RGBA", img.size, (0, 0, 0, 0))
        ImageDraw.Draw(gl).rounded_rectangle((x0, y0, x1, y1), 30, outline=GOLD + (255,), width=8)
        img.alpha_composite(gl.filter(ImageFilter.GaussianBlur(14)))
    panel(img, (x0, y0, x1, y1), 30, fill=(10, 13, 28, 228), outline=ring + (235,))
    if winner:
        rays(img, cx, ay + S // 2, seed, 0.66)
        confetti(img, cx, ay + S // 2, seed, 0.56)
    avatar(img, cx, ay, S, p.get("pfp"), p.get("name"), color, winner=winner, gray=not winner)
    if p["parts"].get("premium", 0) > 0:
        star_badge(img, cx + int(S * 0.38), ay + int(S * 0.84), r=17)
    if winner:
        crown(img, cx, ay - 62, w=104, h=50)
    else:
        banner = mogged_banner(S + 40, 76)
        img.alpha_composite(banner, (cx - banner.width // 2, ay + S // 2 - banner.height // 2))
    info_lines(img, cx, ay + S + 38, p, color, GOLD if winner else GREY, score_font=62)


def verdict_bar(img, win, lose, margin, ra, rb):
    panel(img, (50, 662, 1030, 724), radius=20, fill=(10, 13, 28, 225))
    d = ImageDraw.Draw(img)
    wn, ln = win["name"][:12].upper(), lose["name"][:12].upper()
    f = fit_font(f"{wn} MOGS {ln}", 600, 32, 18)
    x = 84
    for txt, col in ((wn, GOLD), (" MOGS ", GREY), (ln, WHITE)):
        d.text((x, 693), txt, font=f, fill=col, anchor="lm")
        x += f.getlength(txt)
    pill(img, 790, 693, f"ROUNDS  {ra} - {rb}", CYAN, font(16))
    pill(img, 972, 693, f"+{margin}" if margin else "TIE", GOLD, font(24))


def draw_stats(img, a, b):
    panel(img, (50, 748, 1030, 1176), radius=26)
    d = ImageDraw.Draw(img)
    cx, bar_max, bh = 540, 400, 20
    for i, (key, label) in enumerate(ROWS):
        y0 = 770 + i * 56
        va, vb = a["parts"].get(key, 0), b["parts"].get(key, 0)
        d.text((cx, y0 + 4), label, font=font(18), fill=(190, 198, 218), anchor="mm")
        mx = MAXES[key]
        aa_rrect(img, (cx - 12 - bar_max, y0 + 18, cx - 12, y0 + 18 + bh), 10, fill=(255, 255, 255, 14))
        aa_rrect(img, (cx + 12, y0 + 18, cx + 12 + bar_max, y0 + 18 + bh), 10, fill=(255, 255, 255, 14))
        for val, other, color, side in ((va, vb, CYAN, -1), (vb, va, PINK, 1)):
            length = max(bh, int(bar_max * min(val, mx) / mx)) if mx else bh
            win = val >= other
            c1 = color if win else darken(color, 0.45)
            c2 = lighten(color, 0.35) if win else darken(color, 0.6)
            if side < 0:
                bx, tx, anchor = cx - 12 - length, cx - 12 - length - 10, "rm"
                c1, c2 = c2, c1
            else:
                bx, tx, anchor = cx + 12, cx + 12 + length + 10, "lm"
            gbar(img, bx, y0 + 18, length, bh, c1, c2, glow=color if (win and val != other) else None)
            ImageDraw.Draw(img).text((tx, y0 + 28), str(val), font=font(20),
                                     fill=WHITE if win else (140, 148, 168), anchor=anchor)


def make_battle_image(a, b, winner_is_a):
    """a / b: dicts with name, score, tier, parts{}, pfp (PIL image or None),
    optional handle / rec strings."""
    img = background2()
    header(img, "PROFILE BATTLE", CYAN)
    seed = a["score"] * 31 + b["score"]
    fighter_card(img, 50, 520, CYAN, a, winner_is_a, seed)
    fighter_card(img, 560, 1030, PINK, b, not winner_is_a, seed + 1)

    glow_ring(img, (500, 339, 580, 419), GREEN, 8, blur=12)
    aa_ellipse(img, (500, 339, 580, 419), fill=(10, 14, 30, 255), outline=GREEN + (255,), width=4)
    ImageDraw.Draw(img).text((540, 380), "VS", font=font(36), fill=GREEN, anchor="mm")

    ra = sum(1 for k, _ in ROWS if a["parts"].get(k, 0) > b["parts"].get(k, 0))
    rb = sum(1 for k, _ in ROWS if b["parts"].get(k, 0) > a["parts"].get(k, 0))
    win, lose = (a, b) if winner_is_a else (b, a)
    verdict_bar(img, win, lose, abs(a["score"] - b["score"]), ra, rb)
    draw_stats(img, a, b)
    footer(img)
    return _jpeg(img)


# ------------------------------------------------------------ leaderboard
def make_leaderboard(rows, title="LEADERBOARD"):
    """rows: list of dicts name, w, l (already sorted, best first)."""
    img = background2()
    header(img, title, GOLD)
    panel(img, (50, 118, 1030, 1176), radius=30)
    d = ImageDraw.Draw(img)
    if not rows:
        d.text((540, 600), "NO BATTLES YET", font=font(56), fill=WHITE, anchor="mm")
        d.text((540, 672), "challenge someone to start the board", font=font(26),
               fill=(165, 175, 198), anchor="mm")
        footer(img)
        return _jpeg(img)
    medals = [GOLD, (205, 210, 222), (205, 127, 50)]
    for i, r in enumerate(rows[:5]):
        cy = 238 + i * 190
        total = r["w"] + r["l"]
        wr = r["w"] / total if total else 0
        mc = medals[i] if i < 3 else (90, 100, 140)
        if i:
            d.line([(110, cy - 94), (970, cy - 94)], fill=(255, 255, 255, 18), width=2)
        glow_ring(img, (84, cy - 46, 176, cy + 46), mc, 8, blur=10)
        aa_ellipse(img, (84, cy - 46, 176, cy + 46), fill=darken(mc, 0.35) + (255,),
                   outline=mc + (255,), width=5)
        d = ImageDraw.Draw(img)
        d.text((130, cy), str(i + 1), font=font(48), fill=WHITE, anchor="mm")
        nm = (r["name"] or "Player")[:16]
        d.text((210, cy - 26), nm, font=fit_font(nm, 560, 46, 24), fill=WHITE, anchor="lm",
               stroke_width=2, stroke_fill=(0, 0, 0))
        aa_rrect(img, (210, cy + 14, 780, cy + 40), 13, fill=(255, 255, 255, 16))
        if wr > 0:
            gbar(img, 210, cy + 14, 570 * wr, 26, darken(mc, 0.7), lighten(mc, 0.2), glow=mc)
        d = ImageDraw.Draw(img)
        d.text((1000, cy - 16), f"{int(round(wr * 100))}%", font=font(54), fill=GOLD, anchor="rm",
               stroke_width=2, stroke_fill=(0, 0, 0))
        d.text((1000, cy + 28), f"{r['w']}W - {r['l']}L", font=font(24), fill=(190, 198, 218), anchor="rm")
    footer(img)
    return _jpeg(img)


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




def make_challenge_banner(name=None):
    img = background()
    blob(img, (640, 360), 320, GREEN, 60)

    glow = Image.new("RGBA", img.size, (0, 0, 0, 0))
    _sword(glow, (430, 560), (850, 190))
    _sword(glow, (850, 560), (430, 190))
    img.alpha_composite(glow.filter(ImageFilter.GaussianBlur(16)))
    _sword(img, (430, 560), (850, 190))
    _sword(img, (850, 560), (430, 190))

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
    if name:
        line = f"{name[:16].upper()} CHALLENGES YOU"
        glow_text(img, (640, 622), line, fit_font(line, 1100, 50, 26), CYAN, blur=12, stroke=3)
        pill(img, 640, 680, "TAP  ACCEPT  TO BATTLE", GREEN, font(20))
    else:
        d.text((640, 626), "Challenge someone to a profile battle", font=font(40),
               fill=WHITE, anchor="mm", stroke_width=2, stroke_fill=(0, 0, 0))
        d.text((640, 680), "/challenge   |   /battle   |   /score   |   /top", font=font(24),
               fill=(165, 175, 198), anchor="mm")
    return _jpeg(img)


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

# ------------------------------------------------------------------ embedded artwork
# The inline-menu / challenge image (1280x426 JPEG), embedded so this stays a single file.
ABOUT_B64 = (
    "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"
)
_ABOUT = {}


def about_bytes():
    if "full" not in _ABOUT:
        import base64
        _ABOUT["full"] = base64.b64decode(ABOUT_B64)
    return _ABOUT["full"]


def about_thumb_bytes():
    if "thumb" not in _ABOUT:
        im = Image.open(io.BytesIO(about_bytes())).convert("RGB")
        im = im.resize((320, round(320 * im.height / im.width)), Image.LANCZOS)
        out = io.BytesIO()
        im.save(out, "JPEG", quality=85)
        _ABOUT["thumb"] = out.getvalue()
    return _ABOUT["thumb"]


# ------------------------------------------------------------------ health web
web = Flask(__name__)


@web.get("/")
def health():
    return "telemaxxing ok"


@web.get("/healthz")
def healthz():
    return "ok"


BASE_URL = (os.environ.get("PUBLIC_URL") or os.environ.get("RENDER_EXTERNAL_URL") or "").rstrip("/")

USERS = {}      # user_id -> telegram User, remembered from inline queries
LOOP = None     # bot event loop (set in post_init) so Flask threads can use the bot
BOT = None
_CACHE = {}


def cached(key, ttl, fn):
    hit = _CACHE.get(key)
    now = time.time()
    if hit and hit[0] > now:
        return hit[1]
    data = fn()
    if data is not None:
        if len(_CACHE) > 300:
            for k in [k for k, v in _CACHE.items() if v[0] <= now]:
                _CACHE.pop(k, None)
        _CACHE[key] = (now + ttl, data)
    return data


def jpg(data):
    if not data:
        abort(404)
    return Response(data, mimetype="image/jpeg", headers={"Cache-Control": "public, max-age=60"})


def remember(user):
    if len(USERS) > 5000:
        USERS.clear()
    USERS[user.id] = user


@web.get("/banner/<int:uid>.jpg")
def route_banner(uid):
    return jpg(about_bytes())


@web.get("/top.jpg")
def route_top():
    return jpg(cached("top", 30, lambda: make_leaderboard(leaderboard_rows("global")).getvalue()))


@web.get("/img/<token>.jpg")
def route_img(token):
    hit = _CACHE.get(f"img:{token}")
    return jpg(hit[1] if hit and hit[0] > time.time() else None)


@web.get("/thumb/<kind>.jpg")
def route_thumb(kind):
    return jpg(cached(f"thumb:{kind}", 86400, lambda: thumb_bytes(kind)))


def thumb_bytes(kind):
    if kind == "banner":
        return about_thumb_bytes()
    sample = {"id": 0, "name": "YOU", "handle": "@you", "rec": "", "score": 96, "tier": tier_for(96),
              "pfp": None, "parts": {"photos": 30, "username": 12, "premium": 0, "bio": 10,
                                     "age": 18, "quality": 16, "aura": 10}}
    if kind == "top":
        buf = make_leaderboard([{"name": "Alex", "w": 9, "l": 2}, {"name": "Sam", "w": 6, "l": 4},
                                {"name": "Kai", "w": 3, "l": 5}])
    else:
        buf = make_challenge_banner()
    out = io.BytesIO()
    Image.open(buf).resize((320, 180), Image.LANCZOS).save(out, "JPEG", quality=85)
    return out.getvalue()


@web.before_request
def _t0():
    from flask import g
    g.t0 = time.perf_counter()


@web.after_request
def _log_request(resp):
    from flask import g, request
    if request.path not in ("/", "/healthz"):
        ms = (time.perf_counter() - getattr(g, "t0", time.perf_counter())) * 1000
        log.info("img %s -> %s (%.0f ms, %s bytes)", request.path, resp.status_code, ms,
                 resp.calculate_content_length() or "?")
    return resp


def prewarm():
    """Render every static image once at startup so Telegram never waits on a cold render,
    then fetch one through our own public URL to prove it is reachable."""
    try:
        for kind in ("banner", "top"):
            cached(f"thumb:{kind}", 86400, lambda k=kind: thumb_bytes(k))
        cached("top", 30, lambda: make_leaderboard(leaderboard_rows("global")).getvalue())
        log.info("prewarm: images ready")
    except Exception:
        log.exception("prewarm failed")
        return
    if not BASE_URL:
        return
    try:
        import httpx
    except ImportError:
        return
    for attempt in range(6):
        time.sleep(3)
        try:
            r = httpx.get(f"{BASE_URL}/thumb/banner.jpg", timeout=15, follow_redirects=True)
            if r.status_code == 200 and r.headers.get("content-type", "").startswith("image/"):
                log.info("SELF-TEST OK: %s/thumb/banner.jpg is publicly reachable", BASE_URL)
                return
            log.warning("SELF-TEST got %s %s", r.status_code, r.headers.get("content-type"))
        except Exception as e:
            log.warning("SELF-TEST attempt %d failed: %s", attempt + 1, e)
    log.error("SELF-TEST FAILED: %s is not reachable from outside - Telegram cannot load images. "
              "Check PUBLIC_URL / that this is a Web Service.", BASE_URL)


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




# ------------------------------------------------------------------ records (win/loss per chat)
RECORDS_PATH = BASE_DIR / "records.json"
RECORDS = {}


def load_records():
    global RECORDS
    try:
        RECORDS = json.loads(RECORDS_PATH.read_text())
    except Exception:
        RECORDS = {}


def save_records():
    try:
        RECORDS_PATH.write_text(json.dumps(RECORDS))
    except Exception as e:  # read-only / ephemeral disk is fine
        log.debug("could not save records: %s", e)


def record_result(chat_id, win, lose):
    chat = RECORDS.setdefault(str(chat_id), {})
    for p, key in ((win, "w"), (lose, "l")):
        e = chat.setdefault(str(p["id"]), {"name": p["name"], "w": 0, "l": 0})
        e["name"] = p["name"]
        e[key] += 1
    save_records()


def rec_str(chat_id, uid):
    e = RECORDS.get(str(chat_id), {}).get(str(uid))
    return f"{e['w']}W - {e['l']}L" if e else ""


def leaderboard_rows(chat_id):
    rows = list(RECORDS.get(str(chat_id), {}).values())
    rows.sort(key=lambda r: (r["w"], r["w"] / max(1, r["w"] + r["l"])), reverse=True)
    return rows[:5]


_LAST_USE = {}


def on_cooldown(uid, secs=6.0):
    now = time.monotonic()
    if now - _LAST_USE.get(uid, 0.0) < secs:
        return True
    _LAST_USE[uid] = now
    return False


# ------------------------------------------------------------------ profiles + battles
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
        "handle": f"@{img_safe(user.username, '')}" if user.username else "",
        "rec": "",
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


def battle_kb(a, b):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Leaderboard", callback_data=f"lb:{a}:{b}")],
        [InlineKeyboardButton("Refresh profile", callback_data=f"rf:{a}:{b}")],
        [InlineKeyboardButton("Challenge someone", switch_inline_query_current_chat="")],
    ])


def board_kb(a, b):
    return InlineKeyboardMarkup([[InlineKeyboardButton("Back to battle", callback_data=f"bk:{a}:{b}")]])


async def get_user(bot, uid):
    u = USERS.get(uid)
    if u is not None:
        return u
    try:
        chat = await bot.get_chat(uid)
        u = User(id=chat.id, first_name=chat.first_name or "Player", is_bot=False, username=chat.username)
        USERS[uid] = u
        return u
    except TelegramError:
        return None


async def battle_url(bot, user_a: User, user_b: User, record=True):
    """Builds the battle image, stores it for Telegram to fetch, returns its URL."""
    a, b = await asyncio.gather(build_profile(bot, user_a), build_profile(bot, user_b))
    winner_is_a = decide_winner(a, b)
    win, lose = (a, b) if winner_is_a else (b, a)
    if record:
        record_result("global", win, lose)
    a["rec"], b["rec"] = rec_str("global", a["id"]), rec_str("global", b["id"])
    data = await asyncio.to_thread(lambda: make_battle_image(a, b, winner_is_a).getvalue())
    token = hashlib.sha256(os.urandom(16)).hexdigest()[:24]
    cached(f"img:{token}", 900, lambda: data)
    url = f"{BASE_URL}/img/{token}.jpg"
    _CACHE[f"pair:{user_a.id}:{user_b.id}"] = (time.time() + 900, url)
    return url


# ------------------------------------------------------------------ inline mode
async def on_inline(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.inline_query
    user = q.from_user
    remember(user)
    if not BASE_URL:
        log.error("No PUBLIC_URL / RENDER_EXTERNAL_URL set - inline images cannot be served")
        await q.answer([], cache_time=5)
        return
    t = int(time.time() // 60)
    kb = InlineKeyboardMarkup(
        [[InlineKeyboardButton("ACCEPT CHALLENGE", callback_data=f"acc:{user.id}")]]
    )
    result = InlineQueryResultPhoto(
        id=f"ch{user.id}",
        photo_url=f"{BASE_URL}/banner/{user.id}.jpg?t={t}",
        thumbnail_url=f"{BASE_URL}/thumb/banner.jpg",
        photo_width=1280, photo_height=426,
        title="Challenge to a profile battle",
        description="Send it - first person to accept fights you",
        reply_markup=kb,
    )
    await q.answer([result], cache_time=5, is_personal=True)


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
    inline_id = q.inline_message_id
    if not inline_id:
        await q.answer()
        return
    claimed = context.bot_data.setdefault("claimed", set())
    if inline_id in claimed:
        await q.answer("Someone already accepted this one.", show_alert=True)
        return
    if on_cooldown(q.from_user.id, 4.0):
        await q.answer("Slow down a sec.", show_alert=True)
        return
    challenger = await get_user(context.bot, challenger_id)
    if challenger is None:
        await q.answer("Challenge expired - post a new one.", show_alert=True)
        return

    claimed.add(inline_id)
    remember(q.from_user)
    await q.answer("Battle starting...")
    try:
        url = await battle_url(context.bot, challenger, q.from_user)
        await context.bot.edit_message_media(
            inline_message_id=inline_id, media=InputMediaPhoto(media=url),
            reply_markup=battle_kb(challenger_id, q.from_user.id),
        )
    except Exception:
        claimed.discard(inline_id)
        log.exception("inline battle failed")
        try:
            await q.answer("Battle crashed, tap again.", show_alert=True)
        except TelegramError:
            pass


async def cb_nav(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Leaderboard / Refresh profile / Back buttons under a finished battle."""
    q = update.callback_query
    try:
        action, a, b = q.data.split(":")
        a, b = int(a), int(b)
    except ValueError:
        await q.answer()
        return
    inline_id = q.inline_message_id
    if not inline_id:
        await q.answer()
        return
    if on_cooldown(q.from_user.id, 3.0):
        await q.answer("Slow down a sec.", show_alert=True)
        return
    remember(q.from_user)
    await q.answer()
    try:
        if action == "lb":
            cached("top", 30, lambda: make_leaderboard(leaderboard_rows("global")).getvalue())
            url, kb = f"{BASE_URL}/top.jpg?t={int(time.time())}", board_kb(a, b)
        else:
            hit = _CACHE.get(f"pair:{a}:{b}")
            if action == "bk" and hit and hit[0] > time.time():
                url = hit[1]
            else:  # refresh (or expired back) -> rebuild with fresh profile data
                ua, ub = await get_user(context.bot, a), await get_user(context.bot, b)
                if ua is None or ub is None:
                    await q.answer("Expired - start a new challenge.", show_alert=True)
                    return
                url = await battle_url(context.bot, ua, ub, record=False)
            kb = battle_kb(a, b)
        await context.bot.edit_message_media(
            inline_message_id=inline_id, media=InputMediaPhoto(media=url), reply_markup=kb
        )
    except TelegramError as e:
        log.info("nav edit skipped: %s", e)
    except Exception:
        log.exception("nav failed")


async def on_private(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        f"I work inline. In any chat type @{context.bot.username} and send the challenge."
    )


async def on_error(update, context: ContextTypes.DEFAULT_TYPE):
    log.error("unhandled error", exc_info=context.error)


async def post_init(app):
    global LOOP, BOT
    LOOP, BOT = asyncio.get_running_loop(), app.bot
    try:
        await app.bot.delete_my_commands()  # inline only - no commands
    except Exception as e:
        log.warning("delete_my_commands failed: %s", e)


# ------------------------------------------------------------------ main
def main():
    if not TOKEN:
        log.error("BOT_TOKEN is not set. Add it in Render -> Environment, then redeploy.")
        sys.exit(1)
    if not BASE_URL:
        log.warning("RENDER_EXTERNAL_URL / PUBLIC_URL not set - inline images will not work")

    load_records()
    threading.Thread(target=run_web, name="web", daemon=True).start()
    threading.Thread(target=prewarm, name="prewarm", daemon=True).start()
    log.info("health + image server on port %s (public: %s)", PORT, BASE_URL or "unset")

    app = Application.builder().token(TOKEN).post_init(post_init).build()
    app.add_handler(InlineQueryHandler(on_inline))
    app.add_handler(CallbackQueryHandler(cb_accept, pattern=r"^acc:\d+$"))
    app.add_handler(CallbackQueryHandler(cb_nav, pattern=r"^(lb|rf|bk):\d+:\d+$"))
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & ~filters.VIA_BOT, on_private))
    app.add_error_handler(on_error)

    log.info("starting polling")
    app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception:
        log.exception("fatal error on startup")
        raise
