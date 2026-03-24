# Fonts für den MTG Card Renderer

Lege folgende Schriftdateien in diesen Ordner (fonts/):

┌─────────────────────────────────┬──────────────────────────────┬────────────────────────────┐
│ Dateiname (exakt)               │ Verwendung                   │ Suchwort / Quelle          │
├─────────────────────────────────┼──────────────────────────────┼────────────────────────────┤
│ Beleren2016-Bold.ttf            │ Kartenname, P/T              │ "Beleren MTG font"         │
│ MatrixBoldSmallCaps.ttf         │ Typzeile                     │ "Matrix Bold Small Caps"   │
│ MPlantin.ttf                    │ Oracle-Text (normal)         │ "MPlantin font MTG"        │
│ MPlantin-Italic.ttf             │ Reminder-Text, Flavor-Text   │ "MPlantin Italic"          │
└─────────────────────────────────┴──────────────────────────────┴────────────────────────────┘

Alle vier Fonts sind in der MTG-Proxy-Community weit verbreitet und
über einschlägige Foren und Discord-Server erhältlich.

Falls eine Datei fehlt, wird PIL-Default (Courier-ähnlich) als Fallback
verwendet – die Karte funktioniert, sieht aber deutlich schlechter aus.

WICHTIG – Dateinamen sind case-sensitiv (Linux/Mac):
  ✓  Beleren2016-Bold.ttf
  ✗  beleren2016-bold.ttf
  ✗  Beleren Bold.ttf

Nach dem Ablegen der Fonts app.py neu starten.
