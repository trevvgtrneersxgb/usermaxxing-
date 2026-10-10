"""Telemaxxing - inline @username battle bot (fast + simple).

Battle = whose @username is better.
  * looks up the @ on Fragment (sold price / auction / asking price in TON)
  * checks if the @ is a real word (regular words are cool)
  * rates length, digits, underscores
  * shows a score out of 10, a tier, and a side-by-side stats table

Env vars: BOT_TOKEN (required), PORT + RENDER_EXTERNAL_URL (Render sets both),
          PUBLIC_URL (custom domain only), FONT_PATH (optional)
requirements.txt:
    python-telegram-bot==21.6
    Flask==3.1.3
    Pillow
    httpx
"""
import asyncio
import hashlib
import io
import json
import logging
import math
import os
import re
import sys
import threading
import time

import httpx
from flask import Flask, Response, abort, jsonify
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont, ImageOps
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

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                    stream=sys.stdout)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("telemaxxing")

TOKEN = os.environ.get("BOT_TOKEN", "").strip()
PORT = int(os.environ.get("PORT", "10000"))


def _clean_url(u):
    u = (u or "").strip().rstrip("/")
    if not u or "your-app" in u or "example.com" in u:
        return ""
    if not u.startswith("http"):
        u = "https://" + u
    return u


BASE_URL = _clean_url(os.environ.get("PUBLIC_URL")) or _clean_url(os.environ.get("RENDER_EXTERNAL_URL"))
BASE_DIR = Path(__file__).resolve().parent

# ======================================================================
# @USERNAME RATING
# ======================================================================
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124 Safari/537.36",
      "Accept-Language": "en-US,en;q=0.9"}

COMMON_WORDS = set("""
king queen love money cash gold rich boss lord chief prince princess angel devil demon ghost shadow
fire ice storm thunder lightning wolf lion tiger bear eagle hawk snake dragon fox panda shark cobra
alpha omega sigma delta beta gamma ninja samurai pirate hero villain legend myth wizard magic witch
star moon sun sky cloud rain snow wind earth ocean river forest mountain stone rock iron steel silver
diamond ruby pearl crystal black white red blue green pink purple orange yellow dark light night day
dream hope faith truth peace war power speed force energy chaos order mind soul spirit heart brain
code hack game games gamer play player pixel robot cyber tech data byte bit web net link
crypto coin token bitcoin ton wallet trade market bank gift luck win winner victory champion
cool nice best good great super mega ultra hyper epic wild crazy mad happy sad lazy fast slow
smile laugh joke fun party music song dance beat bass rap jazz art draw paint photo video
food pizza burger cake candy cookie coffee tea milk water beer wine sugar honey spice salt
car bike plane train ship road city town home house room door key lock box bag book pen
man woman boy girl kid baby mom dad bro sis friend buddy mate team crew gang club squad
real fake true false free open secret hidden lost found top max min one two three zero
""".split())

_WORD_CACHE = {}
_FRAG_CACHE = {}


def check_word(word):
    """True if `word` is a real english word. Local list first, then a quick dictionary API."""
    w = (word or "").lower()
    if len(w) < 3 or not w.isalpha():
        return False
    if w in COMMON_WORDS:
        return True
    hit = _WORD_CACHE.get(w)
    if hit is not None:
        return hit
    ok = False
    try:
        r = httpx.get(f"https://api.dictionaryapi.dev/api/v2/entries/en/{w}", timeout=2.5)
        ok = r.status_code == 200
    except Exception as e:
        log.info("dictionary check failed for %s: %s", w, e)
        return False  # do not cache failures
    _WORD_CACHE[w] = ok
    return ok


def fragment_lookup(name):
    """Returns {'status': sold|auction|sale|taken|none, 'ton': float|None} or None if unreachable."""
    key = (name or "").lower()
    hit = _FRAG_CACHE.get(key)
    if hit and hit[0] > time.time():
        return hit[1]
    try:
        r = httpx.get(f"https://fragment.com/username/{key}", headers=UA, timeout=4.0,
                      follow_redirects=True)
    except Exception as e:
        log.info("fragment fetch failed for %s: %s", key, e)
        return None
    if r.status_code != 200:
        log.info("fragment %s -> HTTP %s", key, r.status_code)
        return None
    html = r.text
    ton = None
    m = re.search(r'icon-ton[^>]*>\s*([0-9][0-9,\.]*)\s*<', html)
    if m:
        try:
            ton = float(m.group(1).replace(",", ""))
        except ValueError:
            ton = None
    sm = re.search(r'tm-section-header-status[^>]*>\s*([^<]+?)\s*<', html)
    label = (sm.group(1).lower() if sm else "")
    if "sold" in label:
        status = "sold"
    elif "auction" in label:
        status = "auction"
    elif "unavail" in label or "taken" in label:
        status = "taken"
    elif "avail" in label or "sale" in label:
        status = "sale"
    else:
        status = "none"
    res = {"status": status, "ton": ton}
    log.info("fragment %s -> %s", key, res)
    _FRAG_CACHE[key] = (time.time() + 6 * 3600, res)
    return res


def estimate_ton(name, is_word):
    n = len(name)
    base = {4: 500, 5: 120, 6: 25, 7: 10}.get(n, 3 if n <= 9 else 1)
    return base * (5 if is_word else 1)


def rate_username(name, frag, is_word):
    """0-100 rating of the @ itself (one decimal). Shown as score/10 in the image."""
    if not name:
        return 0.0
    n = len(name)
    s = {4: 35, 5: 30, 6: 22, 7: 15, 8: 10}.get(n, 4 if n <= 12 else 1)
    if is_word:
        s += 30
    digits = sum(ch.isdigit() for ch in name)
    s -= min(24, digits * 8)
    s -= min(20, name.count("_") * 10)
    if name.isalpha():
        s += 5
    ton = frag.get("ton") if frag else None
    if ton:
        s += min(35, math.log10(ton + 1) * 8)
    return round(max(1.0, min(100.0, float(s))), 1)


TIER_LIST = [
    (20, "TRASH @", (150, 158, 178)),
    (40, "MID @", (110, 230, 140)),
    (60, "CLEAN @", (0, 210, 255)),
    (80, "RARE @", (255, 60, 180)),
    (101, "GOD TIER @", (255, 200, 40)),
]


def tier_of(rating):
    for lim, name, col in TIER_LIST:
        if rating < lim:
            return name, col
    return TIER_LIST[-1][1], TIER_LIST[-1][2]


def fmt_ton(x):
    if x is None:
        return "?"
    return f"{x:,.0f}" if x >= 10 else f"{x:,.1f}"


def cost_lines(name, frag, is_word):
    """(main line, small line) shown under the @ rating."""
    if not name:
        return "NO @USERNAME", ""
    word_txt = "REAL WORD" if is_word else f"{len(name)} LETTERS"
    if frag and frag["status"] == "sold" and frag["ton"]:
        return f"SOLD FOR {fmt_ton(frag['ton'])} TON", word_txt
    if frag and frag["status"] == "auction" and frag["ton"]:
        return f"AUCTION {fmt_ton(frag['ton'])} TON", word_txt
    if frag and frag["status"] == "sale" and frag["ton"]:
        return f"ASKING {fmt_ton(frag['ton'])} TON", word_txt
    if frag and frag["status"] == "taken":
        return "TAKEN - NOT FOR SALE", word_txt
    est = estimate_ton(name, is_word)
    return f"EST ~{fmt_ton(est)} TON", word_txt + (" | NOT ON FRAGMENT" if frag else "")


STAT_LABELS = ["Avatar", "Username", "Real word", "Clean", "Collectible", "Rarity", "Account age",
               "Premium"]


def account_age(uid):
    """Rough join year from the Telegram user id (lower id = older account)."""
    for lim, year, score in ((1e8, "2015-", 10), (5e8, "~2017", 9), (1e9, "~2019", 8), (2e9, "~2020", 7),
                             (5e9, "~2021", 6), (6e9, "~2022", 5), (7e9, "~2023", 4), (8e9, "~2024", 3)):
        if uid < lim:
            return year, score
    return "2025+", 2


def stat_facts(uid, name, frag, is_word, premium, has_pic, rating, tier):
    """List of (value text, score 0-10) in the same order as STAT_LABELS."""
    age = account_age(uid)
    prem = ("Yes" if premium else "No", 6 if premium else 2)
    ava = ("Yes" if has_pic else "None", 7 if has_pic else 2)
    if not name:
        return [ava, ("-", 0), ("-", 0), ("-", 0), ("-", 0), ("NONE", 0), age, prem]
    n = len(name)
    digits = sum(ch.isdigit() for ch in name)
    unders = name.count("_")
    ton = (frag or {}).get("ton")
    clean = max(0.5, 10 - digits * 3 - unders * 4)
    if digits or unders:
        clean_txt = f"{digits}d {unders}_"
    else:
        clean_txt = "Perfect"
    coll = (f"{fmt_ton(ton)} TON", min(10, math.log10(ton + 1) * 2.5)) if ton else ("None", 0)
    return [
        ava,
        (f"{n} letters", {4: 10, 5: 9, 6: 7, 7: 5, 8: 4}.get(n, 2 if n <= 12 else 1)),
        ("Yes" if is_word else "No", 9 if is_word else 2),
        (clean_txt, clean),
        coll,
        (tier.replace(" @", ""), rating / 10),
        age,
        prem,
    ]


# ======================================================================
# IMAGES (simple + fast)
# ======================================================================
W, H = 960, 540          # banner + leaderboard
BW, BH = 800, 1040       # battle card (portrait, like a profile battle)
GOLD = (255, 200, 40)
CYAN = (0, 210, 255)
PINK = (255, 60, 180)
GREEN = (120, 255, 60)
RED = (255, 50, 50)
WHITE = (255, 255, 255)
GREY = (150, 158, 178)

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
            f = ImageFont.load_default(size)
        except TypeError:
            f = ImageFont.load_default()
    _FONTS[size] = f
    return f


def fit_font(text, max_w, start, minimum=14):
    size = start
    while size > minimum:
        f = font(size)
        if f.getlength(text) <= max_w:
            return f
        size -= 2
    return font(minimum)


def img_safe(text, fallback="Player"):
    cleaned = "".join(ch for ch in (text or "") if ord(ch) <= 0xFFFF).strip()
    return cleaned or fallback


def darken(c, k):
    return tuple(int(v * k) for v in c)


def lighten(c, k):
    return tuple(min(255, int(v + (255 - v) * k)) for v in c)


def jpeg(img):
    buf = io.BytesIO()
    img.convert("RGB").save(buf, "JPEG", quality=88)
    return buf.getvalue()


_BG = {}


def background(w=W, h=H, dark=False):
    key = (w, h, dark)
    if key not in _BG:
        mask = Image.linear_gradient("L").resize((w, h))
        top, bot = ((26, 21, 14), (9, 9, 11)) if dark else ((30, 10, 50), (8, 10, 24))
        _BG[key] = Image.composite(Image.new("RGB", (w, h), top), Image.new("RGB", (w, h), bot), mask)
    return _BG[key].copy()


def circle_paste(img, cx, cy, r, source):
    """Paste `source` (RGB image or solid colour) as an anti-aliased circle."""
    size = r * 2
    mask = Image.new("L", (size * 3, size * 3), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, size * 3 - 1, size * 3 - 1), fill=255)
    mask = mask.resize((size, size), Image.LANCZOS)
    if isinstance(source, tuple):
        source = Image.new("RGB", (size, size), source)
    img.paste(source, (cx - r, cy - r), mask)


def avatar(img, cx, cy, r, pic, letter, ring, gray=False):
    circle_paste(img, cx, cy, r + 6, ring)
    if pic is not None:
        pic = ImageOps.fit(pic.convert("RGB"), (r * 2, r * 2))
    else:
        pic = Image.new("RGB", (r * 2, r * 2), (60, 70, 110))
        d = ImageDraw.Draw(pic)
        d.text((r, r), letter, font=font(r), fill=(215, 222, 240), anchor="mm")
    if gray:
        pic = ImageOps.grayscale(pic).convert("RGB").point(lambda v: int(v * 0.6))
    circle_paste(img, cx, cy, r, pic)


def pill(d, cx, cy, text, color, size=20):
    f = font(size)
    w, h = int(f.getlength(text) + 36), size + 16
    d.rounded_rectangle((cx - w // 2, cy - h // 2, cx + w // 2, cy + h // 2), h // 2,
                        fill=darken(color, 0.25), outline=color, width=2)
    d.text((cx, cy), text, font=f, fill=lighten(color, 0.3), anchor="mm")


def header(d, tag, w=W):
    d.text((30, 38), "TELEMAXXING", font=font(36), fill=GREEN, anchor="lm")
    pill(d, w - 120, 38, tag, CYAN, 16)


def stamp_mogged(img, cx, cy):
    layer = Image.new("RGBA", (250, 66), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    d.rectangle((0, 6, 250, 60), fill=GREEN + (255,))
    d.rectangle((0, 6, 250, 11), fill=(20, 20, 20, 255))
    d.rectangle((0, 55, 250, 60), fill=(20, 20, 20, 255))
    d.text((125, 33), "MOGGED!", font=font(36), fill=RED, anchor="mm", stroke_width=3,
           stroke_fill=(0, 0, 0))
    layer = layer.rotate(12, expand=True, resample=Image.BICUBIC)
    img.paste(layer, (cx - layer.width // 2, cy - layer.height // 2), layer)


def crown(d, cx, y):
    w, h = 80, 38
    pts = [(cx - w // 2, y + h), (cx - w // 2, y + 8), (cx - w // 4, y + 22), (cx, y),
           (cx + w // 4, y + 22), (cx + w // 2, y + 8), (cx + w // 2, y + h)]
    d.polygon(pts, fill=GOLD, outline=(160, 110, 0))


CARD_BG = (20, 20, 25)
CARD_LINE = (48, 48, 58)
TABLE_BG = (16, 16, 20)


def record_line(uid):
    e = RECORDS.get("global", {}).get(str(uid))
    if not e:
        return "0 battles"
    return f"{e['w'] + e['l']} battles | {e['w']} wins"


def fighter(img, x0, x1, color, p, winner):
    d = ImageDraw.Draw(img)
    ring = GOLD if winner else (70, 72, 84)
    d.rounded_rectangle((x0, 100, x1, 505), 26, fill=CARD_BG, outline=ring if winner else CARD_LINE,
                        width=3 if winner else 2)
    cx = (x0 + x1) // 2
    avatar(img, cx, 195, 60, p["pfp"], (p["name"][:1] or "?").upper(), ring, gray=not winner)
    d = ImageDraw.Draw(img)
    if winner:
        crown(d, cx, 118)
    else:
        stamp_mogged(img, cx, 200)
        d = ImageDraw.Draw(img)
    mw = x1 - x0 - 30
    name = p["name"][:16]
    d.text((cx, 290), name, font=fit_font(name, mw, 30), fill=WHITE if winner else (190, 190, 198),
           anchor="mm")
    handle = f"@{p['username']}" if p["username"] else "no username"
    d.text((cx, 320), handle, font=fit_font(handle, mw, 22), fill=GREY, anchor="mm")
    pill(d, cx, 355, p["tier"], p["tier_color"] if winner else (120, 124, 140), 16)
    score = f"{p['rating'] / 10:.2f}"
    d.text((cx, 412), score, font=font(62), fill=WHITE if winner else (150, 150, 158), anchor="mm")
    d.text((cx, 458), p["cost"], font=fit_font(p["cost"], mw, 18), fill=GOLD if winner else GREY,
           anchor="mm")
    d.text((cx, 483), record_line(p["id"]), font=font(14), fill=(100, 104, 118), anchor="mm")


def stats_table(img, a, b, a_wins):
    d = ImageDraw.Draw(img)
    x0, x1, y0 = 30, 770, 585
    row_h = 46
    n = len(STAT_LABELS)
    d.rounded_rectangle((x0, y0, x1, y0 + 20 + n * row_h), 22, fill=TABLE_BG, outline=CARD_LINE, width=2)
    lx, rx, maxw = x0 + 24, x1 - 24, 270
    for i, label in enumerate(STAT_LABELS):
        ry = y0 + 32 + i * row_h
        (va, sa), (vb, sb) = a["facts"][i], b["facts"][i]
        a_better, b_better = sa > sb, sb > sa
        d.text((400, ry), label, font=font(15), fill=(120, 124, 140), anchor="mm")
        d.text((lx, ry), va, font=fit_font(va, 200, 19), fill=WHITE if a_better else (140, 144, 156),
               anchor="lm")
        d.text((rx, ry), vb, font=fit_font(vb, 200, 19), fill=WHITE if b_better else (140, 144, 156),
               anchor="rm")
        ya = ry + 17
        wa, wb = max(4, int(maxw * sa / 10)), max(4, int(maxw * sb / 10))
        d.rounded_rectangle((lx, ya, lx + wa, ya + 5), 2, fill=CYAN if a_better else (60, 62, 74))
        d.rounded_rectangle((rx - wb, ya, rx, ya + 5), 2, fill=PINK if b_better else (60, 62, 74))


def make_battle_image(a, b, winner_is_a):
    img = background(BW, BH, dark=True)
    d = ImageDraw.Draw(img)
    d.text((30, 50), "TELEMAXXING", font=font(38), fill=WHITE, anchor="lm")
    pill(d, BW - 125, 50, "USERNAME BATTLE", (130, 134, 150), 15)
    fighter(img, 30, 392, CYAN, a, winner_is_a)
    fighter(img, 408, 770, PINK, b, not winner_is_a)
    d = ImageDraw.Draw(img)
    d.ellipse((370, 175, 430, 235), fill=(10, 10, 14), outline=(70, 72, 84), width=2)
    d.text((400, 205), "VS", font=font(24), fill=WHITE, anchor="mm")

    win, lose = (a, b) if winner_is_a else (b, a)
    diff = abs(a["rating"] - b["rating"]) / 10
    d.rounded_rectangle((30, 520, 770, 566), 23, fill=TABLE_BG, outline=CARD_LINE, width=2)
    line = f"{win['name'][:14]} MOGS {lose['name'][:14]}"
    d.text((52, 543), line, font=fit_font(line, 560, 20), fill=WHITE, anchor="lm")
    pill(d, 700, 543, f"+{diff:.2f}", GOLD, 16)

    stats_table(img, a, b, winner_is_a)
    d = ImageDraw.Draw(img)
    d.text((400, 1012), "refresh to re-check your @ on Fragment", font=font(14), fill=(80, 84, 98),
           anchor="mm")
    return jpeg(img)


def make_leaderboard(rows):
    img = background()
    d = ImageDraw.Draw(img)
    header(d, "LEADERBOARD")
    if not rows:
        d.text((W // 2, 260), "NO BATTLES YET", font=font(48), fill=WHITE, anchor="mm")
        d.text((W // 2, 315), "challenge someone to start the board", font=font(22), fill=GREY, anchor="mm")
        return jpeg(img)
    medals = [GOLD, (205, 210, 222), (205, 127, 50), (90, 100, 140), (90, 100, 140)]
    for i, r in enumerate(rows[:5]):
        cy = 120 + i * 78
        total = r["w"] + r["l"]
        wr = int(round(100 * r["w"] / total)) if total else 0
        d.rounded_rectangle((40, cy - 32, W - 40, cy + 32), 16, fill=(14, 17, 36), outline=medals[i], width=2)
        circle_paste(img, 85, cy, 22, medals[i])
        d = ImageDraw.Draw(img)
        d.text((85, cy), str(i + 1), font=font(26), fill=(10, 10, 20), anchor="mm")
        nm = (r["name"] or "Player")[:16]
        d.text((135, cy), nm, font=fit_font(nm, 450, 30), fill=WHITE, anchor="lm")
        d.text((W - 190, cy), f"{r['w']}W - {r['l']}L", font=font(24), fill=GREY, anchor="rm")
        d.text((W - 70, cy), f"{wr}%", font=font(32), fill=GOLD, anchor="rm")
    return jpeg(img)


def make_challenge_banner():
    """Uses banner.png (next to this file) for inline mode; falls back to a generated banner."""
    for name in ("banner.png", "banner.jpg", "banner.jpeg", "banner.webp"):
        path = BASE_DIR / name
        if path.exists():
            try:
                im = Image.open(path).convert("RGB")
                im.thumbnail((1280, 1280), Image.LANCZOS)
                return jpeg(im)
            except Exception:
                log.exception("could not load %s", name)
    img = background()
    d = ImageDraw.Draw(img)
    d.text((W // 2, 190), "TELEMAXXING", font=font(110), fill=GREEN, anchor="mm", stroke_width=3,
           stroke_fill=(0, 0, 0))
    d.text((W // 2, 300), "WHOSE @ IS BETTER?", font=font(52), fill=WHITE, anchor="mm")
    d.text((W // 2, 360), "real words, short names and Fragment price win", font=font(24), fill=GREY,
           anchor="mm")
    pill(d, W // 2, 440, "TAP  ACCEPT  TO BATTLE", CYAN, 24)
    return jpeg(img)


# ======================================================================
# WEB (serves images to Telegram)
# ======================================================================
web = Flask(__name__)
_CACHE = {}
STATIC = {}


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


def static_img(kind):
    if kind not in STATIC:
        if kind == "banner":
            STATIC[kind] = make_challenge_banner()
        elif kind == "banner_thumb":
            im = Image.open(io.BytesIO(static_img("banner")))
            im.thumbnail((320, 320), Image.LANCZOS)
            STATIC[kind] = jpeg(im)
    return STATIC[kind]


def jpg(data):
    if not data:
        abort(404)
    return Response(data, mimetype="image/jpeg", headers={"Cache-Control": "public, max-age=60"})


@web.get("/")
def health():
    return "telemaxxing ok"


@web.get("/healthz")
def healthz():
    return "ok"


@web.get("/status")
def status():
    return f"base_url={BASE_URL or 'UNSET'}"


@web.get("/fragtest/<name>")
def fragtest(name):
    """Open /fragtest/durov in a browser to see what the Fragment lookup returns."""
    frag = fragment_lookup(name)
    word = check_word(name)
    return jsonify({"name": name, "fragment": frag, "is_word": word,
                    "rating": rate_username(name, frag, word)})


@web.get("/banner/<int:uid>.jpg")
def route_banner(uid):
    return jpg(static_img("banner"))


@web.get("/thumb/banner.jpg")
def route_thumb():
    return jpg(static_img("banner_thumb"))


@web.get("/top.jpg")
def route_top():
    return jpg(cached("top", 30, lambda: make_leaderboard(leaderboard_rows("global"))))


@web.get("/img/<token>.jpg")
def route_img(token):
    hit = _CACHE.get(f"img:{token}")
    return jpg(hit[1] if hit and hit[0] > time.time() else None)


def run_web():
    logging.getLogger("werkzeug").setLevel(logging.WARNING)
    web.run(host="0.0.0.0", port=PORT, threaded=True, use_reloader=False)


def prewarm():
    try:
        static_img("banner")
        static_img("banner_thumb")
        cached("top", 30, lambda: make_leaderboard(leaderboard_rows("global")))
        log.info("prewarm: images ready")
    except Exception:
        log.exception("prewarm failed")
        return
    if not BASE_URL:
        return
    for attempt in range(6):
        time.sleep(3)
        try:
            r = httpx.get(f"{BASE_URL}/thumb/banner.jpg", timeout=15, follow_redirects=True)
            if r.status_code == 200 and r.headers.get("content-type", "").startswith("image/"):
                log.info("SELF-TEST OK: %s/thumb/banner.jpg is publicly reachable", BASE_URL)
                return
            log.warning("SELF-TEST got %s", r.status_code)
        except Exception as e:
            log.warning("SELF-TEST attempt %d failed: %s", attempt + 1, e)
    log.error("SELF-TEST FAILED: %s not reachable from outside", BASE_URL)


# ======================================================================
# RECORDS
# ======================================================================
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
    except Exception as e:
        log.debug("could not save records: %s", e)


def record_result(chat_id, win, lose):
    chat = RECORDS.setdefault(str(chat_id), {})
    for p, key in ((win, "w"), (lose, "l")):
        e = chat.setdefault(str(p["id"]), {"name": p["name"], "w": 0, "l": 0})
        e["name"] = p["name"]
        e[key] += 1
    save_records()


def leaderboard_rows(chat_id):
    rows = list(RECORDS.get(str(chat_id), {}).values())
    rows.sort(key=lambda r: (r["w"], r["w"] / max(1, r["w"] + r["l"])), reverse=True)
    return rows[:5]


_LAST_USE = {}


def on_cooldown(uid, secs):
    now = time.monotonic()
    if now - _LAST_USE.get(uid, 0.0) < secs:
        return True
    _LAST_USE[uid] = now
    return False


# ======================================================================
# PROFILES + BATTLES
# ======================================================================
USERS = {}
PROFILES = {}   # uid -> (expires, profile)


def remember(user):
    if len(USERS) > 5000:
        USERS.clear()
    USERS[user.id] = user


async def fetch_pfp(bot, uid):
    """Small profile photo only (fast)."""
    try:
        photos = await bot.get_user_profile_photos(uid, limit=1)
        if not photos.total_count:
            return None
        sizes = photos.photos[0]
        tg_file = await sizes[min(1, len(sizes) - 1)].get_file()
        raw = await tg_file.download_as_bytearray()
        pic = Image.open(io.BytesIO(bytes(raw)))
        pic.load()
        return pic
    except Exception as e:
        log.info("pfp failed for %s: %s", uid, e)
        return None


async def build_profile(bot, user, fresh=False):
    hit = PROFILES.get(user.id)
    if hit and hit[0] > time.time() and not fresh:
        return hit[1]
    uname = (user.username or "").strip()
    if fresh:
        _FRAG_CACHE.pop(uname.lower(), None)
    if uname:
        pic, frag, word = await asyncio.gather(
            fetch_pfp(bot, user.id),
            asyncio.to_thread(fragment_lookup, uname),
            asyncio.to_thread(check_word, uname),
        )
    else:
        pic, frag, word = await fetch_pfp(bot, user.id), None, False
    rating = rate_username(uname, frag, word)
    tier, tcol = tier_of(rating) if uname else ("NO @", GREY)
    cost, sub = cost_lines(uname, frag, word)
    premium = bool(getattr(user, "is_premium", False))
    prof = {
        "id": user.id,
        "name": img_safe(user.first_name or user.full_name or "", "Player"),
        "username": uname,
        "rating": rating,
        "tier": tier,
        "tier_color": tcol,
        "cost": cost,
        "sub": sub,
        "ton": (frag or {}).get("ton") or 0,
        "pfp": pic,
        "facts": stat_facts(user.id, uname, frag, word, premium, pic is not None, rating, tier),
    }
    PROFILES[user.id] = (time.time() + 600, prof)
    if len(PROFILES) > 500:
        PROFILES.clear()
    return prof


def decide_winner(a, b):
    if a["rating"] != b["rating"]:
        return a["rating"] > b["rating"]
    if a["ton"] != b["ton"]:
        return a["ton"] > b["ton"]
    return hashlib.sha256(f"{a['id']}:{b['id']}".encode()).digest()[0] % 2 == 0


def battle_kb(a, b):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Leaderboard", callback_data=f"lb:{a}:{b}")],
        [InlineKeyboardButton("Refresh profile", callback_data=f"rf:{a}:{b}")],
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


async def battle_url(bot, user_a, user_b, record=True, fresh=False):
    t0 = time.perf_counter()
    a, b = await asyncio.gather(build_profile(bot, user_a, fresh), build_profile(bot, user_b, fresh))
    winner_is_a = decide_winner(a, b)
    if record:
        win, lose = (a, b) if winner_is_a else (b, a)
        record_result("global", win, lose)
    data = await asyncio.to_thread(make_battle_image, a, b, winner_is_a)
    token = hashlib.sha256(os.urandom(16)).hexdigest()[:24]
    cached(f"img:{token}", 900, lambda: data)
    url = f"{BASE_URL}/img/{token}.jpg"
    _CACHE[f"pair:{user_a.id}:{user_b.id}"] = (time.time() + 900, url)
    log.info("battle built in %.2fs", time.perf_counter() - t0)
    return url


# ======================================================================
# HANDLERS
# ======================================================================
async def on_inline(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.inline_query
    user = q.from_user
    remember(user)
    if not BASE_URL:
        log.error("No valid PUBLIC_URL / RENDER_EXTERNAL_URL")
        await q.answer([], cache_time=5)
        return
    # start warming this user's @ so the battle is instant
    asyncio.create_task(build_profile(context.bot, user))
    t = int(time.time() // 60)
    bw, bh = Image.open(io.BytesIO(static_img("banner"))).size
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("ACCEPT CHALLENGE", callback_data=f"acc:{user.id}")]])
    result = InlineQueryResultPhoto(
        id=f"ch{user.id}_{t}",
        photo_url=f"{BASE_URL}/banner/{user.id}.jpg?t={t}",
        thumbnail_url=f"{BASE_URL}/thumb/banner.jpg?v=5",
        photo_width=bw, photo_height=bh,
        title="Challenge: whose @ is better?",
        description="First person to accept fights you",
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
    if on_cooldown(q.from_user.id, 3.0):
        await q.answer("Slow down a sec.", show_alert=True)
        return
    challenger = await get_user(context.bot, challenger_id)
    if challenger is None:
        await q.answer("Challenge expired - post a new one.", show_alert=True)
        return
    claimed.add(inline_id)
    remember(q.from_user)
    await q.answer("Comparing @s...")
    try:
        url = await battle_url(context.bot, challenger, q.from_user)
        await context.bot.edit_message_media(
            inline_message_id=inline_id, media=InputMediaPhoto(media=url),
            reply_markup=battle_kb(challenger_id, q.from_user.id))
    except Exception:
        claimed.discard(inline_id)
        log.exception("inline battle failed")


async def cb_nav(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
    if on_cooldown(q.from_user.id, 2.0):
        await q.answer("Slow down a sec.", show_alert=True)
        return
    remember(q.from_user)
    await q.answer()
    try:
        if action == "lb":
            cached("top", 30, lambda: make_leaderboard(leaderboard_rows("global")))
            url, kb = f"{BASE_URL}/top.jpg?t={int(time.time())}", board_kb(a, b)
        else:
            hit = _CACHE.get(f"pair:{a}:{b}")
            if action == "bk" and hit and hit[0] > time.time():
                url = hit[1]
            else:
                ua, ub = await get_user(context.bot, a), await get_user(context.bot, b)
                if ua is None or ub is None:
                    await q.answer("Expired - start a new challenge.", show_alert=True)
                    return
                url = await battle_url(context.bot, ua, ub, record=False, fresh=(action == "rf"))
            kb = battle_kb(a, b)
        await context.bot.edit_message_media(
            inline_message_id=inline_id, media=InputMediaPhoto(media=url), reply_markup=kb)
    except TelegramError as e:
        log.info("nav edit skipped: %s", e)
    except Exception:
        log.exception("nav failed")


async def on_private(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        f"I work inline. In any chat type @{context.bot.username} and send the challenge.")


async def on_error(update, context: ContextTypes.DEFAULT_TYPE):
    log.error("unhandled error", exc_info=context.error)


async def post_init(app):
    try:
        await app.bot.delete_my_commands()
    except Exception as e:
        log.warning("delete_my_commands failed: %s", e)


def main():
    if not TOKEN:
        log.error("BOT_TOKEN is not set.")
        sys.exit(1)
    if not BASE_URL:
        log.warning("No valid RENDER_EXTERNAL_URL / PUBLIC_URL - inline images will not work")
    load_records()
    threading.Thread(target=run_web, name="web", daemon=True).start()
    threading.Thread(target=prewarm, name="prewarm", daemon=True).start()
    log.info("server on port %s (public: %s)", PORT, BASE_URL or "UNSET")

    app = Application.builder().token(TOKEN).post_init(post_init).concurrent_updates(True).build()
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
