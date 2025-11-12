
import os
import re
import csv
import json
import logging
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP

import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
import stripe

# ----------------------------------------------------------------------
# Token / Environment / Config Setup
# ----------------------------------------------------------------------

# Prefer reading Telegram bot token from token.txt; fall back to BOT_TOKEN env
TOKEN = None
try:
    if os.path.exists("token.txt"):
        with open("token.txt", "r", encoding="utf-8") as f:
            TOKEN = f.read().strip()
except Exception:
    TOKEN = None

if not TOKEN:
    TOKEN = os.getenv("BOT_TOKEN", "").strip()

if not TOKEN:
    raise ValueError("❌ No Telegram token found. Put it in token.txt or set BOT_TOKEN env var.")

MAINTENANCE = os.getenv("MAINTENANCE", "false").lower() == "true"
ENV = os.getenv("ENV", "dev")  # dev / prod
WEBHOOK_URL = os.getenv("WEBHOOK_URL")

# Session timeout for cart / checkout flows
SESSION_TIMEOUT_SECONDS = 3600  # 1 hour

# Initialize bot
bot = telebot.TeleBot(TOKEN)

# ----------------------------------------------------------------------
# Load configuration (shop, catalog, admins, delivery, Stripe)
# ----------------------------------------------------------------------

with open("config.json", "r", encoding="utf-8") as f:
    cfg = json.load(f)

SHOP_NAME = cfg.get("shop_name", "Sticker Shop")
CURRENCY = cfg.get("currency", "GBP")
SYMBOL = cfg.get("symbol", "£")

# Delivery configuration
DELIVERY_FEE = Decimal(str(cfg.get("delivery_fee", "2.50")))
FREE_DELIVERY_THRESHOLD = Decimal(str(cfg.get("free_delivery_threshold", "10.00")))

# Admin / notifications
ADMIN_IDS = cfg.get("admin_ids", [])
NOTIFY_CHANNEL_ID = cfg.get("notify_channel_id")

# Stripe configuration
# Prefer stripe.txt, then environment, then (optionally) config.json fallback
STRIPE_SECRET_KEY = ""
if os.path.exists("stripe.txt"):
    with open("stripe.txt", "r", encoding="utf-8") as _f:
        STRIPE_SECRET_KEY = _f.read().strip()
if not STRIPE_SECRET_KEY:
    STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY", "").strip()
if not STRIPE_SECRET_KEY:
    STRIPE_SECRET_KEY = cfg.get("stripe_secret_key", "").strip()

SUCCESS_URL = cfg.get("success_url", "https://example.com/success")
CANCEL_URL = cfg.get("cancel_url", "https://example.com/cancel")

stripe.api_key = STRIPE_SECRET_KEY or None
# Catalog configuration
raw_catalog = cfg.get("catalog", {})
catalog = {}
for name, data in raw_catalog.items():
    price = Decimal(str(data.get("price", 0)))
    catalog[name] = {
        "emoji": data.get("emoji", ""),
        "image": data.get("image", ""),
        "price": price,
    }

# ----------------------------------------------------------------------
# In-memory data stores
# ----------------------------------------------------------------------

# Cart: user_id -> { item_name: qty }
user_carts = {}

# Checkout state: user_id -> { "step": int, "data": {...} }
user_states = {}

# Last user activity for timeout: user_id -> datetime
last_activity = {}

# Track menu messages so we can mark them outdated: user_id -> [(chat_id, msg_id), ...]
user_menu_messages = {}

# Track the single "live" cart message per user for inline refresh:
# user_id -> (chat_id, msg_id)
user_cart_message = {}

# Delivery flow configuration
delivery_steps = ["name", "house", "street", "city", "postcode"]
delivery_prompts = {
    "name": "📝 (1/5) Please enter your *Full Name:*",
    "house": "📝 (2/5) Enter your *House Number / Name:*",
    "street": "📝 (3/5) Enter your *Street Name:*",
    "city": "📝 (4/5) Enter your *City / Town:*",
    "postcode": "📝 (5/5) Enter your *Postcode:*",
}

# ----------------------------------------------------------------------
# CSV order storage setup
# ----------------------------------------------------------------------

csv_filename = "orders.csv"

if not os.path.exists(csv_filename) or os.path.getsize(csv_filename) == 0:
    # Create file with headers
    with open(csv_filename, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            "order_id",
            "username",
            "items",
            "name",
            "house",
            "street",
            "city",
            "postcode",
            "status",
            "date",
            "order_total",
            "currency",
        ])

# ----------------------------------------------------------------------
# Order counter for friendly IDs
# ----------------------------------------------------------------------

counter_file = "order_counter.json"

if os.path.exists(counter_file):
    with open(counter_file, "r", encoding="utf-8") as f:
        order_counters = json.load(f)
else:
    order_counters = {}


def generate_order_id():
    """Create a friendly order ID: ORD-YYMMDD-XX"""
    today = datetime.now().strftime("%y%m%d")
    count = order_counters.get(today, 0) + 1
    order_counters[today] = count
    with open(counter_file, "w", encoding="utf-8") as f:
        json.dump(order_counters, f)
    return f"ORD-{today}-{count:02d}"


# ----------------------------------------------------------------------
# Helper functions: sessions, maintenance, menus, validation
# ----------------------------------------------------------------------

def is_down(chat_id):
    """If maintenance mode is enabled, inform the user and block the action."""
    if MAINTENANCE:
        bot.send_message(
            chat_id,
            "⚙️ Sorry! The shop is currently *down for maintenance.*\n\n"
            "Please try again soon.",
            parse_mode="Markdown",
        )
        return True
    return False


def has_active_session(user_id):
    """Return True if user has cart or checkout state."""
    return (
        (user_id in user_states and bool(user_states[user_id]))
        or (user_id in user_carts and bool(user_carts[user_id]))
    )


def update_activity(user_id):
    """Bump last activity timestamp for timeout tracking."""
    last_activity[user_id] = datetime.now()


def clear_session(user_id):
    """Clear cart and checkout state for the user."""
    user_carts.pop(user_id, None)
    user_states.pop(user_id, None)
    last_activity.pop(user_id, None)
    # We intentionally do not delete user_cart_message; old messages just become stale.


def check_and_handle_expiry(user_id, chat_id, is_callback=False, callback_id=None):
    """If session expired, clear and notify. Return True if expired."""
    if not has_active_session(user_id):
        return False

    ts = last_activity.get(user_id)
    if not ts:
        return False

    if datetime.now() - ts > timedelta(seconds=SESSION_TIMEOUT_SECONDS):
        clear_session(user_id)
        if is_callback and callback_id:
            try:
                bot.answer_callback_query(callback_id, "⏰ Session expired.")
            except Exception:
                pass
        bot.send_message(
            chat_id,
            "⏰ Your session has expired. Please start again with /order.",
        )
        return True

    return False


def mark_old_menus_outdated(user_id):
    """Edit previous /order messages for this user and mark them outdated."""
    entries = user_menu_messages.get(user_id, [])
    if not entries:
        return

    for chat_id, msg_id in entries:
        try:
            bot.edit_message_text(
                chat_id=chat_id,
                message_id=msg_id,
                text="❌ This menu is outdated. Please use /order to see the latest stickers.",
                parse_mode="Markdown",
            )
        except Exception:
            pass

    user_menu_messages[user_id] = []


def build_cart_text(user_id):
    """Build the cart summary including delivery fee rules."""
    cart = user_carts.get(user_id, {})
    if not cart:
        return ("🛒 Your cart is empty. Use /order to add stickers.", False)

    text = "🛒 *Your Cart:*\n\n"
    total_items = 0
    subtotal = Decimal("0.00")

    for item, qty in cart.items():
        if item not in catalog:
            continue
        price = catalog[item]["price"]
        line_total = (price * qty).quantize(Decimal("0.01"), ROUND_HALF_UP)
        text += f"{qty}x {catalog[item]['emoji']} {item} — {SYMBOL}{line_total:.2f}\n"
        total_items += qty
        subtotal += line_total

    # Delivery logic
    if subtotal >= FREE_DELIVERY_THRESHOLD:
        delivery = Decimal("0.00")
        delivery_line = f"🚚 *Free delivery!* (orders over {SYMBOL}{FREE_DELIVERY_THRESHOLD:.2f})"
    else:
        delivery = DELIVERY_FEE
        delivery_line = f"🚚 Delivery fee: {SYMBOL}{DELIVERY_FEE:.2f}"

    total = (subtotal + delivery).quantize(Decimal("0.01"), ROUND_HALF_UP)

    text += (
        f"\nTotal items: {total_items}\n"
        f"Subtotal: {SYMBOL}{subtotal:.2f}\n"
        f"{delivery_line}\n"
        f"💰 *Total: {SYMBOL}{total:.2f}*"
    )

    return (text, True)


def refresh_cart_message(user_id, chat_id):
    """Create or update the single cart message with inline controls."""
    text, has_items = build_cart_text(user_id)

    kb = InlineKeyboardMarkup()
    if has_items:
        kb.add(
            InlineKeyboardButton("✅ Checkout", callback_data="begin_checkout"),
            InlineKeyboardButton("🛍 Continue Shopping", callback_data="continue_order"),
            InlineKeyboardButton("🗑 Clear Cart", callback_data="clear_cart"),
        )
    else:
        kb.add(InlineKeyboardButton("🛍 Continue Shopping", callback_data="continue_order"))

    existing = user_cart_message.get(user_id)

    if existing:
        e_chat_id, e_msg_id = existing
        try:
            bot.edit_message_text(
                chat_id=e_chat_id,
                message_id=e_msg_id,
                text=text,
                parse_mode="Markdown",
                reply_markup=kb,
            )
            return
        except Exception:
            pass  # fall through and send new

    msg = bot.send_message(chat_id, text, parse_mode="Markdown", reply_markup=kb)
    user_cart_message[user_id] = (chat_id, msg.message_id)


def prompt_next_field(chat_id, field, step):
    """Prompt user for the given delivery field."""
    kb = InlineKeyboardMarkup()
    if step == 0:
        kb.add(InlineKeyboardButton("🛍 Continue Shopping", callback_data="continue_order"))
    else:
        kb.add(InlineKeyboardButton("↩️ /back", callback_data="back"))

    bot.send_message(
        chat_id,
        delivery_prompts[field],
        parse_mode="Markdown",
        reply_markup=kb,
    )


def validate_field(field, text):
    """Basic validation rules for checkout fields."""
    t = text.strip()
    if field == "name":
        return bool(re.match(r"^[A-Za-z\s]{3,}$", t)) and " " in t
    if field == "house":
        return bool(re.match(r"^[A-Za-z0-9\s\-]{1,10}$", t))
    if field == "street":
        return len(t) >= 3 and any(c.isalpha() for c in t)
    if field == "city":
        return len(t) >= 2 and any(c.isalpha() for c in t)
    if field == "postcode":
        # UK-style; adjust if needed
        return bool(re.match(r"^[A-Z]{1,2}[0-9][0-9A-Z]?\s?[0-9][A-Z]{2}$", t.upper()))
    return True


def send_order_review(chat_id, user_id):
    """Show final confirmation: items + address + delivery + total."""
    info = user_states[user_id]["data"]
    cart = user_carts.get(user_id, {})

    if not cart:
        bot.send_message(chat_id, "🛒 Your cart is empty. Please /order again.")
        clear_session(user_id)
        return

    lines = []
    subtotal = Decimal("0.00")

    for item, qty in cart.items():
        if item not in catalog:
            continue
        price = catalog[item]["price"]
        line_total = (price * qty).quantize(Decimal("0.01"), ROUND_HALF_UP)
        subtotal += line_total
        lines.append(f"{qty}x {catalog[item]['emoji']} {item} — {SYMBOL}{line_total:.2f}")

    if subtotal >= FREE_DELIVERY_THRESHOLD:
        delivery = Decimal("0.00")
        delivery_line = "🚚 *Free delivery applied!* 🎉"
    else:
        delivery = DELIVERY_FEE
        delivery_line = f"🚚 Delivery: {SYMBOL}{DELIVERY_FEE:.2f}"

    total = (subtotal + delivery).quantize(Decimal("0.01"), ROUND_HALF_UP)

    summary = (
        "✅ *Confirm your order:*\n\n"
        "🛍 *Stickers:*\n" + "\n".join(lines) +
        f"\n\nSubtotal: {SYMBOL}{subtotal:.2f}\n"
        f"{delivery_line}\n"
        f"💰 *Total: {SYMBOL}{total:.2f}*\n\n"
        "📍 *Delivery Address:*\n"
        f"{info['name']}\n"
        f"{info['house']} {info['street']}\n"
        f"{info['city']} {info['postcode']}"
    )

    kb = InlineKeyboardMarkup()
    kb.add(
        InlineKeyboardButton("✅ Confirm", callback_data="confirm_details"),
        InlineKeyboardButton("✏️ Edit Address", callback_data="edit_address"),
        InlineKeyboardButton("↩️ /back", callback_data="back"),
    )

    bot.send_message(chat_id, summary, parse_mode="Markdown", reply_markup=kb)


def notify_admins(order_id, user, cart, info, subtotal, delivery, total):
    """Notify admins (and optional channel) of a new order."""
    lines = []
    for item, qty in cart.items():
        if item not in catalog:
            continue
        price = catalog[item]["price"]
        line_total = (price * qty).quantize(Decimal("0.01"), ROUND_HALF_UP)
        lines.append(f"{qty}x {catalog[item]['emoji']} {item} — {SYMBOL}{line_total:.2f}")
    stickers_block = "\n".join(lines)

    if delivery == 0:
        delivery_text = "🚚 Free delivery"
    else:
        delivery_text = f"🚚 Delivery: {SYMBOL}{delivery:.2f}"

    text = (
        f"📦 *New order received!*\n"
        f"🆔 Order ID: *{order_id}*\n"
        f"👤 Telegram: @{user.username or user.first_name}\n\n"
        f"{stickers_block}\n\n"
        f"Subtotal: {SYMBOL}{subtotal:.2f}\n"
        f"{delivery_text}\n"
        f"💰 Total: *{SYMBOL}{total:.2f}*\n\n"
        "📍 Address:\n"
        f"{info['name']}\n"
        f"{info['house']} {info['street']}\n"
        f"{info['city']} {info['postcode']}\n\n"
        "Status: _pending_"
    )

    for admin_id in ADMIN_IDS:
        try:
            bot.send_message(admin_id, text, parse_mode="Markdown")
        except Exception:
            pass

    if NOTIFY_CHANNEL_ID:
        try:
            bot.send_message(NOTIFY_CHANNEL_ID, text, parse_mode="Markdown")
        except Exception:
            pass


def is_admin(user_id):
    return user_id in ADMIN_IDS


# ----------------------------------------------------------------------
# Core commands: /start, /restart, /help
# ----------------------------------------------------------------------

@bot.message_handler(commands=["start"])
def start(message):
    chat_id = message.chat.id
    user_id = message.from_user.id

    if is_down(chat_id):
        return

    update_activity(user_id)

    bot.send_message(
        chat_id,
        f"👋 Welcome to *{SHOP_NAME}!*\n\n"
        f"🚚 Delivery is {SYMBOL}{DELIVERY_FEE:.2f}, "
        f"*free over {SYMBOL}{FREE_DELIVERY_THRESHOLD:.2f}!* 🎉\n\n"
        "Use /order to browse stickers or /cart to view your cart.\n"
        "💡 Use /restart if anything feels stuck.",
        parse_mode="Markdown",
    )


@bot.message_handler(commands=["restart"])
def restart(message):
    chat_id = message.chat.id
    user_id = message.from_user.id

    if is_down(chat_id):
        return

    clear_session(user_id)
    bot.send_message(chat_id, "🔄 Session reset. Use /order to start again.")


@bot.message_handler(commands=["help"])
def help_cmd(message):
    bot.reply_to(
        message,
        "🛒 *Commands:*\n"
        "/order – browse stickers\n"
        "/cart – view your cart\n"
        "/restart – reset session\n"
        "/help – show this message",
        parse_mode="Markdown",
    )


# ----------------------------------------------------------------------
# /order - Show catalog with inline add buttons
# ----------------------------------------------------------------------

@bot.message_handler(commands=["order"])
def order(message):
    chat_id = message.chat.id
    user_id = message.from_user.id

    if is_down(chat_id):
        return

    update_activity(user_id)

    mark_old_menus_outdated(user_id)
    user_menu_messages[user_id] = []

    if not catalog:
        msg = bot.send_message(chat_id, "⚠️ No products are available right now.")
        user_menu_messages[user_id].append((chat_id, msg.message_id))
        return

    text = "🛍 *Our Stickers:*\n\n"
    for name, data in catalog.items():
        text += f"{data['emoji']} {name} — {SYMBOL}{data['price']:.2f}\n"

    text += (
        f"\n🚚 Delivery: {SYMBOL}{DELIVERY_FEE:.2f} "
        f"(free over {SYMBOL}{FREE_DELIVERY_THRESHOLD:.2f})\n"
        "Tap a button below to add to your cart 👇"
    )

    kb = InlineKeyboardMarkup(row_width=2)
    for name, data in catalog.items():
        kb.add(InlineKeyboardButton(f"{data['emoji']} {name}", callback_data=f"add|{name}"))

    # Persistent Open Cart button (replaces Checkout in catalog view)
    kb.add(InlineKeyboardButton("🛒 Open Cart", callback_data="open_cart"))

    msg = bot.send_message(chat_id, text, parse_mode="Markdown", reply_markup=kb)
    user_menu_messages[user_id].append((chat_id, msg.message_id))


# ----------------------------------------------------------------------
# Cart operations
# ----------------------------------------------------------------------

@bot.callback_query_handler(func=lambda c: c.data.startswith("add|"))
def add_to_cart(callback):
    user_id = callback.from_user.id
    chat_id = callback.message.chat.id

    if check_and_handle_expiry(user_id, chat_id, is_callback=True, callback_id=callback.id):
        return

    item = callback.data.split("|", 1)[1]

    if item not in catalog:
        bot.answer_callback_query(callback.id, "⚠️ Item no longer available.")
        return

    update_activity(user_id)

    user_carts.setdefault(user_id, {})
    user_carts[user_id][item] = user_carts[user_id].get(item, 0) + 1

    bot.answer_callback_query(callback.id, f"🛒 Added {item}!")

    # Always show or refresh cart (auto open on first add)
    refresh_cart_message(user_id, chat_id)


@bot.callback_query_handler(func=lambda c: c.data == "open_cart")
def open_cart_callback(callback):
    user_id = callback.from_user.id
    chat_id = callback.message.chat.id
    bot.answer_callback_query(callback.id)
    refresh_cart_message(user_id, chat_id)


@bot.message_handler(commands=["cart"])
def show_cart(message):
    chat_id = message.chat.id
    user_id = message.from_user.id

    if is_down(chat_id):
        return

    if check_and_handle_expiry(user_id, chat_id):
        return

    update_activity(user_id)
    refresh_cart_message(user_id, chat_id)


@bot.callback_query_handler(func=lambda c: c.data == "clear_cart")
def clear_cart(callback):
    user_id = callback.from_user.id
    chat_id = callback.message.chat.id

    if check_and_handle_expiry(user_id, chat_id, is_callback=True, callback_id=callback.id):
        return

    update_activity(user_id)

    user_carts[user_id] = {}
    bot.answer_callback_query(callback.id, "🗑 Cart cleared!")
    refresh_cart_message(user_id, chat_id)


@bot.callback_query_handler(func=lambda c: c.data in ["continue_order", "begin_checkout"])
def handle_cart_actions(callback):
    user_id = callback.from_user.id
    chat_id = callback.message.chat.id

    if check_and_handle_expiry(user_id, chat_id, is_callback=True, callback_id=callback.id):
        return

    update_activity(user_id)

    if callback.data == "continue_order":
        bot.answer_callback_query(callback.id)
        order(callback.message)

    elif callback.data == "begin_checkout":
        bot.answer_callback_query(callback.id)
        begin_checkout(callback)


def begin_checkout(callback):
    user_id = callback.from_user.id
    chat_id = callback.message.chat.id

    cart = user_carts.get(user_id, {})
    if not cart:
        bot.send_message(chat_id, "🛍 Your cart is empty! Add stickers first with /order.")
        return

    if check_and_handle_expiry(user_id, chat_id, is_callback=True, callback_id=callback.id):
        return

    update_activity(user_id)

    user_states[user_id] = {"step": 0, "data": {}}

    subtotal = sum(
        (catalog[item]["price"] * qty for item, qty in cart.items() if item in catalog),
        Decimal("0.00"),
    ).quantize(Decimal("0.01"), ROUND_HALF_UP)

    lines = [f"{qty}x {item}" for item, qty in cart.items() if item in catalog]
    summary = "\n".join(lines)

    bot.send_message(
        chat_id,
        f"🧾 *Your Order Summary:*\n\n"
        f"{summary}\n\n"
        f"Current subtotal: {SYMBOL}{subtotal:.2f}\n"
        f"🚚 Delivery: {SYMBOL}{DELIVERY_FEE:.2f} "
        f"(free over {SYMBOL}{FREE_DELIVERY_THRESHOLD:.2f})\n\n"
        "Now let's collect your delivery details.",
        parse_mode="Markdown",
    )

    prompt_next_field(chat_id, "name", step=0)


# ----------------------------------------------------------------------
# Checkout navigation (back / edit)
# ----------------------------------------------------------------------

@bot.callback_query_handler(func=lambda c: c.data == "back")
def go_back(callback):
    user_id = callback.from_user.id
    chat_id = callback.message.chat.id

    if check_and_handle_expiry(user_id, chat_id, is_callback=True, callback_id=callback.id):
        return

    if user_id not in user_states:
        bot.answer_callback_query(callback.id, "No active checkout.")
        return

    step = user_states[user_id]["step"]

    if step > 0:
        user_states[user_id]["step"] -= 1
        prev_field = delivery_steps[user_states[user_id]["step"]]
        bot.answer_callback_query(callback.id)
        bot.send_message(chat_id, "↩️ Going back.", parse_mode="Markdown")
        prompt_next_field(chat_id, prev_field, user_states[user_id]["step"])
        update_activity(user_id)
    else:
        bot.answer_callback_query(callback.id, "You're already at the first step.")


@bot.callback_query_handler(func=lambda c: c.data == "edit_address")
def edit_address(callback):
    user_id = callback.from_user.id
    chat_id = callback.message.chat.id

    if check_and_handle_expiry(user_id, chat_id, is_callback=True, callback_id=callback.id):
        return

    if user_id not in user_states or "data" not in user_states[user_id]:
        bot.answer_callback_query(callback.id, "No address to edit.")
        return

    user_states[user_id]["step"] = 0
    bot.answer_callback_query(callback.id, "✏️ Let's edit your address.")
    prompt_next_field(chat_id, "name", step=0)


# ----------------------------------------------------------------------
# Text input handler during checkout
# ----------------------------------------------------------------------

@bot.message_handler(func=lambda m: True)
def handle_checkout_input(message):
    user_id = message.from_user.id
    chat_id = message.chat.id
    text = message.text.strip()

    if user_id in user_states:
        if check_and_handle_expiry(user_id, chat_id):
            return

        step = user_states[user_id]["step"]
        if step >= len(delivery_steps):
            return

        field = delivery_steps[step]

        if not validate_field(field, text):
            bot.send_message(
                chat_id,
                f"⚠️ That doesn’t look like a valid {field}. Please try again.",
            )
            prompt_next_field(chat_id, field, step)
            return

        user_states[user_id]["data"][field] = text
        user_states[user_id]["step"] += 1
        update_activity(user_id)

        if user_states[user_id]["step"] >= len(delivery_steps):
            send_order_review(chat_id, user_id)
            return

        next_field = delivery_steps[user_states[user_id]["step"]]
        prompt_next_field(chat_id, next_field, user_states[user_id]["step"])
        return

    # Not in checkout: respond helpfully
    if text.startswith("/"):
        bot.send_message(
            chat_id,
            "❓ Unknown command.\n"
            "Use /order to browse, /cart to view your cart, or /restart to reset.",
        )
    else:
        bot.send_message(
            chat_id,
            "🛍 To start shopping, use /order.\n"
            "To see your cart, use /cart.\n"
            "If something feels stuck, use /restart.",
        )


# ----------------------------------------------------------------------
# Confirm Order → CSV + Stripe Checkout + Admin notify
# ----------------------------------------------------------------------

@bot.callback_query_handler(func=lambda c: c.data == "confirm_details")
def confirm_order(callback):
    user_id = callback.from_user.id
    chat_id = callback.message.chat.id

    if check_and_handle_expiry(user_id, chat_id, is_callback=True, callback_id=callback.id):
        return

    if user_id not in user_states or "data" not in user_states[user_id]:
        bot.answer_callback_query(callback.id, "No order to confirm.")
        return

    info = user_states[user_id]["data"]
    cart = user_carts.get(user_id, {})

    if not cart:
        bot.answer_callback_query(callback.id, "Cart is empty.")
        bot.send_message(chat_id, "🛒 Your cart is empty. Please /order again.")
        clear_session(user_id)
        return

    subtotal = sum(
        (catalog[item]["price"] * qty for item, qty in cart.items() if item in catalog),
        Decimal("0.00"),
    ).quantize(Decimal("0.01"), ROUND_HALF_UP)

    if subtotal >= FREE_DELIVERY_THRESHOLD:
        delivery = Decimal("0.00")
    else:
        delivery = DELIVERY_FEE

    total = (subtotal + delivery).quantize(Decimal("0.01"), ROUND_HALF_UP)

    order_id = generate_order_id()
    cart_summary = ", ".join(
        [f"{qty}x {item}" for item, qty in cart.items() if item in catalog]
    )

    # Save order as pending in CSV
    with open(csv_filename, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            order_id,
            callback.from_user.username or callback.from_user.first_name,
            cart_summary,
            info["name"],
            info["house"],
            info["street"],
            info["city"],
            info["postcode"],
            "pending",
            datetime.now().strftime("%Y-%m-%d %H:%M"),
            f"{total:.2f}",
            CURRENCY,
        ])

    bot.answer_callback_query(callback.id, "✅ Order saved!")

    # Notify admins about new order
    notify_admins(order_id, callback.from_user, cart, info, subtotal, delivery, total)

    # If Stripe configured → create Checkout Session
    if stripe.api_key:
        try:
            checkout_session = stripe.checkout.Session.create(
                mode="payment",
                payment_method_types=["card"],
                line_items=[
                    {
                        "price_data": {
                            "currency": CURRENCY.lower(),
                            "product_data": {
                                "name": f"{SHOP_NAME} Order {order_id}",
                            },
                            "unit_amount": int(total * 100),
                        },
                        "quantity": 1,
                    }
                ],
                success_url=f"{SUCCESS_URL}?order_id={order_id}",
                cancel_url=f"{CANCEL_URL}?order_id={order_id}",
                metadata={
                    "order_id": order_id,
                    "telegram_user": callback.from_user.username or "",
                },
            )

            pay_url = checkout_session.url

            kb = InlineKeyboardMarkup()
            kb.add(InlineKeyboardButton("💳 Pay Now", url=pay_url))
            kb.add(InlineKeyboardButton("🛍 Make Another Order", callback_data="continue_order"))

            bot.send_message(
                chat_id,
                f"✅ Order *{order_id}* saved.\n"
                f"💰 Total: {SYMBOL}{total:.2f}\n"
                "Tap below to complete your payment securely:",
                parse_mode="Markdown",
                reply_markup=kb,
            )

        except Exception as e:
            # Fallback to manual payment if Stripe fails
            bot.send_message(
                chat_id,
                "✅ Your order has been saved, but payment setup failed.\n"
                "We'll contact you soon to arrange payment manually.\n"
                f"Error: {e}",
                parse_mode="Markdown",
            )
    else:
        # No Stripe: old behaviour
        kb = InlineKeyboardMarkup()
        kb.add(InlineKeyboardButton("🛍 Make Another Order", callback_data="continue_order"))

        bot.send_message(
            chat_id,
            f"✅ Order *{order_id}* saved.\n"
            f"💰 Total: {SYMBOL}{total:.2f}\n"
            "We'll contact you soon for payment.",
            parse_mode="Markdown",
            reply_markup=kb,
        )

    # Clear session after confirmation
    clear_session(user_id)


# ----------------------------------------------------------------------
# Admin Commands
# ----------------------------------------------------------------------

@bot.message_handler(commands=["maintenance_on"])
def maintenance_on(message):
    global MAINTENANCE
    if not is_admin(message.from_user.id):
        return
    MAINTENANCE = True
    bot.reply_to(message, "⚙️ Maintenance mode *enabled*.", parse_mode="Markdown")


@bot.message_handler(commands=["maintenance_off"])
def maintenance_off(message):
    global MAINTENANCE
    if not is_admin(message.from_user.id):
        return
    MAINTENANCE = False
    bot.reply_to(message, "✅ Maintenance mode *disabled*.", parse_mode="Markdown")


@bot.message_handler(commands=["last_orders"])
def last_orders(message):
    if not is_admin(message.from_user.id):
        return
    try:
        with open(csv_filename, "r", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        if not rows:
            bot.reply_to(message, "No orders found.")
            return
        recent = rows[-5:]
        text = "🧾 *Last 5 Orders:*\n\n"
        for row in reversed(recent):
            text += (
                f"• {row['order_id']} — {row['username']} — "
                f"{row['status']} — {SYMBOL}{row['order_total']}\n"
            )
        bot.reply_to(message, text, parse_mode="Markdown")
    except Exception as e:
        bot.reply_to(message, f"⚠️ Error reading orders: {e}")


# ----------------------------------------------------------------------
# Run bot (polling or webhook)
# ----------------------------------------------------------------------

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

if __name__ == "__main__":
    if WEBHOOK_URL:
        logger.info("🚀 Starting bot in WEBHOOK mode")
        bot.remove_webhook()
        bot.set_webhook(url=WEBHOOK_URL)
    else:
        logger.info("💡 Starting bot in POLLING mode")
        print("✅ Sticker Shop Bot running with Stripe Checkout & delivery rules...")
        bot.infinity_polling(skip_pending=True, timeout=20, long_polling_timeout=20)


# ----------------------------------------------------------------------
# Stripe Webhook Stub (for Phase 2 receipts after payment)
# ----------------------------------------------------------------------
"""
# Example Flask webhook for Stripe payment confirmation
# Uncomment and expand when ready to send Telegram receipts after payment

from flask import Flask, request

app = Flask(__name__)

@app.route("/stripe-webhook", methods=["POST"])
def stripe_webhook():
    payload = request.data
    sig_header = request.headers.get("Stripe-Signature")
    endpoint_secret = os.getenv("STRIPE_WEBHOOK_SECRET")
    try:
        event = stripe.Webhook.construct_event(payload, sig_header, endpoint_secret)
    except Exception as e:
        return f"Webhook error: {e}", 400

    if event["type"] == "checkout.session.completed":
        session = event["data"]["object"]
        order_id = session.get("metadata", {}).get("order_id")
        username = session.get("metadata", {}).get("telegram_user")
        # TODO: mark order as paid in CSV
        # bot.send_message(chat_id_of_user, f"🧾 Payment confirmed! Your order {order_id} is now complete.")
    return "", 200
"""

#new comment