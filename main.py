import html
import io
import logging
import os
import random
import threading

from flask import Flask
from PIL import Image, ImageDraw, ImageFont, ImageOps
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InlineQueryResultCachedPhoto,
    InputMediaPhoto,
    Update,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    InlineQueryHandler,
)

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO
)
log = logging.getLogger("telemaxxing")

BOT_TOKEN = os.environ["BOT_TOKEN"]
# A chat the bot can send images to (your own user ID works, after pressing Start on the bot).
STORAGE_CHAT_ID = int(os.environ["STORAGE_CHAT_ID"])
PORT = int(os.environ.get("PORT", "10000"))

# Max random "aura" points added to each score so results aren't always the same.
RANDOM_SPICE = 15

# ---------------------------------------------------------------- keep-alive
web = Flask(__name__)


@web.route("/")
def home():
    return "Telemaxxing bot is alive"


def run_web():
    web.run(host="0.0.0.0", port=PORT)


# ---------------------------------------------------------------- fonts / images
FONT_PATHS = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "arialbd.ttf",
]


def font(size):
    for p in FONT_PATHS:
        try:
            return ImageFont.truetype(p, size)
        except OSError:
            continue
    return ImageFont.load_default(size)  # Pillow >= 10.1


def img_safe(text, fallback):
    """Strip emoji / chars the font can't draw."""
    cleaned = "".join(ch for ch in text if ord(ch) <= 0xFFFF).strip()
    return cleaned or fallback


def center_text(d, cx, y, text, f, fill, stroke=0, stroke_fill=None):
    bbox = d.textbbox((0, 0), text, font=f, stroke_width=stroke)
    w = bbox[2] - bbox[0]
    d.text(
        (cx - w / 2 - bbox[0], y),
        text,
        font=f,
        fill=fill,
        stroke_width=stroke,
        stroke_fill=stroke_fill,
    )


def make_challenge_banner():
    img = Image.new("RGB", (1000, 560), (14, 20, 28))
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, 1000, 20], fill=(120, 255, 60))
    d.rectangle([0, 540, 1000, 560], fill=(120, 255, 60))
    center_text(d, 500, 150, "TELEMAXXING", font(120), (120, 255, 60))
    center_text(d, 500, 300, "PROFILE BATTLE", font(72), (255, 255, 255))
    center_text(d, 500, 400, "who mogs who?", font(46), (150, 160, 175))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=90)
    buf.seek(0)
    return buf


def placeholder_pfp(size, letter="?"):
    img = Image.new("RGB", (size, size), (45, 55, 70))
    d = ImageDraw.Draw(img)
    center_text(d, size / 2, size / 2 - 70, letter, font(140), (120, 130, 145))
    return img


def mogged_banner(width):
    bw, bh = width + 80, 120
    layer = Image.new("RGBA", (bw, bh), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    d.rectangle([0, 0, bw, bh], fill=(120, 255, 60, 255))
    f = font(80)
    text = "MOGGED!"
    bbox = d.textbbox((0, 0), text, font=f, stroke_width=4)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    d.text(
        ((bw - tw) / 2 - bbox[0], (bh - th) / 2 - bbox[1]),
        text,
        font=f,
        fill=(255, 40, 40),
        stroke_width=4,
        stroke_fill=(0, 0, 0),
    )
    return layer.rotate(12, expand=True, resample=Image.BICUBIC)


def make_battle_image(a, b, winner_is_a):
    """a / b are dicts: name, score, pfp (PIL image or None)."""
    W, H, S = 1200, 760, 440
    img = Image.new("RGB", (W, H), (14, 20, 28))
    d = ImageDraw.Draw(img)
    positions = [(90, 90), (W - 90 - S, 90)]

    for (x, y), p, is_winner in zip(positions, (a, b), (winner_is_a, not winner_is_a)):
        pic = p["pfp"] or placeholder_pfp(S, p["name"][:1].upper() or "?")
        pic = ImageOps.fit(pic, (S, S))
        border = (255, 200, 40) if is_winner else (70, 80, 95)
        d.rectangle([x - 10, y - 10, x + S + 10, y + S + 10], fill=border)
        img.paste(pic, (x, y))
        if not is_winner:
            banner = mogged_banner(S)
            img.paste(banner, (x - 40, y + S // 2 - banner.height // 2), banner)
        center_text(d, x + S / 2, y + S + 40, p["name"][:16], font(46), (255, 255, 255))
        color = (255, 200, 40) if is_winner else (150, 160, 175)
        center_text(d, x + S / 2, y + S + 105, f"{p['score']} pts", font(40), color)

    center_text(d, W / 2, 250, "VS", font(90), (120, 255, 60))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=90)
    buf.seek(0)
    return buf


# ---------------------------------------------------------------- scoring
def age_points(uid):
    """Lower Telegram IDs = older accounts (rough estimate)."""
    if uid < 1_000_000_000:
        return 30, "old account"
    if uid < 3_000_000_000:
        return 22, "veteran account"
    if uid < 5_000_000_000:
        return 14, "mid-age account"
    if uid < 6_500_000_000:
        return 8, "newer account"
    return 3, "fresh account"


async def score_user(bot, uid, name, premium, has_username):
    lines = []
    total = 0

    # profile photos
    pfp = None
    count = 0
    try:
        photos = await bot.get_user_profile_photos(uid, limit=1)
        count = photos.total_count
        if count:
            f = await bot.get_file(photos.photos[0][-1].file_id)
            data = await f.download_as_bytearray()
            pfp = Image.open(io.BytesIO(bytes(data))).convert("RGB")
    except Exception as e:  # noqa: BLE001
        log.warning("pfp fetch failed for %s: %s", uid, e)

    pts = min(count, 10) * 5
    total += pts
    lines.append(f"📸 {count} profile pics: +{pts}")

    # bio / username from get_chat (only works if the user has talked to the bot)
    bio = None
    try:
        chat = await bot.get_chat(uid)
        bio = getattr(chat, "bio", None)
        if getattr(chat, "username", None):
            has_username = True
    except Exception:  # noqa: BLE001
        pass

    if has_username:
        total += 15
        lines.append("🔗 username: +15")
    if premium:
        total += 25
        lines.append("⭐ premium: +25")
    if bio:
        total += 10
        lines.append("📝 bio: +10")

    pts, label = age_points(uid)
    total += pts
    lines.append(f"🕰 {label}: +{pts}")

    aura = random.randint(0, RANDOM_SPICE)
    total += aura
    lines.append(f"✨ aura: +{aura}")

    return {
        "name": img_safe(name, "Player"),
        "raw_name": name,
        "score": total,
        "lines": lines,
        "pfp": pfp,
    }


# ---------------------------------------------------------------- handlers
def short_name(user):
    return (user.full_name or user.first_name or "Someone").replace("|", "")[:25]


def pack_callback(user):
    prefix = f"acc|{user.id}|{int(bool(user.is_premium))}|{int(bool(user.username))}|"
    room = 64 - len(prefix.encode())
    name = short_name(user).encode()[:room].decode(errors="ignore")
    return prefix + name


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "⚔️ Telemaxxing\n\nIn any chat, type my @username and pick "
        "'Telemaxxing' to challenge someone to a profile battle.\n"
        "Higher account score mogs the other."
    )


async def inline_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    iq = update.inline_query
    file_id = context.bot_data.get("banner_file_id")
    if not file_id:
        return
    u = iq.from_user
    kb = InlineKeyboardMarkup(
        [[InlineKeyboardButton("Accept ⚔️", callback_data=pack_callback(u))]]
    )
    result = InlineQueryResultCachedPhoto(
        id=f"battle-{u.id}",
        photo_file_id=file_id,
        title="Telemaxxing",
        description="Challenge someone to a profile battle",
        caption=(
            f"⚔️ <b>{html.escape(short_name(u))}</b> challenges you to a profile battle!\n"
            "Tap Accept to see who mogs who."
        ),
        parse_mode=ParseMode.HTML,
        reply_markup=kb,
    )
    await iq.answer([result], cache_time=0, is_personal=True)


async def accept(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    parts = q.data.split("|", 4)
    if len(parts) < 5:
        await q.answer("Bad challenge.", show_alert=True)
        return

    challenger_id = int(parts[1])
    challenger_premium = parts[2] == "1"
    challenger_username = parts[3] == "1"
    challenger_name = parts[4] or "Challenger"
    clicker = q.from_user

    if clicker.id == challenger_id:
        await q.answer("You can't accept your own challenge 😭", show_alert=True)
        return
    imid = q.inline_message_id
    if not imid:
        await q.answer("Use this from inline mode.", show_alert=True)
        return

    done = context.bot_data.setdefault("done", set())
    if imid in done:
        await q.answer("This battle already happened!")
        return
    if len(done) > 5000:
        done.clear()
    done.add(imid)
    await q.answer("⚔️ Battle started...")

    try:
        a = await score_user(
            context.bot, challenger_id, challenger_name,
            challenger_premium, challenger_username,
        )
        b = await score_user(
            context.bot, clicker.id, short_name(clicker),
            bool(clicker.is_premium), bool(clicker.username),
        )
        if a["score"] == b["score"]:
            a["score"] += random.choice([-1, 1])
        winner_is_a = a["score"] > b["score"]
        win, lose = (a, b) if winner_is_a else (b, a)

        image = make_battle_image(a, b, winner_is_a)
        sent = await context.bot.send_photo(STORAGE_CHAT_ID, image)
        file_id = sent.photo[-1].file_id

        def block(p):
            body = "\n".join(p["lines"])
            return f"<b>{html.escape(p['raw_name'])}</b> — {p['score']} pts\n{body}"

        caption = (
            f"🏆 <b>{html.escape(win['raw_name'])}</b> mogged "
            f"<b>{html.escape(lose['raw_name'])}</b>!\n\n"
            f"{block(a)}\n\n{block(b)}"
        )
        await context.bot.edit_message_media(
            inline_message_id=imid,
            media=InputMediaPhoto(file_id, caption=caption, parse_mode=ParseMode.HTML),
        )
    except Exception:  # noqa: BLE001
        log.exception("battle failed")
        done.discard(imid)


async def post_init(app: Application):
    try:
        sent = await app.bot.send_photo(
            STORAGE_CHAT_ID, make_challenge_banner(), caption="banner cache"
        )
        app.bot_data["banner_file_id"] = sent.photo[-1].file_id
        log.info("Challenge banner cached.")
    except Exception:  # noqa: BLE001
        log.exception(
            "Could not send to STORAGE_CHAT_ID. Press Start on the bot first and "
            "check the ID."
        )


def main():
    threading.Thread(target=run_web, daemon=True).start()
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(InlineQueryHandler(inline_query))
    app.add_handler(CallbackQueryHandler(accept, pattern=r"^acc\|"))
    app.run_polling(
        allowed_updates=["message", "inline_query", "callback_query"],
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()
