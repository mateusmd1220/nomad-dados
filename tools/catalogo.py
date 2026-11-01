"""Rebuilds catalogo.json from the manifest.json of every <tipo>-<regiao> release.

The app reads this one file (raw.githubusercontent.com) to know what it can download.
Rebuilding from scratch every time makes it safe to run after any number of parallel jobs.
"""
import json
import os
import subprocess
import time
import urllib.request

REPO = os.environ.get("GITHUB_REPOSITORY", "mateusmd1220/nomad-dados")
BASE = f"https://github.com/{REPO}/releases/download"
KINDS = ("mapa", "lugares", "rotas")


def gh(*args):
    return subprocess.run(["gh", *args], check=True, capture_output=True, text=True).stdout


def main():
    releases = json.loads(gh("release", "list", "--repo", REPO, "--limit", "1000", "--json", "tagName"))
    items = []
    for rel in releases:
        tag = rel["tagName"]
        if tag.split("-", 1)[0] not in KINDS:
            continue
        try:
            with urllib.request.urlopen(f"{BASE}/{tag}/manifest.json", timeout=60) as r:
                m = json.load(r)
        except Exception as e:  # release still being published
            print(f"{tag}: sem manifest ({e})")
            continue
        items.append(m)
    items.sort(key=lambda m: (m["kind"], m["id"]))
    old = {}
    if os.path.exists("catalogo.json"):
        old = json.load(open("catalogo.json", encoding="utf-8"))
    if old.get("items") == items:
        print("catalogo.json sem mudanças")
        return
    out = {"version": 1, "updated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "base": BASE, "items": items}
    with open("catalogo.json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
        f.write("\n")
    print(f"catalogo.json: {len(items)} arquivos")


if __name__ == "__main__":
    main()
