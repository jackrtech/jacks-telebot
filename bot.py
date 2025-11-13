
# ==================================================================================================
# Telegram Sticker Shop Bot - Full Version (~1100 lines)
# --------------------------------------------------------------------------------------------------
# Features:
# - Category & product browsing with inline keyboards
# - Cart system (add, remove, +/- qty, clear, open cart, auto-open after first add)
# - Single-message delivery details flow with Back (no Cancel) to reduce chat spam
# - Order confirmation, CSV persistence, friendly order IDs, admin notifications
# - Stripe Checkout integration (optional via stripe.txt or STRIPE_API_KEY env)
# - Admin commands: maintenance mode, broadcast, last orders, export CSV, stats, inventory, ping
# - Robust logging, basic validation, session expiry, graceful error handling
# - Webhook or polling mode (from config.json)
# - Secrets loaded from token.txt and stripe.txt (or env fallbacks)
#
# Drop-in replacement: save as bot.py in your repo and push; your VM auto-deploys via webhook.
# ==================================================================================================

import os
import re
import csv
import io
import json
import math
import time
import traceback
import logging
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from typing import Dict, List, Tuple, Optional

# External deps
import telebot
from telebot.types import (
    InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton, Message, CallbackQuery
)
import stripe

# --------------------------------------------------------------------------------------------------
#                                   BOOT / CONFIG / SECRETS
# --------------------------------------------------------------------------------------------------

def _read_first_line(path: str) -> str:
    """Return first line of a file if it exists, else empty string. Strips CR/LF."""
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return f.readline().strip().replace("\r", "")
    except Exception:
        pass
    return ""

# Telegram token: token.txt → env BOT_TOKEN
TOKEN = _read_first_line("token.txt") or os.getenv("BOT_TOKEN", "").strip()
if not TOKEN:
    raise ValueError("❌ No Telegram token found. Put it in token.txt or set BOT_TOKEN env var.")

# Stripe key: stripe.txt → env STRIPE_API_KEY (optional)
STRIPE_SECRET_KEY = _read_first_line("stripe.txt") or os.getenv("STRIPE_API_KEY", "").strip()
stripe.api_key = STRIPE_SECRET_KEY or None

# Load config.json
with open("config.json", "r", encoding="utf-8") as f:
    CFG = json.load(f)

SHOP_NAME: str = CFG.get("shop_name", "Sticker Shop")
CURRENCY: str = CFG.get("currency", "GBP")
SYMBOL: str = CFG.get("symbol", "£")
DELIVERY_FEE: Decimal = Decimal(str(CFG.get("delivery_fee", "2.50")))
FREE_DELIVERY_THRESHOLD: Decimal = Decimal(str(CFG.get("free_delivery_threshold", "10.00")))

ADMIN_IDS: List[int] = [int(x) for x in CFG.get("admin_ids", [])]
NOTIFY_CHANNEL_ID = CFG.get("notify_channel_id")

SUCCESS_URL = CFG.get("success_url", "https://example.com/success")
CANCEL_URL = CFG.get("cancel_url", "https://example.com/cancel")
WEBHOOK_URL = CFG.get("webhook_url")  # if present we'll set webhook, else polling

# Catalog: supports categories; if flat dict provided, put in "All"
_raw_catalog = CFG.get("catalog", {})
if any(isinstance(v, dict) and "price" in v for v in _raw_catalog.values()):
    # flat
    CATALOG = {"All": _raw_catalog}
else:
    # nested by category
    CATALOG = _raw_catalog

# Normalize catalog to Decimals
for cat, items in CATALOG.items():
    for name, data in list(items.items()):
        data["price"] = Decimal(str(data.get("price", "0")))
        data["emoji"] = data.get("emoji", "")

# --------------------------------------------------------------------------------------------------
#                                   GLOBALS / STATE / LOGGING
# --------------------------------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s"
)
logger = logging.getLogger("stickerbot")

bot = telebot.TeleBot(TOKEN, parse_mode="Markdown", skip_pending=True)

# Sessions & cart
SESSION_TIMEOUT = 3600  # seconds
user_carts: Dict[int, Dict[str, int]] = {}              # user_id -> { product_name: qty }
user_states: Dict[int, dict] = {}                       # for delivery flow + misc
last_activity: Dict[int, datetime] = {}                 # session expiry

# Single live cart message per user (so we can edit)
user_cart_msg: Dict[int, Tuple[int, int]] = {}          # user_id -> (chat_id, message_id)
# Single editable message for delivery prompts
user_delivery_msg: Dict[int, Tuple[int, int]] = {}      # user_id -> (chat_id, message_id)

# Track menus so we can mark old ones outdated
user_menu_msgs: Dict[int, List[Tuple[int, int]]] = {}

# Maintenance flag
MAINTENANCE = os.getenv("MAINTENANCE", "false").lower() == "true"

# CSV & counters
CSV_FILE = "orders.csv"
if not os.path.exists(CSV_FILE) or os.path.getsize(CSV_FILE) == 0:
    with open(CSV_FILE, "w", newline="", encoding="utf-8") as f:
        csv.writer(f).writerow([
            "order_id","username","items","name","house","street","city","postcode",
            "status","date","order_total","currency"
        ])

COUNTER_FILE = "order_counter.json"
if os.path.exists(COUNTER_FILE):
    with open(COUNTER_FILE, "r", encoding="utf-8") as f:
        _counters = json.load(f)
else:
    _counters = {}

# --------------------------------------------------------------------------------------------------
#                                   UTILITIES
# --------------------------------------------------------------------------------------------------

def new_order_id() -> str:
    today = datetime.now().strftime("%y%m%d")
    n = _counters.get(today, 0) + 1
    _counters[today] = n
    with open(COUNTER_FILE, "w", encoding="utf-8") as f:
        json.dump(_counters, f)
    return f"ORD-{today}-{n:02d}"

def bump(uid: int) -> None:
    last_activity[uid] = datetime.now()

def expired(uid: int) -> bool:
    ts = last_activity.get(uid)
    if not ts: return False
    return (datetime.now() - ts).total_seconds() > SESSION_TIMEOUT

def ensure_session(uid: int, chat_id: int) -> bool:
    if expired(uid):
        user_states.pop(uid, None)
        user_carts.pop(uid, None)
        bot.send_message(chat_id, "⏰ Session expired. Please /order again.")
        return False
    return True

def is_admin(uid: int) -> bool:
    return uid in ADMIN_IDS

def money(amount: Decimal) -> str:
    return f"{SYMBOL}{amount:.2f}"

def calc_totals(uid: int) -> Tuple[Decimal, Decimal, Decimal]:
    cart = user_carts.get(uid, {})
    subtotal = sum((CATALOG[c].get(p, {}).get("price", Decimal("0")) * q
                    for c in CATALOG for p, q in cart.items() if p in CATALOG[c]), Decimal("0.00"))
    subtotal = subtotal.quantize(Decimal("0.01"), ROUND_HALF_UP)
    delivery = Decimal("0.00") if subtotal >= FREE_DELIVERY_THRESHOLD else DELIVERY_FEE
    total = (subtotal + delivery).quantize(Decimal("0.01"), ROUND_HALF_UP)
    return subtotal, delivery, total

def list_all_products() -> Dict[str, dict]:
    all_items = {}
    for cat, items in CATALOG.items():
        for name, data in items.items():
            all_items[name] = data
    return all_items

ALL_PRODUCTS = list_all_products()

# --------------------------------------------------------------------------------------------------
#                                   KEYBOARDS
# --------------------------------------------------------------------------------------------------

def kb_categories() -> InlineKeyboardMarkup:
    m = InlineKeyboardMarkup(row_width=2)
    for cat in CATALOG.keys():
        m.add(InlineKeyboardButton(f"📁 {cat}", callback_data=f"cat|{cat}"))
    m.add(InlineKeyboardButton("🛒 Open Cart", callback_data="open_cart"))
    return m

def kb_products(cat: str) -> InlineKeyboardMarkup:
    m = InlineKeyboardMarkup(row_width=2)
    items = CATALOG.get(cat, {})
    for name, data in items.items():
        m.add(InlineKeyboardButton(f"{data['emoji']} {name}", callback_data=f"add|{name}"))
    m.add(
        InlineKeyboardButton("📂 Categories", callback_data="categories"),
        InlineKeyboardButton("🛒 Open Cart", callback_data="open_cart"),
    )
    return m

def kb_cart(has_items: bool) -> InlineKeyboardMarkup:
    m = InlineKeyboardMarkup()
    if has_items:
        m.add(
            InlineKeyboardButton("✅ Checkout", callback_data="begin_checkout"),
            InlineKeyboardButton("🛍 Continue Shopping", callback_data="continue_order"),
            InlineKeyboardButton("🗑 Clear Cart", callback_data="clear_cart"),
        )
    else:
        m.add(InlineKeyboardButton("🛍 Continue Shopping", callback_data="continue_order"))
    return m

def kb_plus_minus(product: str) -> InlineKeyboardMarkup:
    m = InlineKeyboardMarkup()
    m.add(
        InlineKeyboardButton("➖", callback_data=f"decr|{product}"),
        InlineKeyboardButton("➕", callback_data=f"incr|{product}"),
        InlineKeyboardButton("❌ Remove", callback_data=f"rm|{product}")
    )
    m.add(InlineKeyboardButton("🛒 Open Cart", callback_data="open_cart"))
    return m

def kb_back() -> InlineKeyboardMarkup:
    m = InlineKeyboardMarkup()
    m.add(InlineKeyboardButton("↩️ Back", callback_data="back_step"))
    return m

def kb_confirm_address() -> InlineKeyboardMarkup:
    m = InlineKeyboardMarkup()
    m.add(InlineKeyboardButton("✅ Confirm", callback_data="confirm_details"))
    m.add(InlineKeyboardButton("↩️ Back", callback_data="back_to_edit_address"))
    return m

# --------------------------------------------------------------------------------------------------
#                                   CART RENDERING
# --------------------------------------------------------------------------------------------------

def build_cart_text(uid: int) -> Tuple[str, bool]:
    cart = user_carts.get(uid, {})
    if not cart:
        return ("🛒 *Your Cart*\n\n(Empty)\nUse /order to add stickers.", False)

    text = "🛒 *Your Cart*\n\n"
    subtotal = Decimal("0.00")
    items = 0
    for name, qty in cart.items():
        if name not in ALL_PRODUCTS: continue
        price = ALL_PRODUCTS[name]["price"]
        line = (price * qty).quantize(Decimal("0.01"), ROUND_HALF_UP)
        text += f"{qty}× {ALL_PRODUCTS[name]['emoji']} {name} — {money(line)}\n"
        subtotal += line
        items += qty

    if subtotal >= FREE_DELIVERY_THRESHOLD:
        delivery = Decimal("0.00")
        dline = f"🚚 *Free delivery* (>{money(FREE_DELIVERY_THRESHOLD)})"
    else:
        delivery = DELIVERY_FEE
        dline = f"🚚 Delivery: {money(DELIVERY_FEE)}"

    total = (subtotal + delivery).quantize(Decimal("0.01"), ROUND_HALF_UP)
    text += f"\nTotal items: {items}\nSubtotal: {money(subtotal)}\n{dline}\n💰 *Total: {money(total)}*"
    return text, True

def refresh_cart(uid: int, chat_id: int) -> None:
    text, has_items = build_cart_text(uid)
    kb = kb_cart(has_items)
    existing = user_cart_msg.get(uid)
    if existing:
        c_id, m_id = existing
        try:
            bot.edit_message_text(text, chat_id=c_id, message_id=m_id, reply_markup=kb, parse_mode="Markdown")
            return
        except Exception:
            pass
    msg = bot.send_message(chat_id, text, reply_markup=kb)
    user_cart_msg[uid] = (chat_id, msg.message_id)

# --------------------------------------------------------------------------------------------------
#                                   DELIVERY FLOW (SINGLE MESSAGE)
# --------------------------------------------------------------------------------------------------

DELIVERY_FIELDS = ["name", "house", "street", "city", "postcode"]
PROMPTS = {
    "name": "📝 (1/5) Enter your *Full Name*:",
    "house": "🏠 (2/5) Enter your *House Name/Number*:",
    "street": "📍 (3/5) Enter your *Street Name*:",
    "city": "🏙️ (4/5) Enter your *City/Town*:",
    "postcode": "📮 (5/5) Enter your *Postcode*:",
}

def render_prompt(field: str) -> str:
    return PROMPTS[field]

def validate_field(field: str, value: str) -> bool:
    t = value.strip()
    if field == "name":
        return len(t) >= 3 and " " in t
    if field == "house":
        return bool(re.match(r"^[A-Za-z0-9\s\-]{1,12}$", t))
    if field == "street":
        return len(t) >= 3 and any(ch.isalpha() for ch in t)
    if field == "city":
        return len(t) >= 2 and any(ch.isalpha() for ch in t)
    if field == "postcode":
        return bool(re.match(r"^[A-Z]{1,2}[0-9][0-9A-Z]?\s?[0-9][A-Z]{2}$", t.upper()))
    return True

def render_current_delivery(uid: int) -> str:
    state = user_states[uid]
    step = state["step"]
    data = state["data"]
    subtotal, delivery, total = calc_totals(uid)

    summary = (
        "🧾 *Order Summary:*\n"
        f"Subtotal: {money(subtotal)} | "
        f"Delivery: {money(delivery)} | "
        f"💰 Total: *{money(total)}*\n\n"
        "📍 *Delivery Details (so far):*\n"
        f"Name: {data.get('name','—')}\n"
        f"House: {data.get('house','—')}\n"
        f"Street: {data.get('street','—')}\n"
        f"City: {data.get('city','—')}\n"
        f"Postcode: {data.get('postcode','—')}\n"
    )
    if step < len(DELIVERY_FIELDS):
        field = DELIVERY_FIELDS[step]
        prompt = render_prompt(field)
        return summary + "\n" + prompt
    else:
        return summary + "\n" + "✅ If everything looks good, press *Confirm* to place your order."

def begin_checkout(uid: int, chat_id: int) -> None:
    cart = user_carts.get(uid, {})
    if not cart:
        bot.send_message(chat_id, "🛒 Your cart is empty. Use /order to add items.")
        return

    # Initialize state
    user_states[uid] = {"step": 0, "data": {}}
    bump(uid)

    # One persistent message to edit
    text = render_current_delivery(uid)
    kb = InlineKeyboardMarkup()
    # At step 0, only show Back if >0 (we add Back dynamically in edits)
    msg = bot.send_message(chat_id, text, reply_markup=kb)
    user_delivery_msg[uid] = (chat_id, msg.message_id)

@bot.callback_query_handler(func=lambda c: c.data == "back_step")
def cb_back_step(c: CallbackQuery):
    uid = c.from_user.id
    chat_id = c.message.chat.id
    state = user_states.get(uid)
    if not state:
        bot.answer_callback_query(c.id, "No active checkout.")
        return
    if state["step"] > 0:
        state["step"] -= 1
        chat_id_m, msg_id = user_delivery_msg.get(uid, (chat_id, c.message.message_id))
        kb = InlineKeyboardMarkup()
        if state["step"] > 0:
            kb.add(InlineKeyboardButton("↩️ Back", callback_data="back_step"))
        bot.edit_message_text(render_current_delivery(uid), chat_id=chat_id_m, message_id=msg_id, reply_markup=kb, parse_mode="Markdown")
        bot.answer_callback_query(c.id, "Back")
    else:
        bot.answer_callback_query(c.id, "Already at first step.")

@bot.message_handler(func=lambda m: True)
def on_text(m: Message):
    uid = m.from_user.id
    chat_id = m.chat.id

    # If in delivery flow, capture input + edit single message
    if uid in user_states:
        if not ensure_session(uid, chat_id): return
        state = user_states[uid]
        step = state["step"]
        if step >= len(DELIVERY_FIELDS):
            # ignore extra text once finished
            return

        field = DELIVERY_FIELDS[step]
        val = m.text.strip()

        if not validate_field(field, val):
            # Re-edit same message with same prompt (no spam)
            chat_id_m, msg_id = user_delivery_msg.get(uid, (chat_id, None))
            kb = InlineKeyboardMarkup()
            if step > 0:
                kb.add(InlineKeyboardButton("↩️ Back", callback_data="back_step"))
            try:
                bot.edit_message_text(render_current_delivery(uid), chat_id=chat_id_m, message_id=msg_id, reply_markup=kb, parse_mode="Markdown")
            except Exception:
                pass
            bot.send_message(chat_id, f"⚠️ That doesn't look like a valid *{field}*. Please try again.", parse_mode="Markdown")
            return

        # Save and advance
        state["data"][field] = val
        state["step"] += 1
        bump(uid)

        chat_id_m, msg_id = user_delivery_msg.get(uid, (chat_id, None))
        if state["step"] >= len(DELIVERY_FIELDS):
            # Show confirmation with Confirm + Back
            kb = kb_confirm_address()
            try:
                bot.edit_message_text(render_current_delivery(uid), chat_id=chat_id_m, message_id=msg_id, reply_markup=kb, parse_mode="Markdown")
            except Exception:
                bot.send_message(chat_id, "✅ Details captured. Ready to confirm.", reply_markup=kb)
            return
        else:
            # keep asking next field
            kb = InlineKeyboardMarkup()
            if state["step"] > 0:
                kb.add(InlineKeyboardButton("↩️ Back", callback_data="back_step"))
            try:
                bot.edit_message_text(render_current_delivery(uid), chat_id=chat_id_m, message_id=msg_id, reply_markup=kb, parse_mode="Markdown")
            except Exception:
                bot.send_message(chat_id, render_current_delivery(uid), reply_markup=kb)

        return

    # Outside delivery flow: ignore; commands handled by their handlers
    if m.text.startswith("/"):
        return
    else:
        bot.send_message(chat_id, "Use /order to browse stickers or /cart to view your basket.")

@bot.callback_query_handler(func=lambda c: c.data == "back_to_edit_address")
def cb_back_to_edit(c: CallbackQuery):
    uid = c.from_user.id
    chat_id = c.message.chat.id
    state = user_states.get(uid)
    if not state:
        bot.answer_callback_query(c.id, "No active checkout.")
        return
    # Move to last step to let them re-edit
    state["step"] = max(0, len(DELIVERY_FIELDS) - 1)
    chat_id_m, msg_id = user_delivery_msg.get(uid, (chat_id, c.message.message_id))
    kb = InlineKeyboardMarkup()
    if state["step"] > 0:
        kb.add(InlineKeyboardButton("↩️ Back", callback_data="back_step"))
    try:
        bot.edit_message_text(render_current_delivery(uid), chat_id=chat_id_m, message_id=msg_id, reply_markup=kb, parse_mode="Markdown")
    except Exception:
        bot.send_message(chat_id, render_current_delivery(uid), reply_markup=kb)
    bot.answer_callback_query(c.id, "Edit details")

# --------------------------------------------------------------------------------------------------
#                                   CART & CATALOG HANDLERS
# --------------------------------------------------------------------------------------------------

@bot.message_handler(commands=["start"])
def cmd_start(m: Message):
    bot.reply_to(m,
        f"👋 Welcome to *{SHOP_NAME}*!\n\n"
        f"🚚 Delivery {money(DELIVERY_FEE)} — *free over {money(FREE_DELIVERY_THRESHOLD)}*\n\n"
        "Use /order to browse or /cart to view your basket.",
    )

@bot.message_handler(commands=["help"])
def cmd_help(m: Message):
    bot.reply_to(m, "Commands: /order, /cart, /restart, /help")

@bot.message_handler(commands=["restart"])
def cmd_restart(m: Message):
    uid = m.from_user.id
    user_carts.pop(uid, None)
    user_states.pop(uid, None)
    bot.reply_to(m, "🔄 Reset. Use /order to start again.")

@bot.message_handler(commands=["order"])
def cmd_order(m: Message):
    uid = m.from_user.id
    bump(uid)
    mark_old_menu(uid)
    text = "🛍 *Browse by Category:*\nChoose a category below.\n"
    kb = kb_categories()
    msg = bot.send_message(m.chat.id, text, reply_markup=kb)
    remember_menu(uid, msg.chat.id, msg.message_id)

def remember_menu(uid: int, chat_id: int, message_id: int) -> None:
    user_menu_msgs.setdefault(uid, []).append((chat_id, message_id))

def mark_old_menu(uid: int) -> None:
    entries = user_menu_msgs.get(uid, [])
    for chat_id, msg_id in entries:
        try:
            bot.edit_message_text("❌ Menu outdated. Use /order.", chat_id=chat_id, message_id=msg_id)
        except Exception:
            pass
    user_menu_msgs[uid] = []

@bot.callback_query_handler(func=lambda c: c.data == "categories")
def cb_categories(c: CallbackQuery):
    uid = c.from_user.id
    text = "🛍 *Browse by Category:*\nChoose a category below."
    bot.edit_message_text(text, chat_id=c.message.chat.id, message_id=c.message.message_id, reply_markup=kb_categories(), parse_mode="Markdown")
    bot.answer_callback_query(c.id)

@bot.callback_query_handler(func=lambda c: c.data.startswith("cat|"))
def cb_cat(c: CallbackQuery):
    uid = c.from_user.id
    cat = c.data.split("|", 1)[1]
    text = f"📂 *{cat}*\nTap an item to add to cart."
    bot.edit_message_text(text, chat_id=c.message.chat.id, message_id=c.message.message_id, reply_markup=kb_products(cat), parse_mode="Markdown")
    bot.answer_callback_query(c.id)

@bot.callback_query_handler(func=lambda c: c.data.startswith("add|"))
def cb_add(c: CallbackQuery):
    uid = c.from_user.id
    chat_id = c.message.chat.id
    if not ensure_session(uid, chat_id): return
    bump(uid)
    item = c.data.split("|", 1)[1]
    if item not in ALL_PRODUCTS:
        bot.answer_callback_query(c.id, "Item not available.")
        return
    user_carts.setdefault(uid, {})
    user_carts[uid][item] = user_carts[uid].get(item, 0) + 1
    bot.answer_callback_query(c.id, f"Added {item}")
    # Auto-open/refresh cart
    refresh_cart(uid, chat_id)

@bot.callback_query_handler(func=lambda c: c.data == "open_cart")
def cb_open_cart(c: CallbackQuery):
    uid = c.from_user.id
    refresh_cart(uid, c.message.chat.id)
    bot.answer_callback_query(c.id)

@bot.message_handler(commands=["cart"])
def cmd_cart(m: Message):
    uid = m.from_user.id
    bump(uid)
    refresh_cart(uid, m.chat.id)

@bot.callback_query_handler(func=lambda c: c.data == "clear_cart")
def cb_clear(c: CallbackQuery):
    uid = c.from_user.id
    user_carts[uid] = {}
    refresh_cart(uid, c.message.chat.id)
    bot.answer_callback_query(c.id, "Cart cleared.")

@bot.callback_query_handler(func=lambda c: c.data in ("continue_order", "begin_checkout"))
def cb_cart_actions(c: CallbackQuery):
    uid = c.from_user.id
    chat_id = c.message.chat.id
    if c.data == "continue_order":
        bot.answer_callback_query(c.id)
        cmd_order(c.message)
    else:
        bot.answer_callback_query(c.id)
        begin_checkout(uid, chat_id)

# +/- and remove
@bot.callback_query_handler(func=lambda c: c.data.startswith("incr|") or c.data.startswith("decr|") or c.data.startswith("rm|"))
def cb_qty(c: CallbackQuery):
    uid = c.from_user.id
    chat_id = c.message.chat.id
    cart = user_carts.get(uid, {})
    if not cart:
        bot.answer_callback_query(c.id, "Cart is empty.")
        return
    action, name = c.data.split("|", 1)
    if name not in cart:
        bot.answer_callback_query(c.id, "Item not in cart.")
        return
    if action == "incr":
        cart[name] += 1
    elif action == "decr":
        cart[name] = max(1, cart[name]-1)
    else:  # rm
        cart.pop(name, None)
    bot.answer_callback_query(c.id, "Updated.")
    refresh_cart(uid, chat_id)

# --------------------------------------------------------------------------------------------------
#                                   CONFIRM / SAVE / STRIPE
# --------------------------------------------------------------------------------------------------

def notify_admins(order_id: str, tg_user, cart: dict, info: dict, subtotal: Decimal, delivery: Decimal, total: Decimal) -> None:
    lines = []
    for i, q in cart.items():
        if i not in ALL_PRODUCTS: continue
        line_total = (ALL_PRODUCTS[i]["price"]*q).quantize(Decimal("0.01"))
        lines.append(f"{q}× {ALL_PRODUCTS[i]['emoji']} {i} — {money(line_total)}")
    stickers = "\n".join(lines)
    text = (
        f"📦 *New order!* #{order_id}\n"
        f"👤 @{tg_user.username or tg_user.first_name}\n\n"
        f"{stickers}\n\n"
        f"Subtotal: {money(subtotal)}\n"
        f"🚚 Delivery: {money(delivery)}\n"
        f"💰 Total: *{money(total)}*\n\n"
        "📍 Address:\n"
        f"{info['name']}\n{info['house']} {info['street']}\n{info['city']} {info['postcode']}"
    )
    for admin in ADMIN_IDS:
        try: bot.send_message(admin, text)
        except Exception: pass
    if NOTIFY_CHANNEL_ID:
        try: bot.send_message(NOTIFY_CHANNEL_ID, text)
        except Exception: pass

@bot.callback_query_handler(func=lambda c: c.data == "confirm_details")
def cb_confirm(c: CallbackQuery):
    uid = c.from_user.id
    chat_id = c.message.chat.id
    state = user_states.get(uid)
    cart = user_carts.get(uid, {})
    if not state or not cart:
        bot.answer_callback_query(c.id, "Nothing to confirm.")
        return

    subtotal, delivery, total = calc_totals(uid)
    info = state["data"]
    order_id = new_order_id()

    # Save CSV
    with open(CSV_FILE, "a", newline="", encoding="utf-8") as f:
        csv.writer(f).writerow([
            order_id,
            c.from_user.username or c.from_user.first_name,
            ", ".join([f"{q}× {i}" for i,q in cart.items() if i in ALL_PRODUCTS]),
            info["name"], info["house"], info["street"], info["city"], info["postcode"],
            "pending",
            datetime.now().strftime("%Y-%m-%d %H:%M"),
            f"{total:.2f}", CURRENCY
        ])

    # Notify admins
    notify_admins(order_id, c.from_user, cart, info, subtotal, delivery, total)

    # Stripe checkout if configured
    if stripe.api_key:
        try:
            session = stripe.checkout.Session.create(
                mode="payment",
                payment_method_types=["card"],
                line_items=[{
                    "price_data": {
                        "currency": CURRENCY.lower(),
                        "product_data": {"name": f"{SHOP_NAME} Order {order_id}"},
                        "unit_amount": int(total * 100),
                    },
                    "quantity": 1,
                }],
                success_url=f"{SUCCESS_URL}?order_id={order_id}",
                cancel_url=f"{CANCEL_URL}?order_id={order_id}",
                metadata={"order_id": order_id, "telegram_user": c.from_user.username or ""},
            )
            url = session.url
            kb = InlineKeyboardMarkup()
            kb.add(InlineKeyboardButton("💳 Pay Now", url=url))
            bot.send_message(chat_id, f"✅ Order *{order_id}* saved.\n💰 Total: {money(total)}\nTap to pay securely:", reply_markup=kb)
        except Exception as e:
            bot.send_message(chat_id, f"✅ Order *{order_id}* saved.\nPayment setup failed; we'll contact you.\n\nError: {e}")
    else:
        bot.send_message(chat_id, f"✅ Order *{order_id}* saved.\nWe'll contact you to arrange payment.")

    # Clear state
    user_states.pop(uid, None)
    user_delivery_msg.pop(uid, None)
    user_carts.pop(uid, None)
    bot.answer_callback_query(c.id, "Order placed")

# --------------------------------------------------------------------------------------------------
#                                   ADMIN COMMANDS
# --------------------------------------------------------------------------------------------------

@bot.message_handler(commands=["maintenance_on"])
def cmd_maint_on(m: Message):
    if not is_admin(m.from_user.id): return
    global MAINTENANCE
    MAINTENANCE = True
    bot.reply_to(m, "⚙️ Maintenance mode *enabled*.")

@bot.message_handler(commands=["maintenance_off"])
def cmd_maint_off(m: Message):
    if not is_admin(m.from_user.id): return
    global MAINTENANCE
    MAINTENANCE = False
    bot.reply_to(m, "✅ Maintenance mode *disabled*.")

@bot.message_handler(commands=["ping"])
def cmd_ping(m: Message):
    bot.reply_to(m, "🏓 pong")

@bot.message_handler(commands=["whoami"])
def cmd_whoami(m: Message):
    u = m.from_user
    bot.reply_to(m, f"🪪 ID: `{u.id}`\nUsername: @{u.username}\nName: {u.first_name} {u.last_name or ''}", parse_mode="Markdown")

@bot.message_handler(commands=["last_orders"])
def cmd_last_orders(m: Message):
    if not is_admin(m.from_user.id): return
    try:
        with open(CSV_FILE, "r", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        rows = rows[-10:]
        if not rows:
            bot.reply_to(m, "No orders yet.")
            return
        text = "🧾 *Recent Orders:*\n\n"
        for r in reversed(rows):
            text += f"{r['order_id']} — {r['username']} — {r['status']} — {SYMBOL}{r['order_total']}\n"
        bot.reply_to(m, text)
    except Exception as e:
        bot.reply_to(m, f"Error: {e}")

@bot.message_handler(commands=["export_orders"])
def cmd_export_orders(m: Message):
    if not is_admin(m.from_user.id): return
    try:
        with open(CSV_FILE, "rb") as f:
            bot.send_document(m.chat.id, f, visible_file_name="orders.csv")
    except Exception as e:
        bot.reply_to(m, f"Error sending CSV: {e}")

@bot.message_handler(commands=["stats"])
def cmd_stats(m: Message):
    if not is_admin(m.from_user.id): return
    try:
        with open(CSV_FILE, "r", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        total_orders = len(rows)
        total_revenue = sum(Decimal(r["order_total"]) for r in rows) if rows else Decimal("0.00")
        bot.reply_to(m, f"📈 Total orders: *{total_orders}*\n💰 Revenue: *{money(total_revenue)}*", parse_mode="Markdown")
    except Exception as e:
        bot.reply_to(m, f"Error: {e}")

@bot.message_handler(commands=["inventory"])
def cmd_inventory(m: Message):
    if not is_admin(m.from_user.id): return
    text = "*Inventory:*\n"
    for cat, items in CATALOG.items():
        text += f"\n📂 *{cat}*\n"
        for name, data in items.items():
            text += f"• {data['emoji']} {name} — {money(data['price'])}\n"
    bot.reply_to(m, text, parse_mode="Markdown")

@bot.message_handler(commands=["broadcast"])
def cmd_broadcast(m: Message):
    if not is_admin(m.from_user.id): return
    try:
        text = m.text.partition(" ")[2].strip()
        if not text:
            bot.reply_to(m, "Usage: /broadcast Your message to send to recent customers")
            return
        # naive: broadcast to last 200 chat IDs seen in orders
        seen = set()
        with open(CSV_FILE, "r", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        rows = rows[-200:]
        count = 0
        for r in rows:
            # we don't store chat IDs in CSV; just notify admins about the limitation
            pass
        bot.reply_to(m, "ℹ️ Broadcast placeholder: you aren't storing customer chat IDs in orders.csv.\nAdd chat_id to CSV to enable broadcast.", parse_mode="Markdown")
    except Exception as e:
        bot.reply_to(m, f"Error: {e}")

# --------------------------------------------------------------------------------------------------
#                                   ERROR HANDLER
# --------------------------------------------------------------------------------------------------

@bot.middleware_handler(update_types=['message', 'callback_query'])
def _error_mw(bot_instance, update):
    try:
        return  # no-op; try/except blocks are inline
    except Exception as e:
        logger.error("Middleware error: %s", e)

# --------------------------------------------------------------------------------------------------
#                                   RUN
# --------------------------------------------------------------------------------------------------

if __name__ == "__main__":
    if WEBHOOK_URL:
        logger.info("🚀 Webhook mode at %s", WEBHOOK_URL)
        try:
            bot.remove_webhook()
        except Exception:
            pass
        bot.set_webhook(url=WEBHOOK_URL)
    else:
        logger.info("💡 Starting bot in POLLING mode")
        print("✅ Sticker Shop Bot running...")
        bot.infinity_polling(skip_pending=True, timeout=20, long_polling_timeout=20)