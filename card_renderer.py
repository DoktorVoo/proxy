"""
card_renderer.py
================
Lädt die englische High-Res-Version einer MTG-Karte von Scryfall,
löscht alle Textbereiche und rendert den deutschen Text (aus den
Scryfall printed_* Feldern) mit korrekten Mana-Symbolen neu.

Unterstützte Layouts: normal, leveler, class, token, emblem
Nicht unterstützt (Fallback auf None): split, flip, transform,
    meld, saga, adventure, planeswalker, battle, modal_dfc
"""

import io
import re
import json
import time
import logging
import requests
import numpy as np
import cv2
from pathlib import Path
from PIL import Image, ImageDraw, ImageFilter, ImageFont

logger = logging.getLogger(__name__)

# Mana-Symbole werden als PIL-Kreise gerendert (keine Cairo/GTK-Abhaengigkeit).
_HAS_SVGLIB = False
_HAS_CAIROSVG = False


# ══════════════════════════════════════════════════════════════════════════════
# Zonen-Definitionen (relative Koordinaten für 745 × 1040 Scryfall-PNG)
# ══════════════════════════════════════════════════════════════════════════════

# Modern Frame (M15 / 2015 bis heute) – gilt für ~95 % aller Karten
# Kalibriert via Pixel-Scan auf 672×936 JPG (Anointed Procession AKH).
# Relative Y-Werte gelten identisch für das 745×1040 Scryfall-PNG.
#
#  Gemessene Pixel-Grenzen (672×936):
#    Name-Bar:   y  40.. 58  →  0.043..0.062
#    Type-Bar:   y 520..580  →  0.556..0.620
#    Oracle-Box: y 584..800  →  0.624..0.855
ZONES_MODERN = {
    "name":       (0.045, 0.038, 0.928, 0.066),
    "type_line":  (0.045, 0.553, 0.928, 0.623),
    "oracle_box": (0.045, 0.623, 0.958, 0.858),
    "pt_box":     (0.828, 0.858, 0.958, 0.888),
}

# Legacy Frame (8th Edition bis 2014)
ZONES_LEGACY = {
    "name":       (0.045, 0.036, 0.928, 0.068),
    "type_line":  (0.045, 0.551, 0.928, 0.621),
    "oracle_box": (0.045, 0.621, 0.958, 0.860),
    "pt_box":     (0.825, 0.860, 0.958, 0.890),
}

# Älteste Frames (Alpha/Beta bis 7th Edition)
ZONES_OLD = {
    "name":       (0.043, 0.034, 0.930, 0.068),
    "type_line":  (0.043, 0.549, 0.930, 0.619),
    "oracle_box": (0.043, 0.619, 0.958, 0.862),
    "pt_box":     (0.822, 0.862, 0.958, 0.892),
}

# Layouts, die dieser Renderer unterstützt
SUPPORTED_LAYOUTS = {"normal", "leveler", "class", "token", "emblem"}

# Farben für PIL-Symbol-Fallback {Mana-Code: (Hintergrund, Textfarbe)}
MANA_FALLBACK_COLORS: dict[str, tuple[str, str]] = {
    "W": ("#F8F6D8", "#000"), "U": ("#C1D7E9", "#000"),
    "B": ("#B4A9C3", "#FFF"), "R": ("#E9A184", "#000"),
    "G": ("#A3C095", "#000"), "C": ("#CFC5C0", "#000"),
    "S": ("#CDE6F5", "#000"), "X": ("#DDD",    "#000"),
    "T": ("#E8C84A", "#000"), "Q": ("#E8C84A", "#000"),
    "E": ("#B0A0FF", "#000"),
}
for _n in range(21):
    MANA_FALLBACK_COLORS[str(_n)] = ("#CCC", "#000")


# ══════════════════════════════════════════════════════════════════════════════
# Font-Profile
# Jedes Profil definiert die Dateinamen für name/type/oracle/oracle_italic/pt.
# Falls eine Datei fehlt, fällt _font() auf das Modern-Profil zurück.
# ══════════════════════════════════════════════════════════════════════════════

FONT_PROFILES: dict[str, dict[str, str]] = {
    "modern": {
        # M15-Frame (2015+) und 8th-Edition-Frame (2003–2014)
        "name":          "beleren-bold_P1.01.ttf",
        "type":          "MatrixBold.ttf",
        "oracle":        "mplantin.ttf",
        "oracle_italic": "mplantinit.ttf",
        "pt":            "beleren-bold_P1.01.ttf",
    },
    "old": {
        # Old-Frame (Alpha bis 7th Edition, vor 2003)
        # Name/Typ nutzen hier bewusst Medieval-Font,
        # Oracle nutzt dieselben Fonts wie modern.
        "name":          "MagicMedieval.ttf",
        "type":          "MagicMedieval.ttf",
        "oracle":        "mplantin.ttf",
        "oracle_italic": "mplantinit.ttf",
        "pt":            "MagicMedieval.ttf",
    },
    "medieval": {
        # Sehr alte / Fan-Art Karten mit MagicMedieval
        "name":          "MagicMedieval.ttf",
        "type":          "matrixb.ttf",
        "oracle":        "relay-medium.ttf",
        "oracle_italic": "relay-medium.ttf",
        "pt":            "MagicMedieval.ttf",
    },
}

# Gängige Alternativ-Dateinamen je Rolle (Community/Legacy-Namen).
FONT_FILE_ALIASES: dict[str, list[str]] = {
    "name": [
        "Beleren2016-Bold.ttf",
        "Beleren-Bold.ttf",
        "Beleren.ttf",
    ],
    "type": [
        "MatrixBoldSmallCaps.ttf",
        "MatrixBold.ttf",
        "matrixb.ttf",
    ],
    "oracle": [
        "MPlantin.ttf",
        "mplantin.ttf",
    ],
    "oracle_italic": [
        "MPlantin-Italic.ttf",
        "MPlantinItalic.ttf",
        "mplantinit.ttf",
    ],
    "pt": [
        "Beleren2016-Bold.ttf",
        "Beleren-Bold.ttf",
        "Beleren.ttf",
    ],
}

# Automatische Profil-Auswahl nach Erscheinungsjahr
def _auto_font_profile(released_at: str) -> str:
    try:
        year = int(released_at[:4])
    except (ValueError, TypeError):
        return "modern"
    if year < 2003:
        return "old"
    return "modern"



# ══════════════════════════════════════════════════════════════════════════════
class CardRenderer:
# ══════════════════════════════════════════════════════════════════════════════

    def __init__(
        self,
        fonts_dir: str = "fonts",
        symbol_cache_dir: str = "symbol_cache",
        calibration_file: str = "zone_calibration.json",
    ):
        self.fonts_dir = Path(fonts_dir)
        self.symbol_cache_dir = Path(symbol_cache_dir)
        self.calibration_file = Path(calibration_file)
        self.symbol_cache_dir.mkdir(exist_ok=True)
        self._font_paths: dict = {}           # {profile_key: abs_path}
        self._sym_cache: dict[str, Image.Image] = {}
        self._calibrated_zones: dict | None = None
        self._font_size_overrides: dict = {}
        self._x_offsets: dict = {"name": 16, "type": 16}
        self._blur_values: dict = {}  # Blur-Radius pro Zone (px)
        self._load_fonts()
        self._try_load_calibration()

    def _try_load_calibration(self):
        """Lädt gespeicherte Zonen-Kalibrierung falls vorhanden."""
        if not self.calibration_file.exists():
            return
        try:
            with open(self.calibration_file, encoding="utf-8") as f:
                data = json.load(f)
            if not {"name", "type_line", "oracle_box"}.issubset(data.keys()):
                logger.warning("[Renderer] Kalibrierungsdatei unvollständig.")
                return
            self._calibrated_zones = dict(data)
            if "_font_sizes" in data:
                fs = data["_font_sizes"]
                if "name" in fs:
                    self._font_size_overrides["name"] = int(fs["name"])
                    self._font_size_overrides["pt"]   = int(fs["name"])
                if "type" in fs:
                    self._font_size_overrides["type"] = int(fs["type"])
                logger.info("[Renderer] Schriftgrößen übernommen: Name=%s Type=%s",
                            self._font_size_overrides.get("name"),
                            self._font_size_overrides.get("type"))
            if "_x_offsets" in data:
                xo = data["_x_offsets"]
                if "name" in xo: self._x_offsets["name"] = int(xo["name"])
                if "type" in xo: self._x_offsets["type"] = int(xo["type"])
                logger.info("[Renderer] X-Offsets: Name=%s Type=%s",
                            self._x_offsets.get("name"), self._x_offsets.get("type"))
            if "_blur_values" in data:
                self._blur_values = {k: max(0, int(v)) for k, v in data["_blur_values"].items()}
                logger.info("[Renderer] Blur-Werte: %s", self._blur_values)
            logger.info("[Renderer] Kalibrierung geladen: %s", self.calibration_file)
        except Exception as exc:
            logger.warning("[Renderer] Kalibrierung konnte nicht geladen werden: %s", exc)

    def reload_zones(self, calibration_file: str | None = None):
        """Lädt Kalibrierung neu – wird nach dem Speichern aus Flask aufgerufen."""
        if calibration_file:
            self.calibration_file = Path(calibration_file)
        self._calibrated_zones = None
        self._try_load_calibration()
        logger.info("[Renderer] Zonen neu geladen.")

    # ──────────────────────────────────────────────────────────────────────────
    # Font-Loading
    # ──────────────────────────────────────────────────────────────────────────

    def _load_fonts(self):
        """Lädt absolute Pfade für alle Font-Profile."""
        # _font_paths: {"{profile}/{role}": abs_path_or_None}
        self._font_paths = {}
        for profile_name, specs in FONT_PROFILES.items():
            for role, filename in specs.items():
                key  = f"{profile_name}/{role}"
                candidate_files = [filename] + FONT_FILE_ALIASES.get(role, [])
                resolved_path = None

                for candidate in candidate_files:
                    p = self.fonts_dir / candidate
                    if p.exists():
                        resolved_path = p
                        break

                if resolved_path is None:
                    # Fallback: modern-Profil für diesen Role inkl. Alias-Kandidaten
                    fallback_file = FONT_PROFILES["modern"].get(role, "")
                    fallback_candidates = [fallback_file] + FONT_FILE_ALIASES.get(role, [])
                    for candidate in fallback_candidates:
                        if not candidate:
                            continue
                        fb_path = self.fonts_dir / candidate
                        if fb_path.exists():
                            resolved_path = fb_path
                            break

                if resolved_path is not None:
                    self._font_paths[key] = str(resolved_path.resolve())
                else:
                    logger.warning("Font fehlt für Rolle '%s' (Profil %s)", role, profile_name)
                    self._font_paths[key] = None

    def _font(self, key: str, size: int | None = None,
              profile: str = "modern") -> ImageFont.ImageFont:
        """Lädt Font in der gewünschten Größe aus dem angegebenen Profil."""
        defaults = {"name": 36, "type": 26, "oracle": 24, "oracle_italic": 24, "pt": 30}
        if size is None:
            size = self._font_size_overrides.get(key, defaults.get(key, 24))

        abs_path = (self._font_paths.get(f"{profile}/{key}")
                    or self._font_paths.get(f"modern/{key}"))
        if abs_path:
            try:
                return ImageFont.truetype(abs_path, size)
            except Exception as e:
                logger.warning("Font laden fehlgeschlagen (%s/%s, %dpt): %s",
                               profile, key, size, e)
        return ImageFont.load_default()

    def _auto_scale_font(self, font, text: str, max_w: int, profile: str = "modern"):
        """Verkleinert Font bis Text in max_w passt."""
        if not isinstance(font, ImageFont.FreeTypeFont):
            return font
        try:
            tw = font.getlength(text)
        except AttributeError:
            return font
        if tw <= max_w:
            return font
        ratio    = max_w / tw
        new_size = max(10, int(font.size * ratio))
        # Pfad über _font_paths suchen statt font.path (Windows-Bug-Fix)
        font_basename = Path(font.path).name
        for k, abs_path in self._font_paths.items():
            if abs_path and Path(abs_path).name == font_basename:
                try:
                    return ImageFont.truetype(abs_path, new_size)
                except Exception:
                    break
        try:
            return ImageFont.truetype(font.path, new_size)
        except Exception:
            return font

    # ──────────────────────────────────────────────────────────────────────────
    # Öffentliche Haupt-Methode
    # ──────────────────────────────────────────────────────────────────────────

    def process_card(self, card_data: dict, original_path: str,
                     overrides: dict | None = None,
                     force: bool = False) -> str | None:
        """
        Verarbeitet eine Karte.

        overrides:  Feinabstimmungs-Optionen als dict – erweiterbar ohne API-Änderung.
                    Bekannte Schlüssel:
                      'dy'           – {'name': 0.005, ...}  relative Y-Verschiebung
                      'font_profile' – 'modern' | 'old' | 'medieval' (None = auto)
                      'font_sizes'   – {'name': 36, 'type': 26}  (reserviert)
                      'x_offsets'    – {'name': 16, 'type': 16}  (reserviert)

        force:      Cache immer ignorieren (unabhängig von overrides).

        WICHTIG: Das Cache-Bypass-Kriterium ist bewusst `not overrides`.
        Neue Features müssen NUR hier im overrides-dict ergänzt werden –
        die Cache-Bedingung selbst muss NICHT mehr geändert werden.
        """
        layout = card_data.get("layout", "normal")
        if layout not in SUPPORTED_LAYOUTS:
            logger.info("Layout '%s' nicht unterstützt – kein Rendering.", layout)
            return None

        card_id  = card_data.get("id", "unknown")
        out_path = str(Path(original_path).parent / f"{card_id}_rendered.png")

        # Cache-Check: nur überspringen wenn KEINE overrides und kein force.
        # Jedes zukünftige Feature landet in overrides → dieser Check ändert sich nie.
        if Path(out_path).exists() and not force and not overrides:
            return out_path

        # Overrides extrahieren (sicher, auch wenn None oder unbekannte Keys)
        ov             = overrides or {}
        dy_overrides   = ov.get("dy")           or None   # {'zone': float}
        font_profile   = ov.get("font_profile")  or None  # str | None
        font_sizes     = ov.get("font_sizes")    or None  # {'name': 36, 'type': 26, 'oracle': 24, 'flavor': 24}
        text_overrides = ov.get("text_overrides") or None # {'name': '...', 'type_line': '...', 'oracle': '...', 'flavor': '...'}

        # Font-Profil bestimmen: explizit > auto > modern
        if font_profile is None:
            font_profile = _auto_font_profile(card_data.get("released_at", "2020-01-01"))
        if font_profile not in FONT_PROFILES:
            font_profile = "modern"

        logger.info("[Renderer] Font-Profil: %s (Karte: %s)", font_profile, card_id)

        # 1. Englisches HR-Bild holen
        en_img = self._fetch_en_hr(card_data)
        if en_img is None:
            logger.warning("Kein EN-HR für '%s' – kein Rendering.", card_id)
            return None

        # 2. Zonen wählen und ggf. per-Karte verschieben
        zones = self._choose_zones(card_data)
        if dy_overrides:
            zones = self._apply_dy_overrides(zones, dy_overrides)

        # 3. Textzonen löschen
        img = self._erase_text_zones(en_img.convert("RGBA"), card_data, zones)

        # 4. Deutschen Text einzeichnen (mit optionalen Text- und Schriftgrößen-Overrides)
        texts = self._extract_texts(card_data)
        if text_overrides:
            texts = self._apply_text_overrides(texts, text_overrides)
        img = self._render_all(img, texts, card_data, zones, font_profile, font_sizes=font_sizes)

        # 5. Speichern
        img.save(out_path, "PNG", optimize=True)
        logger.info("Gerendert: %s", out_path)
        return out_path

    def _apply_text_overrides(self, texts: dict, text_overrides: dict) -> dict:
        """Überschreibt einzelne Text-Felder mit benutzerdefinierten Werten."""
        result = dict(texts)
        if "name" in text_overrides and text_overrides["name"].strip():
            result["name"] = text_overrides["name"].strip()
        if "type_line" in text_overrides and text_overrides["type_line"].strip():
            result["type_line"] = text_overrides["type_line"].strip()
        if "oracle" in text_overrides:
            result["oracle"] = text_overrides["oracle"]
        if "flavor" in text_overrides:
            result["flavor"] = text_overrides["flavor"]
        return result

    def _apply_dy_overrides(self, zones: dict, dy_overrides: dict) -> dict:
        """Verschiebt Zonen um relative Y-Deltas (z.B. {'type_line': 0.005})."""
        adjusted = {}
        for key, zdata in zones.items():
            if key.startswith("_"):
                adjusted[key] = zdata
                continue
            dy = dy_overrides.get(key, 0.0)
            if dy == 0.0:
                adjusted[key] = zdata
                continue
            coords = zdata["coords"] if isinstance(zdata, dict) else zdata
            new_coords = [coords[0], coords[1] + dy,
                          coords[2], coords[3] + dy]
            if isinstance(zdata, dict):
                adjusted[key] = dict(zdata, coords=new_coords)
            else:
                adjusted[key] = new_coords
        return adjusted

    # ──────────────────────────────────────────────────────────────────────────
    # Scryfall-Kommunikation
    # ──────────────────────────────────────────────────────────────────────────

    def _fetch_en_hr(self, card_data: dict) -> Image.Image | None:
        """Holt die englische PNG-Version der identischen Printing von Scryfall."""
        set_code = card_data.get("set")
        collector = card_data.get("collector_number")
        if not set_code or not collector:
            return None

        meta_url = f"https://api.scryfall.com/cards/{set_code}/{collector}/en"
        try:
            resp = requests.get(meta_url, timeout=15)
            time.sleep(0.1)
            if resp.status_code != 200:
                return None
            en_data = resp.json()

            img_url = None
            if "image_uris" in en_data:
                img_url = en_data["image_uris"].get("png") or en_data["image_uris"].get("large")
            elif "card_faces" in en_data:
                img_url = en_data["card_faces"][0].get("image_uris", {}).get("png")

            if not img_url:
                return None

            img_resp = requests.get(img_url, timeout=30)
            time.sleep(0.1)
            img_resp.raise_for_status()
            return Image.open(io.BytesIO(img_resp.content))

        except Exception as exc:
            logger.error("Fehler beim EN-HR-Download: %s", exc)
            return None

    # ──────────────────────────────────────────────────────────────────────────
    # Zonen-Auswahl (Frame-Epoche)
    # ──────────────────────────────────────────────────────────────────────────

    def _choose_zones(self, card_data: dict) -> dict:
        """
        Gibt Zonen zurück. Reihenfolge:
          1. Manuell kalibrierte Zonen (zone_calibration.json) – immer bevorzugt
          2. Frame-Epoche aus Erscheinungsjahr (Fallback)
        """
        if self._calibrated_zones:
            return self._calibrated_zones

        released = card_data.get("released_at", "2020-01-01")
        year = int(released[:4]) if released else 2020
        if year >= 2015:
            return ZONES_MODERN
        elif year >= 2003:
            return ZONES_LEGACY
        else:
            return ZONES_OLD

    def _adjust_zones_to_card(self, img: Image.Image, zones: dict) -> dict:
        """
        Passt kalibrierte Zonen per Anchor-Detection an die konkrete Karte an.

        Strategie: Sucht die charakteristischen dunklen Trennlinien zwischen
        Artwork / Typzeile / Oracle auf der Karte. Jede Zone wird nur entlang
        der Y-Achse verschoben (X bleibt unverändert). X-Positionen auf MTG-
        Karten sind deutlich stabiler als Y-Positionen.

        Anker-Definitionen basieren auf den kalibrierten Zonen:
          - artwork_bottom ≈ Oberkante type_line
          - type_bottom    ≈ Oberkante oracle_box
          - oracle_bottom  ≈ Unterkante oracle_box
        """
        try:
            import cv2 as _cv2
        except ImportError:
            return zones  # kein OpenCV → unverändert

        arr  = np.array(img.convert("RGB"))
        bgr  = _cv2.cvtColor(arr, _cv2.COLOR_RGB2BGR)
        h, w = bgr.shape[:2]

        def _get_coords(z):
            return z["coords"] if isinstance(z, dict) else z

        # Erwartete Anker-Y aus kalibrierten Zonen ableiten
        anchors = {}
        if "type_line" in zones:
            c = _get_coords(zones["type_line"])
            anchors["artwork_bottom"] = c[1]   # y1 der Typzeile ≈ Artwork-Unterkante
            anchors["type_bottom"]    = c[3]   # y2 der Typzeile
        if "oracle_box" in zones:
            c = _get_coords(zones["oracle_box"])
            anchors["oracle_bottom"]  = c[3]   # y2 der Oracle-Box

        if not anchors:
            return zones

        # Anker suchen mit Hough-Lines in einem ±3% Suchband
        def _find_hline(expected_rel: float, band: float = 0.03) -> float | None:
            y_center = expected_rel
            y_min    = max(0.0, expected_rel - band)
            y_max    = min(1.0, expected_rel + band)
            py_min, py_max = int(y_min * h), int(y_max * h)
            crop  = bgr[py_min:py_max, 20:w-20]
            gray  = _cv2.cvtColor(crop, _cv2.COLOR_BGR2GRAY)
            edges = _cv2.Canny(gray, 20, 60)
            lines = _cv2.HoughLinesP(
                edges, 1, np.pi / 180,
                threshold=max(60, int(w * 0.3)),
                minLineLength=int(w * 0.45),
                maxLineGap=30,
            )
            if lines is None:
                return None
            ys = [l[0][1] for l in lines if abs(l[0][3] - l[0][1]) < 4]
            if not ys:
                return None
            return (int(np.median(ys)) + py_min) / h  # zurück als relative Koordinate

        # Delta pro Anker berechnen
        deltas = {}
        for anchor_name, expected_rel in anchors.items():
            found_rel = _find_hline(expected_rel)
            if found_rel is not None:
                delta = found_rel - expected_rel
                # Plausibilitätscheck: max ±5% Verschiebung erlaubt
                if abs(delta) < 0.05:
                    deltas[anchor_name] = delta
                    logger.debug("Anker '%s': delta=%.4f (%.1fpx)",
                                 anchor_name, delta, delta * h)

        if not deltas:
            return zones  # nichts gefunden → unverändert

        # Zonen-Zuordnung zu Ankern:
        # Name-Bar ist über alle Editionen sehr stabil → kein Shift
        # Typzeile folgt artwork_bottom, Oracle folgt type_bottom, P/T oracle_bottom
        zone_anchor_map = {
            "type_line":      "artwork_bottom",
            "oracle_box":     "type_bottom",
            "pt_box":         "oracle_bottom",
            "watermark_bump": "type_bottom",
        }

        adjusted = {}
        for key, zdata in zones.items():
            if key.startswith("_"):
                adjusted[key] = zdata
                continue
            anchor = zone_anchor_map.get(key)
            dy = deltas.get(anchor, 0.0)
            if dy == 0.0:
                adjusted[key] = zdata
                continue

            coords = _get_coords(zdata)
            new_coords = [coords[0], coords[1] + dy,
                          coords[2], coords[3] + dy]
            if isinstance(zdata, dict):
                adjusted[key] = dict(zdata, coords=new_coords)
            else:
                adjusted[key] = new_coords

        logger.debug("Zone-Adjustment abgeschlossen. Deltas: %s", deltas)
        return adjusted

    # ──────────────────────────────────────────────────────────────────────────
    # Text-Extraktion aus Scryfall-Daten
    # ──────────────────────────────────────────────────────────────────────────

    def _extract_texts(self, card_data: dict) -> dict:
        """
        Extrahiert lokalisierten Text. printed_* Felder = gedruckte Version
        (lokalisiert), oracle_* = englischer Regeltext. Wir bevorzugen immer
        printed_* und fallen auf oracle_* zurück.
        """
        # Für DFC-Karten: erste Seite
        face = None
        if "card_faces" in card_data and card_data["card_faces"]:
            face = card_data["card_faces"][0]

        def get(field, oracle_fallback):
            v = card_data.get(f"printed_{field}") or (face or {}).get(f"printed_{field}")
            if not v:
                v = card_data.get(oracle_fallback) or (face or {}).get(oracle_fallback, "")
            return v or ""

        return {
            "name":      get("name",      "name"),
            "type_line": get("type_line", "type_line"),
            "oracle":    get("text",      "oracle_text"),
            "flavor":    card_data.get("flavor_text") or (face or {}).get("flavor_text", ""),
            "power":     card_data.get("power")      or (face or {}).get("power", ""),
            "toughness": card_data.get("toughness")  or (face or {}).get("toughness", ""),
            "loyalty":   card_data.get("loyalty", ""),
        }

    # ──────────────────────────────────────────────────────────────────────────
    # Textzonen löschen
    # ──────────────────────────────────────────────────────────────────────────

    def _abs(self, img: Image.Image, zone) -> tuple[int, int, int, int]:
        """Konvertiert relative Zonen-Koordinaten in absolute Pixel.
        Akzeptiert sowohl [x1,y1,x2,y2] als auch {"shape":…,"coords":[…]}."""
        if isinstance(zone, dict):
            zone = zone["coords"]
        w, h = img.size
        return (int(zone[0]*w), int(zone[1]*h), int(zone[2]*w), int(zone[3]*h))

    def _erase_zone(self, img: Image.Image, x1, y1, x2, y2,
                    watermark_bump: tuple | None = None,
                    blur_radius: int = 0) -> Image.Image:
        """
        Entfernt Text aus einer Zone und rekonstruiert den Hintergrund.

        Für normale Karten (gleichmäßiger Frame-Hintergrund):
          → Solid-Fill mit gesampelter Rahmenfarbe (schnell, sauber)

        Für Full-Art-Karten (Artwork unter dem Text):
          → OpenCV TELEA Inpainting (rekonstruiert Artwork unter dem Text)

        Erkennung: Wenn die Standardabweichung der Helligkeit in der Zone
        hoch ist (> 25), handelt es sich wahrscheinlich um Full-Art → Inpainting.

        blur_radius > 0: Weicher Übergang an den Zonenrändern (Feathering).
        """
        from PIL import ImageFilter as _IFilt
        import cv2 as _cv2

        original = img  # für Blur-Blending merken
        arr = np.array(img.convert("RGB"))
        zone = arr[y1:y2, x1:x2]

        # Erkennung: Full-Art vs. normaler Frame
        gray_zone = cv2.cvtColor(zone, cv2.COLOR_RGB2GRAY) if zone.size > 0 else None
        is_fullart = gray_zone is not None and float(gray_zone.std()) > 25.0

        if is_fullart:
            result = self._inpaint_zone(img, x1, y1, x2, y2, arr)
        else:
            result = self._solidfill_zone(img, x1, y1, x2, y2, arr)

        if watermark_bump:
            # Ellipsen-Ausschnitt: Original-Pixel zurückkleben
            bx1, by1, bx2, by2 = watermark_bump
            orig_crop = img.crop((bx1, by1, bx2, by2))
            mask = Image.new("L", (bx2-bx1, by2-by1), 0)
            ImageDraw.Draw(mask).ellipse([0, 0, bx2-bx1, by2-by1], fill=255)
            result.paste(orig_crop, (bx1, by1), mask=mask)

        # Feathering: weicher Überblendbereich an den Zonenrändern
        if blur_radius > 0:
            # Maske: innen 255 (gelöschte Zone), Rand weich auslaufen
            blend_mask = Image.new("L", original.size, 0)
            ImageDraw.Draw(blend_mask).rectangle([x1, y1, x2, y2], fill=255)
            blend_mask = blend_mask.filter(_IFilt.GaussianBlur(radius=blur_radius))
            # composite: blend_mask=255 → result (gelöscht), blend_mask=0 → original
            result = Image.composite(result.convert("RGBA"),
                                     original.convert("RGBA"),
                                     blend_mask).convert(img.mode)

        return result

    def _solidfill_zone(self, img: Image.Image, x1, y1, x2, y2,
                        arr: np.ndarray) -> Image.Image:
        """Füllt Zone mit gesampelter Rahmenfarbe (für normale Karten)."""
        zh = y2 - y1
        strip_w = max(8, min(16, (x2 - x1) // 20))
        pad_y   = max(2, zh // 8)

        left  = arr[y1+pad_y : y2-pad_y, x1         : x1+strip_w]
        right = arr[y1+pad_y : y2-pad_y, x2-strip_w : x2        ]
        samples = np.concatenate([left.reshape(-1, 3), right.reshape(-1, 3)])
        lum    = samples.astype(np.int32).sum(axis=1)
        bright = samples[lum > 400]
        bg     = tuple(int(v) for v in np.median(
            bright if len(bright) >= 8 else samples, axis=0))

        result = img.copy()
        fill   = bg + (255,) if result.mode == "RGBA" else bg
        ImageDraw.Draw(result).rectangle([x1, y1, x2, y2], fill=fill)
        return result

    def _inpaint_zone(self, img: Image.Image, x1, y1, x2, y2,
                      arr: np.ndarray) -> Image.Image:
        """
        Entfernt Text via TELEA-Inpainting und rekonstruiert Artwork-Hintergrund.

        Maske: Blur-Differenz-Methode.
          1. Starken Gauß-Blur auf die Zone anwenden → schätzt den Hintergrund
          2. Differenz Original - Blur → hebt dunkle Textstriche hervor
          3. Schwellwert → Binärmaske
          4. Morphologie → Löcher schließen, Ränder aufdicken
          5. cv2.inpaint(TELEA) → rekonstruiert Pixel aus der Umgebung
        """
        import cv2 as _cv2

        bgr_full = _cv2.cvtColor(arr, _cv2.COLOR_RGB2BGR)
        zone_bgr = bgr_full[y1:y2, x1:x2].copy()
        gray     = _cv2.cvtColor(zone_bgr, _cv2.COLOR_BGR2GRAY)

        # Hintergrundschätzung via starkem Blur
        blur_r = max(21, (gray.shape[1] // 10) | 1)   # muss ungerade sein
        bg_est = _cv2.GaussianBlur(gray, (blur_r, blur_r), 0)

        # Text erscheint als dunkle Abweichung vom geschätzten Hintergrund
        diff = bg_est.astype(np.int16) - gray.astype(np.int16)
        diff_clipped = np.clip(diff, 0, 255).astype(np.uint8)

        # Schwellwert: alles was mehr als 18 Graustufen dunkler als Umgebung ist
        _, mask = _cv2.threshold(diff_clipped, 18, 255, _cv2.THRESH_BINARY)

        # Morphologie: Textstriche schließen + leicht aufdicken
        k3 = _cv2.getStructuringElement(_cv2.MORPH_ELLIPSE, (3, 3))
        k5 = _cv2.getStructuringElement(_cv2.MORPH_ELLIPSE, (5, 5))
        mask = _cv2.morphologyEx(mask, _cv2.MORPH_CLOSE, k3, iterations=2)
        mask = _cv2.dilate(mask, k5, iterations=2)

        # Inpainting
        inpainted = _cv2.inpaint(zone_bgr, mask, inpaintRadius=7,
                                  flags=_cv2.INPAINT_TELEA)

        # Zurück nach PIL
        result_arr = arr.copy()
        result_arr[y1:y2, x1:x2] = _cv2.cvtColor(inpainted, _cv2.COLOR_BGR2RGB)

        result = img.copy()
        if result.mode == "RGBA":
            rgb  = Image.fromarray(result_arr)
            rgba = rgb.convert("RGBA")
            # Alpha-Kanal aus Original erhalten
            orig_alpha = img.split()[3]
            rgba.putalpha(orig_alpha)
            return rgba
        return Image.fromarray(result_arr)

    def _erase_text_zones(self, img: Image.Image, card_data: dict, zones: dict) -> Image.Image:
        """Löscht alle Textzonen.

        Benutzt durchgängig self._abs() für die Koordinaten-Umrechnung –
        _abs() versteht sowohl plain-list-Zonen als auch dict-Zonen
        {shape:…, coords:[…]}, daher keine manuelle Entschachtelung nötig.
        """
        original_img = img.copy()
        w, h = img.size

        for key in ["name", "type_line"]:
            if key in zones:
                img = self._erase_zone(img, *self._abs(img, zones[key]),
                                       blur_radius=self._blur_values.get(key, 0))

        # Wassermarken-Bump aus Kalibrierung lesen
        bump_abs = None
        if "watermark_bump" in zones:
            wb     = zones["watermark_bump"]
            coords = wb["coords"] if isinstance(wb, dict) else wb
            bump_abs = (
                int(coords[0]*w), int(coords[1]*h),
                int(coords[2]*w), int(coords[3]*h),
            )

        if "oracle_box" in zones:
            img = self._erase_zone(img, *self._abs(img, zones["oracle_box"]),
                                   watermark_bump=bump_abs,
                                   blur_radius=self._blur_values.get("oracle_box", 0))

        # pt_box als Schutzbereich: Original-Pixel nach Oracle-Löschung zurückkleben,
        # damit der Oracle-Cleanup nicht in den P/T-Bereich hineinragt.
        if "pt_box" in zones:
            pt_abs = self._abs(img, zones["pt_box"])
            pt_crop = original_img.crop(pt_abs)
            img.paste(pt_crop, (pt_abs[0], pt_abs[1]))

        # P/T-Box bewusst NICHT löschen:
        # Power/Toughness soll als Originalwert auf der Karte erhalten bleiben.

        return img

    # ──────────────────────────────────────────────────────────────────────────
    # Text rendern – Koordinator
    # ──────────────────────────────────────────────────────────────────────────

    def _render_all(self, img: Image.Image, texts: dict, card_data: dict,
                    zones: dict, font_profile: str = "modern",
                    font_sizes: dict | None = None) -> Image.Image:
        draw = ImageDraw.Draw(img)
        fs = font_sizes or {}

        if texts["name"]:
            self._render_name(img, draw, texts["name"], zones, font_profile,
                              size_override=fs.get("name"))

        if texts["type_line"]:
            self._render_type_line(img, draw, texts["type_line"], zones, font_profile,
                                   size_override=fs.get("type_line"))

        if texts["oracle"] or texts["flavor"]:
            self._render_oracle(img, draw, texts["oracle"], texts["flavor"],
                                zones, font_profile=font_profile,
                                size_override=fs.get("oracle"),
                                flavor_size_override=fs.get("flavor"))

        # P/T bewusst nicht neu rendern:
        # Zahlen werden nicht übersetzt und sollen im Overlay-Workflow leer bleiben.

        return img

    # ──────────────────────────────────────────────────────────────────────────
    # Einzel-Render-Methoden
    # ──────────────────────────────────────────────────────────────────────────

    def _render_name(self, img: Image.Image, draw: ImageDraw.ImageDraw,
                     name: str, zones: dict, profile: str = "modern",
                     size_override: int | None = None):
        x1, y1, x2, y2 = self._abs(img, zones["name"])
        size  = size_override if size_override is not None else self._font_size_overrides.get("name", 36)
        x_off = self._x_offsets.get("name", 16)
        font  = self._font("name", size, profile=profile)
        print(f"[Renderer] Name '{name}': {size}pt  x_off={x_off}  profil={profile}")
        try:
            bb = font.getbbox(name)
            th, desc = bb[3] - bb[1], bb[1]
        except Exception:
            th, desc = size, 0
        cy = (y1 + y2) // 2
        ty = max(y1, cy - th // 2 - desc // 2)
        draw.text((x1 + x_off, ty), name, font=font, fill=(0, 0, 0, 255))

    def _render_type_line(self, img: Image.Image, draw: ImageDraw.ImageDraw,
                          type_line: str, zones: dict, profile: str = "modern",
                          size_override: int | None = None):
        x1, y1, x2, y2 = self._abs(img, zones["type_line"])
        size  = size_override if size_override is not None else self._font_size_overrides.get("type", 26)
        x_off = self._x_offsets.get("type", 16)
        font  = self._font("type", size, profile=profile)
        print(f"[Renderer] Type '{type_line}': {size}pt  x_off={x_off}  profil={profile}")
        try:
            bb = font.getbbox(type_line)
            th, desc = bb[3] - bb[1], bb[1]
        except Exception:
            th, desc = size, 0
        cy = (y1 + y2) // 2
        ty = max(y1, cy - th // 2 - desc // 2)
        draw.text((x1 + x_off, ty), type_line, font=font, fill=(0, 0, 0, 255))

    def _render_centered(self, draw: ImageDraw.ImageDraw, img: Image.Image,
                         text: str, box: tuple, font):
        """Rendert Text horizontal und vertikal zentriert in einer Box."""
        x1, y1, x2, y2 = box
        font = self._auto_scale_font(font, text, x2 - x1 - 8)
        try:
            bb = font.getbbox(text)
            tw, th = bb[2] - bb[0], bb[3] - bb[1]
        except Exception:
            tw = len(text) * 10
            th = 20
        cx, cy = (x1 + x2) // 2, (y1 + y2) // 2
        draw.text((cx - tw // 2, cy - th // 2), text, font=font, fill=(0, 0, 0, 255))

    # ──────────────────────────────────────────────────────────────────────────
    # Oracle-Text-Renderer (Herzstück)
    # ──────────────────────────────────────────────────────────────────────────

    def _render_oracle(self, img: Image.Image, draw: ImageDraw.ImageDraw,
                       oracle: str, flavor: str, zones: dict, _min_size: int = 11,
                       font_profile: str = "modern",
                       size_override: int | None = None,
                       flavor_size_override: int | None = None):
        """
        Rendert Oracle-Text mit dynamischer Schriftgrößenanpassung.
        Strategie: Starte bei max. Größe (28pt), reduziere in 1pt-Schritten
        bis der gesamte Text in die Box passt. Messen vor dem Rendern.
        Wenn size_override gesetzt, wird diese Größe als Ausgangspunkt erzwungen
        (aber weiter reduziert wenn Text nicht passt).
        """
        x1, y1, x2, y2 = self._abs(img, zones["oracle_box"])
        max_w = x2 - x1 - 20   # horizontaler Innenabstand
        max_h = y2 - y1 - 12   # vertikaler Innenabstand
        pad_x = x1 + 10
        pad_y = y1 + 6

        # Startgröße: override (erzwungen) oder Standard 28pt
        size = size_override if size_override is not None else 28

        lines = []
        line_height = 0
        para_gap = 0
        sym_size = 0

        while size >= _min_size:
            sym_size    = max(size, int(size * 1.08))
            font_reg    = self._font("oracle",        size, profile=font_profile)
            font_ita    = self._font("oracle_italic", size, profile=font_profile)
            line_height = int(size * 1.50)
            para_gap    = int(size * 0.60)

            lines = self._build_lines(oracle, flavor, font_reg, font_ita,
                                      draw, max_w, sym_size)
            total_h = sum(line_height if ln is not None else para_gap
                          for ln in lines)
            if total_h <= max_h:
                break
            size -= 1

        # Vertikal oben ausrichten (wie echte Karten), nicht zentrieren
        y = pad_y

        for line in lines:
            if line is None:
                y += para_gap
                continue
            x = pad_x
            for seg in line:
                if seg["type"] == "sym":
                    si = self._get_symbol(seg["text"], sym_size)
                    if si:
                        oy = y + max(0, (line_height - sym_size) // 2)
                        mask = si.split()[3] if si.mode == "RGBA" else None
                        img.paste(si, (int(x), int(oy)), mask)
                    x += sym_size + 2
                else:
                    draw.text((x, y + (line_height - size) // 2),
                              seg["text"], font=seg["font"], fill=(0, 0, 0, 255))
                    x += self._text_w(seg["text"], seg["font"], draw)
            y += line_height

    def _build_lines(self, oracle: str, flavor: str,
                     font_reg, font_ita, draw,
                     max_w: int, sym_size: int) -> list:
        """Erstellt eine Liste von Zeilen (None = Absatzlücke)."""
        all_lines: list = []

        for para in oracle.strip().split("\n"):
            para = para.strip()
            if not para:
                continue
            all_lines += self._wrap(para, font_reg, font_ita, draw, max_w, sym_size)
            all_lines.append(None)

        if flavor:
            all_lines.append(None)
            for para in flavor.strip().split("\n"):
                para = para.strip()
                if not para:
                    continue
                all_lines += self._wrap(para, font_ita, font_ita, draw,
                                        max_w, sym_size, force_italic=True)
                all_lines.append(None)

        # Trailing None entfernen
        while all_lines and all_lines[-1] is None:
            all_lines.pop()
        return all_lines

    # ──────────────────────────────────────────────────────────────────────────
    # Tokenizer + Word-Wrapper
    # ──────────────────────────────────────────────────────────────────────────

    _TOKEN_RE = re.compile(r'(\{[^}]+\}|\([^)]*\)|[^{(]+)')

    def _tokenize(self, text: str, font_reg, font_ita, force_italic: bool) -> list:
        """
        Zerlegt einen Absatz in atomare Segmente:
          {"type": "sym",  "text": "{W}",       "font": None}
          {"type": "text", "text": "Fliegend",  "font": <font>}
        Reminder-Text (in Klammern) und force_italic erhalten font_ita.
        """
        segs = []
        for m in self._TOKEN_RE.finditer(text):
            tok = m.group(0)
            if re.fullmatch(r'\{[^}]+\}', tok):
                segs.append({"type": "sym", "text": tok, "font": None})
            elif tok.startswith("(") and tok.endswith(")"):
                segs.append({"type": "text", "text": tok, "font": font_ita})
            else:
                f = font_ita if force_italic else font_reg
                segs.append({"type": "text", "text": tok, "font": f})
        return segs

    def _wrap(self, text: str, font_reg, font_ita, draw,
              max_w: int, sym_size: int, force_italic: bool = False) -> list:
        """Gibt eine Liste von Zeilen zurück (Word-wrap)."""
        raw_segs = self._tokenize(text, font_reg, font_ita, force_italic)

        # Aufteilen in Wörter & Symbole (atomare Einheiten)
        atoms: list = []
        for seg in raw_segs:
            if seg["type"] == "sym":
                atoms.append(seg)
            else:
                for part in re.split(r'(\s+)', seg["text"]):
                    if part:
                        atoms.append({"type": "text", "text": part, "font": seg["font"]})

        lines: list = []
        cur_line: list = []
        cur_w = 0.0

        for atom in atoms:
            if atom["type"] == "sym":
                w = sym_size + 2
            else:
                w = self._text_w(atom["text"], atom["font"], draw)

            is_space = atom["type"] == "text" and atom["text"].strip() == ""

            if not is_space and cur_w + w > max_w and cur_line:
                lines.append(self._strip_trailing_space(cur_line))
                cur_line, cur_w = [], 0.0

            cur_line.append(atom)
            cur_w += w

        if cur_line:
            lines.append(self._strip_trailing_space(cur_line))
        return lines

    @staticmethod
    def _strip_trailing_space(line: list) -> list:
        while line and line[-1]["type"] == "text" and line[-1]["text"].strip() == "":
            line.pop()
        return line

    # ──────────────────────────────────────────────────────────────────────────
    # Mana-Symbole
    # ──────────────────────────────────────────────────────────────────────────

    def _get_symbol(self, symbol: str, size: int) -> Image.Image | None:
        """Gibt ein gecachtes Symbol-Bild zurück."""
        key = f"{symbol}_{size}"
        if key in self._sym_cache:
            return self._sym_cache[key]

        clean = symbol.strip("{}").upper().replace("/", "-")
        disk_path = self.symbol_cache_dir / f"{clean}_{size}.png"

        if disk_path.exists():
            img = Image.open(disk_path).convert("RGBA")
            self._sym_cache[key] = img
            return img

        img = self._fallback_symbol(clean, size)
        img.save(disk_path)
        self._sym_cache[key] = img
        return img

    def _fallback_symbol(self, clean: str, size: int) -> Image.Image:
        """Zeichnet ein einfaches farbiges Kreis-Symbol als Fallback."""
        base = clean.split("-")[0]
        bg, fg = MANA_FALLBACK_COLORS.get(base, ("#CCC", "#000"))
        img  = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        draw.ellipse([1, 1, size - 2, size - 2], fill=bg, outline="#666", width=1)

        label = base[:2] if len(base) <= 2 else base[0]
        fs = max(8, size // 2)
        try:
            f = self._font("oracle", fs)
        except Exception:
            f = ImageFont.load_default()
        try:
            bb = draw.textbbox((0, 0), label, font=f)
            tw, th = bb[2] - bb[0], bb[3] - bb[1]
        except Exception:
            tw, th = fs, fs
        draw.text(((size - tw) // 2, (size - th) // 2), label, font=f, fill=fg)
        return img

    # ──────────────────────────────────────────────────────────────────────────
    # Hilfsmethoden
    # ──────────────────────────────────────────────────────────────────────────

    def _text_w(self, text: str, font, draw: ImageDraw.ImageDraw) -> float:
        try:
            return font.getlength(text)
        except AttributeError:
            return draw.textlength(text, font=font)

    def _auto_scale_font(self, font, text: str, max_w: int):
        """Reduziert Schriftgröße, falls Text breiter als max_w ist."""
        if not isinstance(font, ImageFont.FreeTypeFont):
            return font
        try:
            tw = font.getlength(text)
        except AttributeError:
            return font
        if tw <= max_w:
            return font
        ratio = max_w / tw
        new_size = max(10, int(font.size * ratio))
        try:
            return ImageFont.truetype(font.path, new_size)
        except Exception:
            return font
