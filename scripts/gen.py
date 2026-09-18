# -*- coding: utf-8 -*-
"""
gen.py — генерация картинок-слайдов через Genosai Public API (chatgpt-image-2-5, 1K).

Читает project.json (см. project.example.json), один раз заливает референсы стиля,
ПАРАЛЛЕЛЬНО отправляет задачи (с интервалом ~1 с, чтобы не ловить 429), опрашивает их
одновременно, переотправляет упавшие/зависшие (таймаут 5 мин на попытку).

Запуск:
  python gen.py project.json                 # догенерить недостающие слайды
  python gen.py project.json 1               # только слайд 1 (пилот на утверждение)
  python gen.py project.json 3 7 --force     # пересоздать слайды 3 и 7
  python gen.py project.json --force         # пересоздать все
  python gen.py project.json --balance       # только показать баланс кредитов

Ключ API ищется по порядку: GENOSAI_API_KEY → ~/.secrets/genosai-slides.env →
~/.secrets/genosai.env → E:\\Claude\\genosai-cli\\api_key.txt.
Ключ sdk_live_* работает только с https://api.genosai.io (иначе 403).
"""
import os
import sys
import json
import time
import uuid
import mimetypes
import urllib.request
import urllib.error
import urllib.parse

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

BASE_URL_PROD = "https://api.genosai.io"
BASE_URL_DEV = "https://api.dev.genosai.io"
BASE_URL = BASE_URL_PROD

POLL_TIMEOUT = 300       # 5 минут на одну попытку
MAX_ATTEMPTS = 6
SUBMIT_INTERVAL = 1.0    # пауза между createTask (сек)
GLOBAL_MIN_GAP = 0.3     # минимальный интервал между любыми запросами
_last_submit = [0.0]
_last_api = [0.0]

REF_MAX_PX = 1024        # референсы сжимаем до 1024px по большей стороне
REF_JPEG_QUALITY = 85

KEY_FILES = [
    os.path.expanduser("~/.secrets/genosai-slides.env"),
    os.path.expanduser("~/.secrets/genosai.env"),
    r"E:\Claude\genosai-cli\api_key.txt",
]

# Общие правила качества — добавляются к каждому промпту
QUALITY_TAIL = (
    " Rules: correct human anatomy (exactly two hands, five fingers each, natural "
    "proportions), no watermark, no logos unless asked, no random letters or gibberish, "
    "no extra text beyond what is specified. If text is specified, render it EXACTLY, "
    "letter by letter, in clean legible typography with high contrast against the "
    "background; keep a safe margin from the edges."
)


# ---------- ключ ----------

def get_key():
    k = os.environ.get("GENOSAI_API_KEY", "").strip()
    if k:
        return k
    for kf in KEY_FILES:
        if not os.path.exists(kf):
            continue
        with open(kf, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" in line:
                    line = line.split("=", 1)[1].strip().strip('"').strip("'")
                if line.startswith("sdk_"):
                    return line
    sys.exit("Нет API-ключа Genosai. Положи строку GENOSAI_API_KEY=sdk_live_... в "
             + KEY_FILES[0])


def pick_base_url(key):
    global BASE_URL
    if os.environ.get("GENOSAI_BASE_URL"):
        BASE_URL = os.environ["GENOSAI_BASE_URL"]
    else:
        BASE_URL = BASE_URL_PROD if key.startswith("sdk_live_") else BASE_URL_DEV
    print("API: %s" % BASE_URL)


# ---------- REST ----------

def pace_api():
    gap = GLOBAL_MIN_GAP - (time.time() - _last_api[0])
    if gap > 0:
        time.sleep(gap)
    _last_api[0] = time.time()


def api_request(method, path, key, body=None, raw_body=None, content_type=None, timeout=120):
    pace_api()
    url = BASE_URL + path
    headers = {"Authorization": "Bearer " + key}
    data = None
    if raw_body is not None:
        data = raw_body
        if content_type:
            headers["Content-Type"] = content_type
    elif body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def safe_request(method, path, key, body=None, retries=6):
    """Ретраит 5xx/сеть/таймаут; 429 ждёт дольше и не расходует обычные ретраи.
    4xx (плохой ключ, нет кредитов, кривой запрос) — останавливаемся сразу."""
    attempt = 0
    r429 = 0
    while attempt < retries and r429 < 10:
        try:
            return api_request(method, path, key, body=body)
        except urllib.error.HTTPError as e:
            code = getattr(e, "code", None)
            if code == 429:
                r429 += 1
                print("   [429 rate limit] пауза 12с (%d/10)" % r429)
                time.sleep(12)
                continue
            try:
                detail = json.loads(e.read().decode("utf-8"))
                msg = detail.get("message") or detail.get("error") or ""
            except Exception:
                msg = ""
            if code in (400, 401, 402, 403):
                sys.exit("Ошибка API %s: %s" % (code, msg or str(e)))
            attempt += 1
            print("   [ретрай %d/%d] HTTP %s %s" % (attempt, retries, code, msg[:80]))
            time.sleep(5)
        except Exception as e:
            attempt += 1
            print("   [ретрай %d/%d] %s" % (attempt, retries, str(e)[:90]))
            time.sleep(5)
    return None


def _extract_url(data):
    for key in ("url", "media_url", "fileUrl", "image_url"):
        if isinstance(data, dict) and data.get(key):
            return data[key]
    d = data.get("data") if isinstance(data, dict) else None
    if isinstance(d, dict):
        for key in ("url", "media_url", "fileUrl", "image_url"):
            if d.get(key):
                return d[key]
    return None


def upload(path_to_file, key):
    """multipart-загрузка файла → URL (или None)."""
    for _ in range(4):
        try:
            with open(path_to_file, "rb") as f:
                file_data = f.read()
            filename = os.path.basename(path_to_file)
            mime = mimetypes.guess_type(filename)[0] or "application/octet-stream"
            boundary = "----GenosaiBoundary" + uuid.uuid4().hex
            parts = [
                ("--" + boundary).encode(),
                ('Content-Disposition: form-data; name="file"; filename="%s"' % filename).encode(),
                ("Content-Type: %s" % mime).encode(),
                b"", file_data,
                ("--" + boundary + "--").encode(), b"",
            ]
            data = api_request("POST", "/v1/uploads", key, raw_body=b"\r\n".join(parts),
                               content_type="multipart/form-data; boundary=" + boundary)
            url = _extract_url(data)
            if url:
                return url
        except Exception as e:
            print("   загрузка не удалась (%s), ретрай" % str(e)[:80])
        time.sleep(5)
    return None


def compress_ref(path, cache_dir):
    """Референс один раз пережимаем в лёгкий JPEG (тяжёлые модель грузит ненадёжно)."""
    try:
        from PIL import Image
    except Exception:
        return path
    try:
        os.makedirs(cache_dir, exist_ok=True)
        base = os.path.splitext(os.path.basename(path))[0]
        out = os.path.join(cache_dir, "ref_" + base + ".jpg")
        if os.path.exists(out) and os.path.getmtime(out) >= os.path.getmtime(path):
            return out
        im = Image.open(path).convert("RGB")
        im.thumbnail((REF_MAX_PX, REF_MAX_PX))
        im.save(out, "JPEG", quality=REF_JPEG_QUALITY)
        print("   референс сжат: %s (%d KB)" % (os.path.basename(path), os.path.getsize(out) // 1024))
        return out
    except Exception as e:
        print("   [сжатие референса не удалось: %s]" % str(e)[:80])
        return path


# ---------- промпт ----------

def compose_prompt(cfg, slide):
    """style-преамбула + сцена слайда + текст на слайде (ровно тот, что утверждён) + правила."""
    parts = [cfg.get("style", "").strip()]
    if cfg.get("references_note"):
        parts.append(cfg["references_note"].strip())
    parts.append(slide["image_prompt"].strip())
    title = (slide.get("title") or "").strip()
    text = (slide.get("text") or "").strip()
    if cfg.get("render_text", True) and (title or text):
        block = "TEXT ON THE SLIDE (render exactly this, in Russian, nothing else):"
        if title:
            block += ' Headline: "%s".' % title.replace('"', "'")
        if text:
            block += ' Body text: "%s".' % text.replace('"', "'")
        block += (" Headline large and bold, body text smaller; place the text where it "
                  "does not cover key elements of the scene.")
        parts.append(block)
    else:
        parts.append("No text on the image at all.")
    parts.append(QUALITY_TAIL.strip())
    return " ".join(p for p in parts if p)


# ---------- генерация ----------

def slide_path(out_dir, num):
    return os.path.join(out_dir, "slide_%02d.png" % num)


def download_result(out_dir, num, url):
    out = slide_path(out_dir, num)
    for _ in range(4):
        try:
            with urllib.request.urlopen(url, timeout=120) as r, open(out, "wb") as f:
                f.write(r.read())
            print("[слайд %d] сохранено: %s" % (num, out))
            return True
        except Exception as e:
            print("[слайд %d] не скачалось (%s), ретрай" % (num, str(e)[:80]))
            time.sleep(4)
    print("[слайд %d] !! не удалось скачать. URL: %s" % (num, url))
    return False


class Generator:
    def __init__(self, cfg, key):
        self.cfg = cfg
        self.key = key
        self.model = cfg.get("model", "chatgpt-image-2-5")
        self.ar = cfg.get("aspect_ratio", "16:9")
        self.resolution = cfg.get("resolution", "1K")
        self.out_dir = cfg["out_dir"]
        cfg_dir = cfg["_cfg_dir"]
        self.ref_files = []
        for r in cfg.get("references", []) or []:
            if r.startswith("http"):
                self.ref_files.append(r)
                continue
            p = r if os.path.isabs(r) else os.path.join(cfg_dir, r)
            if not os.path.exists(p):
                sys.exit("Референс не найден: " + p)
            self.ref_files.append(compress_ref(p, os.path.join(self.out_dir, "_ref_cache")))
        self.ref_urls = []
        self._last_ref_refresh = 0.0
        self.log_path = os.path.join(self.out_dir, "generation_log.jsonl")

    def log(self, rec):
        rec["ts"] = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(self.log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    def upload_refs(self):
        urls = []
        for p in self.ref_files:
            if p.startswith("http"):
                urls.append(p)
                continue
            u = upload(p, self.key)
            if not u:
                sys.exit("Не удалось залить референс: " + p)
            print("   референс залит: %s" % u)
            urls.append(u)
        self.ref_urls = urls
        self._last_ref_refresh = time.time()

    def maybe_refresh_refs(self):
        if time.time() - self._last_ref_refresh < 25:
            return
        self.upload_refs()
        print("   референсы перезалиты")

    def submit(self, slide):
        prompt = compose_prompt(self.cfg, slide)
        inp = {"prompt": prompt, "aspect_ratio": self.ar, "resolution": self.resolution}
        if self.ref_urls:
            inp["image_urls"] = list(self.ref_urls)
        gap = SUBMIT_INTERVAL - (time.time() - _last_submit[0])
        if gap > 0:
            time.sleep(gap)
        _last_submit[0] = time.time()
        resp = safe_request("POST", "/v1/createTask", self.key,
                            body={"model": self.model, "input": inp})
        if not resp:
            return None
        tid = (resp.get("data") or {}).get("taskId") or resp.get("taskId")
        self.log({"slide": slide["num"], "task": tid, "prompt": prompt, "refs": self.ref_urls})
        return tid

    def run(self, slides):
        state = {}
        for s in slides:
            tid = self.submit(s)
            state[s["num"]] = {"slide": s, "task": tid, "start": time.time(), "att": 1}
            print("[слайд %d] отправлено (task=%s)" % (s["num"], tid))
        while state:
            for num in list(state.keys()):
                st = state[num]
                if not st["task"]:
                    tid = self.submit(st["slide"])
                    if tid:
                        st["task"] = tid
                        st["start"] = time.time()
                        print("[слайд %d] переотправлено (попытка %d)" % (num, st["att"]))
                    continue
                info = safe_request("GET", "/v1/taskInfo?taskId=" + urllib.parse.quote(st["task"]), self.key)
                if not info:
                    continue
                d = info.get("data", info)
                status = d.get("status")
                if status == "succeeded":
                    res = d.get("result") or {}
                    url = res.get("media_url") or (res.get("media_urls") or [None])[0]
                    if url:
                        download_result(self.out_dir, num, url)
                        self.log({"slide": num, "task": st["task"], "status": "ok",
                                  "url": url, "cost": d.get("cost")})
                    else:
                        print("[слайд %d] succeeded, но URL не найден: %s" % (num, str(res)[:200]))
                    del state[num]
                    print("   >>> осталось слайдов: %d" % len(state))
                elif status in ("failed", "error", "canceled"):
                    msg = d.get("message", "") or ""
                    print("[слайд %d] ПРОВАЛ: %s (%s)" % (num, status, msg))
                    self.log({"slide": num, "task": st["task"], "status": status, "message": msg})
                    if "референс" in msg.lower() or "reference" in msg.lower():
                        self.maybe_refresh_refs()
                    self._retry_or_drop(state, num)
                else:
                    if time.time() - st["start"] > POLL_TIMEOUT:
                        print("[слайд %d] 5 мин вышло — повторная отправка" % num)
                        self._retry_or_drop(state, num)
            if state:
                time.sleep(4)

    def _retry_or_drop(self, state, num):
        st = state[num]
        if st["att"] < MAX_ATTEMPTS:
            st["att"] += 1
            st["task"] = None
        else:
            print("[слайд %d] !! отказ после %d попыток" % (num, MAX_ATTEMPTS))
            del state[num]


def show_balance(key):
    data = safe_request("GET", "/v1/balance", key)
    if data:
        print("Баланс: %s кредитов (основной %s, бонус %s)" % (
            data.get("total"), data.get("main"), data.get("bonus")))


def main():
    args = sys.argv[1:]
    if not args:
        sys.exit(__doc__)
    cfg_path = args[0]
    force = "--force" in args
    only = [int(a) for a in args[1:] if a.isdigit()]

    key = get_key()
    pick_base_url(key)
    if "--balance" in args:
        show_balance(key)
        return

    with open(cfg_path, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    cfg["_cfg_dir"] = os.path.dirname(os.path.abspath(cfg_path))
    if not os.path.isabs(cfg["out_dir"]):
        cfg["out_dir"] = os.path.join(cfg["_cfg_dir"], cfg["out_dir"])
    os.makedirs(cfg["out_dir"], exist_ok=True)

    gen = Generator(cfg, key)
    targets = []
    for s in cfg["slides"]:
        if "image_prompt" not in s:
            continue
        if only and s["num"] not in only:
            continue
        if (not force) and os.path.exists(slide_path(cfg["out_dir"], s["num"])):
            print("== Слайд %d уже есть, пропускаю ==" % s["num"])
            continue
        targets.append(s)
    if not targets:
        print("=== Нечего генерировать ===")
        return

    cost = {"1K": 6, "2K": 12, "4K": 24}.get(gen.resolution, 6)
    print("Модель %s, %s, %s; слайдов: %d, ориентировочно %d кредитов" % (
        gen.model, gen.ar, gen.resolution, len(targets), cost * len(targets)))
    show_balance(key)
    if gen.ref_files:
        print("Заливаю референсы (%d шт.)..." % len(gen.ref_files))
        gen.upload_refs()
    print("Генерация слайдов: %s" % ", ".join(str(s["num"]) for s in targets))
    gen.run(targets)
    print("=== Готово ===")


if __name__ == "__main__":
    main()
