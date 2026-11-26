# IssueScan

IssueScan watches new messages received by connected Discord accounts and sends Telegram alerts for likely requests for help. It evaluates messages as Discord delivers them; it does not poll channel history in the background.

## Install

On an Ubuntu VPS, from the project directory:

```bash
sudo apt update
sudo apt install -y python3-venv
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
sudo python3 install_service.py
```

Configure `TELEGRAM_TOKEN` and `TELEGRAM_CHAT_ID` in `.env`. The Telegram chat ID also identifies the sole authorized administrator. Connect Discord accounts using `/connect` or `/addtoken` in that administrator's private Telegram chat.

The service installer creates or updates the `issuescan` systemd service. Use `sudo systemctl status issuescan --no-pager`, `sudo journalctl -u issuescan -f`, and `sudo systemctl restart issuescan` to manage it. Do not run two IssueScan instances against the same database.

## Matching rules

`/rules` lists the current rules and commands. Defaults include weighted phrases such as `i can't log in` (5), `locked out` (4), `need help` (2), and `login` (1). The default alert threshold is 3.

- `/ruleadd 5 I can't log in` adds or updates a weighted phrase.
- `/ruleexclude solved` suppresses alerts for messages containing that phrase.
- `/ruledel 3` deletes the rule with that ID.
- `/threshold 4` changes the score required to alert.

Matching is case-insensitive substring matching. Matching weights are added; any enabled exclusion phrase suppresses the alert. Keep phrases specific and tune weights and threshold in response to alert noise.

## Alerts and lookback

Alerts include server, channel, author, message link, matched phrases, score, and a short excerpt. Alert records retain only the message ID, metadata, matched phrases, excerpt, and timestamp. The durable event inbox temporarily holds incoming event payloads until processed; it is cleared after processing. Telegram delivery is queued with retry and deduplication. Delivery is at least once if a send outcome is ambiguous.

There is no automatic history scan. An administrator can explicitly run `/lookback [hours]` to inspect up to 25 recent messages in each of up to 100 readable text or announcement channels per connected account (6 hours by default, maximum 24 hours). Existing message IDs are deduplicated against prior alerts. This can take time because Discord REST requests are rate limited.

`/report [hours]` summarizes activity from the last 24 hours by default (maximum 168). It counts all processed guild messages for active-server totals, then separately reports likely support requests that matched the rules, including recent excerpts and links. Message IDs and flagged issue details are retained for 30 days to keep activity counts accurate across duplicate gateway events; full message bodies are not retained.

Only messages delivered to the connected account by Discord can be matched live. Access depends on that account's channel permissions and Discord gateway configuration. Message content is required for text matching.

## Local data

`data/bot.db` stores matching rules, alert metadata, temporary gateway events, Telegram delivery state, and expiring server-list records. Keep it when updating the service. Discord session tokens are stored in `data/discord_accounts.json` with restricted permissions where supported; protect that file and its backups as credentials.

The service uses `websockets`, `python-telegram-bot`, `python-dotenv`, and `httpx` from `requirements.txt`.
