#!/usr/bin/env python3
"""Хроники времени — монтажный цех.

Получает задание (JSON) из переменной окружения JOB или из файла (аргумент),
делает карусель (JPEG 1080x1350) или Reels (MP4 1080x1920 со звуком и субтитрами),
публикует результат на GitHub Pages и отправляет ссылки на callback_url (n8n).
Витрина последних материалов: https://kazdemeu2026.github.io/history-render/

Формат задания:
{
  "type": "carousel" | "reel",
  "id": "строка",
  "brand": "ХРОНИКИ ВРЕМЕНИ",
  "handle": "@khronika_vremenii",
  "callback_url": "https://...",          # необязательно
  "slides": [{"image_url": "...", "title": "...", "text": "..."}],   # для carousel
  "scenes": [{"image_url": "...", "caption": "..."}],                # для reel
  "audio_url": "https://...mp3"                                       # для reel
}
"""
import io
import json
import math
import os
import random
import subprocess
import sys
import tempfile
import textwrap
import time

import requests
from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps

HERE = os.path.dirname(os.path.abspath(__file__))
FONT_DIR = os.path.join(HERE, "fonts")
GOLD = (233, 196, 106)
WHITE = (255, 255, 255)
INK = (14, 12, 10)

# ---------------------------------------------------------------- шрифты

def _font(path, size, weight=None):
    f = ImageFont.truetype(path, size)
    if weight is not None:
        try:
            f.set_variation_by_axes([weight])
        except Exception:
            pass
    return f


def font_title(size):
    p = os.path.join(FONT_DIR, "Playfair.ttf")
    if os.path.exists(p) and os.path.getsize(p) > 10000:
        return _font(p, size, 800)
    return _font("/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf", size)


def font_sans(size, weight=800):
    p = os.path.join(FONT_DIR, "Montserrat.ttf")
    if os.path.exists(p) and os.path.getsize(p) > 10000:
        return _font(p, size, weight)
    name = "DejaVuSans-Bold.ttf" if weight >= 600 else "DejaVuSans.ttf"
    return _font("/usr/share/fonts/truetype/dejavu/" + name, size)

# ---------------------------------------------------------------- утилиты

UA = {"User-Agent": "HistoryRender/1.0 (https://github.com/KazDemeu2026/history-render) python-requests"}


def load_image(src):
    if src.startswith("http"):
        for attempt in range(4):
            try:
                r = requests.get(src, timeout=60, headers=UA)
                r.raise_for_status()
                return Image.open(io.BytesIO(r.content)).convert("RGB")
            except Exception:
                if attempt == 3:
                    raise
                time.sleep(3 * (attempt + 1))
    return Image.open(src).convert("RGB")


def download(src, dst):
    if not src.startswith("http"):
        return src
    r = requests.get(src, timeout=120, headers=UA)
    r.raise_for_status()
    with open(dst, "wb") as fh:
        fh.write(r.content)
    return dst


def cover(img, w, h):
    """Масштабирует и обрезает картинку под размер w x h."""
    sw, sh = img.size
    s = max(w / sw, h / sh)
    img = img.resize((math.ceil(sw * s), math.ceil(sh * s)), Image.LANCZOS)
    l = (img.width - w) // 2
    t = (img.height - h) // 2
    return img.crop((l, t, l + w, t + h))


def wrap(draw, text, font, max_w):
    words = str(text).split()
    lines, cur = [], ""
    for wd in words:
        test = (cur + " " + wd).strip()
        if draw.textlength(test, font=font) <= max_w or not cur:
            cur = test
        else:
            lines.append(cur)
            cur = wd
    if cur:
        lines.append(cur)
    return lines


def fit_text(draw, text, font_fn, max_w, max_h, start, minimum, spacing=1.15):
    size = start
    while size >= minimum:
        f = font_fn(size)
        lines = wrap(draw, text, f, max_w)
        h = int(len(lines) * size * spacing)
        if h <= max_h:
            return f, lines, size
        size -= 4
    f = font_fn(minimum)
    return f, wrap(draw, text, f, max_w), minimum


def gradient(w, h, top_alpha, bottom_alpha, start=0.0):
    g = Image.new("L", (1, h))
    for y in range(h):
        k = y / max(1, h - 1)
        if k < start:
            a = top_alpha
        else:
            kk = (k - start) / max(1e-6, 1 - start)
            a = int(top_alpha + (bottom_alpha - top_alpha) * kk)
        g.putpixel((0, y), a)
    g = g.resize((w, h))
    layer = Image.new("RGBA", (w, h), INK + (0,))
    layer.putalpha(g)
    return layer


def diamond(draw, cx, cy, r, fill=GOLD):
    draw.polygon([(cx, cy - r), (cx + r * 0.75, cy), (cx, cy + r), (cx - r * 0.75, cy)], fill=fill)


def badge(draw, x, y, brand, size=30):
    """Фирменная плашка: золотой ромб + название канала."""
    f = font_sans(size, 700)
    r = size * 0.36
    diamond(draw, x + r * 0.75, y + size * 0.62, r)
    draw.text((x + r * 1.5 + size * 0.45, y), brand.upper(), font=f, fill=GOLD)
    return x + r * 1.5 + size * 0.45 + draw.textlength(brand.upper(), font=f)


BROWSER_UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"}


def _up_catbox(path):
    with open(path, "rb") as fh:
        r = requests.post("https://catbox.moe/user/api.php", data={"reqtype": "fileupload", "userhash": ""},
                          files={"fileToUpload": (os.path.basename(path), fh)}, headers=BROWSER_UA, timeout=300)
    url = r.text.strip()
    if r.ok and url.startswith("https://"):
        return url
    raise RuntimeError(f"catbox {r.status_code} {url[:120]}")


def _up_tmpfiles(path):
    with open(path, "rb") as fh:
        r = requests.post("https://tmpfiles.org/api/v1/upload", files={"file": (os.path.basename(path), fh)},
                          headers=BROWSER_UA, timeout=300)
    j = r.json()
    url = j.get("data", {}).get("url", "")
    if r.ok and "tmpfiles.org/" in url:
        url = url.replace("http://", "https://").replace("tmpfiles.org/", "tmpfiles.org/dl/", 1)
        return url
    raise RuntimeError(f"tmpfiles {r.status_code} {str(j)[:120]}")


def _up_uguu(path):
    with open(path, "rb") as fh:
        r = requests.post("https://uguu.se/upload", files={"files[]": (os.path.basename(path), fh)},
                          headers=BROWSER_UA, timeout=300)
    j = r.json()
    files = j.get("files") or []
    if r.ok and files and files[0].get("url", "").startswith("https://"):
        return files[0]["url"]
    raise RuntimeError(f"uguu {r.status_code} {str(j)[:120]}")


def upload(path):
    """Загружает файл на бесплатный хостинг (catbox → tmpfiles → uguu), возвращает прямую ссылку."""
    errors = []
    for fn in (_up_catbox, _up_tmpfiles, _up_uguu):
        for attempt in range(2):
            try:
                url = fn(path)
                print("uploaded", os.path.basename(path), "->", url)
                return url
            except Exception as e:  # noqa: BLE001
                errors.append(str(e)[:160])
                time.sleep(3 * (attempt + 1))
    raise RuntimeError("upload failed: " + " | ".join(errors))

# ---------------------------------------------------------------- карусель

CW, CH = 1080, 1350


def carousel_slide(slide, idx, total, brand, handle):
    img = cover(load_image(slide["image_url"]), CW, CH)
    canvas = img.convert("RGBA")
    is_cover = idx == 0
    is_last = idx == total - 1 and total > 2
    canvas.alpha_composite(gradient(CW, CH, 40, 245, start=0.30 if is_cover else 0.38))
    d = ImageDraw.Draw(canvas)
    pad = 72
    badge(d, pad, 60, brand)
    if total > 1:
        f = font_sans(28, 700)
        label = f"{idx + 1}/{total}"
        tw = d.textlength(label, font=f)
        d.rounded_rectangle((CW - pad - tw - 36, 50, CW - pad, 100), radius=25, fill=(0, 0, 0, 150))
        d.text((CW - pad - tw - 18, 58), label, font=f, fill=WHITE)

    title = slide.get("title") or ""
    body = slide.get("text") or ""
    if is_cover:
        f, lines, size = fit_text(d, title, font_title, CW - 2 * pad, 560, 104, 56, 1.12)
        y = CH - 150 - int(len(lines) * size * 1.12)
        d.rectangle((pad, y - 34, pad + 120, y - 26), fill=GOLD)
        for ln in lines:
            d.text((pad, y), ln, font=f, fill=WHITE)
            y += int(size * 1.12)
        fs = font_sans(32, 600)
        d.text((pad, CH - 110), "Листай →", font=fs, fill=GOLD)
    else:
        ft, tl, ts = fit_text(d, title, font_title, CW - 2 * pad, 260, 72, 44, 1.12)
        fb, bl, bs = fit_text(d, body, lambda s: font_sans(s, 500), CW - 2 * pad, 330, 40, 28, 1.3)
        block = int(len(tl) * ts * 1.12) + 28 + int(len(bl) * bs * 1.3)
        y = CH - 120 - block
        d.rectangle((pad, y - 30, pad + 90, y - 23), fill=GOLD)
        for ln in tl:
            d.text((pad, y), ln, font=ft, fill=WHITE)
            y += int(ts * 1.12)
        y += 28
        for ln in bl:
            d.text((pad, y), ln, font=fb, fill=(235, 230, 220))
            y += int(bs * 1.3)
        if is_last and handle:
            fs = font_sans(30, 700)
            d.text((pad, CH - 92), "Сохрани и подпишись " + handle, font=fs, fill=GOLD)
    return canvas.convert("RGB")


def render_carousel(job, out_dir):
    slides = job["slides"][:10]
    paths = []
    for i, s in enumerate(slides):
        im = carousel_slide(s, i, len(slides), job.get("brand", "Хроники времени"), job.get("handle", ""))
        p = os.path.join(out_dir, f"slide_{i + 1:02d}.jpg")
        im.save(p, "JPEG", quality=92, optimize=True)
        paths.append(p)
    return paths

# ---------------------------------------------------------------- reels

VW, VH, FPS = 1080, 1920, 30


def probe_duration(path):
    out = subprocess.check_output([
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=nw=1:nk=1", path,
    ]).decode().strip()
    return float(out)


def chunk_caption(text, max_words=4):
    words = str(text).split()
    chunks, cur = [], []
    for w in words:
        cur.append(w)
        if len(cur) >= max_words or w.endswith((".", "!", "?", ",", ":", "—")):
            chunks.append(" ".join(cur))
            cur = []
    if cur:
        chunks.append(" ".join(cur))
    return chunks or [""]


def subtitle_layer(text, draw_probe):
    # Субтитры ниже и чуть мельче; центр на 73% высоты — над подписью и кнопками Instagram.
    f, lines, size = fit_text(draw_probe, text.upper(), lambda s: font_sans(s, 900), VW - 180, 260, 70, 44, 1.12)
    layer = Image.new("RGBA", (VW, VH), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    total_h = int(len(lines) * size * 1.12)
    y = int(VH * 0.73) - total_h // 2
    for ln in lines:
        w = d.textlength(ln, font=f)
        x = (VW - w) / 2
        d.text((x, y), ln, font=f, fill=WHITE, stroke_width=max(6, size // 10), stroke_fill=(0, 0, 0))
        y += int(size * 1.12)
    return layer


# ---------------------------------------------------------------- ИИ-видео (fal.ai)

FAL_MODEL = os.environ.get("FAL_MODEL") or "fal-ai/kling-video/v2.5-turbo/pro/text-to-video"
AI_NEG = "text, letters, watermark, logo, subtitles, blood, gore, modern objects, cars, extra limbs, deformed faces, blur, low quality"


def fal_clips(scenes, tmp):
    """Генерирует видео для сцен с полем video_prompt. Возвращает {индекс сцены: путь к mp4}.
    Любая ошибка — не страшно: сцена останется с картиной."""
    key = os.environ.get("FAL_KEY", "").strip()
    todo = [(i, s["video_prompt"]) for i, s in enumerate(scenes) if s.get("video_prompt")][:3]
    if not key or not todo:
        print("ai video: skipped (no key or no prompts)")
        return {}
    hdr = {"Authorization": "Key " + key, "Content-Type": "application/json"}
    jobs = {}
    for i, prompt in todo:
        body = {"prompt": prompt + " Cinematic historical scene, vertical frame, dramatic natural light, no text.",
                "duration": "5", "aspect_ratio": "9:16", "negative_prompt": AI_NEG}
        try:
            r = requests.post("https://queue.fal.run/" + FAL_MODEL, json=body, headers=hdr, timeout=60)
            r.raise_for_status()
            j = r.json()
            jobs[i] = j
            print("ai video submitted", i, j.get("request_id"))
        except Exception as e:  # noqa: BLE001
            print("ai video submit failed", i, str(e)[:200])
    out, t0 = {}, time.time()
    while jobs and time.time() - t0 < 600:
        time.sleep(8)
        for i in list(jobs):
            j = jobs[i]
            try:
                st = requests.get(j["status_url"], headers=hdr, timeout=30).json()
                status = st.get("status")
                if status == "COMPLETED":
                    res = requests.get(j["response_url"], headers=hdr, timeout=60).json()
                    url = ((res.get("video") or {}).get("url")) or ""
                    if url:
                        path = os.path.join(tmp, f"clip_{i}.mp4")
                        rr = requests.get(url, timeout=180)
                        rr.raise_for_status()
                        with open(path, "wb") as fh:
                            fh.write(rr.content)
                        out[i] = path
                        print("ai video ready", i, round(time.time() - t0), "s")
                    else:
                        print("ai video no url", i, str(res)[:200])
                    del jobs[i]
                elif status not in ("IN_QUEUE", "IN_PROGRESS"):
                    print("ai video failed", i, str(st)[:200])
                    del jobs[i]
            except Exception as e:  # noqa: BLE001
                print("ai video poll error", i, str(e)[:160])
    for i in jobs:
        print("ai video timeout", i)
    return out


def pexels_clips(scenes, tmp, seed="x"):
    """Бесплатные стоковые видео Pexels для сцен с полем stock_query (обычно первые две)."""
    key = os.environ.get("PEXELS_KEY", "").strip()
    todo = [(i, s.get("stock_query")) for i, s in enumerate(scenes) if s.get("stock_query")][:3]
    if not key or not todo:
        print("pexels: skipped (no key or no queries)")
        return {}
    rng = random.Random(seed)
    out, used = {}, set()
    for i, q in todo:
        try:
            r = requests.get("https://api.pexels.com/videos/search", headers={"Authorization": key}, timeout=30,
                             params={"query": q, "orientation": "portrait", "per_page": 15, "size": "medium"})
            r.raise_for_status()
            cands = []
            for v in r.json().get("videos", []):
                if v.get("id") in used or (v.get("duration") or 0) < 4:
                    continue
                files = [f for f in v.get("video_files", []) if (f.get("height") or 0) >= 1280 and (f.get("width") or 9999) < (f.get("height") or 0)]
                if files:
                    best = min(files, key=lambda f: abs((f.get("height") or 0) - 1920))
                    cands.append((v["id"], best["link"]))
            if not cands:
                print("pexels: nothing for", q)
                continue
            vid, link = rng.choice(cands[:5])
            used.add(vid)
            path = os.path.join(tmp, f"stock_{i}.mp4")
            rr = requests.get(link, timeout=180)
            rr.raise_for_status()
            with open(path, "wb") as fh:
                fh.write(rr.content)
            out[i] = path
            print("pexels ok", i, q, vid)
        except Exception as e:  # noqa: BLE001
            print("pexels failed", i, q, str(e)[:200])
    return out


def pixabay_clips(scenes, tmp, seed="x"):
    """Бесплатные стоковые видео Pixabay для сцен с полем stock_query."""
    key = os.environ.get("PIXABAY_KEY", "").strip()
    todo = [(i, s.get("stock_query")) for i, s in enumerate(scenes) if s.get("stock_query")][:3]
    if not key or not todo:
        print("pixabay: skipped (no key or no queries)")
        return {}
    rng = random.Random(seed)
    out, used = {}, set()
    for i, q in todo:
        try:
            r = requests.get("https://pixabay.com/api/videos/", timeout=30,
                             params={"key": key, "q": q, "per_page": 20, "safesearch": "true", "video_type": "film"})
            r.raise_for_status()
            cands = []
            for h in r.json().get("hits", []):
                if h.get("id") in used or (h.get("duration") or 0) < 4:
                    continue
                vids = h.get("videos") or {}
                pick = next((vids[k] for k in ("large", "medium") if (vids.get(k) or {}).get("url") and (vids[k].get("height") or 0) >= 1080), None)
                if pick:
                    cands.append((h["id"], pick["url"]))
            if not cands:
                print("pixabay: nothing for", q)
                continue
            vid, link = rng.choice(cands[:5])
            used.add(vid)
            path = os.path.join(tmp, f"stock_{i}.mp4")
            rr = requests.get(link, timeout=180)
            rr.raise_for_status()
            with open(path, "wb") as fh:
                fh.write(rr.content)
            out[i] = path
            print("pixabay ok", i, q, vid)
        except Exception as e:  # noqa: BLE001
            print("pixabay failed", i, q, str(e)[:200])
    return out


class ClipReader:
    """Отдаёт кадры ИИ-видео 1080x1920 по одному; растягивает клип (замедляет) до длины сцены."""

    def __init__(self, path, need_frames):
        clip_d = max(0.5, probe_duration(path))
        slow = min(1.8, max(1.0, (need_frames / FPS) / clip_d))
        vf = (f"scale={VW}:{VH}:force_original_aspect_ratio=increase,crop={VW}:{VH},"
              f"setpts={slow:.3f}*PTS,fps={FPS}")
        self.proc = subprocess.Popen(["ffmpeg", "-loglevel", "error", "-i", path, "-vf", vf, "-an",
                                      "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], stdout=subprocess.PIPE)
        self.last = None

    def next(self):
        if self.proc:
            raw = self.proc.stdout.read(VW * VH * 3)
            if len(raw) == VW * VH * 3:
                self.last = Image.frombytes("RGB", (VW, VH), raw)
            else:
                self.close()
        return self.last

    def close(self):
        if self.proc:
            try:
                self.proc.stdout.close()
                self.proc.kill()
            except Exception:  # noqa: BLE001
                pass
            self.proc = None


# ---------------------------------------------------------------- озвучка через fal.ai (ElevenLabs и др.)

def fal_tts(text, tts, path):
    """tts = {"model": "fal-ai/elevenlabs/tts/eleven-v3", "body": {...}}. Возвращает список таймкодов слов или None."""
    key = os.environ.get("FAL_KEY", "").strip()
    if not key:
        raise RuntimeError("no FAL_KEY")
    model = tts.get("model") or "fal-ai/elevenlabs/tts/eleven-v3"
    body = dict(tts.get("body") or {})
    body["text"] = text
    if "elevenlabs" in model:
        body.setdefault("language_code", "ru")
        body.setdefault("timestamps", True)
    if "minimax" in model:
        body.setdefault("output_format", "url")
        body.setdefault("language_boost", "Russian")
    r = requests.post("https://fal.run/" + model, json=body,
                      headers={"Authorization": "Key " + key, "Content-Type": "application/json"}, timeout=300)
    if r.status_code >= 300:
        raise RuntimeError(f"tts {r.status_code} {r.text[:300]}")
    j = r.json()
    url = ((j.get("audio") or {}).get("url")) or j.get("audio_url") or ""
    if not url:
        raise RuntimeError("tts: no audio url " + str(j)[:200])
    rr = requests.get(url, timeout=120)
    rr.raise_for_status()
    with open(path, "wb") as fh:
        fh.write(rr.content)
    return j.get("timestamps")


def word_starts(stamps):
    """Приводит таймкоды разных форматов к списку времени начала слов."""
    out = []
    for w in stamps or []:
        if not isinstance(w, dict):
            continue
        txt = str(w.get("text", w.get("word", ""))).strip()
        st = w.get("start", w.get("start_time", w.get("startTime")))
        if txt and st is not None and any(ch.isalnum() for ch in txt):
            out.append(float(st))
    return out


def render_voices(job, out_dir):
    """Пробник голосов: один и тот же текст разными голосами, чтобы выбрать на слух."""
    files = []
    for i, v in enumerate(job.get("voices", [])):
        label = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in v.get("label", f"voice{i}"))[:60]
        path = os.path.join(out_dir, f"{i + 1:02d}_{label}.mp3")
        try:
            if v.get("file"):
                with open(os.path.join(HERE, v["file"]), "rb") as src, open(path, "wb") as dst:
                    dst.write(src.read())
            else:
                fal_tts(job["text"], v, path)
            files.append(path)
            print("voice ok", label)
        except Exception as e:  # noqa: BLE001
            print("voice failed", label, str(e)[:300])
    return files


# ---------------------------------------------------------------- карта походов

WORLD_PATH = os.path.join(FONT_DIR, "world.jpg")
_WORLD = {}
RED = (196, 52, 40)


def world_map():
    """Карта мира NASA Blue Marble (общественное достояние) в старинной тонировке, без современных границ."""
    if "img" not in _WORLD:
        img = None
        if os.path.exists(WORLD_PATH) and os.path.getsize(WORLD_PATH) > 100000:
            try:
                raw = Image.open(WORLD_PATH).convert("RGB")
                if raw.width > 5400:
                    raw = raw.resize((5400, 2700), Image.LANCZOS)
                sep = ImageOps.colorize(ImageOps.grayscale(raw), (28, 20, 12), (238, 214, 170))
                img = Image.blend(raw, sep, 0.6)
            except Exception as e:  # noqa: BLE001
                print("world map failed:", e)
        _WORLD["img"] = img
    return _WORLD["img"]


def map_points(m):
    pts = []
    for p in (m or {}).get("points", [])[:6]:
        try:
            lat, lon = float(p.get("lat")), float(p.get("lon"))
        except (TypeError, ValueError):
            continue
        if -75 <= lat <= 80 and -180 <= lon <= 180:
            pts.append({"name": str(p.get("name") or "")[:28], "lat": lat, "lon": lon})
    return pts


class MapScene:
    """Анимированная карта: камера плавно приближается, маршрут прорисовывается, города появляются по ходу."""

    def __init__(self, m, n):
        W = world_map()
        self.n = n
        self.pts = map_points(m)
        self.route = bool(m.get("route", True)) and len(self.pts) > 1
        self.title = str(m.get("title") or "").upper()[:42]
        Wd, Ht = W.size
        xy = [((p["lon"] + 180) / 360 * Wd, (90 - p["lat"]) / 180 * Ht) for p in self.pts]
        xs, ys = [a for a, _ in xy], [b for _, b in xy]
        span_w = max((max(xs) - min(xs)) * 1.3, Wd * 22 / 360)
        span_h = max((max(ys) - min(ys)) * 1.5, span_w * VH / VW)
        span_h = min(span_h, Ht * 0.95)
        span_w = span_h * VW / VH
        cx = (min(xs) + max(xs)) / 2
        cy = (min(ys) + max(ys)) / 2 + span_h * 0.08  # точки чуть выше центра — ниже субтитры
        def box(k):
            w, h = span_w * k, span_h * k
            x0 = min(max(cx - w / 2, 0), Wd - w)
            y0 = min(max(cy - h / 2, 0), Ht - h)
            return (x0, y0, x0 + w, y0 + h)
        self.b1, self.b2 = box(1.22), box(1.0)
        ox0, oy0 = min(self.b1[0], self.b2[0]), min(self.b1[1], self.b2[1])
        ox1, oy1 = max(self.b1[2], self.b2[2]), max(self.b1[3], self.b2[3])
        self.off = (int(ox0), int(oy0))
        self.sub = W.crop((int(ox0), int(oy0), int(ox1) + 1, int(oy1) + 1))
        self.xy = xy
        seg = [math.dist(xy[i], xy[i + 1]) for i in range(len(xy) - 1)]
        self.total = sum(seg) or 1
        acc, self.at = 0, [0.0]
        for d in seg:
            acc += d
            self.at.append(acc / self.total)
        self.lf = font_sans(40, 800)
        self.tf = font_sans(46, 900)

    def frame(self, fi):
        t = fi / max(1, self.n - 1)
        e = t * t * (3 - 2 * t)
        b = [self.b1[i] + (self.b2[i] - self.b1[i]) * e for i in range(4)]
        ox, oy = self.off
        # субпиксельная камера: без целочисленного округления карта движется плавно, без рывков
        img = self.sub.resize((VW, VH), Image.BICUBIC, box=(b[0] - ox, b[1] - oy, b[2] - ox, b[3] - oy))
        img = img.convert("RGBA")
        sx, sy = VW / (b[2] - b[0]), VH / (b[3] - b[1])
        P = [((x - b[0]) * sx, (y - b[1]) * sy) for x, y in self.xy]
        d = ImageDraw.Draw(img)
        prog = min(1.0, max(0.0, (t - 0.08) / 0.7)) if self.route else 1.0
        if self.route and prog > 0:
            pts, tip = [P[0]], P[0]
            for i in range(len(P) - 1):
                if prog >= self.at[i + 1]:
                    pts.append(P[i + 1]); tip = P[i + 1]
                else:
                    k = (prog - self.at[i]) / max(1e-6, self.at[i + 1] - self.at[i])
                    tip = (P[i][0] + (P[i + 1][0] - P[i][0]) * k, P[i][1] + (P[i + 1][1] - P[i][1]) * k)
                    pts.append(tip)
                    break
            if len(pts) > 1:
                d.line(pts, fill=(0, 0, 0, 160), width=18, joint="curve")
                d.line(pts, fill=RED + (255,), width=10, joint="curve")
                a, c = pts[-2], pts[-1]
                ang = math.atan2(c[1] - a[1], c[0] - a[0])
                L = 34
                d.polygon([c, (c[0] - L * math.cos(ang - 0.45), c[1] - L * math.sin(ang - 0.45)),
                           (c[0] - L * math.cos(ang + 0.45), c[1] - L * math.sin(ang + 0.45))], fill=RED + (255,))
        vis = [((prog >= self.at[k] - 1e-6) if self.route else (t >= 0.08 + k * 0.12)) for k in range(len(P))]
        placed = [(x - 22, y - 22, x + 22, y + 22) for k, (x, y) in enumerate(P) if vis[k]]
        for k, (x, y) in enumerate(P):
            if not vis[k]:
                continue
            r = 15
            d.ellipse((x - r - 4, y - r - 4, x + r + 4, y + r + 4), fill=(0, 0, 0, 150))
            d.ellipse((x - r, y - r, x + r, y + r), fill=GOLD + (255,), outline=(255, 255, 255, 255), width=4)
            name = self.pts[k]["name"].upper()
            if name:
                tw = d.textlength(name, font=self.lf)
                best = None
                for dy in (-26, -80, 28, -134, 82):
                    for right in (True, False):
                        tx = x + 30 if right else x - 30 - tw
                        if tx < 30 or tx + tw > VW - 30:
                            continue
                        boxr = (tx - 6, y + dy - 6, tx + tw + 6, y + dy + 52)
                        if not any(not (boxr[2] < q[0] or boxr[0] > q[2] or boxr[3] < q[1] or boxr[1] > q[3]) for q in placed):
                            best = (tx, y + dy, boxr)
                            break
                    if best:
                        break
                if best is None:
                    tx = x + 30 if x + 30 + tw < VW - 30 else x - 30 - tw
                    best = (tx, y - 26, (tx, y - 26, tx + tw, y + 26))
                placed.append(best[2])
                d.text((best[0], best[1]), name, font=self.lf, fill=WHITE, stroke_width=6, stroke_fill=(0, 0, 0))
        if self.title:
            tw = d.textlength(self.title, font=self.tf)
            d.text(((VW - tw) / 2, 262), self.title, font=self.tf, fill=GOLD, stroke_width=6, stroke_fill=(0, 0, 0))
        return img


def render_reel(job, out_dir):
    scenes = job["scenes"][:12]
    tmp = tempfile.mkdtemp()
    audio, starts = None, []
    if job.get("tts") and os.environ.get("FAL_KEY"):
        try:
            p = os.path.join(tmp, "voice_fal.mp3")
            starts = word_starts(fal_tts(" ".join(s.get("caption", "") for s in scenes), job["tts"], p))
            audio = p
            print("tts: fal ok, words with time:", len(starts))
        except Exception as e:  # noqa: BLE001
            print("tts: fal failed, fallback to audio_url:", str(e)[:300])
    if not audio:
        audio = download(job["audio_url"], os.path.join(tmp, "voice.mp3"))
    dur = probe_duration(audio) + 0.6
    weights = [max(12, len(s.get("caption", ""))) for s in scenes]
    total_w = sum(weights)
    durs = [max(2.2, dur * w / total_w) for w in weights]
    k = dur / sum(durs)
    durs = [x * k for x in durs]
    sub_bounds = {}
    words_per_scene = [len(str(s.get("caption", "")).split()) for s in scenes]
    total_words = sum(words_per_scene)
    if starts and total_words and abs(len(starts) - total_words) <= max(3, total_words // 10):
        # Точная синхронизация по таймкодам голоса: смена сцен и субтитров — на начале нужного слова.
        ratio = len(starts) / total_words
        idx = lambda wi: min(len(starts) - 1, int(round(wi * ratio)))
        cum, scene_t = 0, []
        for wc in words_per_scene:
            scene_t.append(0.0 if cum == 0 else starts[idx(cum)])
            cum += wc
        scene_t.append(dur)
        durs = [max(0.8, scene_t[i + 1] - scene_t[i]) for i in range(len(scenes))]
        cum = 0
        for si, s in enumerate(scenes):
            chunks = chunk_caption(s.get("caption", ""))
            b, wcount = [], cum
            for c in chunks:
                wcount += len(c.split())
                end_t = starts[idx(wcount)] if wcount < total_words else dur
                b.append(min(1.0, max(0.0, (end_t - scene_t[si]) / max(0.1, durs[si]))))
            b[-1] = 1.0
            sub_bounds[si] = b
            cum += words_per_scene[si]
        print("timing: synced to voice")

    brand = job.get("brand", "Хроники времени")
    overlay_static = Image.new("RGBA", (VW, VH), (0, 0, 0, 0))
    overlay_static.alpha_composite(gradient(VW, VH, 0, 210, start=0.45))
    top = gradient(VW, 260, 150, 0)
    overlay_static.alpha_composite(top, (0, 0))
    od = ImageDraw.Draw(overlay_static)
    bf = font_sans(34, 700)
    bw = od.textlength(brand.upper(), font=bf) + 34 * 0.36 * 1.5 + 34 * 0.45
    badge(od, (VW - bw) / 2, 90, brand, 34)
    label = (job.get("series_label") or "").upper().strip()
    if label:
        lf = font_sans(30, 800)
        lw = od.textlength(label, font=lf)
        x0, y0 = (VW - lw) / 2 - 26, 146
        od.rounded_rectangle((x0, y0, x0 + lw + 52, y0 + 56), radius=28, fill=(0, 0, 0, 170), outline=GOLD, width=3)
        od.text((x0 + 26, y0 + 10), label, font=lf, fill=WHITE)

    probe = ImageDraw.Draw(Image.new("RGB", (10, 10)))
    bases = []
    for s in scenes:
        img = load_image(s["image_url"])
        bases.append(cover(img, int(VW * 1.18), int(VH * 1.18)))
    clips = fal_clips(scenes, tmp)
    if not clips:
        clips = pexels_clips(scenes, tmp, job.get("id", "x"))
    if not clips:
        clips = pixabay_clips(scenes, tmp, job.get("id", "x"))

    out = os.path.join(out_dir, "reel.mp4")
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{VW}x{VH}", "-r", str(FPS), "-i", "-",
        "-i", audio,
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
        "-af", "apad", "-c:a", "aac", "-b:a", "160k", "-ar", "44100",
        "-shortest", "-movflags", "+faststart", out,
    ]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    fade = int(0.35 * FPS)
    prev_tail = None
    rng = random.Random(job.get("id", "x"))
    last = len(scenes) - 1
    for si, (s, base, sd) in enumerate(zip(scenes, bases, durs)):
        n = max(1, int(round(sd * FPS))) + (fade if si < last else 0)
        chunks = chunk_caption(s.get("caption", ""))
        cw = [max(4, len(c)) for c in chunks]
        bounds, acc = [], 0
        for c in cw:
            acc += c
            bounds.append(acc / sum(cw))
        if si in sub_bounds and len(sub_bounds[si]) == len(chunks):
            bounds = sub_bounds[si]
        subs = [subtitle_layer(c, probe) for c in chunks]
        zoom_in = si % 2 == 0
        px, py = rng.uniform(-0.5, 0.5), rng.uniform(-0.5, 0.5)
        frames_tail = []
        reader = None
        mapper = None
        if si in clips:
            try:
                reader = ClipReader(clips[si], n)
            except Exception as e:  # noqa: BLE001
                print("clip reader failed", si, e)
        elif s.get("map") and map_points(s["map"]) and world_map() is not None:
            try:
                mapper = MapScene(s["map"], n)
                print("map scene", si, [p["name"] for p in mapper.pts])
            except Exception as e:  # noqa: BLE001
                print("map scene failed", si, e)
        for fi in range(n):
            t = fi / max(1, n - 1)
            e = t * t * (3 - 2 * t)
            vframe = reader.next() if reader else None
            if vframe is not None:
                frame = vframe.copy()
            elif mapper is not None:
                frame = mapper.frame(fi)
            else:
                z = 1.0 + 0.16 * (e if zoom_in else 1 - e)
                cw_, ch_ = VW * 1.18 / z, VH * 1.18 / z
                cx = (base.width - cw_) / 2 + px * (base.width - cw_) * 0.5 * e
                cy = (base.height - ch_) / 2 + py * (base.height - ch_) * 0.5 * e
                frame = base.resize((VW, VH), Image.BILINEAR, box=(cx, cy, cx + cw_, cy + ch_))
            frame = frame.convert("RGBA")
            frame.alpha_composite(overlay_static)
            if fi >= n - fade and si < last:
                # хвост сцены: сохраняем только фон, он плавно перейдёт в следующую сцену
                frames_tail.append(frame)
                continue
            if prev_tail is not None and fi < len(prev_tail):
                a = (fi + 1) / (len(prev_tail) + 1)
                frame = Image.blend(prev_tail[fi], frame, a)
            ci = next((i for i, b in enumerate(bounds) if t <= b), len(subs) - 1)
            frame.alpha_composite(subs[ci])
            proc.stdin.write(frame.convert("RGB").tobytes())
        if reader:
            reader.close()
        prev_tail = frames_tail if frames_tail else None
    if prev_tail:
        for fr in prev_tail:
            proc.stdin.write(fr.convert("RGB").tobytes())
    proc.stdin.close()
    if proc.wait() != 0:
        raise RuntimeError("ffmpeg failed")
    files = [out]
    cover_base = bases[0]
    if 0 in clips:
        try:
            fr = ClipReader(clips[0], FPS * 2)
            img = None
            for _ in range(int(FPS * 1.5)):
                nxt = fr.next()
                img = nxt if nxt is not None else img
            fr.close()
            if img is not None:
                cover_base = img
        except Exception as e:  # noqa: BLE001
            print("cover frame failed", e)
    try:
        files.append(reel_cover(job, cover_base, brand, out_dir))
    except Exception as e:  # noqa: BLE001
        print("cover skipped:", e)
    return files


def reel_cover(job, base, brand, out_dir):
    """Обложка Reels 1080x1920. Текст в центре, чтобы он был виден и в сетке профиля (обрезка 3:4)."""
    img = cover(base, VW, VH).convert("RGBA")
    img.alpha_composite(Image.new("RGBA", (VW, VH), INK + (95,)))
    img.alpha_composite(gradient(VW, VH, 30, 235, start=0.30))
    d = ImageDraw.Draw(img)
    bf = font_sans(34, 700)
    bw = d.textlength(brand.upper(), font=bf) + 34 * 0.36 * 1.5 + 34 * 0.45
    badge(d, (VW - bw) / 2, 300, brand, 34)
    kicker = (job.get("cover_kicker") or "").upper().strip()
    title = (job.get("cover_title") or job.get("title") or "").strip()
    pad = 90
    f, lines, size = fit_text(d, title, font_title, VW - 2 * pad, 620, 112, 60, 1.1)
    block = int(len(lines) * size * 1.1)
    y = VH // 2 - block // 2 + 40
    if kicker:
        kf = font_sans(38, 800)
        kw = d.textlength(kicker, font=kf)
        d.rounded_rectangle(((VW - kw) / 2 - 30, y - 120, (VW + kw) / 2 + 30, y - 52), radius=34, fill=GOLD)
        d.text(((VW - kw) / 2, y - 111), kicker, font=kf, fill=INK)
    for ln in lines:
        w = d.textlength(ln, font=f)
        d.text(((VW - w) / 2, y), ln, font=f, fill=WHITE, stroke_width=3, stroke_fill=(0, 0, 0))
        y += int(size * 1.1)
    p = os.path.join(out_dir, "cover.jpg")
    img.convert("RGB").save(p, "JPEG", quality=90, optimize=True)
    return p

# ---------------------------------------------------------------- main

def callback(job, payload):
    url = job.get("callback_url")
    print("RESULT", json.dumps(payload, ensure_ascii=False))
    if not url:
        return
    for attempt in range(5):
        try:
            r = requests.post(url, json=payload, timeout=60)
            print("callback", r.status_code)
            if r.status_code < 500:
                return
        except Exception as e:  # noqa: BLE001
            print("callback error", e)
        time.sleep(5 * (attempt + 1))


def safe_id(job):
    raw = str(job.get("id") or int(time.time()))
    return "".join(ch for ch in raw if ch.isalnum() or ch in "-_")[:60] or str(int(time.time()))


def wait_public(url, timeout=240):
    """Ждёт, пока файл появится на GitHub Pages (CDN обновляется не мгновенно)."""
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            r = requests.head(url, timeout=20, allow_redirects=True)
            if r.status_code == 200:
                return True
        except Exception:  # noqa: BLE001
            pass
        time.sleep(8)
    return False


# ---------------------------------------------------------------- витрина

KEEP_HOURS = 72


def carry_over(base_out, jid):
    """GitHub Pages публикует сайт целиком. Чтобы прошлые ролики и карусели
    не пропадали (Instagram скачивает видео не сразу, а вы можете посмотреть
    превью), переносим в новый сайт всё, что опубликовано за последние KEEP_HOURS."""
    base = (os.environ.get("SITE_BASE") or "").rstrip("/") + "/"
    if base == "/":
        return []
    try:
        r = requests.get(base + "manifest.json", timeout=30)
        items = r.json().get("items", []) if r.status_code == 200 else []
    except Exception as e:  # noqa: BLE001
        print("manifest not loaded:", e)
        items = []
    keep, now = [], time.time()
    for it in items:
        iid = it.get("id")
        if not iid or iid == jid or now - float(it.get("ts", 0)) > KEEP_HOURS * 3600:
            continue
        d = os.path.join(base_out, iid)
        os.makedirs(d, exist_ok=True)
        try:
            for f in it.get("files", []):
                rr = requests.get(base + iid + "/" + f, timeout=180)
                rr.raise_for_status()
                with open(os.path.join(d, f), "wb") as fh:
                    fh.write(rr.content)
            keep.append(it)
        except Exception as e:  # noqa: BLE001
            print("skip old item", iid, e)
    print("carried over:", [k["id"] for k in keep])
    return keep


def write_showcase(base_out, items):
    with open(os.path.join(base_out, "manifest.json"), "w") as fh:
        json.dump({"items": items}, fh, ensure_ascii=False)
    esc = lambda t: (str(t or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
    cards = []
    for it in sorted(items, key=lambda x: -float(x.get("ts", 0))):
        when = time.strftime("%d.%m %H:%M", time.gmtime(float(it.get("ts", 0)) + 5 * 3600))
        kind = {"reel": "Reels", "voices": "Пробник голосов"}.get(it.get("type"), "Карусель")
        if it.get("type") == "voices":
            media = "".join('<div style="margin:8px 0"><b>%s</b><br><audio controls preload="none" src="%s/%s"></audio></div>' % (esc(f[3:-4].replace("_", " ")), it["id"], f) for f in it.get("files", []))
            cards.append('<section><h2>%s · %s</h2><p>%s</p>%s</section>' % (kind, when, esc(it.get("title")), media))
            continue
        if it.get("type") == "reel":
            media = "".join(('<video controls preload="metadata" src="%s/%s"></video>' if f.endswith(".mp4") else '<img loading="lazy" style="height:480px" src="%s/%s">') % (it["id"], f) for f in it.get("files", []))
        else:
            media = "".join('<a href="%s/%s"><img loading="lazy" src="%s/%s"></a>' % (it["id"], f, it["id"], f) for f in it.get("files", []))
        cards.append('<section><h2>%s · %s</h2><p>%s</p><div class="row">%s</div></section>' % (kind, when, esc(it.get("title")), media))
    html = """<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Хроники времени — превью</title><style>
body{margin:0;background:#111;color:#eee;font:16px/1.4 system-ui,sans-serif;padding:16px}
h1{color:#e9c46a;font-size:22px}h2{font-size:16px;color:#e9c46a;margin:24px 0 4px}p{margin:0 0 8px;color:#bbb}
.row{display:flex;gap:8px;overflow-x:auto;padding-bottom:8px}.row img{height:320px;border-radius:6px}
.row video{height:480px;border-radius:6px;background:#000}</style></head><body>
<h1>Хроники времени — что подготовлено за последние дни</h1>""" + "".join(cards) + "</body></html>"
    with open(os.path.join(base_out, "index.html"), "w") as fh:
        fh.write(html)


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 and sys.argv[1].startswith("--") else "--render"
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    raw = os.environ.get("JOB") or (open(args[0]).read() if args else "")
    job = json.loads(raw)
    jid = safe_id(job)
    base_out = os.environ.get("OUT_DIR") or tempfile.mkdtemp()
    out_dir = os.path.join(base_out, jid)

    if mode == "--fail":
        callback(job, {"id": job.get("id"), "type": job.get("type"), "status": "error",
                       "error": os.environ.get("FAIL_REASON", "render workflow failed")})
        return

    if mode == "--callback":
        # Файлы уже опубликованы на GitHub Pages: собираем ссылки и сообщаем n8n.
        page = os.environ["PAGE_URL"].rstrip("/") + "/"
        files = json.load(open(os.path.join(out_dir, "result.json")))["files"]
        urls = [page + jid + "/" + os.path.basename(f) for f in files]
        missing = [u for u in urls if not wait_public(u)]
        if missing:
            callback(job, {"id": job.get("id"), "type": job.get("type"), "status": "error",
                           "error": "files not reachable on Pages: " + ", ".join(missing)[:400]})
            sys.exit(1)
        payload = {"id": job.get("id"), "type": job.get("type"), "status": "ok"}
        if job["type"] == "reel":
            payload["video_url"] = urls[0]
            covers = [u for u in urls[1:] if u.endswith(".jpg")]
            if covers:
                payload["cover_url"] = covers[0]
        else:
            payload["image_urls"] = urls
        callback(job, payload)
        return

    # --render
    os.makedirs(out_dir, exist_ok=True)
    if job["type"] == "carousel":
        files = render_carousel(job, out_dir)
    elif job["type"] == "reel":
        files = render_reel(job, out_dir)
    elif job["type"] == "voices":
        files = render_voices(job, out_dir)
    else:
        raise ValueError("unknown type " + str(job.get("type")))
    with open(os.path.join(out_dir, "result.json"), "w") as fh:
        json.dump({"files": [os.path.basename(f) for f in files]}, fh)
    print("RENDERED", jid, [os.path.basename(f) for f in files])
    try:
        title = job.get("title") or ((job.get("slides") or [{}])[0].get("title")) or ((job.get("scenes") or [{}])[0].get("caption")) or ""
        items = carry_over(base_out, jid) + [{"id": jid, "type": job["type"], "title": title,
                                               "files": [os.path.basename(f) for f in files], "ts": time.time()}]
        write_showcase(base_out, items)
    except Exception as e:  # noqa: BLE001
        print("showcase skipped:", e)
    if os.environ.get("UPLOAD_MODE") == "hosting":
        payload = {"id": job.get("id"), "type": job.get("type"), "status": "ok"}
        urls = [upload(p) for p in files]
        if job["type"] == "reel":
            payload["video_url"] = urls[0]
        else:
            payload["image_urls"] = urls
        callback(job, payload)


if __name__ == "__main__":
    main()
