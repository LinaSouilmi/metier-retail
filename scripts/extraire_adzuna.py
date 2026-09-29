r"""Récupère les offres Adzuna des métiers suivis et les enregistre dans data/adzuna/.

Deuxième canal, à côté de France Travail : Adzuna agrège des offres publiées sur d'autres
sites d'emploi. API : https://developer.adzuna.com — usage de recherche académique,
Adzuna doit être cité comme source partout où ses offres sont affichées.

Usage :
    python scripts/extraire_adzuna.py

Ce que ça écrit :
    data/adzuna/brut/<AAAA-MM>.jsonl   une ligne par offre (JSON tel que l'API le renvoie),
                                       écrite la première fois qu'on la voit, et de nouveau
                                       si son contenu a changé (même logique que extraire.py)
    data/adzuna/serie.csv              une ligne par requête et par jour : total annoncé,
                                       récupérées, nouvelles

Les identifiants sont lus dans l'environnement (secrets GitHub Actions ADZUNA_APP_ID et
ADZUNA_APP_KEY) ou dans le fichier .env. Sans eux, le script s'arrête sans erreur : la
collecte France Travail continue de tourner seule.

Compte gratuit : 25 appels par minute, 250 par jour, 1 000 par semaine, 2 500 par mois.
Avec REQUETES x PAGES_MAX = 35 appels par jour au plus, on reste à ~1 050 par mois.
"""
import csv
import hashlib
import json
import os
import sys
import time
from datetime import date
from pathlib import Path

import requests
from dotenv import load_dotenv

RACINE = Path(__file__).resolve().parent.parent
load_dotenv(RACINE / ".env")

URL = "https://api.adzuna.com/v1/api/jobs/fr/search/{page}"

# Requêtes : expression exacte cherchée dans l'annonce -> code ROME de rattachement.
# Le code ROME sert à ranger l'offre dans le filtre « métiers » du site ; resumer.py
# l'affine d'après l'intitulé pour les chefs de rayon (frais / alimentaire).
REQUETES = {
    "responsable de magasin": "D1302",
    "responsable de boutique": "D1302",
    "store manager": "D1302",
    "directeur de magasin": "D1504",
    "chef de rayon": "D1503",
    "chef de secteur": "D1510",
    "responsable de département": "D1509",
}
PAR_PAGE = 50           # maximum autorisé par l'API
PAGES_MAX = 5           # 250 offres par requête et par jour, les plus récentes d'abord
PAUSE = 2.6             # secondes entre deux appels : 25 par minute au plus

# Champs qui bougent sans que l'offre change : ignorés pour l'empreinte.
CHAMPS_VOLATILS = {"adref", "redirect_url"}


def empreinte(offre):
    stable = {k: v for k, v in offre.items() if k not in CHAMPS_VOLATILS}
    return hashlib.sha1(json.dumps(stable, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()[:16]


def versions_connues():
    vues = set()
    for f in (RACINE / "data" / "adzuna" / "brut").glob("*.jsonl"):
        with f.open(encoding="utf-8") as fh:
            for ligne in fh:
                if ligne.strip():
                    v = json.loads(ligne)
                    vues.add((v["id"], v["empreinte"]))
    return vues


def chercher(app_id, app_key, expression):
    """Pagine une requête ; renvoie (offres, total annoncé par l'API)."""
    offres, total = [], None
    for page in range(1, PAGES_MAX + 1):
        r = requests.get(URL.format(page=page), params={
            "app_id": app_id, "app_key": app_key,
            "what_phrase": expression,
            "results_per_page": PAR_PAGE,
            "sort_by": "date",
            "content-type": "application/json",
        }, timeout=30)
        time.sleep(PAUSE)
        if r.status_code != 200:
            raise RuntimeError(f"{r.status_code} : {r.text[:200]}")
        d = r.json()
        total = d.get("count", total)
        lot = d.get("results", [])
        offres.extend(lot)
        if len(lot) < PAR_PAGE or (total is not None and len(offres) >= total):
            break
    return offres, total


def main():
    app_id, app_key = os.getenv("ADZUNA_APP_ID"), os.getenv("ADZUNA_APP_KEY")
    if not app_id or not app_key:
        print("Adzuna : identifiants absents (ADZUNA_APP_ID / ADZUNA_APP_KEY), collecte ignorée.")
        return

    aujourdhui = f"{date.today():%Y-%m-%d}"
    dossier = RACINE / "data" / "adzuna"
    (dossier / "brut").mkdir(parents=True, exist_ok=True)
    vues = versions_connues()
    ids_connus = {i for i, _ in vues}

    lignes_serie = []
    with (dossier / "brut" / f"{aujourdhui[:7]}.jsonl").open("a", encoding="utf-8") as brut:
        for expression, rome in REQUETES.items():
            try:
                offres, total = chercher(app_id, app_key, expression)
            except (RuntimeError, requests.RequestException) as e:
                # Une requête qui échoue (quota, panne) ne doit pas faire échouer la veille.
                print(f"Adzuna : « {expression} » a échoué — {e}")
                continue
            nouvelles = 0
            for o in offres:
                oid = str(o.get("id"))
                e = empreinte(o)
                if (oid, e) in vues:
                    continue
                if oid not in ids_connus:
                    nouvelles += 1
                    ids_connus.add(oid)
                vues.add((oid, e))
                brut.write(json.dumps({"id": oid, "empreinte": e, "vu_le": aujourdhui, "requete": expression,
                                       "rome": rome, "offre": o}, ensure_ascii=False) + "\n")
            lignes_serie.append([aujourdhui, expression, total if total is not None else len(offres),
                                 len(offres), nouvelles])
            print(f"Adzuna  {expression:<28} {len(offres):4d} récupérées sur {total}, {nouvelles:4d} nouvelles")

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

    print(f"\nAdzuna {aujourdhui} : {sum(r[4] for r in lignes_serie)} nouvelles offres sur {len(lignes_serie)} requêtes.")


if __name__ == "__main__":
    sys.exit(main())
