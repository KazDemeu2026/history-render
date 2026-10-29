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
import base64
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
from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont, ImageOps

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


WAN_MOTION = ("Animate this historical painting with subtle, realistic cinematic motion: a slow camera push-in, "
              "wind moving clothes, banners and grass, drifting dust and smoke, people and horses moving naturally. "
              "Keep the painting's style, colors and composition. No text, no new objects.")


VEO_MOTION = ("Cinematic slow camera push-in, subtle natural motion: wind in clothes, banners and grass, drifting dust and mist, "
              "people and horses moving naturally. Keep the painting's style, colors and composition. No text, no subtitles, no new objects.")


def _gcp_token():
    """Токен доступа к Vertex AI по ключу служебного аккаунта из секрета GCP_SA_KEY."""
    raw = os.environ.get("GCP_SA_KEY", "").strip()
    if not raw:
        return None, None
    from google.oauth2 import service_account  # noqa: WPS433
    import google.auth.transport.requests as gtr  # noqa: WPS433
    info = json.loads(raw)
    creds = service_account.Credentials.from_service_account_info(info, scopes=["https://www.googleapis.com/auth/cloud-platform"])
    creds.refresh(gtr.Request())
    return creds.token, info.get("project_id")


def veo_clips(scenes, tmp, count=None):
    """Оживление первой сцены (или нескольких) моделью Google Veo на Vertex AI.
    Картина сцены — первый кадр. Возвращает {индекс сцены: путь к mp4}; любая ошибка — сцена останется картиной."""
    try:
        token, project = _gcp_token()
    except Exception as e:  # noqa: BLE001
        print("veo: auth failed", str(e)[:200])
        return {}
    if not token:
        print("veo: skipped (no GCP_SA_KEY)")
        return {}
    loc = os.environ.get("VEO_LOCATION", "us-central1")
    model = os.environ.get("VEO_MODEL", "veo-3.1-lite-generate-001")
    count = int(count or os.environ.get("VEO_SCENES", "1") or 1)
    dur = int(os.environ.get("VEO_SECONDS", "6") or 6)
    base = f"https://{loc}-aiplatform.googleapis.com/v1/projects/{project}/locations/{loc}/publishers/google/models/{model}"
    hdr = {"Authorization": "Bearer " + token, "Content-Type": "application/json"}
    todo = [(i, s) for i, s in enumerate(scenes) if s.get("image_url") and not s.get("map")][:count]
    ops = {}
    for i, s in todo:
        try:
            img = cover(load_image(s["image_url"]), 720, 1280)
            buf = io.BytesIO()
            img.save(buf, "JPEG", quality=90)
            prompt = (str(s.get("video_prompt") or s.get("caption") or "") + " " + VEO_MOTION).strip()[:1800]
            params = {"aspectRatio": "9:16", "durationSeconds": dur, "sampleCount": 1, "resolution": "720p",
                      "personGeneration": "allow_adult", "generateAudio": False}
            body = {"instances": [{"prompt": prompt, "image": {"bytesBase64Encoded": base64.b64encode(buf.getvalue()).decode(), "mimeType": "image/jpeg"}}],
                    "parameters": params}
            r = requests.post(base + ":predictLongRunning", json=body, headers=hdr, timeout=60)
            if r.status_code >= 300 and "generateAudio" in r.text:
                params.pop("generateAudio", None)
                r = requests.post(base + ":predictLongRunning", json=body, headers=hdr, timeout=60)
            j = r.json()
            if not j.get("name"):
                print("veo submit failed", i, r.status_code, str(j)[:400])
                continue
            ops[i] = j["name"]
            print("veo submitted", i, model, j["name"][-24:])
        except Exception as e:  # noqa: BLE001
            print("veo submit error", i, str(e)[:200])
    out, t0 = {}, time.time()
    while ops and time.time() - t0 < 540:
        time.sleep(12)
        for i in list(ops):
            try:
                r = requests.post(base + ":fetchPredictOperation", json={"operationName": ops[i]}, headers=hdr, timeout=120)
                j = r.json()
                if not j.get("done"):
                    continue
                del ops[i]
                if j.get("error"):
                    print("veo failed", i, str(j["error"])[:300])
                    continue
                vids = (j.get("response") or {}).get("videos") or []
                if not vids or not vids[0].get("bytesBase64Encoded"):
                    print("veo: no video", i, str(j.get("response"))[:300])
                    continue
                path = os.path.join(tmp, f"veo_{i}.mp4")
                with open(path, "wb") as fh:
                    fh.write(base64.b64decode(vids[0]["bytesBase64Encoded"]))
                out[i] = path
                print("veo ready", i, round(time.time() - t0), "s")
            except Exception as e:  # noqa: BLE001
                print("veo poll error", i, str(e)[:160])
    for i in ops:
        print("veo timeout", i)
    return out


def wan_clips(scenes, tmp):
    """Оживление картины первой сцены (или нескольких) моделью Wan 2.2 (Alibaba Cloud Model Studio, Сингапур).
    Картина сцены — первый кадр. Возвращает {индекс сцены: путь к mp4}; любая ошибка — сцена останется картиной."""
    key = os.environ.get("DASHSCOPE_KEY", "").strip()
    if not key:
        print("wan: skipped (no key)")
        return {}
    base = os.environ.get("DASHSCOPE_BASE", "https://dashscope-intl.aliyuncs.com").rstrip("/")
    model = os.environ.get("WAN_MODEL", "wan2.2-i2v-plus")
    res = os.environ.get("WAN_RES", "480P")
    count = int(os.environ.get("WAN_SCENES", "1") or 1)
    todo = [(i, s) for i, s in enumerate(scenes) if s.get("image_url") and not s.get("map")][:count]
    hdr = {"Authorization": "Bearer " + key, "Content-Type": "application/json", "X-DashScope-Async": "enable"}
    tasks = {}
    for i, s in todo:
        try:
            img = cover(load_image(s["image_url"]), 720, 1280)  # сразу вертикальный кадр 9:16
            buf = io.BytesIO()
            img.save(buf, "JPEG", quality=90)
            data = "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()
            prompt = (str(s.get("video_prompt") or "") + " " + WAN_MOTION).strip()[:790]
            body = {"model": model, "input": {"prompt": prompt, "img_url": data, "negative_prompt": AI_NEG[:490]},
                    "parameters": {"resolution": res, "prompt_extend": True, "watermark": False}}
            r = requests.post(base + "/api/v1/services/aigc/video-generation/video-synthesis",
                              json=body, headers=hdr, timeout=60)
            j = r.json()
            tid = (j.get("output") or {}).get("task_id")
            if not tid:
                print("wan submit failed", i, r.status_code, str(j)[:300])
                continue
            tasks[i] = tid
            print("wan submitted", i, tid)
        except Exception as e:  # noqa: BLE001
            print("wan submit error", i, str(e)[:200])
    out, t0 = {}, time.time()
    while tasks and time.time() - t0 < 600:
        time.sleep(15)
        for i in list(tasks):
            try:
                st = requests.get(base + "/api/v1/tasks/" + tasks[i], headers={"Authorization": "Bearer " + key},
                                  timeout=30).json()
                o = st.get("output") or {}
                status = o.get("task_status")
                if status == "SUCCEEDED" and o.get("video_url"):
                    path = os.path.join(tmp, f"wan_{i}.mp4")
                    rr = requests.get(o["video_url"], timeout=180)
                    rr.raise_for_status()
                    with open(path, "wb") as fh:
                        fh.write(rr.content)
                    out[i] = path
                    print("wan ready", i, round(time.time() - t0), "s", str(st.get("usage"))[:120])
                    del tasks[i]
                elif status in ("FAILED", "CANCELED", "UNKNOWN"):
                    print("wan failed", i, str(st)[:300])
                    del tasks[i]
            except Exception as e:  # noqa: BLE001
                print("wan poll error", i, str(e)[:160])
    for i in tasks:
        print("wan timeout", i)
    return out


# Учёт уже использованных стоковых видео: файл used_media.json живёт на GitHub Pages и переносится в каждый новый сайт.
_USED = {"loaded": False, "ids": []}


def used_media():
    if not _USED["loaded"]:
        _USED["loaded"] = True
        base = (os.environ.get("SITE_BASE") or "").rstrip("/") + "/"
        if base != "/":
            for attempt in range(3):
                try:
                    r = requests.get(base + "used_media.json", params={"t": int(time.time())}, timeout=30)
                    if r.status_code == 200:
                        _USED["ids"] = list(r.json().get("ids", []))
                    break
                except Exception as e:  # noqa: BLE001
                    print("used_media not loaded:", e)
                    time.sleep(3)
    return _USED["ids"]


def mark_used(mid):
    ids = used_media()
    if mid not in ids:
        ids.append(mid)


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
                if v.get("id") in used or ("pexels:%s" % v.get("id")) in used_media() or (v.get("duration") or 0) < 4:
                    continue
                files = [f for f in v.get("video_files", []) if (f.get("height") or 0) >= 1280 and (f.get("width") or 9999) < (f.get("height") or 0)]
                if files:
                    best = min(files, key=lambda f: abs((f.get("height") or 0) - 1920))
                    cands.append((v["id"], best["link"]))
            if not cands:
                print("pexels: nothing for", q)
                continue
            vid, link = rng.choice(cands[:6])
            used.add(vid)
            path = os.path.join(tmp, f"stock_{i}.mp4")
            rr = requests.get(link, timeout=180)
            rr.raise_for_status()
            with open(path, "wb") as fh:
                fh.write(rr.content)
            out[i] = path
            mark_used("pexels:%s" % vid)
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
                if h.get("id") in used or ("pixabay:%s" % h.get("id")) in used_media() or (h.get("duration") or 0) < 4:
                    continue
                vids = h.get("videos") or {}
                pick = next((vids[k] for k in ("large", "medium") if (vids.get(k) or {}).get("url") and (vids[k].get("height") or 0) >= 1080), None)
                if pick:
                    cands.append((h["id"], pick["url"]))
            if not cands:
                print("pixabay: nothing for", q)
                continue
            vid, link = rng.choice(cands[:6])
            used.add(vid)
            path = os.path.join(tmp, f"stock_{i}.mp4")
            rr = requests.get(link, timeout=180)
            rr.raise_for_status()
            with open(path, "wb") as fh:
                fh.write(rr.content)
            out[i] = path
            mark_used("pixabay:%s" % vid)
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


def safe_box(img, x0, y0, x1, y1):
    """Окно камеры внутри картинки (PIL не принимает box за её пределами)."""
    w, h = img.size
    bw, bh = min(x1 - x0, w), min(y1 - y0, h)
    x0 = min(max(x0, 0.0), w - bw)
    y0 = min(max(y0, 0.0), h - bh)
    return (x0, y0, x0 + bw, y0 + bh)

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


# ---------------------------------------------------------------- векторная карта (Natural Earth, общественное достояние)
# Рисуется заново под каждое окно камеры: берега и реки остаются чёткими при любом приближении.
try:
    import numpy as np
except Exception:  # noqa: BLE001
    np = None

NE_DIR = os.environ.get("NE_DIR", os.path.join(FONT_DIR, "ne"))
MAP_STYLE = os.environ.get("MAP_STYLE", "atlas")  # atlas | dark | nasa
_NE = {}

MAP_STYLES = {
    # пергамент старинного атласа
    "atlas": {"sea": (163, 182, 176), "sea_deep": (140, 162, 160), "land": (232, 214, 172), "land_hi": (196, 164, 118),
              "coast": (92, 74, 52), "ripple": (110, 130, 128), "river": (86, 122, 140), "label": (70, 92, 104),
              "shade": 0.55, "grain": 10, "grid": (120, 105, 80)},
    # тёмная «документальная»
    "dark": {"sea": (16, 22, 30), "sea_deep": (10, 14, 20), "land": (58, 50, 40), "land_hi": (96, 82, 62),
             "coast": (233, 196, 106), "ripple": (40, 52, 64), "river": (70, 104, 130), "label": (120, 140, 156),
             "shade": 0.7, "grain": 6, "grid": None},
}

# подписи морей, озёр и рек (рисуются в фон карты, курсивом)
WATER_LABELS = [
    ("Каспийское море", 42.0, 50.8, 1), ("Аральское море", 45.2, 59.6, 1), ("оз. Балхаш", 46.4, 74.5, 1),
    ("Чёрное море", 43.3, 34.0, 1), ("Байкал", 53.5, 108.2, 1), ("Иссык-Куль", 42.4, 77.3, 2),
    ("Средиземное море", 34.5, 18.0, 0), ("Персидский залив", 27.0, 51.5, 1), ("Жёлтое море", 35.5, 123.5, 1),
    ("Японское море", 40.5, 135.0, 1), ("Аравийское море", 15.0, 64.0, 0), ("Бенгальский залив", 15.0, 88.5, 0),
    ("Индийский океан", 0.0, 80.0, 0), ("Тихий океан", 30.0, 150.0, 0), ("Атлантический океан", 30.0, -40.0, 0),
    ("Северный Ледовитый океан", 78.0, 80.0, 0), ("Балтийское море", 57.0, 19.5, 1),
    ("Сырдарья", 44.6, 66.0, 2), ("Амударья", 40.2, 62.8, 2), ("Волга", 49.0, 45.3, 2), ("Иртыш", 51.0, 76.5, 2),
    ("Или", 44.0, 77.5, 2), ("Урал", 50.2, 51.9, 2), ("Хуанхэ", 37.3, 111.5, 2), ("Янцзы", 29.8, 114.5, 2),
    ("Дунай", 45.0, 22.5, 2), ("Днепр", 49.5, 33.0, 2), ("Обь", 61.0, 74.0, 2), ("Енисей", 62.0, 88.5, 2),
    ("Инд", 27.5, 68.6, 2), ("Тигр", 35.0, 43.5, 2), ("Керулен", 47.7, 113.5, 2), ("Селенга", 49.8, 106.4, 2),
]


def _rings_of(geom):
    if not geom:
        return []
    t, c = geom["type"], geom["coordinates"]
    if t == "Polygon":
        return [c]
    if t == "MultiPolygon":
        return c
    return []


def _lines_of(geom):
    if not geom:
        return []
    t, c = geom["type"], geom["coordinates"]
    if t == "LineString":
        return [c]
    if t == "MultiLineString":
        return c
    return []


def _arr(coords):
    a = np.asarray(coords, dtype=np.float64)[:, :2]
    return a, (a[:, 0].min(), a[:, 1].min(), a[:, 0].max(), a[:, 1].max())


def ne_data():
    """Суша, озёра (с историческим Аралом), реки и рельеф. None — если данных нет (тогда карта NASA)."""
    if "ok" in _NE:
        return _NE if _NE["ok"] else None
    if np is None:
        _NE["ok"] = False
        return None
    try:
        def load(name):
            with open(os.path.join(NE_DIR, name + ".geojson"), encoding="utf-8") as f:
                return json.load(f)["features"]
        land = []
        for ft in load("ne_10m_land"):
            for poly in _rings_of(ft["geometry"]):
                rings = [_arr(r) for r in poly if len(r) >= 3]
                if rings and rings[0][1][3] > -60:  # без Антарктиды
                    land.append(rings)
        lakes = []
        for ft in load("ne_10m_lakes"):
            p = ft["properties"]
            if "Aral" in str(p.get("name") or "") or "Reservoir" in str(p.get("featurecla") or ""):
                continue  # современный обмелевший Арал и водохранилища — не для исторических карт
            for poly in _rings_of(ft["geometry"]):
                rings = [_arr(r) for r in poly if len(r) >= 3]
                if rings:
                    lakes.append((float(p.get("scalerank") or 9), rings))
        try:
            for ft in load("ne_10m_lakes_historic"):
                if str(ft["properties"].get("name")) in ("Aral Sea", "Lop Nur"):
                    for poly in _rings_of(ft["geometry"]):
                        lakes.append((0.0, [_arr(r) for r in poly if len(r) >= 3]))
        except Exception:  # noqa: BLE001
            pass
        rivers = []
        for ft in load("ne_10m_rivers_lake_centerlines"):
            sr = float(ft["properties"].get("scalerank") or 9)
            if sr > 7:
                continue
            for ln in _lines_of(ft["geometry"]):
                if len(ln) >= 2:
                    rivers.append((sr,) + _arr(ln))
        elev = None
        bp = os.path.join(NE_DIR, "elevation.jpg")
        if os.path.exists(bp):
            elev = np.asarray(Image.open(bp).convert("RGB").split()[0], dtype=np.float32) / 255.0
        _NE.update(ok=True, land=land, lakes=lakes, rivers=rivers, elev=elev)
        print("vector map: land", len(land), "lakes", len(lakes), "rivers", len(rivers), "relief", elev is not None)
        return _NE
    except Exception as e:  # noqa: BLE001
        print("vector map unavailable:", str(e)[:200])
        _NE["ok"] = False
        return None


def _box_blur(a, r):
    """Размытие массива (3 прохода скользящего среднего ≈ гаусс)."""
    r = int(max(1, r))
    for _ in range(3):
        for ax in (0, 1):
            pad = [(0, 0), (0, 0)]
            pad[ax] = (r + 1, r)
            c = np.cumsum(np.pad(a, pad, mode="edge"), axis=ax, dtype=np.float64)
            a = ((np.take(c, range(2 * r + 1, c.shape[ax]), axis=ax) - np.take(c, range(0, c.shape[ax] - 2 * r - 1), axis=ax)) / (2 * r + 1)).astype(np.float32)
    return a


def _hit(bb, view):
    return not (bb[2] < view[0] or bb[0] > view[2] or bb[3] < view[1] or bb[1] > view[3])


def _value_noise(w, h, cell, rng):
    g = rng.random((h // cell + 3, w // cell + 3)).astype(np.float32)
    im = Image.fromarray((g * 255).astype(np.uint8)).resize(((w // cell + 3) * cell, (h // cell + 3) * cell), Image.BICUBIC)
    return np.asarray(im, dtype=np.float32)[:h, :w] / 255.0


def vector_base(lon0, lat0, lon1, lat1, w, h, style="atlas", seed=7):
    """Карта окна [lon0..lon1] × [lat1..lat0] (lat0 — верх) размером w×h пикселей."""
    D = ne_data()
    st = MAP_STYLES.get(style, MAP_STYLES["atlas"])
    sx, sy = w / (lon1 - lon0), h / (lat0 - lat1)
    ppd = sx  # пикселей на градус
    view = (lon0 - 1, lat1 - 1, lon1 + 1, lat0 + 1)

    SSb = 2  # сглаживание: контуры рисуются вдвое крупнее и уменьшаются

    def proj(a):
        return [((x - lon0) * sx * SSb, (lat0 - y) * sy * SSb) for x, y in a]

    # маска суши
    mask2 = Image.new("L", (w * SSb, h * SSb), 0)
    md = ImageDraw.Draw(mask2)
    for rings in D["land"]:
        if not _hit(rings[0][1], view):
            continue
        md.polygon(proj(rings[0][0]), fill=255)
        for a, bb in rings[1:]:
            if _hit(bb, view):
                md.polygon(proj(a), fill=0)
    lake_max_rank = 7 if ppd > 30 else (4 if ppd > 14 else 2)
    for sr, rings in D["lakes"]:
        if sr > lake_max_rank or not _hit(rings[0][1], view):
            continue
        md.polygon(proj(rings[0][0]), fill=0)
        for a, bb in rings[1:]:
            md.polygon(proj(a), fill=255)
    mask = mask2.reduce(SSb)
    M = np.asarray(mask, dtype=np.float32) / 255.0
    rng = np.random.default_rng(seed)

    # рельеф: отмывка по карте высот
    shade = np.zeros((h, w), np.float32)
    hi = np.zeros((h, w), np.float32)
    if D.get("elev") is not None:
        E = D["elev"]
        EH, EW = E.shape
        ex0, ex1 = (lon0 + 180) / 360 * EW, (lon1 + 180) / 360 * EW
        ey0, ey1 = (90 - lat0) / 180 * EH, (90 - lat1) / 180 * EH
        pad = 4
        X0, Y0 = int(math.floor(ex0)) - pad, int(math.floor(ey0)) - pad
        X1, Y1 = int(math.ceil(ex1)) + pad, int(math.ceil(ey1)) + pad
        buf = np.zeros((Y1 - Y0, X1 - X0), np.float32)
        cx0, cy0, cx1, cy1 = max(X0, 0), max(Y0, 0), min(X1, EW), min(Y1, EH)
        if cx1 > cx0 and cy1 > cy0:
            buf[cy0 - Y0:cy1 - Y0, cx0 - X0:cx1 - X0] = E[cy0:cy1, cx0:cx1]
        im = Image.fromarray(buf, mode="F").resize((w, h), Image.BICUBIC,
                                                    box=(ex0 - X0, ey0 - Y0, ex1 - X0, ey1 - Y0))
        el = _box_blur(np.asarray(im, np.float32), max(1.0, ppd / 20))
        gy, gx = np.gradient(el)
        k = 2.0 * max(ppd, 1.0)  # уклон на градус — одинаковая сила рельефа при любом масштабе
        shade = np.clip((gx + gy) * -k, -1, 1)  # свет с северо-запада
        hi = np.clip((el - 0.08) * 2.2, 0, 1)

    def C(c):
        return np.array(c, np.float32)[None, None, :]

    land = C(st["land"]) * (1 - hi[..., None] * 0.9) + C(st["land_hi"]) * (hi[..., None] * 0.9)
    land = land * (1 + shade[..., None] * st["shade"] * 0.5)
    # море: чуть темнее вдали от берега + береговая «рябь» старинных карт
    blur = np.asarray(mask.filter(ImageFilter.GaussianBlur(max(6, ppd * 0.9))), np.float32) / 255.0
    sea = C(st["sea_deep"]) * (1 - blur[..., None]) + C(st["sea"]) * blur[..., None]
    rip = np.zeros((h, w), np.float32)
    bl2 = np.asarray(mask.filter(ImageFilter.GaussianBlur(max(5, min(22, ppd * 0.45)))), np.float32) / 255.0
    for lev, a in ((0.30, 0.8), (0.16, 0.55), (0.07, 0.35)):
        rip = np.maximum(rip, a * np.clip(1 - np.abs(bl2 - lev) / 0.018, 0, 1))
    rip *= (1 - M)
    sea = sea * (1 - rip[..., None]) + C(st["ripple"]) * rip[..., None]
    img = land * M[..., None] + sea * (1 - M[..., None])
    # бумага: крупные пятна + зерно
    g = st["grain"]
    if g:  # только мягкие «пятна» бумаги: мелкое зерно в видео мерцает и портит сжатие
        n1 = _value_noise(w, h, 90, rng) - 0.5
        img = img + (n1 * g * 1.6)[..., None]
    out = Image.fromarray(np.clip(img, 0, 255).astype(np.uint8), "RGB")
    lines = Image.new("RGBA", (w * SSb, h * SSb), (0, 0, 0, 0))
    d = ImageDraw.Draw(lines)
    # градусная сетка (атлас)
    if st.get("grid"):
        step = 10 if ppd > 6 else 20
        for lon in range(-180, 181, step):
            if lon0 <= lon <= lon1:
                x = (lon - lon0) * sx * SSb
                d.line([(x, 0), (x, h * SSb)], fill=st["grid"] + (38,), width=2 * SSb)
        for lat in range(-80, 81, step):
            if lat1 <= lat <= lat0:
                y = (lat0 - lat) * sy * SSb
                d.line([(0, y), (w * SSb, y)], fill=st["grid"] + (38,), width=2 * SSb)
    # реки
    riv_max = 7 if ppd > 30 else (6 if ppd > 14 else 5)
    for sr, a, bb in D["rivers"]:
        if sr > riv_max or not _hit(bb, view):
            continue
        wd = max(2, int(round(SSb * (3.2 if sr <= 3 else 2.4 if sr <= 5 else 1.6) * min(1.6, max(0.6, ppd / 18)))))
        d.line(proj(a), fill=st["river"] + (230,), width=wd, joint="curve")
    lines = lines.reduce(SSb)
    out.paste(lines, (0, 0), lines)
    # береговая линия (по маске двойного разрешения — гладкая)
    edge = mask2.filter(ImageFilter.FIND_EDGES).point(lambda v: 255 if v > 60 else 0)
    edge = edge.filter(ImageFilter.MaxFilter(5 if ppd > 20 else 3)).reduce(SSb)
    coast = Image.new("RGBA", (w, h), st["coast"] + (0,))
    coast.putalpha(edge.point(lambda v: int(v * 0.8)))
    if style == "dark":
        glow = coast.filter(ImageFilter.GaussianBlur(4))
        out.paste(glow, (0, 0), glow)
    out.paste(coast, (0, 0), coast)
    return out, mask


def water_labels(draw, lon0, lat0, lon1, lat1, w, h, style, font_fn, ok=None):
    """Подписи морей/рек курсивом — в фон, под территориями и стрелками; без наложений."""
    st = MAP_STYLES.get(style, MAP_STYLES["atlas"])
    sx, sy = w / (lon1 - lon0), h / (lat0 - lat1)
    ppd = sx
    placed = []
    for name, lat, lon, kind in sorted(WATER_LABELS, key=lambda r: (1, 2, 0).index(r[3])):
        if not (lon0 < lon < lon1 and lat1 < lat < lat0):
            continue
        if kind == 0 and ppd > 40:
            continue  # океаны — только на крупных картах
        if (kind == 1 and ppd < 12) or (kind == 2 and ppd < 16):
            continue  # моря поменьше и реки — только при приближении
        size = 34 if kind == 0 else (30 if kind == 1 else 26)
        f = font_fn(size)
        tw = draw.textlength(name, font=f)
        x, y = (lon - lon0) * sx - tw / 2, (lat0 - lat) * sy - size / 2
        bx = (x - 10, y - 8, x + tw + 10, y + size + 8)
        if bx[0] < 0 or bx[2] > w or bx[1] < 0 or bx[3] > h:
            continue
        if any(not (bx[2] < q[0] or bx[0] > q[2] or bx[3] < q[1] or bx[1] > q[3]) for q in placed):
            continue
        if ok is not None and not ok(bx):
            continue
        placed.append(bx)
        draw.text((x, y), name, font=f, fill=st["label"] + (235,))


def font_italic(size):
    for p in (os.path.join(FONT_DIR, "PlayfairItalic.ttf"),
              "/usr/share/fonts/truetype/dejavu/DejaVuSerif-Italic.ttf",
              "/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf"):
        if os.path.exists(p) and os.path.getsize(p) > 10000:
            return _font(p, size, 500 if "Playfair" in p else None)
    return font_title(size)


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


# Примерные границы держав (широта, долгота). Грубые контуры для наглядности — в кадре подпись «границы примерные».
TERRITORIES = {
    "mongol_1206": ("Монгольское государство", "1206", [(50.5, 88), (52, 95), (52, 105), (51, 112), (50, 118), (48, 120), (45, 118), (42, 112), (41, 104), (42, 96), (44, 90), (47, 87)]),
    "jin_1206": ("Империя Цзинь", "1206", [(48, 118), (52, 124), (50, 132), (45, 135), (41, 130), (39, 124), (37, 122), (34, 120), (32.5, 116), (33, 111), (34.5, 106), (38, 107), (41, 110), (43, 114), (45, 117)]),
    "xixia_1206": ("Си Ся (тангуты)", "1206", [(42, 97), (41, 104), (39, 107), (36, 106), (35, 103), (37, 99), (39, 96), (41, 95)]),
    "khwarezm_1215": ("Хорезм", "1215", [(44, 60), (45, 66), (43.5, 70), (41, 72), (38, 71), (36, 70), (34, 68), (30, 66), (26, 62), (26, 56), (28, 52), (31, 48), (35, 47), (38, 48), (40, 53), (42, 56)]),
    "mongol_1227": ("Империя Чингисхана", "1227", [(52, 62), (53, 80), (53, 100), (52, 115), (49, 122), (43, 122), (40, 117), (38, 110), (37, 103), (39, 96), (37, 86), (36, 74), (34, 67), (35, 59), (38, 54), (42, 52), (47, 52), (50, 55)]),
    "mongol_1279": ("Монгольская империя", "1279", [(56, 28), (60, 40), (60, 55), (57, 70), (55, 85), (53, 100), (53, 115), (53, 128), (45, 135), (40, 128), (31, 122), (24, 118), (22, 110), (22, 103), (26, 98), (29, 92), (30, 80), (33, 72), (31, 66), (27, 62), (26, 56), (29, 50), (32, 46), (34, 43), (37, 40), (40, 42), (42, 45), (44, 40), (46, 34), (48, 28), (52, 25)]),
    "golden_horde_1300": ("Золотая Орда", "XIV в.", [(47.5, 29), (50, 32), (53, 38), (56, 45), (56.5, 52), (56, 60), (55, 68), (53, 77), (49, 80), (45, 78), (43, 72), (43.5, 66), (42, 60), (41.5, 58), (43, 52), (44, 47), (43, 44), (44.5, 38), (44.5, 34), (45.5, 30)]),
    "chagatai_1300": ("Улус Чагатая", "XIV в.", [(46, 70), (47, 78), (45, 85), (43, 92), (40, 92), (37, 85), (36, 77), (37, 72), (36, 67), (37.5, 63), (40, 63), (42, 66), (44, 68)]),
    "ilkhanate_1300": ("Хулагуиды", "XIV в.", [(40, 40), (41, 48), (38, 54), (37, 62), (35, 65), (30, 64), (25, 61), (26, 54), (29, 50), (31, 47), (33, 44), (36, 40), (37, 37), (39, 38)]),
    "yuan_1300": ("Империя Юань", "XIV в.", [(52, 90), (53, 110), (53, 128), (45, 135), (40, 128), (36, 121), (31, 122), (24, 118), (22, 110), (21, 103), (25, 98), (29, 92), (32, 80), (36, 78), (42, 88), (47, 88)]),
    "moghulistan_1400": ("Моголистан", "XV в.", [(46, 74), (47, 80), (45, 85), (43, 90), (40, 90), (37.5, 80), (38.5, 74), (40, 71), (42, 70), (44, 71)]),
    "abulkhair_1450": ("Ханство Абулхаира", "XV в.", [(54, 55), (55, 65), (54, 72), (51, 74), (48, 72), (45, 69), (43.5, 66), (43, 61), (45, 57), (48, 53), (51, 52)]),
    "kazakh_khanate_1600": ("Казахское ханство", "XVI–XVII вв.", [(52, 51), (55, 60), (55, 70), (53, 78), (50, 84), (47, 83), (45, 80), (43, 77), (42, 71), (41, 69), (43, 64), (44, 59), (46, 54), (48, 50)]),
}
TERR_COLORS = [(214, 64, 52), (233, 196, 106), (64, 140, 160), (150, 90, 170), (230, 130, 50), (90, 160, 90)]
# на светлом пергаменте нужны более густые цвета
TERR_COLORS_LIGHT = [(196, 44, 36), (34, 104, 168), (46, 132, 70), (128, 62, 152), (214, 112, 20), (120, 84, 50)]


def _ll(v):
    try:
        lat, lon = float(v[0]), float(v[1])
    except (TypeError, ValueError, IndexError, KeyError):
        try:
            lat, lon = float(v.get("lat")), float(v.get("lon"))
        except Exception:  # noqa: BLE001
            return None
    if -80 <= lat <= 82 and -180 <= lon <= 180:
        return (lat, lon)
    return None


def _chaikin(poly, it=3):
    """Сглаживание ломаных контуров держав (срез углов), чтобы границы выглядели естественно."""
    for _ in range(it):
        out = []
        for i in range(len(poly)):
            a, b = poly[i], poly[(i + 1) % len(poly)]
            out.append((0.75 * a[0] + 0.25 * b[0], 0.75 * a[1] + 0.25 * b[1]))
            out.append((0.25 * a[0] + 0.75 * b[0], 0.25 * a[1] + 0.75 * b[1]))
        poly = out
    return poly


def map_territories(m):
    out = []
    for i, t in enumerate((m or {}).get("territories", [])[:5]):
        if isinstance(t, str):
            t = {"key": t}
        if not isinstance(t, dict):
            continue
        key = str(t.get("key") or "").strip()
        if key in TERRITORIES:
            name, year, poly = TERRITORIES[key]
            fam = key.split("_")[0]  # mongol_1206 → mongol_1227 → mongol_1279: одна держава растёт
        else:
            poly = [p for p in (_ll(v) for v in (t.get("polygon") or [])) if p]
            name, year = str(t.get("name") or ""), str(t.get("year") or "")
            fam = "custom%d" % i
        if len(poly) >= 3:
            out.append({"name": str(t.get("name") or name)[:30], "year": str(t.get("year") or year)[:14],
                        "poly": _chaikin(list(poly)), "fam": fam})
    return out


def map_arrows(m):
    out = []
    for a in (m or {}).get("arrows", [])[:4]:
        if not isinstance(a, dict):
            continue
        pts = [p for p in (_ll(v) for v in (a.get("points") or [])) if p]
        if not pts:
            f, t = _ll(a.get("from") or []), _ll(a.get("to") or [])
            pts = [p for p in (f, t) if p]
        if len(pts) >= 2:
            out.append({"pts": pts, "label": str(a.get("label") or "")[:26]})
    return out


def has_map(m):
    return bool(m) and bool(map_points(m) or map_territories(m) or map_arrows(m))


class MapScene:
    """Анимированная карта: территории держав заливаются по очереди (рост империи),
    стрелки походов прорисовываются, города появляются по ходу, камера плавно приближается."""

    def __init__(self, m, n, style=None):
        style = str(style or m.get("style") or MAP_STYLE)
        D = ne_data() if style != "nasa" else None
        W = None if D else world_map()
        if D is None and W is None:
            raise RuntimeError("no world map")
        Wd, Ht = (5400, 2700) if D else W.size
        self.n = n
        self.pts = map_points(m)
        self.terrs = map_territories(m)
        self.arrows = map_arrows(m)
        self.route = bool(m.get("route", True)) and len(self.pts) > 1 and not self.arrows
        self.title = str(m.get("title") or "").upper()[:42]
        P = lambda la, lo: ((lo + 180) / 360 * Wd, (90 - la) / 180 * Ht)
        allxy = [P(p["lat"], p["lon"]) for p in self.pts]
        for t in self.terrs:
            t["wxy"] = [P(la, lo) for la, lo in t["poly"]]
            allxy += t["wxy"]
        for a in self.arrows:
            a["wxy"] = [P(la, lo) for la, lo in a["pts"]]
            allxy += a["wxy"]
        xs, ys = [a for a, _ in allxy], [b for _, b in allxy]
        # Всё, что нужно показать, вписываем в «окно» кадра между заголовком и субтитрами (980×880 px),
        # остальная часть вертикального кадра — продолжение карты.
        cw = max((max(xs) - min(xs)) * 1.12, 1.0)
        ch = max((max(ys) - min(ys)) * 1.12, 1.0)
        scale = min(980 / cw, 880 / ch, VW / (Wd * 20 / 360))
        ccx, ccy = (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2
        ax, ay = VW / 2, 820
        def box(k):
            sc = scale / k
            x0, y0 = ccx - ax / sc, ccy - ay / sc
            return (x0, y0, x0 + VW / sc, y0 + VH / sc)
        self.b1, self.b2 = box(1.15), box(1.0)
        X0, Y0 = int(math.floor(self.b1[0])), int(math.floor(self.b1[1]))
        X1, Y1 = int(math.ceil(self.b1[2])) + 1, int(math.ceil(self.b1[3])) + 1
        self.f = 1.0
        self.land = None  # маска суши: заливки держав не заходят в моря
        if D:
            # векторная карта рисуется сразу в разрешении кадра
            sw, sh = int(round((X1 - X0) * scale)), int(round((Y1 - Y0) * scale))
            lon0, lon1 = X0 / Wd * 360 - 180, X1 / Wd * 360 - 180
            lat0, lat1 = 90 - Y0 / Ht * 180, 90 - Y1 / Ht * 180
            sub, self.land = vector_base(lon0, lat0, lon1, lat1, sw, sh, style)
            self.f = fs = sw / (X1 - X0)

            def inside(bx):  # подпись не должна попадать под заголовок и субтитры ни в начале, ни в конце наезда
                for bb in (self.b1, self.b2):
                    k = VW / (bb[2] - bb[0])
                    fx0, fx1 = (bx[0] / fs + X0 - bb[0]) * k, (bx[2] / fs + X0 - bb[0]) * k
                    fy0, fy1 = (bx[1] / fs + Y0 - bb[1]) * k, (bx[3] / fs + Y0 - bb[1]) * k
                    if fx0 < 30 or fx1 > VW - 30 or fy0 < 390 or fy1 > VH * 0.64:
                        return False
                return True
            water_labels(ImageDraw.Draw(sub, "RGBA"), lon0, lat0, lon1, lat1, sw, sh, style, font_italic, inside)
            ix0 = ix1 = 0
        else:
            sub = Image.new("RGB", (X1 - X0, Y1 - Y0), (34, 27, 20))
            ix0, iy0, ix1, iy1 = max(X0, 0), max(Y0, 0), min(X1, Wd), min(Y1, Ht)
        if ix1 > ix0 and iy1 > iy0:
            sub.paste(W.crop((ix0, iy0, ix1, iy1)), (ix0 - X0, iy0 - Y0))
            # край карты (полюса) не обрываем резкой линией — мягко растворяем в тёмный фон
            F = int(min(300 / scale, 20 * Ht / 180))
            if F > 4:
                ramp = Image.linear_gradient("L").resize((ix1 - ix0, F))  # 0 сверху → 255 снизу
                fill = Image.new("RGB", (ix1 - ix0, F), (34, 27, 20))
                if Y0 < 0:
                    y = iy0 - Y0
                    sub.paste(fill, (ix0 - X0, y), ImageOps.invert(ramp))
                if Y1 > Ht:  # снизу — Антарктида: растворяем глубже
                    Fb = int(min(500 / scale, 35 * Ht / 180))
                    rb = Image.linear_gradient("L").resize((ix1 - ix0, Fb))
                    sub.paste(Image.new("RGB", (ix1 - ix0, Fb), (34, 27, 20)), (ix0 - X0, iy1 - Y0 - Fb), rb)
        self.sub, self.off = sub, (X0, Y0)
        self.xy = [P(p["lat"], p["lon"]) for p in self.pts]
        seg = [math.dist(self.xy[i], self.xy[i + 1]) for i in range(len(self.xy) - 1)]
        self.total = sum(seg) or 1
        acc, self.at = 0, [0.0]
        for d in seg:
            acc += d
            self.at.append(acc / self.total)
        # расписание: территории → стрелки → города
        nt, na = len(self.terrs), len(self.arrows)
        self.t_terr = [0.05 + i * (0.45 / max(1, nt)) for i in range(nt)]
        a0 = 0.15 if nt == 0 else 0.3
        self.t_arrow = [(a0 + i * (0.5 / max(1, na)), a0 + (i + 1) * (0.5 / max(1, na))) for i in range(na)]
        self.light = bool(D) and style == "atlas"
        self.lf = font_sans(40, 800)
        self.tf = font_sans(46, 900)
        self.sf = font_sans(26, 600)
        self.yf = font_title(88)

    @staticmethod
    def _bez(p0, p1, bend=0.18):
        """Плавная дуга между двумя точками (для стрелок походов)."""
        mx, my = (p0[0] + p1[0]) / 2, (p0[1] + p1[1]) / 2
        dx, dy = p1[0] - p0[0], p1[1] - p0[1]
        c = (mx - dy * bend, my + dx * bend)
        return [((1 - s) ** 2 * p0[0] + 2 * (1 - s) * s * c[0] + s * s * p1[0],
                 (1 - s) ** 2 * p0[1] + 2 * (1 - s) * s * c[1] + s * s * p1[1]) for s in [i / 24 for i in range(25)]]

    def _arrow(self, d, pts, prog, color, k=1):
        path = []
        for i in range(len(pts) - 1):
            path += self._bez(pts[i], pts[i + 1])[(1 if i else 0):]
        cut = max(2, int(len(path) * prog))
        part = path[:cut]
        if len(part) < 2:
            return
        d.line(part, fill=(0, 0, 0, 170), width=24 * k, joint="curve")
        d.line(part, fill=color + (255,), width=14 * k, joint="curve")
        a, c = part[-2], part[-1]
        ang = math.atan2(c[1] - a[1], c[0] - a[0])
        L = max(24 * k, min(46 * k, 0.3 * math.dist(path[0], path[-1])))
        tri = [(c[0] + 10 * k * math.cos(ang), c[1] + 10 * k * math.sin(ang)),
               (c[0] - L * math.cos(ang - 0.5), c[1] - L * math.sin(ang - 0.5)),
               (c[0] - L * math.cos(ang + 0.5), c[1] - L * math.sin(ang + 0.5))]
        d.polygon(tri, fill=color + (255,), outline=(0, 0, 0, 200))

    def frame(self, fi):
        t = fi / max(1, self.n - 1)
        e = t * t * (3 - 2 * t)
        b = [self.b1[i] + (self.b2[i] - self.b1[i]) * e for i in range(4)]
        ox, oy = self.off
        f = self.f
        cam = safe_box(self.sub, (b[0] - ox) * f, (b[1] - oy) * f, (b[2] - ox) * f, (b[3] - oy) * f)
        img = self.sub.resize((VW, VH), Image.BICUBIC, box=cam).convert("RGBA")
        sx, sy = VW / (b[2] - b[0]), VH / (b[3] - b[1])
        S = lambda p: ((p[0] - b[0]) * sx, (p[1] - b[1]) * sy)
        K = 2  # сглаживание: заливки, стрелки и точки рисуются вдвое крупнее и уменьшаются
        S2 = lambda p: ((p[0] - b[0]) * sx * K, (p[1] - b[1]) * sy * K)
        # территории (полупрозрачная заливка на отдельном слое)
        year_now = ""
        # прежние границы той же державы (рост империи) уходят на второй план, подпись остаётся только у новой
        sup = [0.0] * len(self.terrs)
        for i, tr in enumerate(self.terrs):
            for j in range(i + 1, len(self.terrs)):
                if self.terrs[j]["fam"] == tr["fam"]:
                    sup[i] = max(sup[i], min(1.0, max(0.0, (t - self.t_terr[j]) / 0.12)))
        if self.terrs:
            layer = Image.new("RGBA", (VW * K, VH * K), (0, 0, 0, 0))
            for i, tr in enumerate(self.terrs):
                a = min(1.0, max(0.0, (t - self.t_terr[i]) / 0.12))
                if a <= 0:
                    continue
                pal = TERR_COLORS_LIGHT if self.light else TERR_COLORS
                col = pal[i % len(pal)]
                poly = [S2(p) for p in tr["wxy"]]
                k = 1.0 - 0.6 * sup[i]
                # каждая держава — на своём кусочке слоя, затем смешивание (иначе новая заливка стирает прежнюю)
                lw = (6 if sup[i] < 0.5 else 3) * K
                x0 = int(max(0, min(p[0] for p in poly) - lw)); y0 = int(max(0, min(p[1] for p in poly) - lw))
                x1 = int(min(VW * K, max(p[0] for p in poly) + lw + 1)); y1 = int(min(VH * K, max(p[1] for p in poly) + lw + 1))
                if a >= 0.5:
                    year_now = tr["year"] or year_now
                if x1 <= x0 or y1 <= y0:
                    continue
                piece = Image.new("RGBA", (x1 - x0, y1 - y0), (0, 0, 0, 0))
                pd = ImageDraw.Draw(piece)
                lp = [(x - x0, y - y0) for x, y in poly]
                pd.polygon(lp, fill=col + (int((105 if self.light else 95) * a * k),))
                pd.line(lp + [lp[0]], fill=col + (int(255 * a * k),), width=lw, joint="curve")
                layer.alpha_composite(piece, (x0, y0))
            layer = layer.reduce(K)
            if self.land is not None:
                lm = self.land.resize((VW, VH), Image.BILINEAR, box=cam)
                layer.putalpha(ImageChops.multiply(layer.getchannel("A"), lm))
            img.alpha_composite(layer)
        geo = Image.new("RGBA", (VW * K, VH * K), (0, 0, 0, 0))
        gd = ImageDraw.Draw(geo)
        d = ImageDraw.Draw(img)
        tlabels, texts = [], []  # подписи рисуются после геометрии, поверх неё
        # стрелки походов
        for i, ar in enumerate(self.arrows):
            s0, s1 = self.t_arrow[i]
            prog = min(1.0, max(0.0, (t - s0) / max(0.05, s1 - s0)))
            if prog > 0:
                self._arrow(gd, [S2(p) for p in ar["wxy"]], prog, RED, K)
                if prog >= 1 and ar["label"]:
                    p0, p1 = S(ar["wxy"][0]), S(ar["wxy"][-1])
                    mx, my = (p0[0] + p1[0]) / 2, (p0[1] + p1[1]) / 2
                    ln = math.dist(p0, p1) or 1.0
                    nx, ny = -(p1[1] - p0[1]) / ln, (p1[0] - p0[0]) / ln  # перпендикуляр к стрелке
                    lab = ar["label"].upper()
                    tw = d.textlength(lab, font=self.sf)
                    avoid = [(x - 34, y - 34, x + 34, y + 34) for x, y in (S(p) for p in self.xy)] + tlabels
                    best = None
                    for dist in (46, 74, 104):
                        for sg in (-1, 1):
                            cx, cy = mx + nx * dist * sg, my + ny * dist * sg
                            lx, ly = min(max(cx - tw / 2, 30), VW - 30 - tw), cy - 18
                            bx = (lx - 4, ly - 4, lx + tw + 4, ly + 36)
                            if not any(not (bx[2] < q[0] or bx[0] > q[2] or bx[3] < q[1] or bx[1] > q[3]) for q in avoid):
                                best = (lx, ly, bx)
                                break
                        if best:
                            break
                    if best is None:
                        lx, ly = min(max(mx - tw / 2, 30), VW - 30 - tw), my - 58
                        best = (lx, ly, (lx - 4, ly - 4, lx + tw + 4, ly + 36))
                    texts.append((best[:2], lab, self.sf, GOLD, 5))
                    tlabels.append(best[2])
        # маршрут по городам (если нет отдельных стрелок)
        P = [S(p) for p in self.xy]
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
                self._arrow(gd, [(x * K, y * K) for x, y in pts], 1.0, RED, K)
        # города
        start = 0.08 if not self.terrs else 0.35
        vis = [((prog >= self.at[k] - 1e-6) if self.route else (t >= start + k * 0.1)) for k in range(len(P))]
        placed = [(x - 22, y - 22, x + 22, y + 22) for k, (x, y) in enumerate(P) if vis[k]] + tlabels
        for k, (x, y) in enumerate(P):
            if not vis[k]:
                continue
            r, X, Y = 15 * K, x * K, y * K
            gd.ellipse((X - r - 4 * K, Y - r - 4 * K, X + r + 4 * K, Y + r + 4 * K), fill=(0, 0, 0, 150))
            gd.ellipse((X - r, Y - r, X + r, Y + r), fill=GOLD + (255,), outline=(255, 255, 255, 255), width=4 * K)
            name = self.pts[k]["name"].upper()
            if not name:
                continue
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
            texts.append(((best[0], best[1]), name, self.lf, WHITE, 6))
        img.alpha_composite(geo.reduce(K))
        for xy, txt, fnt, col, sw in texts:
            d.text(xy, txt, font=fnt, fill=col, stroke_width=sw, stroke_fill=(0, 0, 0))
        # подписи держав — после городов и стрелок, не налезая на них
        for i in reversed(range(len(self.terrs))):
            tr = self.terrs[i]
            if t < self.t_terr[i] + 0.04 or not tr["name"] or sup[i] > 0.5:
                continue
            poly = [S(p) for p in tr["wxy"]]
            cxp = sum(p[0] for p in poly) / len(poly)
            cyp = sum(p[1] for p in poly) / len(poly)
            name = tr["name"].upper()
            tw = d.textlength(name, font=self.lf)
            x = min(max(cxp - tw / 2, 30), VW - 30 - tw)
            for dy in (0, -64, 64, -128, 128):
                y = min(max(cyp - 24 + dy, 400), VH * 0.62)
                bx = (x - 8, y - 6, x + tw + 8, y + 52)
                if not any(not (bx[2] < q[0] or bx[0] > q[2] or bx[3] < q[1] or bx[1] > q[3]) for q in placed):
                    placed.append(bx)
                    d.text((x, y), name, font=self.lf, fill=(255, 244, 214), stroke_width=6, stroke_fill=(0, 0, 0))
                    break
        # заголовок, год, пометка о точности
        if self.title:
            tw = d.textlength(self.title, font=self.tf)
            d.text(((VW - tw) / 2, 262), self.title, font=self.tf, fill=GOLD, stroke_width=6, stroke_fill=(0, 0, 0))
        if self.terrs:
            note = "границы примерные"
            tw = d.textlength(note, font=self.sf)
            d.text(((VW - tw) / 2, 322), note, font=self.sf, fill=(235, 225, 205), stroke_width=4, stroke_fill=(0, 0, 0))
            # крупный год — только для «роста империи», если в заголовке своей даты нет
            if year_now and len({tr["year"] for tr in self.terrs if tr["year"]}) > 1 \
                    and not any(ch.isdigit() for ch in self.title):
                yw = d.textlength(year_now, font=self.yf)
                d.text((VW - 70 - yw, 380), year_now, font=self.yf, fill=GOLD, stroke_width=6, stroke_fill=(0, 0, 0))
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
        # 27.10: диктор чуть быстрее (по умолчанию +5%), без смены высоты голоса.
        tempo = float(job.get("voice_tempo") or os.environ.get("VOICE_TEMPO") or 1.05)
        if abs(tempo - 1.0) > 0.005:
            fast = os.path.join(tmp, "voice_fast.mp3")
            try:
                subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", audio, "-filter:a", f"atempo={tempo:.3f}",
                                "-c:a", "libmp3lame", "-q:a", "2", fast], check=True)
                audio = fast
                print("voice tempo", tempo)
            except Exception as e:  # noqa: BLE001
                print("voice tempo skipped:", e)
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
    clips = veo_clips(scenes, tmp) or wan_clips(scenes, tmp)  # оживлённая картина в начале ролика (Veo, запасной Wan)
    ai = fal_clips(scenes, tmp) if not clips else {}
    for k, v in ai.items():
        clips.setdefault(k, v)
    if len(clips) < 2:
        stock = pexels_clips(scenes, tmp, job.get("id", "x")) or pixabay_clips(scenes, tmp, job.get("id", "x"))
        for k, v in stock.items():
            clips.setdefault(k, v)

    out = os.path.join(out_dir, "reel.mp4")
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{VW}x{VH}", "-r", str(FPS), "-i", "-",
        "-i", audio,
        "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-profile:v", "high", "-pix_fmt", "yuv420p",
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
        elif s.get("map") and has_map(s["map"]) and (ne_data() is not None or world_map() is not None):
            try:
                mapper = MapScene(s["map"], n, job.get("map_style"))
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
                frame = base.resize((VW, VH), Image.BILINEAR, box=safe_box(base, cx, cy, cx + cw_, cy + ch_))
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
    try:
        files.append(reel_story(job, cover_base, brand, out_dir))
    except Exception as e:  # noqa: BLE001
        print("story announce skipped:", e)
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

# ---------------------------------------------------------------- истории (Stories)
# Безопасные зоны Stories 1080x1920: сверху ~250 px занимают полоски и имя профиля,
# снизу ~300 px поле «Отправить сообщение». Весь текст держим между ними.

def _center_text(d, y, text, font, fill, stroke=0):
    w = d.textlength(text, font=font)
    d.text(((VW - w) / 2, y), text, font=font, fill=fill,
           stroke_width=stroke, stroke_fill=(0, 0, 0) if stroke else None)


def _story_badge(d, brand, y=250):
    bf = font_sans(32, 700)
    bw = d.textlength(brand.upper(), font=bf) + 32 * 0.36 * 1.5 + 32 * 0.45
    badge(d, (VW - bw) / 2, y, brand, 32)


def _pill(d, y, text, size=36):
    kf = font_sans(size, 800)
    kw = d.textlength(text, font=kf)
    d.rounded_rectangle(((VW - kw) / 2 - 30, y, (VW + kw) / 2 + 30, y + size + 32), radius=(size + 32) // 2, fill=GOLD)
    d.text(((VW - kw) / 2, y + 14), text, font=kf, fill=INK)
    return y + size + 32


def story_layers(frame, brand, handle):
    """Слои кадра истории: картина, затемнение и текст отдельно (для видео с плавным приближением)."""
    bg = cover(load_image(frame["image_url"]), VW, VH).convert("RGBA")
    shade = Image.new("RGBA", (VW, VH), INK + (70,))
    shade.alpha_composite(gradient(VW, VH, 20, 245, start=0.28))
    txt = Image.new("RGBA", (VW, VH), (0, 0, 0, 0))
    d = ImageDraw.Draw(txt)
    _story_badge(d, brand)
    pad = 84
    kicker = (frame.get("kicker") or "").upper().strip()[:34]
    title = (frame.get("title") or "").strip()
    text = (frame.get("text") or "").strip()
    source = (frame.get("source") or "").strip()
    ft, tl, ts = fit_text(d, title, font_title, VW - 2 * pad, 400, 92, 54, 1.1)
    fb, bl, bs = fit_text(d, text, lambda s: font_sans(s, 500), VW - 2 * pad, 560, 46, 30, 1.32)
    block = (68 + 34 if kicker else 0) + int(len(tl) * ts * 1.1) + 36 + int(len(bl) * bs * 1.32) + (90 if source else 0)
    y = max(380, 1540 - block)
    if kicker:
        y = _pill(d, y, kicker) + 34
    for ln in tl:
        _center_text(d, y, ln, ft, WHITE, 3)
        y += int(ts * 1.1)
    y += 34
    d.rectangle(((VW - 110) / 2, y, (VW + 110) / 2, y + 6), fill=GOLD)
    y += 30
    for ln in bl:
        d.text((pad, y), ln, font=fb, fill=(242, 237, 226), stroke_width=1, stroke_fill=(0, 0, 0))
        y += int(bs * 1.32)
    if source:
        sf = font_sans(25, 600)
        y += 22
        for ln in wrap(d, "Источник: " + source, sf, VW - 2 * pad)[:2]:
            d.text((pad, y), ln, font=sf, fill=(200, 185, 150))
            y += 34
    if handle:
        hf = font_sans(30, 700)
        _center_text(d, 1590, "Больше историй в профиле " + handle, hf, GOLD)
    return bg, shade, txt


def story_card(frame, brand, handle):
    """Кадр истории: картина на весь экран, рубрика, заголовок и короткий факт."""
    bg, shade, txt = story_layers(frame, brand, handle)
    bg.alpha_composite(shade)
    bg.alpha_composite(txt)
    return bg.convert("RGB")


# Фоновая музыка для историй: Pixabay, Pixabay Content License (бесплатно, без указания авторства).
MUSIC = {
    "kazakh": ["https://cdn.pixabay.com/audio/2026/07/05/audio_469bd6ddf4.mp3",   # Atlas • Kazakhstan
               "https://cdn.pixabay.com/audio/2026/09/16/audio_40e31c078c.mp3"],  # Steppe Thunder
    "kyrgyz": ["https://cdn.pixabay.com/audio/2026/07/17/audio_3ce79af73e.mp3"],  # Atlas • Kyrgyzstan
    "uzbek": ["https://cdn.pixabay.com/audio/2026/07/14/audio_9f4d78dcb7.mp3"],   # Atlas • Uzbekistan
    "steppe": ["https://cdn.pixabay.com/audio/2026/02/18/audio_0a13adcc1f.mp3",   # Nomads
               "https://cdn.pixabay.com/audio/2023/04/14/audio_fd79f974b4.mp3",   # Steppe
               "https://cdn.pixabay.com/audio/2026/07/05/audio_469bd6ddf4.mp3"],
    "mongol": ["https://cdn.pixabay.com/audio/2026/07/08/audio_14e3737a27.mp3"],  # Atlas • Mongolia
}
STORY_SEC = 12


def story_music(key, seed, tmp):
    urls = MUSIC.get(key) or MUSIC["steppe"]
    url = urls[sum(map(ord, str(seed))) % len(urls)]
    path = os.path.join(tmp, "music.mp3")
    r = requests.get(url, timeout=120, headers=BROWSER_UA)
    r.raise_for_status()
    with open(path, "wb") as fh:
        fh.write(r.content)
    dur = probe_duration(path)
    off = max(0.0, min(dur * 0.15, dur - STORY_SEC - 2))
    print("story music", key, url.rsplit("/", 1)[-1], "offset", round(off, 1))
    return path, off


def story_video(frame, brand, handle, out_path, seed):
    """История-видео: медленное приближение картины, неподвижный текст и тематическая музыка."""
    bg, shade, txt = story_layers(frame, brand, handle)
    over = shade.copy()
    over.alpha_composite(txt)
    tmp = tempfile.mkdtemp()
    music, off = story_music(frame.get("music") or "steppe", seed, tmp)
    n = STORY_SEC * FPS
    Z = 1.08
    big = bg.convert("RGB").resize((int(VW * Z), int(VH * Z)), Image.LANCZOS)
    fade = STORY_SEC - 1.5
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{VW}x{VH}", "-r", str(FPS), "-i", "-",
           "-ss", f"{off:.2f}", "-t", str(STORY_SEC), "-i", music,
           "-filter_complex", f"[1:a]afade=t=in:st=0:d=1,afade=t=out:st={fade}:d=1.5,loudnorm=I=-16:TP=-1.5:LRA=11,aresample=44100[a]",
           "-map", "0:v", "-map", "[a]", "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p",
           "-c:a", "aac", "-b:a", "128k", "-shortest", "-movflags", "+faststart", out_path]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    for i in range(n):
        k = i / max(1, n - 1)
        s = 1.0 + (Z - 1.0) * (1 - (1 - k) ** 2) * 0.999
        cw, ch = big.width / s, big.height / s
        l, t = (big.width - cw) / 2, (big.height - ch) / 2
        fr = big.resize((VW, VH), Image.BILINEAR, box=(l, t, l + cw, t + ch)).convert("RGBA")
        fr.alpha_composite(over)
        proc.stdin.write(fr.convert("RGB").tobytes())
    proc.stdin.close()
    if proc.wait() != 0:
        raise RuntimeError("ffmpeg story failed")
    return out_path


def render_story(job, out_dir):
    paths = []
    frames = (job.get("frames") or [])[:5]
    brand, handle = job.get("brand", "Хроники времени"), job.get("handle", "")
    for i, fr in enumerate(frames):
        im = story_card(fr, brand, handle)
        p = os.path.join(out_dir, f"story_{i + 1:02d}.jpg")
        im.save(p, "JPEG", quality=92, optimize=True)
        if job.get("video"):
            try:
                paths.append(story_video(fr, brand, handle, os.path.join(out_dir, f"story_{i + 1:02d}.mp4"), str(job.get("id")) + str(i)))
            except Exception as e:  # noqa: BLE001
                print("story video skipped:", e)
                try:
                    os.remove(os.path.join(out_dir, f"story_{i + 1:02d}.mp4"))
                except OSError:
                    pass
        paths.append(p)
    if not paths:
        raise ValueError("story job without frames")
    return paths


def reel_story(job, base, brand, out_dir):
    """Анонс нового Reels для истории: кадр ролика, «Новая серия» и призыв смотреть в профиле."""
    img = cover(base, VW, VH).convert("RGBA")
    img.alpha_composite(Image.new("RGBA", (VW, VH), INK + (110,)))
    img.alpha_composite(gradient(VW, VH, 20, 220, start=0.45))
    d = ImageDraw.Draw(img)
    _story_badge(d, brand)
    label = (job.get("series_label") or "").strip()
    title = (job.get("cover_title") or job.get("title") or "").strip()
    pad = 90
    f, lines, size = fit_text(d, title, font_title, VW - 2 * pad, 560, 104, 58, 1.1)
    block = int(len(lines) * size * 1.1)
    y = VH // 2 - block // 2 - 40
    y0 = _pill(d, y - 130, "НОВАЯ СЕРИЯ")
    if label:
        lf = font_sans(34, 700)
        lab = label if d.textlength(label, font=lf) < VW - 2 * pad else label[:40]
        _center_text(d, y0 + 18, lab, lf, (235, 225, 200), 2)
        y = max(y, y0 + 90)
    for ln in lines:
        _center_text(d, y, ln, f, WHITE, 3)
        y += int(size * 1.1)
    cf = font_sans(42, 800)
    _center_text(d, max(y + 70, 1380), "Смотри ролик в профиле", cf, GOLD, 2)
    handle = job.get("handle") or ""
    if handle:
        _center_text(d, max(y + 130, 1440), handle, font_sans(34, 700), WHITE, 2)
    p = os.path.join(out_dir, "story.jpg")
    img.convert("RGB").save(p, "JPEG", quality=90, optimize=True)
    return p

# ---------------------------------------------------------------- кинематографичный монтаж (montage)
# Ролик без диктора: ИИ-кадры с медленным движением камеры, дымкой, зерном, музыкой,
# названием в начале и финальной карточкой «Подписывайтесь».

def _grade_lut():
    """Тон «кино»: чуть приглушённые цвета, холодные тени, тёплые света, мягкий контраст."""
    def curve(v, lift, gain):
        x = v / 255.0
        x = x * x * (3 - 2 * x) * 0.35 + x * 0.65          # мягкая S-кривая
        x = lift + x * (gain - lift)
        return max(0, min(255, int(round(x * 255))))
    r = [curve(v, 0.03, 1.00) for v in range(256)]
    g = [curve(v, 0.035, 0.97) for v in range(256)]
    b = [curve(v, 0.06, 0.93) for v in range(256)]
    return r + g + b


def _vignette(w, h, strength=0.55):
    small = Image.new("L", (w // 8, h // 8))
    cx, cy = small.width / 2, small.height / 2
    md = math.hypot(cx, cy)
    px = small.load()
    for y in range(small.height):
        for x in range(small.width):
            d = math.hypot(x - cx, (y - cy) * 0.8) / md
            px[x, y] = int(255 * max(0.0, min(1.0, (d - 0.35) / 0.65)) ** 1.6 * strength)
    v = small.resize((w, h), Image.BILINEAR)
    layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    layer.putalpha(v)
    return layer


def _mist(w, h, seed=3):
    """Полупрозрачная дымка (шире кадра, чтобы её можно было сдвигать)."""
    rng = random.Random(seed)
    base = Image.new("L", (w // 16, h // 16))
    px = base.load()
    for y in range(base.height):
        for x in range(base.width):
            px[x, y] = rng.randint(0, 255)
    m = base.resize((w * 2, h), Image.BICUBIC).filter(ImageFilter.GaussianBlur(110))
    m = ImageOps.autocontrast(m)
    # дымка гуще у низа кадра, как туман над землёй
    grad = Image.linear_gradient("L").resize((w * 2, h))
    m = ImageChops.multiply(m, grad.point(lambda v: int(80 + v * 0.69)))
    m = m.point(lambda v: int(max(0, v - 70) * 0.42))
    layer = Image.new("RGBA", (w * 2, h), (225, 228, 232, 0))
    layer.putalpha(m)
    return layer


def _grain_frames(w, h, n=6, seed=5):
    rng = random.Random(seed)
    out = []
    for _ in range(n):
        g = Image.effect_noise((w // 2, h // 2), 28).resize((w, h), Image.NEAREST)
        a = g.point(lambda v: int(abs(v - 128) * 0.22))
        layer = Image.new("RGBA", (w, h), (128, 128, 128, 0))
        layer.putalpha(a)
        out.append(layer)
        rng.random()
    return out


def _spaced(d, xy_center_y, text, font, fill, spacing):
    widths = [d.textlength(ch, font=font) for ch in text]
    total = sum(widths) + spacing * (len(text) - 1)
    x = (VW - total) / 2
    for ch, cw in zip(text, widths):
        d.text((x, xy_center_y), ch, font=font, fill=fill)
        x += cw + spacing


def _montage_title(title, sub):
    lay = Image.new("RGBA", (VW, VH), (0, 0, 0, 0))
    d = ImageDraw.Draw(lay)
    f = font_title(96)
    t = title.upper()
    while d.textlength(t, font=f) + 14 * len(t) > VW - 120 and f.size > 50:
        f = font_title(f.size - 6)
    _spaced(d, VH * 0.40, t, f, (245, 236, 214), 14)
    if sub:
        sf = font_sans(34, 600)
        _center_text(d, VH * 0.40 + f.size + 40, sub, sf, GOLD)
    return lay


def _montage_label(text):
    lay = Image.new("RGBA", (VW, VH), (0, 0, 0, 0))
    if not text:
        return lay
    d = ImageDraw.Draw(lay)
    f = font_sans(38, 600)
    w = d.textlength(text, font=f)
    y = VH - 430
    d.rectangle(((VW - w) / 2 - 2, y + 58, (VW - w) / 2 + 60, y + 62), fill=GOLD)
    d.text(((VW - w) / 2, y), text, font=f, fill=(240, 234, 222), stroke_width=2, stroke_fill=(0, 0, 0))
    return lay


def _montage_outro(job):
    lay = Image.new("RGBA", (VW, VH), INK + (150,))
    d = ImageDraw.Draw(lay)
    brand = job.get("brand", "Хроники времени")
    _story_badge(d, brand, y=VH * 0.33)
    lines = job.get("outro") or ["Подписывайтесь", "на «Хроники времени»"]
    size = 84
    f = font_title(size)
    while max(d.textlength(ln, font=f) for ln in lines) > VW - 140 and size > 48:
        size -= 4
        f = font_title(size)
    y = VH * 0.40
    for ln in lines:
        _center_text(d, y, ln, f, WHITE, 3)
        y += int(size * 1.25)
    if job.get("handle"):
        _center_text(d, y + 30, job["handle"], font_sans(40, 700), GOLD, 2)
    if job.get("outro_sub"):
        _center_text(d, y + 100, job["outro_sub"], font_sans(32, 500), (225, 218, 205), 2)
    return lay


def render_montage(job, out_dir):
    scenes = job["scenes"][:14]
    tmp = tempfile.mkdtemp()
    per = float(job.get("scene_sec") or 2.8)
    outro_sec = float(job.get("outro_sec") or 3.6)
    xf = 0.45                                   # длина перекрёстного перехода
    n_sc = len(scenes)
    total = n_sc * per + outro_sec
    n = int(total * FPS)
    lut = _grade_lut()
    vig = _vignette(VW, VH)
    mist = _mist(VW, VH, seed=len(scenes))
    grains = _grain_frames(VW, VH)
    Z = 1.14
    bigs, labels = [], []
    for i, s in enumerate(scenes):
        img = load_image(s["image_url"])
        big = cover(img, int(VW * Z), int(VH * Z)).convert("RGB").point(lut)
        bigs.append(big)
        labels.append(_montage_label(s.get("label") or ""))
    title_lay = _montage_title(job.get("title") or "", job.get("subtitle") or "")
    outro_lay = _montage_outro(job)
    rng = random.Random(str(job.get("id")))
    moves = []
    for i in range(n_sc):
        zin = i % 2 == 0
        dx, dy = rng.uniform(-0.5, 0.5), rng.uniform(-0.4, 0.4)
        moves.append((zin, dx, dy))

    def scene_frame(i, t):
        """t от 0 до 1 внутри сцены (с запасом на переход)."""
        big = bigs[i]
        zin, dx, dy = moves[i]
        k = max(0.0, min(1.0, t))
        e = k * k * (3 - 2 * k)
        s = 1.0 + (Z - 1.0) * (e if zin else 1 - e)
        cw, ch = big.width / s, big.height / s
        mx, my = (big.width - cw) / 2, (big.height - ch) / 2
        l = mx + mx * dx * (e - 0.5) * 1.6
        tp = my + my * dy * (e - 0.5) * 1.6
        l = max(0, min(big.width - cw, l))
        tp = max(0, min(big.height - ch, tp))
        return big.resize((VW, VH), Image.BILINEAR, box=(l, tp, l + cw, tp + ch))

    music = job.get("music_url") or (MUSIC.get(job.get("music") or "kazakh") or MUSIC["steppe"])[0]
    if music.startswith("http"):
        mpath = os.path.join(tmp, "music.mp3")
        r = requests.get(music, timeout=120, headers=BROWSER_UA)
        r.raise_for_status()
        with open(mpath, "wb") as fh:
            fh.write(r.content)
    else:
        mpath = music
    mdur = probe_duration(mpath)
    off = float(job.get("music_offset") or 0)
    off = max(0.0, min(off, mdur - total - 0.5)) if mdur > total + 1 else 0.0
    out = os.path.join(out_dir, "reel.mp4")
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{VW}x{VH}", "-r", str(FPS), "-i", "-",
           "-ss", f"{off:.2f}", "-t", f"{total:.2f}", "-i", mpath,
           "-filter_complex", f"[1:a]afade=t=in:st=0:d=0.6,afade=t=out:st={total - 2.2:.2f}:d=2.2,loudnorm=I=-14:TP=-1.2:LRA=11,aresample=44100[a]",
           "-map", "0:v", "-map", "[a]", "-c:v", "libx264", "-preset", "medium", "-crf", "19", "-profile:v", "high", "-pix_fmt", "yuv420p",
           "-c:a", "aac", "-b:a", "160k", "-shortest", "-movflags", "+faststart", out]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    cover_frame = None
    for fi in range(n):
        tt = fi / FPS
        if tt < n_sc * per:
            i = min(n_sc - 1, int(tt // per))
            local = (tt - i * per) / per
            fr = scene_frame(i, local * 0.92 + 0.04)
            if i + 1 < n_sc and tt > (i + 1) * per - xf:
                a = (tt - ((i + 1) * per - xf)) / xf
                fr = Image.blend(fr, scene_frame(i + 1, 0.04 * a), a * a * (3 - 2 * a))
            fr = fr.convert("RGBA")
            # дымка плывёт медленно
            sh = int((tt * 26) % VW)
            fr.alpha_composite(mist.crop((sh, 0, sh + VW, VH)))
            la = labels[i]
            if local > 0.12 and local < 0.9 and (job.get("labels", True)):
                aa = min(1.0, (local - 0.12) / 0.15, (0.9 - local) / 0.15)
                if aa > 0:
                    tmpl = la.copy()
                    tmpl.putalpha(la.getchannel("A").point(lambda v: int(v * aa)))
                    fr.alpha_composite(tmpl)
            if tt < 2.6 and job.get("title"):
                ta = min(1.0, tt / 0.5, (2.6 - tt) / 0.5)
                tl = title_lay.copy()
                tl.putalpha(title_lay.getchannel("A").point(lambda v: int(v * ta)))
                fr.alpha_composite(Image.new("RGBA", (VW, VH), INK + (int(70 * ta),)))
                fr.alpha_composite(tl)
            if fi == int(FPS * 1.4):
                cover_frame = fr.copy()
        else:
            to = (tt - n_sc * per) / outro_sec
            fr = scene_frame(n_sc - 1, 0.96 + 0.04 * to).convert("RGBA")
            sh = int((tt * 26) % VW)
            fr.alpha_composite(mist.crop((sh, 0, sh + VW, VH)))
            oa = min(1.0, to / 0.18)
            ol = outro_lay.copy()
            ol.putalpha(outro_lay.getchannel("A").point(lambda v: int(v * oa)))
            fr.alpha_composite(ol)
        fr.alpha_composite(vig)
        fr.alpha_composite(grains[fi % len(grains)])
        proc.stdin.write(fr.convert("RGB").tobytes())
    proc.stdin.close()
    if proc.wait() != 0:
        raise RuntimeError("ffmpeg montage failed")
    files = [out]
    try:
        cimg = (cover_frame or scene_frame(0, 0.2).convert("RGBA")).convert("RGB")
        p = os.path.join(out_dir, "cover.jpg")
        cimg.save(p, "JPEG", quality=90, optimize=True)
        files.append(p)
    except Exception as e:  # noqa: BLE001
        print("montage cover skipped:", e)
    return files


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
    with open(os.path.join(base_out, "used_media.json"), "w") as fh:
        json.dump({"ids": used_media()[-3000:]}, fh)
    esc = lambda t: (str(t or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
    cards = []
    for it in sorted(items, key=lambda x: -float(x.get("ts", 0))):
        when = time.strftime("%d.%m %H:%M", time.gmtime(float(it.get("ts", 0)) + 5 * 3600))
        kind = {"reel": "Reels", "montage": "Reels", "voices": "Пробник голосов", "story": "История"}.get(it.get("type"), "Карусель")
        if it.get("type") == "voices":
            media = "".join('<div style="margin:8px 0"><b>%s</b><br><audio controls preload="none" src="%s/%s"></audio></div>' % (esc(f[3:-4].replace("_", " ")), it["id"], f) for f in it.get("files", []))
            cards.append('<section><h2>%s · %s</h2><p>%s</p>%s</section>' % (kind, when, esc(it.get("title")), media))
            continue
        if it.get("type") in ("reel", "montage"):
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
<h1>Хроники времени — что подготовлено за последние дни</h1>""" + "".join(cards) + LEGAL_FOOTER + "</body></html>"
    with open(os.path.join(base_out, "index.html"), "w") as fh:
        fh.write(html)
    write_legal(base_out)


LEGAL_FOOTER = ('<footer style="margin-top:32px;color:#888;font-size:13px">«Хроники времени» — исторический канал '
                '(Instagram @khronika_vremenii, YouTube «Хроники времени»). '
                '<a style="color:#e9c46a" href="privacy.html">Политика конфиденциальности / Privacy Policy</a> · '
                '<a style="color:#e9c46a" href="terms.html">Условия использования / Terms of Service</a></footer>')

LEGAL_PAGES = {
    "privacy.html": ("Политика конфиденциальности / Privacy Policy", """
<p>Приложение «Khroniki Vremeni n8n» — внутренний инструмент автора канала «Хроники времени». Оно публикует
исторические видео автора в его собственный YouTube-канал и Instagram-аккаунт через официальные API
(YouTube Data API, Instagram Graph API).</p>
<p>Какие данные используются: только OAuth-токен доступа к каналу автора. Токен хранится в зашифрованном виде в
сервисе автоматизации n8n и используется только для загрузки видео, названий и описаний в этот канал.</p>
<p>Мы не собираем, не продаём и не передаём третьим лицам персональные данные зрителей или других пользователей
Google. Приложение не читает личные данные из аккаунта Google, кроме того, что нужно для загрузки видео.</p>
<p>Использование данных, полученных через Google API, соответствует
<a href="https://developers.google.com/terms/api-services-user-data-policy">Google API Services User Data Policy</a>,
включая требования Limited Use. Отозвать доступ можно в любой момент на странице
<a href="https://myaccount.google.com/permissions">myaccount.google.com/permissions</a>.</p>
<hr>
<p><b>English.</b> “Khroniki Vremeni n8n” is an internal tool of the “Хроники времени” history channel. It uploads the
owner's own videos to the owner's own YouTube channel and Instagram account via the official APIs. The only data used
is the OAuth access token for the owner's channel, stored encrypted in the n8n automation service and used solely to
upload videos, titles and descriptions. We do not collect, sell or share personal data of viewers or any other Google
users. Use of information received from Google APIs adheres to the Google API Services User Data Policy, including the
Limited Use requirements. Access can be revoked at any time at myaccount.google.com/permissions.</p>
"""),
    "terms.html": ("Условия использования / Terms of Service", """
<p>«Khroniki Vremeni n8n» — частный инструмент автора канала «Хроники времени» для публикации собственных
исторических видео в собственные аккаунты автора на YouTube и Instagram. Сторонним пользователям приложение
не предоставляется. Контент публикуется в соответствии с правилами YouTube и Instagram; иллюстрации берутся
из общественного достояния (Wikimedia Commons) или создаются автором.</p>
<hr>
<p><b>English.</b> “Khroniki Vremeni n8n” is a private tool used only by the owner of the “Хроники времени” channel to
publish the owner's own history videos to the owner's own YouTube and Instagram accounts. It is not offered to third
parties. Content is published in accordance with YouTube and Instagram policies.</p>
"""),
}


def write_legal(base_out):
    for name, (title, body) in LEGAL_PAGES.items():
        page = ("""<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>%s — Хроники времени</title><style>body{margin:0 auto;max-width:760px;background:#111;color:#eee;font:16px/1.55 system-ui,sans-serif;padding:16px}
h1{color:#e9c46a;font-size:22px}a{color:#e9c46a}hr{border:0;border-top:1px solid #333;margin:24px 0}</style></head><body>
<h1>%s</h1>%s<p><a href="index.html">← Хроники времени</a></p></body></html>""") % (title, title, body)
        with open(os.path.join(base_out, name), "w") as fh:
            fh.write(page)


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
        if job["type"] in ("reel", "montage"):
            payload["video_url"] = urls[0]
            covers = [u for u in urls[1:] if u.endswith("cover.jpg")] or [u for u in urls[1:] if u.endswith(".jpg") and not u.endswith("story.jpg")]
            if covers:
                payload["cover_url"] = covers[0]
            stories = [u for u in urls if u.endswith("story.jpg")]
            if stories:
                payload["story_url"] = stories[0]
        elif job["type"] == "story":
            payload["video_urls"] = [u for u in urls if u.endswith(".mp4")]
            payload["image_urls"] = [u for u in urls if u.endswith(".jpg")]
        else:
            payload["image_urls"] = urls
        callback(job, payload)
        return

    # --render
    os.makedirs(out_dir, exist_ok=True)
    if job["type"] == "site":
        # Только пересобрать витрину и служебные страницы, без рендера.
        with open(os.path.join(out_dir, "result.json"), "w") as fh:
            json.dump({"files": []}, fh)
        write_showcase(base_out, carry_over(base_out, jid))
        print("SITE refreshed")
        return
    if job["type"] == "carousel":
        files = render_carousel(job, out_dir)
    elif job["type"] == "reel":
        files = render_reel(job, out_dir)
    elif job["type"] == "voices":
        files = render_voices(job, out_dir)
    elif job["type"] == "story":
        files = render_story(job, out_dir)
    elif job["type"] == "montage":
        files = render_montage(job, out_dir)
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
