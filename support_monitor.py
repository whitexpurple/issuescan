"""Live-first support request matching and compact alert persistence."""

import asyncio
import logging
import re
import time

from database import get_connection
from delivery import TelegramNotification

logger = logging.getLogger("support-monitor")
MAX_EXCERPT = 280


class SupportMonitor:
    def __init__(self, client, outbox, account_name="Discord account"):
        self.client, self.outbox, self.account_name = client, outbox, account_name
        self.last_error = None
        self.scanned_messages = 0
        self.alert_count = 0

    @staticmethod
    def rules(conn=None):
        owned = conn is None
        conn = conn or get_connection()
        try:
            rules = conn.execute("SELECT id,phrase,weight,exclusion FROM support_rules WHERE enabled=1 ORDER BY id").fetchall()
            threshold = int((conn.execute("SELECT value FROM support_settings WHERE setting='threshold'").fetchone() or ("3",))[0])
            return rules, threshold
        finally:
            if owned:
                conn.close()

    @staticmethod
    def match(content, rules, threshold):
        folded = content.casefold()
        matches = [(row[0], row[1], row[2], row[3]) for row in rules
                   if row[1].casefold() in folded]
        if any(exclusion for _rule_id, _phrase, _weight, exclusion in matches):
            return 0, []
        score = sum(weight for _rule_id, _phrase, weight, _exclusion in matches)
        return (score, matches) if score >= threshold else (score, [])

    def message_link(self, guild_id, channel_id, message_id):
        return f"https://discord.com/channels/{guild_id}/{channel_id}/{message_id}"

    async def message_created(self, message):
        if not message.get("guild_id") or not message.get("id") or not message.get("channel_id"):
            return False
        author = message.get("author") or {}
        content = str(message.get("content") or "").strip()
        conn = get_connection()
        try:
            account_id = str((self.client.user or {}).get("id", ""))
            guild_id, channel_id = str(message["guild_id"]), str(message["channel_id"])
            now = time.time()
            try:
                message_at = ((int(message["id"]) >> 22) + 1420070400000) / 1000
            except (TypeError, ValueError):
                message_at = now
            bucket = int(message_at // 300) * 300
            with conn:
                cursor = conn.execute("INSERT OR IGNORE INTO support_scanned_messages VALUES (?,?,?,?)",
                                      (str(message["id"]), account_id, guild_id, now))
                if cursor.rowcount:
                    self.scanned_messages += 1
                    conn.execute("""INSERT INTO support_activity
                        (account_id,guild_id,bucket_start,message_count,last_message_at)
                        VALUES (?,?,?,1,?) ON CONFLICT(account_id,guild_id,bucket_start)
                        DO UPDATE SET message_count=message_count+1,last_message_at=excluded.last_message_at""",
                        (account_id, guild_id, bucket, message_at))
            if not content:
                return False
            rules, threshold = self.rules(conn)
            score, matches = self.match(content, rules, threshold)
            if not matches:
                return False
            message_id = str(message["id"])
            author_id = str(author.get("id") or "unknown")
            excerpt = re.sub(r"\s+", " ", content)
            if len(excerpt) > MAX_EXCERPT:
                excerpt = excerpt[:MAX_EXCERPT - 1].rstrip() + "…"
            matched = ", ".join(phrase for _id, phrase, _weight, _exclusion in matches)
            guild = self.client.get_guild(guild_id) or {}
            channel = self.client.get_channel(channel_id) or {}
            guild_name = guild.get("name") or f"Server {guild_id}"
            channel_name = channel.get("name") or f"channel-{channel_id}"
            author_name = author.get("global_name") or author.get("username") or author_id
            username = author.get("username") or "Unknown"
            notification = TelegramNotification()
            notification.add(f"🆘 Possible support request\n")
            notification.field("Score", score)
            notification.field("Server", guild_name)
            notification.field("Channel", f"#{channel_name}")
            notification.field("User", author_name)
            notification.field("Username", username)
            notification.field("User ID", author_id)
            notification.field("Keywords detected", matched)
            notification.field("Message", excerpt)
            notification.link("View Message in Discord", self.message_link(guild_id, channel_id, message_id))
            text, entities = notification.render()
            with conn:
                cursor = conn.execute("""INSERT OR IGNORE INTO support_alerts
                    (message_id,account_id,guild_id,channel_id,author_id,score,matched,excerpt,created_at,
                     guild_name,channel_name,author_name,message_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (message_id, account_id, guild_id, channel_id, author_id, score, matched, excerpt, now,
                     guild_name, channel_name, author_name, message_at))
                if not cursor.rowcount:
                    return False
                self.outbox.insert(conn, text, f"support:{message_id}", entities=entities,
                                   notification_type="support")
            self.alert_count += 1
            self.outbox.wake.set()
            return True
        finally:
            conn.close()

    async def lookback(self, hours=6, per_channel=25):
        """Explicit admin-triggered, bounded recent-message scan."""
        cutoff_ms = int((time.time() - hours * 3600) * 1000)
        found, checked, failed = 0, 0, 0
        channel_budget = 100
        for guild in self.client.guilds_list:
            if channel_budget <= 0:
                break
            guild_id = str(guild.get("id", ""))
            channels = await asyncio.to_thread(
                self.client._api_get, f"/api/v10/guilds/{guild_id}/channels")
            if not isinstance(channels, list):
                failed += 1
                continue
            for channel in channels:
                if channel.get("type") not in (0, 5):
                    continue
                if channel_budget <= 0:
                    break
                channel_budget -= 1
                channel_id = str(channel.get("id", ""))
                messages = await asyncio.to_thread(
                    self.client._api_get,
                    f"/api/v10/channels/{channel_id}/messages?limit={max(1, min(25, per_channel))}")
                if not isinstance(messages, list):
                    continue
                self.client.channels[channel_id] = {**channel, "guild_id": guild_id}
                eligible = [m for m in messages if int(m.get("id", 0)) >> 22 >= cutoff_ms - 1420070400000]
                for message in sorted(eligible, key=lambda m: int(m.get("id", 0))):
                    message.update(guild_id=guild_id, channel_id=channel_id)
                    found += bool(await self.message_created(message))
                    checked += 1
        return checked, found, failed
