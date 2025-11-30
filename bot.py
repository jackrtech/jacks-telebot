
import os
import re
import csv
import json
import logging
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
import requests
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

# ------------------------------
# Load Stripe keys safely
# ------------------------------
STRIPE_SECRET_KEY = ""
STRIPE_PUBLISHABLE_KEY = ""
STRIPE_WEBHOOK_SECRET = ""



if os.path.exists("stripe.txt"):
    try:
        with open("stripe.txt", "r", encoding="utf-8") as f:
            lines = [line.strip() for line in f.readlines()]

        # Expecting exactly 3 lines:
        # Line 1 → secret key
        # Line 2 → publishable key
        # Line 3 → webhook secret
        if len(lines) >= 1:
            STRIPE_SECRET_KEY = lines[0]
        if len(lines) >= 2:
            STRIPE_PUBLISHABLE_KEY = lines[1]
        if len(lines) >= 3:
            STRIPE_WEBHOOK_SECRET = lines[2]

    except Exception as e:
        print("⚠️ Error reading stripe.txt:", e)

# Fallbacks if environment variables or config provide keys
if not STRIPE_SECRET_KEY:
    STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY", "").strip()
if not STRIPE_SECRET_KEY:
    STRIPE_SECRET_KEY = cfg.get("stripe_secret_key", "").strip()

# Apply Stripe secret key
stripe.api_key = STRIPE_SECRET_KEY

# -----------------------------
# Load Mailgun Credentials
# -----------------------------
MAILGUN_DOMAIN = ""
MAILGUN_API_KEY = ""
MAILGUN_SMTP_LOGIN = ""
MAILGUN_SMTP_PASSWORD = ""

def load_mailgun_config():
    config = {}
    if os.path.exists("mailgun.txt"):
        with open("mailgun.txt", "r", encoding="utf-8") as f:
            for line in f:
                if "=" in line:
                    k, v = line.strip().split("=", 1)
                    config[k] = v
    return config

mailgun_config = load_mailgun_config()

MAILGUN_DOMAIN = mailgun_config.get("MAILGUN_DOMAIN", "")
MAILGUN_SMTP_SERVER = mailgun_config.get("MAILGUN_SMTP_SERVER", "")
MAILGUN_API_KEY = mailgun_config.get("MAILGUN_API_KEY", "")
MAILGUN_SMTP_LOGIN = mailgun_config.get("MAILGUN_SMTP_LOGIN", "")
MAILGUN_SMTP_PASSWORD = mailgun_config.get("MAILGUN_SMTP_PASSWORD", "")
MAILGUN_FROM_EMAIL = mailgun_config.get("MAILGUN_FROM_EMAIL", "")
MAILGUN_TO_EMAIL = mailgun_config.get("MAILGUN_TO_EMAIL", "")

#comment

# Success / cancel URLs for Stripe Checkout
SUCCESS_URL = cfg.get("success_url", "https://postmenuk.org/success")
CANCEL_URL = cfg.get("cancel_url", "https://postmenuk.org/cancel")


# ----------------------------------------------------------------------
# Load Outlook email credentials (sender, password, recipient)
# ----------------------------------------------------------------------
EMAIL_USER, EMAIL_PASS, EMAIL_TO = None, None, None
try:
    if os.path.exists("email.txt"):
        with open("email.txt", "r", encoding="utf-8") as ef:
            lines = [ln.strip() for ln in ef.read().splitlines() if ln.strip()]
        if len(lines) >= 2:
            EMAIL_USER = lines[0]
            EMAIL_PASS = lines[1]
            EMAIL_TO = lines[2] if len(lines) >= 3 else lines[0]
except Exception as e:
    print(f"⚠️ Email credentials not loaded: {e}")


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
    "name": "📝 (1/5) Enter *name* for delivery:",
    "house": "📝 (2/5) Enter *House Number / Name:*",
    "street": "📝 (3/5) Enter *Street Name:*",
    "city": "📝 (4/5) Enter *City / Town:*",
    "postcode": "📝 (5/5) Enter *Postcode:*",
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

def validate_field(field, text):
    """Basic validation for checkout fields."""
    text = text.strip()

    if field == "name":
        return len(text) >= 2

    if field == "house":
        return len(text) >= 1

    if field == "street":
        return len(text) >= 2

    if field == "city":
        return len(text) >= 2

    if field == "postcode":
        # Very loose UK postcode check
        return len(text) >= 4

    return True


# =====================================================
#  DELIVERY MESSAGE EDITOR  (add near the top of bot.py)
# =====================================================
def edit_or_send(bot, chat_id, text, reply_markup=None, user_states=None):
    """
    Try editing the existing message instead of sending a new one.
    If editing fails (e.g. old message deleted), send a new message and store its id.
    """
    msg_id = None
    if user_states and chat_id in user_states:
        msg_id = user_states[chat_id].get("msg_id")

    try:
        if msg_id:
            bot.edit_message_text(
                chat_id=chat_id,
                message_id=msg_id,
                text=text,
                reply_markup=reply_markup,
                parse_mode="Markdown",
            )
            return msg_id
        else:
            sent = bot.send_message(chat_id, text, reply_markup=reply_markup, parse_mode="Markdown")
            user_states[chat_id]["msg_id"] = sent.message_id
            return sent.message_id
    except Exception:
        sent = bot.send_message(chat_id, text, reply_markup=reply_markup, parse_mode="Markdown")
        user_states[chat_id]["msg_id"] = sent.message_id
        return sent.message_id


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
    """Delete previous /order messages instead of marking them outdated."""
    entries = user_menu_messages.get(user_id, [])
    if not entries:
        return

    for chat_id, msg_id in entries:
        try:
            bot.delete_message(chat_id, msg_id)
        except Exception:
            pass

    user_menu_messages[user_id] = []



def build_cart_text(user_id):
    """Build the cart summary showing EACH item on its own line (delivery always free)."""
    cart = user_carts.get(user_id, {})
    if not cart:
        return ("🛒 Your cart is empty. Use /order to add stickers.", False)

    text = "🛒 *Your Cart:*\n\n"
    subtotal = Decimal("0.00")
    total_items = 0

    for item, qty in cart.items():
        if item not in catalog:
            continue

        price = catalog[item]["price"]
        for _ in range(qty):
            text += f"{item} — {SYMBOL}{price:.2f}\n"
            subtotal += price
            total_items += 1

    total = subtotal.quantize(Decimal("0.01"), ROUND_HALF_UP)

    text += (
        f"\n*Total items:* {total_items}\n"
        f"*Total:* {SYMBOL}{total:.2f}"
    )

    return (text, True)



def refresh_cart_message(user_id, chat_id):
    """Edit the existing cart message if possible. Only create a new one if edit fails."""
    text, has_items = build_cart_text(user_id)

    kb = InlineKeyboardMarkup()
    if has_items:
        kb.add(
            InlineKeyboardButton("🏁 Checkout", callback_data="begin_checkout"),
            InlineKeyboardButton("🔌 Continue Shopping", callback_data="continue_order"),
            InlineKeyboardButton("🪓 Clear Cart", callback_data="clear_cart"),
        )
    else:
        kb.add(InlineKeyboardButton("🔌 Continue Shopping", callback_data="continue_order"))

    existing = user_cart_message.get(user_id)

    # Try EDIT first (only correct behaviour for adding items)
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
            return   # SUCCESS: edited, no new message created
        except Exception:
            pass  # If editing fails, we fall through and send a new one

    # If no cart exists or edit failed → SEND NEW (for /cart, open_cart, checkout)
    msg = bot.send_message(chat_id, text, parse_mode="Markdown", reply_markup=kb)
    user_cart_message[user_id] = (chat_id, msg.message_id)




def prompt_next_field(chat_id, field, step):
    """Prompt user for the given delivery field."""
    kb = InlineKeyboardMarkup()
    if step == 0:
        kb.add(InlineKeyboardButton("🛍 Continue Shopping", callback_data="continue_order"))
    else:
        kb.add(InlineKeyboardButton("↩️ /back", callback_data="back"))

    edit_or_send(bot, chat_id, delivery_prompts[field],
             reply_markup=kb, user_states=user_states)



def send_order_review(chat_id, user_id):

    # Move user to the review screen
    user_states[user_id]["step"] = len(delivery_steps)

    info = user_states[user_id]["data"]
    cart = user_carts.get(user_id, {})

    if not cart:
        bot.send_message(chat_id, "🛒 Your cart is empty. Please /order again.")
        clear_session(user_id)
        return

    # Build items list
    lines = []
    subtotal = Decimal("0.00")

    for item, qty in cart.items():
        if item not in catalog:
            continue

        price = catalog[item]["price"]
        line_total = (price * qty).quantize(Decimal("0.01"), ROUND_HALF_UP)
        subtotal += line_total

        # Clean simple format: 3 x Multipack — £5.00
        lines.append(f"{qty} × {item} — {SYMBOL}{line_total:.2f}")

    # Delivery logic
    if subtotal >= FREE_DELIVERY_THRESHOLD:
        delivery = Decimal("0.00")
    else:
        delivery = DELIVERY_FEE

    total = (subtotal + delivery).quantize(Decimal("0.01"), ROUND_HALF_UP)

    # Build the message
    summary = (
        "🧾 *Confirm your order:*\n\n"
        "*Items:*\n"
        + "\n".join(lines)
        + "\n\n"
        f"*Total:* {SYMBOL}{total:.2f}\n\n"
        "*Delivery address:*\n"
        f"{info['name']}\n"
        f"{info['house']} {info['street']}\n"
        f"{info['city']} {info['postcode']}"
    )

    # Buttons
    kb = InlineKeyboardMarkup()
    kb.add(
        InlineKeyboardButton("✅ Confirm", callback_data="confirm_details"),
        InlineKeyboardButton("✏️ Edit Address", callback_data="edit_address"),
        InlineKeyboardButton("↩️ Back", callback_data="back"),
    )

    # Reset stored msg_id so prompt_next_field can safely write new messages
    user_states[user_id].pop("msg_id", None)

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

    if delivery:
        delivery_text = "✉️ Free Postage"
    #else:
     #   delivery_text = f"🚚 Delivery: {SYMBOL}{delivery:.2f}"

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
        f"🏴 Welcome to *{SHOP_NAME}*\n\n"
        #f"🚚 Delivery is First-Class & Free, "
        #f"*free over {SYMBOL}{FREE_DELIVERY_THRESHOLD:.2f}* \n\n"
        "Use /order to browse or /cart to view your cart.\n"
        "🚧 Use /restart at anytime.",
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

    text = "📠 *Our Stickers:*\n"
    #for name, data in catalog.items():
        #text += f"{data['emoji']} {name} — {SYMBOL}{data['price']:.2f}\n"
    text = "Free First-Class Postage (UK)\n"
    text += "For international orders, contact us directly\n"

    text += (
       #f"\n🔌 Delivery: {SYMBOL}{DELIVERY_FEE:.2f} "
        #f"(free over {SYMBOL}{FREE_DELIVERY_THRESHOLD:.2f})\n"
        "Tap a button below to add to your cart 👇"
    )

    kb = InlineKeyboardMarkup(row_width=2)
    for name, data in catalog.items():
        kb.add(InlineKeyboardButton(f"{name} - £{data['price']}", callback_data=f"add|{name}"))

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

    # DELETE OLD CART MESSAGE on purpose
    old = user_cart_message.get(user_id)
    if old:
        try:
            bot.delete_message(old[0], old[1])
        except Exception:
            pass

    # Create NEW cart message
    refresh_cart_message(user_id, chat_id)



@bot.message_handler(commands=["cart"])
def show_cart(message):
    chat_id = message.chat.id
    user_id = message.from_user.id

    # DELETE OLD CART MESSAGE for manual /cart
    old = user_cart_message.get(user_id)
    if old:
        try:
            bot.delete_message(old[0], old[1])
        except Exception:
            pass

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

    # --- DELETE OLD CART MESSAGE IF STARTING CHECKOUT ---
    old = user_cart_message.get(user_id)
    if old:
        try:
            bot.delete_message(old[0], old[1])
        except:
            pass

    cart = user_carts.get(user_id, {})
    if not cart:
        bot.send_message(chat_id, "🛍 Your cart is empty! Add stickers first with /order.")
        return

    if check_and_handle_expiry(user_id, chat_id, is_callback=True, callback_id=callback.id):
        return

    update_activity(user_id)

    # Set initial checkout state
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
        f"Total: {SYMBOL}{subtotal:.2f}\n"
        "Enter Delivery Details:",
        parse_mode="Markdown",
    )

    # NOW the missing line:
    prompt_next_field(chat_id, "name", step=0)




# ----------------------------------------------------------------------
# Checkout navigation (back / edit)
# ----------------------------------------------------------------------

@bot.callback_query_handler(func=lambda c: c.data == "back")
def go_back(callback):
    user_id = callback.from_user.id
    chat_id = callback.message.chat.id

    if user_id not in user_states or "step" not in user_states[user_id]:
        bot.answer_callback_query(callback.id, "No previous step to go back to.")
        return

    step = user_states[user_id]["step"]

    if step == 0:
        bot.answer_callback_query(callback.id, "You're already at the first step.")
        return

    # Move one step back
    user_states[user_id]["step"] = step - 1
    prev_field = delivery_steps[user_states[user_id]["step"]]

    # Build back button again
    back_markup = InlineKeyboardMarkup()
    back_markup.add(InlineKeyboardButton("↩️ Back", callback_data="back"))

    # Show the previous prompt
    edit_or_send(
        bot,
        chat_id,
        delivery_prompts[prev_field],
        reply_markup=back_markup,
        user_states=user_states
    )

    bot.answer_callback_query(callback.id)



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

    # ---- CALCULATE COSTS ----
    subtotal = sum(
        (catalog[item]["price"] * qty for item, qty in cart.items() if item in catalog),
        Decimal("0.00"),
    ).quantize(Decimal("0.01"), ROUND_HALF_UP)

    delivery = Decimal("0.00") if subtotal >= FREE_DELIVERY_THRESHOLD else DELIVERY_FEE
    total = (subtotal + delivery).quantize(Decimal("0.01"), ROUND_HALF_UP)

    order_id = generate_order_id()
    cart_summary = ", ".join(
        [f"{qty}x {item}" for item, qty in cart.items() if item in catalog]
    )

    # ---- SAVE TO CSV ----
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

    # Notify admins
    notify_admins(order_id, callback.from_user, cart, info, subtotal, delivery, total)

    # ---- BUILD METADATA ----
    # Item breakdown for Stripe webhook
    items_list = [
        {
            "name": item,
            "qty": qty,
            "price": int(catalog[item]["price"] * 100)
        }
        for item, qty in cart.items()
        if item in catalog
    ]

    # Delivery address multiline
    delivery_address = (
        f"{info['name']}\n"
        f"{info['house']} {info['street']}\n"
        f"{info['city']}\n"
        f"{info['postcode']}"
    )

    # ---- STRIPE CHECKOUT ----
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

                # ---- FULL METADATA FOR RECEIPTS ----
                metadata={
                    "order_id": order_id,
                    "telegram_user": callback.from_user.username or "",
                    "telegram_user_id": str(user_id),
                    "items_json": json.dumps(items_list),
                    "subtotal": str(int(subtotal * 100)),
                    "delivery_cost": str(int(delivery * 100)),
                    "delivery_address": delivery_address,
                },
            )

            pay_url = checkout_session.url

            kb = InlineKeyboardMarkup()
            kb.add(InlineKeyboardButton("💳 Pay Now", url=pay_url))

            bot.send_message(
                chat_id,
                f"✅ Order *{order_id}* saved.\n"
                f"💷 Total: {SYMBOL}{total:.2f}\n"
                "Tap below to complete your payment securely:",
                parse_mode="HTML",
                reply_markup=kb,
            )

        except Exception as e:
            bot.send_message(
                chat_id,
                "✅ Your order has been saved, but payment setup failed.\n"
                "We'll contact you soon to arrange payment manually.\n"
                f"Error: {e}",
                parse_mode="HTML",
            )
    else:
        # No Stripe
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
# Test Outlook email sending (manual trigger)
# ----------------------------------------------------------------------
import smtplib
from email.mime.text import MIMEText

def test_email():
    """Send a test email using Outlook credentials."""
    if not (EMAIL_USER and EMAIL_PASS):
        print("⚠️ Email credentials missing.")
        return

    msg = MIMEText("Hello! This is a test email from your Telegram bot.")
    msg["Subject"] = "Test email from Sticker Shop Bot"
    msg["From"] = EMAIL_USER
    msg["To"] = EMAIL_TO or EMAIL_USER

    try:
        with smtplib.SMTP("smtp.office365.com", 587) as s:
            s.starttls()
            s.login(EMAIL_USER, EMAIL_PASS)
            s.send_message(msg)
        print("✅ Test email sent successfully!")
    except Exception as e:
        print(f"⚠️ Email send error: {e}")


# ----------------------------------------------------------------------
# Start bot (webhook or polling mode)
# ----------------------------------------------------------------------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)




# ----------------------------------------------------------------------
# Run bot (polling or webhook)
# ----------------------------------------------------------------------

# ================================
#  MAILGUN EMAIL SENDER (STAGING)
# ================================
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

import smtplib
from email.mime.text import MIMEText

import smtplib
from email.mime.text import MIMEText

def send_internal_email(subject, text):
    """
    Sends order notification email using Mailgun SMTP (FREE TIER).
    """
    print("📨 DEBUG: send_internal_email() called")
    print("📨 Subject:", subject)
    print("📨 Text preview:", text[:60])


    try:
        import smtplib
        from email.mime.text import MIMEText
        

        # Load SMTP credentials
        smtp_server = MAILGUN_SMTP_SERVER
        smtp_login = MAILGUN_SMTP_LOGIN
        smtp_password = MAILGUN_SMTP_PASSWORD

        print("📨 DEBUG: Preparing SMTP with:", MAILGUN_SMTP_SERVER, MAILGUN_SMTP_LOGIN)


        sender = MAILGUN_FROM_EMAIL or smtp_login
        recipient = MAILGUN_TO_EMAIL

        if not smtp_server or not smtp_login or not smtp_password:
            print("❌ SMTP not fully configured.")
            return False

        # ⭐ IMPORTANT: UTF-8 encoding (fixes £, ×, —, emojis)
        msg = MIMEText(text, _charset="UTF-8")
        msg["Subject"] = subject
        msg["From"] = sender
        msg["To"] = recipient

        print("📨 DEBUG: Sending SMTP now...")

        # ⭐ Use the configured SMTP server (not hard-coded)
        with smtplib.SMTP(smtp_server, 587) as server:
            server.starttls()
            server.login(smtp_login, smtp_password)
            server.sendmail(sender, [recipient], msg.as_string())

        print("📧 SMTP email sent successfully.")
        return True

    except Exception as e:
        print(f"❌ SMTP email failed: {e}")
        return False




# ----------------------------------------------------------------------
# Logging
# ----------------------------------------------------------------------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ----------------------------------------------------------------------
# Flask Web Server for Stripe Webhooks
# ----------------------------------------------------------------------
from flask import Flask, request
app = Flask(__name__)


@app.route("/stripe-webhook", methods=["POST"])
def stripe_webhook():
    print("🔔 Stripe webhook triggered")
    print("Headers:", dict(request.headers))
    print("Raw payload:", request.data.decode("utf-8"))

    payload = request.data
    sig_header = request.headers.get("Stripe-Signature")
    endpoint_secret = STRIPE_WEBHOOK_SECRET

    # ------------------------------------------------------
    # 1. VALIDATE SIGNATURE & BUILD EVENT
    # ------------------------------------------------------
    try:
        event = stripe.Webhook.construct_event(
            payload, sig_header, endpoint_secret
        )
    except Exception as e:
        print("❌ Webhook signature error:", e)
        return f"Webhook error: {e}", 400

    print("🔎 Event type:", event.get("type"))

    # ------------------------------------------------------
    # 2. HANDLE SUCCESSFUL PAYMENT
    # ------------------------------------------------------
    if event["type"] == "checkout.session.completed":
        session = event["data"]["object"]

        print("✅ checkout.session.completed event received")
        print("Session:", session)
        print("Metadata:", session.get("metadata"))

        metadata = session.get("metadata", {})

        order_id = metadata.get("order_id")
        username = metadata.get("telegram_user")
        telegram_user_id = int(metadata.get("telegram_user_id"))

        # Itemisation fields
        import json
        items_json = metadata.get("items_json", "[]")
        delivery_address = metadata.get("delivery_address", "No address provided")
        subtotal = int(metadata.get("subtotal", 0))
        delivery_cost = int(metadata.get("delivery_cost", 0))

        try:
            items = json.loads(items_json)
        except:
            items = []

        total_paid = session.get("amount_total", 0)

        # UK timestamp
        from datetime import datetime
        import pytz
        uk = pytz.timezone("Europe/London")
        order_time_uk = datetime.now(uk).strftime("%d %b %Y, %H:%M")

        # Build item lines
        item_lines = []
        for item in items:
            name = item.get("name")
            qty = int(item.get("qty"))
            price_each = int(item.get("price", 0))
            line_total = (price_each * qty) / 100
            item_lines.append(f"{qty} × {name} — £{line_total:.2f}")

        items_formatted = "\n".join(item_lines) if item_lines else "No item breakdown."

        # Final receipt
        receipt_message = (
            "🧾 *Payment Receipt*\n\n"
            f"*Order ID:* `{order_id}`\n"
            f"*Date:* {order_time_uk} (UK)\n\n"
            f"👤 *Customer* @{username} \n"
            f"@{username} (ID: `{telegram_user_id}`)\n\n"
            "📦 *Items*\n"
            f"{items_formatted}\n\n"
            #f"*Subtotal:* £{subtotal/100:.2f}\n"
            #f"*Delivery:* £{delivery_cost/100:.2f}\n"
            f"*Total Paid:* £{total_paid/100:.2f}\n\n"
            "*Delivery Address*\n"
            f"{delivery_address}\n\n"
            "*Welcome to the Postmen 📮*"
        )

        # -------------- SEND TELEGRAM RECEIPT ----------------
        try:
            bot.send_message(
                telegram_user_id,
                receipt_message,
                parse_mode="Markdown"
            )
            print(f"📨 Sent Telegram receipt to user {telegram_user_id}")
        except Exception as e:
            print(f"⚠️ Telegram receipt failed: {e}")

        # ------------------------------------------------------
        # 3. SEND ADMIN EMAIL (Mailgun)
        # ------------------------------------------------------
        email_subject = f"New Order Received — {order_id}"

        email_text = (
            f"New order received!\n\n"
            f"Order ID: {order_id}\n"
            f"Date: {order_time_uk} (UK)\n"
            f"Customer: @{username} (ID: {telegram_user_id})\n\n"
            "Items:\n"
            f"{items_formatted}\n\n"
            f"Subtotal: £{subtotal/100:.2f}\n"
            f"Delivery: £{delivery_cost/100:.2f}\n"
            f"Total Paid: £{total_paid/100:.2f}\n\n"
            "Delivery Address:\n"
            f"{delivery_address}\n\n"
        )

        try:
            send_internal_email(email_subject, email_text)
            print("📧 Admin email sent via Mailgun.")
        except Exception as e:
            print(f"❌ Failed to send admin email: {e}")

    print("Webhook handler finished")
    return "", 200




# ----------------------------------------------------------------------
# Run Telegram Bot + Flask Together
# ----------------------------------------------------------------------
from threading import Thread

@app.route("/")
def home():
    return "PostmenUK Bot is running!"


@app.route("/telegram_webhook", methods=["POST"])
def telegram_webhook():
    if request.headers.get("content-type") == "application/json":
        update = request.get_json(force=True)
        bot.process_new_updates([telebot.types.Update.de_json(update)])
        return "OK", 200
    return "Invalid", 400

@app.route("/success")
def payment_success():
    return """
    <html>
        <body style='font-family: Arial; text-align:center; padding-top:50px;'>
            <h2>🎉 Payment Successful</h2>
            <p>Your payment has been received.</p>
            <p>You may now return to Telegram.</p>
        </body>
    </html>
    """, 200


@app.route("/cancel")
def payment_cancel():
    return """
    <html>
        <body style='font-family: Arial; text-align:center; padding-top:50px;'>
            <h2>❌ Payment Cancelled</h2>
            <p>Your payment was cancelled.</p>
            <p>You can return to Telegram and try again.</p>
        </body>
    </html>
    """, 200




def run_telegram():
    logger.info("💡 Starting Telegram bot (Polling mode)...")
    bot.infinity_polling(skip_pending=True, timeout=20, long_polling_timeout=20)

    

def run_flask():
    logger.info("🌐 Starting Flask webhook server on port 8000...")
    app.run(host="0.0.0.0", port=8000)

if __name__ == "__main__":
    # Start Flask background thread
    flask_thread = Thread(target=run_flask)
    flask_thread.daemon = True
    flask_thread.start()

    # Start Telegram bot in main thread
    run_telegram()
