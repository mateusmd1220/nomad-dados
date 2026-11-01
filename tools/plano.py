"""Which (region, kind) files this run generates. Writes `matriz=<json>` to $GITHUB_OUTPUT.

Manual run: REGIOES = ids separated by commas, or "todas"; TIPOS = mapa,lugares,rotas.
Schedule: AGENDA = the cron that fired. Monthly it refreshes the places files already published;
every 3 months the maps and routes already published.
"""
import json
import os
import sys

# What a free GitHub runner (4 CPUs, 16 GB RAM, ~70 GB disk) can build in under 6 hours.
LIMITE = {
    "mapa": lambda r: r["size"]["mapa"] is not None and r["size"]["mapa"] <= 30e9,
    "lugares": lambda r: r["size"]["lugares"] is not None and (r["id"] == "mundo" or (r.get("osm") or 0) <= 13e9),
    "rotas": lambda r: r["size"]["rotas"] is not None and r["id"] != "mundo",
}
MENSAL = "0 6 3 * *"  # keep in sync with .github/workflows/gerar.yml


def main():
    regions = json.load(open("regions.json", encoding="utf-8"))["regions"]
    agenda = os.environ.get("AGENDA", "")
    if agenda:
        tipos = ["lugares"] if agenda == MENSAL else ["mapa", "rotas"]
        catalogo = json.load(open("catalogo.json", encoding="utf-8")) if os.path.exists("catalogo.json") else {"items": []}
        publicados = {(i["kind"], i["id"]) for i in catalogo["items"]}
        escolhidas = [r for r in regions if any((t, r["id"]) in publicados for t in tipos)]
    else:
        tipos = [t.strip() for t in os.environ.get("TIPOS", "mapa,lugares,rotas").split(",") if t.strip()]
        pedidas = [x.strip() for x in os.environ.get("REGIOES", "").split(",") if x.strip()]
        if pedidas == ["todas"]:
            escolhidas = regions
        else:
            ids = {r["id"] for r in regions}
            desconhecidas = [x for x in pedidas if x not in ids]
            if desconhecidas:
                raise SystemExit(f"Regiões desconhecidas: {', '.join(desconhecidas)}")
            escolhidas = [r for r in regions if r["id"] in pedidas]
    matriz = []
    for r in escolhidas:
        for t in tipos:
            if t not in LIMITE:
                raise SystemExit(f"Tipo desconhecido: {t}")
            if agenda and (t, r["id"]) not in publicados:
                continue
            if LIMITE[t](r):
                matriz.append({"id": r["id"], "tipo": t})
            else:
                print(f"::warning::{t} de {r['id']}: grande demais para o GitHub gratuito, pulei", file=sys.stderr)
    linha = f"matriz={json.dumps(matriz)}"
    print(linha)
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as f:
            f.write(linha + "\n")


if __name__ == "__main__":
    main()
