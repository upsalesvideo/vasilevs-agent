# -*- coding: utf-8 -*-
"""
review_table.py — печатает Markdown-таблицу готовых слайдов для показа пользователю.

Формат: | № | Картинка | Заголовок и текст | (колонки 2–3 равной ширины).
Пути к картинкам — с прямыми слэшами (E:/...), чтобы кликались и рендерились.

Запуск:  python review_table.py project.json [номера...]
"""
import os
import sys
import json

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    cfg_path = sys.argv[1]
    only = [int(a) for a in sys.argv[2:] if a.isdigit()]
    with open(cfg_path, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    cfg_dir = os.path.dirname(os.path.abspath(cfg_path))
    out_dir = cfg["out_dir"]
    if not os.path.isabs(out_dir):
        out_dir = os.path.join(cfg_dir, out_dir)

    print("| № | Картинка | Заголовок и текст |")
    print("|:-:|:--------:|:------------------|")
    for s in sorted(cfg.get("slides", []), key=lambda s: s["num"]):
        if only and s["num"] not in only:
            continue
        p = os.path.join(out_dir, "slide_%02d.png" % s["num"]).replace("\\", "/")
        if not os.path.exists(p):
            img = "_(нет файла)_"
        else:
            img = "![слайд %d](%s)" % (s["num"], p)
        txt = "**%s**<br>%s" % (s.get("title", ""), s.get("text", ""))
        print("| %d | %s | %s |" % (s["num"], img, txt))


if __name__ == "__main__":
    main()
