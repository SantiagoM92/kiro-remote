# Kiro Remote — prototype for macOS

This is a Telegram-to-tmux bridge for controlling an opted-in Kiro CLI session on macOS. It does not automatically start or resume Kiro sessions; Kiro must be started manually inside tmux.

## Install

```bash
brew install tmux python
cd kiro-remote
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
mkdir -p ~/.config/kiro-remote
cp .env.example ~/.config/kiro-remote/.env
chmod 600 ~/.config/kiro-remote/.env
```

Edit `.env` with your own token and numeric Telegram chat ID. Never share the token. Message your bot with `/start` first. You can obtain your ID through Telegram's Bot API `getUpdates` or a trusted ID bot. This bridge accepts only messages from the configured chat ID.

Optional notification settings:

```env
KIRO_PROGRESS_DELAY=15
KIRO_PROGRESS_INTERVAL=30
```

`KIRO_PROGRESS_DELAY` controls how long the bot waits after sending a message
before the first progress update. `KIRO_PROGRESS_INTERVAL` controls the time
between subsequent updates.

## Use

From a project directory:

```bash
python /path/to/kiro-remote/kiro_remote.py start myproject --dir .
tmux attach -t kiro_remote_myproject
```

Start Kiro CLI inside this tmux terminal using the command supported by your installed version; for existing sessions, use its verified resume-by-ID command. Detach from tmux with `Ctrl+B` then `D` (Kiro keeps running).

In another terminal, start the Telegram listener:

```bash
cd /path/to/kiro-remote
source .venv/bin/activate
python kiro_remote.py bot
```

Telegram commands:
- `/sessions` list sessions
- `/use myproject` select session
- `/status` show recent terminal output
- `/esc` send the Escape key to the selected terminal
- `/send Please continue with the tests` send a command to the selected terminal
- Plain messages also send to the selected terminal

Messages are sent through tmux as bracketed paste, so multiline text is kept as
one block instead of being delivered as individual key presses. This applies to
`/send` and plain Telegram messages.

While the bot is running, it monitors active `tmux` sessions every few seconds
and sends one Telegram notification when a session transitions to an input
prompt, a confirmation request, or a completed task. Focus changes and screen
redraws do not send repeated notifications. Terminal escape sequences and shell
formatting are removed from Telegram messages. Use `/status` to inspect the full
recent output, or `/esc` to interrupt the current Kiro interaction.

After `/send` or a plain message, the bot sends periodic progress updates while
Kiro is working. It stops progress updates when the terminal appears to return
control to the user, request approval, or finish a task. Detection is heuristic
because Kiro CLI output can vary by version; it does not replace reviewing the
terminal before approving sensitive actions.

## Limitations / security

- The bridge sends **keystrokes** into the active terminal; if Kiro is not at its prompt, text can go to a shell or an interactive approval dialog. Do not treat this as a secure autonomous approvals system.
- Use only a private Telegram chat. The chat ID check is mandatory.
- The tmux session remains live while the Mac stays awake and online.
- Telegram messages and responses traverse Telegram's servers; avoid sending secrets.
- Notifications are based on visible terminal output and are sent on state transitions to avoid focus/redraw noise.
- Terminal output sent to Telegram is cleaned of ANSI control sequences, separators, and excessive spacing.
- If `Cmd+V` does not work in Terminal.app inside tmux, use `pbpaste | tmux load-buffer` followed by `tmux paste-buffer -p` to paste a block reliably.
- Network interruptions while polling Telegram are retried automatically with longer timeouts and IPv4 transport.
