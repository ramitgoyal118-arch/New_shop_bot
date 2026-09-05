import sqlite3
import asyncio
import json
import urllib.request
import urllib.parse
import secrets
from decimal import Decimal, ROUND_DOWN
from datetime import datetime, timezone, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, MessageHandler,
    ContextTypes, filters
)

# =========================================================
# HARD-CODED CREDENTIALS
# Replace ONLY the text inside the quotes with your NEW token.
# OWNER_ID is your Telegram numeric ID.
# =========================================================
BOT_TOKEN = "8658247467:AAEx7LIuCDT1h3qDHaIDsjkmjStvhG8GE8k"
OWNER_ID = 7737039539

DB_FILE = "shop.db"
PRODUCT_ID = "gemini_premium"
PRODUCT_NAME = "Gemini Premium"
DEFAULT_PRICE = 100.0
PAYMENT_TIMEOUT_MINUTES = 10
WARRANTY_HOURS = 24
SUPPORT_USERNAME = "@your_support_username"

# =========================================================
# BSC / BEP-20 USDT PAYMENT SETTINGS
# =========================================================
PAYMENT_RECEIVING_WALLET = "0x43415B1E2843F6635A9E13d81032D202F95efb91"
USDT_CONTRACT = "0x55d398326f99059fF775485246999027B3197955"
BSCSCAN_API_KEY = "PASTE_BSCSCAN_API_KEY_HERE"
BSC_RPC_URL = "https://bsc-dataseed.binance.org"
USDT_DECIMALS = 6
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"



def now():
    return datetime.now(timezone.utc).isoformat()


def parse_dt(value):
    return datetime.fromisoformat(value)


def is_owner(user_id):
    return user_id == OWNER_ID


def db():
    con = sqlite3.connect(DB_FILE)
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
        id TEXT PRIMARY KEY,
        user_id INTEGER NOT NULL,
        username TEXT,
        product_id TEXT NOT NULL,
        qty INTEGER NOT NULL,
        price REAL NOT NULL,
        total REAL NOT NULL,
        status TEXT NOT NULL,
        created_at TEXT NOT NULL,
        paid_at TEXT,
        expires_at TEXT,
        payment_amount TEXT,
        payment_txid TEXT UNIQUE
    );

    CREATE TABLE IF NOT EXISTS order_links (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        order_id TEXT NOT NULL,
        link TEXT NOT NULL UNIQUE
    );

    CREATE TABLE IF NOT EXISTS warranty_claims (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        order_id TEXT NOT NULL,
        user_id INTEGER NOT NULL,
        link TEXT NOT NULL UNIQUE,
        proof TEXT,
        status TEXT NOT NULL DEFAULT 'pending',
        reason TEXT,
        replacement_link TEXT,
        created_at TEXT NOT NULL
    );
    """)
    con.execute(
        "INSERT OR IGNORE INTO settings(key,value) VALUES('bot_enabled','1')"
    )
    con.execute(
        "INSERT OR IGNORE INTO settings(key,value) VALUES('price','100')"
    )
    con.execute(
        "INSERT OR IGNORE INTO settings(key,value) VALUES('payment_info','Payment information not set yet.')"
    )
    con.commit()
    con.close()


def setting(key, default=""):
    con = db()
    row = con.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    con.close()
    return row["value"] if row else default


def set_setting(key, value):
    con = db()
    con.execute(
        "INSERT INTO settings(key,value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value)),
    )
    con.commit()
    con.close()


def is_admin(user_id):
    if is_owner(user_id):
        return True
    con = db()
    row = con.execute("SELECT 1 FROM admins WHERE user_id=?", (user_id,)).fetchone()
    con.close()
    return row is not None


def user_price(user_id):
    con = db()
    row = con.execute(
        "SELECT value FROM settings WHERE key=?",
        (f"userprice:{user_id}:{PRODUCT_ID}",),
    ).fetchone()
    con.close()
    return float(row["value"]) if row else float(setting("price", DEFAULT_PRICE))


def stock_count():
    con = db()
    row = con.execute(
        "SELECT COUNT(*) AS n FROM stock WHERE product_id=? AND sold=0",
        (PRODUCT_ID,),
    ).fetchone()
    con.close()
    return row["n"]


def unique_order_id():
    con = db()
    rows = con.execute("SELECT id FROM orders WHERE id LIKE 'ORD%'").fetchall()
    con.close()
    used = set()
    for row in rows:
        try:
            used.add(int(str(row["id"])[3:]))
        except (ValueError, TypeError):
            pass
    n = 1
    while n in used:
        n += 1
    return f"ORD{n:03d}"


def admin_ids():
    con = db()
    rows = con.execute("SELECT user_id FROM admins").fetchall()
    con.close()
    ids = {int(r["user_id"]) for r in rows}
    if OWNER_ID:
        ids.add(int(OWNER_ID))
    return ids


async def notify_admins(context, text, exclude_id=None):
    for admin_id in admin_ids():
        if exclude_id and admin_id == exclude_id:
            continue
        try:
            await context.bot.send_message(admin_id, text)
        except Exception:
            pass


def main_keyboard(user_id=None):
    rows = [
        [InlineKeyboardButton("🛒 Buy Gemini Premium", callback_data="buy")],
        [InlineKeyboardButton("💳 Payment Info", callback_data="payment")],
        [InlineKeyboardButton("🛡️ Warranty", callback_data="warranty_help")],
    ]
    if user_id is not None and is_admin(user_id):
        rows.append([InlineKeyboardButton("⚙️ Admin Panel", callback_data="admin_panel")])
    rows.append([InlineKeyboardButton("🔄 Refresh", callback_data="refresh")])
    return InlineKeyboardMarkup(rows)


def admin_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📦 Add Stock", callback_data="admin_stock")],
        [InlineKeyboardButton("💰 Payment Centre", callback_data="admin_payments")],
        [InlineKeyboardButton("💵 Set Price", callback_data="admin_price")],
        [InlineKeyboardButton("🟢 Bot ON", callback_data="admin_on"), InlineKeyboardButton("🔴 Bot OFF", callback_data="admin_off")],
        [InlineKeyboardButton("👥 Admin Commands", callback_data="admin_help")],
        [InlineKeyboardButton("🛡️ Warranty Commands", callback_data="admin_warranty")],
        [InlineKeyboardButton("🏠 Buyer Menu", callback_data="refresh")],
    ])


def product_text(user_id):
    price = user_price(user_id)
    return (
        f"✨ {PRODUCT_NAME}\n\n"
        f"Price: {price:g}\n"
        f"Stock: {stock_count()}\n\n"
        "Choose an option:"
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        product_text(update.effective_user.id), reply_markup=main_keyboard(update.effective_user.id)
    )


async def adminpanel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    await update.message.reply_text(
        "⚙️ ADMIN PANEL\n\nChoose an option:",
        reply_markup=admin_keyboard(),
    )


async def botstatus(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state = "ON 🟢" if setting("bot_enabled", "1") == "1" else "OFF 🔴"
    await update.message.reply_text(f"Bot status: {state}")


async def callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id

    if q.data == "buy":
        if setting("bot_enabled", "1") != "1" and not is_admin(uid):
            await q.message.reply_text("🔴 Bot is currently OFF.")
            return
        available = stock_count()
        if available <= 0:
            await q.message.reply_text("❌ Out of stock. Please try again later.")
            return
        await q.message.reply_text(
            f"Send quantity (minimum 2)\nAvailable: {available}"
        )
        context.user_data["waiting_qty"] = True

    elif q.data == "payment":
        await q.message.reply_text("💳 Payment Info:\n\n" + setting("payment_info"))

    elif q.data == "support":\n        support = setting("support_username", SUPPORT_USERNAME)\n        await q.message.reply_text(f"🆘 Support\\n\\nContact support: {support}")\n\n    elif q.data == "warranty_help":
        await q.message.reply_text(
            "🛡️ 24-hour warranty\n\n"
            "After delivery, send:\n"
            "/warranty ORDER_ID DELIVERED_LINK\n\n"
            "Then send your proof as a photo, document, or text."
        )

    elif q.data == "refresh":
        await q.message.reply_text(
            product_text(uid), reply_markup=main_keyboard(uid)
        )

    elif q.data.startswith("approve:"):
        if not is_admin(uid):
            return
        order_id = q.data.split(":", 1)[1]
        context.args = [order_id]
        await approve(update, context, from_callback=True)

    elif q.data.startswith("reject:"):
        if not is_admin(uid):
            return
        order_id = q.data.split(":", 1)[1]
        context.args = [order_id]
        await reject(update, context, from_callback=True)

    elif q.data.startswith("wapprove:"):
        if not is_admin(uid):
            return
        cid = q.data.split(":", 1)[1]
        con = db()
        claim = con.execute("SELECT * FROM warranty_claims WHERE id=?", (cid,)).fetchone()
        if not claim:
            con.close()
            await q.message.reply_text("❌ Claim not found.")
            return
        con.execute("UPDATE warranty_claims SET status='approved' WHERE id=?", (cid,))
        con.commit(); con.close()
        await q.edit_message_reply_markup(reply_markup=None)
        await context.bot.send_message(claim["user_id"], f"✅ Warranty claim #{cid} approved.")
        await q.message.reply_text(f"✅ Warranty claim #{cid} approved.")

    elif q.data.startswith("wreject:"):
        if not is_admin(uid):
            return
        cid = q.data.split(":", 1)[1]
        await q.message.reply_text(f"❌ Reject this claim with:\n/warranty_reject {cid} REASON")

    elif q.data.startswith("wresolve:"):
        if not is_admin(uid):
            return
        cid = q.data.split(":", 1)[1]
        await q.message.reply_text(f"🔁 Resolve this claim with replacement stock link:\n/warranty_resolve {cid} REPLACEMENT_LINK")

    elif q.data == "admin_panel":
        if is_admin(uid):
            await q.message.reply_text("⚙️ ADMIN PANEL\n\nChoose an option:", reply_markup=admin_keyboard())

    elif q.data == "admin_stock":
        if is_admin(uid):
            await q.message.reply_text("📦 Add stock\n\nUse /addlink followed by links. Paste 20–25 links at once, one per line.")

    elif q.data == "admin_payments":
        if is_admin(uid):
            await q.message.reply_text("💰 Payment Centre:\n/paymentcenter\n\nApprove: /approve ORDER_ID\nReject: /reject ORDER_ID")

    elif q.data == "admin_price":
        if is_owner(uid):
            await q.message.reply_text("💵 Set global price with:\n/setprice PRICE\n\nUser-specific:\n/setuserprice @USERNAME gemini_premium PRICE")
        else:
            await q.message.reply_text("❌ Owner only.")

    elif q.data == "admin_on":
        if is_owner(uid):
            set_setting("bot_enabled", "1")
            await q.message.reply_text("🟢 Bot is ON.")
            await notify_admins(context, "🟢 BOT STATUS CHANGED\nBot is now ON.", exclude_id=uid)

    elif q.data == "admin_off":
        if is_owner(uid):
            set_setting("bot_enabled", "0")
            await q.message.reply_text("🔴 Bot is OFF.")
            await notify_admins(context, "🔴 BOT STATUS CHANGED\nBot is now OFF.", exclude_id=uid)

    elif q.data == "admin_help":
        if is_admin(uid):
            await q.message.reply_text("👥 Admin management:\n/addadmin TELEGRAM_ID\n/removeadmin TELEGRAM_ID\n\nStock: /addlink LINK\nDuplicates: /checkduplicates")

    elif q.data == "admin_warranty":
        if is_admin(uid):
            await q.message.reply_text("🛡️ Warranty:\n/warranty_approve CLAIM_ID\n/warranty_reject CLAIM_ID REASON\n/warranty_resolve CLAIM_ID REPLACEMENT_LINK")
\n\nasync def setsupport(update: Update, context: ContextTypes.DEFAULT_TYPE):\n    if not is_owner(update.effective_user.id):\n        await update.message.reply_text("❌ Owner only.")\n        return\n    if not context.args:\n        await update.message.reply_text("Use: /setsupport @username")\n        return\n    username = context.args[0].strip()\n    if not username.startswith("@"):\n        username = "@" + username\n    set_setting("support_username", username)\n    await update.message.reply_text(f"✅ Support username set to {username}")\n\n

async def quantity_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("waiting_qty"):
        return
    context.user_data["waiting_qty"] = False

    text = (update.message.text or "").strip()
    try:
        qty = int(text)
        if qty < 2:
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Minimum quantity is 2.")
        return

    available = stock_count()
    if available <= 0:
        await update.message.reply_text("❌ Out of stock right now.")
        return
    if qty > available:
        await update.message.reply_text(f"❌ Only {available} item(s) available.")
        return

    price = Decimal(str(user_price(update.effective_user.id)))
    total = (price * qty).quantize(Decimal("0.000001"))

    # Give every active order a distinct 6-decimal USDT amount.
    con = db()
    used = {Decimal(r["payment_amount"]) for r in con.execute(
        "SELECT payment_amount FROM orders WHERE status IN ('pending','paid_pending') AND payment_amount IS NOT NULL"
    ).fetchall()}
    while True:
        unique_part = Decimal(secrets.randbelow(99999) + 1) / Decimal("1000000")
        payment_amount = (total + unique_part).quantize(Decimal("0.000001"))
        if payment_amount not in used:
            break

    order_id = unique_order_id()
    expires = datetime.now(timezone.utc) + timedelta(minutes=PAYMENT_TIMEOUT_MINUTES)
    con.execute(
        "INSERT INTO orders(id,user_id,username,product_id,qty,price,total,status,created_at,expires_at,payment_amount) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (order_id, update.effective_user.id, update.effective_user.username or "",
         PRODUCT_ID, qty, float(price), float(total), "pending", now(), expires.isoformat(),
         format(payment_amount, ".6f")),
    )
    con.commit()
    con.close()

    await update.message.reply_text(
        f"🧾 Order: {order_id}\n"
        f"Quantity: {qty}\n"
        f"Price each: {price:g} USDT\n"
        f"Amount to pay: {payment_amount:.6f} USDT\n\n"
        f"🌐 Network: BSC / BEP-20\n"
        f"📥 Send USDT to:\n{PAYMENT_RECEIVING_WALLET}\n\n"
        f"📸 After payment, use /paid {order_id} so an admin can verify it manually.\n"
        f"⏳ Payment expires in {PAYMENT_TIMEOUT_MINUTES} minutes.\n\n"
        f"⚠️ Send the exact amount shown above."
    )


async def paid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Use: /paid ORDER_ID")
        return
    order_id = context.args[0].strip()
    con = db()
    row = con.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
    if not row:
        con.close()
        await update.message.reply_text("❌ Order not found.")
        return
    if int(row["user_id"]) != int(update.effective_user.id):
        con.close()
        await update.message.reply_text("❌ This order does not belong to you.")
        return
    if row["status"] != "pending":
        con.close()
        await update.message.reply_text(f"❌ Order status: {row['status']}")
        return
    con.execute("UPDATE orders SET status='paid_pending' WHERE id=?", (order_id,))
    con.commit(); con.close()
    await update.message.reply_text("✅ Payment submitted for manual verification. Please wait for admin approval.")
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Approve Payment", callback_data=f"approve:{order_id}"),
        InlineKeyboardButton("❌ Reject", callback_data=f"reject:{order_id}")
    ]])
    await notify_admins(context,
        f"💳 PAYMENT AWAITING MANUAL APPROVAL\n\nOrder: {order_id}\nUser: @{row['username'] or 'N/A'} ({row['user_id']})\n"
        f"Quantity: {row['qty']}\nAmount: {row['payment_amount']} USDT\n\nVerify payment manually, then approve.",
    )
    for admin_id in admin_ids():
        try:
            await context.bot.send_message(admin_id, f"Order {order_id}: approve/reject payment:", reply_markup=keyboard)
        except Exception:
            pass


async def q_or_message_reply(update, text):
    if update.callback_query:
        await update.callback_query.edit_message_reply_markup(reply_markup=None)
        await update.callback_query.message.reply_text(text)
    else:
        await update.message.reply_text(text)


async def fulfill_paid_order(context, order_id, txid):
    con = db()
    row = con.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
    if not row or row["status"] != "paid_pending":
        con.close()
        return False
    links = con.execute(
        "SELECT id,link FROM stock WHERE product_id=? AND sold=0 ORDER BY id LIMIT ?",
        (PRODUCT_ID, row["qty"]),
    ).fetchall()
    if len(links) < row["qty"]:
        con.close()
        await context.bot.send_message(row["user_id"], "❌ Payment received, but stock is insufficient. Please contact support.")
        return False
    paid_at = now()
    for item in links:
        con.execute("UPDATE stock SET sold=1 WHERE id=?", (item["id"],))
        con.execute("INSERT INTO order_links(order_id,link) VALUES(?,?)", (order_id, item["link"]))
    con.execute("UPDATE orders SET status='fulfilled',payment_txid=?,paid_at=? WHERE id=?", (txid, paid_at, order_id))
    con.commit(); con.close()
    delivery = "\n".join(x["link"] for x in links)
    warranty_end = parse_dt(paid_at) + timedelta(hours=WARRANTY_HOURS)
    await context.bot.send_message(
        row["user_id"],
        f"✅ Payment verified automatically!\n\nOrder: {order_id}\n"
        f"Amount: {row['payment_amount']} USDT\n"
        f"Your {PRODUCT_NAME} link(s):\n{delivery}\n\n"
        f"🛡️ Warranty valid for {WARRANTY_HOURS} hours.\n"
        f"To claim, use:\n/warranty {order_id} EXACT_LINK"
    )
    await notify_admins(context, f"💰 AUTO PAYMENT VERIFIED\nOrder: {order_id}\nAmount: {row['payment_amount']} USDT\nTXID: {txid}")
    return True


async def approve(update: Update, context: ContextTypes.DEFAULT_TYPE, from_callback=False):
    if not is_admin(update.effective_user.id):
        return
    if not context.args:
        await q_or_message_reply(update, "Use: /approve ORDER_ID")
        return
    order_id = context.args[0]
    ok = await fulfill_paid_order(context, order_id, "MANUAL_APPROVAL")
    if ok:
        await q_or_message_reply(update, "✅ Payment manually approved and link(s) delivered.")
    else:
        await q_or_message_reply(update, "❌ Could not approve this order. Make sure the customer has submitted /paid ORDER_ID and stock is available.")


async def reject(update: Update, context: ContextTypes.DEFAULT_TYPE, from_callback=False):
    if not is_admin(update.effective_user.id):
        return
    if not context.args:
        await update.message.reply_text("Use: /reject ORDER_ID")
        return
    order_id = context.args[0]
    con = db()
    row = con.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
    if not row:
        con.close()
        await update.message.reply_text("❌ Order not found.")
        return
    con.execute("UPDATE orders SET status='rejected' WHERE id=?", (order_id,))
    con.commit()
    con.close()
    await context.bot.send_message(row["user_id"], f"❌ Payment rejected for {order_id}.")
    if from_callback:
        await q_or_message_reply(update, "✅ Rejected.")
    else:
        await update.message.reply_text("✅ Rejected.")


async def addlink(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    raw = (update.message.text or "").split(" ", 1)
    if len(raw) < 2 or not raw[1].strip():
        await update.message.reply_text("Use /addlink followed by links. You can paste 20–25 links at once, one per line.")
        return
    links = [x.strip() for x in raw[1].splitlines() if x.strip()]
    con = db(); added = 0; duplicates = 0
    for link in links:
        try:
            con.execute("INSERT INTO stock(product_id,link,sold) VALUES(?,?,0)", (PRODUCT_ID, link))
            added += 1
        except sqlite3.IntegrityError:
            duplicates += 1
    con.commit(); con.close()
    await update.message.reply_text(f"✅ Added: {added} link(s)\n⚠️ Duplicates skipped: {duplicates}")


async def checkduplicates(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.effective_user.id):
        return
    con = db()
    rows = con.execute(
        "SELECT link,COUNT(*) c FROM stock GROUP BY link HAVING c>1"
    ).fetchall()
    con.close()
    await update.message.reply_text(
        "✅ No duplicate stock links found."
        if not rows else "⚠️ Duplicates:\n" + "\n".join(r["link"] for r in rows)
    )


async def setprice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.effective_user.id):
        return
    if not context.args:
        await update.message.reply_text("Use: /setprice PRICE")
        return
    try:
        price = float(context.args[0])
        if price < 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Invalid price.")
        return
    set_setting("price", price)
    await update.message.reply_text(f"✅ Global price set to {price:g}")


async def setuserprice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.effective_user.id):
        return
    if len(context.args) < 3:
        await update.message.reply_text("Use: /setuserprice USERNAME PRODUCT_ID PRICE")
        return

    username = context.args[0].lstrip("@")
    product = context.args[1]
    try:
        price = float(context.args[2])
        if price < 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Invalid price.")
        return

    if product != PRODUCT_ID:
        await update.message.reply_text(f"❌ Only product ID is {PRODUCT_ID}")
        return

    con = db()
    rows = con.execute(
        "SELECT DISTINCT user_id FROM orders WHERE LOWER(REPLACE(username,'@',''))=?",
        (username.lower(),),
    ).fetchall()
    con.close()

    if not rows:
        await update.message.reply_text(
            "❌ Username not found in previous orders. Use the user's Telegram numeric ID with /setuserprice_id if needed."
        )
        return

    for row in rows:
        set_setting(f"userprice:{row['user_id']}:{PRODUCT_ID}", price)

    await update.message.reply_text(
        f"✅ User price set to {price:g} for @{username}."
    )


async def setuserprice_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.effective_user.id):
        return
    if len(context.args) < 3:
        await update.message.reply_text("Use: /setuserprice_id TELEGRAM_ID PRODUCT_ID PRICE")
        return
    try:
        uid = int(context.args[0])
        price = float(context.args[2])
        if price < 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Invalid Telegram ID/price.")
        return
    if context.args[1] != PRODUCT_ID:
        await update.message.reply_text(f"❌ Only product ID is {PRODUCT_ID}")
        return
    set_setting(f"userprice:{uid}:{PRODUCT_ID}", price)
    await update.message.reply_text(f"✅ User price set to {price:g}")


async def setpaymentinfo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.effective_user.id):
        return
    if not context.args:
        await update.message.reply_text("Use: /setpaymentinfo YOUR_PAYMENT_DETAILS")
        return
    set_setting("payment_info", " ".join(context.args))
    await update.message.reply_text("✅ Payment info updated.")


async def paymentcenter(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.effective_user.id):
        return
    con = db()
    pending = con.execute(
        "SELECT COUNT(*) n FROM orders WHERE status IN ('pending','paid_pending')"
    ).fetchone()["n"]
    total = con.execute(
        "SELECT COALESCE(SUM(total),0) n FROM orders WHERE status='fulfilled'"
    ).fetchone()["n"]
    con.close()
    await update.message.reply_text(
        f"💳 PAYMENT CENTRE\n\n"
        f"Pending orders: {pending}\n"
        f"Approved sales total: {total:g}\n\n"
        f"Payment info:\n{setting('payment_info')}\n\nBSC wallet: {PAYMENT_RECEIVING_WALLET}\nUSDT contract: {USDT_CONTRACT}"
    )


async def notify_buyers(context, text):
    con = db()
    rows = con.execute("SELECT DISTINCT user_id FROM orders").fetchall()
    con.close()
    for row in rows:
        try:
            await context.bot.send_message(int(row["user_id"]), text)
        except Exception:
            pass


async def bot_on(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.effective_user.id):
        return
    set_setting("bot_enabled", "1")
    await update.message.reply_text("🟢 Bot is ON.")
    await notify_buyers(context, "🟢 Gemini Premium shop is now ON. You can place new orders.")
    await notify_admins(context, "🟢 BOT STATUS CHANGED\nBot is now ON.", exclude_id=update.effective_user.id)


async def bot_off(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.effective_user.id):
        return
    set_setting("bot_enabled", "0")
    await update.message.reply_text("🔴 Bot is OFF. Existing orders/admin controls still work.")
    await notify_buyers(context, "🔴 Gemini Premium shop is temporarily OFF. Existing orders are still being handled.")
    await notify_admins(context, "🔴 BOT STATUS CHANGED\nBot is now OFF.", exclude_id=update.effective_user.id)


async def addadmin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.effective_user.id):
        return
    if not context.args:
        await update.message.reply_text("Use: /addadmin TELEGRAM_ID")
        return
    try:
        uid = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ Invalid Telegram ID.")
        return
    con = db()
    con.execute("INSERT OR IGNORE INTO admins(user_id) VALUES(?)", (uid,))
    con.commit()
    con.close()
    await update.message.reply_text("✅ Admin added.")


async def removeadmin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.effective_user.id):
        return
    if not context.args:
        await update.message.reply_text("Use: /removeadmin TELEGRAM_ID")
        return
    try:
        uid = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ Invalid Telegram ID.")
        return
    con = db()
    con.execute("DELETE FROM admins WHERE user_id=?", (uid,))
    con.commit()
    con.close()
    await update.message.reply_text("✅ Admin removed.")


async def warranty(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2:
        await update.message.reply_text("Use: /warranty ORDER_ID DELIVERED_LINK")
        return

    order_id = context.args[0]
    link = " ".join(context.args[1:]).strip()
    uid = update.effective_user.id

    con = db()
    order = con.execute(
        "SELECT * FROM orders WHERE id=? AND user_id=?",
        (order_id, uid),
    ).fetchone()

    if not order or order["status"] != "fulfilled":
        con.close()
        await update.message.reply_text("❌ Valid fulfilled order not found.")
        return

    if not order["paid_at"] or datetime.now(timezone.utc) > parse_dt(order["paid_at"]) + timedelta(hours=WARRANTY_HOURS):
        con.close()
        await update.message.reply_text("❌ 24-hour warranty period has expired.")
        return

    valid = con.execute(
        "SELECT 1 FROM order_links WHERE order_id=? AND link=?",
        (order_id, link),
    ).fetchone()
    if not valid:
        con.close()
        await update.message.reply_text("❌ This is not the exact delivered link for this order.")
        return

    try:
        con.execute(
            "INSERT INTO warranty_claims(order_id,user_id,link,created_at) VALUES(?,?,?,?)",
            (order_id, uid, link, now()),
        )
        con.commit()
    except sqlite3.IntegrityError:
        con.close()
        await update.message.reply_text("❌ This warranty link has already been claimed.")
        return
    con.close()

    context.user_data["warranty_claim_id"] = con.lastrowid if False else None
    con = db()
    claim_id = con.execute(
        "SELECT id FROM warranty_claims WHERE order_id=? AND user_id=? AND link=?",
        (order_id, uid, link),
    ).fetchone()["id"]
    con.close()
    context.user_data["warranty_claim_id"] = claim_id

    await update.message.reply_text(
        f"🛡️ Warranty claim #{claim_id} created.\n"
        "Now send your proof as a photo, document, or text."
    )


async def warranty_proof(update: Update, context: ContextTypes.DEFAULT_TYPE):
    claim_id = context.user_data.get("warranty_claim_id")
    if not claim_id:
        return

    if update.message.photo:
        proof = f"photo:{update.message.photo[-1].file_id}"
    elif update.message.document:
        proof = f"document:{update.message.document.file_id}"
    else:
        proof = (update.message.text or "")[:4000]

    con = db()
    claim = con.execute(
        "SELECT * FROM warranty_claims WHERE id=?",
        (claim_id,),
    ).fetchone()
    if not claim:
        con.close()
        context.user_data.pop("warranty_claim_id", None)
        return

    con.execute(
        "UPDATE warranty_claims SET proof=? WHERE id=?",
        (proof, claim_id),
    )
    con.commit()
    con.close()
    context.user_data.pop("warranty_claim_id", None)

    await update.message.reply_text("✅ Proof submitted. Owner will review your warranty claim.")

    for admin_id in admin_ids():
        try:
            await context.bot.send_message(
                admin_id,
                f"🛡️ WARRANTY CLAIM #{claim_id}\n"
                f"Order: {claim['order_id']}\n"
                f"User ID: {claim['user_id']}\n"
                f"Link: {claim['link']}\n"
                f"Proof: {proof[:1500]}\n\nChoose an action:",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("✅ Approve", callback_data=f"wapprove:{claim_id}")],
                    [InlineKeyboardButton("❌ Reject", callback_data=f"wreject:{claim_id}")],
                    [InlineKeyboardButton("🔁 Resolve", callback_data=f"wresolve:{claim_id}")],
                ]),
            )
        except Exception:
            pass


async def warranty_approve(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id) or not context.args:
        return
    cid = int(context.args[0])
    con = db()
    claim = con.execute("SELECT * FROM warranty_claims WHERE id=?", (cid,)).fetchone()
    if not claim:
        con.close()
        await update.message.reply_text("❌ Claim not found.")
        return
    con.execute("UPDATE warranty_claims SET status='approved' WHERE id=?", (cid,))
    con.commit()
    con.close()
    await context.bot.send_message(claim["user_id"], f"✅ Warranty claim #{cid} approved.")
    await update.message.reply_text("✅ Warranty approved.")


async def warranty_reject(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id) or len(context.args) < 1:
        return
    cid = int(context.args[0])
    reason = " ".join(context.args[1:]) or "Rejected by owner."
    con = db()
    claim = con.execute("SELECT * FROM warranty_claims WHERE id=?", (cid,)).fetchone()
    if not claim:
        con.close()
        await update.message.reply_text("❌ Claim not found.")
        return
    con.execute(
        "UPDATE warranty_claims SET status='rejected',reason=? WHERE id=?",
        (reason, cid),
    )
    con.commit()
    con.close()
    await context.bot.send_message(claim["user_id"], f"❌ Warranty claim #{cid} rejected.\nReason: {reason}")
    await update.message.reply_text("✅ Warranty rejected.")


async def warranty_resolve(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id) or len(context.args) < 2:
        await update.message.reply_text("Use: /warranty_resolve CLAIM_ID REPLACEMENT_LINK")
        return
    cid = int(context.args[0])
    replacement = " ".join(context.args[1:])
    con = db()
    claim = con.execute("SELECT * FROM warranty_claims WHERE id=?", (cid,)).fetchone()
    if not claim:
        con.close()
        await update.message.reply_text("❌ Claim not found.")
        return

    stock = con.execute(
        "SELECT id FROM stock WHERE link=? AND sold=0",
        (replacement,),
    ).fetchone()
    if not stock:
        con.close()
        await update.message.reply_text("❌ Replacement link is not in unsold stock.")
        return

    con.execute("UPDATE stock SET sold=1 WHERE id=?", (stock["id"],))
    con.execute(
        "UPDATE warranty_claims SET status='resolved',replacement_link=? WHERE id=?",
        (replacement, cid),
    )
    con.commit()
    con.close()

    await context.bot.send_message(
        claim["user_id"],
        f"✅ Warranty claim #{cid} resolved.\nReplacement link:\n{replacement}"
    )
    await update.message.reply_text("✅ Warranty resolved and replacement delivered.")


async def expiry_job(context: ContextTypes.DEFAULT_TYPE):
    con = db()
    rows = con.execute(
        "SELECT id,user_id FROM orders WHERE status IN ('pending','paid_pending') AND expires_at IS NOT NULL"
    ).fetchall()
    expired = []
    for row in rows:
        if datetime.now(timezone.utc) >= parse_dt(row["expires_at"]):
            con.execute("UPDATE orders SET status='expired' WHERE id=?", (row["id"],))
            expired.append((row["id"], row["user_id"]))
    con.commit()
    con.close()
    for order_id, user_id in expired:
        try:
            await context.bot.send_message(user_id, f"⏳ Order {order_id} expired due to payment timeout.")
        except Exception:
            pass


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Buyer commands:\n"
        "/start\n/warranty ORDER_ID DELIVERED_LINK\n/botstatus\n\n"
        "Owner/Admin commands are available to authorized accounts."
    )




def build_app():
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))\n    app.add_handler(CommandHandler("setsupport", setsupport))
    app.add_handler(CommandHandler("warranty", warranty))
    app.add_handler(CommandHandler("paid", paid))
    app.add_handler(CommandHandler("botstatus", botstatus))
    app.add_handler(CommandHandler("adminpanel", adminpanel))

    app.add_handler(CommandHandler("approve", approve))
    app.add_handler(CommandHandler("reject", reject))
    app.add_handler(CommandHandler("addlink", addlink))
    app.add_handler(CommandHandler("checkduplicates", checkduplicates))
    app.add_handler(CommandHandler("setprice", setprice))
    app.add_handler(CommandHandler("setuserprice", setuserprice))
    app.add_handler(CommandHandler("setuserprice_id", setuserprice_id))
    app.add_handler(CommandHandler("setpaymentinfo", setpaymentinfo))
    app.add_handler(CommandHandler("paymentcenter", paymentcenter))
    app.add_handler(CommandHandler("bot_on", bot_on))
    app.add_handler(CommandHandler("bot_off", bot_off))
    app.add_handler(CommandHandler("addadmin", addadmin))
    app.add_handler(CommandHandler("removeadmin", removeadmin))
    app.add_handler(CommandHandler("warranty_approve", warranty_approve))
    app.add_handler(CommandHandler("warranty_reject", warranty_reject))
    app.add_handler(CommandHandler("warranty_resolve", warranty_resolve))

    app.add_handler(CallbackQueryHandler(callback))
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, quantity_message),
        group=0,
    )
    app.add_handler(
        MessageHandler(
            (filters.PHOTO | filters.Document.ALL | (filters.TEXT & ~filters.COMMAND)),
            warranty_proof,
        ),
        group=1,
    )

    app.job_queue.run_repeating(expiry_job, interval=20, first=10)
    return app


def main():
    if BOT_TOKEN == "PASTE_NEW_BOT_TOKEN_HERE" or not OWNER_ID:
        raise RuntimeError(
            "Edit BOT_TOKEN and OWNER_ID at the top of bot.py before starting."
        )
    app = build_app()
    app.run_polling()


if __name__ == "__main__":
    main()
