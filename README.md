# MTG Proxy Generator – Render.com Deployment

## ⚠️ Wichtig: Einschränkung der Free-Tier

Render.com's Free-Plan nutzt **ephemeres Dateisystem** – d.h. alle zur Laufzeit
heruntergeladenen Karten-Images und generierten PDFs gehen nach jedem Neustart/
Schlafzustand verloren. Das ist normal und kein Bug: Die Karten werden einfach
beim nächsten Klick erneut von Scryfall geladen.

**Was im Git-Repo bleibt (persistent):**
- `fonts/` – alle Schriftarten
- `card_backs/` – deine Kartenrücken
- `zone_calibration.json` – deine Kalibrierung
- `symbol_cache/` – Mana-Symbol-Cache (optional)

---

## 🚀 Deployment auf Render.com

### Schritt 1 – GitHub-Repo anlegen

```bash
git init
git add .
git commit -m "Initial commit: MTG Proxy Generator"
```

Neues Repo auf github.com erstellen (z.B. `mtg-proxy-generator`), dann:

```bash
git remote add origin https://github.com/DEIN-USERNAME/mtg-proxy-generator.git
git branch -M main
git push -u origin main
```

### Schritt 2 – Render.com Web Service einrichten

1. [render.com](https://render.com) → **New → Web Service**
2. GitHub-Repo verbinden
3. Einstellungen:
   - **Name:** `mtg-proxy-generator` (oder nach Wunsch)
   - **Region:** Frankfurt (EU)
   - **Branch:** `main`
   - **Runtime:** Python 3
   - **Build Command:** `pip install -r requirements.txt`
   - **Start Command:** `gunicorn app:app --workers 1 --threads 4 --timeout 120 --bind 0.0.0.0:$PORT`
   - **Plan:** Free

4. **Environment Variables** hinzufügen:
   - `SECRET_KEY` → beliebiger langer zufälliger String (z.B. von [randomkeygen.com](https://randomkeygen.com))

5. → **Create Web Service**

Der erste Build dauert 2–5 Minuten. Danach ist die App unter
`https://mtg-proxy-generator.onrender.com` erreichbar.

---

## 🔄 Updates deployen

```bash
git add .
git commit -m "Meine Änderungen"
git push
```

Render.com deployed automatisch nach jedem Push.

---

## 💻 Lokale Entwicklung

```bash
pip install -r requirements.txt
python app.py
```

App läuft dann auf http://localhost:5000

---

## 📁 Projektstruktur

```
mtg-proxy-generator/
├── app.py                  # Flask-App (Backend + Frontend in einem)
├── card_renderer.py        # Text-Overlay-Renderer
├── font_setup.py           # Font-Hilfsskript
├── zone_calibration.json   # Gespeicherte Zonen-Kalibrierung
├── requirements.txt        # Python-Abhängigkeiten
├── Procfile                # Gunicorn-Startbefehl für Render
├── render.yaml             # Optionale Render-Konfiguration
├── fonts/                  # MTG-Schriftarten (im Git eingecheckt)
├── card_backs/             # Kartenrücken-Bilder (im Git)
├── symbol_cache/           # Mana-Symbol-Cache
├── card_images/            # Zur Laufzeit befüllt (ephemer!)
├── output_pdfs/            # Zur Laufzeit befüllt (ephemer!)
└── user_uploads/           # Zur Laufzeit befüllt (ephemer!)
```

---

## 🛠️ Troubleshooting

**App startet nicht / Build schlägt fehl:**
- Logs unter Render → dein Service → **Logs** prüfen
- Häufigste Ursache: `opencv-python` statt `opencv-python-headless` → bereits in requirements.txt korrigiert

**Renderer meldet "Konnte nicht initialisiert werden":**
- Kalibrierungs-Seite aufrufen: `/calibrate`
- Zonen neu setzen und speichern

**Free-Tier schläft nach 15 Minuten Inaktivität ein:**
- Erster Request nach dem Aufwachen dauert ~30 Sekunden
- Kein Datenverlust (Fonts/Calibration bleiben), nur Downloads werden wiederholt

**Kartenbilder-Cache leeren:**
- `/calibrate` → "Speichern & Cache leeren"
