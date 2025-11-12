import os
import re
import csv
import json
import logging
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP

import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton

# ----------------------------------------------------------------------
# Environment / Config Setup
# ----------------------------------------------------------------------
TOKEN = os.getenv("BOT_TOKEN", "").strip()
if not TOKEN:
    with open("token.txt", "r") as f:
        TOKEN = f.read().strip()

MAINTENANCE = os.getenv("MAINTENANCE", "false").lower() == "true"
ENV = os.getenv("ENV", "dev")  # dev / prod
WEBHOOK_URL = os.getenv("WEBHOOK_URL")

SESSION_TIMEOUT_SECONDS = 3600  # 1 hour

bot = telebot.TeleBot(TOKEN)

# ----------------------------------------------------------------------
# Load configuration (shop, catalog, admins)
# ----------------------------------------------------------------------
with open("config.json", "r") as f:
    cfg = json.load(f)

SHOP_NAME = cfg.get("shop_name", "Sticker Shop")
CURRENCY = cfg.get("currency", "GBP")
SYMBOL = cfg.get("symbol", "£")
ADMIN_IDS = cfg.get("admin_ids", [])
NOTIFY_CHANNEL_ID = cfg.get("notify_channel_id")

DELIVERY_FEE = Decimal(str(cfg.get("delivery_fee", 2.50)))
FREE_DELIVERY_THRESHOLD = Decimal(str(cfg.get("free_delivery_threshold", 10.00)))

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
# Data stores
# ----------------------------------------------------------------------
user_carts = {}
user_states = {}
last_activity = {}
user_menu_messages = {}
user_cart_message = {}

delivery_steps = ["name", "house", "street", "city", "postcode"]
delivery_prompts = {
    "name": "📝 (1/5) Please enter your *Full Name:*",
    "house": "📝 (2/5) Enter your *House Number / Name:*",
    "street": "📝 (3/5) Enter your *Street Name:*",
    "city": "📝 (4/5) Enter your *City / Town:*",
    "postcode": "📝 (5/5) Enter your *Postcode:*",
}

# ----------------------------------------------------------------------
# CSV setup
# ----------------------------------------------------------------------
csv_filename = "orders.csv"
if not os.path.exists(csv_filename) or os.path.getsize(csv_filename) == 0:
    with open(csv_filename, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(
            [
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
            ]
        )

# ----------------------------------------------------------------------
# Order counter file setup
# ----------------------------------------------------------------------
counter_file = "order_counter.json"
if os.path.exists(counter_file):
    with open(counter_file, "r") as f:
        order_counters = json.load(f)
else:
    order_counters = {}


def generate_order_id():
    today = datetime.now().strftime("%y%m%d")
    count = order_counters.get(today, 0) + 1
    order_counters[today] = count
    with open(counter_file, "w") as f:
        json.dump(order_counters, f)
    return f"ORD-{today}-{count:02d}"



# ----------------------------------------------------------------------
# Helper functions: sessions, maintenance, menus, validation
# ----------------------------------------------------------------------

# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------
def is_down(chat_id):
    if MAINTENANCE:
        bot.send_message(
            chat_id,
            "⚙️ Sorry! The shop is currently *down for maintenance.*\n\nPlease try again soon.",
            parse_mode="Markdown",
        )
        return True
    return False


def has_active_session(user_id):
    return (user_id in user_states and user_states[user_id]) or (
        user_id in user_carts and bool(user_carts[user_id])
    )


def update_activity(user_id):
    last_activity[user_id] = datetime.now()


def clear_session(user_id):
    user_carts.pop(user_id, None)
    user_states.pop(user_id, None)
    last_activity.pop(user_id, None)


def check_and_handle_expiry(user_id, chat_id, is_callback=False, callback_id=None):
    if not has_active_session(user_id):
        return False
    ts = last_activity.get(user_id)
    if not ts:
        return False
    if datetime.now() - ts > timedelta(seconds=SESSION_TIMEOUT_SECONDS):
        clear_session(user_id)
        if is_callback and callback_id:
            bot.answer_callback_query(callback_id, "⏰ Session expired.")
        bot.send_message(
            chat_id,
            "⏰ Your session has expired. Please start again with /order.",
        )
        return True
    return False


def mark_old_menus_outdated(user_id):
    entries = user_menu_messages.get(user_id, [])
    if not entries:
        return
    for chat_id, msg_id in entries:
        try:
            bot.edit_message_caption(
                chat_id=chat_id,
                message_id=msg_id,
                caption="❌ This menu is outdated. Please use /order again.",
            )
        except Exception:
            try:
                bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=msg_id,
                    text="❌ This menu is outdated. Please use /order again.",
                )
            except Exception:
                pass
    user_menu_messages[user_id] = []


def build_cart_text(user_id):
    cart = user_carts.get(user_id, {})
    if not cart:
        return ("🛒 Your cart is empty. Use /order to add stickers.", False)
    text = "🛒 *Your Cart:*\n\n"
    total_items = 0
    total_price = Decimal("0.00")
    for item, qty in cart.items():
        if item not in catalog:
            continue
        price = catalog[item]["price"]
        subtotal = (price * qty).quantize(Decimal("0.01"), ROUND_HALF_UP)
        text += f"{qty}x {catalog[item]['emoji']} {item} — {SYMBOL}{subtotal:.2f}\n"
        total_items += qty
        total_price += subtotal

    # Apply delivery fee/discount logic
    delivery_fee = DELIVERY_FEE if total_price < FREE_DELIVERY_THRESHOLD else Decimal("0.00")
    total_price += delivery_fee

    if delivery_fee > 0:
        text += f"\n🚚 Delivery: {SYMBOL}{delivery_fee:.2f}"
    else:
        text += f"\n🚚 Delivery: *FREE*"

    text += f"\n\nTotal items: {total_items}\n💰 *Total: {SYMBOL}{total_price:.2f}*"
    return (text, True)


def refresh_cart_message(user_id, chat_id):
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
            pass
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

# ----------------------------------------------------------------------
# Core Commands
# ----------------------------------------------------------------------
@bot.message_handler(commands=["start"])
def start(message):
    chat_id = message.chat.id
    user_id = message.from_user.id
    if is_down(chat_id):
        return
    update_activity(user_id)
    if user_carts.get(user_id):
        extra = "You still have items in your cart — open /cart to review.\n\n"
    else:
        extra = ""
    bot.send_message(
        chat_id,
        f"👋 Welcome to *{SHOP_NAME}!*\n\n{extra}"
        "Use /order to browse stickers or /cart to view your cart.\n\n"
        "💡 Use /restart to start fresh.",
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
# Catalog browsing (with persistent Checkout button)
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

    header = bot.send_message(chat_id, "🛍 *Browse our sticker collection:*", parse_mode="Markdown")
    user_menu_messages[user_id].append((chat_id, header.message_id))

    for name, data in catalog.items():
        price_text = f"{SYMBOL}{data['price']:.2f}"
        caption = f"{data['emoji']} {name}\n💷 {price_text} each"
        kb = InlineKeyboardMarkup()
        kb.add(InlineKeyboardButton(f"Add {data['emoji']} {name} ({price_text})", callback_data=f"add|{name}"))
        try:
            with open(data["image"], "rb") as img:
                msg = bot.send_photo(chat_id, img, caption=caption, reply_markup=kb)
                user_menu_messages[user_id].append((chat_id, msg.message_id))
        except FileNotFoundError:
            msg = bot.send_message(chat_id, f"⚠️ Image for {name} missing.")
            user_menu_messages[user_id].append((chat_id, msg.message_id))

    # 🆕 Persistent Checkout button below the product list
    footer_kb = InlineKeyboardMarkup()
    footer_kb.add(InlineKeyboardButton("✅ Checkout", callback_data="checkout_now"))
    footer = bot.send_message(chat_id, "When ready, tap below to checkout.", reply_markup=footer_kb)
    user_menu_messages[user_id].append((chat_id, footer.message_id))


# ----------------------------------------------------------------------
# Persistent Checkout button handler
# ----------------------------------------------------------------------
@bot.callback_query_handler(func=lambda c: c.data == "checkout_now")
def checkout_now(callback):
    user_id = callback.from_user.id
    chat_id = callback.message.chat.id
    cart = user_carts.get(user_id, {})

    if not cart:
        bot.answer_callback_query(callback.id, "🛒 Your cart is empty! Add items before checkout.")
        return

    bot.answer_callback_query(callback.id, "✅ Proceeding to checkout...")
    begin_checkout(callback)


# ----------------------------------------------------------------------
# Cart management
# ----------------------------------------------------------------------
@bot.callback_query_handler(func=lambda c: c.data.startswith("add|"))
def add_to_cart(callback):
    user_id = callback.from_user.id
    chat_id = callback.message.chat.id
    if check_and_handle_expiry(user_id, chat_id, is_callback=True, callback_id=callback.id):
        return
    item = callback.data.split("|", 1)[1]
    if item not in catalog:
        bot.answer_callback_query(callback.id, "⚠️ Menu outdated. Use /order.")
        bot.send_message(chat_id, "⚠️ Please use /order for current stickers.")
        return
    update_activity(user_id)
    user_carts.setdefault(user_id, {})
    user_carts[user_id][item] = user_carts[user_id].get(item, 0) + 1
    bot.answer_callback_query(callback.id, f"✅ Added {item}!")
    if user_cart_message.get(user_id):
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
    update_activity(user_id)
    user_states[user_id] = {"step": 0, "data": {}}
    total_price = sum(
        (catalog[item]["price"] * qty for item, qty in cart.items()), Decimal("0.00")
    ).quantize(Decimal("0.01"), ROUND_HALF_UP)

    delivery_fee = DELIVERY_FEE if total_price < FREE_DELIVERY_THRESHOLD else Decimal("0.00")
    total_price += delivery_fee

    summary_lines = [f"{qty}x {item}" for item, qty in cart.items()]
    summary = "\n".join(summary_lines)
    delivery_text = (
        f"🚚 Delivery: {SYMBOL}{delivery_fee:.2f}"
        if delivery_fee > 0
        else "🚚 Delivery: FREE"
    )

    bot.send_message(
        chat_id,
        f"🧾 *Your Order Summary:*\n\n{summary}\n\n{delivery_text}\n💰 *Total: {SYMBOL}{total_price:.2f}*\n\nLet's collect your delivery details.",
        parse_mode="Markdown",
    )
    prompt_next_field(chat_id, "name", step=0)


# ----------------------------------------------------------------------
# Checkout flow
# ----------------------------------------------------------------------
def prompt_next_field(chat_id, field, step):
    kb = InlineKeyboardMarkup()
    if step == 0:
        kb.add(InlineKeyboardButton("🛍 Continue Shopping", callback_data="continue_order"))
    else:
        kb.add(InlineKeyboardButton("↩️ /back", callback_data="back"))
    bot.send_message(chat_id, delivery_prompts[field], parse_mode="Markdown", reply_markup=kb)


def validate_field(field, text):
    t = text.strip()
    if field == "name":
        return bool(re.match(r"^[A-Za-z\s]{3,}$", t)) and " " in t
    if field == "house":
        return bool(re.match(r"^[A-Za-z0-9\s]{1,10}$", t))
    if field == "street":
        return len(t) >= 3 and any(c.isalpha() for c in t)
    if field == "city":
        return len(t) >= 2 and any(c.isalpha() for c in t)
    if field == "postcode":
        return bool(re.match(r"^[A-Z]{1,2}[0-9][0-9A-Z]?\s?[0-9][A-Z]{2}$", t.upper()))
    return True


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
        bot.answer_callback_query(callback.id, "You're at the first step.")


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
    bot.answer_callback_query(callback.id, "✏️ Let’s edit your address.")
    prompt_next_field(chat_id, "name", step=0)


def send_order_review(chat_id, user_id):
    info = user_states[user_id]["data"]
    cart = user_carts.get(user_id, {})
    if not cart:
        bot.send_message(chat_id, "🛒 Your cart is empty. Please /order again.")
        clear_session(user_id)
        return
    lines = [
        f"{qty}x {catalog[item]['emoji']} {item} — {SYMBOL}{(catalog[item]['price'] * qty):.2f}"
        for item, qty in cart.items() if item in catalog
    ]
    total_price = sum(
        (catalog[item]["price"] * qty for item, qty in cart.items() if item in catalog),
        Decimal("0.00"),
    ).quantize(Decimal("0.01"), ROUND_HALF_UP)

    delivery_fee = DELIVERY_FEE if total_price < FREE_DELIVERY_THRESHOLD else Decimal("0.00")
    total_price += delivery_fee

    delivery_text = (
        f"🚚 Delivery: {SYMBOL}{delivery_fee:.2f}"
        if delivery_fee > 0
        else "🚚 Delivery: FREE"
    )

    summary = (
        f"✅ *Confirm your address and cart:*\n\n"
        f"🛍 Stickers:\n" + "\n".join(lines) +
        f"\n\n{delivery_text}\n💰 *Total: {SYMBOL}{total_price:.2f}*\n\n"
        f"👤 Name: {info['name']}\n"
        f"🏠 House: {info['house']}\n"
        f"🛣 Street: {info['street']}\n"
        f"🌆 City: {info['city']}\n"
        f"📮 Postcode: {info['postcode']}"
    )
    kb = InlineKeyboardMarkup()
    kb.add(
        InlineKeyboardButton("✅ Confirm", callback_data="confirm_details"),
        InlineKeyboardButton("✏️ Edit Address", callback_data="edit_address"),
        InlineKeyboardButton("↩️ /back", callback_data="back"),
    )
    bot.send_message(chat_id, summary, parse_mode="Markdown", reply_markup=kb)


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
            bot.send_message(chat_id, f"⚠️ That doesn’t look like a valid {field}. Please try again.")
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

    if text.startswith("/"):
        bot.send_message(
            chat_id,
            "❓ Unknown command.\nUse /order to browse, /cart to view your cart, or /restart to reset.",
        )
    else:
        bot.send_message(
            chat_id,
            "🛍 To start shopping, use /order.\n"
            "To see your cart, use /cart.\n"
            "If something feels stuck, use /restart.",
        )


# ----------------------------------------------------------------------
# Confirm Order → Save to CSV + Notify Admins
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

    cart_summary = ", ".join([f"{qty}x {item}" for item, qty in cart.items() if item in catalog])
    total_price = sum(
        (catalog[item]["price"] * qty for item, qty in cart.items() if item in catalog),
        Decimal("0.00")
    ).quantize(Decimal("0.01"), ROUND_HALF_UP)

    delivery_fee = DELIVERY_FEE if total_price < FREE_DELIVERY_THRESHOLD else Decimal("0.00")
    total_price += delivery_fee

    order_id = generate_order_id()

    with open(csv_filename, "a", newline="") as f:
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
            f"{total_price:.2f}",
            CURRENCY
        ])

    bot.answer_callback_query(callback.id, "✅ Order saved!")

    notify_admins(
        order_id=order_id,
        user=callback.from_user,
        cart=cart,
        info=info,
        total=total_price
    )

    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("🛍 Make Another Order", callback_data="continue_order"))

    bot.send_message(
        chat_id,
        f"✅ Order *{order_id}* saved.\n"
        f"💰 Total: {SYMBOL}{total_price:.2f}\n"
        "We'll contact you soon for payment.",
        parse_mode="Markdown",
        reply_markup=kb
    )

    clear_session(user_id)


def notify_admins(order_id, user, cart, info, total):
    lines = [
        f"{qty}x {catalog[item]['emoji']} {item} — {SYMBOL}{(catalog[item]['price'] * qty):.2f}"
        for item, qty in cart.items() if item in catalog
    ]
    stickers_block = "\n".join(lines)

    text = (
        f"📦 *New order received!* \n"
        f"🆔 Order ID: *{order_id}*\n"
        f"👤 Telegram: @{user.username or user.first_name}\n\n"
        f"{stickers_block}\n\n"
        f"💰 Total: *{SYMBOL}{total:.2f}*\n\n"
        f"📍 Address:\n"
        f"{info['name']}\n"
        f"{info['house']} {info['street']}\n"
        f"{info['city']} {info['postcode']}\n\n"
        f"Status: _pending_"
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


# ----------------------------------------------------------------------
# Admin Commands + Startup
# ----------------------------------------------------------------------
def is_admin(user_id):
    return user_id in ADMIN_IDS


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
        with open(csv_filename, "r") as f:
            rows = list(csv.DictReader(f))
        if not rows:
            bot.reply_to(message, "No orders found.")
            return
        recent = rows[-5:]
        text = "🧾 *Last 5 Orders:*\n\n"
        for row in reversed(recent):
            text += f"• {row['order_id']} — {row['username']} — {row['status']} — {SYMBOL}{row['order_total']}\n"
        bot.reply_to(message, text, parse_mode="Markdown")
    except Exception as e:
        bot.reply_to(message, f"⚠️ Error reading orders: {e}")


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
