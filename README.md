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

`/rules` lists the current rules and commands. Rules are grouped as `topic`, `problem`, `help`, `detail`, or `resolution`. Defaults include topic signals like `login` (1), problem signals like `can't` (2) and `error` (2), help requests like `need help` (3), details like `error code` (1), and resolution phrases like `fixed` (3). The default alert threshold is 3.

- `/ruleadd topic 1 login` adds or updates a topic signal.
- `/ruleadd problem 2 can't` adds or updates a problem signal.
- `/ruleadd help 2 can anyone help` adds or updates a help-request signal.
- `/ruleadd detail 1 error code` adds or updates a useful-detail signal.
- `/ruleadd resolution 3 works now` adds a phrase that reduces the score.
- `/ruleadd 5 login` remains supported and treats the phrase as a topic signal.
- `/ruleexclude solved` suppresses alerts for messages containing that phrase.
- `/ruledel 3` deletes the rule with that ID.
- `/threshold 4` changes the score required to alert.

Matching is case-insensitive and requires whole-word or whole-phrase boundaries. For each signal category, only its strongest matched weight contributes, so overlapping phrases do not inflate the score. Resolution weight is subtracted, and an exclusion phrase suppresses the alert. A bare topic such as `login` is informational and scores low; `can't log in` combines problem and topic signals, while a help request such as `can anyone help` adds another signal. Messages are classified as unresolved problems, help requests, informational mentions, resolved issues, or noise; alerts are sent only for likely unresolved problems and help requests that reach the threshold.

IssueScan keeps up to six recent messages per channel in memory for two minutes. A short follow-up such as `same here` can inherit the likely-issue context from a nearby message. This context is not saved as a conversation history. Each alert has `Useful issue` and `Not an issue` buttons; responses are kept with the alert metadata for 30 days. `/feedback [1-20]` shows recent labeled examples to help tune rules and weights.

## Alerts and lookback

Alerts include server, channel, author, message link, matched phrases, score, and a short excerpt. Alert records retain only the message ID, metadata, matched phrases, excerpt, and timestamp. The durable event inbox temporarily holds incoming event payloads until processed; it is cleared after processing. Telegram delivery is queued with retry and deduplication. Delivery is at least once if a send outcome is ambiguous.

There is no automatic history scan. An administrator can explicitly run `/lookback [hours]` to inspect up to 25 recent messages in each of up to 100 readable text or announcement channels per connected account (6 hours by default, maximum 24 hours). Existing message IDs are deduplicated against prior alerts. This can take time because Discord REST requests are rate limited.

`/report [hours]` summarizes activity from the last 24 hours by default (maximum 168). It counts all processed guild messages for active-server totals, then separately reports likely support requests that matched the rules, including type, recent excerpts, and links, plus recent alert feedback totals. Message IDs and flagged issue details are retained for 30 days to keep activity counts accurate across duplicate gateway events; full message bodies are not retained.

Only messages delivered to the connected account by Discord can be matched live. Access depends on that account's channel permissions and Discord gateway configuration. Message content is required for text matching.

## Local data

`data/bot.db` stores matching rules, alert metadata, temporary gateway events, Telegram delivery state, and expiring server-list records. Keep it when updating the service. Discord session tokens are stored in `data/discord_accounts.json` with restricted permissions where supported; protect that file and its backups as credentials.

The service uses `websockets`, `python-telegram-bot`, `python-dotenv`, and `httpx` from `requirements.txt`.
