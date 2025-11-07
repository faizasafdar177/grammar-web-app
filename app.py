from flask import Flask, render_template, request, send_file
from docx import Document
from docx.shared import RGBColor
import requests
import io
import os

app = Flask(__name__)

LT_API_URL = "https://api.languagetool.org/v2/check"

def check_grammar(text, lang="en-US"):
    """Send text to LanguageTool API."""
    data = {"text": text, "language": lang}
    resp = requests.post(LT_API_URL, data=data)
    resp.raise_for_status()
    return resp.json()

def highlight_incorrect_words(doc, matches):
    """
    Highlights only the incorrect word(s) in red
    and writes the corrected word next to it in green parentheses.
    """
    for match in matches:
        wrong_word = match["context"]["text"][match["context"]["offset"]:match["context"]["offset"] + match["context"]["length"]]
        replacements = match.get("replacements", [])
        suggestion = replacements[0]["value"] if replacements else None

        if not wrong_word.strip() or not suggestion:
            continue

        # Loop through each paragraph to find and highlight the wrong word
        for para in doc.paragraphs:
            if wrong_word in para.text:
                runs = []
                start = 0
                new_runs = []
                text = para.text
                while wrong_word in text[start:]:
                    idx = text.find(wrong_word, start)
                    if idx == -1:
                        break

                    # Text before wrong word
                    if idx > start:
                        new_runs.append((text[start:idx], None))

                    # Wrong word (red)
                    new_runs.append((wrong_word, "red"))

                    # Add corrected word in green parentheses
                    new_runs.append((f"({suggestion})", "green"))

                    start = idx + len(wrong_word)

                # Remaining text after last match
                if start < len(text):
                    new_runs.append((text[start:], None))

                # Clear old paragraph
                for run in para.runs:
                    run.text = ""
                para.text = ""

                # Rebuild paragraph with formatting
                for text_part, color in new_runs:
                    run = para.add_run(text_part)
                    if color == "red":
                        run.font.color.rgb = RGBColor(255, 0, 0)  # Red for wrong word
                        run.bold = True
                    elif color == "green":
                        run.font.color.rgb = RGBColor(0, 128, 0)  # Green for suggestion
                        run.italic = True

                break  # stop after one replacement per paragraph
    return doc


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/upload", methods=["POST"])
def upload():
    file = request.files.get("file")
    if not file or not file.filename.endswith(".docx"):
        return "Please upload a .docx file", 400

    original_filename = os.path.splitext(file.filename)[0]  # without extension

    doc = Document(file)
    paras = [p.text for p in doc.paragraphs]
    full_text = "\n".join(paras)

    lt_result = check_grammar(full_text, lang="en-US")
    matches = lt_result.get("matches", [])

    highlighted_doc = highlight_incorrect_words(doc, matches)

    # Save output with "_corrected" suffix
    output_filename = f"{original_filename}_corrected.docx"
    output = io.BytesIO()
    highlighted_doc.save(output)
    output.seek(0)

    return send_file(
        output,
        as_attachment=True,
        download_name=output_filename,
        mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
