#!/usr/bin/env bash
# Publishes one built file as release <tipo>-<regiao>: publicar.sh <tipo> <regiao> <arquivo> <fonte.txt>
# The file goes up in parts of 512 MiB (GitHub takes < 2 GiB per file; small parts also make a
# failed download on the phone cheap to redo), each with its SHA-256, plus manifest.json.
# The runner has ~110 GB free, so the file is simply split (needs twice its size on disk).
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

# Upload with curl (streams the file with an exact Content-Length), replacing an asset of the same name.
upload() {
  local name aid
  name=$(basename "$1")
  aid=$(gh api "repos/$GH_REPO/releases/$rid/assets" --paginate -q ".[] | select(.name == \"$name\") | .id")
  [ -z "$aid" ] || gh api -X DELETE "repos/$GH_REPO/releases/assets/$aid"
  local code
  code=$(curl -sS -o resp.json -w '%{http_code}' -X POST -T "$1" -H "Authorization: Bearer $GH_TOKEN"     -H "Content-Type: application/octet-stream" "https://uploads.github.com/repos/$GH_REPO/releases/$rid/assets?name=$name")
  [ "$code" = 201 ] && return 0
  echo "upload de $name ($(stat -c %s "$1") bytes): HTTP $code $(head -c 400 resp.json)"
  return 1
}

if ! gh release view "$tag" > /dev/null 2>&1; then
  retry gh release create "$tag" --latest=false --title "$tipo: $id" \
    --notes "Arquivo offline do NOMAD ($tipo, região $id). Baixe pelo app (Mapas → Regiões). Dados: OpenStreetMap (ODbL), Protomaps, GeoNames, OurAirports, Valhalla."
fi
antigos=$(gh release view "$tag" --json assets -q '.assets[].name')
rid=$(gh api "repos/$GH_REPO/releases/tags/$tag" -q .id)

: > partes.txt
split -b "$PART" -d -a 3 "$f" "$base.$ver.part"
rm -f "$f"
total=0
for name in "$base.$ver".part*; do
  psize=$(stat -c %s "$name")
  [ "$psize" -gt 0 ] || { echo "::error::parte vazia: $name"; exit 1; }
  total=$((total + psize))
  printf '%s %s %s\n' "$name" "$psize" "$(sha256sum "$name" | cut -d' ' -f1)" >> partes.txt
done
[ "$total" = "$size" ] || { echo "::error::partes somam $total, esperado $size"; exit 1; }
while read -r -u 3 name _; do
  retry upload "$name"
  rm "$name"
done 3< partes.txt

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
retry upload manifest.json

# Remove the parts of the previous version.
for a in $antigos; do
  [ "$a" = manifest.json ] && continue
  grep -q "^$a " partes.txt || retry gh release delete-asset "$tag" "$a" --yes
done
echo "Publicado: $tag ($n partes, $size bytes)"
