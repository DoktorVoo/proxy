"""
font_setup.py
=============
Einmalig ausführen nach dem Entpacken des Full-Magic-Pack.
Scannt das fonts/-Verzeichnis, erkennt MTG-Fonts automatisch
und gibt die korrekte _load_fonts()-Konfiguration aus.

Aufruf:
    python font_setup.py
    python font_setup.py --fonts-dir "C:/Users/maxbi/fonts"
"""

import argparse
import re
from pathlib import Path

# ── Mapping: Schlüsselwörter im Dateinamen → Renderer-Rolle ──────────────────
# Reihenfolge innerhalb einer Rolle = Priorität (erstes Match gewinnt)
ROLE_PATTERNS: list[tuple[str, list[str]]] = [
    ("name", [
        r"beleren.*bold",
        r"beleren2016",
        r"beleren",
    ]),
    ("type", [
        r"matrix.*bold.*small",
        r"matrixboldsm",
        r"matrix.*bold",
        r"matrix",
    ]),
    ("oracle", [
        r"mplantin(?!.*italic)(?!.*it\.)",
        r"plantin(?!.*italic)(?!.*it\.)",
        r"magicthegathering(?!.*italic)",
    ]),
    ("oracle_italic", [
        r"mplantin.*italic",
        r"mplantin.*it\.",
        r"plantin.*italic",
        r"plantin.*it\.",
    ]),
    ("pt", [
        r"beleren.*bold",
        r"beleren",
        r"matrix.*bold",
    ]),
]

# Rollen, für die ein Fallback auf eine andere Rolle akzeptabel ist
FALLBACKS: dict[str, str] = {
    "oracle_italic": "oracle",
    "pt":            "name",
}


def scan_fonts(fonts_dir: Path) -> dict[str, Path]:
    """
    Scannt fonts_dir und gibt ein Dict {rolle: pfad} zurück.
    Gibt für jede Rolle den besten Fund zurück.
    """
    all_fonts = sorted(
        p for p in fonts_dir.iterdir()
        if p.suffix.lower() in {".ttf", ".otf"}
    )

    if not all_fonts:
        print(f"⚠  Keine Fonts in '{fonts_dir}' gefunden.")
        return {}

    print(f"Gefundene Font-Dateien in '{fonts_dir}':")
    for f in all_fonts:
        print(f"  {f.name}")
    print()

    result: dict[str, Path] = {}

    for role, patterns in ROLE_PATTERNS:
        if role in result:
            continue  # bereits gefüllt (z. B. "pt" gleich wie "name")
        for pattern in patterns:
            for font_path in all_fonts:
                name_lower = font_path.stem.lower().replace(" ", "").replace("-", "").replace("_", "")
                pat_clean  = pattern.replace(" ", "").replace("-", "").replace("_", "")
                if re.search(pat_clean, name_lower):
                    result[role] = font_path
                    break
            if role in result:
                break

    # Fallbacks anwenden
    for role, fallback_role in FALLBACKS.items():
        if role not in result and fallback_role in result:
            result[role] = result[fallback_role]
            print(f"  (Fallback: '{role}' → '{fallback_role}')")

    return result


def print_config(mapping: dict[str, Path]) -> None:
    """Gibt den fertigen _load_fonts()-Block aus."""
    print("\n" + "="*60)
    print("Erkannte Zuordnung:")
    print("="*60)
    roles = ["name", "type", "oracle", "oracle_italic", "pt"]
    for role in roles:
        if role in mapping:
            print(f"  {role:15s} → {mapping[role].name}")
        else:
            print(f"  {role:15s} → ⚠ NICHT GEFUNDEN (PIL-Fallback)")

    print("\n" + "="*60)
    print("Füge das in card_renderer.py → _load_fonts() ein:")
    print("="*60)
    print("""    def _load_fonts(self):
        specs = {""")
    sizes = {"name": 36, "type": 26, "oracle": 24, "oracle_italic": 24, "pt": 30}
    for role in roles:
        if role in mapping:
            fname = mapping[role].name
            size  = sizes[role]
            print(f'            "{role}": ("{fname}", {size}),')
        else:
            print(f'            # "{role}": NOT FOUND – PIL fallback wird benutzt')
    print("""        }
        for key, (filename, size) in specs.items():
            path = self.fonts_dir / filename
            try:
                self._font_cache[key] = ImageFont.truetype(str(path), size)
            except (IOError, OSError):
                self._font_cache[key] = ImageFont.load_default()""")

    print("\n" + "="*60)
    print("Oder: card_renderer.py AUTO-UPDATE (--apply flag nutzen)")
    print("="*60)


def apply_to_renderer(mapping: dict[str, Path], renderer_path: Path) -> None:
    """Patcht card_renderer.py direkt mit den erkannten Fonts."""
    if not renderer_path.exists():
        print(f"⚠  '{renderer_path}' nicht gefunden – kein Auto-Update.")
        return

    src = renderer_path.read_text(encoding="utf-8")

    roles = ["name", "type", "oracle", "oracle_italic", "pt"]
    sizes = {"name": 36, "type": 26, "oracle": 24, "oracle_italic": 24, "pt": 30}

    lines = []
    for role in roles:
        if role in mapping:
            fname = mapping[role].name
            size  = sizes[role]
            lines.append(f'            "{role}": ("{fname}", {size}),')
        else:
            lines.append(f'            # "{role}": NOT FOUND')

    new_specs = "\n".join(lines)

    # Ersetze den specs-Block in _load_fonts
    pattern = re.compile(
        r'(def _load_fonts\(self\):.*?specs = \{)(.*?)(\})',
        re.DOTALL
    )

    def replacer(m):
        return m.group(1) + "\n" + new_specs + "\n        " + m.group(3)

    new_src, n = pattern.subn(replacer, src, count=1)
    if n == 0:
        print("⚠  _load_fonts() specs-Block nicht gefunden. Manuell anpassen.")
        return

    renderer_path.write_text(new_src, encoding="utf-8")
    print(f"✓  card_renderer.py aktualisiert: {renderer_path}")


def main():
    parser = argparse.ArgumentParser(
        description="MTG Font Auto-Detector für card_renderer.py"
    )
    parser.add_argument(
        "--fonts-dir", default="fonts",
        help="Pfad zum fonts/-Verzeichnis (Standard: ./fonts)"
    )
    parser.add_argument(
        "--renderer", default="card_renderer.py",
        help="Pfad zu card_renderer.py (Standard: ./card_renderer.py)"
    )
    parser.add_argument(
        "--apply", action="store_true",
        help="card_renderer.py direkt patchen (sonst nur Ausgabe)"
    )
    args = parser.parse_args()

    fonts_dir     = Path(args.fonts_dir)
    renderer_path = Path(args.renderer)

    if not fonts_dir.exists():
        print(f"⚠  Verzeichnis '{fonts_dir}' existiert nicht.")
        print("   Erstelle es und kopiere die TTF-Dateien aus dem Full-Magic-Pack hinein.")
        return

    mapping = scan_fonts(fonts_dir)
    print_config(mapping)

    if args.apply:
        apply_to_renderer(mapping, renderer_path)
    else:
        print("\nTipp: Mit --apply wird card_renderer.py automatisch angepasst.")


if __name__ == "__main__":
    main()
