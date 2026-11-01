#!/usr/bin/env bash
# Publishes one built file as release <tipo>-<regiao>: publicar.sh <tipo> <regiao> <arquivo> <fonte.txt>
# The file goes up in parts of 512 MiB (GitHub takes < 2 GiB per file; small parts also make a
# failed download on the phone cheap to redo), each with its SHA-256, plus manifest.json.
# Parts are cut from the end of the file, so the disk never holds much more than the file itself.
set -euo pipefail
tipo=$1 id=$2 f=$3 fonte=$(cat "$4")
tag="$tipo-$id"
base=$(basename "$f")
ver=$(date -u +%Y%m%d)
size=$(stat -c %s "$f")
sha=$(sha256sum "$f" | cut -d' ' -f1)
PART=$((512 * 1024 * 1024))
n=$(((size + PART - 1) / PART))

retry() { for i in 1 2 3 4 5; do "$@" && return 0; echo "tentativa $i falhou: $*"; sleep $((i * 20)); done; return 1; }

if ! gh release view "$tag" > /dev/null 2>&1; then
  retry gh release create "$tag" --latest=false --title "$tipo: $id" \
    --notes "Arquivo offline do NOMAD ($tipo, região $id). Baixe pelo app (Mapas → Regiões). Dados: OpenStreetMap (ODbL), Protomaps, GeoNames, OurAirports, Valhalla."
fi
antigos=$(gh release view "$tag" --json assets -q '.assets[].name')

: > partes.txt
for ((i = n - 1; i >= 0; i--)); do
  off=$((i * PART))
  name=$(printf '%s.%s.part%03d' "$base" "$ver" "$i")
  tail -c +$((off + 1)) "$f" > "$name"
  truncate -s "$off" "$f"
  printf '%s %s %s\n' "$name" "$(stat -c %s "$name")" "$(sha256sum "$name" | cut -d' ' -f1)" >> partes.txt
  retry gh release upload "$tag" "$name" --clobber
  rm "$name"
done
rm -f "$f"

python3 - "$tipo" "$id" "$base" "$size" "$sha" "$fonte" > manifest.json <<'EOF'
import json, sys, time
tipo, id_, base, size, sha, fonte = sys.argv[1:]
parts = [dict(zip(("name", "size", "sha256"), l.split())) for l in open("partes.txt")]
parts.sort(key=lambda p: p["name"])
for p in parts:
    p["size"] = int(p["size"])
assert sum(p["size"] for p in parts) == int(size)
print(json.dumps({"kind": tipo, "id": id_, "file": base, "version": time.strftime("%Y-%m-%d", time.gmtime()),
                  "size": int(size), "sha256": sha, "source": fonte, "parts": parts}, ensure_ascii=False, indent=1))
EOF
retry gh release upload "$tag" manifest.json --clobber

# Remove the parts of the previous version.
for a in $antigos; do
  [ "$a" = manifest.json ] && continue
  grep -q "^$a " partes.txt || retry gh release delete-asset "$tag" "$a" --yes
done
echo "Publicado: $tag ($n partes, $size bytes)"
