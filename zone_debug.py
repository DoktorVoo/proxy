"""
zone_debug.py
=============
Zeichnet die Zonen-Rechtecke direkt auf gecachte Karten-Bilder
und zeigt die gemessenen Hintergrundfarben.

Aufruf:
    python zone_debug.py                        # erstes Bild in card_images/
    python zone_debug.py card_images/xxxx.jpg   # bestimmtes Bild
"""
import sys
import numpy as np
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

from card_renderer import ZONES_MODERN

COLORS = {
    "name":       "#FF0000",   # Rot
    "type_line":  "#0066FF",   # Blau
    "oracle_box": "#00CC00",   # Grün
    "pt_box":     "#FF8800",   # Orange
}

def sample_bg(arr, x1, y1, x2, y2):
    zh = y2 - y1
    strip_w = max(8, min(16, (x2 - x1) // 20))
    pad_y   = max(2, zh // 8)
    left  = arr[y1+pad_y:y2-pad_y, x1:x1+strip_w].reshape(-1, 3)
    right = arr[y1+pad_y:y2-pad_y, x2-strip_w:x2].reshape(-1, 3)
    samples = np.concatenate([left, right])
    lum = samples.sum(axis=1)
    bright = samples[lum > 400]
    if len(bright) < 8:
        bright = samples
    return tuple(int(v) for v in np.median(bright, axis=0))

def debug_image(img_path: Path):
    img = Image.open(img_path).convert("RGB")
    arr = np.array(img).astype(np.int32)
    w, h = img.size
    print(f"\nBild: {img_path.name}  ({w}×{h} px)")

    debug = img.copy()
    draw  = ImageDraw.Draw(debug)

    for key, zone in ZONES_MODERN.items():
        x1 = int(zone[0] * w); y1 = int(zone[1] * h)
        x2 = int(zone[2] * w); y2 = int(zone[3] * h)
        bg = sample_bg(arr, x1, y1, x2, y2)
        col = COLORS.get(key, "#FFFFFF")
        draw.rectangle([x1, y1, x2, y2], outline=col, width=4)
        # Beschriftung
        label = f"{key} y={y1}..{y2} ({zone[1]:.3f}..{zone[3]:.3f}) bg=RGB{bg}"
        draw.text((x1 + 5, y1 + 3), label, fill=col)
        print(f"  {key:12s}: px ({x1},{y1})→({x2},{y2})  bg=RGB{bg}")

    out = img_path.parent / f"DEBUG_{img_path.stem}.png"
    debug.save(out)
    print(f"  → Gespeichert: {out}")

    # Auch Erase-Test
    erased = img.copy()
    draw2  = ImageDraw.Draw(erased)
    for key, zone in ZONES_MODERN.items():
        x1 = int(zone[0] * w); y1 = int(zone[1] * h)
        x2 = int(zone[2] * w); y2 = int(zone[3] * h)
        bg = sample_bg(arr, x1, y1, x2, y2)
        fill = bg + (255,) if erased.mode == "RGBA" else bg
        draw2.rectangle([x1, y1, x2, y2], fill=fill)
    out2 = img_path.parent / f"ERASE_{img_path.stem}.png"
    erased.save(out2)
    print(f"  → Erase-Test: {out2}")

if __name__ == "__main__":
    if len(sys.argv) > 1:
        paths = [Path(sys.argv[1])]
    else:
        card_dir = Path("card_images")
        paths = sorted(card_dir.glob("*.jpg"))[:3] + sorted(card_dir.glob("*.png"))[:1]
        paths = [p for p in paths if "rendered" not in p.name and "DEBUG" not in p.name]

    if not paths:
        print("Keine Bilder gefunden. Pfad als Argument angeben.")
        sys.exit(1)

    for p in paths:
        debug_image(p)
