"""Image generation for the Telemaxxing bot (pure Pillow, no extra deps)."""
import io
import math
import os
import random
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps, ImageStat

W, H = 1280, 720
GOLD = (255, 200, 40)
CYAN = (0, 210, 255)
PINK = (255, 60, 180)
GREEN = (120, 255, 60)
RED = (255, 50, 50)
WHITE = (255, 255, 255)

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

BASE_DIR = Path(__file__).resolve().parent
# Drop a bold .ttf next to this file named font.ttf (e.g. Anton or Bebas Neue
# from Google Fonts) for a much cooler look, or set FONT_PATH.
FONT_CANDIDATES = [
    os.environ.get("FONT_PATH", ""),
    str(BASE_DIR / "font.ttf"),
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "DejaVuSans-Bold.ttf",
    "arialbd.ttf",
]
_FONTS = {}


def font(size):
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
        f = ImageFont.load_default(size)  # Pillow >= 10.1
    _FONTS[size] = f
    return f


def img_safe(text, fallback="Player"):
    """Drop emoji / characters outside the BMP that fonts can't draw."""
    cleaned = "".join(ch for ch in text if ord(ch) <= 0xFFFF).strip()
    return cleaned or fallback


def tier_for(score):
    for limit, name in (
        (50, "NPC"),
        (80, "SIDE CHARACTER"),
        (110, "MAIN CHARACTER"),
        (140, "CERTIFIED MOGGER"),
    ):
        if score < limit:
            return name
    return "FINAL BOSS"


# ------------------------------------------------------------ pfp analysis
def pic_quality(img):
    """0-30 points from how sharp / well lit / colourful the picture is."""
    if img is None:
        return 0
    small = ImageOps.fit(img.convert("RGB"), (256, 256))
    gray = small.convert("L")
    sharp = ImageStat.Stat(gray.filter(ImageFilter.FIND_EDGES)).stddev[0]
    bright = ImageStat.Stat(gray).mean[0]
    sat = ImageStat.Stat(small.convert("HSV")).mean[1]
    pts = (
        min(sharp / 35, 1) * 12
        + max(0, 1 - abs(bright - 135) / 135) * 8
        + min(sat / 140, 1) * 10
    )
    return int(round(pts))


# ------------------------------------------------------------ drawing helpers
def gradient(size, top, bottom):
    w, h = size
    mask = Image.linear_gradient("L").resize((w, h))
    return Image.composite(Image.new("RGB", size, bottom), Image.new("RGB", size, top), mask)


def blob(img, center, radius, color, alpha):
    layer = Image.new("RGBA", img.size, color + (0,))
    cx, cy = center
    ImageDraw.Draw(layer).ellipse(
        (cx - radius, cy - radius, cx + radius, cy + radius), fill=color + (alpha,)
    )
    img.alpha_composite(layer.filter(ImageFilter.GaussianBlur(radius // 2)))


def glow_ring(img, box, color, width, blur=16):
    layer = Image.new("RGBA", img.size, color + (0,))
    ImageDraw.Draw(layer).ellipse(box, outline=color + (255,), width=width)
    img.alpha_composite(layer.filter(ImageFilter.GaussianBlur(blur)))


def glow_text(img, xy, text, f, color, blur=12, fill=WHITE, stroke=0):
    layer = Image.new("RGBA", img.size, color + (0,))
    ImageDraw.Draw(layer).text(xy, text, font=f, fill=color + (255,), anchor="mm", stroke_width=6)
    img.alpha_composite(layer.filter(ImageFilter.GaussianBlur(blur)))
    ImageDraw.Draw(img).text(
        xy, text, font=f, fill=fill, anchor="mm", stroke_width=stroke, stroke_fill=(0, 0, 0)
    )


def panel(img, box, radius=28, fill=(8, 10, 24, 160), outline=(255, 255, 255, 45)):
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ImageDraw.Draw(layer).rounded_rectangle(box, radius, fill=fill, outline=outline, width=2)
    img.alpha_composite(layer)


def background():
    bg = gradient((W, H), (10, 12, 28), (44, 12, 62)).convert("RGBA")
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ld = ImageDraw.Draw(layer)
    for x in range(-H, W, 90):
        ld.line([(x, H), (x + H, 0)], fill=(255, 255, 255, 9), width=28)
    bg.alpha_composite(layer)
    blob(bg, (210, 340), 380, CYAN, 80)
    blob(bg, (1070, 340), 380, PINK, 80)
    return bg


def circle_crop(img, size):
    img = ImageOps.fit(img.convert("RGB"), (size, size))
    mask = Image.new("L", (size * 4, size * 4), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, size * 4 - 1, size * 4 - 1), fill=255)
    mask = mask.resize((size, size), Image.LANCZOS)
    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    out.paste(img.convert("RGBA"), (0, 0), mask)
    return out


def placeholder(size, letter):
    base = gradient((size, size), (70, 82, 125), (28, 32, 58))
    ImageDraw.Draw(base).text(
        (size / 2, size / 2), letter, font=font(int(size * 0.5)), fill=(205, 212, 232), anchor="mm"
    )
    return base


def crown(d, cx, y, w=110, h=52):
    x0, x1 = cx - w / 2, cx + w / 2
    pts = [
        (x0, y + h), (x0, y + h * 0.25), (cx - w / 4, y + h * 0.62), (cx, y),
        (cx + w / 4, y + h * 0.62), (x1, y + h * 0.25), (x1, y + h),
    ]
    d.polygon(pts, fill=GOLD, outline=(160, 110, 0))
    d.rectangle((x0, y + h - 9, x1, y + h), fill=(220, 150, 20))
    for px in (x0, cx, x1):
        py = y + h * 0.25 if px != cx else y
        d.ellipse((px - 7, py - 7, px + 7, py + 7), fill=(255, 245, 190), outline=(160, 110, 0))


def confetti(img, cx, cy, seed):
    rnd = random.Random(seed)
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ld = ImageDraw.Draw(layer)
    for _ in range(46):
        ang = rnd.uniform(0, 2 * math.pi)
        r = rnd.uniform(172, 228)
        x, y = cx + math.cos(ang) * r, cy + math.sin(ang) * r * 0.92
        col = rnd.choice([GOLD, CYAN, PINK, GREEN, WHITE])
        s = rnd.randint(6, 13)
        ld.rectangle((x, y, x + s, y + s * 0.5), fill=col + (235,))
    img.alpha_composite(layer)


def mogged_banner(width):
    bw, bh = width, 112
    layer = Image.new("RGBA", (bw, bh), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    d.rectangle([0, 0, bw, bh], fill=GREEN + (255,))
    d.rectangle([0, 0, bw, 8], fill=(20, 20, 20, 255))
    d.rectangle([0, bh - 8, bw, bh], fill=(20, 20, 20, 255))
    d.text((bw / 2, bh / 2), "MOGGED!", font=font(76), fill=RED, anchor="mm",
           stroke_width=5, stroke_fill=(0, 0, 0))
    return layer.rotate(12, expand=True, resample=Image.BICUBIC)


def team_dim(color, k=0.42):
    return tuple(int(c * k) for c in color)


# ------------------------------------------------------------ battle card
def draw_fighter(img, cx, color, p, is_winner, seed):
    S, y = 300, 170
    x = cx - S // 2
    if is_winner:
        confetti(img, cx, y + S // 2, seed)
        glow_ring(img, (x - 14, y - 14, x + S + 14, y + S + 14), GOLD, 16, blur=20)
    glow_ring(img, (x - 8, y - 8, x + S + 8, y + S + 8), color, 10)

    pic = p.get("pfp") or placeholder(S, (p["name"][:1] or "?").upper())
    pic = ImageOps.fit(pic.convert("RGB"), (S, S))
    if not is_winner:
        pic = Image.blend(ImageOps.grayscale(pic).convert("RGB"), Image.new("RGB", pic.size, (0, 0, 0)), 0.38)
    img.alpha_composite(circle_crop(pic, S), (x, y))

    d = ImageDraw.Draw(img)
    d.ellipse((x - 7, y - 7, x + S + 7, y + S + 7), outline=GOLD if is_winner else color, width=7)
    if is_winner:
        crown(d, cx, y - 66)
    else:
        banner = mogged_banner(S + 30)
        img.paste(banner, (cx - banner.width // 2, y + S // 2 - banner.height // 2), banner)

    d = ImageDraw.Draw(img)
    d.text((cx, y + S + 46), p["name"][:14], font=font(40), fill=WHITE, anchor="mm",
           stroke_width=2, stroke_fill=(0, 0, 0))
    d.text((cx, y + S + 88), p["tier"], font=font(24), fill=color, anchor="mm")
    d.text((cx, y + S + 142), f"{p['score']} PTS", font=font(52),
           fill=GOLD if is_winner else (170, 178, 195), anchor="mm",
           stroke_width=2, stroke_fill=(0, 0, 0))


def draw_stats(img, a, b):
    panel(img, (392, 160, 888, 625))
    d = ImageDraw.Draw(img)
    cx, bar_max = 640, 205
    for i, (key, label) in enumerate(ROWS):
        y0 = 188 + i * 61
        va, vb = a["parts"].get(key, 0), b["parts"].get(key, 0)
        d.text((cx, y0), label, font=font(19), fill=(190, 198, 218), anchor="mm")
        mx = MAXES[key]
        for val, other, color, side in ((va, vb, CYAN, -1), (vb, va, PINK, 1)):
            length = max(7, int(bar_max * val / mx)) if mx else 7
            col = color if val >= other else team_dim(color)
            if side < 0:
                box = (cx - 10 - length, y0 + 16, cx - 10, y0 + 36)
                tx, anchor = box[0] - 8, "rm"
            else:
                box = (cx + 10, y0 + 16, cx + 10 + length, y0 + 36)
                tx, anchor = box[2] + 8, "lm"
            d.rounded_rectangle(box, 10, fill=col)
            d.text((tx, y0 + 26), str(val), font=font(20),
                   fill=WHITE if val >= other else (140, 148, 168), anchor=anchor)


def make_battle_image(a, b, winner_is_a):
    """a / b: dicts with name, score, tier, parts{}, pfp (PIL image or None)."""
    img = background()
    glow_text(img, (640, 46), "PROFILE BATTLE", font(52), CYAN, blur=14)

    # VS badge
    glow_ring(img, (602, 74, 678, 150), GREEN, 8, blur=10)
    d = ImageDraw.Draw(img)
    d.ellipse((602, 74, 678, 150), fill=(10, 14, 30), outline=GREEN, width=4)
    d.text((640, 112), "VS", font=font(36), fill=GREEN, anchor="mm")

    seed = a["score"] * 31 + b["score"]
    draw_fighter(img, 210, CYAN, a, winner_is_a, seed)
    draw_fighter(img, 1070, PINK, b, not winner_is_a, seed + 1)
    draw_stats(img, a, b)

    win, lose = (a, b) if winner_is_a else (b, a)
    d = ImageDraw.Draw(img)
    d.text((640, 672), f"{win['name'][:12].upper()} MOGGED {lose['name'][:12].upper()}",
           font=font(40), fill=GOLD, anchor="mm", stroke_width=3, stroke_fill=(0, 0, 0))

    buf = io.BytesIO()
    img.convert("RGB").save(buf, "JPEG", quality=93)
    buf.seek(0)
    return buf


# ------------------------------------------------------------ challenge banner
def _sword(d, hilt, tip):
    hx, hy = hilt
    tx, ty = tip
    dx, dy = tx - hx, ty - hy
    length = math.hypot(dx, dy)
    ux, uy = dx / length, dy / length
    px, py = -uy, ux
    gx, gy = hx + ux * length * 0.22, hy + uy * length * 0.22
    d.line([(hx, hy), (gx, gy)], fill=(150, 90, 40), width=16)
    d.line([(gx, gy), (tx, ty)], fill=(205, 212, 228), width=20)
    d.line([(gx, gy), (tx, ty)], fill=WHITE, width=7)
    d.line([(gx - px * 58, gy - py * 58), (gx + px * 58, gy + py * 58)], fill=GOLD, width=18)
    d.ellipse((hx - 15, hy - 15, hx + 15, hy + 15), fill=GOLD)


def make_challenge_banner():
    img = background()
    blob(img, (640, 360), 300, GREEN, 60)
    swords = Image.new("RGBA", img.size, (0, 0, 0, 0))
    sd = ImageDraw.Draw(swords)
    _sword(sd, (430, 560), (850, 190))
    _sword(sd, (850, 560), (430, 190))
    img.alpha_composite(swords.filter(ImageFilter.GaussianBlur(14)))
    img.alpha_composite(swords)
    glow_text(img, (640, 92), "TELEMAXXING", font(116), GREEN, blur=18, stroke=3)
    d = ImageDraw.Draw(img)
    d.text((640, 626), "Challenge someone to a profile battle", font=font(40),
           fill=WHITE, anchor="mm", stroke_width=2, stroke_fill=(0, 0, 0))
    d.text((640, 680), "tap to send the challenge  -  who mogs who?", font=font(24),
           fill=(165, 175, 198), anchor="mm")
    buf = io.BytesIO()
    img.convert("RGB").save(buf, "JPEG", quality=93)
    buf.seek(0)
    return buf
