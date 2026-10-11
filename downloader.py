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
from pathlib import Path

STB_UA = ("Mozilla/5.0 (QtEmbedded; U; Linux; C) AppleWebKit/533.3 "
          "(KHTML, like Gecko) MAG200 stbapp ver: 2 rev: 250 Safari/533.3")

DEFAULT_WORKERS = 24
DEFAULT_SEGMENT_TIMEOUT = 20

_segment_connections = threading.local()


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


def number_parts(parts, base_path):
    parts = list(parts)
    if len(parts) < 2:
        return parts

    numbered = []
    for index, part in enumerate(parts, 1):
        destination = unique_path(base_path.with_name(
            "{}_part_{:03d}{}".format(
                base_path.stem, index, part.suffix)))
        if part != destination:
            part.replace(destination)
        numbered.append(destination)
    return numbered


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


def has_audio_stream(src_path):
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        raise RuntimeError("ffprobe not found on PATH")
    result = subprocess.run([
        ffprobe, "-v", "error",
        "-select_streams", "a:0",
        "-show_entries", "stream=index",
        "-of", "csv=p=0",
        str(src_path),
    ], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode != 0:
        error = result.stderr.decode("utf-8", "replace").strip()
        raise RuntimeError("ffprobe failed: {}".format(error))
    return bool(result.stdout.strip())


def probe_video_metadata(src_path):
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        raise RuntimeError("ffprobe not found on PATH")
    result = subprocess.run([
        ffprobe, "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height,duration:format=duration",
        "-of", "json",
        str(src_path),
    ], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode != 0:
        error = result.stderr.decode("utf-8", "replace").strip()
        raise RuntimeError("ffprobe failed: {}".format(error))
    try:
        data = _json.loads(result.stdout.decode("utf-8", "replace"))
        stream = data["streams"][0]
        duration = float(stream.get("duration") or data["format"]["duration"])
        width = int(stream["width"])
        height = int(stream["height"])
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise RuntimeError(
            "could not determine video duration and dimensions for {}".format(
                src_path)) from exc
    if duration <= 0 or width <= 0 or height <= 0:
        raise RuntimeError("invalid video metadata for {}".format(src_path))
    return duration, width, height


def extract_video_thumbnail(ffmpeg, src_path, thumbnail_path, timestamp):
    _run_ffmpeg([
        ffmpeg, "-y", "-loglevel", "error",
        "-ss", str(max(0, float(timestamp))),
        "-i", str(src_path),
        "-frames:v", "1",
        "-vf", "scale=320:320:force_original_aspect_ratio=decrease",
        "-q:v", "4",
        str(thumbnail_path),
    ])
    if not thumbnail_path.exists() or thumbnail_path.stat().st_size == 0:
        raise RuntimeError("ffmpeg produced no thumbnail for {}".format(
            src_path))


def _concat_media_parts(ffmpeg, parts, destination):
    with tempfile.TemporaryDirectory(
            prefix=".concat_", dir=destination.parent) as tmp:
        concat_list = Path(tmp) / "parts.txt"
        concat_list.write_text("".join(
            "file '{}'\n".format(
                str(part).replace("\\", "\\\\").replace("'", "\\'"))
            for part in parts
        ), encoding="utf-8")
        _run_ffmpeg([
            ffmpeg, "-y", "-loglevel", "error",
            "-f", "concat", "-safe", "0", "-i", str(concat_list),
            "-map", "0", "-c", "copy",
            "-movflags", "+faststart", str(destination),
        ])


def split_by_size_limit(ffmpeg, src_path, out_dir, max_bytes, headroom=0.95,
                        log=print):
    if max_bytes <= 0:
        raise ValueError("max_bytes must be positive")
    if not 0 < headroom < 1:
        raise ValueError("headroom must be between 0 and 1")
    if src_path.stat().st_size <= max_bytes:
        return [src_path]

    out_dir.mkdir(parents=True, exist_ok=True)
    queue = [(src_path, 0, False)]
    ready = []
    split_count = 0

    with tempfile.TemporaryDirectory(prefix=".size_split_", dir=out_dir) as tmp:
        tmp_dir = Path(tmp)

        while queue:
            current, depth, is_temporary = queue.pop()
            size = current.stat().st_size
            if size <= max_bytes:
                ready.append(current)
                continue
            if depth >= 12:
                raise RuntimeError(
                    "could not split {} below {} bytes".format(
                        current.name, max_bytes))

            duration = probe_duration_seconds(ffmpeg, current)
            if duration <= 0:
                raise RuntimeError("invalid duration for split")
            target_bytes = min(max_bytes * headroom, size / 2)
            segment_seconds = max(
                1, int(duration * target_bytes / size))

            pattern = tmp_dir / "split_{:04d}_part_%03d{}".format(
                split_count, current.suffix)

            def make_parts(reencode_video=False):
                cmd = [ffmpeg, "-y", "-loglevel", "error",
                       "-i", str(current)]
                if reencode_video:
                    cmd.extend([
                        "-map", "0:v:0?", "-map", "0:a:0?", "-sn",
                        "-c:v", "libx264", "-preset", "veryfast",
                        "-crf", "20",
                        "-force_key_frames",
                        "expr:gte(t,n_forced*{})".format(segment_seconds),
                        "-c:a", "aac", "-b:a", "192k",
                    ])
                else:
                    cmd.extend(["-c", "copy", "-map", "0"])
                cmd.extend([
                    "-f", "segment",
                    "-segment_time", str(segment_seconds),
                    "-reset_timestamps", "1",
                    "-avoid_negative_ts", "make_zero",
                    "-segment_format_options", "movflags=+faststart",
                    str(pattern),
                ])
                _run_ffmpeg(cmd)
                return sorted(tmp_dir.glob(
                    "split_{:04d}_part_*{}".format(
                        split_count, current.suffix)))

            copy_error = None
            try:
                parts = make_parts()
            except RuntimeError as error:
                if current.suffix.lower() != ".mp4":
                    raise
                copy_error = error
                parts = []

            split_failed = len(parts) < 2 or any(
                part.stat().st_size >= size for part in parts)
            if split_failed and current.suffix.lower() == ".mp4":
                log("  ⚠️ keyframe-aligned size split failed; re-encoding "
                    "{}".format(current.name))
                for part in parts:
                    part.unlink()
                for part in tmp_dir.glob(
                        "split_{:04d}_part_*{}".format(
                            split_count, current.suffix)):
                    part.unlink()
                parts = make_parts(reencode_video=True)

            split_count += 1
            if len(parts) < 2 or any(p.stat().st_size >= size for p in parts):
                if copy_error and not parts:
                    raise RuntimeError(
                        "stream-copy and re-encoded size splitting failed "
                        "for {}".format(current.name)) from copy_error
                raise RuntimeError(
                    "ffmpeg could not reduce oversized file {}".format(
                        current.name))

            if is_temporary:
                current.unlink()
            queue.extend((part, depth + 1, True) for part in reversed(parts))

        coalesced = []
        merge_count = 0
        for part in ready:
            if coalesced \
                    and coalesced[-1].stat().st_size + part.stat().st_size \
                    <= max_bytes:
                previous = coalesced[-1]
                merged = tmp_dir / "merged_{:04d}{}".format(
                    merge_count, src_path.suffix)
                merge_count += 1
                _concat_media_parts(ffmpeg, [previous, part], merged)
                if merged.stat().st_size <= max_bytes:
                    previous.unlink()
                    part.unlink()
                    coalesced[-1] = merged
                    continue
                merged.unlink()
            coalesced.append(part)

        result = []
        for index, part in enumerate(coalesced, 1):
            if not part.is_relative_to(tmp_dir):
                result.append(part)
                continue
            destination = unique_path(out_dir / "{}_sizepart_{:03d}{}".format(
                src_path.stem, index, src_path.suffix))
            part.replace(destination)
            result.append(destination)

    return result


def split_by_duration(ffmpeg, src_path, out_dir, segment_seconds, log=print):
    if segment_seconds <= 0:
        return [src_path]
    if probe_duration_seconds(ffmpeg, src_path) <= segment_seconds:
        return [src_path]

    ext = src_path.suffix
    stem = src_path.stem
    out_dir.mkdir(parents=True, exist_ok=True)
    tolerance = max(5, min(30, int(segment_seconds * 0.01)))

    with tempfile.TemporaryDirectory(prefix=".time_split_", dir=out_dir) as tmp:
        tmp_dir = Path(tmp)

        def make_parts(reencode_video=False):
            pattern = tmp_dir / "time_part_%03d{}".format(ext)
            cmd = [ffmpeg, "-y", "-loglevel", "error", "-i", str(src_path)]
            if reencode_video:
                cmd.extend([
                    "-map", "0:v:0?", "-map", "0:a:0?", "-sn",
                    "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                    "-force_key_frames",
                    "expr:gte(t,n_forced*{})".format(int(segment_seconds)),
                    "-c:a", "aac", "-b:a", "192k",
                ])
            else:
                cmd.extend(["-c", "copy", "-map", "0"])
            cmd.extend([
                "-f", "segment",
                "-segment_time", str(int(segment_seconds)),
                "-reset_timestamps", "1",
                "-avoid_negative_ts", "make_zero",
                "-segment_format_options", "movflags=+faststart",
                str(pattern),
            ])
            _run_ffmpeg(cmd)
            return sorted(tmp_dir.glob("time_part_*{}".format(ext)))

        copy_error = None
        try:
            parts = make_parts()
        except RuntimeError as error:
            if ext.lower() != ".mp4":
                raise
            copy_error = error
            parts = []

        if ext.lower() == ".mp4" and len(parts) < 2:
            log("  ⚠️ video time-split failed in stream-copy mode; "
                "re-encoding {}".format(src_path.name))
            for part in tmp_dir.glob("time_part_*{}".format(ext)):
                part.unlink()
            parts = make_parts(reencode_video=True)
        if not parts:
            raise RuntimeError("time-split produced no parts")
        if len(parts) < 2:
            error = RuntimeError(
                "time-split did not split {} despite its duration exceeding "
                "{} seconds".format(src_path.name, int(segment_seconds)))
            if copy_error:
                raise error from copy_error
            raise error

        durations = [probe_duration_seconds(ffmpeg, part) for part in parts]
        if ext.lower() == ".mp4" and any(
                duration > segment_seconds + tolerance
                for duration in durations):
            log("  ⚠️ video time-split exceeded its duration; re-encoding "
                "{}".format(src_path.name))
            for part in parts:
                part.unlink()
            for part in tmp_dir.glob("time_part_*{}".format(ext)):
                part.unlink()
            parts = make_parts(reencode_video=True)
            if len(parts) < 2:
                raise RuntimeError(
                    "accurate time-split produced fewer than two parts for "
                    "{}".format(src_path.name))
            durations = [probe_duration_seconds(ffmpeg, part) for part in parts]

        if any(duration > segment_seconds + tolerance for duration in durations):
            raise RuntimeError(
                "time-split produced a part longer than the requested "
                "{} seconds for {}".format(
                    int(segment_seconds), src_path.name))

        coalesced_parts = []
        coalesced_durations = []
        merge_count = 0
        for part, duration in zip(parts, durations):
            if coalesced_parts \
                    and coalesced_durations[-1] + duration \
                    <= segment_seconds + tolerance:
                previous = coalesced_parts[-1]
                merged = tmp_dir / "time_merged_{:03d}{}".format(
                    merge_count, ext)
                merge_count += 1
                _concat_media_parts(ffmpeg, [previous, part], merged)
                merged_duration = probe_duration_seconds(ffmpeg, merged)
                if merged_duration <= segment_seconds + tolerance:
                    previous.unlink()
                    part.unlink()
                    coalesced_parts[-1] = merged
                    coalesced_durations[-1] = merged_duration
                    continue
                merged.unlink()
            coalesced_parts.append(part)
            coalesced_durations.append(duration)

        result = []
        for index, part in enumerate(coalesced_parts, 1):
            destination = unique_path(out_dir / "{}_tpart_{:03d}{}".format(
                stem, index - 1, ext))
            part.replace(destination)
            result.append(destination)
        return result


def ensure_ffmpeg():
    ff = shutil.which("ffmpeg")
    if not ff:
        raise RuntimeError("ffmpeg not found on PATH")
    return ff
