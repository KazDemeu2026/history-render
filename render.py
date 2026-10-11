#!/usr/bin/env python3
"""Хроники времени — монтажный цех.

Получает задание (JSON) из переменной окружения JOB или из файла (аргумент),
делает карусель (JPEG 1080x1350) или Reels (MP4 1080x1920 со звуком и субтитрами),
загружает результат на catbox.moe и отправляет ответ на callback_url (n8n).

Формат задания:
{
  "type": "carousel" | "reel",
  "id": "строка",
  "brand": "ХРОНИКИ ВРЕМЕНИ",
  "handle": "@jumsaq_yu",
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
from PIL import Image, ImageDraw, ImageFilter, ImageFont

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

def load_image(src):
    if src.startswith("http"):
        for attempt in range(4):
            try:
                r = requests.get(src, timeout=60)
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
    r = requests.get(src, timeout=120)
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


def badge(draw, x, y, brand, size=30):
    f = font_sans(size, 700)
    draw.text((x, y), "✦ " + brand.upper(), font=f, fill=GOLD)


def upload(path):
    """Загружает файл на catbox.moe, возвращает прямую ссылку."""
    last = None
    for attempt in range(4):
        try:
            with open(path, "rb") as fh:
                r = requests.post(
                    "https://catbox.moe/user/api.php",
                    data={"reqtype": "fileupload"},
                    files={"fileToUpload": (os.path.basename(path), fh)},
                    timeout=300,
                )
            url = r.text.strip()
            if r.ok and url.startswith("https://"):
                return url
            last = f"{r.status_code} {url[:200]}"
        except Exception as e:  # noqa: BLE001
            last = str(e)
        time.sleep(5 * (attempt + 1))
    raise RuntimeError("catbox upload failed: " + str(last))

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
    f, lines, size = fit_text(draw_probe, text.upper(), lambda s: font_sans(s, 900), VW - 160, 330, 84, 52, 1.12)
    layer = Image.new("RGBA", (VW, VH), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    total_h = int(len(lines) * size * 1.12)
    y = int(VH * 0.64) - total_h // 2
    for ln in lines:
        w = d.textlength(ln, font=f)
        x = (VW - w) / 2
        d.text((x, y), ln, font=f, fill=WHITE, stroke_width=max(6, size // 10), stroke_fill=(0, 0, 0))
        y += int(size * 1.12)
    return layer


def render_reel(job, out_dir):
    scenes = job["scenes"][:12]
    tmp = tempfile.mkdtemp()
    audio = download(job["audio_url"], os.path.join(tmp, "voice.mp3"))
    dur = probe_duration(audio) + 0.6
    weights = [max(12, len(s.get("caption", ""))) for s in scenes]
    total_w = sum(weights)
    durs = [max(2.2, dur * w / total_w) for w in weights]
    k = dur / sum(durs)
    durs = [x * k for x in durs]

    brand = job.get("brand", "Хроники времени")
    overlay_static = Image.new("RGBA", (VW, VH), (0, 0, 0, 0))
    overlay_static.alpha_composite(gradient(VW, VH, 0, 210, start=0.45))
    top = gradient(VW, 260, 150, 0)
    overlay_static.alpha_composite(top, (0, 0))
    od = ImageDraw.Draw(overlay_static)
    bf = font_sans(34, 700)
    bw = od.textlength("✦ " + brand.upper(), font=bf)
    od.text(((VW - bw) / 2, 90), "✦ " + brand.upper(), font=bf, fill=GOLD)

    probe = ImageDraw.Draw(Image.new("RGB", (10, 10)))
    bases = []
    for s in scenes:
        img = load_image(s["image_url"])
        bases.append(cover(img, int(VW * 1.18), int(VH * 1.18)))

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
        subs = [subtitle_layer(c, probe) for c in chunks]
        zoom_in = si % 2 == 0
        px, py = rng.uniform(-0.5, 0.5), rng.uniform(-0.5, 0.5)
        frames_tail = []
        for fi in range(n):
            t = fi / max(1, n - 1)
            e = t * t * (3 - 2 * t)
            z = 1.0 + 0.16 * (e if zoom_in else 1 - e)
            cw_, ch_ = VW * 1.18 / z, VH * 1.18 / z
            cx = (base.width - cw_) / 2 + px * (base.width - cw_) * 0.5 * e
            cy = (base.height - ch_) / 2 + py * (base.height - ch_) * 0.5 * e
            frame = base.crop((int(cx), int(cy), int(cx + cw_), int(cy + ch_))).resize((VW, VH), Image.BILINEAR)
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
        prev_tail = frames_tail if frames_tail else None
    if prev_tail:
        for fr in prev_tail:
            proc.stdin.write(fr.convert("RGB").tobytes())
    proc.stdin.close()
    if proc.wait() != 0:
        raise RuntimeError("ffmpeg failed")
    return [out]

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


def main():
    raw = os.environ.get("JOB") or (open(sys.argv[1]).read() if len(sys.argv) > 1 else "")
    job = json.loads(raw)
    out_dir = os.environ.get("OUT_DIR") or tempfile.mkdtemp()
    os.makedirs(out_dir, exist_ok=True)
    payload = {"id": job.get("id"), "type": job.get("type"), "status": "ok"}
    try:
        if job["type"] == "carousel":
            files = render_carousel(job, out_dir)
        elif job["type"] == "reel":
            files = render_reel(job, out_dir)
        else:
            raise ValueError("unknown type " + str(job.get("type")))
        if os.environ.get("NO_UPLOAD"):
            payload["files"] = files
        else:
            urls = [upload(p) for p in files]
            if job["type"] == "reel":
                payload["video_url"] = urls[0]
            else:
                payload["image_urls"] = urls
    except Exception as e:  # noqa: BLE001
        payload = {"id": job.get("id"), "type": job.get("type"), "status": "error", "error": str(e)[:500]}
        callback(job, payload)
        raise
    callback(job, payload)


if __name__ == "__main__":
    main()
