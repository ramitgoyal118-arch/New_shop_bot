import os
import io
import re
import sqlite3
import asyncio
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from datetime import datetime, timezone, timedelta

import aiohttp
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputFile
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, filters
)

# =========================
# CONFIG
# =========================
BOT_TOKEN = os.getenv("BOT_TOKEN", "PASTE_NEW_BOT_TOKEN_HERE")
OWNER_ID = int(os.getenv("OWNER_ID", "7737039539"))

PRODUCT_ID = "gemini_premium"
PRODUCT_NAME = "Gemini Premium"

DEFAULT_PRICE = Decimal(os.getenv("DEFAULT_PRICE", "0.6"))
MIN_QTY = 2
PAYMENT_TIMEOUT_MINUTES = 10
AMOUNT_TOLERANCE = Decimal("0.01")

PAYMENT_WALLET = os.getenv(
    "PAYMENT_WALLET",
    "0x43415B1E2843F6635A9E13d81032D202F95efb91"
)
USDT_CONTRACT = os.getenv(
    "USDT_CONTRACT",
    "0x55d398326f990f59fF775485246999027B3197955"
)
BSC_RPC_URL = os.getenv("BSC_RPC_URL", "https://bsc-dataseed.binance.org")
USDT_DECIMALS = 18

DB_FILE = os.getenv("DB_FILE", "shop.db")

TRANSFER_TOPIC = (
    "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a"
    "df523b3ef"
)

# =========================
# DB
# =========================
def db():
    con = sqlite3.connect(DB_FILE, timeout=30)
    con.row_factory = sqlite3.Row
    return con

def init_db():
    con = db()
    con.executescript("""
    CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS admins (
        user_id INTEGER PRIMARY KEY
    );

    CREATE TABLE IF NOT EXISTS stock (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        product_id TEXT NOT NULL,
        link TEXT NOT NULL UNIQUE,
        sold INTEGER NOT NULL DEFAULT 0
    );

    CREATE TABLE IF NOT EXISTS orders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        order_code TEXT UNIQUE,
        user_id INTEGER NOT NULL,
        product_id TEXT NOT NULL,
        qty INTEGER NOT NULL,
        amount TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending',
        payment_txid TEXT UNIQUE,
        created_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        paid_at TEXT
    );

    CREATE TABLE IF NOT EXISTS order_links (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        order_id INTEGER NOT NULL,
        stock_id INTEGER NOT NULL,
        link TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS warranty_claims (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        order_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        link TEXT,
        reason TEXT,
        status TEXT NOT NULL DEFAULT 'pending',
        replacement_link TEXT,
        created_at TEXT NOT NULL
    );
    """)
    con.execute(
        "INSERT OR IGNORE INTO settings(key,value) VALUES('price',?)",
        (str(DEFAULT_PRICE),)
    )
    con.execute(
        "INSERT OR IGNORE INTO settings(key,value) VALUES('payment_info',?)",
        ("BSC / BEP-20 USDT\nSend USDT to the wallet shown below.",)
    )
    con.commit()
    con.close()

def get_setting(key, default=""):
    con = db()
    row = con.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    con.close()
    return row["value"] if row else default

def set_setting(key, value):
    con = db()
    con.execute(
        "INSERT INTO settings(key,value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value)
    )
    con.commit()
    con.close()

def price():
    try:
        return Decimal(get_setting("price", str(DEFAULT_PRICE)))
    except InvalidOperation:
        return DEFAULT_PRICE

def stock_count():
    con = db()
    n = con.execute(
        "SELECT COUNT(*) AS n FROM stock WHERE product_id=? AND sold=0",
        (PRODUCT_ID,)
    ).fetchone()["n"]
    con.close()
    return int(n)

def is_owner(uid):
    return uid == OWNER_ID

def is_admin(uid):
    if uid == OWNER_ID:
        return True
    con = db()
    row = con.execute("SELECT 1 FROM admins WHERE user_id=?", (uid,)).fetchone()
    con.close()
    return bool(row)

def now_utc():
    return datetime.now(timezone.utc)

def iso(dt):
    return dt.astimezone(timezone.utc).isoformat()

def parse_iso(s):
    return datetime.fromisoformat(s)

def fmt_amount(d):
    d = Decimal(d).normalize()
    s = format(d, "f")
    return s.rstrip("0").rstrip(".") if "." in s else s

def order_code(order_id):
    return f"ORD{order_id:03d}"

# =========================
# UI
# =========================
def main_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🛒 Buy Gemini Premium", callback_data="buy")],
        [InlineKeyboardButton("💳 Payment Info", callback_data="payment_info")],
        [InlineKeyboardButton("🛡️ Warranty", callback_data="warranty")],
        [InlineKeyboardButton("🔄 Refresh", callback_data="refresh")],
    ])

def cancel_keyboard(order_id):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("❌ Cancel Order", callback_data=f"cancel:{order_id}")]
    ])

def home_text():
    return (
        f"✨ {PRODUCT_NAME}\n\n"
        f"Price: {fmt_amount(price())} USDT\n"
        f"Stock: {stock_count()}\n\n"
        "Choose an option:"
    )

# =========================
# START / INFO
# =========================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("awaiting_qty", None)
    await update.message.reply_text(home_text(), reply_markup=main_keyboard())

async def payment_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "💳 Payment Info\n\n"
        f"🌐 Network: BSC / BEP-20\n"
        f"📥 USDT wallet:\n`{PAYMENT_WALLET}`\n\n"
        f"{get_setting('payment_info')}\n\n"
        "After payment use:\n"
        "`/paid ORDER_ID TX_HASH`"
    )
    await update.message.reply_text(text, parse_mode="Markdown")

async def warranty_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🛡️ Warranty\n\n"
        "If a delivered link does not work, contact the bot with your order ID "
        "and the affected link. Admin can replace the link from available stock."
    )

async def refresh(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(home_text(), reply_markup=main_keyboard())

# =========================
# BUY / QUANTITY
# =========================
async def buy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["awaiting_qty"] = True
    await update.message.reply_text(
        f"Send quantity (minimum {MIN_QTY})\nAvailable: {stock_count()}"
    )

async def create_order(update: Update, context: ContextTypes.DEFAULT_TYPE, qty: int):
    available = stock_count()
    if qty < MIN_QTY:
        await update.message.reply_text(f"❌ Minimum quantity is {MIN_QTY}.")
        return
    if qty > available:
        await update.message.reply_text(f"❌ Only {available} links are available.")
        return

    total = (price() * Decimal(qty)).quantize(
        Decimal("0.000000000000000001"), rounding=ROUND_DOWN
    )
    created = now_utc()
    expires = created + timedelta(minutes=PAYMENT_TIMEOUT_MINUTES)

    con = db()
    cur = con.execute(
        """INSERT INTO orders(order_code,user_id,product_id,qty,amount,status,created_at,expires_at)
           VALUES(NULL,?,?,?,?,?,?,?)""",
        (update.effective_user.id, PRODUCT_ID, qty, str(total), "pending",
         iso(created), iso(expires))
    )
    oid = cur.lastrowid
    code = order_code(oid)
    con.execute("UPDATE orders SET order_code=? WHERE id=?", (code, oid))
    con.commit()
    con.close()

    text = (
        f"🧾 Order: {code}\n"
        f"Quantity: {qty}\n"
        f"Price each: {fmt_amount(price())} USDT\n"
        f"Amount to pay: {fmt_amount(total)} USDT\n\n"
        f"🌐 Network: BSC / BEP-20\n"
        f"📥 Send USDT to:\n`{PAYMENT_WALLET}`\n\n"
        "🔐 After payment, submit the transaction hash:\n"
        f"`/paid {code} TX_HASH`\n\n"
        f"⏳ Time remaining: {PAYMENT_TIMEOUT_MINUTES:02d}:00\n"
        "You may cancel this order if it was created by mistake."
    )
    msg = await update.message.reply_text(
        text, parse_mode="Markdown", reply_markup=cancel_keyboard(oid)
    )
    context.user_data["active_order_id"] = oid
    asyncio.create_task(countdown_task(context.application, update.effective_chat.id, msg.message_id, oid))

async def quantity_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("awaiting_qty"):
        return
    text = (update.message.text or "").strip()
    if not text.isdigit():
        await update.message.reply_text("❌ Please send a whole-number quantity.")
        return
    context.user_data["awaiting_qty"] = False
    await create_order(update, context, int(text))

# =========================
# COUNTDOWN
# =========================
async def countdown_task(application, chat_id, message_id, order_id):
    while True:
        con = db()
        row = con.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
        con.close()
        if not row or row["status"] != "pending":
            return

        remaining = parse_iso(row["expires_at"]) - now_utc()
        seconds = int(remaining.total_seconds())
        if seconds <= 0:
            con = db()
            con.execute(
                "UPDATE orders SET status='expired' WHERE id=? AND status='pending'",
                (order_id,)
            )
            con.commit()
            con.close()
            try:
                await application.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=message_id,
                    text=f"⌛ Order {row['order_code']} expired.\nPlease create a new order.",
                )
            except Exception:
                pass
            return

        mm, ss = divmod(seconds, 60)
        base = (
            f"🧾 Order: {row['order_code']}\n"
            f"Quantity: {row['qty']}\n"
            f"Price each: {fmt_amount(Decimal(row['amount']) / Decimal(row['qty']))} USDT\n"
            f"Amount to pay: {fmt_amount(Decimal(row['amount']))} USDT\n\n"
            f"🌐 Network: BSC / BEP-20\n"
            f"📥 Send USDT to:\n`{PAYMENT_WALLET}`\n\n"
            "🔐 After payment, submit the transaction hash:\n"
            f"`/paid {row['order_code']} TX_HASH`\n\n"
            f"⏳ Time remaining: {mm:02d}:{ss:02d}\n"
            "You may cancel this order if it was created by mistake."
        )
        try:
            await application.bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=base,
                parse_mode="Markdown",
                reply_markup=cancel_keyboard(order_id),
            )
        except Exception:
            pass
        await asyncio.sleep(5)

# =========================
# CANCEL
# =========================
async def cancel_order(update: Update, context: ContextTypes.DEFAULT_TYPE, order_id=None):
    uid = update.effective_user.id
    if order_id is None:
        if not context.args:
            await update.message.reply_text("Usage: /cancel ORDER_ID")
            return
        order_id = context.args[0]

    con = db()
    row = con.execute(
        "SELECT * FROM orders WHERE (order_code=? OR id=?) AND user_id=?",
        (str(order_id), str(order_id), uid)
    ).fetchone()
    if not row:
        con.close()
        await update.message.reply_text("❌ Order not found.")
        return
    if row["status"] != "pending":
        con.close()
        await update.message.reply_text(f"❌ Order is already {row['status']}.")
        return
    if parse_iso(row["expires_at"]) <= now_utc():
        con.execute("UPDATE orders SET status='expired' WHERE id=?", (row["id"],))
        con.commit()
        con.close()
        await update.message.reply_text("⌛ This order has expired.")
        return

    con.execute("UPDATE orders SET status='cancelled' WHERE id=? AND status='pending'", (row["id"],))
    con.commit()
    con.close()
    await update.message.reply_text(f"✅ Order {row['order_code']} cancelled.")

# =========================
# BSC TX VERIFICATION
# =========================
def valid_tx_hash(tx):
    return bool(re.fullmatch(r"0x[a-fA-F0-9]{64}", tx.strip()))

def padded_address(addr):
    return "0x" + addr.lower().replace("0x", "").rjust(64, "0")

async def rpc(method, params):
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    timeout = aiohttp.ClientTimeout(total=20)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(BSC_RPC_URL, json=payload) as r:
            data = await r.json()
            if "error" in data:
                raise RuntimeError(data["error"].get("message", "RPC error"))
            return data.get("result")

async def verify_bsc_usdt(tx_hash, expected_amount):
    receipt = await rpc("eth_getTransactionReceipt", [tx_hash])
    if not receipt:
        return False, "Transaction is not mined yet."

    if receipt.get("status") != "0x1":
        return False, "Transaction failed on BSC."

    recipient_topic = padded_address(PAYMENT_WALLET)
    expected_raw = int(
        (Decimal(expected_amount) * (Decimal(10) ** USDT_DECIMALS))
        .to_integral_value(rounding=ROUND_DOWN)
    )
    tolerance_raw = int(
        (AMOUNT_TOLERANCE * (Decimal(10) ** USDT_DECIMALS))
        .to_integral_value(rounding=ROUND_DOWN)
    )

    for log in receipt.get("logs", []):
        if log.get("address", "").lower() != USDT_CONTRACT.lower():
            continue
        topics = log.get("topics", [])
        if len(topics) < 3 or topics[0].lower() != TRANSFER_TOPIC:
            continue
        if topics[2].lower() != recipient_topic.lower():
            continue
        try:
            raw = int(log.get("data", "0x0"), 16)
        except ValueError:
            continue
        if abs(raw - expected_raw) <= tolerance_raw:
            actual = Decimal(raw) / (Decimal(10) ** USDT_DECIMALS)
            return True, f"{fmt_amount(actual)} USDT transfer verified."
    return False, "No matching BSC USDT transfer to the shop wallet was found."

# =========================
# FULFILLMENT
# =========================
async def fulfill_order(update, order_id, tx_hash):
    uid = update.effective_user.id
    con = db()
    try:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute(
            "SELECT * FROM orders WHERE id=? AND user_id=?",
            (order_id, uid)
        ).fetchone()
        if not row:
            con.rollback()
            return "❌ Order not found."

        if row["status"] != "pending":
            con.rollback()
            return f"❌ Order is already {row['status']}."

        if parse_iso(row["expires_at"]) <= now_utc():
            con.execute("UPDATE orders SET status='expired' WHERE id=?", (order_id,))
            con.commit()
            return "⌛ This order has expired."

        used = con.execute(
            "SELECT order_code FROM orders WHERE payment_txid=?",
            (tx_hash,)
        ).fetchone()
        if used:
            con.rollback()
            return "❌ This TX hash has already been used."

        available = con.execute(
            "SELECT * FROM stock WHERE product_id=? AND sold=0 ORDER BY id LIMIT ?",
            (PRODUCT_ID, row["qty"])
        ).fetchall()
        if len(available) < row["qty"]:
            con.rollback()
            return "❌ Not enough stock available right now."

        paid_at = iso(now_utc())
        for s in available:
            con.execute("UPDATE stock SET sold=1 WHERE id=? AND sold=0", (s["id"],))
            con.execute(
                "INSERT INTO order_links(order_id,stock_id,link) VALUES(?,?,?)",
                (order_id, s["id"], s["link"])
            )

        con.execute(
            "UPDATE orders SET status='fulfilled', payment_txid=?, paid_at=? WHERE id=?",
            (tx_hash, paid_at, order_id)
        )
        con.commit()

        links = [s["link"] for s in available]
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()

    text = (
        f"✅ Payment verified!\n\n"
        f"Order: {row['order_code']}\n"
        f"Quantity: {row['qty']}\n\n"
        "🔗 Your links:\n" +
        "\n".join(f"{i+1}. {link}" for i, link in enumerate(links)) +
        "\n\n🛡️ Warranty support is available for this order."
    )
    await update.message.reply_text(text, disable_web_page_preview=True)
    return None

async def paid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) != 2:
        await update.message.reply_text("Usage:\n/paid ORDER_ID TX_HASH")
        return

    code, tx_hash = context.args[0].strip(), context.args[1].strip()
    if not valid_tx_hash(tx_hash):
        await update.message.reply_text("❌ Invalid TX hash format.")
        return

    con = db()
    row = con.execute(
        "SELECT * FROM orders WHERE order_code=? AND user_id=?",
        (code, update.effective_user.id)
    ).fetchone()
    con.close()

    if not row:
        await update.message.reply_text("❌ Order not found.")
        return
    if row["status"] != "pending":
        await update.message.reply_text(f"❌ Order is already {row['status']}.")
        return
    if parse_iso(row["expires_at"]) <= now_utc():
        con = db()
        con.execute("UPDATE orders SET status='expired' WHERE id=?", (row["id"],))
        con.commit()
        con.close()
        await update.message.reply_text("⌛ This order has expired.")
        return

    con = db()
    used = con.execute(
        "SELECT order_code FROM orders WHERE payment_txid=?",
        (tx_hash,)
    ).fetchone()
    con.close()
    if used:
        await update.message.reply_text("❌ This TX hash has already been used.")
        return

    await update.message.reply_text("🔎 Checking transaction on BSC... Please wait.")
    try:
        ok, reason = await verify_bsc_usdt(tx_hash, Decimal(row["amount"]))
    except Exception as e:
        await update.message.reply_text(
            "⚠️ BSC verification temporarily failed. Please try again in a moment."
        )
        return

    if not ok:
        await update.message.reply_text(
            "❌ Payment not verified.\n\n" + reason +
            "\n\nAllowed amount difference: up to 0.01 USDT."
        )
        return

    error = await fulfill_order(update, row["id"], tx_hash)
    if error:
        await update.message.reply_text(error)

# =========================
# OWNER / ADMIN
# =========================
async def addlink(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if not context.args:
        await update.message.reply_text("Usage: /addlink LINK1 LINK2 ...")
        return
    links = []
    for x in context.args:
        x = x.strip()
        if x and x not in links:
            links.append(x)

    con = db()
    added = 0
    for link in links:
        try:
            con.execute(
                "INSERT INTO stock(product_id,link,sold) VALUES(?,?,0)",
                (PRODUCT_ID, link)
            )
            added += 1
        except sqlite3.IntegrityError:
            pass
    con.commit()
    con.close()
    await update.message.reply_text(
        f"✅ Added: {added}\n⏭️ Duplicates skipped.\n📦 Stock: {stock_count()}"
    )

async def addlinks_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if not context.user_data.get("adding_links"):
        return
    context.user_data["adding_links"] = False
    links = [x.strip() for x in (update.message.text or "").splitlines() if x.strip()]
    con = db()
    added = 0
    for link in dict.fromkeys(links):
        try:
            con.execute(
                "INSERT INTO stock(product_id,link,sold) VALUES(?,?,0)",
                (PRODUCT_ID, link)
            )
            added += 1
        except sqlite3.IntegrityError:
            pass
    con.commit()
    con.close()
    await update.message.reply_text(f"✅ Added {added} links. Stock: {stock_count()}")

async def addlinks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    context.user_data["adding_links"] = True
    await update.message.reply_text(
        "📦 Send links now, one per line.\nYou can send 20–25 links together."
    )

async def stock(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    await update.message.reply_text(f"📦 Available stock: {stock_count()}")

async def backupstock(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    con = db()
    rows = con.execute(
        "SELECT link FROM stock WHERE product_id=? AND sold=0 ORDER BY id",
        (PRODUCT_ID,)
    ).fetchall()
    con.close()
    data = "\n".join(r["link"] for r in rows) or "NO UNSOLD STOCK"
    bio = io.BytesIO(data.encode("utf-8"))
    bio.name = "stock_backup.txt"
    await update.message.reply_document(
        document=InputFile(bio, filename="stock_backup.txt"),
        caption=f"📦 Unsold stock backup\nTotal links: {len(rows)}"
    )

async def setprice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if len(context.args) != 1:
        await update.message.reply_text("Usage: /setprice 0.6")
        return
    try:
        p = Decimal(context.args[0])
        if p <= 0:
            raise ValueError
    except Exception:
        await update.message.reply_text("❌ Invalid price.")
        return
    set_setting("price", str(p))
    await update.message.reply_text(f"✅ Price set to {fmt_amount(p)} USDT.")

async def setpaymentinfo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if not context.args:
        await update.message.reply_text("Usage: /setpaymentinfo YOUR_TEXT")
        return
    set_setting("payment_info", " ".join(context.args))
    await update.message.reply_text("✅ Payment info updated.")

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    con = db()
    counts = {}
    for s in ("pending", "fulfilled", "cancelled", "expired"):
        counts[s] = con.execute(
            "SELECT COUNT(*) AS n FROM orders WHERE status=?", (s,)
        ).fetchone()["n"]
    con.close()
    await update.message.reply_text(
        f"📊 Stats\n\n"
        f"Stock: {stock_count()}\n"
        f"Pending: {counts['pending']}\n"
        f"Fulfilled: {counts['fulfilled']}\n"
        f"Cancelled: {counts['cancelled']}\n"
        f"Expired: {counts['expired']}"
    )

async def addadmin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.effective_user.id):
        return
    if len(context.args) != 1 or not context.args[0].isdigit():
        await update.message.reply_text("Usage: /addadmin USER_ID")
        return
    uid = int(context.args[0])
    con = db()
    con.execute("INSERT OR IGNORE INTO admins(user_id) VALUES(?)", (uid,))
    con.commit()
    con.close()
    await update.message.reply_text(f"✅ Admin added: {uid}")

async def removeadmin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.effective_user.id):
        return
    if len(context.args) != 1 or not context.args[0].isdigit():
        await update.message.reply_text("Usage: /removeadmin USER_ID")
        return
    uid = int(context.args[0])
    con = db()
    con.execute("DELETE FROM admins WHERE user_id=?", (uid,))
    con.commit()
    con.close()
    await update.message.reply_text("✅ Admin removed.")

async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data

    if data == "buy":
        context.user_data["awaiting_qty"] = True
        await q.message.reply_text(
            f"Send quantity (minimum {MIN_QTY})\nAvailable: {stock_count()}"
        )
    elif data == "payment_info":
        await q.message.reply_text(
            f"💳 BSC / BEP-20 USDT\n\n"
            f"Send USDT to:\n`{PAYMENT_WALLET}`\n\n"
            f"{get_setting('payment_info')}\n\n"
            "`/paid ORDER_ID TX_HASH`",
            parse_mode="Markdown"
        )
    elif data == "warranty":
        await q.message.reply_text(
            "🛡️ Warranty support is available for delivered orders.\n"
            "Send your order ID and affected link to request help."
        )
    elif data == "refresh":
        await q.message.reply_text(home_text(), reply_markup=main_keyboard())
    elif data.startswith("cancel:"):
        oid = int(data.split(":", 1)[1])
        con = db()
        row = con.execute(
            "SELECT * FROM orders WHERE id=? AND user_id=?",
            (oid, q.from_user.id)
        ).fetchone()
        if not row:
            con.close()
            await q.message.reply_text("❌ Order not found.")
            return
        if row["status"] != "pending":
            con.close()
            await q.message.reply_text(f"❌ Order is already {row['status']}.")
            return
        if parse_iso(row["expires_at"]) <= now_utc():
            con.execute("UPDATE orders SET status='expired' WHERE id=?", (oid,))
            con.commit()
            con.close()
            await q.message.reply_text("⌛ Order expired.")
            return
        con.execute("UPDATE orders SET status='cancelled' WHERE id=? AND status='pending'", (oid,))
        con.commit()
        con.close()
        try:
            await q.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        await q.message.reply_text(f"✅ Order {row['order_code']} cancelled.")


async def warranty_claim(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2:
        await update.message.reply_text(
            "Usage:\n/warrantyclaim ORDER_ID LINK [reason]"
        )
        return
    code = context.args[0].strip()
    link = context.args[1].strip()
    reason = " ".join(context.args[2:]).strip() or "Link not working"

    con = db()
    order = con.execute(
        "SELECT * FROM orders WHERE order_code=? AND user_id=? AND status='fulfilled'",
        (code, update.effective_user.id)
    ).fetchone()
    if not order:
        con.close()
        await update.message.reply_text("❌ Fulfilled order not found.")
        return

    owned = con.execute(
        "SELECT 1 FROM order_links WHERE order_id=? AND link=?",
        (order["id"], link)
    ).fetchone()
    if not owned:
        con.close()
        await update.message.reply_text("❌ That link is not part of this order.")
        return

    cur = con.execute(
        """INSERT INTO warranty_claims
           (order_id,user_id,link,reason,status,created_at)
           VALUES(?,?,?,?,?,?)""",
        (order["id"], update.effective_user.id, link, reason,
         "pending", iso(now_utc()))
    )
    claim_id = cur.lastrowid
    con.commit()
    con.close()

    await update.message.reply_text(
        f"🛡️ Warranty claim #{claim_id} submitted.\n"
        "Admin will review it."
    )

async def warranty_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    con = db()
    rows = con.execute(
        """SELECT w.*, o.order_code FROM warranty_claims w
           JOIN orders o ON o.id=w.order_id
           WHERE w.status='pending' ORDER BY w.id DESC LIMIT 20"""
    ).fetchall()
    con.close()
    if not rows:
        await update.message.reply_text("✅ No pending warranty claims.")
        return
    lines = ["🛡️ Pending warranty claims:"]
    for r in rows:
        lines.append(
            f"#{r['id']} | {r['order_code']} | user {r['user_id']}\n"
            f"Link: {r['link']}\nReason: {r['reason']}"
        )
    await update.message.reply_text("\n\n".join(lines))

async def warranty_approve(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if len(context.args) != 1 or not context.args[0].isdigit():
        await update.message.reply_text("Usage: /warrantyapprove CLAIM_ID")
        return
    cid = int(context.args[0])
    con = db()
    con.execute("BEGIN IMMEDIATE")
    claim = con.execute(
        "SELECT * FROM warranty_claims WHERE id=? AND status='pending'", (cid,)
    ).fetchone()
    if not claim:
        con.rollback()
        con.close()
        await update.message.reply_text("❌ Pending claim not found.")
        return
    replacement = con.execute(
        "SELECT * FROM stock WHERE product_id=? AND sold=0 ORDER BY id LIMIT 1",
        (PRODUCT_ID,)
    ).fetchone()
    if not replacement:
        con.rollback()
        con.close()
        await update.message.reply_text("❌ No replacement stock available.")
        return
    con.execute("UPDATE stock SET sold=1 WHERE id=? AND sold=0", (replacement["id"],))
    con.execute(
        "UPDATE warranty_claims SET status='approved',replacement_link=? WHERE id=?",
        (replacement["link"], cid)
    )
    con.commit()
    con.close()
    await update.message.reply_text(
        f"✅ Claim #{cid} approved.\nReplacement: {replacement['link']}"
    )
    try:
        await context.bot.send_message(
            claim["user_id"],
            f"🛡️ Warranty approved for claim #{cid}.\n"
            f"Replacement link:\n{replacement['link']}"
        )
    except Exception:
        pass

async def pending_orders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    con = db()
    rows = con.execute(
        """SELECT order_code,user_id,qty,amount,created_at,expires_at
           FROM orders WHERE status='pending' ORDER BY id DESC LIMIT 20"""
    ).fetchall()
    con.close()
    if not rows:
        await update.message.reply_text("✅ No pending orders.")
        return
    lines = ["💳 Pending orders:"]
    for r in rows:
        lines.append(
            f"{r['order_code']} | user {r['user_id']} | qty {r['qty']} | "
            f"{fmt_amount(Decimal(r['amount']))} USDT"
        )
    await update.message.reply_text("\n".join(lines))

# =========================
# EXPIRY CLEANUP
# =========================
async def expiry_job(context: ContextTypes.DEFAULT_TYPE):
    con = db()
    rows = con.execute(
        "SELECT id FROM orders WHERE status='pending' AND expires_at<=?",
        (iso(now_utc()),)
    ).fetchall()
    for r in rows:
        con.execute(
            "UPDATE orders SET status='expired' WHERE id=? AND status='pending'",
            (r["id"],)
        )
    con.commit()
    con.close()

# =========================
# ERROR
# =========================
async def error_handler(update, context):
    print("BOT ERROR:", repr(context.error))

def main():
    if BOT_TOKEN == "PASTE_NEW_BOT_TOKEN_HERE":
        raise SystemExit("Set BOT_TOKEN environment variable before starting.")

    init_db()

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("buy", buy))
    app.add_handler(CommandHandler("paymentinfo", payment_info))
    app.add_handler(CommandHandler("warranty", warranty_info))
    app.add_handler(CommandHandler("refresh", refresh))
    app.add_handler(CommandHandler("paid", paid))
    app.add_handler(CommandHandler("cancel", cancel_order))
    app.add_handler(CommandHandler("warrantyclaim", warranty_claim))
    app.add_handler(CommandHandler("warrantylist", warranty_list))
    app.add_handler(CommandHandler("warrantyapprove", warranty_approve))

    app.add_handler(CommandHandler("addlink", addlink))
    app.add_handler(CommandHandler("addlinks", addlinks))
    app.add_handler(CommandHandler("stock", stock))
    app.add_handler(CommandHandler("backupstock", backupstock))
    app.add_handler(CommandHandler("setprice", setprice))
    app.add_handler(CommandHandler("setpaymentinfo", setpaymentinfo))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("pending", pending_orders))
    app.add_handler(CommandHandler("addadmin", addadmin))
    app.add_handler(CommandHandler("removeadmin", removeadmin))

    app.add_handler(CallbackQueryHandler(on_callback))

    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, quantity_handler),
        group=0
    )
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, addlinks_text),
        group=1
    )

    app.add_error_handler(error_handler)
    app.job_queue.run_repeating(expiry_job, interval=30, first=10)

    # IMPORTANT: only ONE polling process should run for this token.
    print("Gemini Shop Bot started.")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
