import logging
import os
import json
import re
import threading
from datetime import datetime, timedelta
from functools import wraps
from typing import Optional, Dict, List, Any, Tuple
from collections import defaultdict

from telegram import ChatMember, ChatPermissions, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes
from telegram.error import TelegramError

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

BOT_TOKEN = '8664785841:AAEmsPzvR7s8gdJzcryBAkugucPczIijDKQ'

DEFAULT_MUTE_MINUTES = 60
REPORT_COOLDOWN_MINUTES = 10
MUTED_FILE = "muted_users.json"
MAX_COMMANDS_PER_10_SEC = 5
MAX_AUTO_UNMUTE_FAILURES = 3

MUTE_PERMISSIONS = ChatPermissions(
    can_send_messages=False,
    can_send_audios=False,
    can_send_documents=False,
    can_send_photos=False,
    can_send_videos=False,
    can_send_video_notes=False,
    can_send_voice_notes=False,
    can_send_polls=False,
    can_send_other_messages=False,
    can_add_web_page_previews=False,
    can_change_info=False,
    can_invite_users=False,
    can_pin_messages=False,
    can_manage_topics=False
)

UNMUTE_PERMISSIONS = ChatPermissions(
    can_send_messages=True,
    can_send_audios=True,
    can_send_documents=True,
    can_send_photos=True,
    can_send_videos=True,
    can_send_video_notes=True,
    can_send_voice_notes=True,
    can_send_polls=True,
    can_send_other_messages=True,
    can_add_web_page_previews=True,
    can_change_info=True,
    can_invite_users=True,
    can_pin_messages=True,
    can_manage_topics=True
)

muted_users: Dict[int, Dict[str, Any]] = {}
report_cooldown: Dict[int, Dict[int, List[datetime]]] = defaultdict(lambda: defaultdict(list))
user_command_times: Dict[int, List[datetime]] = defaultdict(list)
unmute_failures: Dict[Tuple[int, int], int] = {}
muted_lock = threading.Lock()

def load_muted_users():
    global muted_users
    if os.path.exists(MUTED_FILE):
        try:
            with open(MUTED_FILE, 'r') as f:
                data = json.load(f)
                muted_users = {
                    int(k): {
                        'unmute_time': datetime.fromisoformat(v['unmute_time']),
                        'chat_id': v['chat_id']
                    } for k, v in data.items()
                }
        except Exception as e:
            logger.error(f"Failed to load muted users: {e}")

def save_muted_users():
    try:
        with muted_lock:
            data = {
                str(k): {
                    'unmute_time': v['unmute_time'].isoformat(),
                    'chat_id': v['chat_id']
                } for k, v in muted_users.items()
            }
            with open(MUTED_FILE, 'w') as f:
                json.dump(data, f)
    except Exception as e:
        logger.error(f"Failed to save muted users: {e}")

async def rate_limit(update: Update) -> bool:
    if not update.effective_user:
        return True

    user_id = update.effective_user.id
    now = datetime.now()
    cutoff = now - timedelta(seconds=10)

    user_command_times[user_id] = [t for t in user_command_times[user_id] if t > cutoff]

    if len(user_command_times[user_id]) >= MAX_COMMANDS_PER_10_SEC:
        if update.message:
            await update.message.reply_text("Too many commands. Please slow down.")
        return False

    user_command_times[user_id].append(now)
    return True

async def is_admin(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int = None) -> bool:
    if not update.effective_chat:
        return False

    if user_id is None:
        if not update.effective_user:
            return False
        user_id = update.effective_user.id

    chat_id = update.effective_chat.id

    try:
        chat_member = await context.bot.get_chat_member(chat_id, user_id)
        return chat_member.status in [ChatMember.ADMINISTRATOR, ChatMember.OWNER]
    except TelegramError as e:
        logger.error(f"Error checking admin status: {e}")
        return False

async def bot_has_permissions(chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> bool:
    try:
        bot_member = await context.bot.get_chat_member(chat_id, context.bot.id)
        return bot_member.status == ChatMember.ADMINISTRATOR
    except TelegramError as e:
        logger.error(f"Error checking bot permissions: {e}")
        return False

def admin_only(func):
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        if not await rate_limit(update):
            return

        if await is_admin(update, context):
            return await func(update, context, *args, **kwargs)

        if update.message:
            await update.message.reply_text("You don't have permission to use this command.")
        return
    return wrapper

def get_time_from_text(text: str) -> Optional[timedelta]:
    match = re.search(r'(\d+)([smhd]?)', text.lower())
    if not match:
        return None

    value = int(match.group(1))
    unit = match.group(2) or 'm'

    if unit == 's':
        return timedelta(seconds=value)
    elif unit == 'm':
        return timedelta(minutes=value)
    elif unit == 'h':
        return timedelta(hours=value)
    elif unit == 'd':
        return timedelta(days=value)

    return timedelta(minutes=value)

def get_message_link(chat_id: int, message_id: int, chat_username: str = None) -> str:
    if chat_username:
        return f"https://t.me/{chat_username}/{message_id}"

    chat_id_str = str(chat_id)
    if chat_id_str.startswith('-100'):
        chat_id_str = chat_id_str[4:]
    return f"https://t.me/c/{chat_id_str}/{message_id}"

async def handle_telegram_error(update: Update, error: TelegramError, action: str) -> str:
    error_str = str(error).lower()

    if "bot is not an administrator" in error_str:
        return "Bot lost admin privileges. Please reinvite with admin rights."
    elif "user is an administrator" in error_str:
        return "Cannot moderate other administrators."
    elif "not enough rights" in error_str:
        return "Bot doesn't have enough rights to perform this action."
    elif "user not found" in error_str:
        return "User not found in this chat."
    elif "chat not found" in error_str:
        return "Chat not found."
    else:
        logger.error(f"Telegram error during {action}: {error}")
        return f"Failed to {action}: {str(error)}"

async def clean_report_cooldown(context: ContextTypes.DEFAULT_TYPE):
    now = datetime.now()
    cooldown_period = timedelta(minutes=REPORT_COOLDOWN_MINUTES)

    for reporter_id in list(report_cooldown.keys()):
        for reported_id in list(report_cooldown[reporter_id].keys()):
            reports = report_cooldown[reporter_id][reported_id]
            reports = [t for t in reports if now - t < cooldown_period]
            if reports:
                report_cooldown[reporter_id][reported_id] = reports
            else:
                del report_cooldown[reporter_id][reported_id]

        if not report_cooldown[reporter_id]:
            del report_cooldown[reporter_id]

# update

async def report_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await rate_limit(update):
        return

    if not update.message or not update.message.reply_to_message:
        await update.message.reply_text("Please reply to a message to report it.")
        return

    reporter_id = update.effective_user.id
    reported_user = update.message.reply_to_message.from_user
    chat_id = update.effective_chat.id
    message_id = update.message.reply_to_message.message_id
    message_text = update.message.reply_to_message.text or "Media message"

    if reported_user.id == context.bot.id:
        await update.message.reply_text("You cannot report the bot.")
        return

    if reported_user.id == reporter_id:
        await update.message.reply_text("You cannot report yourself.")
        return

    now = datetime.now()
    cooldown_period = timedelta(minutes=REPORT_COOLDOWN_MINUTES)

    user_reports = report_cooldown[reporter_id][reported_user.id]
    user_reports = [t for t in user_reports if now - t < cooldown_period]
    report_cooldown[reporter_id][reported_user.id] = user_reports

    if len(user_reports) >= 1:
        next_report_time = user_reports[0] + cooldown_period
        time_left = next_report_time - now
        minutes_left = int(time_left.total_seconds() / 60)
        seconds_left = int(time_left.total_seconds() % 60)

        await update.message.reply_text(
            f"You cannot report this user yet. Wait {minutes_left}m {seconds_left}s."
        )
        return

    report_cooldown[reporter_id][reported_user.id].append(now)

    if not await bot_has_permissions(chat_id, context):
        await update.message.reply_text("Bot needs admin permissions to moderate.")
        return

    keyboard = [
        [
            InlineKeyboardButton("⛔️ Ban", callback_data=f"ban_{reported_user.id}_{message_id}"),
            InlineKeyboardButton("👢 Kick", callback_data=f"kick_{reported_user.id}_{message_id}"),
        ],
        [
            InlineKeyboardButton("🔇 Mute", callback_data=f"mute_{reported_user.id}_{message_id}"),
            InlineKeyboardButton("✅ Skip", callback_data=f"skip_{reported_user.id}_{message_id}"),
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    message_link = get_message_link(chat_id, message_id, update.effective_chat.username)

    await update.message.reply_text(
        f"🚨 REPORT\n"
        f"👤 User: {reported_user.full_name} (ID: {reported_user.id})\n"
        f"📝 Reporter: {update.effective_user.full_name} (ID: {reporter_id})\n"
        f"💬 Message: {message_text}\n"
        f"🔗 <a href='{message_link}'>Jump to message</a>",
        reply_markup=reply_markup,
        parse_mode=ParseMode.HTML
    )

    await update.message.delete()

async def report_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if not update.effective_chat:
        await query.edit_message_text("Invalid chat.")
        return

    user_id = query.from_user.id
    if not await is_admin(update, context, user_id):
        await query.edit_message_text("Only admins can take action on reports.")
        return

    data = query.data.split('_')
    action = data[0]
    reported_user_id = int(data[1])
    message_id = int(data[2])

    if action == "skip":
        await query.edit_message_text("✅ Report skipped.")
        return

    chat_id = update.effective_chat.id

    if not await bot_has_permissions(chat_id, context):
        await query.edit_message_text("Bot needs admin permissions to moderate.")
        return

    try:
        if action == "mute":
            unmute_time = datetime.now() + timedelta(minutes=DEFAULT_MUTE_MINUTES)
            with muted_lock:
                muted_users[reported_user_id] = {
                    'unmute_time': unmute_time,
                    'chat_id': chat_id
                }
            save_muted_users()

            await context.bot.restrict_chat_member(chat_id, reported_user_id, MUTE_PERMISSIONS)

            await query.edit_message_text(
                f"🔇 User muted for {DEFAULT_MUTE_MINUTES} minutes.\n"
                f"Unmute at: {unmute_time.strftime('%Y-%m-%d %H:%M:%S UTC')}"
            )

        elif action == "ban":
            await context.bot.ban_chat_member(chat_id, reported_user_id)
            await query.edit_message_text("⛔️ User banned.")

        elif action == "kick":
            await context.bot.ban_chat_member(chat_id, reported_user_id)
            await context.bot.unban_chat_member(chat_id, reported_user_id)
            await query.edit_message_text("👢 User kicked.")

    except TelegramError as e:
        error_msg = await handle_telegram_error(update, e, action)
        await query.edit_message_text(error_msg)

@admin_only
async def ban_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.reply_to_message:
        await update.message.reply_text("Reply to a user's message to ban them.")
        return

    user_id = update.message.reply_to_message.from_user.id
    chat_id = update.effective_chat.id

    if not await bot_has_permissions(chat_id, context):
        await update.message.reply_text("Bot needs admin permissions to ban.")
        return

    try:
        await context.bot.ban_chat_member(chat_id, user_id)
        await update.message.reply_text(
            f"⛔️ User {update.message.reply_to_message.from_user.full_name} has been banned."
        )
    except TelegramError as e:
        error_msg = await handle_telegram_error(update, e, "ban")
        await update.message.reply_text(error_msg)

@admin_only
async def kick_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.reply_to_message:
        await update.message.reply_text("Reply to a user's message to kick them.")
        return

    user_id = update.message.reply_to_message.from_user.id
    chat_id = update.effective_chat.id

    if not await bot_has_permissions(chat_id, context):
        await update.message.reply_text("Bot needs admin permissions to kick.")
        return

    try:
        await context.bot.ban_chat_member(chat_id, user_id)
        await context.bot.unban_chat_member(chat_id, user_id)
        await update.message.reply_text(
            f"👢 User {update.message.reply_to_message.from_user.full_name} has been kicked."
        )
    except TelegramError as e:
        error_msg = await handle_telegram_error(update, e, "kick")
        await update.message.reply_text(error_msg)

@admin_only
async def mute_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.reply_to_message:
        await update.message.reply_text("Reply to a user's message to mute them.")
        return

    user_id = update.message.reply_to_message.from_user.id
    chat_id = update.effective_chat.id
    mute_duration = get_time_from_text(update.message.text)

    if not mute_duration:
        mute_duration = timedelta(minutes=DEFAULT_MUTE_MINUTES)

    if not await bot_has_permissions(chat_id, context):
        await update.message.reply_text("Bot needs admin permissions to mute.")
        return

    unmute_time = datetime.now() + mute_duration
    with muted_lock:
        muted_users[user_id] = {
            'unmute_time': unmute_time,
            'chat_id': chat_id
        }
    save_muted_users()

    try:
        await context.bot.restrict_chat_member(chat_id, user_id, MUTE_PERMISSIONS)

        if mute_duration.total_seconds() < 60:
            time_str = f"{int(mute_duration.total_seconds())}s"
        elif mute_duration.total_seconds() < 3600:
            time_str = f"{int(mute_duration.total_seconds() / 60)}m"
        elif mute_duration.total_seconds() < 86400:
            time_str = f"{int(mute_duration.total_seconds() / 3600)}h"
        else:
            time_str = f"{int(mute_duration.total_seconds() / 86400)}d"

        await update.message.reply_text(
            f"🔇 User {update.message.reply_to_message.from_user.full_name} "
            f"muted for {time_str}.\n"
            f"Unmute at: {unmute_time.strftime('%Y-%m-%d %H:%M:%S UTC')}"
        )
    except TelegramError as e:
        with muted_lock:
            if user_id in muted_users:
                del muted_users[user_id]
        save_muted_users()
        error_msg = await handle_telegram_error(update, e, "mute")
        await update.message.reply_text(error_msg)

@admin_only
async def unmute_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.reply_to_message:
        await update.message.reply_text("Reply to a user's message to unmute them.")
        return

    user_id = update.message.reply_to_message.from_user.id
    chat_id = update.effective_chat.id

    if not await bot_has_permissions(chat_id, context):
        await update.message.reply_text("Bot needs admin permissions to unmute.")
        return

    try:
        await context.bot.restrict_chat_member(chat_id, user_id, UNMUTE_PERMISSIONS)

        with muted_lock:
            if user_id in muted_users:
                del muted_users[user_id]
        save_muted_users()

        await update.message.reply_text(
            f"🔊 User {update.message.reply_to_message.from_user.full_name} has been unmuted."
        )
    except TelegramError as e:
        error_msg = await handle_telegram_error(update, e, "unmute")
        await update.message.reply_text(error_msg)

@admin_only
async def warn_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.reply_to_message:
        await update.message.reply_text("Reply to a user's message to warn them.")
        return

    warned_user = update.message.reply_to_message.from_user
    admin_user = update.message.from_user

    reason = "No reason provided"
    if len(update.message.text.split()) > 1:
        reason = ' '.join(update.message.text.split()[1:])

    warning_text = (
        f"⚠️ WARNING\n"
        f"User: {warned_user.full_name} (ID: {warned_user.id})\n"
        f"Admin: {admin_user.full_name}\n"
        f"Reason: {reason}"
    )

    await update.message.reply_text(warning_text)

    if 'warnings' not in context.chat_data:
        context.chat_data['warnings'] = []

    context.chat_data['warnings'].append({
        'user_id': warned_user.id,
        'admin_id': admin_user.id,
        'reason': reason,
        'timestamp': datetime.now().isoformat()
    })

@admin_only
async def clear_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.reply_to_message:
        await update.message.reply_text("Reply to a message to clear from there.")
        return

    chat_id = update.effective_chat.id
    message_id = update.message.reply_to_message.message_id

    if not await bot_has_permissions(chat_id, context):
        await update.message.reply_text("Bot needs admin permissions to delete messages.")
        return

    try:
        await context.bot.delete_message(chat_id, message_id)
        await update.message.delete()
        await update.message.reply_text("✅ Messages cleared.")
    except TelegramError as e:
        error_msg = await handle_telegram_error(update, e, "clear messages")
        await update.message.reply_text(error_msg)

@admin_only
async def pin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.reply_to_message:
        await update.message.reply_text("Reply to a message to pin it.")
        return

    message_id = update.message.reply_to_message.message_id
    chat_id = update.effective_chat.id

    if not await bot_has_permissions(chat_id, context):
        await update.message.reply_text("Bot needs admin permissions to pin messages.")
        return

    disable_notification = True
    if context.args and context.args[0].lower() == 'notify':
        disable_notification = False

    try:
        await context.bot.pin_chat_message(
            chat_id=chat_id,
            message_id=message_id,
            disable_notification=disable_notification
        )
        await update.message.reply_text("📌 Message pinned.")
    except TelegramError as e:
        error_msg = await handle_telegram_error(update, e, "pin message")
        await update.message.reply_text(error_msg)

@admin_only
async def unpin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id

    if not await bot_has_permissions(chat_id, context):
        await update.message.reply_text("Bot needs admin permissions to unpin messages.")
        return

    try:
        if context.args and context.args[0] == 'all':
            await context.bot.unpin_all_chat_messages(chat_id)
            await update.message.reply_text("📌 All messages unpinned.")
        else:
            await context.bot.unpin_chat_message(chat_id)
            await update.message.reply_text("📌 Latest message unpinned.")
    except TelegramError as e:
        error_msg = await handle_telegram_error(update, e, "unpin message")
        await update.message.reply_text(error_msg)

async def check_muted_users(context: ContextTypes.DEFAULT_TYPE):
    current_time = datetime.now()
    to_unmute = []

    with muted_lock:
        for user_id, data in list(muted_users.items()):
            if current_time >= data['unmute_time']:
                to_unmute.append((user_id, data['chat_id']))

    for user_id, chat_id in to_unmute:
        try:
            await context.bot.restrict_chat_member(chat_id, user_id, UNMUTE_PERMISSIONS)

            with muted_lock:
                if user_id in muted_users:
                    del muted_users[user_id]
            save_muted_users()

            key = (user_id, chat_id)
            if key in unmute_failures:
                del unmute_failures[key]

            logger.info(f"Auto-unmuted user {user_id} in chat {chat_id}")
        except TelegramError as e:
            key = (user_id, chat_id)
            unmute_failures[key] = unmute_failures.get(key, 0) + 1

            if unmute_failures[key] >= MAX_AUTO_UNMUTE_FAILURES:
                logger.error(f"Removing user {user_id} from mute list after {MAX_AUTO_UNMUTE_FAILURES} failures")
                with muted_lock:
                    if user_id in muted_users:
                        del muted_users[user_id]
                save_muted_users()
                del unmute_failures[key]
            else:
                logger.warning(f"Failed to auto-unmute user {user_id} in chat {chat_id} (attempt {unmute_failures[key]}): {e}")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = (
        "🛠 <b>Admin Commands</b>\n\n"
        "<b>Moderation:</b>\n"
        "<code>/ban</code> - Ban replied user\n"
        "<code>/kick</code> - Kick replied user\n"
        "<code>/mute [time]</code> - Mute replied user (30m, 2h, 1d)\n"
        "<code>/unmute</code> - Unmute replied user\n"
        "<code>/warn [reason]</code> - Warn replied user\n"
        "<code>/report</code> - Report replied message\n\n"
        "<b>Message Management:</b>\n"
        "<code>/clear</code> - Clear messages\n"
        "<code>/pin [notify]</code> - Pin replied message\n"
        "<code>/unpin [all]</code> - Unpin messages\n\n"
        "<b>Other:</b>\n"
        "<code>/help</code> - Show this help\n\n"
        f"⏱ Report cooldown: {REPORT_COOLDOWN_MINUTES} minutes per user\n"
        f"🔇 Default mute time: {DEFAULT_MUTE_MINUTES} minutes"
    )
    await update.message.reply_text(help_text, parse_mode=ParseMode.HTML)

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id if update.effective_chat else 'unknown'
    user_id = update.effective_user.id if update.effective_user else 'unknown'
    logger.error(f"Error in chat {chat_id} for user {user_id}: {context.error}", exc_info=True)

def main():
    load_muted_users()

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("ban", ban_command))
    app.add_handler(CommandHandler("kick", kick_command))
    app.add_handler(CommandHandler("mute", mute_command))
    app.add_handler(CommandHandler("unmute", unmute_command))
    app.add_handler(CommandHandler("warn", warn_command))
    app.add_handler(CommandHandler("report", report_command))
    app.add_handler(CommandHandler("clear", clear_command))
    app.add_handler(CommandHandler("pin", pin_command))
    app.add_handler(CommandHandler("unpin", unpin_command))
    app.add_handler(CommandHandler("help", help_command))

    app.add_handler(CallbackQueryHandler(report_callback, pattern="^(ban|kick|mute|skip)_"))

    app.job_queue.run_repeating(check_muted_users, interval=30, first=10)
    app.job_queue.run_repeating(clean_report_cooldown, interval=300, first=60)

    app.add_error_handler(error_handler)

    print("Bot is starting...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
