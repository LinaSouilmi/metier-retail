r"""Récupère les offres en alternance de La bonne alternance et les enregistre dans data/lba/.

Quatrième canal : l'API publique de La bonne alternance (État, licence Etalab 2.0),
https://api.apprentissage.beta.gouv.fr — clé d'API dans le secret LBA_API_KEY.

La recherche renvoie au plus 150 offres par source et ne se pagine pas. Pour ne rien
perdre, on interroge métier par métier ; si une source atteint le plafond, on redemande
par groupes de départements, puis département par département.
Les offres France Travail qu'elle relaie sont écartées : on les a déjà par extraire.py.

Usage :
    python scripts/extraire_lba.py

Ce que ça écrit :
    data/lba/brut/<AAAA-MM>.jsonl   une ligne par offre (JSON de l'API), écrite la première
                                    fois qu'on la voit et de nouveau si elle change
    data/lba/actives/<date>.csv     les offres vues ce jour-là : id, code ROME demandé
    data/lba/serie.csv              une ligne par métier et par jour : offres, nouvelles, appels
"""
import csv
import hashlib
import json
import os
import re
import sys
import time
from datetime import date
from pathlib import Path

import requests
from dotenv import load_dotenv

RACINE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RACINE / "scripts"))
from extraire import METIERS  # noqa: E402  (les mêmes métiers que France Travail)

load_dotenv(RACINE / ".env")
URL = "https://api.apprentissage.beta.gouv.fr/api/job/v1/search"
PLAFOND = 150           # offres au plus par source et par requête (documentation de l'API)
PAUSE = 1.1             # 60 appels par minute au plus
FRANCE_TRAVAIL = re.compile(r"france.?travail|pole.?emploi", re.I)

DEPARTEMENTS = ([f"{n:02d}" for n in range(1, 96) if n != 20] + ["2A", "2B"]
                + ["971", "972", "973", "974", "976"])
GROUPES = [DEPARTEMENTS[i:i + 10] for i in range(0, len(DEPARTEMENTS), 10)]


class Client:
    def __init__(self, cle):
        self.s = requests.Session()
        self.s.headers["Authorization"] = f"Bearer {cle}"
        self.appels = 0

    def chercher(self, rome, departements=None):
        """Offres d'une requête ; réessaie en respectant retry-after si le quota est atteint."""
        params = [("romes", rome)] + [("departements", d) for d in departements or []]
        for essai in range(4):
            r = self.s.get(URL, params=params, timeout=60)
            self.appels += 1
            time.sleep(PAUSE)
            if r.status_code in (419, 429):
                time.sleep(int(r.headers.get("retry-after") or 30))
                continue
            r.raise_for_status()
            return r.json().get("jobs") or []
        raise RuntimeError("quota dépassé")


def sature(offres):
    """Une source a-t-elle atteint le plafond de 150 (donc des offres manquent) ?"""
    par_source = {}
    for o in offres:
        lab = (o.get("identifier") or {}).get("partner_label") or "?"
        par_source[lab] = par_source.get(lab, 0) + 1
    return any(n >= PLAFOND for n in par_source.values())


def offres_du_metier(client, rome):
    """Toutes les offres d'un métier, en découpant par départements si nécessaire."""
    offres = client.chercher(rome)
    if not sature(offres):
        return offres
    offres = []
    for groupe in GROUPES:
        lot = client.chercher(rome, groupe)
        if sature(lot):
            lot = [o for d in groupe for o in client.chercher(rome, [d])]
        offres.extend(lot)
    return offres


def identifiant(o):
    i = o.get("identifier") or {}
    return i.get("id") or f"{i.get('partner_label')}:{i.get('partner_job_id')}"


def empreinte(o):
    return hashlib.sha1(json.dumps(o, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()[:16]


def main():
    cle = os.getenv("LBA_API_KEY")
    if not cle:
        print("La bonne alternance : clé absente (LBA_API_KEY), collecte ignorée.")
        return
    aujourdhui = f"{date.today():%Y-%m-%d}"
    dossier = RACINE / "data" / "lba"
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

    client = Client(cle)
    actives, lignes_serie = {}, []
    with (dossier / "brut" / f"{aujourdhui[:7]}.jsonl").open("a", encoding="utf-8") as brut:
        for rome, (libelle, _, _) in METIERS.items():
            avant = client.appels
            try:
                offres = offres_du_metier(client, rome)
            except (requests.RequestException, RuntimeError) as e:
                print(f"LBA  {rome} a échoué — {e}", flush=True)
                continue
            nouvelles = retenues = 0
            for o in offres:
                if FRANCE_TRAVAIL.search((o.get("identifier") or {}).get("partner_label") or ""):
                    continue
                oid = identifiant(o)
                if oid in actives:                  # déjà vue sous un autre métier aujourd'hui
                    continue
                retenues += 1
                actives[oid] = rome
                e = empreinte(o)
                if (oid, e) not in vues:
                    nouvelles += oid not in ids_connus
                    ids_connus.add(oid)
                    vues.add((oid, e))
                    brut.write(json.dumps({"id": oid, "empreinte": e, "vu_le": aujourdhui, "rome": rome,
                                           "offre": o}, ensure_ascii=False) + "\n")
            lignes_serie.append([aujourdhui, rome, retenues, nouvelles, client.appels - avant])
            print(f"LBA  {rome}  {libelle:<52} {retenues:5d} offres, {nouvelles:4d} nouvelles "
                  f"({client.appels - avant} appels)", flush=True)

    with (dossier / "actives" / f"{aujourdhui}.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["id", "rome"])
        w.writerows(sorted(actives.items()))

    serie = dossier / "serie.csv"
    lignes = []
    if serie.exists():
        with serie.open(encoding="utf-8") as f:
            lignes = [r for r in csv.reader(f)][1:]
    faits = {r[1] for r in lignes_serie}
    lignes = [r for r in lignes if not (r[0] == aujourdhui and r[1] in faits)] + lignes_serie
    with serie.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["date", "rome", "offres", "nouvelles", "appels"])
        w.writerows(sorted(lignes))
    print(f"\nLBA {aujourdhui} : {len(actives)} offres en alternance (hors France Travail), "
          f"{client.appels} appels.", flush=True)


if __name__ == "__main__":
    main()
