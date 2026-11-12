#!/usr/bin/env bash
# Reads the text in every tracked image with OCR and fails on the same names as scripts/check-repo.sh.
# Raster images (PNG, JPEG, GIF, WebP, ICO, ICNS) and SVGs (rendered first) are checked, each also inverted,
# so that light text on dark backgrounds is found too.
# Needs: tesseract with the German and English data, rsvg-convert, Python 3 with Pillow.
# Usage: scripts/check-images.sh [file ...]   (default: all tracked images)
set -euo pipefail
export LC_ALL=C.UTF-8
cd "$(git rev-parse --show-toplevel)"
WORK=$(mktemp -d)
trap 'rm -rf "${WORK:?}"' EXIT

# Same names as the text check; the character classes keep this file from matching itself.
# Schichtplaner, SP5 and DBF are legitimate in this repository and not listed.
names='[s]hiftfold|\b[r]ls\b|[s]teiermark|[r]ote[sn]? ?[k]reuz|[r]ed ?[c]ross|[ö]rk\b|[r]oteskreuz|[s]ource ?[s]ans'
names=$names'|[d]unant|[s]tra(ß|ss)gang|\b[g]raz\b|[g]ithub|[g]hcr|[c]laude|[a]nthropic|[o]penai|[c]hatgpt|[l]lm|[c]odex'
names=$names'|[s]chabhuettl|192\.168\.[0-9]'
names=$names'|[n]otruf calltaking|[k]rankentransport calltaking|[o]ffizier vom dienst|[ä]rztenotdienst|[n]otarzt-einsatzmittel'
# Codes of a real dispatch centre: whole words, case sensitive.
codes='\b([N]CT|[K]CTS?|[A]END|[N]EM|[O]vD|[O]VD|[D]P|[N]-CT|[K]-CT)\b|(^|[^[:alnum:]])[Ä]ND\b'

if [ "$#" -gt 0 ]; then
  files=("$@")
else
  mapfile -t files < <(git ls-files | grep -i -E '\.(png|jpe?g|gif|webp|ico|icns|svg)$')
fi

fail=0
n=0
for f in "${files[@]}"; do
  n=$((n + 1))
  png="$WORK/$n.png"
  case "${f,,}" in
    *.svg) rsvg-convert -w 1600 "$f" -o "$WORK/$n-svg.png"; src="$WORK/$n-svg.png" ;;
    *) src="$f" ;;
  esac
  # Normalise: largest frame of ICO (ICNS opens with its largest), white background, small images enlarged; plus an inverted copy.
  python3 - "$src" "$png" "$WORK/$n-inv.png" <<'EOF'
import sys
from PIL import Image, ImageOps
src, out, inv = sys.argv[1:4]
im = Image.open(src)
if im.format == "ICO":
    im = im.ico.getimage(max(im.ico.sizes()))
im = im.convert("RGBA")
bg = Image.new("RGBA", im.size, (255, 255, 255, 255))
im = Image.alpha_composite(bg, im).convert("L")
if im.width < 1200:
    f = max(2, 1200 // max(1, im.width))
    im = im.resize((im.width * f, im.height * f), Image.LANCZOS)
im.save(out)
ImageOps.invert(im).save(inv)
EOF
  text=$( (tesseract "$png" - -l deu+eng 2>/dev/null; tesseract "$WORK/$n-inv.png" - -l deu+eng 2>/dev/null) || true)
  hits=$( (printf '%s' "$text" | grep -o -i -E "$names"; printf '%s' "$text" | grep -o -E "$codes") || true)
  if [ -n "$hits" ]; then
    echo "$f: $(echo "$hits" | sort -u | tr '\n' ' ')"
    fail=1
  fi
done

if [ "$fail" -ne 0 ]; then
  echo "check-images: FAILED"; exit 1
fi
echo "check-images: OK ($n images)"
