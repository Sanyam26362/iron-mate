import os
import json
import asyncio
from datetime import datetime, timedelta
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes,
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler
import agent
from agent import chat_with_agent, parse_minutes, record_intake, postpone_check
import db

load_dotenv()
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TARGET_CHAT_ID = os.getenv("TARGET_CHAT_ID") or os.getenv("CHAT_ID")
TUNING_MODE = os.getenv("IRONMATE_TUNING") == "1"
OWNER_ID = os.getenv("IRONMATE_OWNER_ID")

conversation_state = {}
scheduler = AsyncIOScheduler()
NUDGE_TRACKER = {}  # reminder_type -> count of nudges sent today


def get_reminder_keyboard() -> InlineKeyboardMarkup:
    """Return low-friction quick reply inline keyboard for reminders."""
    keyboard = [
        [
            InlineKeyboardButton("✅ Took Iron", callback_data="take_iron"),
            InlineKeyboardButton("💊 Took Vitamin", callback_data="take_vitamin"),
        ],
        [
            InlineKeyboardButton("⏰ Snooze 30m", callback_data="snooze_30"),
            InlineKeyboardButton("☕ Just had Chai (Wait 45m)", callback_data="had_chai"),
        ],
    ]
    return InlineKeyboardMarkup(keyboard)


def get_tuning_keyboard() -> InlineKeyboardMarkup:
    """Return small inline keyboard with 👍 / 👎 for tuning mode."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("👍", callback_data="tune_good"),
            InlineKeyboardButton("👎", callback_data="tune_bad"),
        ]
    ])


async def send_snooze_reminder(app, chat_id: int | str, supplement: str = "iron"):
    """Send a personalized follow-up asking how she is doing and checking if she's free to take her supplement."""
    try:
        chat_id = int(chat_id)
        prompt = (
            f"Send a sweet, short follow-up asking how she's doing and checking if she is free to take her {supplement} now."
        )
        res = chat_with_agent(prompt)
        await app.bot.send_message(
            chat_id=chat_id,
            text=res.text,
            reply_markup=get_reminder_keyboard(),
        )
        print(f"[Scheduler] Sent snooze follow-up reminder for {supplement} to chat {chat_id}")
    except Exception as e:
        print(f"[Scheduler Error] Failed to send snooze reminder: {e}")


async def check_reminder_nudge(app, chat_id: int | str, supplement: str = "iron", attempt: int = 1):
    """
    Reminder escalation: if she hasn't logged the supplement within 30 min,
    send at most 2 gentle nudges, respecting snooze/chai periods.
    """
    try:
        chat_id = int(chat_id)
        # Check if intake was logged today
        streak = db.get_streak(supplement)
        key = f"{supplement}_{datetime.now().strftime('%Y-%m-%d')}"
        nudges_sent = NUDGE_TRACKER.get(key, 0)

        if nudges_sent >= 2:
            return

        # Nudge message
        nudge_text = "Medicine le li aapne? ❤️" if supplement == "multivitamin" else "Iron le liya aapne? ❤️"
        await app.bot.send_message(
            chat_id=chat_id,
            text=nudge_text,
            reply_markup=get_reminder_keyboard(),
        )
        NUDGE_TRACKER[key] = nudges_sent + 1
        print(f"[Escalation] Sent nudge #{nudges_sent + 1} for {supplement} to chat {chat_id}")

        # Schedule second nudge if attempt == 1
        if attempt == 1:
            run_date = datetime.now() + timedelta(minutes=30)
            scheduler.add_job(
                check_reminder_nudge,
                "date",
                run_date=run_date,
                args=[app, chat_id, supplement, 2],
            )
    except Exception as e:
        print(f"[Escalation Error] Failed nudge check: {e}")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    global TARGET_CHAT_ID
    if not TARGET_CHAT_ID or TARGET_CHAT_ID == "her_numeric_chat_id_or_leave_empty_for_auto_bind":
        TARGET_CHAT_ID = str(chat_id)
        print(f"Bound to chat ID: {TARGET_CHAT_ID}")

    await update.message.reply_text(
        "IronMate active! Main tumhari iron aur vitamin routines yaad dilane ke liye active hu ❤️",
        reply_markup=get_reminder_keyboard(),
    )


async def last_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show the last turn debug info (for owner only)."""
    user_id = str(update.effective_user.id)
    if OWNER_ID and user_id != str(OWNER_ID):
        await update.message.reply_text("Unauthorized.")
        return

    meta = agent.LAST_TURN_META
    if not meta:
        await update.message.reply_text("No turns recorded yet.")
        return

    text = (
        f"ℹ️ Last Turn Debug Info:\n"
        f"• Message: \"{meta.get('her_message', '')}\"\n"
        f"• Reply: \"{meta.get('reply', '')}\"\n"
        f"• Intent: {meta.get('intent', '')}\n"
        f"• Path: {meta.get('path', '')}\n"
        f"• Model: {meta.get('model', '')}\n"
        f"• Time: {meta.get('ts', '')}"
    )
    await update.message.reply_text(text)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = update.message.text
    chat_id = update.effective_chat.id

    history = conversation_state.get(chat_id, [])
    res = chat_with_agent(user_text, history)
    conversation_state[chat_id] = res.history

    # Wire agent's snooze action directly to APScheduler
    for action in res.actions:
        action_type = action.get("action")
        if action_type == "snooze":
            raw_minutes = action.get("minutes", 30)
            minutes = parse_minutes(raw_minutes)
            supplement = action.get("supplement", "iron")
            run_date = datetime.now() + timedelta(minutes=minutes)
            scheduler.add_job(
                send_snooze_reminder,
                "date",
                run_date=run_date,
                args=[context.application, chat_id, supplement],
            )
            print(f"[Scheduler] Dynamic snooze scheduled for {run_date} ({minutes} mins) for chat {chat_id}")
        elif action_type == "logged":
            print(f"[Intake] Logged intake from message: {action}")

    reply_markup = get_tuning_keyboard() if TUNING_MODE else None
    await update.message.reply_text(res.text, reply_markup=reply_markup)


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle interactive Telegram inline button presses (pure Python, no LLM call)."""
    query = update.callback_query
    data = query.data
    chat_id = update.effective_chat.id
    app = context.application
    user_id = str(query.from_user.id)

    # Tuning mode handlers
    if data == "tune_good":
        if OWNER_ID and user_id != str(OWNER_ID):
            await query.answer("Only owner can rate replies.")
            return
        await query.answer("Marked as good reply 👍")
        return

    if data == "tune_bad":
        if OWNER_ID and user_id != str(OWNER_ID):
            await query.answer("Only owner can rate replies.")
            return

        bad_entry = {
            "her_message": agent.LAST_TURN_META.get("her_message", ""),
            "reply": agent.LAST_TURN_META.get("reply", ""),
            "intent": agent.LAST_TURN_META.get("intent", ""),
            "path": agent.LAST_TURN_META.get("path", ""),
            "model": agent.LAST_TURN_META.get("model", ""),
            "ts": agent.LAST_TURN_META.get("ts", datetime.now().isoformat()),
        }
        filepath = os.path.join(os.path.dirname(__file__), "bad_replies.jsonl")
        try:
            with open(filepath, "a", encoding="utf-8") as f:
                f.write(json.dumps(bad_entry, ensure_ascii=False) + "\n")
            await query.answer("Logged to bad_replies.jsonl 👎")
        except Exception as e:
            await query.answer(f"Logging error: {e}")
        return

    await query.answer()

    if data == "take_iron":
        record_intake("iron", "taken", "via Telegram quick button")
        streak = db.get_streak("iron")
        response_text = (
            f"Good job bb! Logged your iron pill. Streak is at {streak} day{'s' if streak != 1 else ''}! Proud of you 🔥"
        )
        await query.message.reply_text(response_text)
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass

    elif data == "take_vitamin":
        record_intake("multivitamin", "taken", "via Telegram quick button")
        streak = db.get_streak("multivitamin")
        response_text = (
            f"Yay multivitamin sorted! Streak is at {streak} day{'s' if streak != 1 else ''} now. Keep shining ✨"
        )
        await query.message.reply_text(response_text)
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass

    elif data == "snooze_30":
        postpone_check(minutes=30, reason="via Telegram quick button")
        run_date = datetime.now() + timedelta(minutes=30)
        scheduler.add_job(
            send_snooze_reminder,
            "date",
            run_date=run_date,
            args=[app, chat_id, "iron"],
        )
        response_text = "No worries bb, snoozed for 30 mins! I'll ping you then ⏰"
        await query.message.reply_text(response_text)
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass

    elif data == "had_chai":
        postpone_check(minutes=45, reason="had chai")
        run_date = datetime.now() + timedelta(minutes=45)
        scheduler.add_job(
            send_snooze_reminder,
            "date",
            run_date=run_date,
            args=[app, chat_id, "iron"],
        )
        response_text = (
            "Chai peeli? Acha sun, chai and coffee contain tannins that block iron absorption! "
            "Please wait 45 minutes before taking your iron pill. I'll remind you then! ☕🚫💊"
        )
        await query.message.reply_text(response_text)
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass


async def morning_reminder(app):
    if TARGET_CHAT_ID:
        prompt = "Send a short morning reminder for her multivitamin after breakfast."
        res = chat_with_agent(prompt)
        await app.bot.send_message(
            chat_id=int(TARGET_CHAT_ID),
            text=res.text,
            reply_markup=get_reminder_keyboard(),
        )
        # Schedule gentle nudge escalation in 30 min
        run_date = datetime.now() + timedelta(minutes=30)
        scheduler.add_job(
            check_reminder_nudge,
            "date",
            run_date=run_date,
            args=[app, TARGET_CHAT_ID, "multivitamin", 1],
        )


async def evening_reminder(app):
    if TARGET_CHAT_ID:
        prompt = "Send an evening reminder for her iron tablet (remind no chai/milk)."
        res = chat_with_agent(prompt)
        await app.bot.send_message(
            chat_id=int(TARGET_CHAT_ID),
            text=res.text,
            reply_markup=get_reminder_keyboard(),
        )
        # Schedule gentle nudge escalation in 30 min
        run_date = datetime.now() + timedelta(minutes=30)
        scheduler.add_job(
            check_reminder_nudge,
            "date",
            run_date=run_date,
            args=[app, TARGET_CHAT_ID, "iron", 1],
        )


async def post_init(application):
    """Lifecycle hook executed after application is initialized inside the asyncio event loop."""
    if not scheduler.get_job("morning_reminder"):
        scheduler.add_job(
            lambda: asyncio.create_task(morning_reminder(application)),
            "cron",
            hour=10,
            minute=0,
            id="morning_reminder",
            replace_existing=True,
        )
    if not scheduler.get_job("evening_reminder"):
        scheduler.add_job(
            lambda: asyncio.create_task(evening_reminder(application)),
            "cron",
            hour=20,
            minute=30,
            id="evening_reminder",
            replace_existing=True,
        )

    if not scheduler.running:
        scheduler.start()
        print("APScheduler active inside event loop.")


async def post_shutdown(application):
    """Lifecycle hook executed on application shutdown."""
    if scheduler.running:
        scheduler.shutdown(wait=False)
        print("APScheduler cleanly stopped.")


def main():
    app = (
        ApplicationBuilder()
        .token(TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("last", last_command))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_message))

    print("IronMate Telegram bot is online.")
    app.run_polling()


if __name__ == "__main__":
    main()