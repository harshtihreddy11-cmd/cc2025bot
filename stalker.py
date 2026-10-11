#!/usr/bin/env python3
# stalker.py - Stalker portal session, auth, EPG, archive-link logic
# Tuned for cc2025.net (Ministra/Stalker panel).

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))

# STB UA - used for portal AND for the archive CDN when no auth is needed.
STB_UA = ("Mozilla/5.0 (QtEmbedded; U; Linux; C) AppleWebKit/533.3 "
          "(KHTML, like Gecko) MAG200 stbapp ver: 2 rev: 250 Safari/533.3")


# =============================================================
# Session
# =============================================================

class Session:
    def __init__(self, cfg):
        self.cfg = cfg
        self.token = None
        self.random = None
        self.profile = {}
        self._profile_ok = False

    # ---------- headers ----------

    def _portal_headers(self, extra=None):
        mac = urllib.parse.quote(self.cfg["mac"], safe="")
        tz = urllib.parse.quote(self.cfg.get("timezone", "Europe/Paris"),
                                safe="")
        h = {
            "User-Agent": self.cfg["user_agent"],
            "X-User-Agent": self.cfg["x_user_agent"],
            "Referer": self.cfg["referer"],
            "Accept": "*/*",
            "Pragma": "no-cache",
            "Host": self.cfg["host"],
            "Cookie": "mac={}; stb_lang={}; timezone={}".format(
                mac, self.cfg.get("stb_lang", "en"), tz),
        }
        if self.token:
            h["Authorization"] = "Bearer " + self.token
        if extra:
            h.update(extra)
        return h

    def _cdn_headers(self, extra=None):
        """
        Headers for the archive CDN (flussonic/dvr).
        Do NOT send the portal Cookie/Authorization - many CDNs 403 on
        unknown cookies. The token is embedded in the archive URL.
        """
        h = {
            "User-Agent": STB_UA,
            "Accept": "*/*",
            "Connection": "keep-alive",
        }
        if extra:
            h.update(extra)
        return h

    # ---------- POST (portal API) ----------

    def post(self, params, retries=3, timeout=30):
        params = dict(params)
        params.setdefault("JsHttpRequest", "1-xml")
        body = urllib.parse.urlencode(params).encode()
        # cc2025 accepts the JsHttpRequest param on the URL OR in the body.
        # Putting it on the URL is what the STB firmware does.
        url = self.cfg["portal"] + "?JsHttpRequest=1-xml"
        last = None
        for attempt in range(1, retries + 1):
            try:
                req = urllib.request.Request(
                    url,
                    data=body,
                    method="POST",
                    headers=self._portal_headers({
                        "Content-Type":
                            "application/x-www-form-urlencoded; charset=utf-8",
                    }),
                )
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    raw = r.read()
                text = raw.decode("utf-8", errors="replace")
                try:
                    return json.loads(text)
                except json.JSONDecodeError:
                    return {"_raw": text}
            except Exception as e:
                last = e
                if attempt < retries:
                    time.sleep(1.5 * attempt)
        raise RuntimeError("portal POST failed: {}".format(last))

    # ---------- GET (playlists) ----------

    def get(self, url, extra_headers=None, retries=3, timeout=30,
            use_auth=False):
        """
        GET a playlist. Default use_auth=False because archive/EPG m3u8s
        live on the CDN and reject portal cookies.
        """
        headers = self._portal_headers(extra_headers) if use_auth \
            else self._cdn_headers(extra_headers)
        last = None
        for attempt in range(1, retries + 1):
            try:
                req = urllib.request.Request(url, headers=headers)
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    return r.read(), r.status
            except urllib.error.HTTPError as e:
                # 4xx (except 429) are permanent.
                if 400 <= e.code < 500 and e.code != 429:
                    body = ""
                    try:
                        body = e.read().decode("utf-8", "replace")[:200]
                    except Exception:
                        pass
                    raise RuntimeError(
                        "HTTP {} {} {} for {}".format(
                            e.code, e.reason, body, url))
                last = e
                if attempt < retries:
                    time.sleep(1.5 * attempt)
            except Exception as e:
                last = e
                if attempt < retries:
                    time.sleep(1.5 * attempt)
        raise RuntimeError("GET failed: {} ({})".format(url, last))


# =============================================================
# Auth
# =============================================================

def handshake(sess, max_attempts=8, wait_between=8, log=print):
    last = None
    for attempt in range(1, max_attempts + 1):
        r = sess.post({"type": "stb", "action": "handshake",
                       "token": "", "prehash": 0})
        js = r.get("js")
        if isinstance(js, dict) and js.get("token"):
            sess.token = js.get("token", "")
            sess.random = js.get("random", "")
            sess._profile_ok = False
            return js
        last = r
        if attempt < max_attempts:
            log("[~] handshake empty (attempt {}/{}), waiting {}s...".format(
                attempt, max_attempts, wait_between))
            time.sleep(wait_between)
    raise RuntimeError(
        "handshake failed after {} attempts: {}\n"
        "  Another device may be using this MAC. Close any browser tab "
        "on the portal, kill stray players, wait 60s, retry."
        .format(max_attempts, last))


def get_profile(sess):
    metrics = {
        "mac": sess.cfg["mac"], "sn": sess.cfg["sn"], "type": "STB",
        "model": sess.cfg["stb_type"], "uid": "", "random": sess.random or "",
    }
    params = {
        "type": "stb", "action": "get_profile", "hd": "1",
        "ver": ("ImageDescription: 0.2.18-r23-250; "
                "ImageDate: Wed Aug 29 10:49:53 EEST 2018; "
                "PORTAL version: 5.6.1; "
                "API Version: JS API version: 343; "
                "STB API version: 146; Player Engine version: 0x58c"),
        "num_banks": "2", "sn": sess.cfg["sn"],
        "stb_type": sess.cfg["stb_type"], "client_type": "STB",
        "image_version": sess.cfg.get("image_version", "218"),
        "video_out": "hdmi",
        "device_id": sess.cfg["device_id"],
        "device_id2": sess.cfg["device_id2"],
        "signature": sess.cfg["signature"],
        "auth_second_step": "1", "hw_version": sess.cfg["hw_version"],
        "not_valid_token": "0",
        "metrics": json.dumps(metrics, separators=(",", ":")),
        "hw_version_2": sess.cfg.get("hw_version_2", "292"),
        "timestamp": int(time.time()),
        "api_signature": "262", "prehash": "0",
    }
    r = sess.post(params)
    js = r.get("js")
    if isinstance(js, dict) and js.get("mac"):
        sess.profile = js
        sess._profile_ok = True
    return r


def auth(sess, force=False, log=print):
    if sess.token and sess._profile_ok and not force:
        return
    handshake(sess, log=log)
    prof = get_profile(sess)
    js = prof.get("js")
    if not (isinstance(js, dict) and js.get("mac")):
        log("[~] profile refused - waiting 10s and retrying handshake")
        time.sleep(10)
        handshake(sess, max_attempts=4, wait_between=8, log=log)
        get_profile(sess)


def call(sess, params, context="", log=print):
    if not sess.token or not sess._profile_ok:
        auth(sess, log=log)
    r = sess.post(params)
    raw = r.get("_raw", "") if isinstance(r, dict) else ""
    if raw and ("Authorization failed" in raw or "Access denied" in raw
                or "not authorized" in raw.lower()):
        log("[~] {}: re-auth".format(context))
        auth(sess, force=True, log=log)
        r = sess.post(params)
    return r


# =============================================================
# Warmup
# =============================================================

def warmup(sess, ch_id, cmd_id=None, log=print):
    if cmd_id is None:
        cmd_id = ch_id
    log("[*] warmup: account_info")
    call(sess, {"type": "account_info", "action": "get_main_info"},
         context="account_info", log=log)
    log("[*] warmup: itv/get_genres")
    call(sess, {"type": "itv", "action": "get_genres"},
         context="genres", log=log)
    log("[*] warmup: itv/get_all_channels")
    call(sess, {"type": "itv", "action": "get_all_channels"},
         context="all_channels", log=log)
    log("[*] warmup: itv/create_link cmd_id={}".format(cmd_id))
    call(sess, {"type": "itv", "action": "create_link",
                "cmd": "ffrt http://localhost/ch/{}".format(cmd_id),
                "series": "0", "forced_storage": "0",
                "disable_ad": "0", "download": "0",
                "force_ch_link_check": "0"},
         context="create_link({})".format(cmd_id), log=log)
    log("[*] warmup: itv/get_short_epg ch_id={}".format(ch_id))
    call(sess, {"type": "itv", "action": "get_short_epg",
                "ch_id": str(ch_id), "size": "10"},
         context="short_epg", log=log)


# =============================================================
# Channels
# =============================================================

def load_channels(sess, cache_path, refresh=False, log=print):
    if cache_path.exists() and not refresh:
        return json.loads(cache_path.read_text())
    r = call(sess, {"type": "itv", "action": "get_all_channels",
                    "force_ch_link_check": "0"},
             context="channels", log=log)
    if "_raw" in r:
        raise RuntimeError("channels refused: {}".format(r["_raw"][:200]))
    cache_path.write_text(json.dumps(r, indent=2))
    return r


def iter_channels(listing):
    js = listing.get("js", {})
    data = js.get("data", []) if isinstance(js, dict) else (js or [])
    return iter(data)


def channel_id_of(ch):
    return str(ch.get("id"))


def cmd_id_of(ch):
    cmds = ch.get("cmds") or []
    if cmds:
        return str(cmds[0].get("id"))
    return str(ch.get("id"))


# =============================================================
# EPG
# =============================================================

def fetch_epg(sess, ch_id, date_str, log=print):
    progs = []
    page = 1
    per_page = 10
    total = None
    while page <= 200:
        r = call(sess, {"type": "epg", "action": "get_simple_data_table",
                        "ch_id": str(ch_id), "date": date_str,
                        "p": str(page)},
                 context="epg(p{})".format(page), log=log)
        js = r.get("js") or {}
        if not isinstance(js, dict):
            break
        data = js.get("data") or []
        if total is None:
            total = int(js.get("total_items") or 0)
            per_page = int(js.get("max_page_items") or per_page) or per_page
        if not data:
            break
        progs.extend(data)
        if total and page * per_page >= total:
            break
        page += 1
    return progs


# =============================================================
# Archive link
# =============================================================

def create_archive_link(sess, ch_id, epg_id, log=print):
    """
    cc2025 panels return the full archive URL in js['cmd'] for newer
    panels. Older panels return 'auto /media/<id>.mpg' and the URL is
    in js['link'] / js['url'] / js['archive_url'] / js['id'].
    """
    r = call(sess, {
        "type": "tv_archive", "action": "create_link",
        "cmd": "auto /media/{}.mpg".format(epg_id),
        "series": "0", "forced_storage": "0",
        "disable_ad": "0", "download": "0",
        "force_ch_link_check": "0",
    }, context="archive({})".format(epg_id), log=log)
    js = r.get("js") or {}
    if not isinstance(js, dict):
        return "", js

    url = js.get("cmd") or ""
    if url and not url.startswith("http"):
        # legacy portal - try alternate keys
        for key in ("link", "url", "archive_url", "id"):
            v = js.get(key) or ""
            if isinstance(v, str) and v.startswith("http"):
                url = v
                break

    # Some cc2025 panels return the URL under js['cmd'] as
    # 'ffmpeg http://...' or 'auto http://...' - strip the verb.
    if url and not url.startswith("http"):
        m = re.search(r"(https?://\S+)", url)
        if m:
            url = m.group(1)

    return url, js


# =============================================================
# m3u8 parsing (handles fMP4 + master playlists)
# =============================================================

def parse_m3u8(text, base_url):
    """
    Returns (variants, init_segments, media_segments, is_master).
    """
    variants = []
    segments = []
    init_segments = []
    cur_dur = None
    cur_pdt = None
    is_master = False

    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("#EXT-X-STREAM-INF"):
            is_master = True
            variants.append({"inf": line, "uri": None})
        elif line.startswith("#EXT-X-MAP"):
            m = re.search(r'URI="([^"]+)"', line)
            if m:
                uri = m.group(1)
                url = uri if uri.startswith("http") \
                    else urllib.parse.urljoin(base_url, uri)
                init_segments.append(url)
        elif line.startswith("#EXTINF"):
            m = re.search(r"#EXTINF:([\d.]+)", line)
            cur_dur = float(m.group(1)) if m else None
        elif line.startswith("#EXT-X-PROGRAM-DATE-TIME"):
            m = re.search(r":(\S+)", line)
            cur_pdt = m.group(1) if m else None
        elif line.startswith("#"):
            continue
        else:
            url = line if line.startswith("http") \
                else urllib.parse.urljoin(base_url, line)
            if variants and variants[-1]["uri"] is None:
                variants[-1]["uri"] = url
            else:
                segments.append({"uri": url, "duration": cur_dur,
                                 "pdt": cur_pdt})
                cur_dur = None
                cur_pdt = None

    return variants, init_segments, segments, is_master


def pdt_to_epoch(s):
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return int(dt.timestamp())
    except Exception:
        return None


def _variant_bw(v):
    m = re.search(r"BANDWIDTH=(\d+)", v["inf"])
    return int(m.group(1)) if m else 0


def _is_audio_only(v):
    inf = v["inf"]
    if "RESOLUTION=" in inf:
        return False
    m = re.search(r'CODECS="([^"]*)"', inf)
    if m:
        codecs = m.group(1).lower()
        has_video = any(c in codecs for c in
                        ("avc", "hvc", "hev", "vp9", "av01", "mp4v"))
        return ("mp4a" in codecs) and not has_video
    return False


def pick_variant(variants, quality="lowest", want_audio=False):
    vs = [v for v in variants if v["uri"]]
    if not vs:
        return None
    if want_audio:
        audio = [v for v in vs if _is_audio_only(v)]
        if audio:
            return min(audio, key=_variant_bw)
        return min(vs, key=_variant_bw)
    return (min if quality == "lowest" else max)(vs, key=_variant_bw)


# =============================================================
# Archive fetch
# =============================================================

def fetch_archive_segments(
    sess,
    ch_id,
    prog,
    sub_start,
    sub_end,
    log=print,
    quality="lowest",
    want_audio=False,
):
    """
    Resolve the archive m3u8 for one EPG record and return
    (segments_to_download, prog_abs_start, init_segments).
    """
    epg_id = prog.get("id")
    if not epg_id:
        raise RuntimeError("no epg id")

    auth(sess, log=log)
    archive_url, js = create_archive_link(sess, ch_id, epg_id, log=log)
    if not archive_url:
        log("[~] no archive url, re-auth and retry")
        auth(sess, force=True, log=log)
        archive_url, js = create_archive_link(sess, ch_id, epg_id, log=log)
    if not archive_url:
        raise RuntimeError("no archive url: {}".format(js))

    log("archive: {}...".format(archive_url[:120]))

    # CDN fetch - no portal auth.
    raw, _ = sess.get(archive_url, use_auth=False)
    text = raw.decode("utf-8", errors="replace")

    variants, init_segs, direct_segs, is_master = parse_m3u8(text, archive_url)

    if is_master and any(v["uri"] for v in variants):
        for v in variants:
            if v["uri"]:
                log("  variant: {}".format(
                    v["inf"].replace("#EXT-X-STREAM-INF:", "")[:90]))
        chosen = pick_variant(variants, quality, want_audio)
        if not chosen:
            raise RuntimeError("archive playlist has no usable variants")
        log("  chosen: bandwidth={}{}".format(
            _variant_bw(chosen),
            " (audio-only)" if _is_audio_only(chosen) else ""))
        vurl = chosen["uri"]
        vraw, _ = sess.get(vurl, use_auth=False)
        vtext = vraw.decode("utf-8", errors="replace")
        _, init_segs, segs, _ = parse_m3u8(vtext, vurl)
    elif direct_segs:
        segs = direct_segs
        log("  archive URL is a media playlist (no variants)")
    else:
        log("  ! no segments in archive m3u8. First 300 chars:")
        log(text[:300])
        raise RuntimeError("archive playlist empty")

    log("  {} segments in playlist".format(len(segs)))
    if not segs:
        raise RuntimeError("variant playlist empty")

    # Anchor time
    prog_abs_start = int(prog["start_timestamp"])
    first_pdt = next((pdt_to_epoch(s["pdt"]) for s in segs
                      if pdt_to_epoch(s["pdt"])), None)
    if first_pdt:
        prog_abs_start = first_pdt
        log("  anchor: EXT-X-PROGRAM-DATE-TIME")
    else:
        m = re.search(r"[?&]start=(\d+)", archive_url)
        if m:
            prog_abs_start = int(prog["start_timestamp"]) + int(m.group(1))
            log("  anchor: start= param offset")

    start_dt = datetime.fromtimestamp(prog_abs_start, IST)
    log("  first segment at {:%Y-%m-%d %H:%M:%S} IST".format(start_dt))

    want = []
    t = prog_abs_start
    for s in segs:
        dur = s.get("duration") or 9.6
        if t + dur > sub_start and t < sub_end:
            want.append(s)
        t += dur

    log("  segments in window: {}".format(len(want)))
    if not want:
        raise RuntimeError("no segments in requested window")

    return want, prog_abs_start, init_segs
