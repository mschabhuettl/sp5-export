#!/usr/bin/env bash
# Self-test of scripts/check-images.sh: images with planted names must fail, a clean image (with the
# legitimate words Schichtplaner and SP5) must pass.
# The planted words are assembled at run time, so that this file does not contain them.
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
WORK=$(mktemp -d)
trap 'rm -rf "${WORK:?}"' EXIT

python3 - "$WORK" <<'EOF'
import sys
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

out = Path(sys.argv[1])
font_path = next((str(p) for p in Path("/usr/share/fonts").rglob("DejaVuSans.ttf")), None)
font = ImageFont.truetype(font_path, 48) if font_path else ImageFont.load_default(size=48)
planted = {
    "old-name": "Shift" + "fold Dienstplan",
    "area-code": "Bereich " + "N" + "CT Notruf",
    "dark": "Shift" + "fold",
    "private": "Server 192." + "168.10.20",
    "clean": "Dispofold Schichtplaner SP5 Export",
}
for name, text in planted.items():
    dark = name == "dark"
    im = Image.new("RGB", (900, 140), (20, 24, 32) if dark else (255, 255, 255))
    ImageDraw.Draw(im).text((30, 40), text, fill=(230, 230, 230) if dark else (0, 0, 0), font=font)
    im.save(out / f"{name}.png")
svg = ('<svg xmlns="http://www.w3.org/2000/svg" width="900" height="140"><rect width="900" height="140" '
       'fill="white"/><text x="30" y="90" font-size="48" font-family="DejaVu Sans">'
       + "Shift" + "fold" + '</text></svg>')
(out / "planted.svg").write_text(svg)
EOF

fail=0
for name in old-name area-code dark private planted; do
  file="$WORK/$name.png"
  [ "$name" = planted ] && file="$WORK/planted.svg"
  if scripts/check-images.sh "$file" >/dev/null 2>&1; then
    echo "not detected: $name"; fail=1
  else
    echo "detected: $name"
  fi
done
if scripts/check-images.sh "$WORK/clean.png" >/dev/null 2>&1; then
  echo "clean image passes"
else
  echo "clean image fails"; fail=1
fi
[ "$fail" -eq 0 ] && echo "test-check-images: OK" || { echo "test-check-images: FAILED"; exit 1; }
