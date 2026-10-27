#!/usr/bin/env python3
"""Minimal Telegram-to-tmux bridge for opt-in Kiro CLI sessions."""
import argparse
import asyncio
import contextlib
import logging
import os
import re
import subprocess
import time
from pathlib import Path
import httpx
from dotenv import load_dotenv
from telegram import Update
from telegram.error import NetworkError, TelegramError
from telegram.request import HTTPXRequest
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

load_dotenv(Path.home() / '.config' / 'kiro-remote' / '.env')
PREFIX = 'kiro_remote_'
MONITOR_INTERVAL = float(os.environ.get('KIRO_MONITOR_INTERVAL', '2'))
PROGRESS_DELAY = float(os.environ.get('KIRO_PROGRESS_DELAY', '15'))
PROGRESS_INTERVAL = float(os.environ.get('KIRO_PROGRESS_INTERVAL', '30'))
ANSI_ESCAPE = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')
CONTROL_CHARS = re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]')
LOGGER = logging.getLogger(__name__)


def tmux(*args):
    return subprocess.run(['tmux', *args], text=True, capture_output=True, check=True).stdout.strip()


def tmux_paste(name, text):
    buffer_name = 'kiro_remote_input'
    subprocess.run(
        ['tmux', 'load-buffer', '-b', buffer_name, '-'],
        input=text,
        text=True,
        capture_output=True,
        check=True,
    )
    tmux('paste-buffer', '-p', '-b', buffer_name, '-t', PREFIX + name)
    tmux('send-keys', '-t', PREFIX + name, 'Enter')


def sessions():
    try:
        output = tmux('list-sessions', '-F', '#{session_name}')
    except subprocess.CalledProcessError:
        return []
    return [s[len(PREFIX):] for s in output.splitlines() if s.startswith(PREFIX)]


def pane_output(name):
    try:
        return tmux('capture-pane', '-p', '-J', '-t', PREFIX + name, '-S', '-80')
    except subprocess.CalledProcessError:
        return ''


def clean_output(output):
    output = ANSI_ESCAPE.sub('', output).replace('\r', '')
    output = CONTROL_CHARS.sub('', output)
    lines = []
    previous_blank = False
    for line in output.splitlines():
        stripped = line.strip()
        if stripped and set(stripped) <= set('─—_-═=') and len(stripped) >= 10:
            continue
        if not stripped:
            if previous_blank:
                continue
            previous_blank = True
            lines.append('')
            continue
        previous_blank = False
        lines.append(re.sub(r'\s{2,}', ' ', line).strip())
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    return '\n'.join(lines)


def notification_reason(output):
    recent_lines = clean_output(output).splitlines()[-20:]
    recent = '\n'.join(recent_lines)
    last_line = next((line.strip() for line in reversed(recent_lines) if line.strip()), '')
    if (
        re.fullmatch(r'[>❯›]', last_line)
        or re.fullmatch(r'(?:you|user|answer|response)\s*:', last_line, re.IGNORECASE)
        or re.search(r'\b(?:enter|type|write|provide)\s+(?:your\s+)?(?:answer|response|input|request)\b', last_line, re.IGNORECASE)
    ):
        return 'Kiro está esperando tu respuesta.'
    if re.search(
        r'\[(?:y/n|Y/n|y/N|yes/no|Yes/No)\]|'
        r'\b(?:confirm|confirmation|approve|approval|allow|permission|authorize|'
        r'proceed|continue|accept|reject)\b.*(?:\?|:)|'
        r'\b(?:do you want|would you like|should I)\b|¿.*\?',
        recent,
        re.IGNORECASE,
    ):
        return 'Kiro parece requerir una confirmación.'
    if re.search(
        r'\b(?:done|completed|complete|finished|successfully|task complete|'
        r'all tests passed|tests passed|finalizado|completado|terminado)\b',
        recent,
        re.IGNORECASE,
    ):
        return 'Kiro parece haber terminado una tarea.'
    return ''


def notification_state(output):
    reason = notification_reason(output)
    if reason == 'Kiro está esperando tu respuesta.':
        return 'waiting'
    if reason == 'Kiro parece requerir una confirmación.':
        return 'action'
    if reason == 'Kiro parece haber terminado una tarea.':
        return 'completed'
    return ''


async def monitor_sessions(application):
    chat_id = os.environ['TELEGRAM_CHAT_ID']
    states = {}
    activities = application.bot_data.setdefault('activities', {})
    while True:
        active = set(sessions())
        for name in active:
            output = await asyncio.to_thread(pane_output, name)
            cleaned_output = clean_output(output)
            state = notification_state(output)
            activity = activities.get(name)
            now = time.monotonic()
            if activity and cleaned_output != activity['baseline']:
                activity['saw_change'] = True
            if activity and activity['saw_change'] and not state and now - activity['started'] >= PROGRESS_DELAY:
                if now - activity['last_progress'] >= PROGRESS_INTERVAL:
                    excerpt = cleaned_output[-1000:] or '(sin salida nueva)'
                    try:
                        await application.bot.send_message(
                            chat_id=chat_id,
                            text=f'[{name}] Kiro sigue trabajando...\n\nÚltima salida:\n{excerpt}',
                        )
                    except TelegramError:
                        LOGGER.exception('Could not send progress notification for %s', name)
                    else:
                        activity['last_progress'] = now
            if state == states.get(name) and not (activity and activity['saw_change'] and state):
                continue
            if not state:
                states[name] = ''
                continue
            if activity and not activity['saw_change']:
                continue
            reason = notification_reason(output)
            excerpt = cleaned_output[-1800:] or '(terminal vacío)'
            try:
                await application.bot.send_message(
                    chat_id=chat_id,
                    text=f'[{name}] {reason}\n\nÚltima salida:\n{excerpt}',
                )
            except TelegramError:
                LOGGER.exception('Could not send monitor notification for %s', name)
                continue
            states[name] = state
            activities.pop(name, None)
        for name in set(states) - active:
            states.pop(name, None)
            activities.pop(name, None)
        await asyncio.sleep(MONITOR_INTERVAL)


async def start_monitor(application):
    application.bot_data['monitor_task'] = asyncio.create_task(monitor_sessions(application))


async def stop_monitor(application):
    task = application.bot_data.pop('monitor_task', None)
    if task:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def error_handler(update, context):
    if isinstance(context.error, NetworkError):
        LOGGER.warning('Telegram network interruption; polling will retry: %s', context.error)
        return
    LOGGER.exception('Unhandled Telegram error', exc_info=context.error)


def validate_name(name):
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,48}', name):
        raise ValueError('Use 1-48 letters, numbers, underscores or hyphens')


def start_session(name, directory, resume_id=None):
    validate_name(name)
    directory = str(Path(directory).expanduser().resolve(strict=True))
    if not Path(directory).is_dir():
        raise ValueError('Project path must be a directory')
    if name in sessions():
        raise ValueError('That session is already running')
    # Start an interactive shell in tmux; run the Kiro resume command manually
    # until the exact CLI syntax has been verified for the installed version.
    tmux('new-session', '-d', '-s', PREFIX + name, '-c', directory)
    if resume_id:
        print('Resume ID saved for manual use:', resume_id)
    print(f'Started {name}. Attach: tmux attach -t {PREFIX}{name}')


def allowed(update):
    chat_id = os.environ.get('TELEGRAM_CHAT_ID', '')
    return bool(update.effective_chat and str(update.effective_chat.id) == chat_id)


async def deny_or_allow(update):
    if not allowed(update):
        if update.effective_message:
            await update.effective_message.reply_text('Unauthorized.')
        return False
    return True


async def cmd_sessions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await deny_or_allow(update): return
    found = sessions()
    await update.message.reply_text('Sessions: ' + ', '.join(found) if found else 'No active remote sessions.')


async def cmd_use(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await deny_or_allow(update): return
    if len(context.args) != 1 or context.args[0] not in sessions():
        await update.message.reply_text('Usage: /use SESSION (see /sessions)')
        return
    context.user_data['selected'] = context.args[0]
    await update.message.reply_text('Selected: ' + context.args[0])


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await deny_or_allow(update): return
    name = context.user_data.get('selected')
    if not name or name not in sessions():
        await update.message.reply_text('Select a running session with /use SESSION')
        return
    output = clean_output(pane_output(name))
    await update.message.reply_text((output or '(terminal vacío)')[-3500:])


async def cmd_escape(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await deny_or_allow(update): return
    name = context.user_data.get('selected')
    if not name or name not in sessions():
        await update.message.reply_text('Select a running session with /use SESSION')
        return
    tmux('send-keys', '-t', PREFIX + name, 'Escape')
    await update.message.reply_text('ESC enviado a ' + name + '.')


async def cmd_send(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await deny_or_allow(update): return
    name = context.user_data.get('selected')
    if not name or name not in sessions():
        await update.message.reply_text('Select a running session with /use SESSION')
        return
    if context.args:
        message = update.message.text.partition(' ')[2]
    else:
        message = update.message.text if update.message and not update.message.text.startswith('/') else ''
    if not message.strip():
        await update.message.reply_text('Usage: /send your instruction')
        return
    tmux_paste(name, message)
    baseline = clean_output(pane_output(name))
    context.application.bot_data.setdefault('activities', {})[name] = {
        'started': time.monotonic(),
        'last_progress': 0,
        'baseline': baseline,
        'saw_change': False,
    }
    await update.message.reply_text('Sent to ' + name + '. Use /status to inspect output.')


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest='action', required=True)
    p = sub.add_parser('start')
    p.add_argument('name')
    p.add_argument('--dir', default='.')
    p.add_argument('--resume-id')
    sub.add_parser('bot')
    sub.add_parser('list')
    args = parser.parse_args()
    if args.action == 'start':
        start_session(args.name, args.dir, args.resume_id)
    elif args.action == 'list':
        print('\n'.join(sessions()))
    else:
        if not os.environ.get('TELEGRAM_BOT_TOKEN') or not os.environ.get('TELEGRAM_CHAT_ID'):
            parser.error('Configure TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in ~/.config/kiro-remote/.env')
        request = HTTPXRequest(
            connect_timeout=30,
            read_timeout=40,
            write_timeout=30,
            pool_timeout=30,
            httpx_kwargs={
                'transport': httpx.AsyncHTTPTransport(local_address='0.0.0.0'),
            },
        )
        app = (
            Application.builder()
            .token(os.environ['TELEGRAM_BOT_TOKEN'])
            .request(request)
            .get_updates_request(request)
            .post_init(start_monitor)
            .post_shutdown(stop_monitor)
            .build()
        )
        app.add_handler(CommandHandler('sessions', cmd_sessions))
        app.add_handler(CommandHandler('use', cmd_use))
        app.add_handler(CommandHandler('status', cmd_status))
        app.add_handler(CommandHandler('esc', cmd_escape))
        app.add_handler(CommandHandler('send', cmd_send))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, cmd_send))
        app.add_error_handler(error_handler)
        app.run_polling(timeout=30, bootstrap_retries=-1)


if __name__ == '__main__':
    main()
