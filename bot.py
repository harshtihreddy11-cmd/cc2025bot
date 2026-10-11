#!/usr/bin/env python3
# bot.py - Button-driven Telegram UI for the cc2025 / Stalker downloader

import asyncio
import json
import logging
import queue
import shutil as _sh
import time as _time
import urllib.parse
from datetime import datetime, timedelta
from pathlib import Path

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from telegram.request import HTTPXRequest

import downloader
import stalker
from stalker import IST

BASE = Path(__file__).resolve().parent
CONFIG_PATH = BASE / "config.json"
BOT_CONFIG_PATH = BASE / "bot_config.json"
CHANNELS_CACHE = BASE / "channels.json"
OUT_ROOT = BASE / "downloads"
TELEGRAM_MAX_UPLOAD_BYTES = 45_000_000
LOCAL_BOT_API_MAX_UPLOAD_BYTES = 1900 * 1024 * 1024

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("cc2025-bot")


def is_upload_size_error(error):
    message = str(error).lower()
    return "413" in message or "request entity too large" in message


def load_cfg():
    return json.loads(CONFIG_PATH.read_text())


def load_bot_cfg():
    return json.loads(BOT_CONFIG_PATH.read_text())


def is_allowed(user_id, bot_cfg):
    allowed = bot_cfg.get("allowed_user_ids") or []
    return True if not allowed else user_id in allowed


def parse_date_simple(s):
    s = s.lower().strip()
    if s == "today":
        return datetime.now(IST).date()
    if s == "yesterday":
        return (datetime.now(IST) - timedelta(days=1)).date()
    return datetime.strptime(s, "%Y-%m-%d").date()


def _wipe_tree(root: Path):
    try:
        if root.exists():
            _sh.rmtree(root, ignore_errors=True)
    except Exception as e:
        log.warning("wipe failed for %s: %s", root, e)


class ProgressReporter:
    def __init__(self, bot, chat_id, header=""):
        self.bot = bot
        self.chat_id = chat_id
        self.lines = [header] if header else []
        self.q = queue.Queue()
        self.msg_id = None
        self._stopped = False
        self._last_edit = 0.0

    def log(self, line):
        self.q.put(("line", line))

    def done(self, final_text=None):
        self.q.put(("done", final_text))

    async def run(self):
        m = await self.bot.send_message(self.chat_id, "\n".join(self.lines) or "...")
        self.msg_id = m.message_id
        while not self._stopped:
            try:
                kind, payload = self.q.get_nowait()
            except queue.Empty:
                await asyncio.sleep(0.7)
                continue
            if kind == "done":
                if payload:
                    self.lines.append(payload)
                await self._flush()
                self._stopped = True
                break
            self.lines.append(payload)
            if len(self.lines) > 25:
                self.lines = self.lines[-25:]
            now = _time.time()
            if now - self._last_edit >= 1.2:
                self._last_edit = now
                await self._flush()

    async def _flush(self):
        text = "\n".join(self.lines)[-3500:]
        try:
            await self.bot.edit_message_text(
                chat_id=self.chat_id,
                message_id=self.msg_id,
                text=text,
            )
        except Exception:
            pass


def load_all_channels_blocking():
    cfg = load_cfg()
    sess = stalker.Session(cfg)
    return list(stalker.iter_channels(
        stalker.load_channels(sess, CHANNELS_CACHE, False)))


def search_channels(channels, term, limit=25):
    if not term:
        return channels[:limit]
    t = term.lower()
    return [
        c
        for c in channels
        if t in (c.get("name") or "").lower()
        or t == str(c.get("number", "")).strip()
        or t == stalker.channel_id_of(c)
        or t == stalker.cmd_id_of(c)
    ][:limit]


def kb_main():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📺  Channels", callback_data="nav|ch")],
        [InlineKeyboardButton("🕘  Recent", callback_data="nav|recent")],
        [InlineKeyboardButton("❓  Help", callback_data="nav|help")],
    ])


def kb_search_prompt():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔍 Search channel", callback_data="chsearch")],
        [InlineKeyboardButton("🕘 Recent", callback_data="nav|recent")],
        [InlineKeyboardButton("⬅️ Back", callback_data="nav|main")],
    ])


def kb_search_results(channels):
    rows = []
    for i, ch in enumerate(channels, 1):
        num = ch.get("number", "")
        arch = "✅" if ch.get("enable_tv_archive") else "  "
        name = ch.get("name", "?")
        if len(name) > 30:
            name = name[:27] + "…"
        rows.append([
            InlineKeyboardButton(
                "{}. {} #{} {}".format(i, arch, num, name),
                callback_data="chpick|{}".format(stalker.channel_id_of(ch)),
            )
        ])
    rows.append([InlineKeyboardButton("🔍 New search", callback_data="chsearch")])
    rows.append([InlineKeyboardButton("⬅️ Back", callback_data="nav|ch")])
    return InlineKeyboardMarkup(rows)


def kb_day_picker(ch_id):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📅  Yesterday", callback_data="day|{}|yesterday".format(ch_id)),
            InlineKeyboardButton("📅  Today", callback_data="day|{}|today".format(ch_id)),
        ],
        [InlineKeyboardButton("🗓  Other date", callback_data="day|{}|other".format(ch_id))],
        [InlineKeyboardButton("⬅️ Back", callback_data="nav|ch")],
    ])


def kb_programmes(ch_id, date_str, programmes, page=0, per_page=8):
    start = page * per_page
    chunk = programmes[start:start + per_page]
    rows = []
    for i, p in enumerate(chunk):
        idx = start + i
        st = datetime.fromtimestamp(int(p["start_timestamp"]), IST)
        en = datetime.fromtimestamp(int(p["stop_timestamp"]), IST)
        name = (p.get("name") or "?")[:28]
        label = "{:%H:%M}–{:%H:%M}  {}".format(st, en, name)
        rows.append([
            InlineKeyboardButton(label, callback_data="prog|{}|{}|{}".format(ch_id, date_str, idx))
        ])
    nav = []
    if page > 0:
        nav.append(
            InlineKeyboardButton("⬅️ Prev", callback_data="progpg|{}|{}|{}".format(ch_id, date_str, page - 1))
        )
    if start + per_page < len(programmes):
        nav.append(
            InlineKeyboardButton("Next ➡️", callback_data="progpg|{}|{}|{}".format(ch_id, date_str, page + 1))
        )
    if nav:
        rows.append(nav)
    rows.append([
        InlineKeyboardButton("📥 Download whole day", callback_data="allday|{}|{}".format(ch_id, date_str))
    ])
    rows.append([InlineKeyboardButton("⬅️ Back", callback_data="chpick_after|{}".format(ch_id))])
    return InlineKeyboardMarkup(rows)


def kb_media(ch_id, date_str, prog_idx):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "🎬  Video (MP4)",
                callback_data="asksplit|v|{}|{}|{}".format(ch_id, date_str, prog_idx),
            )
        ],
        [
            InlineKeyboardButton(
                "🎵  Audio only (M4A)",
                callback_data="asksplit|a|{}|{}|{}".format(ch_id, date_str, prog_idx),
            )
        ],
        [InlineKeyboardButton("⬅️ Back", callback_data="progpg|{}|{}|0".format(ch_id, date_str))],
    ])


def kb_time_split(ch_id, date_str, prog_idx, media, options):
    rows = []
    for mins in options:
        label = "No time split" if mins == 0 else "Every {} min".format(mins)
        rows.append([
            InlineKeyboardButton(
                label,
                callback_data="tsplit|{}|{}|{}|{}|{}".format(media, ch_id, date_str, prog_idx, mins),
            )
        ])
    rows.append([InlineKeyboardButton("⬅️ Back", callback_data="prog|{}|{}|{}".format(ch_id, date_str, prog_idx))])
    return InlineKeyboardMarkup(rows)


def kb_time_split_allday(ch_id, date_str, media, options):
    rows = []
    for mins in options:
        label = "No time split" if mins == 0 else "Every {} min".format(mins)
        rows.append([
            InlineKeyboardButton(
                label,
                callback_data="atsplit|{}|{}|{}|{}".format(media, ch_id, date_str, mins),
            )
        ])
    rows.append([InlineKeyboardButton("⬅️ Back", callback_data="allday|{}|{}".format(ch_id, date_str))])
    return InlineKeyboardMarkup(rows)


def kb_back_main():
    return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Main menu", callback_data="nav|main")]])


def run_download_job(cfg, ch, media, d, prog_items, max_bytes,
                     time_split_minutes, reporter, out_dir):
    log_ = reporter.log
    sess = stalker.Session(cfg)

    log_("🔐 Authenticating…")
    stalker.auth(sess, force=True, log=log_)

    ch_id = stalker.channel_id_of(ch)
    cmd_id = stalker.cmd_id_of(ch)
    ch_name = ch.get("name", "ch{}".format(ch_id))

    log_("📡 Warming up {}…".format(ch_name))
    stalker.warmup(sess, ch_id, cmd_id, log=log_)

    ff = downloader.ensure_ffmpeg()
    want_audio = media == "a"
    out_dir.mkdir(parents=True, exist_ok=True)

    produced = []
    for idx, (prog, sub_start, sub_end) in enumerate(prog_items, 1):
        title = prog.get("name") or "programme_{}".format(idx)
        log_("▶️ [{}/{}] {}".format(idx, len(prog_items), title[:60]))

        try:
            want, prog_abs_start, init_segs = stalker.fetch_archive_segments(
                sess,
                ch_id,
                prog,
                sub_start,
                sub_end,
                log=log_,
                quality=cfg.get("quality", "lowest"),
                want_audio=want_audio,
            )
        except Exception as e:
            log_("  ❌ {}".format(e))
            continue

        log_("  📦 {} segments".format(len(want)))
        if not want:
            continue

        blk_dir = out_dir / "block_{:02d}_epg{}".format(idx, prog.get("id"))

        # fMP4 init segment (if any)
        init_paths = []
        if init_segs:
            try:
                for j, iu in enumerate(init_segs):
                    dst = blk_dir / "init_{:02d}.mp4".format(j)
                    if not (dst.exists() and dst.stat().st_size > 0):
                        data = downloader.fetch_segment(
                            iu,
                            timeout=max(1, int(cfg.get(
                                "seg_timeout",
                                downloader.DEFAULT_SEGMENT_TIMEOUT))),
                            referer=cfg.get("referer"),
                            host=urllib.parse.urlparse(iu).netloc,
                        )
                        dst.parent.mkdir(parents=True, exist_ok=True)
                        tmp = dst.with_name(dst.name + ".part")
                        tmp.write_bytes(data)
                        tmp.replace(dst)
                    init_paths.append(dst)
            except Exception as e:
                log_("  ⚠️ init segment failed: {}".format(e))

        try:
            paths, _ = downloader.download_segments(
                sess,
                want,
                blk_dir,
                workers=max(1, min(int(cfg.get("workers",
                                               downloader.DEFAULT_WORKERS)), 64)),
                timeout=max(1, int(cfg.get("seg_timeout",
                                           downloader.DEFAULT_SEGMENT_TIMEOUT))),
                log=log_,
            )
        except Exception as e:
            log_("  ❌ download failed: {}".format(e))
            continue
        if not paths:
            continue

        st = datetime.fromtimestamp(sub_start, IST)
        en = datetime.fromtimestamp(sub_end, IST)
        base = "{} {}-{} {}".format(
            d.isoformat(),
            st.strftime("%H%M"),
            en.strftime("%H%M"),
            downloader.sanitize_filename(title),
        )

        out_ext = ".m4a" if want_audio else ".mp4"
        out_path = downloader.unique_path(out_dir / (base + out_ext))

        merged_ts = blk_dir / "_merged.ts"
        merged_for_remux = None

        try:
            if want_audio:
                downloader.concat_ts(init_paths + paths, merged_ts)
                if not downloader.has_audio_stream(merged_ts):
                    raise RuntimeError(
                        "archive contains no audio stream; cannot create "
                        "an audio-only file")
                merged_for_remux = merged_ts
                downloader.extract_audio_to_m4a(ff, merged_ts, out_path)
            else:
                if init_paths:
                    merged_mp4 = blk_dir / "_merged.mp4"
                    downloader.concat_ts(init_paths + paths, merged_mp4)
                    merged_for_remux = merged_mp4
                else:
                    downloader.concat_ts(paths, merged_ts)
                    merged_for_remux = merged_ts
                downloader.remux_ts_to_mp4(ff, merged_for_remux, out_path)
        except Exception as e:
            log_("  ❌ conversion failed: {}".format(e))
            continue

        candidates = [out_path]
        if time_split_minutes and time_split_minutes > 0:
            try:
                parts = downloader.split_by_duration(
                    ff, out_path, out_dir, time_split_minutes * 60)
                if len(parts) > 1:
                    log_("  ✂️ time-split → {} parts".format(len(parts)))
                    try:
                        out_path.unlink()
                    except Exception:
                        pass
                    candidates = parts
                else:
                    log_("  (time split produced 1 part)")
            except Exception as e:
                raise RuntimeError(
                    "time-split failed for {}: {}".format(out_path.name, e)
                ) from e

        final_parts = []
        for c in candidates:
            try:
                sp = downloader.split_by_size_limit(ff, c, out_dir, max_bytes)
            except Exception as e:
                log_("  ⚠️ size-split skipped for {}: {}".format(c.name, e))
                sp = [c]
            if len(sp) > 1:
                log_("  ✂️ size-split {} → {} parts".format(c.name, len(sp)))
                try:
                    c.unlink()
                except Exception:
                    pass
            final_parts.extend(sp)

        final_parts = downloader.number_parts(final_parts, out_path)
        produced.extend(final_parts)

        try:
            for p in paths:
                p.unlink()
            for ip in init_paths:
                try:
                    ip.unlink()
                except Exception:
                    pass
            if merged_ts.exists():
                merged_ts.unlink()
            if merged_for_remux and merged_for_remux.exists() \
                    and merged_for_remux != merged_ts:
                try:
                    merged_for_remux.unlink()
                except Exception:
                    pass
            try:
                blk_dir.rmdir()
            except OSError:
                pass
        except Exception:
            pass

    return produced


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📺 *cc2025 Downloader*\n\nTap a button to get started.",
        reply_markup=kb_main(),
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_id(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Your user ID: `{}`".format(update.effective_user.id),
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    running = ctx.application.bot_data.get("running_jobs", 0)
    await update.message.reply_text("✅ Alive\nRunning jobs: {}".format(running))


async def cmd_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data.clear()
    await update.message.reply_text("Cancelled.", reply_markup=kb_main())


async def cmd_download(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    bot_cfg = ctx.application.bot_data["bot_cfg"]
    if not is_allowed(update.effective_user.id, bot_cfg):
        await update.message.reply_text("Not authorised.")
        return
    await update.message.reply_text(
        "📺 *cc2025 Downloader*\n\nTap a button:",
        reply_markup=kb_main(),
        parse_mode=ParseMode.MARKDOWN,
    )


async def on_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    bot_cfg = ctx.application.bot_data["bot_cfg"]
    if not is_allowed(update.effective_user.id, bot_cfg):
        await q.edit_message_text("Not authorised.")
        return

    data = q.data or ""
    parts = data.split("|")
    kind = parts[0]

    if kind == "nav":
        sub = parts[1] if len(parts) > 1 else "main"
        if sub == "main":
            await q.edit_message_text(
                "📺 *cc2025 Downloader*\n\nTap a button:",
                reply_markup=kb_main(),
                parse_mode=ParseMode.MARKDOWN,
            )
            return
        if sub == "help":
            await q.edit_message_text(
                "How to use:\n\n"
                "1. Tap *Channels*\n"
                "2. Tap *Search channel* and type a name / number\n"
                "3. Tap a result\n"
                "4. Pick a day\n"
                "5. Tap a programme (or *Download whole day*)\n"
                "6. Choose Video or Audio\n"
                "7. Choose split option (No split / Every 90 min)\n\n"
                "Channels with ✅ support catchup.\n"
                "Progress is shown live, then the file is uploaded.\n"
                "All files are deleted from disk after upload.",
                reply_markup=kb_back_main(),
                parse_mode=ParseMode.MARKDOWN,
            )
            return
        if sub == "ch":
            ctx.user_data["awaiting"] = None
            await q.edit_message_text(
                "📺 *Find a channel*\n\nTap *Search channel* and type a name, "
                "number, or ID (e.g. `star`, `HBO`, `101`).\n\n"
                "Channels with ✅ support catchup/replay.",
                reply_markup=kb_search_prompt(),
                parse_mode=ParseMode.MARKDOWN,
            )
            return
        if sub == "recent":
            await show_recent_channels(q, ctx)
            return

    if kind == "chsearch":
        ctx.user_data["awaiting"] = "channel_search"
        await q.edit_message_text(
            "🔍 Type a name or number to search\n_(e.g. star, HBO, 101)_",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("⬅️ Back", callback_data="nav|ch")]]),
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    if kind in ("chpick", "chpick_after"):
        ch_id = parts[1]
        await show_day_picker(q, ctx, ch_id)
        return

    if kind == "day":
        ch_id, which = parts[1], parts[2]
        if which == "other":
            ctx.user_data["awaiting"] = "date"
            ctx.user_data["pending_ch_id"] = ch_id
            await q.edit_message_text(
                "📅 Send a date (YYYY-MM-DD):",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("⬅️ Back",
                                           callback_data="chpick_after|{}".format(ch_id))]]),
            )
            return
        d = parse_date_simple(which)
        await show_programmes(q, ctx, ch_id, d, page=0)
        return

    if kind == "progpg":
        ch_id, date_str, page = parts[1], parts[2], int(parts[3])
        d = datetime.strptime(date_str, "%Y-%m-%d").date()
        await show_programmes(q, ctx, ch_id, d, page=page)
        return

    if kind == "prog":
        ch_id, date_str, idx = parts[1], parts[2], int(parts[3])
        cached = ctx.user_data.get("programmes") or {}
        if cached.get("ch_id") != ch_id or cached.get("date") != date_str:
            await q.edit_message_text(
                "Session expired. Please pick the channel again.",
                reply_markup=kb_back_main(),
            )
            return
        progs = cached["list"]
        if not (0 <= idx < len(progs)):
            await q.edit_message_text("Invalid selection.")
            return
        p = progs[idx]
        st = datetime.fromtimestamp(int(p["start_timestamp"]), IST)
        en = datetime.fromtimestamp(int(p["stop_timestamp"]), IST)
        await q.edit_message_text(
            "🎬 *{}*\n{:%H:%M}–{:%H:%M} IST · {:.0f} min\n\nChoose format:".format(
                p.get("name") or "?",
                st,
                en,
                (int(p["stop_timestamp"]) - int(p["start_timestamp"])) / 60,
            ),
            reply_markup=kb_media(ch_id, date_str, idx),
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    if kind == "asksplit":
        media, ch_id, date_str, idx = parts[1], parts[2], parts[3], int(parts[4])
        cached = ctx.user_data.get("programmes") or {}
        progs = cached.get("list") or []
        if not (0 <= idx < len(progs)):
            await q.edit_message_text("Session expired.", reply_markup=kb_back_main())
            return
        p = progs[idx]
        options = ctx.application.bot_data["bot_cfg"].get(
            "time_split_options_minutes", [0, 90])
        await q.edit_message_text(
            "✂️ *{}*\n\nSplit by time before uploading?".format(p.get("name") or "?"),
            reply_markup=kb_time_split(ch_id, date_str, idx, media, options),
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    if kind == "tsplit":
        media, ch_id, date_str, idx, mins = (
            parts[1], parts[2], parts[3], int(parts[4]), int(parts[5]))
        cached = ctx.user_data.get("programmes") or {}
        progs = cached.get("list") or []
        if not (0 <= idx < len(progs)):
            await q.edit_message_text("Session expired.", reply_markup=kb_back_main())
            return
        d = datetime.strptime(date_str, "%Y-%m-%d").date()
        ch = cached["channel"]
        items = [(progs[idx], int(progs[idx]["start_timestamp"]),
                  int(progs[idx]["stop_timestamp"]))]
        await start_job(q, ctx, ch, media, d, items, time_split_minutes=mins)
        return

    if kind == "allday":
        ch_id, date_str = parts[1], parts[2]
        cached = ctx.user_data.get("programmes") or {}
        if cached.get("ch_id") != ch_id:
            await q.edit_message_text("Session expired.", reply_markup=kb_back_main())
            return
        ch = cached["channel"]
        progs = cached["list"]
        await q.edit_message_text(
            "📥 *Download whole day*\n{} · {} programmes\n\nChoose format:".format(
                ch.get("name"), len(progs)),
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "🎬 Video (MP4) — {} files".format(len(progs)),
                        callback_data="alldaymedia|v|{}|{}".format(ch_id, date_str),
                    )
                ],
                [
                    InlineKeyboardButton(
                        "🎵 Audio (M4A) — {} files".format(len(progs)),
                        callback_data="alldaymedia|a|{}|{}".format(ch_id, date_str),
                    )
                ],
                [InlineKeyboardButton("⬅️ Back",
                                      callback_data="progpg|{}|{}|0".format(ch_id, date_str))],
            ]),
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    if kind == "alldaymedia":
        media, ch_id, date_str = parts[1], parts[2], parts[3]
        cached = ctx.user_data.get("programmes") or {}
        ch = cached["channel"]
        progs = cached["list"]
        options = ctx.application.bot_data["bot_cfg"].get(
            "time_split_options_minutes", [0, 90])
        await q.edit_message_text(
            "✂️ *Download whole day*\n{} · {} programmes\n\nSplit each programme by time?".format(
                ch.get("name"), len(progs)),
            reply_markup=kb_time_split_allday(ch_id, date_str, media, options),
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    if kind == "atsplit":
        media, ch_id, date_str, mins = parts[1], parts[2], parts[3], int(parts[4])
        cached = ctx.user_data.get("programmes") or {}
        ch = cached["channel"]
        d = datetime.strptime(date_str, "%Y-%m-%d").date()
        progs = cached["list"]
        items = [(p, int(p["start_timestamp"]), int(p["stop_timestamp"]))
                 for p in progs]
        await start_job(q, ctx, ch, media, d, items, time_split_minutes=mins)
        return


async def show_recent_channels(q, ctx):
    ids = ctx.user_data.get("recent_channels", [])
    if not ids:
        await q.edit_message_text(
            "No recent channels yet.\nUse 🔍 *Search channel* to find one.",
            reply_markup=kb_search_prompt(),
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    all_ch = ctx.user_data.get("all_channels") or []
    if not all_ch:
        try:
            all_ch = await asyncio.to_thread(load_all_channels_blocking)
            ctx.user_data["all_channels"] = all_ch
        except Exception as e:
            await q.edit_message_text("Failed: {}".format(e),
                                      reply_markup=kb_search_prompt())
            return
    by_id = {stalker.channel_id_of(c): c for c in all_ch}
    recent = [by_id[i] for i in ids if i in by_id]
    if not recent:
        await q.edit_message_text("No recent channels.",
                                  reply_markup=kb_search_prompt())
        return
    await q.edit_message_text(
        "🕘 *Recent channels* — tap one:",
        reply_markup=kb_search_results(recent),
        parse_mode=ParseMode.MARKDOWN,
    )


async def _resolve_channel(ctx, ch_id):
    all_ch = ctx.user_data.get("all_channels") or []
    ch = next((c for c in all_ch if stalker.channel_id_of(c) == ch_id), None)
    if ch:
        return ch
    try:
        all_ch = await asyncio.to_thread(load_all_channels_blocking)
        ctx.user_data["all_channels"] = all_ch
    except Exception:
        return None
    return next((c for c in all_ch if stalker.channel_id_of(c) == ch_id), None)


async def show_day_picker(q, ctx, ch_id):
    ch = await _resolve_channel(ctx, ch_id)
    if not ch:
        await q.edit_message_text("Channel not found.",
                                  reply_markup=kb_search_prompt())
        return

    ctx.user_data["current_channel"] = ch
    recent = ctx.user_data.get("recent_channels", [])
    if ch_id in recent:
        recent.remove(ch_id)
    recent.insert(0, ch_id)
    ctx.user_data["recent_channels"] = recent[:5]

    arch = "✅ catchup" if ch.get("enable_tv_archive") else "⚠️ no catchup"
    await q.edit_message_text(
        "*{}*  (#{}, {})\n\nPick a day:".format(
            ch.get("name"), ch.get("number", ""), arch),
        reply_markup=kb_day_picker(ch_id),
        parse_mode=ParseMode.MARKDOWN,
    )


async def show_programmes(q, ctx, ch_id, d, page=0):
    ch = await _resolve_channel(ctx, ch_id)
    if not ch:
        await q.edit_message_text("Channel not found.",
                                  reply_markup=kb_search_prompt())
        return

    await q.edit_message_text("📡 Loading EPG…")
    date_str = d.isoformat()
    try:
        cfg = load_cfg()
        sess = stalker.Session(cfg)
        await asyncio.to_thread(stalker.auth, sess, True, log.info)
        progs = await asyncio.to_thread(
            stalker.fetch_epg, sess, ch_id, date_str, log.info)
    except Exception as e:
        await q.edit_message_text("EPG failed: {}".format(e),
                                  reply_markup=kb_back_main())
        return

    progs.sort(key=lambda p: int(p["start_timestamp"]))
    ctx.user_data["programmes"] = {
        "ch_id": ch_id, "date": date_str, "list": progs, "channel": ch}
    if not progs:
        await q.edit_message_text(
            "No EPG for {} on {}.".format(ch.get("name"), date_str),
            reply_markup=kb_day_picker(ch_id),
        )
        return

    await q.edit_message_text(
        "🎬 *{}*\n📅 {}\n{} programmes — tap one:".format(
            ch.get("name"), date_str, len(progs)),
        reply_markup=kb_programmes(ch_id, date_str, progs, page=page),
        parse_mode=ParseMode.MARKDOWN,
    )


async def start_job(q, ctx, ch, media, d, items, time_split_minutes=0):
    app = ctx.application
    bot_cfg = app.bot_data["bot_cfg"]
    max_jobs = bot_cfg.get("max_concurrent_jobs", 2)
    if app.bot_data.get("running_jobs", 0) >= max_jobs:
        await q.edit_message_text(
            "⏳ Server busy ({} jobs running). Try again later.".format(max_jobs),
            reply_markup=kb_back_main(),
        )
        return

    app.bot_data["running_jobs"] = app.bot_data.get("running_jobs", 0) + 1

    ch_name = ch.get("name", "ch")
    safe_ch = downloader.sanitize_filename(ch_name, 40)
    out_dir = OUT_ROOT / "{}_{}".format(d.isoformat(), safe_ch)
    out_dir.mkdir(parents=True, exist_ok=True)

    configured_max_bytes = int(bot_cfg.get("telegram_max_mb", 1900)) \
        * 1024 * 1024
    local_bot_api_url = bot_cfg.get("local_bot_api_url")
    upload_limit = LOCAL_BOT_API_MAX_UPLOAD_BYTES \
        if local_bot_api_url else TELEGRAM_MAX_UPLOAD_BYTES
    max_bytes = min(configured_max_bytes, upload_limit)
    doc_threshold = int(bot_cfg.get("upload_as_document_if_over_mb", 1800)) \
        * 1024 * 1024

    split_label = "no time split" if not time_split_minutes \
        else "every {} min".format(time_split_minutes)
    api_mode = "Local Bot API Server" if local_bot_api_url \
        else "hosted Bot API"
    await q.edit_message_text(
        "✅ *Queued*\n{} · {} programme(s)\nSplit: {}\n"
        "Upload cap: {:.0f} MiB/file ({})\nStarting…"
        .format(ch_name, len(items), split_label,
                max_bytes / (1024 * 1024), api_mode),
        parse_mode=ParseMode.MARKDOWN,
    )

    reporter = ProgressReporter(
        ctx.bot,
        q.message.chat_id,
        header="🚀 {} — {} programme(s)".format(ch_name, len(items)),
    )
    reporter_task = asyncio.create_task(reporter.run())

    cfg = load_cfg()
    loop = asyncio.get_running_loop()

    def worker():
        try:
            return run_download_job(cfg, ch, media, d, items, max_bytes,
                                    time_split_minutes, reporter, out_dir), None
        except Exception as e:
            log.exception("job failed")
            return [], e

    try:
        produced, err = await loop.run_in_executor(None, worker)
    finally:
        reporter.done()
        await reporter_task
        app.bot_data["running_jobs"] = max(
            0, app.bot_data.get("running_jobs", 1) - 1)

    if err:
        await q.message.chat.send_message("❌ Job failed: {}".format(err))
        _wipe_tree(out_dir)
        return

    if not produced:
        await q.message.chat.send_message("⚠️ No files produced.")
        _wipe_tree(out_dir)
        return

    await q.message.chat.send_message(
        "📤 Uploading {} file(s)…".format(len(produced)))

    uploaded_ok = 0
    uploaded_fail = 0
    pending_uploads = list(produced)
    ffmpeg = downloader.ensure_ffmpeg()
    while pending_uploads:
        p = pending_uploads.pop(0)
        try:
            size_bytes = p.stat().st_size
            size_mb = size_bytes / 1024 / 1024
            suffix = p.suffix.lower()
            if size_bytes > max_bytes:
                await q.message.chat.send_message(
                    "⏭ Skipped {} ({:.0f} MB, over cap)".format(p.name, size_mb))
                continue

            send_native = size_bytes <= doc_threshold
            with open(p, "rb") as f:
                if suffix == ".mp4" and send_native:
                    await q.message.chat.send_video(
                        video=f,
                        filename=p.name,
                        caption=p.name,
                        supports_streaming=True,
                    )
                elif suffix == ".m4a" and send_native:
                    await q.message.chat.send_audio(
                        audio=f,
                        filename=p.name,
                        caption=p.name,
                    )
                else:
                    await q.message.chat.send_document(
                        document=f,
                        filename=p.name,
                        caption="{} ({:.1f} MB)".format(p.name, size_mb),
                    )
            uploaded_ok += 1
        except Exception as e:
            if is_upload_size_error(e) and p.exists():
                size_bytes = p.stat().st_size
                retry_limit = min(max_bytes // 2, size_bytes // 2)
                if retry_limit > 0:
                    try:
                        smaller_parts = await asyncio.to_thread(
                            downloader.split_by_size_limit,
                            ffmpeg, p, out_dir, retry_limit)
                    except Exception as split_error:
                        log.exception("could not re-split rejected upload %s", p)
                        log.error("re-split failure: %s", split_error)
                    else:
                        if len(smaller_parts) > 1:
                            smaller_parts = downloader.number_parts(
                                smaller_parts, p)
                            try:
                                p.unlink()
                            except OSError:
                                log.warning("could not remove re-split source %s", p)
                            pending_uploads[:0] = smaller_parts
                            await q.message.chat.send_message(
                                "⚠️ Telegram rejected {} as too large; "
                                "retrying as {} smaller file(s).".format(
                                    p.name, len(smaller_parts)))
                            continue
            uploaded_fail += 1
            log.exception("upload failed for %s", p)
            await q.message.chat.send_message(
                "❌ upload failed for {}: {}".format(p.name, e))

    if bot_cfg.get("delete_everything_after_upload", True):
        _wipe_tree(out_dir)

    summary = "✅ Done — {} uploaded".format(uploaded_ok)
    if uploaded_fail:
        summary += ", {} failed".format(uploaded_fail)
    summary += "\n🗑 Files deleted from disk."
    await q.message.chat.send_message(summary, reply_markup=kb_main())


async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    awaiting = ctx.user_data.get("awaiting")
    text = (update.message.text or "").strip()

    if not awaiting:
        await update.message.reply_text(
            "Use the buttons above, or /download to start.",
            reply_markup=kb_main(),
        )
        return

    if awaiting == "channel_search":
        ctx.user_data["awaiting"] = None
        try:
            channels = ctx.user_data.get("all_channels") \
                or await asyncio.to_thread(load_all_channels_blocking)
        except Exception as e:
            await update.message.reply_text(
                "Failed to load channels: {}".format(e))
            return
        ctx.user_data["all_channels"] = channels

        term = text.strip()
        if not term or term == "*":
            matches = channels[:25]
            header = "📺 *First 25 channels* — tap one:"
        else:
            matches = search_channels(channels, term, limit=25)
            if not matches:
                await update.message.reply_text(
                    "❌ No channels match *{}*.\nTry another term.".format(term),
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("🔍 New search", callback_data="chsearch")],
                        [InlineKeyboardButton("⬅️ Back", callback_data="nav|ch")],
                    ]),
                    parse_mode=ParseMode.MARKDOWN,
                )
                return
            header = "🔍 *{} match(es) for “{}”* — tap one:".format(
                len(matches), term)

        await update.message.reply_text(
            header, reply_markup=kb_search_results(matches),
            parse_mode=ParseMode.MARKDOWN)
        return

    if awaiting == "date":
        ctx.user_data["awaiting"] = None
        ch_id = ctx.user_data.get("pending_ch_id")
        try:
            d = datetime.strptime(text, "%Y-%m-%d").date()
        except ValueError:
            await update.message.reply_text(
                "Use YYYY-MM-DD.",
                reply_markup=kb_day_picker(ch_id or ""),
            )
            return
        await show_programmes_msg(update, ctx, ch_id, d, page=0)
        return

    await update.message.reply_text("Okay.", reply_markup=kb_main())


async def show_programmes_msg(update, ctx, ch_id, d, page=0):
    ch = await _resolve_channel(ctx, ch_id)
    if not ch:
        await update.message.reply_text("Channel not found.")
        return

    date_str = d.isoformat()
    try:
        cfg = load_cfg()
        sess = stalker.Session(cfg)
        await asyncio.to_thread(stalker.auth, sess, True, log.info)
        progs = await asyncio.to_thread(
            stalker.fetch_epg, sess, ch_id, date_str, log.info)
    except Exception as e:
        await update.message.reply_text("EPG failed: {}".format(e))
        return

    progs.sort(key=lambda p: int(p["start_timestamp"]))
    ctx.user_data["programmes"] = {
        "ch_id": ch_id, "date": date_str, "list": progs, "channel": ch}
    if not progs:
        await update.message.reply_text(
            "No EPG for {} on {}.".format(ch.get("name"), date_str))
        return

    await update.message.reply_text(
        "🎬 *{}*\n📅 {}\n{} programmes — tap one:".format(
            ch.get("name"), date_str, len(progs)),
        reply_markup=kb_programmes(ch_id, date_str, progs, page=page),
        parse_mode=ParseMode.MARKDOWN,
    )


async def post_init(app):
    await app.bot.set_my_commands([
        BotCommand("download", "📺  Start downloading"),
        BotCommand("status", "Bot status"),
        BotCommand("cancel", "Cancel current session"),
        BotCommand("id", "Show my Telegram user ID"),
        BotCommand("help", "How to use"),
    ])


def main():
    bot_cfg = load_bot_cfg()
    token = bot_cfg["bot_token"]
    if not token or token == "YOUR_BOT_TOKEN_HERE":
        raise SystemExit("Set bot_token in bot_config.json")
    local_bot_api_url = bot_cfg.get("local_bot_api_url")
    if local_bot_api_url is not None:
        if not isinstance(local_bot_api_url, str) or not local_bot_api_url.strip():
            raise SystemExit("local_bot_api_url must be a non-empty URL")
        local_bot_api_url = local_bot_api_url.strip().rstrip("/")

    if bot_cfg.get("delete_everything_after_upload", True):
        try:
            _wipe_tree(OUT_ROOT)
        except Exception:
            pass

    upload_request = HTTPXRequest(
        connection_pool_size=8,
        connect_timeout=30,
        read_timeout=300,
        write_timeout=300,
        media_write_timeout=3600,
        pool_timeout=60,
    )
    builder = ApplicationBuilder().token(token).request(upload_request)
    if local_bot_api_url:
        builder = (
            builder
            .base_url(local_bot_api_url + "/bot")
            .base_file_url(local_bot_api_url + "/file/bot")
            .local_mode(True)
        )
    app = builder.post_init(post_init).build()
    app.bot_data["bot_cfg"] = bot_cfg
    app.bot_data["running_jobs"] = 0

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_start))
    app.add_handler(CommandHandler("download", cmd_download))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("id", cmd_id))
    app.add_handler(CommandHandler("cancel", cmd_cancel))

    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    log.info("polling…")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
