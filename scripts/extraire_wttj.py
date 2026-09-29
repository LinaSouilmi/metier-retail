r"""Récupère les offres Welcome to the Jungle des métiers du retail et les enregistre dans data/wttj/.

Troisième canal, avec l'accord de l'enseignant. La collecte reste dans ce que le robots.txt
du site autorise aux robots :
  - la liste des offres vient du plan du site publié pour les robots (sitemaps/index.xml.gz),
    pas de la recherche (les URL avec « ? » sont interdites aux robots, on n'en appelle aucune) ;
  - on ne lit que les pages d'offres (/fr/companies/<entreprise>/jobs/<offre>), autorisées ;
  - une page toutes les PAUSE secondes, au plus MAX_PAGES pages par jour, et une offre déjà
    lue n'est relue que si le plan du site la dit modifiée ;
  - on ne garde que des faits (intitulé, employeur, lieu, contrat, salaire, niveau demandé,
    dates, lien) : ni le texte de l'annonce, ni les logos. Les outils cités sont repérés
    dans le texte au moment de la lecture, avec la grille OUTILS de resumer.py.

Usage :
    python scripts/extraire_wttj.py

Ce que ça écrit :
    data/wttj/brut/<AAAA-MM>.jsonl   une ligne par offre lue (première lecture, puis à chaque
                                     modification signalée par le plan du site)
    data/wttj/actives/<date>.csv     les offres retail présentes dans le plan du site ce jour-là
    data/wttj/serie.csv              une ligne par jour : présentes, lues, nouvelles, erreurs
"""
import csv
import gzip
import html
import json
import re
import sys
import time
from datetime import date
from pathlib import Path

import requests

RACINE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RACINE / "scripts"))
from resumer import REGEX_OUTILS, normaliser  # noqa: E402  (une seule grille d'outils)

SITE = "https://www.welcometothejungle.com"
INDEX = SITE + "/sitemaps/index.xml.gz"
ENTETES = {"User-Agent": "Mozilla/5.0 (compatible; metier-retail-etudiant/1.0; "
                         "+https://github.com/LinaSouilmi/metier-retail)"}
PAUSE = 2.0             # secondes entre deux pages
MAX_PAGES = 300         # pages d'offres lues au plus par jour
MAX_ERREURS = 5         # erreurs d'affilée avant d'arrêter pour la journée (blocage, panne)

# Les métiers suivis, lus dans l'intitulé (ou dans l'adresse de l'offre, qui le reprend) :
# un poste d'encadrement + un lieu de vente.
POSTE = r"(responsable|directeur|directrice|manager|chef|cheffe|adjoint|adjointe|gerant|gerante)"
LIEU = r"(magasin|boutique|point de vente|rayon|rayons|caisse|caisses|drive|secteur|store)"
RETAIL = re.compile(rf"\b{POSTE}\b.*\b{LIEU}\b|\bstore manager\b")
PAS_RETAIL = re.compile(r"\bagence\b|\bbanque\b|\bentrepot\b|\blogistique\b|\bsecteur public\b|assurance"
                        r"|services a la personne|conformite|risques|informatique|\bbtp\b|industri|production"
                        r"|maintenance|\bsante\b|medico|\bsoins\b|\bhopital\b|\bmedical\b")

CONTRATS = {"full_time": "CDI", "temporary": "CDD", "internship": "STG", "apprenticeship": "CDD",
            "freelance": "FRE", "part_time": "CDI", "vie": "VIE", "other": None}
FORMATIONS = {"no_diploma": "< Bac", "cap": "< Bac", "bep": "< Bac", "bac": "Bac", "bac_2": "Bac+2",
              "bac_3": "Bac+3/4", "bac_4": "Bac+3/4", "bac_5": "Bac+5", "doctorate": "Bac+5"}
PERIODES = {"YEAR": 1, "YEARLY": 1, "MONTH": 12, "MONTHLY": 12, "HOUR": 1607, "HOURLY": 1607}


def rome(titre):
    """Code ROME de rattachement, d'après l'intitulé normalisé."""
    t = normaliser(titre)
    if "rayon" in t:
        if re.search(r"frais|fruits|legumes|boucherie|poissonnerie|fromage|charcuterie|traditionnel|boulangerie|patisserie", t):
            return "D1513"
        return "D1502" if re.search(r"alimentaire|pgc|epicerie|liquides|surgel", t) else "D1503"
    if "caisse" in t:
        return "D1508"
    if "secteur" in t:
        return "D1510"
    if "drive" in t:
        return "D1509"
    if re.search(r"directeur|directrice", t) and "boutique" not in t:
        return "D1504"
    return "D1302"


def lire_gz(url, s):
    r = s.get(url, timeout=60)
    r.raise_for_status()
    try:
        return gzip.decompress(r.content).decode("utf-8")
    except OSError:                   # déjà décompressé par le serveur
        return r.text


def offres_du_plan(s):
    """{url: lastmod} des offres retail en français listées dans le plan du site."""
    plans = [u for u in re.findall(r"<loc>([^<]+)</loc>", lire_gz(INDEX, s)) if "job-listings" in u]
    offres = {}
    for plan in plans:
        for bloc in re.findall(r"<url>(.*?)</url>", lire_gz(plan, s), re.S):
            loc = re.search(r"<loc>([^<]+)</loc>", bloc)
            if not loc or "/fr/companies/" not in loc.group(1) or "/jobs/" not in loc.group(1):
                continue
            url = loc.group(1).strip()
            slug = normaliser(url.rsplit("/jobs/", 1)[1].split("_")[0])
            if RETAIL.search(slug) and not PAS_RETAIL.search(slug):
                mod = re.search(r"<lastmod>([^<]+)</lastmod>", bloc)
                offres[url] = mod.group(1)[:19] if mod else ""
        time.sleep(PAUSE)
    return offres


def champ(page, cle):
    """Valeur simple d'une clé des données de la page (JSON échappé dans le HTML)."""
    m = re.search(r'\\"' + cle + r'\\":(\\"([^"\\]*)\\"|[\w.-]+)', page)
    if not m:
        return None
    return m.group(2) if m.group(2) is not None else m.group(1)


def lire_offre(url, s):
    """Les faits d'une offre, lus dans sa page ; None si elle n'est plus en ligne."""
    r = s.get(url, timeout=30)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    page = r.text
    job = None
    for bloc in re.findall(r'<script[^>]*application/ld\+json[^>]*>(.*?)</script>', page, re.S):
        try:
            d = json.loads(bloc)
        except ValueError:
            continue
        if isinstance(d, dict) and d.get("@type") == "JobPosting":
            job = d
    if not job:
        return None
    lieu = ((job.get("jobLocation") or [{}])[0] or {}).get("address") or {}
    smin = smax = None
    sal = (job.get("baseSalary") or {}).get("value") or {}
    mult = PERIODES.get(str(sal.get("unitText") or "").upper())
    if mult:
        vals = [v * mult for v in (sal.get("minValue"), sal.get("maxValue")) if isinstance(v, (int, float)) and v]
        vals = [v for v in vals if 4000 <= v <= 250000]
        if vals:
            smin, smax = round(min(vals)), round(max(vals))
    texte = html.unescape(re.sub(r"<[^>]+>", " ", job.get("description") or ""))
    t = (str(job.get("title") or "") + " " + texte).lower()
    # L'expérience demandée n'est pas lue : le champ de la page ne correspond pas toujours à ce
    # qu'affiche l'offre (« 1 » pour « > 5 ans »). Mieux vaut « non précisé » qu'un chiffre faux.
    remote = champ(page, "remote")
    contrat = champ(page, "contract_type")
    return {
        "titre": html.unescape(str(job.get("title") or "")).strip(),
        "entreprise": ((job.get("hiringOrganization") or {}).get("name") or "").strip() or None,
        "ville": lieu.get("addressLocality"),
        "code_postal": lieu.get("postalCode"),
        "pays": lieu.get("addressCountry"),
        "contrat": CONTRATS.get(contrat, None) if contrat else {"FULL_TIME": "CDI", "TEMPORARY": "CDD"}.get(job.get("employmentType")),
        "contrat_wttj": contrat or job.get("employmentType"),
        "alternance": contrat == "apprenticeship",
        "smin": smin, "smax": smax,
        "formation": FORMATIONS.get(champ(page, "education_level") or ""),
        "teletravail": remote in ("partial", "fulltime", "punctual"),
        "secteur": job.get("industry"),
        "publiee": (job.get("datePosted") or "")[:10],
        "expire": (job.get("validThrough") or "")[:10],
        "outils": [nom for nom, rx in REGEX_OUTILS.items() if rx.search(t)],
    }


def main():
    aujourdhui = f"{date.today():%Y-%m-%d}"
    dossier = RACINE / "data" / "wttj"
    for d in ("brut", "actives"):
        (dossier / d).mkdir(parents=True, exist_ok=True)
    s = requests.Session()
    s.headers.update(ENTETES)

    try:
        plan = offres_du_plan(s)
    except requests.RequestException as e:
        print(f"WTTJ : plan du site illisible — {e}")
        return
    print(f"WTTJ : {len(plan)} offres retail dans le plan du site")

    # Dernière lecture connue de chaque offre : on ne relit que les nouvelles et les modifiées.
    lues = {}
    for f in sorted((dossier / "brut").glob("*.jsonl")):
        with f.open(encoding="utf-8") as fh:
            for ligne in fh:
                if ligne.strip():
                    v = json.loads(ligne)
                    lues[v["url"]] = v.get("lastmod", "")
    # Offres déjà lues et écartées (hors retail, hors France, retirées) : on ne les relit pas.
    fichier_ecartees = dossier / "ecartees.txt"
    ecartees = set(fichier_ecartees.read_text(encoding="utf-8").split()) if fichier_ecartees.exists() else set()
    a_lire = [u for u, mod in plan.items()
              if u not in ecartees and (u not in lues or (mod and mod > lues[u]))]
    a_lire.sort(key=lambda u: u in lues)          # les nouvelles d'abord
    print(f"WTTJ : {len(a_lire)} à lire (nouvelles ou modifiées), {min(len(a_lire), MAX_PAGES)} aujourd'hui")

    nouvelles = lu = erreurs = suite = 0
    retirees = set()
    with (dossier / "brut" / f"{aujourdhui[:7]}.jsonl").open("a", encoding="utf-8") as brut:
        for url in a_lire[:MAX_PAGES]:
            try:
                o = lire_offre(url, s)
                suite = 0
            except requests.RequestException as e:
                erreurs += 1
                suite += 1
                print(f"WTTJ : {url} — {e}")
                if suite >= MAX_ERREURS:
                    print("WTTJ : trop d'erreurs d'affilée, arrêt pour aujourd'hui.")
                    break
                time.sleep(PAUSE)
                continue
            time.sleep(PAUSE)
            if o is None or o.get("pays") not in (None, "FR") or not RETAIL.search(normaliser(o["titre"])):
                retirees.add(url)
                continue
            lu += 1
            nouvelles += url not in lues
            brut.write(json.dumps({"url": url, "lastmod": plan[url], "vu_le": aujourdhui,
                                   "rome": rome(o["titre"]), "offre": o}, ensure_ascii=False) + "\n")
            lues[url] = plan[url]

    ecartees |= retirees
    fichier_ecartees.write_text("\n".join(sorted(u for u in ecartees if u in plan)), encoding="utf-8")

    # Actives du jour : présentes dans le plan, déjà lues au moins une fois, pas écartées.
    with (dossier / "actives" / f"{aujourdhui}.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["url", "lastmod"])
        w.writerows(sorted((u, m) for u, m in plan.items() if u in lues and u not in ecartees))

    serie = dossier / "serie.csv"
    lignes = []
    if serie.exists():
        with serie.open(encoding="utf-8") as f:
            lignes = [r for r in csv.reader(f)][1:]
    lignes = [r for r in lignes if r[0] != aujourdhui] + [[aujourdhui, len(plan), lu, nouvelles, erreurs]]
    with serie.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["date", "dans_le_plan", "lues", "nouvelles", "erreurs"])
        w.writerows(sorted(lignes))
    print(f"WTTJ {aujourdhui} : {lu} offres lues ({nouvelles} nouvelles), {len(retirees)} écartées, {erreurs} erreurs.")


if __name__ == "__main__":
    main()
