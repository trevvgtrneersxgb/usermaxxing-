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
    u = USERS.get(uid)
    name = img_safe(u.first_name) if u else "Someone"
    return jpg(cached(f"banner:{name}", 600, lambda: make_challenge_banner(name).getvalue()))


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
        photo_width=1280, photo_height=720,
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
