from flask import Flask, render_template, request, send_file
from docx import Document
import requests
import os
import re
import json
from dotenv import load_dotenv
from flask_cors import CORS


# -------------------------------------------------------
# 1) Load environment variables (.env)
# -------------------------------------------------------
load_dotenv()
# -------------------------------------------------------
# 2) Groq client init (Render-safe)
# -------------------------------------------------------
from groq import Groq
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
groq_client = None
if GROQ_API_KEY and GROQ_API_KEY.strip():
    try:
        groq_client = Groq(api_key=GROQ_API_KEY)
        print("Groq Loaded Successfully")
    except Exception as e:
        print("Groq Init Error:", e)
else:
    print("GROQ_API_KEY missing or empty")
# -------------------------------------------------------
# 3) Flask app + LanguageTool URL
# -------------------------------------------------------
app = Flask(__name__)
CORS(app)
LT_API_URL = "https://api.languagetool.org/v2/check"
# -------------------------------------------------------
# 4) Load Black’s Law Dictionary JSON (local)
# -------------------------------------------------------
try:
    with open("blacklaw_terms.json", "r", encoding="utf-8") as f:
        BLACKLAW = json.load(f)
except Exception:
    BLACKLAW = {}
def normalize_key(word: str) -> str:
    """Normalize keys for Black's Law lookup."""
    return re.sub(r"[^a-z\s]", "", word.lower().strip())
# -------------------------------------------------------
# 5) Legal fix rules (wrong → correct)
# -------------------------------------------------------
LEGAL_FIX = {
    "suo moto": "suo motu",
    "prima facia": "prima facie",
    "mens reaa": "mens rea",
    "ratio decedendi": "ratio decidendi",
}
# Common helper words we never want Groq to touch
IGNORE_WORDS = {
    "was", "the", "is", "and", "to", "in", "for",
    "of", "at", "by", "on", "with"
}
def is_reference_like(line: str) -> bool:
    """Lines that look like references / links -> skip grammar checks."""
    s = line.strip()
    return (
        not s
        or "http" in s
        or "www." in s
        or "doi" in s.lower()
        or re.match(r"^\[\d+\]$", s)
        or re.match(r"^\(.+\d{4}.*\)$", s)
    )
# -------------------------------------------------------
# 6) LanguageTool call (basic grammar)
# -------------------------------------------------------
def lt_check_sentence(sentence: str) -> dict:
    try:
        data = {"text": sentence, "language": "en-US"}
        r = requests.post(LT_API_URL, data=data, timeout=8)
        return r.json()
    except Exception:
        return {"matches": []}
# -------------------------------------------------------
# 7) Groq check → 100% SAFE FROM JSON ERROR (NEW)
# -------------------------------------------------------
def groq_check(sentence: str, lt_wrong_words: list) -> list:
    if (not groq_client) or (not lt_wrong_words):
        return []
    lt_wrong_words = [w for w in lt_wrong_words if w.lower() not in IGNORE_WORDS]
    if not lt_wrong_words:
        return []
    prompt = f"""Only correct these words: {lt_wrong_words}
Black's Law fixes (apply if present):
- suo moto → suo motu
- prima facia → prima facie
- mens reaa → mens rea
- ratio decedendi → ratio decidendi
Output ONLY this exact format. No extra text, no explanation:
[{{"wrong": "old word", "suggestion": "new word"}}]
Sentence: \"{sentence}\" """
    try:
        response = groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "user", "content": prompt}],
            temperature=0,
            max_tokens=300
        )
        raw = response.choices[0].message.content.strip()
        # Sabse powerful JSON extractor (kabhi fail nahi hoga)
        import re
        match = re.search(r"\[(?:.|\n)*\]", raw)
        if not match:
            return []
        json_str = match.group(0)
        # Trailing comma fix
        json_str = re.sub(r",\s*]", "]", json_str)
        import json
        return json.loads(json_str)
    except Exception as e:
        print("GROQ FINAL ERROR:", e)
        return []
# -------------------------------------------------------
# 8) Detect legal phrases using manual list
# -------------------------------------------------------
def detect_legal(sentence: str):
    results = []
    for wrong, correct in LEGAL_FIX.items():
        if re.search(rf"\b{re.escape(wrong)}\b", sentence, re.IGNORECASE):
            meaning = BLACKLAW.get(normalize_key(correct), "")
            results.append((wrong, correct, meaning))
    return results
# -------------------------------------------------------
# 9) Build highlighted HTML line by line → FIXED VERSION
# -------------------------------------------------------
def process_text_line_by_line(text: str) -> str:
    lines = text.split("\n")
    final_html = []
    for line in lines:
        if not line.strip():
            final_html.append("<p></p>")
            continue
        if is_reference_like(line):
            final_html.append(f"<p>{line}</p>")
            continue
        working = line
        html_line = line
        # LanguageTool
        lt_res = lt_check_sentence(working)
        lt_wrong_words = []
        for m in lt_res.get("matches", []):
            wrong = working[m["offset"]:m["offset"] + m["length"]]
            if wrong.strip() and wrong.lower() not in IGNORE_WORDS:
                lt_wrong_words.append(wrong)
        legal_hits = detect_legal(working)
        groq_hits = groq_check(working, lt_wrong_words)
        # Combined dictionary: lower_case → data
        combined = {}
        # Add Black's Law hits (lower case key)
        for wrong, correct, meaning in legal_hits:
            key = wrong.lower()
            combined[key] = {
                "original": wrong, # original casing
                "black": correct,
                "groq": None,
                "meaning": meaning
            }
        # Add Groq hits (match by lower case)
        for g in groq_hits:
            wrong_raw = (g.get("wrong") or "").strip()
            suggestion = (g.get("suggestion") or "").strip()
            if not wrong_raw or not suggestion:
                continue
            key = wrong_raw.lower()
            if key not in combined:
                # Find original word in line
                match = re.search(rf"\b{re.escape(wrong_raw)}\b", working, re.IGNORECASE)
                original = match.group(0) if match else wrong_raw
                combined[key] = {
                    "original": original,
                    "black": None,
                    "groq": None,
                    "meaning": ""
                }
            combined[key]["groq"] = suggestion
        # Now replace in html_line with correct original casing
        for key, data in combined.items():
            original_word = data["original"]
            black = data["black"] or ""
            groq = data["groq"] or ""
            meaning = data["meaning"] or ""
            span = (
                f"<span class='grammar-wrong' "
                f"data-wrong='{original_word}' "
                f"data-black='{black}' "
                f"data-groq='{groq}' "
                f"data-meaning='{meaning}'>"
                f"{original_word}</span>"
            )
            # YEHI LINE CHANGE KI HAI (sabse safe method)
            html_line = html_line.replace(original_word, span, 1)

        final_html.append(f"<p>{html_line}</p>")
    return "\n".join(final_html)
# -------------------------------------------------------
# 10) Routes
# -------------------------------------------------------
# Normal frontend route
@app.route("/", methods=["GET", "POST"])
def index():
    if request.method == "POST":
        text_input = request.form.get("text", "").strip()
        file = request.files.get("file")
        if file and file.filename.endswith(".docx"):
            doc = Document(file)
            text = "\n".join([p.text for p in doc.paragraphs])
        else:
            text = text_input
        output = process_text_line_by_line(text)
        return render_template("result.html", highlighted_html=output)
    return render_template("index.html")

# Google Docs sidebar route
@app.route("/check", methods=["POST"])
def check():
    text = request.form.get("text", "")
    output = process_text_line_by_line(text)
    return render_template("result.html", highlighted_html=output)

@app.route("/download_corrected", methods=["POST"])
def download_corrected():
    final_text = request.form.get("final_text", "")
    replacements = json.loads(request.form.get("replacements", "[]"))
    doc = Document()
    for line in final_text.split("\n"):
        doc.add_paragraph(line)
    for para in doc.paragraphs:
        for rep in replacements:
            wrong = rep["old"]
            correct = rep["new"]
            para.text = re.sub(
                rf"\b{re.escape(wrong)}\b",
                correct,
                para.text,
                flags=re.IGNORECASE
            )
    output_path = "static/Corrected_Final_Output.docx"
    doc.save(output_path)
    return send_file(output_path, as_attachment=True)

# -------------------------------------------------------
# 11) Run app → Render ke liye yeh zaroori hai
# -------------------------------------------------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)

