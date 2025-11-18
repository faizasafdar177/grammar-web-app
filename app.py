from flask import Flask, render_template, request, send_file
from docx import Document
import requests
import os
import re
import json
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)

LT_API_URL = "https://api.languagetool.org/v2/check"
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"

try:
    if GROQ_API_KEY:
        print("✅ Groq Key Loaded (REST Mode)")
    else:
        print("⚠️ No GROQ_API_KEY found")
except:
    pass

# ---------------------------------------------------
# Load Legal Terms
# ---------------------------------------------------
LEGAL_FIX = {
    "suo moto": "suo motu",
    "prima facia": "prima facie",
    "mens reaa": "mens rea",
    "ratio decedendi": "ratio decidendi",
}

IGNORE_WORDS = {"was","the","is","and","to","in","for","of","at","by","on","with"}

def normalize_key(w):
    return re.sub(r"[^a-z\s]", "", w.lower().strip())

try:
    with open("blacklaw_terms.json","r",encoding="utf8") as f:
        BLACKLAW = json.load(f)
except:
    BLACKLAW = {}

# ---------------------------------------------------
# LanguageTool
# ---------------------------------------------------
def lt_check(sentence):
    try:
        r = requests.post(LT_API_URL, data={"text": sentence, "language":"en-US"})
        return r.json()
    except:
        return {"matches":[]}

# ---------------------------------------------------
# Groq REST API (NO SDK → NO ERROR)
# ---------------------------------------------------
def groq_rest(sentence, wrong_words):

    if not GROQ_API_KEY or not wrong_words:
        return []

    wrong_words = [w for w in wrong_words if w.lower() not in IGNORE_WORDS]
    if not wrong_words:
        return []

    prompt = f"""
You are a legal+grammar correction engine.

Correct only these words: {wrong_words}
Return only JSON list of objects with "wrong" and "suggestion".

Sentence:
{sentence}
"""

    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json"
    }

    data = {
        "model": "llama-3.3-70b-versatile",
        "messages": [{"role":"user","content":prompt}],
        "temperature": 0
    }

    try:
        r = requests.post(GROQ_URL, headers=headers, json=data)
        res = r.json()

        raw_text = res["choices"][0]["message"]["content"]
        match = re.search(r"\[.*?\]", raw_text, re.DOTALL)

        return json.loads(match.group(0)) if match else []

    except Exception as e:
        print("Groq REST Error:", e)
        return []

# ---------------------------------------------------
# Legal detection
# ---------------------------------------------------
def detect_legal(sentence):
    out = []
    for wrong, correct in LEGAL_FIX.items():
        if re.search(rf"\b{re.escape(wrong)}\b", sentence, re.I):
            meaning = BLACKLAW.get(normalize_key(correct), "")
            out.append((wrong, correct, meaning))
    return out

# ---------------------------------------------------
# Main processing
# ---------------------------------------------------
def process_text(text):

    lines = text.split("\n")
    html = []

    for line in lines:

        if not line.strip():
            html.append("<p></p>")
            continue

        lt = lt_check(line)
        wrong_words = []
        for m in lt.get("matches", []):
            w = line[m["offset"]:m["offset"]+m["length"]]
            if w and w.lower() not in IGNORE_WORDS:
                wrong_words.append(w)

        legal_hits = detect_legal(line)
        groq_hits = groq_rest(line, wrong_words)

        combined = {}

        for w,c,meaning in legal_hits:
            combined[w] = {"black": c, "groq": None}

        for g in groq_hits:
            w = g.get("wrong","")
            s = g.get("suggestion","")
            if w and s:
                combined.setdefault(w, {"black":None,"groq":None})
                combined[w]["groq"] = s

        new_line = line
        for w,fix in combined.items():
            span = (
                f"<span class='grammar-wrong' data-wrong='{w}' "
                f"data-black='{fix['black'] or ''}' "
                f"data-groq='{fix['groq'] or ''}'>{w}</span>"
            )
            new_line = re.sub(rf"\b{re.escape(w)}\b", span, new_line, flags=re.I)

        html.append(f"<p>{new_line}</p>")

    return "\n".join(html)

# ---------------------------------------------------
# Routes
# ---------------------------------------------------
@app.route("/", methods=["GET","POST"])
def index():
    if request.method=="POST":
        text = request.form.get("text","").strip()
        file = request.files.get("file")

        if file and file.filename.endswith(".docx"):
            doc = Document(file)
            text = "\n".join([p.text for p in doc.paragraphs])

        output = process_text(text)
        return render_template("result.html", highlighted_html=output)

    return render_template("index.html")

@app.route("/download_corrected", methods=["POST"])
def download_corrected():
    text = request.form.get("final_text","")
    reps = json.loads(request.form.get("replacements","[]"))

    doc = Document()
    for line in text.split("\n"):
        doc.add_paragraph(line)

    for p in doc.paragraphs:
        for rep in reps:
            p.text = re.sub(
                rf"\b{re.escape(rep['old'])}\b",
                rep["new"],
                p.text,
                flags=re.I
            )

    out = "static/Corrected_Final_Output.docx"
    doc.save(out)
    return send_file(out, as_attachment=True)

# ---------------------------------------------------
# Run
# ---------------------------------------------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT",5000))
    app.run(host="0.0.0.0", port=port)
