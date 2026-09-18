# -*- coding: utf-8 -*-
"""
build_pptx.py — упаковывает готовые слайды (slide_NN.png) в лёгкий PowerPoint.

Каждая картинка сжимается в JPEG (по умолчанию: длинная сторона ≤ 1920 px, quality 82),
кладётся на весь слайд, а продающий текст копирайтера (copy), заголовок и текст слайда
уходят в заметки докладчика.
Если итоговый файл тяжелее лимита (--max-mb, по умолчанию 8 МБ), качество понижается
автоматически, пока не влезет (но не ниже quality 60).

Запуск:
  python build_pptx.py project.json                     # → <out_dir>/<project_name>.pptx
  python build_pptx.py project.json --quality 85 --max-px 1920 --max-mb 8
"""
import os
import sys
import json

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

try:
    from PIL import Image
except ImportError:
    sys.exit("Нужен Pillow: pip install Pillow")
try:
    from pptx import Presentation
    from pptx.util import Inches
except ImportError:
    sys.exit("Нужен python-pptx: pip install python-pptx")

SIZES = {   # дюймы (ширина, высота)
    "16:9": (13.333, 7.5),
    "4:3": (10.0, 7.5),
    "1:1": (9.0, 9.0),
    "9:16": (7.5, 13.333),
    "3:4": (7.5, 10.0),
    "4:5": (8.0, 10.0),
}


def arg(name, default):
    if name in sys.argv:
        i = sys.argv.index(name)
        if i + 1 < len(sys.argv):
            return sys.argv[i + 1]
    return default


def compress(src, dst, max_px, quality):
    img = Image.open(src)
    if img.mode != "RGB":
        bg = Image.new("RGB", img.size, (255, 255, 255))
        rgba = img.convert("RGBA")
        bg.paste(rgba, mask=rgba.split()[-1])
        img = bg
    if max(img.size) > max_px:
        img.thumbnail((max_px, max_px), Image.LANCZOS)
    img.save(dst, "JPEG", quality=quality, optimize=True, progressive=True)
    return os.path.getsize(dst)


def build(cfg, out_dir, jpg_dir, slides, quality, max_px):
    ar = cfg.get("aspect_ratio", "16:9")
    W, H = SIZES.get(ar, SIZES["16:9"])
    prs = Presentation()
    prs.slide_width = Inches(W)
    prs.slide_height = Inches(H)
    blank = prs.slide_layouts[6]
    total_jpg = 0
    added = 0
    for s in slides:
        src = os.path.join(out_dir, "slide_%02d.png" % s["num"])
        if not os.path.exists(src):
            print("ПРОПУСК (нет файла): " + src)
            continue
        dst = os.path.join(jpg_dir, "slide_%02d.jpg" % s["num"])
        total_jpg += compress(src, dst, max_px, quality)
        slide = prs.slides.add_slide(blank)
        slide.shapes.add_picture(dst, 0, 0, width=prs.slide_width, height=prs.slide_height)
        # заметки докладчика: продающий текст копирайтера (copy) первым, потом заголовок и текст
        notes = []
        if s.get("copy"):
            notes.append(s["copy"].strip())
            notes.append("")
        if s.get("title"):
            notes.append(s["title"])
        if s.get("text"):
            notes.append(s["text"])
        if s.get("notes"):
            notes.append("")
            notes.append(s["notes"])
        if notes:
            slide.notes_slide.notes_text_frame.text = "\n".join(notes)
        added += 1
    safe = (cfg.get("project_name") or "presentation").replace("/", "_").replace("\\", "_")
    for ch in ':*?"<>|':
        safe = safe.replace(ch, "_")
    out = os.path.join(out_dir, safe + ".pptx")
    prs.save(out)
    return out, added, total_jpg


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    quality = int(arg("--quality", 82))
    max_px = int(arg("--max-px", 1920))
    max_mb = float(arg("--max-mb", 8))

    cfg_path = sys.argv[1]
    with open(cfg_path, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    cfg_dir = os.path.dirname(os.path.abspath(cfg_path))
    out_dir = cfg["out_dir"]
    if not os.path.isabs(out_dir):
        out_dir = os.path.join(cfg_dir, out_dir)
    jpg_dir = os.path.join(out_dir, "_jpg")
    os.makedirs(jpg_dir, exist_ok=True)
    slides = sorted(cfg.get("slides", []), key=lambda s: s["num"])

    while True:
        out, added, total_jpg = build(cfg, out_dir, jpg_dir, slides, quality, max_px)
        mb = os.path.getsize(out) / 1024 / 1024
        print("PPTX: %s — слайдов %d, %.1f МБ (JPEG quality=%d, ≤%dpx)" % (out, added, mb, quality, max_px))
        if mb <= max_mb or quality <= 60:
            break
        quality -= 6
        if max_px > 1600:
            max_px = 1600
        print("   тяжелее %.0f МБ — пережимаю: quality=%d, ≤%dpx" % (max_mb, quality, max_px))
    print("OK")


if __name__ == "__main__":
    main()
