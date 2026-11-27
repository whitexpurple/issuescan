"""Private, administrator-only Telegram setup for Discord."""

import asyncio
import os
import time
import logging
from collections import Counter, defaultdict
from datetime import datetime, timezone

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import TelegramError
from telegram.ext import CommandHandler, CallbackQueryHandler, MessageHandler, filters

from discord_session import DiscordConnectionError, login_request
from delivery import chunks
from database import get_connection

SETUP_TTL = 300
logger = logging.getLogger(__name__)
def is_authorized(update):
    try:
        admin = int(os.getenv("TELEGRAM_CHAT_ID", ""))
    except ValueError:
        return False
    return bool(update.effective_chat and update.effective_user
                and update.effective_chat.type == "private"
                and update.effective_chat.id == admin
                and update.effective_user.id == admin)


def clear_setup(context):
    state = context.user_data.pop("discord_setup", None)
    if state:
        state.clear()
    task = context.user_data.pop("setup_expiry", None)
    if task:
        task.cancel()


def begin_setup(context, step="email"):
    clear_setup(context)
    state = {"step": step, "expires": time.monotonic() + SETUP_TTL}
    context.user_data["discord_setup"] = state

    async def expire():
        await asyncio.sleep(SETUP_TTL)
        if context.user_data.get("discord_setup") is state:
            context.user_data.pop("discord_setup", None)
            context.user_data.pop("setup_expiry", None)
            state.clear()
            logger.info("Discord setup expired; send /connect to restart.")

    context.user_data["setup_expiry"] = asyncio.create_task(expire())


async def start(update, context):
    if not is_authorized(update):
        return
    clear_setup(context)
    await update.effective_message.reply_text(
        "IssueScan\n\nLive support-request matching across connected Discord accounts.",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("Connect Discord", callback_data="connect")],
            [InlineKeyboardButton("Add Discord token", callback_data="addtoken")],
            [InlineKeyboardButton("Discord accounts", callback_data="accounts")],
            [InlineKeyboardButton("Matching rules", callback_data="rules")],
            [InlineKeyboardButton("Activity report", callback_data="report")],
            [InlineKeyboardButton("Connection status", callback_data="status")],
            [InlineKeyboardButton("My servers", callback_data="servers")],
        ]),
    )


async def connect(update, context):
    logger.info("Telegram /connect received.")
    if not is_authorized(update):
        logger.warning("Discord setup denied: use the private Telegram account matching TELEGRAM_CHAT_ID.")
        return
    begin_setup(context)
    logger.info("Discord setup started: waiting for email.")
    await update.effective_message.reply_text(
        "Connect Discord\n\nSend your Discord email address. "
        "Next I will ask for your password and, if needed, a two-factor code. "
        "I will attempt to delete those messages; email/password are not saved to disk. "
        "The session token is stored in the restricted local account store, not .env.\n\n"
        "Setup expires in 5 minutes. Use /cancel to stop. Add token instead: /addtoken."
    )


async def addtoken(update, context):
    logger.info("Telegram /addtoken received.")
    if not is_authorized(update):
        logger.warning("Discord token setup denied: private administrator chat required.")
        return
    begin_setup(context, "token")
    await update.effective_message.reply_text(
        "Add Discord account token\n\nSend the token as your next message. "
        "IssueScan will delete that message, validate the token, and save the account "
        "in the protected local account store, not .env. Setup expires in 5 minutes. Use /cancel to stop."
    )


async def cancel(update, context):
    if not is_authorized(update):
        return
    clear_setup(context)
    logger.info("Discord setup cancelled by administrator.")
    await update.effective_message.reply_text("Setup cancelled. Your current connection is unchanged.")


async def reconnect(update, context):
    if not is_authorized(update):
        return
    clear_setup(context)
    manager = context.application.bot_data["discord_session"]
    disconnected = [item for item in manager.summaries() if not item["connected"]]
    if not disconnected:
        await update.effective_message.reply_text("All saved Discord accounts are connected.")
        return
    results = []
    for account in disconnected:
        try:
            _, username = await manager.reconnect(account["id"])
            results.append(f"Connected: {username}")
        except DiscordConnectionError as error:
            results.append(f"Failed: {account['username']} ({error})")
    await update.effective_message.reply_text("\n".join(results))


async def status(update, context):
    if not is_authorized(update):
        return
    session = context.application.bot_data["discord_session"]
    if hasattr(session, "summaries"):
        accounts = session.summaries()
    else:
        client = session.client
        accounts = ([{"id": str((client.user or {}).get("id", "unknown")),
                      "username": (client.user or {}).get("username", "Discord account"),
                      "session": session, "guilds": client.guilds_list,
                      "connected": client.ready.is_set()}] if client else [])
    if not accounts:
        text = "No Discord accounts are connected. Use /connect to add one."
    else:
        lines = [f"Discord accounts: {len(accounts)}"]
        for account in accounts:
            lines.append(f"\n{account['username']} ({account['id']})"
                         f"\nGateway: {'connected' if account['connected'] else 'disconnected'}"
                         f"\nServers: {len(account['guilds'])}")
            monitor = account["session"].support_monitor if account["session"] else None
            if monitor:
                lines.append(f"Messages checked live: {monitor.scanned_messages}; support alerts: {monitor.alert_count}")
        text = "\n".join(lines)
    if session.connection_error:
        text += f"\nGateway error: {session.connection_error}"
    if session.events:
        text += f"\nPending Discord events: {session.events.pending_count()}"
        if session.events.last_error:
            text += f"\nEvent processing error: {session.events.last_error} (will retry)"
    outbox = context.application.bot_data.get("notification_outbox")
    if outbox:
        text += f"\nPending notifications: {outbox.pending_count()}"
        if outbox.last_error:
            text += f"\nDelivery error: {outbox.last_error} (will retry)"
        if outbox.last_success:
            text += "\nLast delivery: " + datetime.fromtimestamp(outbox.last_success, timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    await update.effective_message.reply_text(text)


async def rules(update, context):
    if not is_authorized(update):
        return
    conn = get_connection()
    try:
        threshold = conn.execute("SELECT value FROM support_settings WHERE setting='threshold'").fetchone()[0]
        rows = conn.execute("SELECT id,phrase,weight,exclusion,enabled,category FROM support_rules ORDER BY id").fetchall()
    finally:
        conn.close()
    lines = [f"Support matching threshold: {threshold}",
             "Rules (one strongest weight per category; resolution lowers the total):"]
    lines.extend(f"{row[0]}. {'EXCLUDE' if row[3] else row[5].upper()} · {row[1]} · {row[2]} · {'ON' if row[4] else 'OFF'}" for row in rows)
    lines.extend(["", "/ruleadd <category> <weight> <phrase>",
                  "Categories: topic, problem, help, detail, resolution",
                  "/ruleadd <weight> <phrase> (defaults to topic)",
                  "/ruleexclude <phrase>", "/ruledel <id>",
                  "/threshold <score>", "/lookback [hours] (explicit scan of recent messages)"])
    await update.effective_message.reply_text("\n".join(lines))


async def ruleadd(update, context):
    if not is_authorized(update):
        return
    args = context.args or []
    categories = {"topic", "problem", "help", "detail", "resolution"}
    try:
        if args and args[0].lower() in categories:
            if len(args) < 3:
                raise ValueError
            category = args[0].lower()
            weight = int(args[1])
            phrase = " ".join(args[2:]).strip()
        else:
            category = "topic"
            weight = int(args[0])
            phrase = " ".join(args[1:]).strip()
        if not phrase or not 1 <= weight <= 20 or len(phrase) > 100:
            raise ValueError
    except (ValueError, IndexError):
        await update.effective_message.reply_text(
            "Usage: /ruleadd <topic|problem|help|detail|resolution> <weight 1-20> <phrase>")
        return
    conn = get_connection()
    try:
        with conn:
            conn.execute("INSERT INTO support_rules(phrase,weight,exclusion,category) VALUES (?,?,0,?) "
                         "ON CONFLICT(phrase) DO UPDATE SET weight=excluded.weight,exclusion=0,"
                         "category=excluded.category,enabled=1", (phrase, weight, category))
    finally:
        conn.close()
    await update.effective_message.reply_text(
        f"{category.title()} rule saved: {phrase} (weight {weight}).")


async def ruleexclude(update, context):
    if not is_authorized(update):
        return
    phrase = " ".join(context.args or []).strip()
    if not phrase or len(phrase) > 100:
        await update.effective_message.reply_text("Usage: /ruleexclude <phrase>")
        return
    conn = get_connection()
    try:
        with conn:
            conn.execute("INSERT INTO support_rules(phrase,weight,exclusion) VALUES (?,0,1) "
                         "ON CONFLICT(phrase) DO UPDATE SET weight=0,exclusion=1,enabled=1", (phrase,))
    finally:
        conn.close()
    await update.effective_message.reply_text(f"Exclusion saved: messages containing ‘{phrase}’ will not alert.")


async def ruledel(update, context):
    if not is_authorized(update):
        return
    try:
        rule_id = int((context.args or [""])[0])
    except ValueError:
        await update.effective_message.reply_text("Usage: /ruledel <rule id>")
        return
    conn = get_connection()
    try:
        with conn:
            cursor = conn.execute("DELETE FROM support_rules WHERE id=?", (rule_id,))
    finally:
        conn.close()
    await update.effective_message.reply_text("Rule deleted." if cursor.rowcount else "No rule has that ID.")


async def threshold(update, context):
    if not is_authorized(update):
        return
    try:
        value = int((context.args or [""])[0])
        if not 1 <= value <= 100:
            raise ValueError
    except ValueError:
        await update.effective_message.reply_text("Usage: /threshold <score 1-100>")
        return
    conn = get_connection()
    try:
        with conn:
            conn.execute("INSERT INTO support_settings(setting,value) VALUES ('threshold',?) "
                         "ON CONFLICT(setting) DO UPDATE SET value=excluded.value", (str(value),))
    finally:
        conn.close()
    await update.effective_message.reply_text(f"Support alert threshold set to {value}.")


async def lookback(update, context):
    if not is_authorized(update):
        return
    try:
        hours = int((context.args or ["6"])[0])
        if not 1 <= hours <= 24:
            raise ValueError
    except ValueError:
        await update.effective_message.reply_text("Usage: /lookback [hours 1-24]")
        return
    accounts = context.application.bot_data["discord_session"].summaries()
    checked = found = failed = 0
    for account in accounts:
        monitor = account["session"].support_monitor if account["session"] else None
        if monitor:
            counts = await monitor.lookback(hours)
            checked += counts[0]; found += counts[1]; failed += counts[2]
    await update.effective_message.reply_text(
        f"Lookback finished ({hours}h): checked {checked} messages, queued {found} alerts; {failed} server channel lists unavailable.")


async def report(update, context):
    if not is_authorized(update):
        return
    try:
        hours = int((context.args or ["24"])[0])
        if not 1 <= hours <= 168:
            raise ValueError
    except ValueError:
        await update.effective_message.reply_text("Usage: /report [hours 1-168]")
        return

    cutoff = time.time() - hours * 3600
    bucket_cutoff = int(cutoff // 300) * 300
    conn = get_connection()
    try:
        activity = conn.execute("""SELECT guild_id,SUM(message_count),MAX(last_message_at)
            FROM support_activity WHERE bucket_start>=? GROUP BY guild_id ORDER BY SUM(message_count) DESC""",
            (bucket_cutoff,)).fetchall()
        alerts = conn.execute("""SELECT guild_id,guild_name,channel_id,channel_name,author_id,
            author_name,score,matched,excerpt,message_id,message_at,issue_type FROM support_alerts
            WHERE message_at>=? ORDER BY message_at DESC LIMIT 8""", (cutoff,)).fetchall()
        issue_counts = dict(conn.execute("SELECT guild_id,COUNT(*) FROM support_alerts "
                                         "WHERE message_at>=? GROUP BY guild_id", (cutoff,)).fetchall())
        feedback_counts = dict(conn.execute("SELECT feedback,COUNT(*) FROM support_feedback "
                                            "WHERE created_at>=? GROUP BY feedback", (cutoff,)).fetchall())
        with conn:
            conn.execute("DELETE FROM support_activity WHERE bucket_start<?", (int((time.time() - 30 * 86400) // 300) * 300,))
            conn.execute("DELETE FROM support_scanned_messages WHERE scanned_at<?", (time.time() - 30 * 86400,))
            conn.execute("DELETE FROM support_alerts WHERE message_at<?", (time.time() - 30 * 86400,))
            conn.execute("DELETE FROM support_feedback WHERE created_at<?", (time.time() - 30 * 86400,))
    finally:
        conn.close()

    guild_names = {}
    manager = context.application.bot_data.get("discord_session")
    if manager and hasattr(manager, "summaries"):
        for account in manager.summaries():
            for guild in account["guilds"]:
                guild_names[str(guild.get("id"))] = guild.get("name") or str(guild.get("id"))

    total_messages = sum(row[1] or 0 for row in activity)
    total_issues = sum(issue_counts.values())
    lines = [f"IssueScan activity report · last {hours} hours",
             f"Messages checked: {total_messages} · Active servers: {len(activity)} · Likely issues: {total_issues}",
             f"Alert feedback: {feedback_counts.get('useful', 0)} useful · {feedback_counts.get('noise', 0)} not an issue"]
    if activity:
        lines.append("\nActive servers:")
        for guild_id, count, _last_seen in activity[:20]:
            name = guild_names.get(str(guild_id), f"Server {guild_id}")
            lines.append(f"• {name}: {count} messages · {issue_counts.get(guild_id, 0)} likely issue(s)")
        if len(activity) > 20:
            lines.append(f"…and {len(activity) - 20} more active server(s).")
    else:
        lines.append("\nNo readable server messages were processed in this period.")
    if alerts:
        lines.append("\nRecent likely issues:")
        for row in alerts:
            guild_id, saved_guild, channel_id, channel_name, author_id, author_name, score, matched, excerpt, message_id, _message_at, issue_type = row
            guild_name = guild_names.get(str(guild_id), saved_guild or f"Server {guild_id}")
            link = f"https://discord.com/channels/{guild_id}/{channel_id}/{message_id}"
            lines.append(f"\n• {guild_name} · #{channel_name or channel_id} · "
                         f"{author_name or author_id} · score {score}\n"
                         f"Type: {issue_type.title()}\n"
                         f"Keywords detected: {matched}\n{excerpt}\n{link}")
    else:
        lines.append("\nNo likely support requests matched the current rules.")

    for part in chunks("\n".join(lines)):
        await update.effective_message.reply_text(part, disable_web_page_preview=True)


async def feedback(update, context):
    if not is_authorized(update):
        return
    try:
        limit = int((context.args or ["10"])[0])
        if not 1 <= limit <= 20:
            raise ValueError
    except ValueError:
        await update.effective_message.reply_text("Usage: /feedback [1-20]")
        return
    conn = get_connection()
    try:
        rows = conn.execute("""SELECT f.feedback,a.guild_name,a.channel_name,a.author_name,
            a.issue_type,a.score,a.matched,a.excerpt FROM support_feedback f
            JOIN support_alerts a ON a.message_id=f.message_id
            ORDER BY f.created_at DESC LIMIT ?""", (limit,)).fetchall()
        labeled_matches = conn.execute("""SELECT f.feedback,a.matched FROM support_feedback f
            JOIN support_alerts a ON a.message_id=f.message_id
            ORDER BY f.created_at DESC LIMIT 500""").fetchall()
    finally:
        conn.close()
    if not rows:
        await update.effective_message.reply_text("No alert feedback yet. New alerts include Useful issue and Not an issue buttons.")
        return
    noisy = Counter(phrase.strip() for label, matches in labeled_matches if label == "noise"
                    for phrase in (matches or "").split(",") if phrase.strip())
    useful = Counter(phrase.strip() for label, matches in labeled_matches if label == "useful"
                     for phrase in (matches or "").split(",") if phrase.strip())
    lines = ["Recent alert feedback:"]
    if noisy:
        lines.append("Most common phrases in ‘Not an issue’ labels: " +
                     ", ".join(f"{phrase} ({count})" for phrase, count in noisy.most_common(5)))
    if useful:
        lines.append("Most common phrases in ‘Useful issue’ labels: " +
                     ", ".join(f"{phrase} ({count})" for phrase, count in useful.most_common(5)))
    for label, guild, channel, author, issue_type, score, matched, excerpt in rows:
        lines.append(f"\n• {label.replace('_', ' ').title()} · {guild} · #{channel} · {author}")
        lines.append(f"Type: {issue_type.title()} · score {score} · matched: {matched}")
        lines.append(excerpt)
    for part in chunks("\n".join(lines)):
        await update.effective_message.reply_text(part, disable_web_page_preview=True)


def server_page(client, page=0):
    guilds = sorted(client.guilds_list, key=lambda guild: (
        str(guild.get("name") or "Unknown server").casefold(), str(guild.get("id", ""))))
    total = len(guilds)
    page_size = 15
    pages = max(1, (total + page_size - 1) // page_size)
    page = min(max(0, page), pages - 1)
    lines = [f"My Discord servers: {total}"]
    if not client.ready.is_set():
        lines.append("Discord is reconnecting; showing the cached list.")
    if not guilds:
        lines.append("No servers are currently available in the account's server list.")
    for number, guild in enumerate(guilds[page * page_size:(page + 1) * page_size], page * page_size + 1):
        name = " ".join(str(guild.get("name") or "Unknown server").split())[:100]
        guild_id = str(guild.get("id", "Unknown"))[:20]
        unavailable = " (unavailable)" if guild.get("unavailable") else ""
        lines.append(f"\n{number}. {name}{unavailable}\nID: {guild_id}")
    buttons = []
    if page > 0:
        buttons.append(InlineKeyboardButton("Previous", callback_data=f"servers:{page - 1}"))
    if page + 1 < pages:
        buttons.append(InlineKeyboardButton("Next", callback_data=f"servers:{page + 1}"))
    if pages > 1:
        lines.append(f"\nPage {page + 1} of {pages}")
    keyboard = [buttons] if buttons else []
    keyboard.append([InlineKeyboardButton("Close", callback_data="servers:close")])
    return "\n".join(lines), InlineKeyboardMarkup(keyboard)


async def servers(update, context, page=0):
    if not is_authorized(update):
        return
    session = context.application.bot_data.get("discord_session")
    if session and not hasattr(session, "summaries"):
        client = session.client
        if client is None:
            text = "Discord is disconnected. Use /connect to sign in."
            markup = InlineKeyboardMarkup([[InlineKeyboardButton("Close", callback_data="servers:close")]])
        else:
            text, markup = server_page(client, page)
        await context.application.bot_data["server_lists"].show(update, text, markup)
        return
    accounts = session.summaries() if session else []
    aggregate = {}
    for account in accounts:
        for guild in account["guilds"]:
            item = aggregate.setdefault(str(guild.get("id")), {"guild": guild, "accounts": []})
            item["accounts"].append(account["username"])
    entries = sorted(aggregate.values(), key=lambda item: str(item["guild"].get("name", "")).casefold())
    text, markup = server_entries_page(entries, page, "All Discord servers")
    await context.application.bot_data["server_lists"].show(update, text, markup)


def server_entries_page(entries, page, title, callback_prefix="servers"):
    page_size = 15
    total = len(entries)
    pages = max(1, (total + page_size - 1) // page_size)
    page = min(max(0, page), pages - 1)
    lines = [f"{title}: {total}"]
    if not total:
        lines.append("No servers are currently available.")
    for index, item in enumerate(entries[page * page_size:(page + 1) * page_size], page * page_size + 1):
        guild = item["guild"]
        name = " ".join(str(guild.get("name") or "Unknown server").split())[:100]
        lines.append(f"\n{index}. {name}\nID: {guild.get('id', 'Unknown')}\nAccount: {', '.join(item['accounts'])}")
    buttons = []
    if page:
        buttons.append(InlineKeyboardButton("Previous", callback_data=f"{callback_prefix}:{page - 1}"))
    if page + 1 < pages:
        buttons.append(InlineKeyboardButton("Next", callback_data=f"{callback_prefix}:{page + 1}"))
    keyboard = [buttons] if buttons else []
    if pages > 1:
        lines.append(f"\nPage {page + 1} of {pages}")
    keyboard.append([InlineKeyboardButton("Close", callback_data="servers:close")])
    return "\n".join(lines), InlineKeyboardMarkup(keyboard)


async def accounts(update, context):
    if not is_authorized(update):
        return
    items = context.application.bot_data["discord_session"].summaries()
    lines = [f"Discord accounts: {len(items)}"]
    keyboard = [[InlineKeyboardButton("Add account", callback_data="connect")]]
    for item in items:
        state = "connected" if item["connected"] else "disconnected"
        lines.append(f"\n{item['username']} · {state} · {len(item['guilds'])} servers\nID: {item['id']}")
        keyboard.append([InlineKeyboardButton(f"Remove {item['username'][:30]}",
                                               callback_data=f"account:remove:{item['id']}")])
    keyboard.append([InlineKeyboardButton("Close", callback_data="accounts:close")])
    await context.application.bot_data["server_lists"].show(update, "\n".join(lines), InlineKeyboardMarkup(keyboard))


async def server_account_picker(update, context):
    if not is_authorized(update):
        return
    items = context.application.bot_data["discord_session"].summaries()
    keyboard = [[InlineKeyboardButton(item["username"][:50], callback_data=f"server-account:{item['id']}")]
                for item in items]
    keyboard.append([InlineKeyboardButton("Close", callback_data="servers:close")])
    await context.application.bot_data["server_lists"].show(
        update, "Choose a Discord account to view its servers:", InlineKeyboardMarkup(keyboard))


async def button_handler(update, context):
    action = update.callback_query.data
    if not is_authorized(update):
        await update.callback_query.answer()
        if action == "connect":
            logger.warning("Telegram Connect Discord button denied: private administrator chat required.")
        return
    if not action.startswith("feedback:"):
        await update.callback_query.answer()
    if action.startswith("feedback:"):
        parts = action.split(":", 2)
        if len(parts) != 3 or parts[1] not in ("useful", "noise"):
            return
        _, value, message_id = parts
        conn = get_connection()
        try:
            with conn:
                exists = conn.execute("SELECT 1 FROM support_alerts WHERE message_id=?", (message_id,)).fetchone()
                if not exists:
                    await update.callback_query.answer("This alert is no longer available.", show_alert=True)
                    return
                conn.execute("""INSERT INTO support_feedback(message_id,feedback,created_at) VALUES(?,?,?)
                    ON CONFLICT(message_id) DO UPDATE SET feedback=excluded.feedback,created_at=excluded.created_at""",
                    (message_id, value, time.time()))
        finally:
            conn.close()
        await update.callback_query.answer("Feedback saved. Thanks!")
        await update.callback_query.edit_message_reply_markup(reply_markup=None)
    elif action == "connect":
        await connect(update, context)
    elif action == "addtoken":
        await addtoken(update, context)
    elif action == "accounts":
        await accounts(update, context)
    elif action == "accounts:close":
        message = update.effective_message
        await context.application.bot_data["server_lists"].close(message.chat_id, message.message_id)
    elif action == "rules":
        await rules(update, context)
    elif action == "report":
        await report(update, context)
    elif action.startswith("account:remove:"):
        account_id = action.rsplit(":", 1)[-1]
        record = next((item for item in context.application.bot_data["discord_session"].summaries()
                       if item["id"] == account_id), None)
        if not record:
            await update.effective_message.reply_text("That account is no longer connected.")
            return
        await update.effective_message.reply_text(
            f"Remove {record['username']}? This disconnects it and deletes its saved token. "
            "It does not leave servers. Support alert history stays in the database.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("Confirm removal", callback_data=f"account:confirm-remove:{account_id}"),
                InlineKeyboardButton("Cancel", callback_data="accounts"),
            ]]))
    elif action.startswith("account:confirm-remove:"):
        account_id = action.rsplit(":", 1)[-1]
        manager = context.application.bot_data["discord_session"]
        record = next((item for item in manager.summaries() if item["id"] == account_id), None)
        username = record["username"] if record else "Discord account"
        removed = await manager.remove(account_id)
        await update.effective_message.edit_text(
            f"Removed {username}. Support alert history was retained." if removed
            else "That account was already removed.")
    elif action == "status":
        await status(update, context)
    elif action == "servers:close":
        message = update.effective_message
        closed = await context.application.bot_data["server_lists"].close(message.chat_id, message.message_id)
        if not closed:
            await message.reply_text("Could not close the list yet. I will retry automatically.")
    elif action == "servers" or action.startswith("servers:"):
        try:
            page = int(action.split(":", 1)[1]) if ":" in action else 0
        except ValueError:
            page = 0
        await servers(update, context, page)
    elif action == "server-picker":
        await server_account_picker(update, context)
    elif action.startswith("server-page:"):
        _, account_id, page_value = (action.split(":", 2) + ["0"])[:3]
        try:
            page = int(page_value)
        except ValueError:
            page = 0
        account = next((item for item in context.application.bot_data["discord_session"].summaries()
                        if item["id"] == account_id), None)
        if not account:
            await update.effective_message.reply_text("That account is no longer available.")
            return
        entries = [{"guild": guild, "accounts": [account["username"]]} for guild in account["guilds"]]
        text, markup = server_entries_page(
            entries, page, f"{account['username']} servers", f"server-page:{account_id}"
        )
        await context.application.bot_data["server_lists"].show(update, text, markup)
    elif action.startswith("server-account:"):
        account_id = action.split(":", 1)[1]
        account = next((item for item in context.application.bot_data["discord_session"].summaries()
                        if item["id"] == account_id), None)
        if not account:
            await update.effective_message.reply_text("That account is no longer available.")
            return
        entries = [{"guild": guild, "accounts": [account["username"]]} for guild in account["guilds"]]
        text, markup = server_entries_page(
            entries, 0, f"{account['username']} servers", f"server-page:{account_id}"
        )
        await context.application.bot_data["server_lists"].show(update, text, markup)
    else:
        await update.effective_message.reply_text("Manual category setup is no longer needed. Use /start.")


async def setup_message(update, context):
    if not is_authorized(update):
        return
    state = context.user_data.get("discord_setup")
    if not state:
        logger.info("Telegram setup message received without an active login; send /connect first.")
        await update.effective_message.reply_text("Use /connect to sign in or /status to check live monitoring.")
        return
    value = update.effective_message.text or ""
    step = state["step"]
    try:
        await update.effective_message.delete()
    except TelegramError:
        logger.warning("Could not delete a Telegram setup message.")
        if step == "token":
            clear_setup(context)
            await update.effective_message.reply_text(
                "I could not delete that message, so I did not use the token. Delete it manually and retry /addtoken."
            )
            return
        await update.effective_message.reply_text("I could not delete your setup message. Delete it from this chat.")
    if not state or time.monotonic() >= state["expires"]:
        clear_setup(context)
        await update.effective_message.reply_text("Setup expired. Use /connect again.")
        return
    logger.info("Discord setup input received: stage=%s (content omitted).", step)
    if step == "token":
        token = value.strip()
        if not token or len(token) > 2048 or any(character.isspace() for character in token):
            await update.effective_message.reply_text("That token format is not valid. Send the token again or /cancel.")
            return
        clear_setup(context)
        manager = context.application.bot_data["discord_session"]
        await update.effective_message.reply_text("Validating the Discord token...")
        try:
            if hasattr(manager, "add"):
                _account_id, username = await manager.add(token)
            else:
                username = await manager.connect(token)
        except DiscordConnectionError as error:
            logger.warning("Discord token setup failed: %s", error)
            await update.effective_message.reply_text(
                "The token could not be connected or saved. Check that it is valid, then use /addtoken again."
            )
            return
        except Exception as error:
            logger.error("Discord token setup failed: exception_type=%s", type(error).__name__)
            await update.effective_message.reply_text(
                "The token could not be connected. Check it and use /addtoken again."
            )
            return
        finally:
            token = ""
            value = ""
        await update.effective_message.reply_text(
            f"Added {username}. Live support matching is active across its readable servers."
        )
        return
    if step == "email":
        if "@" not in value or len(value) > 320:
            logger.info("Discord setup email format rejected; waiting for another email.")
            await update.effective_message.reply_text("Send a valid email address or /cancel.")
            return
        state.update(step="password", email=value.strip())
        logger.info("Discord setup: waiting for password.")
        await update.effective_message.reply_text("Send your Discord password.")
        return
    try:
        if step == "password":
            result = await login_request("login", {"login": state["email"], "password": value,
                                                    "undelete": False})
        elif step == "mfa":
            code = value.strip().replace("-", "")
            if not (len(code) == 6 and code.isdigit()) and not (len(code) == 8 and code.isalnum()):
                logger.info("Discord setup verification code format rejected; waiting for another code.")
                await update.effective_message.reply_text("Send a six-digit authenticator code or an eight-character backup code.")
                return
            method = "totp" if len(code) == 6 and code.isdigit() else "backup"
            result = await login_request(f"mfa/{method}", {"ticket": state["ticket"], "code": code})
        else:
            return
        if not state or time.monotonic() >= state["expires"]:
            clear_setup(context)
            await update.effective_message.reply_text("Setup expired. Use /connect again.")
            return
        if result.get("ticket"):
            state.pop("email", None)
            state.update(step="mfa", ticket=result["ticket"])
            logger.info("Discord setup: waiting for two-factor or backup code.")
            await update.effective_message.reply_text("Send your authenticator code or Discord backup code.")
            return
        if result.get("error"):
            logger.warning("Discord setup request unsuccessful: stage=%s retry_allowed=%s.",
                           step, bool(result.get("retry")))
            if not result.get("retry"):
                clear_setup(context)
            await update.effective_message.reply_text(result["error"])
            return
        clear_setup(context)
        await update.effective_message.reply_text("Signing in to Discord...")
        manager = context.application.bot_data["discord_session"]
        if hasattr(manager, "add"):
            _account_id, username = await manager.add(result["token"])
        else:
            username = await manager.connect(result["token"])
        result.clear()
        await update.effective_message.reply_text(
            f"Added {username}. Live support matching is active across its readable servers."
        )
    except DiscordConnectionError as error:
        logger.warning("Discord setup connection failed: %s", error)
        clear_setup(context)
        await update.effective_message.reply_text(str(error) + " Existing accounts remain unchanged. Use /accounts to review them.")
    except Exception as error:
        logger.error("Discord setup failed: stage=%s exception_type=%s", step, type(error).__name__)
        clear_setup(context)
        await update.effective_message.reply_text(
            "Discord setup could not finish. Check your login and connection, then use /connect again. "
            "Existing accounts remain unchanged."
        )


async def telegram_error_handler(update, context):
    # Exception strings and Update objects can contain credentials or token URLs.
    logger.error("Telegram update/polling failed: exception_type=%s (details omitted).",
                 type(context.error).__name__)


def register_handlers(application):
    application.add_error_handler(telegram_error_handler)
    for command, handler in (("start", start), ("connect", connect), ("addtoken", addtoken),
                             ("reconnect", reconnect),
                             ("cancel", cancel), ("status", status), ("report", report), ("feedback", feedback),
                             ("rules", rules), ("ruleadd", ruleadd), ("ruleexclude", ruleexclude),
                             ("ruledel", ruledel), ("threshold", threshold), ("lookback", lookback),
                             ("servers", servers), ("accounts", accounts),
                             ("server", server_account_picker)):
        application.add_handler(CommandHandler(command, handler))
    application.add_handler(CallbackQueryHandler(button_handler))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, setup_message))
