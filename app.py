import requests
import time
import os
import json
import io
import uuid
import threading
import re
from fpdf import FPDF
from collections import Counter
from flask import Flask, request, render_template_string, send_from_directory, redirect, url_for, session, jsonify
from PIL import Image, ImageOps

# ── Renderer importieren ──────────────────────────────────────────────────────
from card_renderer import CardRenderer

# --- Basispfad (absolut, unabhängig vom Arbeitsverzeichnis) ---
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# --- Konfiguration ---
CARDS_DIR = os.path.join(BASE_DIR, 'card_images')
OUTPUT_DIR = os.path.join(BASE_DIR, 'output_pdfs')
CARD_BACKS_DIR = os.path.join(BASE_DIR, 'card_backs')
UPLOADS_DIR = os.path.join(BASE_DIR, 'user_uploads')
LANGUAGES = {
    'de': 'Deutsch', 'en': 'Englisch', 'es': 'Spanisch', 'fr': 'Französisch',
    'it': 'Italienisch', 'ja': 'Japanisch', 'ko': 'Koreanisch', 'pt': 'Portugiesisch',
    'ru': 'Russisch', 'zhs': 'Vereinfachtes Chinesisch',
}

# ── Renderer-Konfiguration ────────────────────────────────────────────────────
# Auf True setzen, um den Text-Renderer zu aktivieren.
# Nur Karten, bei denen image_status != 'highres_scan' ODER lang != 'en',
# werden tatsächlich verarbeitet. EN-Highres-Karten werden übersprungen.
ENABLE_RENDERING = True

# Sprachen, für die der Renderer aktiv sein soll.
# 'en' weglassen, da EN-Karten keinen Übersetzungs-Overlay benötigen.
RENDER_FOR_LANGS = {'de', 'es', 'fr', 'it', 'pt', 'ru', 'ja', 'ko', 'zhs'}

# Globale Renderer-Instanz (beim Start initialisiert)
renderer: CardRenderer | None = None

# --- Globale Variable für Hintergrund-Tasks ---
tasks = {}

# --- Initialisierung der Flask-App ---
app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'dev-secret-bitte-setzen')
app.config['MAX_CONTENT_LENGTH'] = 10 * 1024 * 1024

# --- Hilfsfunktionen ---
def fetch_all_pages(api_url):
    all_cards = []
    while api_url:
        try:
            response = requests.get(api_url)
            response.raise_for_status()
            data = response.json()
            all_cards.extend(data.get('data', []))
            api_url = data.get('next_page') if data.get('has_more') else None
            if api_url: time.sleep(0.1)
        except requests.exceptions.RequestException as e:
            print(f"Fehler beim Abrufen der Scryfall-Daten: {e}")
            return []
    return all_cards

def find_specific_card_printing(card_name, set_code, lang='de'):
    """Sucht eine Karte in einer spezifischen Edition mit einer robusteren Sprachlogik."""
    print(f"Suche gezielt nach '{card_name}' in Edition '{set_code.upper()}'...")
    search_query = f'!"{card_name}" set:{set_code}'
    
    params = {'q': search_query}
    api_url = "https://api.scryfall.com/cards/search?" + requests.compat.urlencode(params)
    base_prints = fetch_all_pages(api_url)

    if not base_prints:
        return None, f"Karte '{card_name}' in Edition '{set_code.upper()}' nicht gefunden."
    
    base_print = base_prints[0]
    final_print = None

    if lang != 'en':
        print(f"-> Versuche, die '{lang}'-Version für '{base_print['name']}' ({base_print['set'].upper()}) abzurufen...")
        try:
            lang_url = f"https://api.scryfall.com/cards/{base_print['set']}/{base_print['collector_number']}/{lang}"
            res = requests.get(lang_url, timeout=10)
            res.raise_for_status()
            final_print = res.json()
            print(f"-> '{lang}'-Version erfolgreich gefunden.")
        except requests.exceptions.RequestException as e:
            print(f"-> Keine '{lang}'-Version gefunden (Fehler: {e.response.status_code if e.response else 'N/A'}). Nutze englische Version als Fallback.")
            final_print = base_print
    else:
        final_print = base_print

    is_highres = final_print.get('image_status') == 'highres_scan'
    final_print['quality'] = 'H' if is_highres else 'L'
    final_print['en_highres_fallback'] = None

    return [final_print], None


def find_card_printings(card_name, lang='de', filter_by_artwork=True):
    print(f"Suche alle Drucke für '{card_name}' (Filter Artwork: {filter_by_artwork})...")
    search_query = f'!"{card_name}" unique:prints'
    params = {'q': f'{search_query} lang:{lang}', 'order': 'released', 'dir': 'desc'}
    initial_url = "https://api.scryfall.com/cards/search?" + requests.compat.urlencode(params)
    card_data = fetch_all_pages(initial_url)
    valid_prints_raw = [p for p in card_data if p.get('image_status') in ['highres_scan', 'lowres'] or 'card_faces' in p]

    if not valid_prints_raw and lang != 'en':
        print(f"-> Keine Drucke in '{lang}' gefunden. Wechsle zu Englisch.")
        lang = 'en'
        params['q'] = f'{search_query} lang:en'
        initial_url = "https://api.scryfall.com/cards/search?" + requests.compat.urlencode(params)
        card_data = fetch_all_pages(initial_url)
        valid_prints_raw = [p for p in card_data if p.get('image_status') in ['highres_scan', 'lowres'] or 'card_faces' in p]

    if not valid_prints_raw:
        return None, f"Karte '{card_name}' nicht gefunden"

    for p in valid_prints_raw:
        is_highres = p.get('image_status') == 'highres_scan'
        p['quality'] = 'H' if is_highres else 'L'
        p['en_highres_fallback'] = None

    if not filter_by_artwork:
        print(f"-> {len(valid_prints_raw)} Drucke gefunden (ohne Artwork-Filter).")
        return valid_prints_raw, None

    unique_prints_by_artwork = {}
    for print_ in reversed(valid_prints_raw):
        illustration_id = (print_.get('card_faces', [{}])[0].get('illustration_id') or print_.get('illustration_id'))
        if not illustration_id: continue

        is_highres = print_['quality'] == 'H'

        if lang != 'en':
            try:
                res_en = requests.get(f"https://api.scryfall.com/cards/{print_['set']}/{print_['collector_number']}/en")
                time.sleep(0.1)
                if res_en.status_code == 200:
                    data_en = res_en.json()
                    if data_en.get('image_status') == 'highres_scan':
                        print_['en_highres_fallback'] = data_en
            except requests.exceptions.RequestException: pass

        unique_prints_by_artwork[illustration_id] = print_

    final_prints = list(unique_prints_by_artwork.values())
    print(f"-> {len(final_prints)} einzigartige Artworks für '{card_name}' gefunden.")
    return final_prints, None

def _should_render(card_data: dict, lang: str) -> bool:
    """
    Entscheidet, ob der Renderer für diese Karte sinnvoll ist.
    Rendering lohnt sich wenn:
      - ENABLE_RENDERING aktiv
      - Sprache ist in RENDER_FOR_LANGS
      - Die Karte ein unterstütztes Layout hat
    """
    if not ENABLE_RENDERING or renderer is None:
        return False
    if lang not in RENDER_FOR_LANGS:
        return False
    layout = card_data.get("layout", "normal")
    from card_renderer import SUPPORTED_LAYOUTS
    return layout in SUPPORTED_LAYOUTS

def get_image_by_id(card_id, skip_render=False):
    metadata_path = os.path.join(CARDS_DIR, f"{card_id}.json")
    try:
        card_data = None
        if os.path.exists(metadata_path):
            with open(metadata_path, 'r', encoding='utf-8') as f:
                card_data = json.load(f)
        else:
            api_url = f"https://api.scryfall.com/cards/{card_id}?format=json"
            for attempt in range(3):
                try:
                    response = requests.get(api_url, timeout=15)
                    response.raise_for_status()
                    card_data = response.json()
                    time.sleep(0.1)
                    with open(metadata_path, 'w', encoding='utf-8') as f:
                        json.dump(card_data, f)
                    break
                except requests.exceptions.RequestException as e:
                    print(f"Download attempt {attempt + 1}/3 for ID {card_id} failed: {e}")
                    if attempt < 2:
                        time.sleep(3)
                    else:
                        raise e

        if not card_data:
            return None, None, f"Keine Kartendaten für ID {card_id} nach 3 Versuchen."

        image_uris = []
        if 'card_faces' in card_data and 'image_uris' in card_data['card_faces'][0]:
            image_uris.extend(face['image_uris'].get('png', face['image_uris'].get('large')) for face in card_data['card_faces'])
        elif 'image_uris' in card_data:
            image_uris.append(card_data['image_uris'].get('png', card_data['image_uris'].get('large')))
        else:
            return None, None, f"Keine Bild-URIs für ID {card_id} gefunden."

        downloaded_paths = []
        for i, url in enumerate(image_uris):
            suffix = f"_face_{i}" if len(image_uris) > 1 else ""
            final_path = os.path.join(CARDS_DIR, f"{card_id}{suffix}.jpg")

            if not os.path.exists(final_path):
                img_res = requests.get(url)
                img_res.raise_for_status()
                img = Image.open(io.BytesIO(img_res.content))
                if img.mode in ('RGBA', 'LA'):
                    background = Image.new(img.mode[:-1], img.size, (255, 255, 255))
                    background.paste(img, img.getchannel('A'))
                    img = background
                img.save(final_path, 'jpeg', quality=95)
            downloaded_paths.append(final_path)

        # ── Renderer-Integration ──────────────────────────────────────────────
        lang = card_data.get("lang", "en")
        if not skip_render and _should_render(card_data, lang):
            rendered_paths = []
            for path in downloaded_paths:
                try:
                    rendered = renderer.process_card(card_data, path, overrides=None)
                    rendered_paths.append(rendered if rendered else path)
                    if rendered:
                        print(f"[Renderer] OK: {rendered}")
                except Exception as exc:
                    print(f"[Renderer] Fehler für {card_id}: {exc}")
                    rendered_paths.append(path)
            downloaded_paths = rendered_paths
        # ─────────────────────────────────────────────────────────────────────

        return downloaded_paths if len(downloaded_paths) > 1 else downloaded_paths[0], card_data, None
    except Exception as e:
        return None, None, f"Fehler beim Download für ID {card_id}: {e}"

def process_card_back(source_path, scaling_method='fit'):
    try:
        img = Image.open(source_path)
    except Exception as e:
        return None, f"Fehler beim Öffnen des Bildes: {e}"
    target_size = (744, 1039)
    output_path = os.path.join(UPLOADS_DIR, f"processed_{uuid.uuid4().hex}.jpg")
    if img.mode in ('RGBA', 'LA'):
        background = Image.new(img.mode[:-1], img.size, (255, 255, 255))
        background.paste(img, img.getchannel('A'))
        img = background
    processed_img = ImageOps.fit(img, target_size, Image.Resampling.LANCZOS) if scaling_method == 'fit' else img.resize(target_size, Image.Resampling.LANCZOS)
    processed_img.save(output_path, 'jpeg', quality=95)
    return output_path, None

def create_blank_image():
    blank_path = os.path.join(UPLOADS_DIR, "blank_card.jpg")
    if not os.path.exists(blank_path):
        img = Image.new('RGB', (744, 1039), (255, 255, 255))
        img.save(blank_path, 'jpeg')
    return blank_path

def create_pdf_from_images(image_list, output_path, cols=3, rows=3, mirror_layout=False):
    if not image_list: return
    A4_WIDTH, A4_HEIGHT = 210, 297
    CARD_WIDTH_MM, CARD_HEIGHT_MM = 65.5, 90.9
    MARGIN_X = (A4_WIDTH - cols * CARD_WIDTH_MM) / 2
    MARGIN_Y = (A4_HEIGHT - rows * CARD_HEIGHT_MM) / 2

    if MARGIN_X < 0 or MARGIN_Y < 0:
        MARGIN_X, MARGIN_Y = 1, 1

    pdf = FPDF(orientation='P', unit='mm', format='A4')
    pdf.set_auto_page_break(False)
    pdf.set_margins(0, 0, 0)
    page_size = cols * rows
    for i, path in enumerate(image_list):
        if i % page_size == 0: pdf.add_page()
        col_index = i % cols
        row_index = (i % page_size) // cols
        final_col_index = cols - 1 - col_index if mirror_layout else col_index
        x = MARGIN_X + final_col_index * CARD_WIDTH_MM
        y = MARGIN_Y + row_index * CARD_HEIGHT_MM
        pdf.image(path, x=x, y=y, w=CARD_WIDTH_MM, h=CARD_HEIGHT_MM)
    pdf.output(output_path)
    print(f"PDF erstellt: {output_path}")

def create_duplex_pdf(front_images, back_images, output_path, cols=3, rows=3):
    if not front_images or not back_images or len(front_images) != len(back_images):
        print("Fehler: Listen der Vorder- und Rückseiten sind leer oder nicht gleich lang.")
        return

    A4_WIDTH, A4_HEIGHT = 210, 297
    CARD_WIDTH_MM, CARD_HEIGHT_MM = 65.5, 90.9
    MARGIN_X = (A4_WIDTH - cols * CARD_WIDTH_MM) / 2
    MARGIN_Y = (A4_HEIGHT - rows * CARD_HEIGHT_MM) / 2

    if MARGIN_X < 0 or MARGIN_Y < 0:
        MARGIN_X, MARGIN_Y = 1, 1

    pdf = FPDF(orientation='P', unit='mm', format='A4')
    pdf.set_auto_page_break(False)
    pdf.set_margins(0, 0, 0)
    page_size = cols * rows
    
    num_pages = (len(front_images) + page_size - 1) // page_size

    for page_index in range(num_pages):
        pdf.add_page()
        front_chunk = front_images[page_index * page_size : (page_index + 1) * page_size]
        for i, path in enumerate(front_chunk):
            col_index = i % cols
            row_index = (i % page_size) // cols
            x = MARGIN_X + col_index * CARD_WIDTH_MM
            y = MARGIN_Y + row_index * CARD_HEIGHT_MM
            pdf.image(path, x=x, y=y, w=CARD_WIDTH_MM, h=CARD_HEIGHT_MM)

        pdf.add_page()
        back_chunk = back_images[page_index * page_size : (page_index + 1) * page_size]
        for i, path in enumerate(back_chunk):
            col_index = i % cols
            row_index = (i % page_size) // cols
            final_col_index = cols - 1 - col_index
            x = MARGIN_X + final_col_index * CARD_WIDTH_MM
            y = MARGIN_Y + row_index * CARD_HEIGHT_MM
            pdf.image(path, x=x, y=y, w=CARD_WIDTH_MM, h=CARD_HEIGHT_MM)

    pdf.output(output_path)
    print(f"Duplex-PDF erstellt: {output_path}")

# --- HTML-Vorlagen ---
HOME_TEMPLATE = """<!doctype html><html lang="de"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>MTG Proxy Generator - Schritt 1</title><style>body{font-family:sans-serif;background-color:#333;color:#eee;margin:2rem;line-height:1.6}.container{max-width:800px;margin:auto}textarea,input,select{width:100%;padding:12px;margin-top:5px;margin-bottom:15px;background-color:#444;color:#eee;border:1px solid #666;border-radius:5px;box-sizing:border-box}input[type="submit"]{background-color:#007bff;color:white;font-weight:bold;cursor:pointer;font-size:1.1em;transition:background-color .2s}input[type="submit"]:hover{background-color:#0056b3}label{font-weight:bold}.error{color:#ff8a8a;background-color:#5e3333;padding:10px;border-radius:5px}.info{background-color:#2a3f50;padding:10px;border-radius:5px;border-left:5px solid #007bff;margin-bottom:15px}.renderer-badge{background-color:#1a4a2a;border:1px solid #28a745;padding:8px 12px;border-radius:5px;margin-bottom:15px;font-size:.9em}.gear-btn{position:fixed;top:14px;right:18px;text-decoration:none;font-size:1.5em;opacity:.55;transition:opacity .2s;line-height:1}.gear-btn:hover{opacity:1}</style></head><body><a href="/calibrate" class="gear-btn" title="Zonen-Kalibrierung">⚙️</a><div class="container"><h1>MTG Proxy Generator</h1>
{% if renderer_active %}<div class="renderer-badge">✓ Text-Renderer aktiv – Deutsche Low-Res-Karten werden mit Scryfall-Text überschrieben.</div>{% endif %}
<p><b>Schritt 1:</b> Deckliste, Sprache und Dateiname festlegen.</p>{% if error %}<p class="error">{{ error }}</p>{% endif %}
<div class="info"><p><b>Tipp:</b> Sie können eine bestimmte Edition angeben, indem Sie den 3- bis 5-stelligen Editions-Code in Klammern hinter den Kartennamen schreiben.</p><p><b>Beispiele:</b><br>4 Sol Ring (CM2)<br>1 Counterspell (EMA)<br>10 Island (UNF)</p><p>Wenn keine Edition angegeben wird, werden alle verfügbaren Versionen der Karte zur Auswahl angezeigt.</p></div>
<form id="decklistForm"><label for="decklist">Deckliste hier einfügen:</label><textarea name="decklist" id="decklist" rows="15" placeholder="4 Sol Ring&#10;1 Command Tower&#10;..."></textarea><label for="lang">Sprache der Karten:</label><select name="lang" id="lang">{% for code, name in languages.items() %}<option value="{{ code }}">{{ name }}</option>{% endfor %}</select><label for="filename">Gewünschter PDF-Dateiname (ohne .pdf):</label><input type="text" name="filename" id="filename" value="deck_proxies"><input type="submit" value="Editionen suchen →"></form></div>
<script>
document.getElementById('decklistForm').addEventListener('submit', function(e) {
    e.preventDefault();
    const submitButton = this.querySelector('input[type="submit"]');
    submitButton.value = 'Starte Suche...';
    submitButton.disabled = true;
    const formData = new FormData(this);
    fetch("{{ url_for('start_card_search') }}", {method: 'POST', body: formData})
    .then(response => response.json())
    .then(data => {
        if (data.task_id) { window.location.href = `/loading-search/${data.task_id}`; }
        else { alert('Fehler: ' + (data.error || 'Unbekanntes Problem')); submitButton.value = 'Editionen suchen →'; submitButton.disabled = false; }
    })
    .catch(err => { alert('Netzwerkfehler: ' + err); submitButton.value = 'Editionen suchen →'; submitButton.disabled = false; });
});
</script></body></html>"""

LOADING_TEMPLATE = """<!doctype html><html lang="de"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Suche Karten...</title><style>body{font-family:monospace;background-color:#333;color:#eee;display:flex;justify-content:center;align-items:center;height:100vh;margin:0}.container{width:80%;max-width:600px;text-align:center}.progress-bar-container{border:2px solid #eee;padding:3px;margin:20px 0}.progress-bar{background-color:#007bff;width:0%;height:25px;transition:width .2s ease-in-out;text-align:right;line-height:25px;color:#000;font-weight:bold;overflow:hidden}.status-text{margin-top:15px;height:2em;font-size:1.1em}</style></head><body><div class="container"><h1>Suche nach Karten-Editionen...</h1><p>Dies kann einen Moment dauern.</p><div class="progress-bar-container"><div id="progressBar" class="progress-bar"></div></div><p id="progressText">[....................] 0%</p><div id="statusText">Initialisiere...</div></div>
<script>
const taskId = "{{ task_id }}";
const progressBar = document.getElementById("progressBar");
const progressText = document.getElementById("progressText");
const statusText = document.getElementById("statusText");
function checkStatus() {
    fetch(`/status/${taskId}`).then(response => response.json()).then(data => {
        if (!data || data.status === 'error') { statusText.innerText = `Ein Fehler ist aufgetreten: ${data.message || 'Unbekannter Fehler'}`; clearInterval(interval); return; }
        const percent = data.progress || 0;
        progressBar.style.width = percent + "%";
        const filledChars = Math.round(percent / 5);
        const emptyChars = 20 - filledChars;
        progressText.innerText = `[${'#'.repeat(filledChars)}${'.'.repeat(emptyChars)}] ${Math.round(percent)}%`;
        statusText.innerText = data.message || "";
        if (data.status === 'complete') { clearInterval(interval); statusText.innerText = "Suche abgeschlossen! Leite zur Auswahl weiter..."; window.location.href = `/selection/${taskId}`; }
    }).catch(err => { statusText.innerText = "Verbindung zum Server verloren. Bitte versuche es erneut."; clearInterval(interval); });
}
const interval = setInterval(checkStatus, 1500);
checkStatus();
</script></body></html>"""

SELECTION_TEMPLATE = """<!doctype html><html lang="de"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>MTG Proxy Generator - Schritt 2 & 3</title><style>body{font-family:sans-serif;background-color:#333;color:#eee;margin:0;padding:2rem}.container{max-width:1200px;margin:auto}.card-group,.back-selection-group{background-color:#444;padding:20px;border-radius:8px;margin-bottom:20px}.group-title{border-bottom:2px solid #666;padding-bottom:10px;margin-bottom:15px;font-size:1.5em}div.option-group p{margin-top:0}.option-group label{display:block;margin-bottom:8px}.print-options{display:flex;flex-wrap:wrap;gap:20px}.print-option{cursor:pointer;position:relative;display:flex;flex-direction:column;align-items:center}.print-option input[type="radio"]{display:none}.print-option img{width:150px;border-radius:7px;border:3px solid transparent;transition:all .2s}.print-option .no-image{width:150px;height:209px;border-radius:7px;border:3px solid #666;background-color:#3a3a3a;display:flex;align-items:center;justify-content:center;text-align:center;font-size:.9em;color:#aaa;padding:5px;box-sizing:border-box}input[type="radio"]:checked+.card-display-wrapper{border-color:#007bff;box-shadow:0 0 15px #007bff;border-radius:10px}input[type="radio"][value="do_not_print"]:checked ~ .no-image{border-color:#dc3545;box-shadow:0 0 15px #dc3545}.card-display-wrapper{border:3px solid transparent;padding:5px;display:flex;flex-direction:column;align-items:center;border-radius:10px}.card-display-wrapper.double-face{flex-direction:row;gap:5px}.card-display-wrapper.double-face img{width:120px}.print-option .quality-badge{position:absolute;top:10px;right:10px;padding:2px 5px;font-size:.8em;font-weight:bold;border-radius:4px;z-index:10}.quality-H{background-color:#28a745;color:white}.quality-L{background-color:#dc3545;color:white}.print-option p{text-align:center;margin:5px 0 0;font-size:.9em;color:#ccc}.submit-button{display:block;width:100%;padding:15px;margin-top:20px;background-color:#28a745;color:white;font-weight:bold;cursor:pointer;font-size:1.2em;border:none;border-radius:5px;transition:background-color .2s}.submit-button:hover{background-color:#218838}.fallback-option{border-left:2px dotted #007bff;padding-left:15px}.preview-modal{display:none;position:fixed;z-index:1000;left:0;top:0;width:100%;height:100%;background-color:rgba(0,0,0,.85);justify-content:center;align-items:center}.preview-modal img{max-width:90%;max-height:90%;border-radius:15px}.preview-modal .close-btn{position:absolute;top:20px;right:35px;color:#f1f1f1;font-size:40px;font-weight:bold;cursor:pointer}.upload-section{flex-grow:1}.load-more-btn{background-color:#5a6268;color:white;border:none;padding:10px 15px;border-radius:5px;cursor:pointer;margin-top:10px;transition:background-color .2s}.load-more-btn:hover{background-color:#4a4f54}.load-more-btn:disabled{background-color:#333;cursor:not-allowed}.render-toggle-row{display:flex;align-items:center;gap:12px;margin-bottom:12px;padding:8px 12px;background:#3a3a3a;border-radius:6px;border:1px solid #555}.render-toggle-label{flex:1;font-size:.9em;color:#ccc}.toggle-switch{position:relative;display:inline-block;width:44px;height:24px;flex-shrink:0}.toggle-switch input{opacity:0;width:0;height:0}.toggle-slider{position:absolute;cursor:pointer;top:0;left:0;right:0;bottom:0;background:#666;border-radius:24px;transition:.25s}.toggle-slider:before{position:absolute;content:"";height:18px;width:18px;left:3px;bottom:3px;background:#ccc;border-radius:50%;transition:.25s}.toggle-switch input:checked+.toggle-slider{background:#28a745}.toggle-switch input:checked+.toggle-slider:before{transform:translateX(20px);background:#fff}.render-toggle-hint{font-size:.78em;color:#888}</style></head><body><div class="container">
<form action="{{ url_for('generate_pdf', task_id=task_id) }}" method="post" enctype="multipart/form-data"><h1 class="group-title">Schritt 2: Editionen & Optionen</h1>{% if errors %}<p style="color:red;">Folgende Karten wurden nicht gefunden: {{ errors|join(', ') }}</p>{% endif %}<p>Wähle für jede Karte das gewünschte Artwork aus. (H) = High-Res, (L) = Low-Res.<br><strong>Tipp: Rechtsklick auf ein Bild für eine hochauflösende Vorschau!</strong></p>
<div class="card-group option-group"><h2 class="group-title" style="font-size:1.3em;border-bottom-style:dotted">Optionen für doppelseitige Karten (DFCs)</h2><p>Wie sollen DFCs im PDF platziert werden?</p><label><input type="radio" name="dfc_handling" value="side_by_side" checked> <strong>Nebeneinander</strong></label><label><input type="radio" name="dfc_handling" value="true_backside"> <strong>Echte Rückseiten (Duplex)</strong></label><label><input type="radio" name="dfc_handling" value="dfc_only_backside"> <strong>Nur DFC-Rückseiten (Duplex)</strong></label></div>
{% for card_name, data in cards.items() %}{% set fname = data.original_name|replace(" ", "_") + "_" + (data.set_code or "") %}<div class="card-group"><h2 class="card-title">{{ data.count }}x {{ data.original_name }}{% if data.set_code %} ({{ data.set_code.upper() }}){% endif %}</h2><div class="render-toggle-row"><span class="render-toggle-label">Übersetzer (Text-Renderer)</span><label class="toggle-switch" title="EIN: Karte durch Renderer übersetzen | AUS: Originalkarte direkt übernehmen"><input type="checkbox" name="use_renderer_{{ fname }}" checked onchange="var h=document.getElementById('rtlabel_{{ fname }}');h.textContent=this.checked?'EIN – Renderer aktiv':'AUS – Original–Karte';"><span class="toggle-slider"></span></label><span class="render-toggle-hint" id="rtlabel_{{ fname }}">EIN – Renderer aktiv</span></div><div class="print-options">
<label class="print-option"><input type="radio" name="{{ data.original_name|replace(' ', '_') }}_{{ data.set_code or '' }}" value="do_not_print"><div class="card-display-wrapper"><div class="no-image" style="border-color:#dc3545; color:#ff8a8a;">Nicht drucken</div></div><p>Diese Karte überspringen</p></label>
{% for print in data.printings %}<label class="print-option"><input type="radio" name="{{ data.original_name|replace(' ', '_') }}_{{ data.set_code or '' }}" value="{{ print.id }}" {% if loop.first %}checked{% endif %}><div class="card-display-wrapper {% if 'card_faces' in print %}double-face{% endif %}">{% if 'card_faces' in print and print.card_faces[0].get('image_uris') %}<img src="{{ print.card_faces[0].image_uris.small }}" data-large-url="{{ print.card_faces[0].image_uris.png or print.card_faces[0].image_uris.large }}" alt="{{ print.set_name }} - Front"><img src="{{ print.card_faces[1].image_uris.small }}" data-large-url="{{ print.card_faces[1].image_uris.png or print.card_faces[1].image_uris.large }}" alt="{{ print.set_name }} - Back">{% elif print.image_uris %}<img src="{{ print.image_uris.small }}" data-large-url="{{ print.image_uris.png or print.image_uris.large }}" alt="{{ print.set_name }}">{% else %}<div class="no-image">Bild nicht verfügbar</div>{% endif %}</div><span class="quality-badge quality-{{ print.quality }}">{{ print.quality }}</span><p>{{ print.set_name }} ({{ print.released_at[:4] }})</p></label>{% if print.en_highres_fallback %}<label class="print-option fallback-option"><input type="radio" name="{{ data.original_name|replace(' ', '_') }}_{{ data.set_code or '' }}" value="{{ print.en_highres_fallback.id }}"><div class="card-display-wrapper {% if 'card_faces' in print.en_highres_fallback %}double-face{% endif %}">{% if 'card_faces' in print.en_highres_fallback and print.en_highres_fallback.card_faces[0].get('image_uris') %}<img src="{{ print.en_highres_fallback.card_faces[0].image_uris.small }}" data-large-url="{{ print.en_highres_fallback.card_faces[0].image_uris.png }}" alt="{{ print.set_name }} (EN) - Front"><img src="{{ print.en_highres_fallback.card_faces[1].image_uris.small }}" data-large-url="{{ print.en_highres_fallback.card_faces[1].image_uris.png }}" alt="{{ print.set_name }} (EN) - Back">{% elif print.en_highres_fallback.image_uris %}<img src="{{ print.en_highres_fallback.image_uris.small }}" data-large-url="{{ print.en_highres_fallback.image_uris.png }}" alt="{{ print.set_name }} (EN)">{% else %}<div class="no-image">Bild nicht verfügbar</div>{% endif %}</div><span class="quality-badge quality-H">H</span><p>{{ print.set_name }} (EN - High-Res)</p></label>{% endif %}{% endfor %}</div>
<button type="button" class="load-more-btn" data-card-name="{{ data.original_name }}" data-input-name="{{ data.original_name|replace(' ', '_') }}_{{ data.set_code or '' }}">Mehr laden (Englisch)</button>
</div>{% endfor %}
<div class="back-selection-group"><h1 class="group-title">Schritt 3: Kartenrücken auswählen</h1><label><input type="radio" name="back_choice_type" value="none" checked> Keine allgemeine Rückseite</label><hr style="border-color:#666;margin:15px 0"><label><input type="radio" name="back_choice_type" value="standard"> Standard-Rückseite:</label><div class="print-options back-options">{% for back_img in standard_backs %}<label class="print-option"><input type="radio" name="standard_back" value="{{ back_img }}"><div class="card-display-wrapper"><img src="{{ url_for('serve_card_back', filename=back_img) }}"></div></label>{% endfor %}</div><hr style="border-color:#666;margin:15px 0"><label><input type="radio" name="back_choice_type" value="custom"> Eigene Rückseite:</label><div class="upload-section"><input type="file" name="custom_back_file"><p>Anpassung: <label><input type="radio" name="scaling_method" value="fit" checked> Zuschneiden</label> <label><input type="radio" name="scaling_method" value="stretch"> Strecken</label></p></div></div>
<input type="submit" value="Fertig! PDFs jetzt erstellen" class="submit-button"></form></div>
<div id="previewModal" class="preview-modal"><span class="close-btn">&times;</span><img id="previewImage"></div>
<script>
document.addEventListener('contextmenu', function(e) {
    const img = e.target.closest('img[data-large-url]');
    if (img) { e.preventDefault(); document.getElementById('previewImage').src = img.dataset.largeUrl; document.getElementById('previewModal').style.display = 'flex'; }
});
document.querySelector('.preview-modal .close-btn').addEventListener('click', function() { document.getElementById('previewModal').style.display = 'none'; });
document.getElementById('previewModal').addEventListener('click', function(e) { if (e.target.id === 'previewModal') this.style.display = 'none'; });
document.querySelectorAll('.load-more-btn').forEach(button => {
    button.addEventListener('click', function() {
        const cardName = this.dataset.cardName;
        const inputName = this.dataset.inputName;
        const optionsContainer = this.closest('.card-group').querySelector('.print-options');
        this.textContent = 'Lade...';
        this.disabled = true;
        fetch("{{ url_for('load_more_prints') }}", {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({ card_name: cardName })})
        .then(response => { if (!response.ok) throw new Error('Network response was not ok'); return response.json(); })
        .then(data => {
            if (data.printings) {
                const existingIds = new Set(Array.from(optionsContainer.querySelectorAll('input[type="radio"]')).map(input => input.value));
                data.printings.forEach(print => {
                    if (existingIds.has(print.id)) return;
                    const isDfc = print.card_faces && print.card_faces.length > 1;
                    const frontImg = isDfc ? print.card_faces[0].image_uris : print.image_uris;
                    const backImg = isDfc ? print.card_faces[1].image_uris : null;
                    let imagesHtml = '<div class="no-image">Bild nicht verfügbar</div>';
                    if (frontImg) {
                        imagesHtml = `<img src="${frontImg.small}" data-large-url="${frontImg.png || frontImg.large}" alt="${print.set_name} - Front">`;
                        if (backImg) { imagesHtml += `<img src="${backImg.small}" data-large-url="${backImg.png || backImg.large}" alt="${print.set_name} - Back">`; }
                    }
                    const newOptionHtml = `<label class="print-option"><input type="radio" name="${inputName}" value="${print.id}"><div class="card-display-wrapper ${isDfc ? 'double-face' : ''}">${imagesHtml}</div><span class="quality-badge quality-${print.quality}">${print.quality}</span><p>${print.set_name} (${print.released_at.substring(0, 4)}) (EN)</p></label>`;
                    optionsContainer.insertAdjacentHTML('beforeend', newOptionHtml);
                    existingIds.add(print.id);
                });
                this.textContent = 'Alle englischen Versionen geladen';
            } else { this.textContent = data.error || 'Laden fehlgeschlagen'; }
        })
        .catch(err => { console.error('Fehler beim Laden weiterer Drucke:', err); this.textContent = 'Fehler!'; });
    });
});
</script></body></html>"""

RESULT_TEMPLATE = """<!doctype html><html lang="de"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>MTG Proxy Generator - Fertig!</title><style>body{font-family:sans-serif;background-color:#333;color:#eee;margin:2rem;text-align:center}.container{max-width:800px;margin:auto;background-color:#444;padding:30px;border-radius:8px}a.download-btn{display:inline-block;background-color:#28a745;color:white;padding:15px 30px;text-decoration:none;font-size:1.2em;border-radius:5px;margin:10px;transition:background-color .2s}a.download-btn:hover{background-color:#218838}a.download-btn.duplex{background-color:#007bff}a.download-btn.duplex:hover{background-color:#0056b3}.error{color:#ff8a8a}ul{list-style:none;padding:0;}.download-section{border-bottom: 1px solid #666; padding-bottom: 15px; margin-bottom: 15px;}</style></head><body><div class="container"><h1>PDF-Erstellung abgeschlossen</h1>
{% if pdf_duplex_path %}<div class="download-section"><p><strong>Für den beidseitigen Druck (empfohlen):</strong><br>Diese Datei enthält Vorder- und Rückseiten.</p><a href="{{ url_for('download_file', filename=filename_duplex) }}" class="download-btn duplex">"{{ filename_duplex }}" herunterladen</a></div>{% endif %}
{% if pdf_front_path %}<div class="download-section"><p><strong>Nur Vorderseiten:</strong></p><a href="{{ url_for('download_file', filename=filename_front) }}" class="download-btn">"{{ filename_front }}" herunterladen</a></div>{% endif %}
{% if pdf_back_path %}<div class="download-section"><p><strong>Nur Rückseiten:</strong></p><a href="{{ url_for('download_file', filename=filename_back) }}" class="download-btn">"{{ filename_back }}" herunterladen</a></div>{% endif %}
{% if not pdf_front_path and not pdf_back_path and not pdf_duplex_path %}<p class="error">Es konnten keine PDFs erstellt werden, da keine Karten ausgewählt oder gefunden wurden.</p>{% endif %}
{% if errors %}<h3>Einige Karten konnten nicht geladen werden:</h3><ul>{% for error in errors %}<li>{{ error }}</li>{% endfor %}</ul>{% endif %}
<p style="margin-top:30px;"><a href="{{ url_for('home') }}">Neue PDF erstellen</a></p></div></body></html>"""


# --- Web-Routen ---

@app.route('/')
def home():
    """Zeigt die Startseite an."""
    return render_template_string(HOME_TEMPLATE, languages=LANGUAGES,
                                  renderer_active=ENABLE_RENDERING and renderer is not None)

def _run_card_search(task_id, card_requests, lang, filename):
    """Sucht im Hintergrund nach Karten, entweder gezielt nach Edition oder allgemein."""
    try:
        tasks[task_id] = {'status': 'processing', 'progress': 0, 'message': 'Starte Kartensuche...'}
        cards_for_selection = {}
        error_messages = []
        
        total_cards = len(card_requests)
        for i, req in enumerate(card_requests):
            card_name = req['name']
            set_code = req['set']
            count = req['count']
            
            tasks[task_id]['message'] = f"Suche nach: {card_name}" + (f" ({set_code.upper()})" if set_code else "") + f" ({i+1}/{total_cards})"

            if set_code:
                printings, error = find_specific_card_printing(card_name, set_code, lang)
            else:
                printings, error = find_card_printings(card_name, lang)
            
            if error:
                error_messages.append(error)
            else:
                unique_key = f"{card_name}_{set_code}" if set_code else card_name
                cards_for_selection[unique_key] = {
                    'count': count, 
                    'printings': printings,
                    'set_code': set_code,
                    'original_name': card_name
                }

            tasks[task_id]['progress'] = (i + 1) / total_cards * 100
            time.sleep(0.1)

        tasks[task_id]['result_data'] = {
            'cards_for_selection': cards_for_selection,
            'card_requests': card_requests,
            'filename_base': filename,
            'error_messages': error_messages
        }
        tasks[task_id]['status'] = 'complete'

    except Exception as e:
        tasks[task_id]['status'] = 'error'
        tasks[task_id]['message'] = f"Ein schwerwiegender Fehler ist aufgetreten: {e}"
        print(f"Schwerwiegender Fehler in Task {task_id}: {e}")

@app.route('/start-card-search', methods=['POST'])
def start_card_search():
    """Startet den asynchronen Such-Job nach Analyse der Deckliste."""
    decklist_text = request.form.get('decklist')
    if not decklist_text:
        return jsonify({'error': 'Die Deckliste darf nicht leer sein.'})

    line_regex = re.compile(r"^\s*(\d*)\s*([^(]+?)\s*(?:\(([^)]+)\))?\s*$")
    
    card_requests = []
    lines = [line.strip() for line in decklist_text.strip().split('\n') if line.strip()]

    for line in lines:
        match = line_regex.match(line)
        if match:
            count, name, set_code = match.groups()
            card_requests.append({
                'count': int(count) if count.isdigit() else 1,
                'name': name.strip(),
                'set': set_code.strip().lower() if set_code else None
            })

    if not card_requests:
        return jsonify({'error': "Keine gültigen Karten gefunden."})

    task_id = uuid.uuid4().hex
    lang = request.form.get('lang')
    filename = request.form.get('filename', 'deck_proxies').replace('.pdf', '')
    
    thread = threading.Thread(target=_run_card_search, args=(task_id, card_requests, lang, filename))
    thread.start()
    
    return jsonify({'task_id': task_id})

@app.route('/loading-search/<task_id>')
def loading_search_page(task_id):
    """Zeigt die Lade-Seite für die Kartensuche an."""
    return render_template_string(LOADING_TEMPLATE, task_id=task_id)

@app.route('/status/<task_id>')
def task_status(task_id):
    """Liefert den Status eines Jobs als JSON."""
    task = tasks.get(task_id, {})
    return jsonify({k: v for k, v in task.items() if k != 'result_data'})

@app.route('/selection/<task_id>')
def show_selection_page(task_id):
    task = tasks.get(task_id)
    if not task or task.get('status') != 'complete':
        if task and task.get('status') == 'processing':
            return redirect(url_for('loading_search_page', task_id=task_id))
        return redirect(url_for('home'))

    result_data = task.get('result_data', {})
    cards_for_selection = result_data.get('cards_for_selection', {})
    sorted_cards = sorted(cards_for_selection.items(), key=lambda item: item[1]['original_name'])
    standard_backs = [f for f in os.listdir(CARD_BACKS_DIR) if f.lower().endswith(('.png', '.jpg', '.jpeg', '.webp'))] if os.path.exists(CARD_BACKS_DIR) else []
    
    return render_template_string(
        SELECTION_TEMPLATE, 
        cards=dict(sorted_cards),
        errors=result_data.get('error_messages'),
        standard_backs=standard_backs,
        task_id=task_id
    )

@app.route('/load-more', methods=['POST'])
def load_more_prints():
    """Lädt zusätzliche, englische Karten-Versionen für eine bestimmte Karte."""
    card_name = request.json.get('card_name')
    if not card_name:
        return jsonify({'error': 'Kartenname fehlt.'}), 400
    printings, error = find_card_printings(card_name, lang='en', filter_by_artwork=False)
    if error:
        return jsonify({'error': error}), 404
    return jsonify({'printings': printings})

@app.route('/generate/<task_id>', methods=['POST'])
def generate_pdf(task_id):
    """Verarbeitet die Auswahl und erstellt die PDFs."""
    task = tasks.get(task_id)
    if not task:
        return redirect(url_for('home'))

    selections = request.form
    result_data = task.get('result_data', {})
    card_requests = result_data.get('card_requests', [])
    filename_base = result_data.get('filename_base', 'proxies')
    failed_cards = result_data.get('error_messages', [])

    final_card_ids = []
    skip_render_ids = set()  # IDs, für die der Renderer übersprungen werden soll
    for req in card_requests:
        form_field_name = f"{req['name'].replace(' ', '_')}_{req['set'] or ''}"
        selected_id = selections.get(form_field_name)
        if selected_id and selected_id != 'do_not_print':
            final_card_ids.extend([selected_id] * int(req['count']))
            # Checkbox nicht vorhanden → Toggle war AUS → Renderer überspringen
            if f"use_renderer_{form_field_name}" not in selections:
                skip_render_ids.add(selected_id)

    id_to_path_map, id_to_metadata_map = {}, {}
    unique_ids = sorted(list(set(final_card_ids)))
    for card_id in unique_ids:
        paths, metadata, error = get_image_by_id(card_id, skip_render=card_id in skip_render_ids)
        if paths and metadata:
            id_to_path_map[card_id] = paths
            id_to_metadata_map[card_id] = metadata
        else:
            card_name_for_error = "Unbekannt"
            for m in id_to_metadata_map.values():
                if m.get('id') == card_id:
                    card_name_for_error = m.get('name')
                    break
            failed_cards.append(f"Download für '{card_name_for_error}' (ID: {card_id}) fehlgeschlagen: {error}")

    card_back_path = None
    if request.form.get('back_choice_type') == 'custom':
        if custom_back_file := request.files.get('custom_back_file'):
            if custom_back_file.filename != '':
                temp_path = os.path.join(UPLOADS_DIR, f"{uuid.uuid4().hex}_{custom_back_file.filename}")
                custom_back_file.save(temp_path)
                processed_path, error = process_card_back(temp_path, request.form.get('scaling_method', 'fit'))
                if error: failed_cards.append(f"Verarbeitung der Rückseite fehlgeschlagen: {error}")
                else: card_back_path = processed_path
    elif request.form.get('back_choice_type') == 'standard':
        if back_filename := request.form.get('standard_back'):
            card_back_path = os.path.join(CARD_BACKS_DIR, back_filename)

    dfc_handling = selections.get('dfc_handling', 'side_by_side')
    blank_image_path = create_blank_image() if dfc_handling == 'dfc_only_backside' else None
    needs_back_pdf = (card_back_path is not None) or (dfc_handling in ['true_backside', 'dfc_only_backside'])
    
    all_front_image_paths, all_back_image_paths = [], []

    for card_id in final_card_ids:
        path_or_paths = id_to_path_map.get(card_id)
        metadata = id_to_metadata_map.get(card_id)
        if not path_or_paths or not metadata: continue

        is_dfc = 'card_faces' in metadata and isinstance(path_or_paths, list) and len(path_or_paths) > 1
        front_paths_to_add = []
        if is_dfc and dfc_handling in ['true_backside', 'dfc_only_backside']:
            front_paths_to_add.append(path_or_paths[0])
        else:
            front_paths_to_add.extend(path_or_paths if isinstance(path_or_paths, list) else [path_or_paths])
        all_front_image_paths.extend(front_paths_to_add)

        if needs_back_pdf:
            if is_dfc and dfc_handling in ['true_backside', 'dfc_only_backside']:
                all_back_image_paths.append(path_or_paths[1])
            else:
                back_to_use = card_back_path if card_back_path else blank_image_path
                if back_to_use:
                    all_back_image_paths.extend([back_to_use] * len(front_paths_to_add))
    
    # ── Preview-Seite ─────────────────────────────────────────────────────────
    preview_cards = []
    for card_id in sorted(set(final_card_ids)):
        path = id_to_path_map.get(card_id)
        meta = id_to_metadata_map.get(card_id)
        if not path or not meta:
            continue
        base_path = path if isinstance(path, str) else (path[0] if path else None)
        if not base_path:
            continue
        # Rendered-Version bevorzugen, sonst Original
        rp = str(base_path).replace('.jpg','_rendered.png').replace('.jpeg','_rendered.png')
        display_file = os.path.basename(rp) if os.path.exists(rp) else os.path.basename(base_path)
        is_rendered  = os.path.exists(rp)
        from card_renderer import _auto_font_profile, FONT_PROFILES
        auto_profile = _auto_font_profile(meta.get('released_at', '2020-01-01'))
        preview_name = meta.get('printed_name') or meta.get('name', '')
        preview_type_line = meta.get('printed_type_line') or meta.get('type_line', '')
        preview_oracle_full = meta.get('printed_text') or meta.get('oracle_text', '') or ''
        preview_oracle = (preview_oracle_full or '').strip().split('\n')[0][:80]
        preview_flavor = meta.get('flavor_text', '') or ''
        face = None
        if 'card_faces' in meta and meta['card_faces']:
            face = meta['card_faces'][0]
            if not preview_oracle_full:
                preview_oracle_full = face.get('printed_text') or face.get('oracle_text', '') or ''
            if not preview_flavor:
                preview_flavor = face.get('flavor_text', '') or ''
        preview_pt = ''
        if meta.get('power') and meta.get('toughness'):
            preview_pt = f"{meta.get('power')}/{meta.get('toughness')}"
        preview_cards.append({
            'card_id':            card_id,
            'name':               meta.get('printed_name') or meta.get('name', card_id),
            'rendered_filename':  display_file,
            'is_rendered':        is_rendered,
            'auto_profile':       auto_profile,
            'released_at':        meta.get('released_at', '')[:4],
            'preview_name':       preview_name,
            'preview_type_line':  preview_type_line,
            'preview_oracle':     preview_oracle,
            'preview_oracle_full':preview_oracle_full,
            'preview_flavor':     preview_flavor,
            'preview_pt':         preview_pt,
        })

    if preview_cards:
        preview_id = uuid.uuid4().hex
        preview_tasks[preview_id] = {
            'final_card_ids':   final_card_ids,
            'id_to_path_map':   id_to_path_map,
            'card_meta':        id_to_metadata_map,
            'card_paths':       {cid: (p if isinstance(p, str) else p[0])
                                 for cid, p in id_to_path_map.items()},
            'filename_base':    filename_base,
            'dfc_handling':     dfc_handling,
            'card_back_path':   card_back_path,
            'failed_cards':     failed_cards,
        }
        if task_id in tasks:
            del tasks[task_id]
        session.clear()
        cal_zones = {}
        if os.path.exists(CALIBRATION_FILE):
            with open(CALIBRATION_FILE) as f:
                cal_zones = json.load(f)
        return render_template_string(PREVIEW_TEMPLATE,
                                      cards=preview_cards,
                                      preview_id=preview_id,
                                      cal_zones=cal_zones)

    # Fallback: kein Renderer aktiv → PDFs direkt erstellen
    pdf_front_path, pdf_back_path, pdf_duplex_path = None, None, None
    filename_front = f"{filename_base}_front.pdf"
    filename_back  = f"{filename_base}_back.pdf"
    filename_duplex = f"{filename_base}_duplex.pdf"

    if all_front_image_paths:
        pdf_front_path = os.path.join(OUTPUT_DIR, filename_front)
        create_pdf_from_images(all_front_image_paths, pdf_front_path, mirror_layout=False)

    if all_back_image_paths:
        if len(all_front_image_paths) == len(all_back_image_paths):
            pdf_back_path = os.path.join(OUTPUT_DIR, filename_back)
            create_pdf_from_images(all_back_image_paths, pdf_back_path, mirror_layout=True)
            pdf_duplex_path = os.path.join(OUTPUT_DIR, filename_duplex)
            create_duplex_pdf(all_front_image_paths, all_back_image_paths, pdf_duplex_path)
        else:
            failed_cards.append("Fehler: Anzahl Vorder-/Rückseiten stimmt nicht.")

    if task_id in tasks:
        del tasks[task_id]
    session.clear()

    return render_template_string(
        RESULT_TEMPLATE,
        pdf_front_path=pdf_front_path, pdf_back_path=pdf_back_path, pdf_duplex_path=pdf_duplex_path,
        filename_front=filename_front if pdf_front_path else None,
        filename_back=filename_back if pdf_back_path else None,
        filename_duplex=filename_duplex if pdf_duplex_path else None,
        errors=list(set(failed_cards))
    )

@app.route('/backs/<path:filename>')
def serve_card_back(filename):
    return send_from_directory(CARD_BACKS_DIR, filename)

@app.route('/download/<path:filename>')
def download_file(filename):
    return send_from_directory(OUTPUT_DIR, filename, as_attachment=True)

@app.route('/card-image/<path:filename>')
def serve_card_image(filename):
    return send_from_directory(CARDS_DIR, filename)

# ── Preview-System ────────────────────────────────────────────────────────────

PREVIEW_TEMPLATE = """<!doctype html>
<html lang="de"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Vorschau &amp; Feinabstimmung</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#1a1a1a;color:#eee;font-family:sans-serif;padding:16px}
h1{font-size:1.1em;color:#7cf;margin-bottom:12px}
.hint{font-size:.8em;color:#888;margin-bottom:16px;line-height:1.5}
.cards-grid{display:flex;flex-wrap:wrap;gap:24px}
.card-item{background:#252525;border:1px solid #444;border-radius:8px;padding:12px;display:flex;flex-direction:column;gap:8px;width:280px}
.card-item h3{font-size:.85em;color:#ccc;word-break:break-word}
.card-preview{position:relative;width:100%}
.card-preview img{width:100%;border-radius:4px;display:block}
.card-preview canvas{position:absolute;top:0;left:0;width:100%;height:100%;pointer-events:none}
.controls{display:flex;flex-direction:column;gap:5px;font-size:.78em}
.row{display:flex;align-items:center;gap:6px}
.row label{width:68px;flex-shrink:0;font-family:monospace}
.row input[type=range]{flex:1;accent-color:#7cf;cursor:pointer}
.row span{width:40px;text-align:right;font-family:monospace;color:#7cf}
.section-divider{border:none;border-top:1px solid #3a3a3a;margin:6px 0}
.section-title{font-size:.7em;color:#666;text-transform:uppercase;letter-spacing:.06em;margin-bottom:2px}
.text-input-row{display:flex;flex-direction:column;gap:2px;margin-bottom:3px}
.text-input-row label{font-family:monospace;font-size:.75em}
.text-input-row input[type=text],.text-input-row textarea{background:#1e1e1e;border:1px solid #444;border-radius:3px;color:#eee;font-size:.78em;padding:3px 6px;width:100%;resize:vertical}
.text-input-row textarea{min-height:40px;font-family:sans-serif}
.text-input-row input[type=text]:focus,.text-input-row textarea:focus{outline:none;border-color:#7cf}
.btn-rerender{padding:5px 10px;background:#1a4a6a;border:1px solid #3af;border-radius:4px;color:#7cf;cursor:pointer;font-size:.8em}
.btn-rerender:hover{background:#2a6a9a}
.btn-rerender.loading{opacity:.5;cursor:wait}
.status{font-size:.72em;min-height:1em}
.bottom-bar{position:sticky;bottom:0;background:#1a1a1a;border-top:1px solid #444;padding:12px 0;display:flex;gap:12px;align-items:center;margin-top:24px}
.btn-confirm{padding:10px 28px;background:#2a7;border:none;border-radius:5px;color:#fff;font-size:1em;cursor:pointer;font-weight:bold}
.btn-confirm:hover{background:#3b8}
.btn-confirm:disabled{background:#444;cursor:wait}
.spinner{display:none;width:18px;height:18px;border:2px solid #555;border-top-color:#7cf;border-radius:50%;animation:spin .7s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}
</style></head><body>
<h1>Vorschau &amp; Feinabstimmung</h1>
<div class="hint">
  Schieberegler → Text-Overlay verschiebt sich live auf der Karte.<br>
  Wenn der Text passt → <b>↺ Neu rendern</b> → Karte wird mit neuem Text gerendert.<br>
  Alle bestätigt → <b>✓ PDFs erstellen</b>.
</div>

<div class="cards-grid" id="cardsGrid">
{% for card in cards %}
<div class="card-item"
     data-card-id="{{ card.card_id }}"
     data-preview-name="{{ card.preview_name }}"
     data-preview-type="{{ card.preview_type_line }}"
     data-preview-oracle="{{ card.preview_oracle }}"
     data-preview-pt="{{ card.preview_pt }}">
  <h3>{{ card.name }}</h3>
  <div class="card-preview" id="wrap_{{ card.card_id }}">
    <img src="/card-rendered/{{ card.rendered_filename }}" id="img_{{ card.card_id }}"
         {% if not card.is_rendered %}style="opacity:.5"{% endif %}>
    <canvas id="cvs_{{ card.card_id }}"></canvas>
  </div>
  <div class="controls">
    {% if card.is_rendered %}
    <div class="row" style="margin-bottom:4px">
      <label style="color:#aaa;width:68px">Schrift</label>
      <select id="fp_{{ card.card_id }}"
              data-card="{{ card.card_id }}"
              class="font-profile-select"
              style="flex:1;padding:3px 5px;background:#333;color:#eee;border:1px solid #555;border-radius:4px;font-size:.8em">
        <option value="modern" {% if card.auto_profile != 'old' %}selected{% endif %}>
          Neue Fonts
        </option>
        <option value="old" {% if card.auto_profile == 'old' %}selected{% endif %}>
          Alte Fonts
        </option>
      </select>
    </div>
    {% for zone_key, zone_label, col in [('name','Name','#e33'),('type_line','Typzeile','#33e'),('oracle_box','Oracle','#3a3'),('pt_box','P/T','#e80')] %}
    <div class="row">
      <label style="color:{{ col }}">{{ zone_label }}</label>
      <input type="range" min="-40" max="40" value="0" step="1"
             id="dy_{{ card.card_id }}_{{ zone_key }}"
             data-card="{{ card.card_id }}" data-zone="{{ zone_key }}"
             class="zone-slider">
      <span id="val_{{ card.card_id }}_{{ zone_key }}">0px</span>
    </div>
    {% endfor %}
    <hr class="section-divider">
    <div class="section-title">Schriftgröße</div>
    {% for fs_key, fs_label, fs_default, col in [('name','Name',36,'#e33'),('type_line','Typzeile',26,'#33e'),('oracle','Regeltext',24,'#3a3'),('flavor','Flavortext',24,'#8a8')] %}
    <div class="row">
      <label style="color:{{ col }}">{{ fs_label }}</label>
      <input type="range" min="8" max="60" value="{{ fs_default }}" step="1"
             id="fs_{{ card.card_id }}_{{ fs_key }}"
             data-card="{{ card.card_id }}" data-fskey="{{ fs_key }}"
             class="fontsize-slider">
      <span id="fsval_{{ card.card_id }}_{{ fs_key }}">{{ fs_default }}pt</span>
    </div>
    {% endfor %}
    <hr class="section-divider">
    <div class="section-title">Text bearbeiten</div>
    {% for txt_key, txt_label, txt_val, col in [('name','Name',card.preview_name,'#e33'),('type_line','Typzeile',card.preview_type_line,'#33e')] %}
    <div class="text-input-row">
      <label style="color:{{ col }}">{{ txt_label }}</label>
      <input type="text" id="txt_{{ card.card_id }}_{{ txt_key }}"
             data-card="{{ card.card_id }}" data-txtkey="{{ txt_key }}"
             class="text-override-input"
             value="{{ txt_val }}" placeholder="(unveränderter Text)">
    </div>
    {% endfor %}
    <div class="text-input-row">
      <label style="color:#3a3">Regeltext</label>
      <textarea id="txt_{{ card.card_id }}_oracle"
                data-card="{{ card.card_id }}" data-txtkey="oracle"
                class="text-override-input"
                placeholder="(unveränderter Text)" rows="3">{{ card.preview_oracle_full }}</textarea>
    </div>
    <div class="text-input-row">
      <label style="color:#8a8">Flavortext</label>
      <textarea id="txt_{{ card.card_id }}_flavor"
                data-card="{{ card.card_id }}" data-txtkey="flavor"
                class="text-override-input"
                placeholder="(kein Flavortext)" rows="2">{{ card.preview_flavor }}</textarea>
    </div>
    {% else %}
    <div style="color:#888;font-size:.75em;padding:4px 0">Kein Rendering (kein EN-HR verfügbar)</div>
    {% endif %}
  </div>
  <button class="btn-rerender" onclick="rerender('{{ card.card_id }}')">↺ Neu rendern</button>
  <div class="status" id="status_{{ card.card_id }}"></div>
</div>
{% endfor %}
</div>

<div class="bottom-bar">
  <button class="btn-confirm" id="confirmBtn" onclick="confirmAll()">✓ PDFs erstellen</button>
  <div class="spinner" id="spinner"></div>
  <span id="confirmStatus" style="font-size:.85em;color:#888"></span>
</div>

<script>
const PREVIEW_ID = "{{ preview_id }}";
const IMG_H = 1040;
const CAL_ZONES = {{ cal_zones | tojson }};
const ZCOLS = {name:'#ffe7a6', type_line:'#b3dcff', oracle_box:'#c8ffd2', pt_box:'#ffd1b0'};
const cardDy = {};

// ── Canvas initialisieren ────────────────────────────────────────────────────
function initCanvas(cardId) {
  const img = document.getElementById('img_' + cardId);
  const cvs = document.getElementById('cvs_' + cardId);
  if (!img || !cvs) return;
  cvs.width  = img.naturalWidth  || 745;
  cvs.height = img.naturalHeight || 1040;
  cvs.style.width  = img.offsetWidth  + 'px';
  cvs.style.height = img.offsetHeight + 'px';
  if (!cardDy[cardId]) cardDy[cardId] = {name:0, type_line:0, oracle_box:0, pt_box:0};
  drawPreviewText(cardId);
}

function initAllCanvases() {
  document.querySelectorAll('.card-item').forEach(el => {
    const cid = el.dataset.cardId;
    const img = document.getElementById('img_' + cid);
    if (!img) return;
    if (img.complete && img.naturalWidth > 0) {
      initCanvas(cid);
    } else {
      img.addEventListener('load',  () => initCanvas(cid));
      img.addEventListener('error', () => { img.style.opacity = '.3'; });
    }
  });
}

window.addEventListener('load',   initAllCanvases);
window.addEventListener('resize', () => {
  document.querySelectorAll('.card-item').forEach(el => {
    const cid = el.dataset.cardId;
    const img = document.getElementById('img_' + cid);
    const cvs = document.getElementById('cvs_' + cid);
    if (img && cvs) {
      cvs.style.width  = img.offsetWidth  + 'px';
      cvs.style.height = img.offsetHeight + 'px';
    }
  });
});

// ── Slider: Event-Delegation (kein inline-oninput) ───────────────────────────
document.addEventListener('input', e => {
  const sl = e.target;
  // Y-Verschiebungs-Schieberegler
  if (sl.classList.contains('zone-slider')) {
    const cardId  = sl.dataset.card;
    const zoneKey = sl.dataset.zone;
    const val     = parseInt(sl.value);
    const lbl = document.getElementById('val_' + cardId + '_' + zoneKey);
    if (lbl) lbl.textContent = val + 'px';
    if (!cardDy[cardId]) cardDy[cardId] = {name:0, type_line:0, oracle_box:0, pt_box:0};
    cardDy[cardId][zoneKey] = val / IMG_H;
    drawPreviewText(cardId);
    const st = document.getElementById('status_' + cardId);
    if (st) st.textContent = '';
  }
  // Schriftgrößen-Schieberegler
  if (sl.classList.contains('fontsize-slider')) {
    const cardId = sl.dataset.card;
    const fsKey  = sl.dataset.fskey;
    const val    = parseInt(sl.value);
    const lbl = document.getElementById('fsval_' + cardId + '_' + fsKey);
    if (lbl) lbl.textContent = val + 'pt';
    const st = document.getElementById('status_' + cardId);
    if (st) st.textContent = '';
  }
});

// ── Text-Overlay zeichnen (ohne Kästen) ─────────────────────────────────────
function drawPreviewText(cardId) {
  const cvs = document.getElementById('cvs_' + cardId);
  if (!cvs || cvs.width === 0) return;
  const cardEl = document.querySelector(`.card-item[data-card-id="${cardId}"]`);
  if (!cardEl) return;
  const ctx = cvs.getContext('2d');
  const W = cvs.width, H = cvs.height;
  ctx.clearRect(0, 0, W, H);

  const dy = cardDy[cardId] || {};
  const drawShadowText = (text, x, y, color, font) => {
    if (!text) return;
    ctx.font = font;
    ctx.fillStyle = 'rgba(0,0,0,0.8)';
    ctx.fillText(text, x + 1, y + 1);
    ctx.fillStyle = color;
    ctx.fillText(text, x, y);
  };

  const nameZone = CAL_ZONES.name && (CAL_ZONES.name.coords || CAL_ZONES.name);
  if (nameZone) {
    const y = (nameZone[1] + (dy.name || 0)) * H + 22;
    drawShadowText(cardEl.dataset.previewName || '', nameZone[0] * W + 10, y, ZCOLS.name, 'bold 24px serif');
  }

  const typeZone = CAL_ZONES.type_line && (CAL_ZONES.type_line.coords || CAL_ZONES.type_line);
  if (typeZone) {
    const y = (typeZone[1] + (dy.type_line || 0)) * H + 22;
    drawShadowText(cardEl.dataset.previewType || '', typeZone[0] * W + 10, y, ZCOLS.type_line, 'bold 20px serif');
  }

  const oracleZone = CAL_ZONES.oracle_box && (CAL_ZONES.oracle_box.coords || CAL_ZONES.oracle_box);
  if (oracleZone) {
    const y = (oracleZone[1] + (dy.oracle_box || 0)) * H + 22;
    drawShadowText(cardEl.dataset.previewOracle || '', oracleZone[0] * W + 10, y, ZCOLS.oracle_box, '16px serif');
  }

  const ptZone = CAL_ZONES.pt_box && (CAL_ZONES.pt_box.coords || CAL_ZONES.pt_box);
  const ptText = cardEl.dataset.previewPt || '';
  if (ptZone && ptText) {
    const y = (ptZone[1] + (dy.pt_box || 0)) * H + 22;
    drawShadowText(ptText, ptZone[0] * W + 6, y, ZCOLS.pt_box, 'bold 18px serif');
  }
}

// ── Neu rendern ──────────────────────────────────────────────────────────────
async function rerender(cardId) {
  const btn    = document.querySelector(`.card-item[data-card-id="${cardId}"] .btn-rerender`);
  const status = document.getElementById('status_' + cardId);
  btn.classList.add('loading'); btn.textContent = 'Rendert…';
  if (status) status.textContent = '';

  const dy = cardDy[cardId] || {};
  const dy_overrides = {};
  Object.entries(dy).forEach(([k,v]) => { if (Math.abs(v) > 0.0001) dy_overrides[k] = v; });

  // Font-Profil aus Dropdown lesen
  const fpSel = document.getElementById('fp_' + cardId);
  const font_profile = fpSel ? fpSel.value : 'auto';

  // Schriftgrößen-Overrides lesen
  const font_sizes = {};
  ['name','type_line','oracle','flavor'].forEach(k => {
    const sl = document.getElementById('fs_' + cardId + '_' + k);
    if (sl) {
      const defaults = {name:36, type_line:26, oracle:24, flavor:24};
      const v = parseInt(sl.value);
      if (v !== defaults[k]) font_sizes[k] = v;
    }
  });

  // Text-Overrides lesen
  const text_overrides = {};
  ['name','type_line','oracle','flavor'].forEach(k => {
    const el = document.getElementById('txt_' + cardId + '_' + k);
    if (el) {
      const v = el.value.trim();
      if (v !== '') text_overrides[k] = el.value;
    }
  });

  try {
    const r = await fetch('/preview/rerender', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({
        card_id: cardId,
        dy_overrides,
        preview_id: PREVIEW_ID,
        font_profile,
        font_sizes: Object.keys(font_sizes).length ? font_sizes : undefined,
        text_overrides: Object.keys(text_overrides).length ? text_overrides : undefined
      })
    });
    const d = await r.json();
    if (!r.ok) {
      const msg = d && d.error ? d.error : `HTTP ${r.status}`;
      if (status) { status.textContent = '✗ ' + msg; status.style.color = '#f88'; }
      btn.classList.remove('loading'); btn.textContent = '↺ Neu rendern';
      return;
    }
    if (d.ok) {
      const img = document.getElementById('img_' + cardId);
      img.onload = () => {
        ['name','type_line','oracle_box','pt_box'].forEach(z => {
          const sl = document.getElementById('dy_'+cardId+'_'+z);
          const vl = document.getElementById('val_'+cardId+'_'+z);
          if (sl) sl.value = 0;
          if (vl) vl.textContent = '0px';
        });
        if (cardDy[cardId]) Object.keys(cardDy[cardId]).forEach(k => cardDy[cardId][k]=0);
        initCanvas(cardId);
      };
      img.src = '/card-rendered/' + d.filename + '?t=' + Date.now();
      if (status) { status.textContent = '✓ Aktualisiert'; status.style.color = '#8f8'; }
    } else {
      if (status) { status.textContent = '✗ ' + (d.error||'Fehler'); status.style.color = '#f88'; }
    }
  } catch(e) {
    if (status) { status.textContent = '✗ Netzwerkfehler'; status.style.color='#f88'; }
  }
  btn.classList.remove('loading'); btn.textContent = '↺ Neu rendern';
}

// ── PDFs bestätigen ──────────────────────────────────────────────────────────
async function confirmAll() {
  const btn     = document.getElementById('confirmBtn');
  const spinner = document.getElementById('spinner');
  const status  = document.getElementById('confirmStatus');
  btn.disabled = true; spinner.style.display = 'block';
  status.textContent = 'PDFs werden erstellt…';
  try {
    const r = await fetch('/preview/confirm', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({preview_id:PREVIEW_ID})
    });
    const d = await r.json();
    if (d.ok && d.redirect) { window.location.href = d.redirect; }
    else { status.textContent = '✗ '+(d.error||'Fehler'); btn.disabled = false; }
  } catch(e) { status.textContent = '✗ Netzwerkfehler'; btn.disabled = false; }
  spinner.style.display = 'none';
}
</script></body></html>"""

# Preview-State: preview_id → {card_data, pdf_params, ...}
preview_tasks: dict = {}

@app.route('/card-rendered/<path:filename>')
def serve_rendered(filename):
    """Serviert gerenderte PNG-Dateien aus card_images/."""
    return send_from_directory(CARDS_DIR, filename)

@app.route('/preview/rerender', methods=['POST'])
def preview_rerender():
    """Rendert eine einzelne Karte mit angepassten Feinabstimmungs-Overrides neu.

    Erwartet JSON:
        card_id        – Karten-ID
        preview_id     – ID der aktuellen Preview-Session
        dy_overrides   – {'zone_key': float, …}  (optional)
        font_profile   – 'modern' | 'old' | 'medieval' | 'auto' (optional)
        font_sizes     – {'name': 36, 'type_line': 26, 'oracle': 24, 'flavor': 24} (optional)
        text_overrides – {'name': '...', 'type_line': '...', 'oracle': '...', 'flavor': '...'} (optional)

    Alle Feinabstimmungs-Optionen werden als `overrides`-Dict an process_card()
    übergeben. Neue Features brauchen hier nur einen weiteren `if`-Block –
    process_card() und der Cache-Check müssen NICHT angefasst werden.
    """
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({'ok': False, 'error': 'Ungültige Request-Daten (JSON erwartet)'}), 400

    card_id    = data.get('card_id')
    preview_id = data.get('preview_id')
    if not card_id or not preview_id:
        return jsonify({'ok': False, 'error': 'card_id oder preview_id fehlt'}), 400

    pt = preview_tasks.get(preview_id)
    if not pt or card_id not in pt['card_meta']:
        return jsonify({'ok': False, 'error': 'Preview nicht gefunden'})

    if renderer is None:
        return jsonify({'ok': False, 'error': 'Renderer nicht aktiv'})

    # ── Overrides zusammenbauen ────────────────────────────────────────────────
    # Jedes zukünftige Feinabstimmungs-Feature wird hier als weiterer
    # Schlüssel ins overrides-Dict eingefügt – sonst keine Änderungen nötig.
    overrides: dict = {}

    raw_dy = data.get('dy_overrides', {})
    if not isinstance(raw_dy, dict):
        return jsonify({'ok': False, 'error': 'dy_overrides muss ein Objekt sein'}), 400
    try:
        dy = {k: float(v) for k, v in raw_dy.items()}
    except (TypeError, ValueError) as exc:
        return jsonify({'ok': False, 'error': f'dy_overrides-Wert ungültig: {exc}'}), 400
    if dy:
        overrides['dy'] = dy

    font_profile = data.get('font_profile')
    if font_profile and font_profile != 'auto':
        overrides['font_profile'] = font_profile

    # Schriftgrößen-Overrides
    raw_fs = data.get('font_sizes')
    if isinstance(raw_fs, dict) and raw_fs:
        try:
            fs = {k: int(v) for k, v in raw_fs.items()
                  if k in ('name', 'type_line', 'oracle', 'flavor') and 4 <= int(v) <= 120}
        except (TypeError, ValueError) as exc:
            return jsonify({'ok': False, 'error': f'font_sizes-Wert ungültig: {exc}'}), 400
        if fs:
            overrides['font_sizes'] = fs

    # Text-Overrides
    raw_txt = data.get('text_overrides')
    if isinstance(raw_txt, dict) and raw_txt:
        txt = {k: str(v) for k, v in raw_txt.items()
               if k in ('name', 'type_line', 'oracle', 'flavor')}
        if txt:
            overrides['text_overrides'] = txt

    # ── Rendern ────────────────────────────────────────────────────────────────
    card_data     = pt['card_meta'][card_id]
    original_path = pt['card_paths'][card_id]

    try:
        out_path = renderer.process_card(
            card_data, original_path,
            overrides=overrides or None,
            force=True,
        )
        if out_path:
            filename = os.path.basename(out_path)
            pt['id_to_path_map'][card_id] = out_path
            return jsonify({'ok': True, 'filename': filename})
        return jsonify({'ok': False, 'error': 'Render fehlgeschlagen'})
    except Exception as e:
        logger.exception("[preview_rerender] Fehler bei Karte %s", card_id)
        return jsonify({'ok': False, 'error': str(e)})

@app.route('/preview/confirm', methods=['POST'])
def preview_confirm():
    """Erstellt PDFs mit den final gerenderten Bildern."""
    data       = request.get_json()
    preview_id = data.get('preview_id')
    pt         = preview_tasks.get(preview_id)
    if not pt:
        return jsonify({'ok': False, 'error': 'Preview nicht gefunden'})

    id_to_path_map   = pt['id_to_path_map']
    id_to_metadata_map = pt['card_meta']
    final_card_ids   = pt['final_card_ids']
    filename_base    = pt['filename_base']
    dfc_handling     = pt['dfc_handling']
    card_back_path   = pt['card_back_path']
    failed_cards     = pt.get('failed_cards', [])

    blank_image_path = create_blank_image() if dfc_handling == 'dfc_only_backside' else None
    needs_back_pdf   = (card_back_path is not None) or (dfc_handling in ['true_backside', 'dfc_only_backside'])

    all_front, all_back = [], []
    for card_id in final_card_ids:
        path_or_paths = id_to_path_map.get(card_id)
        metadata      = id_to_metadata_map.get(card_id)
        if not path_or_paths or not metadata:
            continue

        # Rendered-PNG bevorzugen wenn vorhanden
        def _best_path(p):
            if isinstance(p, str):
                rp = p.replace('.jpg','_rendered.png').replace('.jpeg','_rendered.png')
                return rp if os.path.exists(rp) else p
            return p

        if isinstance(path_or_paths, list):
            path_or_paths = [_best_path(p) for p in path_or_paths]
        else:
            path_or_paths = _best_path(path_or_paths)

        is_dfc = 'card_faces' in metadata and isinstance(path_or_paths, list) and len(path_or_paths) > 1
        front_paths = []
        if is_dfc and dfc_handling in ['true_backside', 'dfc_only_backside']:
            front_paths.append(path_or_paths[0] if isinstance(path_or_paths, list) else path_or_paths)
        else:
            front_paths.extend(path_or_paths if isinstance(path_or_paths, list) else [path_or_paths])
        all_front.extend(front_paths)
        if needs_back_pdf:
            if is_dfc and dfc_handling in ['true_backside', 'dfc_only_backside']:
                all_back.append(path_or_paths[1])
            else:
                back = card_back_path or blank_image_path
                if back:
                    all_back.extend([back] * len(front_paths))

    pdf_front = pdf_back = pdf_duplex = None
    fn_front  = f"{filename_base}_front.pdf"
    fn_back   = f"{filename_base}_back.pdf"
    fn_duplex = f"{filename_base}_duplex.pdf"

    if all_front:
        pdf_front = os.path.join(OUTPUT_DIR, fn_front)
        create_pdf_from_images(all_front, pdf_front, mirror_layout=False)
    if all_back and len(all_front) == len(all_back):
        pdf_back   = os.path.join(OUTPUT_DIR, fn_back)
        pdf_duplex = os.path.join(OUTPUT_DIR, fn_duplex)
        create_pdf_from_images(all_back, pdf_back, mirror_layout=True)
        create_duplex_pdf(all_front, all_back, pdf_duplex)

    # Task aufräumen
    preview_tasks.pop(preview_id, None)

    # Redirect-URL für Result-Template
    result_params = {
        'pdf_front':  fn_front  if pdf_front  else None,
        'pdf_back':   fn_back   if pdf_back   else None,
        'pdf_duplex': fn_duplex if pdf_duplex  else None,
        'errors':     list(set(failed_cards)),
    }
    result_id = uuid.uuid4().hex
    tasks[result_id] = {'status': 'complete', 'result_params': result_params}
    return jsonify({'ok': True, 'redirect': url_for('show_result', result_id=result_id)})

@app.route('/result/<result_id>')
def show_result(result_id):
    t = tasks.get(result_id, {})
    p = t.get('result_params', {})
    return render_template_string(
        RESULT_TEMPLATE,
        pdf_front_path  = os.path.join(OUTPUT_DIR, p['pdf_front'])  if p.get('pdf_front')  else None,
        pdf_back_path   = os.path.join(OUTPUT_DIR, p['pdf_back'])   if p.get('pdf_back')   else None,
        pdf_duplex_path = os.path.join(OUTPUT_DIR, p['pdf_duplex']) if p.get('pdf_duplex') else None,
        filename_front  = p.get('pdf_front'),
        filename_back   = p.get('pdf_back'),
        filename_duplex = p.get('pdf_duplex'),
        errors          = p.get('errors', []),
    )

CALIBRATION_FILE = os.path.join(BASE_DIR, 'zone_calibration.json')

CALIBRATE_TEMPLATE = r"""<!doctype html>
<html lang="de"><head><meta charset="utf-8"><title>Zonen-Kalibrierung</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#1a1a1a;color:#eee;font-family:sans-serif;display:flex;flex-direction:column;align-items:center;padding:16px;gap:10px}
h1{font-size:1.2em;color:#7cf}
#hint{font-size:.82em;color:#aaa;max-width:780px;text-align:center;line-height:1.5}
#wrap{position:relative;display:inline-block;border:2px solid #555;user-select:none}
#wrap img{display:block;max-height:78vh;width:auto;pointer-events:none}
#canvas-overlay{position:absolute;top:0;left:0;width:100%;height:100%;cursor:crosshair}
#controls{display:flex;gap:8px;flex-wrap:wrap;justify-content:center}
.zb{padding:7px 14px;border:2px solid;border-radius:5px;background:#222;cursor:pointer;font-size:.88em}
.zb.on{font-weight:bold;filter:brightness(1.5);background:#333}
.zb[data-z="name"]          {border-color:#e33;color:#f99}
.zb[data-z="type_line"]     {border-color:#33e;color:#99f}
.zb[data-z="oracle_box"]    {border-color:#3a3;color:#9f9}
.zb[data-z="pt_box"]        {border-color:#e80;color:#fc8}
.zb[data-z="watermark_bump"]{border-color:#b3f;color:#dbf}
#savebtn{padding:9px 26px;background:#2a7;border:none;border-radius:5px;color:#fff;font-size:.95em;cursor:pointer;font-weight:bold}
#savebtn:hover{background:#3b8}
#status{font-size:.88em;min-height:1.3em}
#coords{font-size:.72em;color:#777;font-family:monospace;min-height:1.2em}
select{padding:5px 8px;background:#333;color:#eee;border:1px solid #555;border-radius:4px}
.sep{width:2px;background:#444;align-self:stretch;border-radius:2px}
</style></head><body>
<h1>Zonen-Kalibrierung</h1>
<div id="hint">Zone wählen → Bereich auf der Karte ziehen → Speichern.<br>
<b>Rechteck-Zonen:</b> Name, Typzeile, Oracle-Box, P/T &nbsp;|&nbsp;
<b>Ellipse:</b> Wassermarken-Ausbuchtung (nach innen, untere Mitte Oracle-Box)</div>

<div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap">
  <label style="font-size:.85em">Gespeicherte Karte:</label>
  <select id="csel" onchange="loadCard(this.value)">
    {% if card_images %}
      {% for f in card_images %}<option value="{{f}}">{{f}}</option>{% endfor %}
    {% else %}
      <option value="">– noch keine Karten vorhanden –</option>
    {% endif %}
  </select>
  <span style="color:#555;font-size:.8em">oder</span>
  <label style="padding:6px 12px;background:#335;border:1px solid #55a;border-radius:5px;cursor:pointer;font-size:.85em">
    📁 Bild direkt hochladen
    <input type="file" id="localfile" accept="image/*" style="display:none" onchange="loadLocal(this)">
  </label>
</div>

<div id="controls">
  <button class="zb on" data-z="name"       data-shape="rect" onclick="setZ(this)">🔴 Name</button>
  <button class="zb"    data-z="type_line"  data-shape="rect" onclick="setZ(this)">🔵 Typzeile</button>
  <button class="zb"    data-z="oracle_box" data-shape="rect" onclick="setZ(this)">🟢 Oracle-Box</button>
  <button class="zb"    data-z="pt_box"     data-shape="rect" onclick="setZ(this)">🟠 P/T (opt.)</button>
  <div class="sep"></div>
  <button class="zb"    data-z="watermark_bump" data-shape="ellipse" onclick="setZ(this)">🟣 Wassermarke (Ellipse)</button>
</div>

<div style="display:flex;gap:20px;flex-wrap:wrap;justify-content:center;font-size:.83em;color:#bbb;align-items:flex-start">
  <div style="display:flex;flex-direction:column;gap:6px;align-items:flex-start">
    <label style="display:flex;align-items:center;gap:8px">
      🔴 Name-Größe (pt):
      <input type="number" id="fs_name" min="10" max="60" value="36" step="1" style="width:56px;padding:3px 6px;background:#333;color:#eee;border:1px solid #555;border-radius:4px;font-family:monospace"
             oninput="updateFs('name',this.value); renderPreview()">
    </label>
    <label style="display:flex;align-items:center;gap:8px">
      🔴 Name X-Offset (px):
      <input type="number" id="ox_name" min="-40" max="40" value="16" step="1" style="width:56px;padding:3px 6px;background:#333;color:#eee;border:1px solid #555;border-radius:4px;font-family:monospace"
             oninput="updateOx('name',this.value); renderPreview()">
    </label>
    <label style="display:flex;align-items:center;gap:8px">
      🔵 Typ-Größe (pt):
      <input type="number" id="fs_type" min="8" max="48" value="26" step="1" style="width:56px;padding:3px 6px;background:#333;color:#eee;border:1px solid #555;border-radius:4px;font-family:monospace"
             oninput="updateFs('type',this.value); renderPreview()">
    </label>
    <label style="display:flex;align-items:center;gap:8px">
      🔵 Typ X-Offset (px):
      <input type="number" id="ox_type" min="-40" max="40" value="16" step="1" style="width:56px;padding:3px 6px;background:#333;color:#eee;border:1px solid #555;border-radius:4px;font-family:monospace"
             oninput="updateOx('type',this.value); renderPreview()">
    </label>
    <label style="display:flex;align-items:center;gap:8px;color:#aaa">
      Beispieltext Name:
      <input type="text" id="preview_text_name" value="Prozession der Gesalbten" style="width:200px;padding:3px 6px;background:#333;color:#eee;border:1px solid #555;border-radius:4px"
             oninput="renderPreview()">
    </label>
    <label style="display:flex;align-items:center;gap:8px;color:#aaa">
      Beispieltext Typ:
      <input type="text" id="preview_text_type" value="Verzauberung" style="width:200px;padding:3px 6px;background:#333;color:#eee;border:1px solid #555;border-radius:4px"
             oninput="renderPreview()">
    </label>
  </div>
  <canvas id="preview_canvas" width="460" height="90"
          style="border:1px solid #444;border-radius:4px;background:#e8e2d0"></canvas>
</div>

<div id="wrap">
  <img id="cimg" src="" alt="">
  <canvas id="canvas-overlay"></canvas>
</div>

<div id="coords">–</div>
<div style="display:flex;gap:10px;align-items:center">
  <button id="savebtn" onclick="save()">💾 Speichern &amp; Cache leeren</button>
  <button onclick="reset()" style="padding:7px 14px;background:#422;border:none;border-radius:5px;color:#f88;cursor:pointer;font-size:.88em">↺ Zurücksetzen</button>
</div>
<div id="status"></div>

<script>
const COLS  = {name:'#e33',type_line:'#33e',oracle_box:'#3a3',pt_box:'#e80',watermark_bump:'#b3f'};
const LBLS  = {name:'Name',type_line:'Typzeile',oracle_box:'Oracle',pt_box:'P/T',watermark_bump:'Wassermarke'};
const SHAPE = {name:'rect',type_line:'rect',oracle_box:'rect',pt_box:'rect',watermark_bump:'ellipse'};

let curZ = 'name', curShape = 'rect', zones = {}, drawing = false, sx = 0, sy = 0;
let fontSizes = {name: 36, type: 26};
let xOffsets  = {name: 16, type: 16};

const prev = {{ saved | tojson }};
if (prev) {
  zones = prev;
  if (prev._font_sizes) {
    fontSizes = Object.assign(fontSizes, prev._font_sizes);
    if (prev._font_sizes.name) document.getElementById('fs_name').value = prev._font_sizes.name;
    if (prev._font_sizes.type) document.getElementById('fs_type').value = prev._font_sizes.type;
  }
  if (prev._x_offsets) {
    xOffsets = Object.assign(xOffsets, prev._x_offsets);
    if (prev._x_offsets.name != null) document.getElementById('ox_name').value = prev._x_offsets.name;
    if (prev._x_offsets.type != null) document.getElementById('ox_type').value = prev._x_offsets.type;
  }
}

window.onload = () => {
  const s = document.getElementById('csel');
  if (s && s.value) loadCard(s.value);
  renderPreview();
};

function updateFs(key, val) { fontSizes[key] = parseInt(val) || fontSizes[key]; }
function updateOx(key, val) { xOffsets[key]  = parseInt(val); }

function renderPreview() {
  const cvp = document.getElementById('preview_canvas');
  const ctx = cvp.getContext('2d');
  ctx.clearRect(0, 0, cvp.width, cvp.height);
  ctx.fillStyle = '#e8e2d0';
  ctx.fillRect(0, 0, cvp.width, cvp.height);

  const nameText = document.getElementById('preview_text_name').value || 'Kartenname';
  const typeText = document.getElementById('preview_text_type').value || 'Typzeile';
  const nameSize = parseInt(document.getElementById('fs_name').value) || 36;
  const typeSize = parseInt(document.getElementById('fs_type').value) || 26;
  const nameOx   = parseInt(document.getElementById('ox_name').value) || 0;
  const typeOx   = parseInt(document.getElementById('ox_type').value) || 0;

  // Linksrand-Hilfslinie
  ctx.strokeStyle = '#aaa'; ctx.lineWidth = 1; ctx.setLineDash([3,3]);
  ctx.beginPath(); ctx.moveTo(nameOx, 0); ctx.lineTo(nameOx, cvp.height); ctx.stroke();
  ctx.setLineDash([]);

  ctx.fillStyle = '#111';
  ctx.font = `bold ${nameSize}px serif`;
  ctx.fillText(nameText, nameOx, nameSize + 2);

  ctx.strokeStyle = '#999'; ctx.lineWidth = 1;
  ctx.beginPath(); ctx.moveTo(0, nameSize + 12); ctx.lineTo(cvp.width, nameSize + 12); ctx.stroke();

  ctx.font = `bold ${typeSize}px serif`;
  ctx.fillText(typeText, typeOx, nameSize + 12 + typeSize + 4);

  ctx.font = '10px monospace'; ctx.fillStyle = '#888';
  ctx.fillText(`Name: ${nameSize}pt  ox:${nameOx}px`, cvp.width - 130, 12);
  ctx.fillText(`Typ:  ${typeSize}pt  ox:${typeOx}px`,  cvp.width - 130, 24);
}

const cvs = document.getElementById('canvas-overlay');

function loadCard(fn) {
  if (!fn) return;
  const img = document.getElementById('cimg');
  img.onload = () => {
    cvs.width  = img.naturalWidth;
    cvs.height = img.naturalHeight;
    syncCanvasSize();
    drawAll();
  };
  img.src = '/card-image/' + fn;
}

function loadLocal(input) {
  if (!input.files || !input.files[0]) return;
  const url = URL.createObjectURL(input.files[0]);
  const img  = document.getElementById('cimg');
  img.onload = () => {
    cvs.width  = img.naturalWidth;
    cvs.height = img.naturalHeight;
    syncCanvasSize();
    drawAll();
    URL.revokeObjectURL(url);
  };
  img.src = url;
}

function syncCanvasSize() {
  const img = document.getElementById('cimg');
  cvs.style.width  = img.offsetWidth  + 'px';
  cvs.style.height = img.offsetHeight + 'px';
}

window.onresize = () => { syncCanvasSize(); drawAll(); };

function setZ(btn) {
  document.querySelectorAll('.zb').forEach(b=>b.classList.remove('on'));
  btn.classList.add('on');
  curZ     = btn.dataset.z;
  curShape = btn.dataset.shape;
  cvs.style.cursor = curShape === 'ellipse' ? 'cell' : 'crosshair';
}

function getXY(e) {
  const r = cvs.getBoundingClientRect();
  const src = e.touches ? e.touches[0] : e;
  return [(src.clientX-r.left)/r.width, (src.clientY-r.top)/r.height];
}

// ── Drag-to-move: prüft ob Punkt (rx,ry) in einer Zone liegt ────────────────
function hitZone(rx, ry) {
  for (const [name, zdata] of Object.entries(zones)) {
    if (!zdata) continue;
    const z   = zdata.coords || zdata;
    const shp = zdata.shape  || 'rect';
    if (shp === 'ellipse') {
      const cx=(z[0]+z[2])/2, cy=(z[1]+z[3])/2;
      const rx2=(z[2]-z[0])/2, ry2=(z[3]-z[1])/2;
      if (rx2>0 && ry2>0 && ((rx-cx)**2/rx2**2 + (ry-cy)**2/ry2**2) <= 1)
        return name;
    } else {
      if (rx>=z[0] && rx<=z[2] && ry>=z[1] && ry<=z[3]) return name;
    }
  }
  return null;
}

let dragZone = null, dragOffX = 0, dragOffY = 0;

['mousedown','touchstart'].forEach(ev => cvs.addEventListener(ev, e => {
  if(e.cancelable) e.preventDefault();
  const [rx, ry] = getXY(e);
  const hit = hitZone(rx, ry);
  if (hit) {
    // Bestehende Zone verschieben
    dragZone = hit;
    const z = zones[hit].coords || zones[hit];
    dragOffX = rx - z[0];
    dragOffY = ry - z[1];
    cvs.style.cursor = 'move';
    drawing = false;
  } else {
    // Neue Zone zeichnen
    dragZone = null;
    sx = rx; sy = ry;
    drawing = true;
  }
}, {passive:false}));

['mousemove','touchmove'].forEach(ev => cvs.addEventListener(ev, e => {
  if(e.cancelable) e.preventDefault();
  const [rx, ry] = getXY(e);

  if (dragZone) {
    // Zone verschieben
    const zdata = zones[dragZone];
    const z     = zdata.coords || zdata;
    const w = z[2]-z[0], h = z[3]-z[1];
    const nx1 = Math.max(0, Math.min(1-w, rx - dragOffX));
    const ny1 = Math.max(0, Math.min(1-h, ry - dragOffY));
    const newCoords = [nx1, ny1, nx1+w, ny1+h];
    if (typeof zdata === 'object' && zdata.coords) {
      zdata.coords = newCoords;
    } else {
      zones[dragZone] = newCoords;
    }
    document.getElementById('coords').textContent =
      dragZone+': x1='+nx1.toFixed(3)+' y1='+ny1.toFixed(3)+
      ' x2='+(nx1+w).toFixed(3)+' y2='+(ny1+h).toFixed(3);
    drawAll();
    return;
  }

  if (!drawing) return;
  const z = [Math.min(sx,rx),Math.min(sy,ry),Math.max(sx,rx),Math.max(sy,ry)];
  drawAll(z);
  document.getElementById('coords').textContent =
    curZ+' ('+curShape+'): x1='+z[0].toFixed(3)+' y1='+z[1].toFixed(3)+
    ' x2='+z[2].toFixed(3)+' y2='+z[3].toFixed(3);
}, {passive:false}));

['mouseup','touchend'].forEach(ev => cvs.addEventListener(ev, e => {
  if (dragZone) {
    dragZone = null;
    cvs.style.cursor = 'crosshair';
    return;
  }
  if(!drawing) return;
  drawing = false;
  const src = e.changedTouches ? e.changedTouches[0] : e;
  const r = cvs.getBoundingClientRect();
  const ex=(src.clientX-r.left)/r.width, ey=(src.clientY-r.top)/r.height;
  const z = [Math.min(sx,ex),Math.min(sy,ey),Math.max(sx,ex),Math.max(sy,ey)];
  if((z[2]-z[0])<0.005 || (z[3]-z[1])<0.003) return;
  zones[curZ] = {shape: curShape, coords: z};
  drawAll();
}));

// Cursor-Feedback beim Hovern
cvs.addEventListener('mousemove', e => {
  if (drawing || dragZone) return;
  const [rx,ry] = getXY(e);
  cvs.style.cursor = hitZone(rx, ry) ? 'move' : (curShape==='ellipse' ? 'cell' : 'crosshair');
});

function drawZone(ctx, W, H, name, zdata, preview) {
  const col = COLS[name] || '#fff';
  const z   = zdata.coords || zdata;
  const shp = zdata.shape  || 'rect';
  const x1=z[0]*W, y1=z[1]*H, w=(z[2]-z[0])*W, h=(z[3]-z[1])*H;
  const cx=x1+w/2, cy=y1+h/2;

  ctx.strokeStyle = preview ? col+'cc' : col;
  ctx.lineWidth   = preview ? 2 : 3;
  if(preview) ctx.setLineDash([6,3]);
  ctx.fillStyle   = col + (preview ? '33' : '44');

  if(shp === 'ellipse') {
    ctx.beginPath();
    ctx.ellipse(cx, cy, w/2, h/2, 0, 0, Math.PI*2);
    ctx.fill(); ctx.stroke();
  } else {
    ctx.beginPath();
    ctx.rect(x1, y1, w, h);
    ctx.fill(); ctx.stroke();
  }
  ctx.setLineDash([]);

  if(!preview) {
    ctx.fillStyle = col;
    ctx.font = 'bold 13px sans-serif';
    // P/T: Hinweis dass es optional ist
    const lbl = name === 'pt_box' ? 'P/T (nur Kreaturen)' : (LBLS[name]||name);
    ctx.fillText(lbl, x1+5, y1+14);
  }
}

function drawAll(previewCoords) {
  const img = document.getElementById('cimg');
  const ctx = cvs.getContext('2d');
  const W = cvs.width, H = cvs.height;
  ctx.clearRect(0,0,W,H);
  for(const [name, zdata] of Object.entries(zones)) {
    if(!zdata) continue;
    drawZone(ctx, W, H, name, zdata, false);
  }
  if(previewCoords) {
    drawZone(ctx, W, H, curZ, {shape: curShape, coords: previewCoords}, true);
  }
}

function reset() {
  zones = {};
  cvs.getContext('2d').clearRect(0,0,cvs.width,cvs.height);
  document.getElementById('coords').textContent = '–';
  document.getElementById('status').textContent = '';
}

async function save() {
  const missing = ['name','type_line','oracle_box'].filter(z=>!zones[z]);
  if(missing.length) {
    const st = document.getElementById('status');
    st.textContent = '⚠ Bitte noch setzen: ' + missing.join(', ');
    st.style.color = '#f88'; return;
  }
  const payload = Object.assign({}, zones, {_font_sizes: fontSizes, _x_offsets: xOffsets});
  const r = await fetch('/calibrate/save', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify(payload)
  });
  const d = await r.json();
  const st = document.getElementById('status');
  st.textContent = d.ok ? `✓ Gespeichert! ${d.cleared} Rendered-Cache(s) geleert.` : '✗ '+d.error;
  st.style.color = d.ok ? '#8f8' : '#f88';
}
</script></body></html>"""


@app.route('/calibrate')
def calibrate():
    card_images = sorted([
        f for f in os.listdir(CARDS_DIR)
        if f.lower().endswith(('.jpg','.jpeg','.png'))
        and 'rendered' not in f and 'DEBUG' not in f and 'ERASE' not in f
    ])
    saved = {}
    if os.path.exists(CALIBRATION_FILE):
        with open(CALIBRATION_FILE) as f:
            saved = json.load(f)
    return render_template_string(CALIBRATE_TEMPLATE, card_images=card_images, saved=saved)


@app.route('/calibrate/save', methods=['POST'])
def calibrate_save():
    data = request.get_json()
    if not data:
        return jsonify({'ok': False, 'error': 'Keine Daten'})
    missing = [k for k in ['name','type_line','oracle_box'] if k not in data]
    if missing:
        return jsonify({'ok': False, 'error': f'Fehlend: {missing}'})
    with open(CALIBRATION_FILE, 'w') as f:
        json.dump(data, f, indent=2)
    cleared = 0
    for fn in os.listdir(CARDS_DIR):
        if fn.endswith('_rendered.png'):
            try: os.remove(os.path.join(CARDS_DIR, fn)); cleared += 1
            except: pass
    global renderer
    if renderer is not None:
        try:
            renderer.reload_zones(CALIBRATION_FILE)
            print(f"[Renderer] Zonen neu geladen, Font-Overrides: {renderer._font_size_overrides}")
        except Exception as e:
            print(f"[Renderer] reload_zones Fehler: {e}")
            return jsonify({'ok': False, 'error': str(e)})
    return jsonify({'ok': True, 'cleared': cleared})


# ── Startup-Initialisierung (läuft auch unter Gunicorn) ──────────────────────
def _startup():
    for dir_path in [CARDS_DIR, OUTPUT_DIR, CARD_BACKS_DIR, UPLOADS_DIR]:
        if not os.path.exists(dir_path):
            os.makedirs(dir_path)

    global renderer
    if ENABLE_RENDERING:
        try:
            renderer = CardRenderer(
                fonts_dir=os.path.join(BASE_DIR, "fonts"),
                symbol_cache_dir=os.path.join(BASE_DIR, "symbol_cache"),
                calibration_file=CALIBRATION_FILE,
            )
            print(f"[Renderer] Initialisiert.")
            print(f"[Renderer] Fonts: {os.path.join(BASE_DIR, 'fonts')}")
            print(f"[Renderer] Kalibrierung: {CALIBRATION_FILE}")
            print(f"[Renderer] Font-Overrides: {renderer._font_size_overrides}")
        except Exception as exc:
            print(f"[Renderer] WARNUNG: Konnte nicht initialisiert werden: {exc}")
            renderer = None

_startup()

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    debug = os.environ.get('FLASK_DEBUG', 'false').lower() == 'true'
    app.run(host='0.0.0.0', port=port, debug=debug)
