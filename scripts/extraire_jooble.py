r"""Récupère les offres Jooble des métiers suivis et les enregistre dans data/jooble/.

Cinquième canal : l'API REST de Jooble (agrégateur d'offres), https://jooble.org/api/about —
clé obtenue sur demande, à mettre dans le secret JOOBLE_API_KEY. Une requête = un POST JSON
{"keywords", "location", "page"} sur https://jooble.org/api/<clé>.

Usage :
    python scripts/extraire_jooble.py

Ce que ça écrit :
    data/jooble/brut/<AAAA-MM>.jsonl   une ligne par offre (JSON de l'API), écrite la première
                                       fois qu'on la voit et de nouveau si elle change
    data/jooble/actives/<date>.csv     les offres vues ce jour-là : id, code ROME
    data/jooble/serie.csv              une ligne par requête et par jour : total, récupérées, nouvelles
"""
import csv
import hashlib
import json
import os
import time
from datetime import date
from pathlib import Path

import requests
from dotenv import load_dotenv

RACINE = Path(__file__).resolve().parent.parent
load_dotenv(RACINE / ".env")

URL = os.getenv("JOOBLE_URL", "https://jooble.org/api/")
# Jooble ne comprend pas « France » comme lieu : il ne renvoie alors que des offres sans lieu
# précis, sans rapport avec les mots-clés (essai du 05/10/2026 : 0 offre retail sur 141).
# On cherche donc ville par ville, dans un rayon de 40 km ; resumer.py ne garde ensuite que les
# intitulés de retail (TITRE_RETAIL).
VILLES = ["Paris", "Lyon", "Marseille", "Toulouse", "Lille", "Bordeaux", "Nantes", "Strasbourg",
          "Montpellier", "Rennes", "Nice", "Clermont-Ferrand"]
RAYON_KM = "40"
# Requête (mots-clés) -> code ROME de rattachement, comme pour Adzuna.
REQUETES = {
    "responsable de magasin": "D1302",
    "directeur de magasin": "D1504",
    "manager de rayon": "D1503",
    "chef de secteur magasin": "D1510",
}
PAGES_MAX = 1           # page de résultats par requête, par ville et par jour
PAR_PAGE = 50
PAUSE = 1.5             # secondes entre deux appels


def empreinte(o):
    stable = {k: v for k, v in o.items() if k not in ("updated",)}
    return hashlib.sha1(json.dumps(stable, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()[:16]


def main():
    cle = os.getenv("JOOBLE_API_KEY")
    if not cle:
        print("Jooble : clé absente (JOOBLE_API_KEY), collecte ignorée.")
        return
    aujourdhui = f"{date.today():%Y-%m-%d}"
    dossier = RACINE / "data" / "jooble"
    for d in ("brut", "actives"):
        (dossier / d).mkdir(parents=True, exist_ok=True)

    vues = set()
    for f in (dossier / "brut").glob("*.jsonl"):
        with f.open(encoding="utf-8") as fh:
            for ligne in fh:
                if ligne.strip():
                    v = json.loads(ligne)
                    vues.add((v["id"], v["empreinte"]))
    ids_connus = {i for i, _ in vues}

    actives, lignes_serie = {}, []
    with (dossier / "brut" / f"{aujourdhui[:7]}.jsonl").open("a", encoding="utf-8") as brut:
        for (requete, rome), ville in ((rq, v) for rq in REQUETES.items() for v in VILLES):
            total, recuperees, nouvelles = None, 0, 0
            for page in range(1, PAGES_MAX + 1):
                try:
                    r = requests.post(URL + cle, json={"keywords": requete, "location": ville, "radius": RAYON_KM,
                                                       "page": str(page), "ResultOnPage": str(PAR_PAGE)},
                                      timeout=30)
                    time.sleep(PAUSE)
                    r.raise_for_status()
                    d = r.json()
                except (requests.RequestException, ValueError) as e:
                    print(f"Jooble : « {requete} » {ville} page {page} a échoué — {e}", flush=True)
                    break
                total = d.get("totalCount", total)
                lot = d.get("jobs") or []
                if page == 1:                     # de quoi vérifier dans le journal que la recherche porte
                    print(f"Jooble  {requete} / {ville} : " + " | ".join(
                        f"{o.get('title')} ({o.get('location')})" for o in lot[:3]), flush=True)
                for o in lot:
                    oid = str(o.get("id") or o.get("link"))
                    if oid in actives:
                        continue
                    recuperees += 1
                    actives[oid] = rome
                    e = empreinte(o)
                    if (oid, e) not in vues:
                        nouvelles += oid not in ids_connus
                        ids_connus.add(oid)
                        vues.add((oid, e))
                        brut.write(json.dumps({"id": oid, "empreinte": e, "vu_le": aujourdhui, "requete": requete,
                                               "ville": ville, "rome": rome, "offre": o}, ensure_ascii=False) + "\n")
                if not lot:
                    break
            lignes_serie.append([aujourdhui, f"{requete} / {ville}", total if total is not None else "",
                                 recuperees, nouvelles])
            print(f"Jooble  {requete} / {ville} : {recuperees} récupérées sur {total}, {nouvelles} nouvelles", flush=True)

    with (dossier / "actives" / f"{aujourdhui}.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["id", "rome"])
        w.writerows(sorted(actives.items()))

    serie = dossier / "serie.csv"
    lignes = []
    if serie.exists():
        with serie.open(encoding="utf-8") as f:
            lignes = [r for r in csv.reader(f)][1:]
    faites = {r[1] for r in lignes_serie}
    lignes = [r for r in lignes if not (r[0] == aujourdhui and r[1] in faites)] + lignes_serie
    with serie.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["date", "requete", "total", "recuperees", "nouvelles"])
        w.writerows(sorted(lignes))
    print(f"\nJooble {aujourdhui} : {len(actives)} offres sur {len(lignes_serie)} requêtes.", flush=True)


if __name__ == "__main__":
    main()
