"""Telegram image contest bot: "winner stays" rounds in a group.

Admin sends photos to the bot privately -> they go into the image pool.
In the group the bot posts image A vs image B with buttons 1 / 2. After N minutes
the round closes: the image with more votes stays (ties: the holder stays), the loser
is out and the next image from the pool challenges. After each round the bot can also
post a "nudge" (your own HTML caption + a random image from the pool).
"""
import asyncio
import logging
import os
import random
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

from sqlalchemy import (BigInteger, Column, Integer, MetaData, String, Table, Text,
                        UniqueConstraint, and_, create_engine, delete, func, insert,
                        select, update as sa_update)
from sqlalchemy.exc import IntegrityError
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler,
                          ContextTypes, MessageHandler, filters)

logging.basicConfig(level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)  # keeps the bot token out of the logs

TOKEN = os.environ["BOT_TOKEN"]
ADMINS = {int(x) for x in os.environ.get("ADMIN_IDS", "").split(",") if x.strip()}

db_url = os.environ.get("DATABASE_URL", "sqlite:///votes.db")
if db_url.startswith("postgres://"):
    db_url = db_url.replace("postgres://", "postgresql+psycopg2://", 1)
elif db_url.startswith("postgresql://"):
    db_url = db_url.replace("postgresql://", "postgresql+psycopg2://", 1)

engine = create_engine(db_url, pool_pre_ping=True)
meta = MetaData()
pool = Table(
    "pool", meta,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("file_id", String(255), nullable=False),
    Column("unique_id", String(255), nullable=False, unique=True),
    Column("status", String(10), nullable=False, default="queue"),  # queue | champ | out
    Column("wins", Integer, nullable=False, default=0),
)
rounds = Table(
    "rounds", meta,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("number", Integer, nullable=False),
    Column("left_id", Integer, nullable=False),
    Column("right_id", Integer, nullable=False),
    Column("ends_at", BigInteger, nullable=False),
    Column("message_id", Integer, nullable=True),
    Column("closed", Integer, nullable=False, default=0),
)
gvotes = Table(
    "gvotes", meta,
    Column("round_id", Integer, nullable=False),
    Column("user_id", BigInteger, nullable=False),
    Column("choice", Integer, nullable=False),
    UniqueConstraint("round_id", "user_id", name="one_vote_per_round"),
)
kv = Table(
    "kv", meta,
    Column("k", String(50), primary_key=True),
    Column("v", Text, nullable=True),
)
meta.create_all(engine)


# ---------- small helpers ----------
def kv_get(key, default=None):
    with engine.connect() as conn:
        row = conn.execute(select(kv.c.v).where(kv.c.k == key)).first()
    return row[0] if row and row[0] is not None else default


def kv_set(key, value):
    with engine.begin() as conn:
        if conn.execute(select(kv.c.k).where(kv.c.k == key)).first():
            conn.execute(sa_update(kv).where(kv.c.k == key).values(v=value))
        else:
            conn.execute(insert(kv).values(k=key, v=value))


def is_admin(update: Update) -> bool:
    u = update.effective_user
    return bool(u and u.id in ADMINS)


def plural(n, word):
    return f"{n} {word}{'' if n == 1 else 's'}"


def label(row, is_holder):
    s = f"#{row.id}"
    if is_holder:
        s += f" · holding, {plural(row.wins, 'win')}" if row.wins else " · holding"
    else:
        s += " · challenger"
    return s


def vote_kb(rid, c1=0, c2=0):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(f"1  ({c1})", callback_data=f"g:{rid}:1"),
        InlineKeyboardButton(f"2  ({c2})", callback_data=f"g:{rid}:2"),
    ]])


def counts(conn, rid):
    c = {1: 0, 2: 0}
    for ch, n in conn.execute(
            select(gvotes.c.choice, func.count()).where(gvotes.c.round_id == rid).group_by(gvotes.c.choice)):
        c[ch] = n
    return c[1], c[2]


# ---------- contest engine ----------
async def start_round(bot):
    chat = kv_get("group")
    if not chat:
        return
    minutes = int(kv_get("minutes", "10"))
    with engine.begin() as conn:
        champ = conn.execute(select(pool).where(pool.c.status == "champ")).first()
        queue = conn.execute(select(pool).where(pool.c.status == "queue").order_by(pool.c.id)).all()
        if champ is None and len(queue) >= 2:
            champ = queue.pop(0)
            conn.execute(sa_update(pool).where(pool.c.id == champ.id).values(status="champ"))
        if champ is None or not queue:
            champ = None
        else:
            chall = queue[0]
            left, right = (champ, chall) if random.random() < 0.5 else (chall, champ)
            number = conn.execute(select(func.count()).select_from(rounds)).scalar() + 1
            res = conn.execute(insert(rounds).values(
                number=number, left_id=left.id, right_id=right.id,
                ends_at=int(time.time()) + minutes * 60, closed=0))
            rid = res.inserted_primary_key[0]
    if champ is None:
        await finish_contest(bot)
        return

    chat = int(chat)
    await bot.send_media_group(chat, [
        InputMediaPhoto(left.file_id, caption=f"1 — {label(left, left.id == champ.id)}"),
        InputMediaPhoto(right.file_id, caption=f"2 — {label(right, right.id == champ.id)}"),
    ])
    m = await bot.send_message(
        chat, f"Round {number}: which one do you pick?\nVoting closes in {minutes} min. One vote each.",
        reply_markup=vote_kb(rid))
    with engine.begin() as conn:
        conn.execute(sa_update(rounds).where(rounds.c.id == rid).values(message_id=m.message_id))


async def send_nudge(bot, chat):
    text = kv_get("nudge")
    if not text:
        return
    with engine.connect() as conn:
        rows = conn.execute(select(pool).where(pool.c.status == "queue")).all()
        if not rows:
            rows = conn.execute(select(pool).where(pool.c.status == "out")).all()
    if not rows:
        return
    row = random.choice(rows)
    caption = text.replace("{id}", str(row.id))
    try:
        await bot.send_photo(chat, row.file_id, caption=caption, parse_mode=ParseMode.HTML)
    except BadRequest:
        logging.warning("Nudge HTML rejected by Telegram, sending as plain text")
        await bot.send_photo(chat, row.file_id, caption=caption)


async def close_round(bot, rid):
    chat = int(kv_get("group"))
    with engine.begin() as conn:
        r = conn.execute(select(rounds).where(rounds.c.id == rid)).first()
        if r is None or r.closed:
            return
        left = conn.execute(select(pool).where(pool.c.id == r.left_id)).first()
        right = conn.execute(select(pool).where(pool.c.id == r.right_id)).first()
        c1, c2 = counts(conn, rid)
        holder_left = left.status == "champ"
        holder, chall = (left, right) if holder_left else (right, left)
        hv, cv = (c1, c2) if holder_left else (c2, c1)
        chall_wins = cv > hv
        winner, loser = (chall, holder) if chall_wins else (holder, chall)
        wv, lv = (cv, hv) if chall_wins else (hv, cv)
        conn.execute(sa_update(pool).where(pool.c.id == loser.id).values(status="out"))
        conn.execute(sa_update(pool).where(pool.c.id == winner.id).values(
            status="champ", wins=pool.c.wins + 1))
        conn.execute(sa_update(rounds).where(rounds.c.id == rid).values(closed=1))
        wins_now = winner.wins + 1
        has_next = conn.execute(
            select(func.count()).select_from(pool).where(pool.c.status == "queue")).scalar() > 0

    if r.message_id:
        try:
            await bot.edit_message_text(
                f"Round {r.number} closed — 1: {c1} vote(s), 2: {c2} vote(s)",
                chat_id=chat, message_id=r.message_id)
        except Exception:
            pass

    note = "tie, the holder stays" if cv == hv else f"{wv}–{lv}"
    await bot.send_photo(
        chat, winner.file_id,
        caption=f"Round {r.number}: #{winner.id} stays ({note}). {plural(wins_now, 'win')} so far.")
    if has_next:
        try:
            await send_nudge(bot, chat)
        except Exception:
            logging.exception("nudge failed")
        await start_round(bot)
    else:
        await finish_contest(bot)


async def finish_contest(bot):
    chat = int(kv_get("group"))
    kv_set("active", "0")
    with engine.connect() as conn:
        champ = conn.execute(select(pool).where(pool.c.status == "champ")).first()
        top = conn.execute(select(pool).order_by(pool.c.wins.desc(), pool.c.id).limit(5)).all()
    if champ is None:
        return
    board = "\n".join(f"#{p.id}: {plural(p.wins, 'win')}" for p in top)
    await bot.send_photo(
        chat, champ.file_id,
        caption=f"Contest finished! Final winner: #{champ.id} with {plural(champ.wins, 'win')}.\n\nTop images:\n{board}")


async def ticker(app: Application):
    while True:
        await asyncio.sleep(10)
        try:
            if kv_get("active") != "1":
                continue
            with engine.connect() as conn:
                r = conn.execute(select(rounds).where(rounds.c.closed == 0)
                                 .order_by(rounds.c.id.desc()).limit(1)).first()
            if r is None:
                await start_round(app.bot)
            elif r.ends_at <= time.time():
                await close_round(app.bot, r.id)
        except Exception:
            logging.exception("ticker error")


async def post_init(app: Application):
    app.bot_data["ticker"] = asyncio.create_task(ticker(app))


# ---------- voting ----------
async def on_vote(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    _, rid, ch = q.data.split(":")
    rid, ch, uid = int(rid), int(ch), q.from_user.id
    with engine.connect() as conn:
        r = conn.execute(select(rounds).where(rounds.c.id == rid)).first()
    if r is None or r.closed or r.ends_at <= time.time():
        await q.answer("This round is closed.", show_alert=True)
        return
    try:
        with engine.begin() as conn:
            conn.execute(insert(gvotes).values(round_id=rid, user_id=uid, choice=ch))
        await q.answer(f"Vote for {ch} saved")
    except IntegrityError:
        await q.answer("You already voted this round.")
        return
    with engine.connect() as conn:
        c1, c2 = counts(conn, rid)
    try:
        await q.edit_message_reply_markup(reply_markup=vote_kb(rid, c1, c2))
    except BadRequest:
        pass


# ---------- admin: adding images ----------
ALBUMS = {}  # media_group_id -> list of new image ids added (for one summary reply)


def add_image(file_id, unique_id):
    """Returns pool id, or None if the image is already in the pool."""
    try:
        with engine.begin() as conn:
            res = conn.execute(insert(pool).values(
                file_id=file_id, unique_id=unique_id, status="queue", wins=0))
            return res.inserted_primary_key[0]
    except IntegrityError:
        return None


def pool_size():
    with engine.connect() as conn:
        return conn.execute(select(func.count()).select_from(pool)).scalar()


async def on_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    msg = update.message
    ph = msg.photo[-1]
    new_id = add_image(ph.file_id, ph.file_unique_id)
    gid = msg.media_group_id
    if gid:
        first = gid not in ALBUMS
        ALBUMS.setdefault(gid, []).append(new_id)
        if not first:
            return
        await asyncio.sleep(2)
        ids = ALBUMS.pop(gid, [])
        added = [i for i in ids if i]
        dup = len(ids) - len(added)
        await msg.reply_text(f"Added {len(added)} image(s) to the pool"
                             + (f" ({dup} duplicate skipped)" if dup else "")
                             + f". Pool now has {pool_size()}.")
        return
    if new_id:
        await msg.reply_text(f"Added as #{new_id}. Pool now has {pool_size()}.")
    else:
        await msg.reply_text("That image is already in the pool.")


# ---------- admin commands ----------
HELP = (
    "Admin commands\n"
    "Send photos here (albums ok) = add to the image pool.\n\n"
    "In the group:\n"
    "/setgroup - tell the bot this is the contest group\n"
    "/startcontest [minutes] - start (default 10 min per round)\n"
    "/stopcontest - pause\n"
    "/nudge <html text> - caption for the extra image posted after each round ({id} = image number)\n"
    "/nudge off - no extra posts\n"
    "/nudgetest - post one nudge now\n"
    "/leaderboard, /status\n"
    "/clearpool confirm - wipe everything"
)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_admin(update):
        await update.message.reply_text(HELP)
    else:
        await update.message.reply_text("Voting happens in the group. Join and tap 1 or 2 on each round!")


async def setgroup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    chat = update.effective_chat
    if chat.type == "private":
        await update.message.reply_text("Send /setgroup inside the group.")
        return
    kv_set("group", str(chat.id))
    await update.message.reply_text("Done. This group is the contest group.")


async def startcontest(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    if not kv_get("group"):
        await update.message.reply_text("First send /setgroup inside the group.")
        return
    if context.args and context.args[0].isdigit() and int(context.args[0]) > 0:
        kv_set("minutes", context.args[0])
    with engine.connect() as conn:
        playable = conn.execute(select(func.count()).select_from(pool)
                                .where(pool.c.status.in_(["queue", "champ"]))).scalar()
    if playable < 2:
        await update.message.reply_text("Need at least 2 images in the pool.")
        return
    if kv_get("active") == "1":
        await update.message.reply_text("Contest is already running.")
        return
    kv_set("active", "1")
    await update.message.reply_text(
        f"Contest started. {kv_get('minutes', '10')} min per round. First round within 10 seconds.")


async def stopcontest(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    kv_set("active", "0")
    await update.message.reply_text("Paused. /startcontest resumes (an open round continues).")


async def nudge_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    parts = update.message.text.split(None, 1)
    if len(parts) < 2:
        await update.message.reply_text(
            f"Current nudge: {kv_get('nudge') or '(none)'}\n\nUsage: /nudge <html text>  or  /nudge off")
        return
    if parts[1].strip().lower() == "off":
        kv_set("nudge", None)
        await update.message.reply_text("Nudge off.")
        return
    kv_set("nudge", parts[1])
    await update.message.reply_text("Nudge saved. /nudgetest posts one in the group.")


async def nudgetest(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update) or not kv_get("group"):
        return
    await send_nudge(context.bot, int(kv_get("group")))


async def leaderboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with engine.connect() as conn:
        top = conn.execute(select(pool).where(pool.c.wins > 0)
                           .order_by(pool.c.wins.desc(), pool.c.id).limit(10)).all()
    if not top:
        await update.message.reply_text("No rounds finished yet.")
        return
    await update.message.reply_text("Leaderboard\n" + "\n".join(
        f"#{p.id}: {plural(p.wins, 'win')}" for p in top))


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    with engine.connect() as conn:
        n = {s: conn.execute(select(func.count()).select_from(pool).where(pool.c.status == s)).scalar()
             for s in ("queue", "champ", "out")}
    await update.message.reply_text(
        f"Active: {kv_get('active') == '1'}\nGroup set: {bool(kv_get('group'))}\n"
        f"Minutes/round: {kv_get('minutes', '10')}\nWaiting: {n['queue']}, holder: {n['champ']}, out: {n['out']}\n"
        f"Nudge: {'on' if kv_get('nudge') else 'off'}")


async def clearpool(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    if context.args != ["confirm"]:
        await update.message.reply_text("This wipes the pool, rounds and votes. Send: /clearpool confirm")
        return
    with engine.begin() as conn:
        for t in (gvotes, rounds, pool):
            conn.execute(delete(t))
    kv_set("active", "0")
    await update.message.reply_text("Wiped.")


# ---------- keep-alive HTTP (for free hosts that need an open port) ----------
class Ping(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *a):
        pass


def serve():
    port = int(os.environ.get("PORT", "10000"))
    HTTPServer(("0.0.0.0", port), Ping).serve_forever()


def main():
    threading.Thread(target=serve, daemon=True).start()
    app = (Application.builder().token(TOKEN).concurrent_updates(True)
           .post_init(post_init).build())
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", start))
    app.add_handler(CommandHandler("setgroup", setgroup))
    app.add_handler(CommandHandler("startcontest", startcontest))
    app.add_handler(CommandHandler("stopcontest", stopcontest))
    app.add_handler(CommandHandler("nudge", nudge_cmd))
    app.add_handler(CommandHandler("nudgetest", nudgetest))
    app.add_handler(CommandHandler("leaderboard", leaderboard))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("clearpool", clearpool))
    app.add_handler(CallbackQueryHandler(on_vote, pattern=r"^g:"))
    app.add_handler(MessageHandler(filters.PHOTO & filters.ChatType.PRIVATE, on_photo))
    app.run_polling()


if __name__ == "__main__":
    main()
