#!/usr/bin/env bash
# Builds one offline file: gerar.sh <tipo> <regiao> <pasta de trabalho>
# Leaves <pasta>/out/<arquivo> and <pasta>/out/fonte.txt (where the data came from).
set -euo pipefail
tipo=$1 id=$2 w=$3
mkdir -p "$w/out" "$w/src"
reg() { jq -r --arg id "$id" ".regions[] | select(.id == \$id) | $1" regions.json; }
[ -n "$(reg .id)" ] || { echo "::error::Região desconhecida: $id"; exit 1; }
geofabrik=$(reg '.geofabrik // empty')
osm=$(reg '.osm // 0')

baixar_osm() {
  # Geofabrik caps each connection: many connections, then check the MD5.
  local url
  url=$(curl -fsSI "https://download.geofabrik.de/${geofabrik}-latest.osm.pbf" | awk 'tolower($1)=="location:"{print $2}' | tr -d '\r')
  [ -n "$url" ] || url="https://download.geofabrik.de/${geofabrik}-latest.osm.pbf"
  case "$url" in http*) ;; *) url="https://download.geofabrik.de/$(dirname "$geofabrik")/$url" ;; esac
  echo "OpenStreetMap: $url"
  aria2c -q -x 16 -s 16 --max-tries=10 -d "$w/src" -o region.osm.pbf "$url"
  local want got
  want=$(curl -fsSL "$url.md5" | cut -d' ' -f1)
  got=$(md5sum "$w/src/region.osm.pbf" | cut -d' ' -f1)
  [ "$want" = "$got" ] || { echo "::error::MD5 do OpenStreetMap não confere"; exit 1; }
  echo "$(basename "$url") (OpenStreetMap, ODbL)" > "$w/out/fonte.txt"
}

case "$tipo" in
  mapa)
    curl -fsSL https://github.com/protomaps/go-pmtiles/releases/download/v1.31.2/go-pmtiles_1.31.2_Linux_x86_64.tar.gz | tar -xz -C "$w/src" pmtiles
    day=$(date -u +%s)
    for _ in $(seq 0 14); do
      url="https://build.protomaps.com/$(date -u -d "@$day" +%Y%m%d).pmtiles"
      curl -fsI "$url" > /dev/null && break
      day=$((day - 86400))
    done
    echo "Protomaps: $url"
    if [ "$id" = mundo ]; then
      opts=(--maxzoom=9)
    else
      opts=("--bbox=$(reg '(.mapBbox // .bbox) | map(tostring) | join(",")')")
    fi
    "$w/src/pmtiles" extract "$url" "$w/out/$id.pmtiles" "${opts[@]}" --download-threads=8 --quiet
    echo "Protomaps $(basename "$url" .pmtiles) (OpenStreetMap, ODbL)" > "$w/out/fonte.txt"
    ;;

  lugares)
    python3 -m pip install -q osmium
    out="$w/out/lugares-$id.sqlite"
    if [ "$id" = mundo ]; then
      for f in cities1000.zip admin1CodesASCII.txt countryInfo.txt; do
        curl -fsSL --retry 5 -o "$w/src/$f" "https://download.geonames.org/export/dump/$f"
      done
      curl -fsSL --retry 5 -o "$w/src/airports.csv" https://davidmegginson.github.io/ourairports-data/airports.csv
      python3 build_places.py --world "$w/src" --out "$out"
      echo "GeoNames (CC BY 4.0) e OurAirports (domínio público), $(date -u +%F)" > "$w/out/fonte.txt"
    else
      baixar_osm
      index=flex_mem
      # Big extracts: keep the node locations on disk instead of in the 16 GB of RAM.
      [ "${osm%.*}" -gt 3000000000 ] && index="sparse_file_array,$w/src/nodes.idx"
      python3 build_places.py --osm "$w/src/region.osm.pbf" --region "$id" --out "$out" --index "$index"
    fi
    ;;

  rotas)
    baixar_osm
    image=ghcr.io/valhalla/valhalla:3.9.0  # same version as the engine in the app
    chmod 777 "$w/src"
    docker run --rm -u "$(id -u):$(id -g)" -v "$w/src:/data" -w /data "$image" bash -euc '
      valhalla_build_config \
        --mjolnir-tile-dir /data/tiles \
        --mjolnir-tile-extract /data/valhalla_tiles.tar \
        --mjolnir-admin /data/admins.sqlite \
        --mjolnir-concurrency "$(nproc)" > /data/valhalla.json
      valhalla_build_admins -c /data/valhalla.json /data/region.osm.pbf
      valhalla_build_tiles -c /data/valhalla.json /data/region.osm.pbf
      valhalla_build_extract -c /data/valhalla.json -v --overwrite
    '
    mv "$w/src/valhalla_tiles.tar" "$w/out/rotas-$id.tar"
    sed -i 's/$/; Valhalla 3.9.0/' "$w/out/fonte.txt"
    ;;

  *) echo "::error::Tipo desconhecido: $tipo"; exit 1 ;;
esac
rm -rf "$w/src"
ls -la "$w/out"
