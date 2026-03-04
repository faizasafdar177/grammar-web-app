import os, re, json, logging
from typing import List, Dict, Any, Tuple

import requests
from flask import Flask, render_template, request, abort, Response, jsonify
from flask_cors import CORS
from dotenv import load_dotenv
from markupsafe import escape

# Rate limiting
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

# Caching
from cachetools import TTLCache


# -----------------------------
# 1) ENV
# -----------------------------
load_dotenv()

GROQ_API_KEY = os.getenv("GROQ_API_KEY", "").strip()
APP_API_KEY  = os.getenv("APP_API_KEY", "").strip()
LT_API_URL   = os.getenv("LT_API_URL", "https://api.languagetool.org/v2/check").strip()

MAX_TEXT_CHARS = int(os.getenv("MAX_TEXT_CHARS", "12000"))

# -----------------------------
# 2) LOGGING
# -----------------------------
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s"
)
log = logging.getLogger("grammar-app")

# -----------------------------
# 3) GROQ INIT
# -----------------------------
groq_client = None
if GROQ_API_KEY:
    try:
        from groq import Groq
        groq_client = Groq(api_key=GROQ_API_KEY)
        log.info("Groq Loaded Successfully")
    except Exception as e:
        log.exception("Groq Init Error: %s", e)
else:
    log.warning("GROQ_API_KEY missing/empty: Groq features disabled")

# -----------------------------
# 4) FLASK APP
# -----------------------------
app = Flask(__name__)
CORS(app)

limiter = Limiter(
    get_remote_address,
    app=app,
    default_limits=["60 per minute"]
)

# -----------------------------
# 5) CACHES
# -----------------------------
lt_cache   = TTLCache(maxsize=2000, ttl=60 * 30)   # 30 min
groq_cache = TTLCache(maxsize=2000, ttl=60 * 60)   # 60 min

http = requests.Session()

# -----------------------------
# 6) BLACKLAW LOAD
# -----------------------------
try:
    with open("blacklaw_terms.json", "r", encoding="utf-8") as f:
        BLACKLAW = json.load(f)
except Exception:
    BLACKLAW = {}
    log.warning("blacklaw_terms.json not found/invalid. Meanings disabled.")

def normalize_key(word: str) -> str:
    return re.sub(r"[^a-z\s]", "", word.lower().strip())

# -----------------------------
# 7) LEGAL FIXES
# -----------------------------
LEGAL_FIX = {
    "suo moto":           "suo motu",
    "prima facia":        "prima facie",
    "mens reaa":          "mens rea",
    "ratio decedendi":    "ratio decidendi",
    "audi alteram partum":"audi alteram partem",
}

def is_reference_like(line: str) -> bool:
    s = line.strip()
    return (
        not s
        or "http" in s
        or "www." in s
        or "doi" in s.lower()
        or re.match(r"^\[\d+\]$", s)
        or re.match(r"^\(.+\d{4}.*\)$", s)
    )

# -----------------------------
# 8) SIMPLE AUTH (optional)
# -----------------------------
def require_api_key():
    if not APP_API_KEY:
        return
    provided = (request.headers.get("X-API-Key", "") or "").strip()
    if not provided:
        provided = (request.form.get("api_key", "") or "").strip()
    if not provided or provided != APP_API_KEY:
        abort(401, description="Unauthorized: missing/invalid API key")

# -----------------------------
# 9) LANGUAGETOOL
# -----------------------------
def lt_check_sentence(sentence: str) -> Dict[str, Any]:
    sentence = sentence.strip()
    if not sentence:
        return {"matches": []}
    if sentence in lt_cache:
        return lt_cache[sentence]
    try:
        data = {"text": sentence, "language": "en-US"}
        r = http.post(LT_API_URL, data=data, timeout=10)
        r.raise_for_status()
        out = r.json()
    except Exception as e:
        log.warning("LT error: %s", e)
        out = {"matches": []}
    lt_cache[sentence] = out
    return out

# -----------------------------
# 10) LEGAL DETECTOR
# -----------------------------
def detect_legal(sentence: str) -> List[Tuple[str, str, str]]:
    results = []
    for wrong, correct in LEGAL_FIX.items():
        if re.search(rf"\b{re.escape(wrong)}\b", sentence, re.IGNORECASE):
            meaning = BLACKLAW.get(normalize_key(correct), "")
            results.append((wrong, correct, meaning))
    return results

# -----------------------------
# 11) GROQ WORD-ONLY CHECK
# -----------------------------
def groq_word_check(sentence: str, lt_wrong_words: List[str]) -> List[Dict[str, str]]:
    if (not groq_client) or (not lt_wrong_words):
        return []

    lt_wrong_words = list(dict.fromkeys([w.strip() for w in lt_wrong_words if w.strip()]))
    if not lt_wrong_words:
        return []

    cache_key = "WORD||" + sentence + "||" + "|".join(sorted([w.lower() for w in lt_wrong_words]))
    if cache_key in groq_cache:
        return groq_cache[cache_key]

    prompt = f"""
Only correct these words (do NOT rewrite the whole sentence): {lt_wrong_words}

Apply these exact legal fixes if present:
- suo moto → suo motu
- prima facia → prima facie
- mens reaa → mens rea
- ratio decedendi → ratio decidendi
- audi alteram partum → audi alteram partem

Output ONLY valid JSON array exactly like:
[{{"wrong":"old","suggestion":"new"}}]

Sentence: "{sentence}"
""".strip()

    try:
        response = groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "user", "content": prompt}],
            temperature=0,
            max_tokens=350
        )
        raw = response.choices[0].message.content.strip()
        m = re.search(r"\[(?:.|\n)*\]", raw)
        if not m:
            groq_cache[cache_key] = []
            return []
        json_str = re.sub(r",\s*]", "]", m.group(0))
        arr = json.loads(json_str)
        if not isinstance(arr, list):
            arr = []
        cleaned = []
        for item in arr:
            w = str(item.get("wrong", "")).strip()
            s = str(item.get("suggestion", "")).strip()
            if w and s and w.lower() != s.lower():
                cleaned.append({"wrong": w, "suggestion": s})
        groq_cache[cache_key] = cleaned
        return cleaned
    except Exception as e:
        log.warning("Groq word-check error: %s", e)
        return []

# -----------------------------
# 12) BUILD HIGHLIGHTED HTML
# -----------------------------
def process_text_line_by_line(text: str) -> str:
    """Highlight grammar errors with suggestions."""
    lines = text.split("\n")
    final_html = []

    for line in lines:
        if not line.strip():
            final_html.append("<p></p>")
            continue

        safe_line = escape(line)

        if is_reference_like(line):
            final_html.append(f"<p>{safe_line}</p>")
            continue

        working = line

        # NOTE: Do NOT pre-escape here. We work on raw line and escape
        # only the plain-text segments AFTER spans are inserted.
        lt_res = lt_check_sentence(working)

        lt_wrong_words = []
        for m in lt_res.get("matches", []):
            wrong = working[m["offset"]:m["offset"] + m["length"]]
            if wrong.strip():
                lt_wrong_words.append(wrong)

        legal_hits = detect_legal(working)
        groq_hits  = groq_word_check(working, lt_wrong_words)

        combined = {}

        for wrong, correct, meaning in legal_hits:
            key = wrong.lower()
            combined[key] = {"original": wrong, "black": correct, "groq": None, "meaning": meaning}

        for g in groq_hits:
            wrong_raw  = (g.get("wrong") or "").strip()
            suggestion = (g.get("suggestion") or "").strip()
            if not wrong_raw or not suggestion:
                continue
            key = wrong_raw.lower()
            if key not in combined:
                mm = re.search(rf"\b{re.escape(wrong_raw)}\b", working, re.IGNORECASE)
                original = mm.group(0) if mm else wrong_raw
                combined[key] = {"original": original, "black": None, "groq": None, "meaning": ""}
            combined[key]["groq"] = suggestion

        def esc_attr(s):
            return (s.replace("&", "&amp;")
                     .replace('"', "&quot;")
                     .replace("<", "&lt;")
                     .replace(">", "&gt;"))

        raw_line = line
        for _, data in combined.items():
            original_word = data["original"]
            black   = data["black"]   or ""
            groq_s  = data["groq"]    or ""
            meaning = data["meaning"] or ""
            span = (
                f'<span class="grammar-wrong" '
                f'data-wrong="{esc_attr(original_word)}" '
                f'data-black="{esc_attr(black)}" '
                f'data-groq="{esc_attr(groq_s)}" '
                f'data-meaning="{esc_attr(meaning)}">'
                f'{esc_attr(original_word)}</span>'
            )
            raw_line = raw_line.replace(original_word, span, 1)

        parts = re.split(r'(<span[^>]*>.*?</span>)', raw_line, flags=re.DOTALL)
        html_line = "".join(
            part if part.startswith('<span') else str(escape(part))
            for part in parts
        )
        final_html.append(f"<p>{html_line}</p>")

    return "\n".join(final_html)

# -----------------------------
# 13) ROUTES
# -----------------------------
@app.route("/", methods=["GET", "POST"])
@limiter.limit("20 per minute")
def index():
    if request.method == "POST":
        require_api_key()
        text = (request.form.get("text", "") or "").strip()
        if len(text) > MAX_TEXT_CHARS:
            abort(413, description=f"Text too long. Max {MAX_TEXT_CHARS} chars allowed.")
        output = process_text_line_by_line(text)
        return render_template("result.html", highlighted_html=output)
    return render_template("index.html")


@app.route("/check", methods=["POST"])
@limiter.limit("30 per minute")
def check_text():
    require_api_key()
    if request.is_json:
        data = request.get_json()
        text = (data.get("text", "") or "")
    else:
        text = (request.form.get("text", "") or "")
    if len(text) > MAX_TEXT_CHARS:
        abort(413, description=f"Text too long. Max {MAX_TEXT_CHARS} chars allowed.")
    output = process_text_line_by_line(text)
    return Response(output, mimetype="text/html")


@app.route("/health", methods=["GET"])
def health():
    return {"status": "ok"}


# DEV ONLY
if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=False)
