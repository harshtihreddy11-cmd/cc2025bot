#!/usr/bin/env python3
# downloader.py - Segment downloads + ffmpeg processing
# Tuned for cc2025.net archive CDN (flussonic/dvr, fMP4-capable).

import http.client
import json as _json
import re
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed

# STB UA - accepted by cc2025's archive CDN.
STB_UA = ("Mozilla/5.0 (QtEmbedded; U; Linux; C) AppleWebKit/533.3 "
          "(KHTML, like Gecko) MAG200 stbapp ver: 2 rev: 250 Safari/533.3")

DEFAULT_WORKERS = 24
DEFAULT_SEGMENT_TIMEOUT = 20

_segment_connections = threading.local()


# =============================================================
# Fast keep-alive segment fetcher
# =============================================================

def _segment_connection(scheme, netloc, timeout):
    connections = getattr(_segment_connections, "connections", None)
    if connections is None:
        connections = _segment_connections.connections = {}
    key = (scheme, netloc)
    conn = connections.get(key)
    if conn is None:
        cls = (http.client.HTTPSConnection if scheme == "https"
               else http.client.HTTPConnection)
        conn = connections[key] = cls(netloc, timeout=timeout)
    return conn, connections, key


def _parse_retry_after(headers, default):
    ra = headers.get("Retry-After")
    if not ra:
        return default
    try:
        return min(int(ra), 30)
    except ValueError:
        return default


def fetch_segment(url, timeout=DEFAULT_SEGMENT_TIMEOUT, attempts=3,
                  max_redirects=5, referer=None, host=None):
    """
    GET a segment with a persistent per-thread connection.
    Retries on network errors, 429 and 5xx; fails fast on other 4xx.
    Follows redirects.
    """
    cur = url
    for _ in range(max_redirects + 1):
        parsed = urllib.parse.urlparse(cur)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise RuntimeError("unsupported segment URL: {}".format(cur))
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query

        headers = {
            "User-Agent": STB_UA,
            "Accept": "*/*",
            "Connection": "keep-alive",
        }
        if referer:
            headers["Referer"] = referer
        if host:
            headers["Host"] = host

        last_error = None
        redirect_to = None

        for attempt in range(1, attempts + 1):
            conn, connections, key = _segment_connection(
                parsed.scheme, parsed.netloc, timeout)
            try:
                conn.request("GET", path, headers=headers)
                response = conn.getresponse()
                data = response.read()

                if response.status in (301, 302, 303, 307, 308):
                    location = response.getheader("Location")
                    if not location:
                        raise RuntimeError("redirect missing Location")
                    redirect_to = urllib.parse.urljoin(cur, location)
                    break

                if response.status == 429 or response.status >= 500:
                    delay = _parse_retry_after(response.headers,
                                               1.0 * attempt)
                    raise OSError("HTTP {} (retry in {:.1f}s)".format(
                        response.status, delay))

                if 400 <= response.status < 500:
                    raise RuntimeError("HTTP {} {}".format(
                        response.status, response.reason))

                if not data:
                    raise OSError("empty segment response")
                return data
            except RuntimeError:
                raise
            except (http.client.HTTPException, OSError) as exc:
                last_error = exc
                try:
                    conn.close()
                except Exception:
                    pass
                connections.pop(key, None)
                if attempt < attempts:
                    time.sleep(0.8 * attempt)
        else:
            raise RuntimeError("segment fetch failed: {} ({})".format(
                cur, last_error))

        if redirect_to:
            cur = redirect_to
            continue
    raise RuntimeError("too many segment redirects: {}".format(url))


def download_segment(sess, url, dst, timeout=DEFAULT_SEGMENT_TIMEOUT,
                     referer=None, host=None):
    # Clean stale .part from a previous crash.
    part = dst.with_name(dst.name + ".part")
    if part.exists():
        try:
            part.unlink()
        except Exception:
            pass
    if dst.exists() and dst.stat().st_size > 0:
        return dst.stat().st_size
    data = fetch_segment(url, timeout=timeout,
                         referer=referer, host=host)
    dst.parent.mkdir(parents=True, exist_ok=True)
    part.write_bytes(data)
    part.replace(dst)
    return len(data)


# =============================================================
# Segment downloader (parallel, with retry passes)
# =============================================================

def download_segments(
    sess,
    segments,
    out_dir,
    workers=DEFAULT_WORKERS,
    timeout=DEFAULT_SEGMENT_TIMEOUT,
    passes=3,
    referer=None,
    host=None,
    log=print,
):
    out_dir.mkdir(parents=True, exist_ok=True)
    n = len(segments)
    results = [None] * n
    total = 0
    t0 = time.time()
    pending = list(range(n))
    done_ok = 0
    failed_details = {}

    def job(i):
        seg = segments[i]
        url = seg["uri"]
        base = urllib.parse.urlparse(url).path.split("/")[-1]
        base = re.sub(r"[^A-Za-z0-9_.-]", "_", base)
        if not (base.endswith(".ts") or base.endswith(".m4s")
                or base.endswith(".mp4") or base.endswith(".aac")):
            base = "seg.bin"
        dst = out_dir / "{:06d}_{}".format(i, base)
        try:
            sz = download_segment(sess, url, dst, timeout=timeout,
                                  referer=referer, host=host)
            return i, dst, sz, None
        except Exception as e:
            return i, dst, 0, str(e)

    for pass_no in range(1, passes + 1):
        if not pending:
            break
        if pass_no > 1:
            log("  retry pass {}/{}: {} segment(s) left".format(
                pass_no, passes, len(pending)))
            time.sleep(2)
        pass_workers = workers if pass_no == 1 else max(2, workers // 3)
        failed = []
        with ThreadPoolExecutor(max_workers=pass_workers) as ex:
            futs = [ex.submit(job, i) for i in pending]
            for fut in as_completed(futs):
                i, dst, sz, err = fut.result()
                if err:
                    failed.append(i)
                    failed_details[i] = (dst.name, err)
                    continue
                total += sz
                results[i] = dst
                failed_details.pop(i, None)
                done_ok += 1
                if done_ok % 10 == 0 or done_ok == n:
                    el = max(time.time() - t0, 0.001)
                    log("  [{:03d}/{:03d}] {:.1f} MiB  {:.2f} MiB/s".format(
                        done_ok, n, total / 1024 / 1024,
                        total / 1024 / 1024 / el))
        pending = sorted(failed)

    if pending:
        examples = "; ".join(
            "{}: {}".format(*failed_details[i]) for i in pending[:3])
        log("  ! {} segment(s) permanently failed ({})".format(
            len(pending), examples))

    return [p for p in results if p], total


# =============================================================
# Helpers
# =============================================================

def sanitize_filename(name, max_len=120):
    name = re.sub(r"[\\/:*?\"<>|]+", "_", name)
    name = re.sub(r"\s+", " ", name).strip()
    name = name.strip(" .")
    if len(name) > max_len:
        name = name[:max_len].rstrip()
    return name or "programme"


def unique_path(path):
    if not path.exists():
        return path
    stem, suffix = path.stem, path.suffix
    i = 2
    while True:
        candidate = path.with_name("{}_{}{}".format(stem, i, suffix))
        if not candidate.exists():
            return candidate
        i += 1


def concat_ts(paths, dst_ts):
    with open(dst_ts, "wb") as w:
        for p in paths:
            with open(p, "rb") as r:
                shutil.copyfileobj(r, w, 1024 * 1024)


def _run_ffmpeg(cmd):
    result = subprocess.run(cmd, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE)
    if result.returncode != 0:
        err = result.stderr.decode("utf-8", "replace")
        tail = "\n".join(err.strip().splitlines()[-6:])
        raise RuntimeError("ffmpeg failed:\n" + tail)


def remux_ts_to_mp4(ffmpeg, ts_path, mp4_path):
    _run_ffmpeg([
        ffmpeg, "-y",
        "-fflags", "+genpts+igndts",
        "-i", str(ts_path),
        "-c", "copy",
        "-bsf:a", "aac_adtstoasc",
        "-movflags", "+faststart",
        str(mp4_path),
    ])


def extract_audio_to_m4a(ffmpeg, ts_path, m4a_path):
    """TS -> M4A, stream copy first, re-encode on failure."""
    copy_cmd = [
        ffmpeg, "-y", "-loglevel", "error",
        "-i", str(ts_path),
        "-vn", "-c:a", "copy",
        "-bsf:a", "aac_adtstoasc",
        "-movflags", "+faststart",
        str(m4a_path),
    ]
    try:
        _run_ffmpeg(copy_cmd)
        return "copy"
    except RuntimeError:
        pass
    enc_cmd = [
        ffmpeg, "-y", "-loglevel", "error",
        "-i", str(ts_path),
        "-vn", "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart",
        str(m4a_path),
    ]
    _run_ffmpeg(enc_cmd)
    return "encode"


def extract_audio_from_segments(ffmpeg, segment_paths, m4a_path):
    """
    Pipe .ts segments straight into ffmpeg (stream copy of the audio).
    Raises RuntimeError on failure so the caller can fall back.
    """
    cmd = [
        ffmpeg, "-y", "-loglevel", "error",
        "-f", "mpegts", "-i", "pipe:0",
        "-vn", "-c:a", "copy",
        "-bsf:a", "aac_adtstoasc",
        "-movflags", "+faststart",
        str(m4a_path),
    ]
    with tempfile.TemporaryFile() as error_file:
        process = subprocess.Popen(
            cmd, stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL, stderr=error_file,
        )
        try:
            for segment_path in segment_paths:
                with open(segment_path, "rb") as source:
                    shutil.copyfileobj(source, process.stdin, 1024 * 1024)
        except (BrokenPipeError, OSError):
            pass
        finally:
            if process.stdin:
                try:
                    process.stdin.close()
                except (BrokenPipeError, OSError):
                    pass
        if process.wait() != 0:
            error_file.seek(0)
            error = error_file.read().decode("utf-8", "replace")
            tail = "\n".join(error.strip().splitlines()[-6:])
            if m4a_path.exists():
                m4a_path.unlink()
            raise RuntimeError("ffmpeg pipe audio copy failed:\n" + tail)


def probe_duration_seconds(ffmpeg, src_path):
    ffprobe = shutil.which("ffprobe")
    if ffprobe:
        cmd = [
            ffprobe, "-v", "error",
            "-show_entries", "format=duration",
            "-of", "json",
            str(src_path),
        ]
        result = subprocess.run(cmd, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE)
        if result.returncode == 0:
            try:
                data = _json.loads(result.stdout.decode("utf-8", "replace"))
                return float(data["format"]["duration"])
            except Exception:
                pass
    cmd = [ffmpeg, "-i", str(src_path)]
    result = subprocess.run(cmd, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE)
    err = result.stderr.decode("utf-8", "replace")
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.\d+)", err)
    if not m:
        raise RuntimeError("could not determine duration of {}".format(
            src_path))
    h, mi, s = int(m.group(1)), int(m.group(2)), float(m.group(3))
    return h * 3600 + mi * 60 + s


def split_by_size_limit(ffmpeg, src_path, out_dir, max_bytes, headroom=0.95):
    size = src_path.stat().st_size
    if size <= max_bytes:
        return [src_path]

    duration = probe_duration_seconds(ffmpeg, src_path)
    if duration <= 0:
        raise RuntimeError("invalid duration for split")

    bps = size / duration
    target_bytes = max_bytes * headroom
    segment_seconds = max(60, int(target_bytes / bps))

    ext = src_path.suffix
    stem = src_path.stem
    pattern = str(out_dir / "{}_part_%03d{}".format(stem, ext))
    _run_ffmpeg([
        ffmpeg, "-y",
        "-i", str(src_path),
        "-c", "copy",
        "-map", "0",
        "-f", "segment",
        "-segment_time", str(segment_seconds),
        "-reset_timestamps", "1",
        "-avoid_negative_ts", "make_zero",
        pattern,
    ])
    parts = sorted(out_dir.glob("{}_part_*{}".format(stem, ext)))
    if not parts:
        raise RuntimeError("split produced no parts")
    return parts


def split_by_duration(ffmpeg, src_path, out_dir, segment_seconds):
    if segment_seconds <= 0:
        return [src_path]

    ext = src_path.suffix
    stem = src_path.stem
    pattern = str(out_dir / "{}_tpart_%03d{}".format(stem, ext))
    _run_ffmpeg([
        ffmpeg, "-y",
        "-loglevel", "error",
        "-i", str(src_path),
        "-c", "copy",
        "-map", "0",
        "-f", "segment",
        "-segment_time", str(int(segment_seconds)),
        "-reset_timestamps", "1",
        "-avoid_negative_ts", "make_zero",
        pattern,
    ])
    parts = sorted(out_dir.glob("{}_tpart_*{}".format(stem, ext)))
    if not parts:
        raise RuntimeError("time-split produced no parts")
    return parts


def ensure_ffmpeg():
    ff = shutil.which("ffmpeg")
    if not ff:
        raise RuntimeError("ffmpeg not found on PATH")
    return ff
