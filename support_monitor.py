"""Live-first support request matching and compact alert persistence."""

import asyncio
import logging
import re
import time
import unicodedata
from collections import deque

from database import get_connection
from delivery import TelegramNotification

logger = logging.getLogger("support-monitor")
MAX_EXCERPT = 280
CONTEXT_SECONDS = 120
CONTEXT_MESSAGES = 6
FOLLOWUP_PATTERN = re.compile(
    r"(?<!\w)(?:same here|me too|same for me|i have the same issue|"
    r"that happens to me|this happens to me|having this issue too)(?!\w)", re.I
)


class SupportMonitor:
    def __init__(self, client, outbox, account_name="Discord account"):
        self.client, self.outbox, self.account_name = client, outbox, account_name
        self.last_error = None
        self.scanned_messages = 0
        self.alert_count = 0
        # Only a tiny, short-lived buffer. Message text is never written to disk here.
        self.recent_by_channel = {}

    def _prune_context(self, now):
        expired = [channel_id for channel_id, messages in self.recent_by_channel.items()
                   if not messages or now - messages[-1]["at"] > CONTEXT_SECONDS]
        for channel_id in expired:
            del self.recent_by_channel[channel_id]

    @staticmethod
    def rules(conn=None):
        owned = conn is None
        conn = conn or get_connection()
        try:
            rules = conn.execute("SELECT id,phrase,weight,exclusion,category FROM support_rules WHERE enabled=1 ORDER BY id").fetchall()
            threshold = int((conn.execute("SELECT value FROM support_settings WHERE setting='threshold'").fetchone() or ("3",))[0])
            return rules, threshold
        finally:
            if owned:
                conn.close()

    @staticmethod
    def match(content, rules, threshold):
        folded = re.sub(r"\s+", " ", unicodedata.normalize("NFKC", content)
                        .replace("’", "'").replace("‘", "'").casefold())
        matches = [row for row in rules
                   if re.search(r"(?<!\w)" + re.escape(unicodedata.normalize("NFKC", row[1])
                                                          .replace("’", "'").replace("‘", "'")
                                                          .casefold()) + r"(?!\w)", folded)]
        if any(row[3] for row in matches):
            return 0, [], "noise"
        contributions = {}
        representatives = {}
        resolution = 0
        for row in matches:
            _rule_id, _phrase, weight, _exclusion, category = row
            if category == "resolution":
                resolution = max(resolution, weight)
            else:
                contributions[category] = max(contributions.get(category, 0), weight)
                previous = representatives.get(category)
                if previous is None or (weight, len(row[1])) > (previous[2], len(previous[1])):
                    representatives[category] = row
        score = sum(contributions.values()) - resolution
        if resolution and resolution >= sum(contributions.values()):
            issue_type = "resolved issue"
        elif contributions.get("help", 0):
            issue_type = "help request"
        elif contributions.get("problem", 0):
            issue_type = "unresolved problem"
        elif contributions.get("topic", 0):
            issue_type = "informational mention"
        else:
            issue_type = "noise"
        positive_matches = list(representatives.values())
        return score, positive_matches, issue_type

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
            self._prune_context(now)
            if not content:
                return False
            rules, threshold = self.rules(conn)
            score, matches, issue_type = self.match(content, rules, threshold)
            recent = self.recent_by_channel.setdefault(channel_id, deque(maxlen=CONTEXT_MESSAGES))
            while recent and now - recent[0]["at"] > CONTEXT_SECONDS:
                recent.popleft()
            context_match = bool(FOLLOWUP_PATTERN.search(content))
            prior_issue = next((item for item in reversed(recent)
                                if item["type"] in ("unresolved problem", "help request")), None)
            used_context = bool(context_match and prior_issue)
            if used_context and issue_type not in ("unresolved problem", "help request"):
                issue_type = "help request"
                score = max(threshold, prior_issue["score"])
                matches = prior_issue["matches"]
            recent.append({"at": now, "type": issue_type, "score": score, "matches": matches})
            if (not matches or score < threshold
                    or issue_type not in ("unresolved problem", "help request")):
                return False
            message_id = str(message["id"])
            author_id = str(author.get("id") or "unknown")
            excerpt = re.sub(r"\s+", " ", content)
            if len(excerpt) > MAX_EXCERPT:
                excerpt = excerpt[:MAX_EXCERPT - 1].rstrip() + "…"
            matched = ", ".join(row[1] for row in matches)
            guild = self.client.get_guild(guild_id) or {}
            channel = self.client.get_channel(channel_id) or {}
            guild_name = guild.get("name") or f"Server {guild_id}"
            channel_name = channel.get("name") or f"channel-{channel_id}"
            author_name = author.get("global_name") or author.get("username") or author_id
            username = author.get("username") or "Unknown"
            notification = TelegramNotification()
            notification.add(f"🆘 Possible support request · score {score}\n")
            notification.field("Type", issue_type.title())
            notification.field("Server", guild_name)
            notification.field("Channel", f"#{channel_name}")
            notification.field("User", author_name)
            notification.field("Username", username)
            notification.field("User ID", author_id)
            notification.field("Keywords detected", matched)
            if used_context:
                notification.field("Context", "Replying to a recent likely support issue")
            notification.field("Message", excerpt)
            notification.link("View Message in Discord", self.message_link(guild_id, channel_id, message_id))
            text, entities = notification.render()
            with conn:
                cursor = conn.execute("""INSERT OR IGNORE INTO support_alerts
                    (message_id,account_id,guild_id,channel_id,author_id,score,matched,excerpt,created_at,
                     guild_name,channel_name,author_name,message_at,issue_type)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (message_id, account_id, guild_id, channel_id, author_id, score, matched, excerpt, now,
                     guild_name, channel_name, author_name, message_at, issue_type))
                if not cursor.rowcount:
                    return False
                self.outbox.insert(conn, text, f"support:{message_id}", entities=entities,
                                   notification_type="support", reply_markup=[[
                                       {"text": "Useful issue", "callback_data": f"feedback:useful:{message_id}"},
                                       {"text": "Not an issue", "callback_data": f"feedback:noise:{message_id}"},
                                   ]])
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
