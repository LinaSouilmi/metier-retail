r"""Lit les offres actives du jour (data/actives/<date>.csv), retrouve leur dernière version dans
data/brut, et écrit data/resume.json : le fichier que la page index.html affiche.

Usage :
    .venv\Scripts\python.exe scripts\resumer.py

C'est ici que la donnée brute est retravaillée :
  - salaire : libellé texte -> minimum et maximum annuels bruts ;
  - outils cités dans l'intitulé + la description (grille OUTILS, à adapter à votre métier) ;
  - position sur la carte : latitude/longitude de l'API quand elle les donne, sinon le centre
    de la commune (geo.api.gouv.fr, mis en cache dans data/geo/), sinon la ville principale
    du département ; les offres « France » n'ont pas de point.
  - niveau de poste déduit de l'intitulé (assistant / chargé / responsable / directeur / autre),
    nature du contrat (apprentissage, professionnalisation, salarié, non salarié) et libellés
    lisibles des codes de contrat (clé « contrats » du résumé).
  - exigences : exp_exige, exp_ans (années, 0 = débutant accepté), qualification, formation
    (niveau le plus élevé demandé), secteur, temps (plein/partiel), postes.
La page recalcule ensuite tous les comptages côté navigateur, selon les métiers cochés.
"""
import csv
import html
import json
import math
import re
import sys
import time
import unicodedata
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

import requests

RACINE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RACINE / "scripts"))
from extraire import METIERS  # noqa: E402  (la liste des métiers vit dans un seul fichier)

# Les outils et compétences que l'on cherche dans les annonces : c'est VOTRE grille, adaptez-la.
# Chaque entrée : libellé affiché -> variantes cherchées (mot entier, insensible à la casse).
OUTILS = {
    "Management d'équipe": ["management d'équipe", "manager une équipe", "encadrer une équipe",
                            "encadrement d'équipe", "animer une équipe", "management"],
    "Recrutement / formation": ["recrutement", "recruter", "former vos équipes", "formation des équipes"],
    "Plannings": ["planning", "plannings"],
    "Pilotage du CA / KPI": ["chiffre d'affaires", "kpi", "indicateurs", "tableaux de bord", "tableau de bord"],
    "Compte d'exploitation / P&L": ["compte d'exploitation", "p&l", "marge", "rentabilité"],
    "Gestion des stocks": ["gestion des stocks", "stocks", "inventaire", "inventaires", "approvisionnement", "réassort"],
    "Merchandising": ["merchandising", "visual merchandising", "implantation", "facing", "mise en rayon"],
    "Expérience client": ["expérience client", "satisfaction client", "relation client", "parcours client"],
    "Fidélisation / CRM": ["fidélisation", "carte de fidélité", "crm"],
    "Omnicanal / e-commerce": ["omnicanal", "omnicanalité", "click & collect", "click and collect",
                               "drive", "e-commerce", "ecommerce", "web-to-store"],
    "Animations commerciales": ["animation commerciale", "animations commerciales", "opérations commerciales",
                                "promotions", "actions commerciales"],
    "Négociation fournisseurs": ["fournisseurs", "négociation", "achats"],
    "Hygiène / sécurité": ["haccp", "hygiène", "sécurité des biens", "normes de sécurité"],
    "Réseaux sociaux": ["réseaux sociaux", "instagram", "facebook", "social media"],
    "Excel": ["excel"],
    "ERP / SAP": ["erp", "sap"],
    "Power BI": ["power bi"],
    "IA générative": ["ia", "intelligence artificielle", "chatgpt", "ia générative"],
    "Anglais": ["anglais", "english"],
    "Permis B": ["permis b", "véhiculé", "véhiculée"],
}
REGEX_OUTILS = {nom: re.compile(r"(?<![\w-])(" + "|".join(re.escape(v) for v in variantes) + r")(?![\w-])")
                for nom, variantes in OUTILS.items()}

GEO = "https://geo.api.gouv.fr"

# Niveau du poste, lu dans l'intitulé : l'ordre compte (un « directeur marketing » n'est pas
# un « chargé »). Première expression qui correspond, en minuscules.
NIVEAUX = [
    ("directeur", r"directeur|directrice|\bhead of\b|\bcdo\b|\bcmo\b|\bvp\b"),
    ("responsable", r"responsable|manager|\bchef|\bcheffe|\blead\b|\bhead\b|gérant|gerant"),
    ("assistant", r"assistant|alternan|apprenti|stagiaire|\bstage\b|junior"),
    ("charge", r"charg[ée]|vendeu|employé|hôte|conseill|consultant|analyste|analyst|spécialiste|specialist"
                r"|expert|technicien|conseiller|animateur|référenceur|rédacteur|designer"
                r"|développeur|business developer|ingénieur|gestionnaire|coordinateur|superviseur"),
]
REGEX_NIVEAUX = [(cle, re.compile(motif, re.IGNORECASE)) for cle, motif in NIVEAUX]
NIVEAUX_LIBELLES = [
    ["assistant", "Assistant·e / junior"],
    ["charge", "Chargé·e / employé·e"],
    ["responsable", "Responsable"],
    ["directeur", "Directeur·rice"],
    ["autre", "Autre"],
]

# Codes de type de contrat de l'API -> libellé court lisible par un étudiant.
CONTRATS = {
    "CDI": "CDI",
    "CDD": "CDD",
    "MIS": "Intérim",
    "SAI": "Saisonnier",
    "FRA": "Franchise",
    "LIB": "Profession libérale",
    "CCE": "Profession commerciale",
    "DDI": "CDI de chantier",
    "DIN": "CDI intérimaire",
    "TTI": "Intérim",
    "CDS": "CDD senior",
    "REP": "Reprise d'entreprise",
    "STG": "Stage",
    "FRE": "Freelance",
    "VIE": "VIE",
}

NATURES = [
    ("apprentissage", "apprentissage"),
    ("professionnalisation", "professionnalisation"),
    ("non salarié", "non_salarie"),
    ("contrat travail", "salarie"),
]

# Niveau de formation demandé : du plus faible au plus élevé (l'ordre sert aussi à l'affichage).
FORMATIONS = ["< Bac", "Bac", "Bac+2", "Bac+3/4", "Bac+5"]


def niveau(intitule):
    """'Directeur marketing' -> 'directeur' ; 'Chargé de com' -> 'charge' ; sinon 'autre'."""
    t = intitule or ""
    for cle, rx in REGEX_NIVEAUX:
        if rx.search(t):
            return cle
    return "autre"


def contrat_libelle(code):
    """Code de contrat de l'API -> libellé court ; les codes inconnus restent identifiables."""
    return CONTRATS.get(code) or f"Autre ({code})"


def nature(o):
    """natureContrat -> 'apprentissage' | 'professionnalisation' | 'salarie' | 'non_salarie' | 'autre'."""
    lib = (o.get("natureContrat") or "").lower()
    if not lib:
        return "autre"
    for motif, cle in NATURES:
        if motif in lib:
            return cle
    return "autre"


def exp_ans(lib):
    """'Débutant accepté'/'0 An(s)' -> 0, '6 Mois' -> 0.5, '5 An(s)' -> 5, 'Expérience exigée' -> None."""
    l = (lib or "").lower()
    if not l:
        return None
    if "débutant" in l or "debutant" in l:
        return 0
    m = re.search(r"(\d+(?:[.,]\d+)?)\s*(an|mois)", l)
    if not m:
        return None
    n = float(m.group(1).replace(",", "."))
    n = n if m.group(2) == "an" else n / 12
    return int(n) if n == int(n) else round(n, 2)


def formation(o):
    """Niveau de formation le plus élevé demandé par l'offre, ou None si rien n'est indiqué."""
    meilleur = None
    for f in o.get("formations") or []:
        l = (f.get("niveauLibelle") or "").lower()
        if not l:
            continue
        if "bac+5" in l or "bac + 5" in l:
            n = "Bac+5"
        elif "bac+3" in l or "bac+4" in l or "bac + 3" in l or "bac + 4" in l:
            n = "Bac+3/4"
        elif "bac+2" in l or "bac + 2" in l:
            n = "Bac+2"
        elif "bac" in l:
            n = "Bac"
        else:
            n = "< Bac"
        if meilleur is None or FORMATIONS.index(n) > FORMATIONS.index(meilleur):
            meilleur = n
    return meilleur


def temps_travail(o):
    """'Temps plein' -> 'plein', 'Temps partiel' -> 'partiel', sinon None."""
    l = (o.get("dureeTravailLibelleConverti") or "").lower()
    return "plein" if "plein" in l else ("partiel" if "partiel" in l else None)


# En-tête normalisé des libellés de salaire de France Travail :
# « Annuel de 32000.0 Euros à 38000.0 Euros », « Mensuel de 486.0 Euros sur 12 mois »,
# « Horaire de 12.31 Euros - 13ème mois + primes »…
MOTIF_SALAIRE = re.compile(
    r"^(annuel|mensuel|horaire)\s+de\s+(\d+(?:[.,]\d+)?)\s*euros"
    r"(?:\s*à\s*(\d+(?:[.,]\d+)?)\s*euros)?",
    re.IGNORECASE,
)
MULTIPLICATEUR = {"annuel": 1, "mensuel": 12, "horaire": 1607}
# Fenêtre de vraisemblance, en brut annuel. En dessous : l'employeur a saisi des
# milliers d'euros dans la case « annuel » (« Annuel de 32.0 Euros à 38.0 Euros »).
# Au dessus : il a saisi un salaire annuel dans la case « mensuel ». Le plancher
# laisse passer les apprentis (27 % du SMIC = 5 832 € par an).
SALAIRE_MIN, SALAIRE_MAX = 4000, 250000


def salaire_min_max(lib):
    """'Annuel de 32000.0 Euros à 38000.0 Euros' -> (32000, 38000) ; mensuel x12, horaire x1607.

    On ne lit que cet en-tête : le commentaire libre qui suit un « - » répète ou
    brouille les chiffres (« De 30 à 35 k€ par an », « 13ème mois », « 35h hebdo »),
    et « sur 12 mois » n'est pas un montant. Lire tous les nombres du libellé
    obligeait à écarter les petites valeurs, ce qui effaçait les vrais salaires
    d'apprenti (486 €/mois = 27 % du SMIC).
    """
    if not lib:
        return None, None
    m = MOTIF_SALAIRE.match(lib.strip())
    if not m:
        return None, None
    mult = MULTIPLICATEUR[m.group(1).lower()]
    vals = [float(x.replace(",", ".")) * mult for x in (m.group(2), m.group(3)) if x]
    vals = [v for v in vals if SALAIRE_MIN <= v <= SALAIRE_MAX]
    return (round(min(vals)), round(max(vals))) if vals else (None, None)


def departement(lieu):
    cp = lieu.get("codePostal") or ""
    if cp[:2].isdigit() and cp != "99999":
        return "2A" if cp[:2] == "20" and cp < "20200" else ("2B" if cp[:2] == "20" else cp[:2])
    m = re.match(r"\s*(\d{2}|2A|2B)\s*-", lieu.get("libelle") or "")
    return m.group(1) if m else ""


class Geocodeur:
    """Centre des communes et villes principales des départements, via geo.api.gouv.fr, avec cache."""

    def __init__(self):
        self.dossier = RACINE / "data" / "geo"
        self.dossier.mkdir(parents=True, exist_ok=True)
        self.communes = self._lire("communes.json")
        self.departements = self._lire("departements.json")
        self.appels = 0

    def _lire(self, nom):
        f = self.dossier / nom
        return json.loads(f.read_text(encoding="utf-8")) if f.exists() else {}

    def _get(self, url):
        self.appels += 1
        time.sleep(0.05)
        try:
            r = requests.get(url, timeout=15)
            return r.json() if r.status_code == 200 else None
        except requests.RequestException:
            return None

    def commune(self, code):
        if code not in self.communes:
            d = self._get(f"{GEO}/communes/{code}?fields=centre")
            self.communes[code] = d["centre"]["coordinates"][::-1] if d and d.get("centre") else None
        return self.communes[code]

    def departement(self, code):
        if code not in self.departements:
            d = self._get(f"{GEO}/communes?codeDepartement={code}&fields=centre&boost=population&limit=1")
            self.departements[code] = d[0]["centre"]["coordinates"][::-1] if d else None
        return self.departements[code]

    def position(self, lieu):
        """(lat, lon, précision) ; précision = 'offre', 'commune', 'departement' ou None."""
        if lieu.get("latitude") and lieu.get("longitude"):
            return lieu["latitude"], lieu["longitude"], "offre"
        if lieu.get("commune"):
            p = self.commune(lieu["commune"])
            if p:
                return p[0], p[1], "commune"
        dep = departement(lieu)
        if dep:
            p = self.departement(dep)
            if p:
                return p[0], p[1], "departement"
        return None, None, None

    def code_postal(self, cp):
        """Centre de la première commune d'un code postal (mis en cache avec les communes)."""
        cle = "cp" + cp
        if cle not in self.communes:
            d = self._get(f"{GEO}/communes?codePostal={cp}&fields=centre&limit=1")
            self.communes[cle] = d[0]["centre"]["coordinates"][::-1] if d and d[0].get("centre") else None
        return self.communes[cle]

    def ville(self, nom):
        """'Aix-en-Provence' -> (département, lat, lon) de la commune la plus peuplée de ce nom."""
        cle = "nom:" + normaliser(nom)
        if cle not in self.communes:
            d = self._get(f"{GEO}/communes?nom={requests.utils.quote(nom)}&fields=codeDepartement,centre"
                          f"&boost=population&limit=1")
            self.communes[cle] = ([d[0]["codeDepartement"]] + d[0]["centre"]["coordinates"][::-1]
                                  if d and d[0].get("centre") else None)
        return self.communes[cle]

    def code_departement(self, nom):
        """'Loiret' -> '45' ; la liste des départements est lue une fois puis mise en cache."""
        if not hasattr(self, "noms"):
            self.noms = self._lire("noms_departements.json")
            if not self.noms:
                d = self._get(f"{GEO}/departements?fields=nom,code") or []
                self.noms = {normaliser(x["nom"]): x["code"] for x in d}
        return self.noms.get(normaliser(nom))

    def sauver(self):
        (self.dossier / "communes.json").write_text(json.dumps(self.communes), encoding="utf-8")
        (self.dossier / "departements.json").write_text(json.dumps(self.departements), encoding="utf-8")
        if getattr(self, "noms", None):
            (self.dossier / "noms_departements.json").write_text(json.dumps(self.noms, ensure_ascii=False), encoding="utf-8")


def normaliser(s):
    """'Île-de-France' -> 'ile de france' : sans accents, sans casse, sans ponctuation.
    L'apostrophe typographique compte comme l'apostrophe simple (« d’optique » = « d'optique »)."""
    s = (s or "").replace("’", "'").replace("‘", "'").replace("œ", "oe").replace("Œ", "oe").replace("æ", "ae")
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()
    return " ".join(re.sub(r"[^a-z0-9]+", " ", s).split())


# ---------------------------------------------------------------------------
# Deuxième canal : Adzuna (scripts/extraire_adzuna.py -> data/adzuna/brut/).
# Adzuna ne donne pas de liste d'offres « actives » : on retient celles publiées depuis
# moins de JOURS_ADZUNA jours. Une offre déjà présente chez France Travail (même intitulé,
# même employeur, même département) est écartée : on garde la version France Travail,
# plus riche (expérience, formation, compétences).
JOURS_ADZUNA = 45
CONTRATS_ADZUNA = {"permanent": "CDI", "contract": "CDD"}
RAYON_FRAIS = re.compile(r"\bfrais\b|boucherie|poissonnerie|fromage|charcuterie|traiteur|boulangerie|p[âa]tisserie|fruits", re.I)
RAYON_ALIMENTAIRE = re.compile(r"alimentaire|[ée]picerie|liquides|\bdrive\b|surgel", re.I)
BALISES = re.compile(r"<[^>]+>")


# Pour reconnaître la même annonce publiée sur deux canaux, on compare l'intitulé et l'employeur
# débarrassés de ce qui varie d'un site à l'autre : « H/F », « (F/H) », « CDI », « SAS »,
# « France », « Magasin »… Exemple : « Responsable de magasin H/F » chez « Carrefour Market SAS »
# et « Responsable de magasin (F/H) » chez « CARREFOUR MARKET » sont la même offre.
MOTS_TITRE = re.compile(r"\b(h f|f h|hf|fh|h|f|m|x|nb|cdi|cdd|temps plein|temps partiel|e|ne)\b")
MOTS_EMPLOYEUR = re.compile(r"\b(sas|sasu|sarl|sa|eurl|snc|scs|france|groupe|group|magasin|magasins|store|stores"
                            r"|retail|hypermarche|supermarche|distribution|sa|et cie)\b")


def cle_doublon(o):
    titre = " ".join(MOTS_TITRE.sub(" ", normaliser(o.get("intitule"))).split())
    employeur = " ".join(MOTS_EMPLOYEUR.sub(" ", normaliser(o.get("entreprise"))).split())
    return (titre, employeur, o.get("dep") or "")


DISTANCE_DOUBLON = 15     # km : au-delà, deux offres identiques d'une même enseigne sont deux magasins


def distance_km(a, b):
    r = math.radians
    h = (math.sin(r(b[0] - a[0]) / 2) ** 2
         + math.cos(r(a[0])) * math.cos(r(b[0])) * math.sin(r(b[1] - a[1]) / 2) ** 2)
    return 2 * 6371 * math.asin(math.sqrt(h))


class Connues:
    """Les offres déjà retenues, pour écarter les doublons des canaux suivants.

    Doublon = même intitulé et même employeur (normalisés) et :
      - même département, quand les deux offres en ont un ;
      - sinon, des positions à moins de DISTANCE_DOUBLON km.
      - ou, à défaut, exactement le même lieu écrit (« Pays de la Loire », « Corse » : offres
        sans département ni position, vérifié le 26/11/2026).
    Sans aucun de ces repères, on ne peut pas trancher : l'offre est gardée. Une enseigne
    publie souvent la même annonce pour plusieurs magasins : ce ne sont pas des doublons.
    Vérifié le 05/10/2026 sur Adzuna : 1 157 doublons sûrs (même département) contre
    868 offres identiques mais situées à plus de 15 km (d'autres magasins), gardées."""

    def __init__(self, offres):
        self.par_cle = defaultdict(list)            # (intitulé, employeur) -> [(dép, lat, lon, lieu)]
        for o in offres:
            self.ajouter(o)

    def ajouter(self, o):
        t, e, d = cle_doublon(o)
        self.par_cle[(t, e)].append((d, o.get("lat"), o.get("lon"), normaliser(o.get("lieu"))))

    def __contains__(self, o):
        t, e, d = cle_doublon(o)
        if not e:                                   # sans employeur, impossible de trancher : on garde
            return False
        lieu = normaliser(o.get("lieu"))
        for d2, lat2, lon2, lieu2 in self.par_cle.get((t, e), ()):
            if lieu and lieu == lieu2:
                return True
            if d and d2:
                if d == d2:
                    return True
            elif None not in (o.get("lat"), o.get("lon"), lat2, lon2):
                if distance_km((o["lat"], o["lon"]), (lat2, lon2)) <= DISTANCE_DOUBLON:
                    return True
        return False


# ---------------------------------------------------------------------------
# Deux résumés : les offres actuelles (data/resume.json) et tout l'historique depuis le début
# de la collecte (data/historique.json). Pour l'historique, chaque offre porte le premier et le
# dernier jour où elle a été vue en ligne, lus dans les listes quotidiennes data/.../actives/.

_ARCHIVES = {}


def charger_versions(nom, fichiers, cle="id"):
    """Dernière version de chaque offre d'une archive brute, et le jour où on l'a vue la première
    fois. Lu une seule fois par source, puis réutilisé par les deux résumés."""
    if nom not in _ARCHIVES:
        versions, premiere = {}, {}
        for f in fichiers:
            with f.open(encoding="utf-8") as fh:
                for ligne in fh:
                    if ligne.strip():
                        v = json.loads(ligne)
                        premiere.setdefault(v[cle], v["vu_le"])
                        versions[v[cle]] = v
        _ARCHIVES[nom] = (versions, premiere)
    return _ARCHIVES[nom]


def presences(dossier, cle="id"):
    """{offre: (premier jour, dernier jour, code ROME)} d'après les listes quotidiennes des offres
    en ligne (un fichier <AAAA-MM-JJ>.csv par jour), et le dernier jour de liste."""
    vues, dernier = {}, None
    for f in sorted(dossier.glob("*.csv")):
        dernier = f.stem
        with f.open(encoding="utf-8") as fh:
            for r in csv.DictReader(fh):
                i = r[cle]
                vues[i] = (vues[i][0], dernier, vues[i][2]) if i in vues else (dernier, dernier, r.get("rome"))
    return vues, dernier


def bornes(oid, pres, premiere, v):
    """(première vue, dernière vue) d'une offre : listes quotidiennes quand elles existent, sinon
    dates d'écriture des versions (Adzuna n'a ses listes quotidiennes que depuis le 26/11/2026)."""
    p = pres.get(oid)
    debut = min(x for x in (p[0] if p else None, premiere.get(oid), v["vu_le"]) if x)
    fin = max(x for x in (p[1] if p else None, v["vu_le"]) if x)
    return debut, fin


def offres_adzuna(jour, geo, cles_ft, historique=False):
    """Offres Adzuna, au même format que celles de France Travail. Actuelles : publiées depuis
    moins de JOURS_ADZUNA jours. Historique : toutes celles vues depuis le début."""
    versions, premiere = charger_versions("adzuna", sorted((RACINE / "data" / "adzuna" / "brut").glob("*.jsonl")))
    pres, _ = presences(RACINE / "data" / "adzuna" / "actives")
    limite = f"{date.fromisoformat(jour) - timedelta(days=JOURS_ADZUNA):%Y-%m-%d}"
    offres, doublons = [], 0
    for oid, v in versions.items():
        o = v["offre"]
        cree = (o.get("created") or "")[:10]
        if not historique and cree and cree < limite:
            continue
        intitule = BALISES.sub("", o.get("title") or "").strip()
        description = BALISES.sub("", o.get("description") or "")
        t = (intitule + " " + description).lower()
        loc = o.get("location") or {}
        dep = next((c for c in map(geo.code_departement, (loc.get("area") or [])[1:]) if c), "")
        lat, lon, precision = o.get("latitude"), o.get("longitude"), "offre"
        if lat is None or lon is None:
            p = geo.departement(dep) if dep else None
            lat, lon, precision = (p[0], p[1], "departement") if p else (None, None, None)
        rome = v["rome"]
        if rome == "D1503":                       # chef de rayon : on précise d'après l'intitulé
            rome = "D1513" if RAYON_FRAIS.search(intitule) else ("D1502" if RAYON_ALIMENTAIRE.search(intitule) else rome)
        smin = smax = libelle = None
        if str(o.get("salary_is_predicted")) != "1":   # les salaires estimés par Adzuna ne comptent pas
            vals = [x for x in (o.get("salary_min"), o.get("salary_max")) if x and SALAIRE_MIN <= x <= SALAIRE_MAX]
            if vals:
                smin, smax = round(min(vals)), round(max(vals))
                libelle = f"Annuel de {smin}.0 Euros à {smax}.0 Euros"
        offre = {
            "id": "AZ" + oid,
            "rome": rome,
            "source": "Adzuna",
            "intitule": intitule,
            "entreprise": (o.get("company") or {}).get("display_name"),
            "lieu": loc.get("display_name"),
            "dep": dep,
            "lat": lat, "lon": lon, "prec": precision,
            "contrat": CONTRATS_ADZUNA.get(o.get("contract_type")),
            "experience": None,
            "alternance": bool(re.search(r"alternan|apprenti", intitule, re.I)),
            "salaire": libelle,
            "smin": smin, "smax": smax,
            "date": cree,
            "vu_le": v["vu_le"],
            "premiere_vue": bornes(oid, pres, premiere, v)[0], "derniere_vue": bornes(oid, pres, premiere, v)[1],
            "url": o.get("redirect_url"),
            "outils": [nom for nom, rx in REGEX_OUTILS.items() if rx.search(t)],
            "teletravail": "télétravail" in t,
            "competences": [],
            "niveau": niveau(intitule),
            "nature": "autre",
            "exp_exige": None,
            "exp_ans": None,
            "qualification": None,
            "formation": None,
            "secteur": None,
            "temps": {"full_time": "plein", "part_time": "partiel"}.get(o.get("contract_time")),
            "postes": 1,
        }
        offres.append(offre)
    offres, republiees = sans_republications(offres)
    print(f"Adzuna : {republiees} republications de la même annonce écartées")
    retenues = []
    for offre in offres:
        if offre in cles_ft:
            doublons += 1
            continue
        retenues.append(offre)
    return retenues, doublons


def sans_republications(offres):
    """Une annonce republiée reçoit un nouvel identifiant (Adzuna, France Travail, Jooble…) et
    s'accumule (constat du 26/11/2026 : 26 562 offres Adzuna pour 16 668 annonces distinctes, dont
    une offre Carrefour présente 71 fois). On n'en garde qu'une par intitulé + employeur
    (normalisés) + lieu exact, la plus récente, avec la première et la dernière vue de l'ensemble.
    Deux magasins d'une même enseigne ont des lieux différents : ils restent deux.
    Sans employeur, on ne peut pas trancher : l'offre est gardée."""
    gardees, sans_employeur = {}, []
    for o in offres:
        t, e, _ = cle_doublon(o)
        if not e:
            sans_employeur.append(o)
            continue
        cle = (t, e, normaliser(o.get("lieu")))
        g = gardees.get(cle)
        if g is None:
            gardees[cle] = o
            continue
        # L'annonce gardée couvre toute la période de ses republications.
        debut, fin = min(g["premiere_vue"], o["premiere_vue"]), max(g["derniere_vue"], o["derniere_vue"])
        if (o["date"] or "", o["vu_le"]) > (g["date"] or "", g["vu_le"]):
            gardees[cle] = g = o
        g["premiere_vue"], g["derniere_vue"] = debut, fin
    resultat = list(gardees.values()) + sans_employeur
    return resultat, len(offres) - len(resultat)


DIPLOMES_EUROPEENS = {"3": "< Bac", "4": "Bac", "5": "Bac+2", "6": "Bac+3/4", "7": "Bac+5"}


def offres_lba(cles_connues, historique=False):
    """Offres en alternance de La bonne alternance (scripts/extraire_lba.py), au format commun.
    Actuelles : vues à la dernière collecte. Historique : toutes celles vues depuis le début."""
    pres, dernier = presences(RACINE / "data" / "lba" / "actives")
    if not pres:
        return [], 0
    versions, premiere = charger_versions("lba", sorted((RACINE / "data" / "lba" / "brut").glob("*.jsonl")))
    offres, doublons = [], 0
    for oid, v in versions.items():
        if oid not in pres or (not historique and pres[oid][1] != dernier):
            continue
        o = v["offre"]
        lieu = o.get("workplace") or {}
        loc = lieu.get("location") or {}
        coords = ((loc.get("geopoint") or {}).get("coordinates") or [None, None])
        adresse = loc.get("address") or ""
        cp = re.search(r"\b(\d{5})\b", adresse)
        contrat = o.get("contract") or {}
        types = contrat.get("type") or []
        offre = o.get("offer") or {}
        titre = offre.get("title") or ""
        t = (titre + " " + BALISES.sub(" ", offre.get("description") or "")).lower()
        codes = [c for c in offre.get("rome_codes") or [] if c in METIERS]
        dep = departement({"codePostal": cp.group(1)}) if cp else ""
        ville = re.sub(r"^.*\b\d{5}\s*", "", adresse).strip() if cp else adresse
        entree = {
            "id": "LB" + oid,
            "rome": codes[0] if codes else (pres[oid][2] or v["rome"]),
            "source": "La bonne alternance",
            "partenaire": (o.get("identifier") or {}).get("partner_label"),
            "intitule": titre,
            "entreprise": lieu.get("brand") or lieu.get("name") or lieu.get("legal_name"),
            "lieu": f"{dep} - {ville.title()}" if dep else (adresse or None),
            "dep": dep,
            "lat": coords[1], "lon": coords[0], "prec": "offre" if coords[0] is not None else None,
            "contrat": None,
            "experience": None,
            "alternance": True,
            "salaire": None,
            "smin": None, "smax": None,
            "date": ((offre.get("publication") or {}).get("creation") or "")[:10],
            "vu_le": v["vu_le"],
            "premiere_vue": bornes(oid, pres, premiere, v)[0], "derniere_vue": bornes(oid, pres, premiere, v)[1],
            "url": (o.get("apply") or {}).get("url"),
            "outils": [nom for nom, rx in REGEX_OUTILS.items() if rx.search(t)],
            "teletravail": contrat.get("remote") in ("hybrid", "remote"),
            "competences": offre.get("desired_skills") or [],
            "niveau": niveau(titre),
            "nature": "apprentissage" if "Apprentissage" in types else ("professionnalisation" if types else "apprentissage"),
            "exp_exige": None,
            "exp_ans": None,
            "qualification": None,
            "formation": DIPLOMES_EUROPEENS.get(str((offre.get("target_diploma") or {}).get("european") or "")),
            "secteur": (((lieu.get("domain") or {}).get("naf") or {}).get("label")),
            "temps": None,
            "postes": int(offre.get("opening_count") or 1),
        }
        if entree in cles_connues:
            doublons += 1
            continue
        offres.append(entree)
    return offres, doublons


# Un intitulé de retail : un poste d'encadrement + un lieu de vente (même règle que extraire_wttj.py).
# Sert aux agrégateurs dont la recherche par mots-clés est trop lâche (Jooble renvoie aussi
# des « chef de partie » ou des « technicien » pour « responsable de magasin »).
TITRE_RETAIL = re.compile(r"\b(responsable|directeur|directrice|manager|chef|cheffe|adjoint|adjointe|gerant|gerante)\b"
                          r".*\b(magasin|boutique|point de vente|rayon|rayons|caisse|caisses|drive|secteur|store)\b"
                          r"|\bstore manager\b")

SALAIRE_LIBRE = re.compile(r"(\d[\d\s.,]*)\s*(k)?\s*(?:€|eur)", re.I)


def salaire_texte(lib):
    """'2 000 € - 2 500 € par mois' -> (24000, 30000) ; None si rien de lisible ou d'invraisemblable."""
    if not lib:
        return None, None
    l = lib.lower()
    mult = 12 if "mois" in l else (1607 if "heure" in l or "/h" in l else 1)
    vals = []
    for nombre, k in SALAIRE_LIBRE.findall(l):
        n = nombre.replace(" ", "").replace(" ", "").replace("\xa0", "")
        n = n.replace(",", ".") if n.count(",") == 1 and len(n.split(",")[1]) <= 2 else n.replace(",", "")
        try:
            v = float(n) * (1000 if k else 1) * mult
        except ValueError:
            continue
        if SALAIRE_MIN <= v <= SALAIRE_MAX:
            vals.append(v)
    return (round(min(vals)), round(max(vals))) if vals else (None, None)


def offres_jooble(geo, cles_connues, historique=False):
    """Offres Jooble (scripts/extraire_jooble.py), au format commun. Actuelles : vues à la
    dernière collecte. Historique : toutes celles vues depuis le début."""
    pres, dernier = presences(RACINE / "data" / "jooble" / "actives")
    if not pres:
        return [], 0
    versions, premiere = charger_versions("jooble", sorted((RACINE / "data" / "jooble" / "brut").glob("*.jsonl")))
    offres, doublons = [], 0
    for oid, v in versions.items():
        # Seules comptent les offres du site français de Jooble : les premières collectes
        # interrogeaient jooble.org, qui sert le marché américain.
        if v.get("hote") != "fr.jooble.org" or oid not in pres or (not historique and pres[oid][1] != dernier):
            continue
        o = v["offre"]
        titre = html.unescape(BALISES.sub("", o.get("title") or "")).strip()
        if not TITRE_RETAIL.search(normaliser(titre)):
            continue                              # hors retail : la recherche Jooble est trop lâche
        t = (titre + " " + BALISES.sub(" ", o.get("snippet") or "")).lower()
        # Lieux Jooble : « Montauban, Tarn-et-Garonne », « Fayet, 80300 », « Grenoble », « Lyon (69) ».
        # Sans département, le contrôle des doublons ne peut pas comparer : on le retrouve par le
        # code postal, le nom du département, ou à défaut le nom de la ville (geo.api.gouv.fr).
        lieu = (o.get("location") or "").strip()
        morceaux = [x.strip() for x in re.split(r"[,/]", lieu) if x.strip()]
        m = re.search(r"\((\d{2,3}|2A|2B)\)", lieu)
        cp = re.search(r"\b(\d{5})\b", lieu)
        dep = (m.group(1) if m else departement({"codePostal": cp.group(1)}) if cp
               else next((c for c in map(geo.code_departement, morceaux[1:]) if c), ""))
        lat = lon = precision = None
        commune = geo.ville(re.sub(r"\s*\(.*\)", "", morceaux[0])) if morceaux and morceaux[0] != "France" else None
        if commune and (not dep or commune[0] == dep):
            dep, lat, lon, precision = commune[0], commune[1], commune[2], "commune"
        elif dep:
            p = geo.departement(dep)
            lat, lon, precision = (p[0], p[1], "departement") if p else (None, None, None)
        rome = v["rome"]
        if rome == "D1503":
            rome = "D1513" if RAYON_FRAIS.search(titre) else ("D1502" if RAYON_ALIMENTAIRE.search(titre) else rome)
        type_ = (o.get("type") or "").lower()
        smin, smax = salaire_texte(o.get("salary"))
        entree = {
            "id": "JO" + oid,
            "rome": rome,
            "source": "Jooble",
            "partenaire": o.get("source"),
            "intitule": titre,
            "entreprise": (o.get("company") or "").strip() or None,
            "lieu": lieu or None,
            "dep": dep,
            "lat": lat, "lon": lon, "prec": precision,
            "contrat": "CDI" if "cdi" in type_ else ("CDD" if "cdd" in type_ else ("MIS" if "intérim" in type_ or "interim" in type_ else None)),
            "experience": None,
            "alternance": bool(re.search(r"alternan|apprenti", titre + " " + type_, re.I)),
            "salaire": f"Annuel de {smin}.0 Euros à {smax}.0 Euros" if smin else None,
            "smin": smin, "smax": smax,
            "date": (o.get("updated") or "")[:10],
            "vu_le": v["vu_le"],
            "premiere_vue": bornes(oid, pres, premiere, v)[0], "derniere_vue": bornes(oid, pres, premiere, v)[1],
            "url": o.get("link"),
            "outils": [nom for nom, rx in REGEX_OUTILS.items() if rx.search(t)],
            "teletravail": "télétravail" in t,
            "competences": [],
            "niveau": niveau(titre),
            "nature": "autre",
            "exp_exige": None,
            "exp_ans": None,
            "qualification": None,
            "formation": None,
            "secteur": None,
            "temps": "partiel" if "partiel" in type_ else ("plein" if "plein" in type_ else None),
            "postes": 1,
        }
        if entree in cles_connues:
            doublons += 1
            continue
        offres.append(entree)
    return offres, doublons


def offres_wttj(geo, cles_connues, historique=False):
    """Offres Welcome to the Jungle (scripts/extraire_wttj.py), au format commun. Actuelles :
    présentes dans le plan du site au dernier passage. Historique : toutes celles lues."""
    pres, dernier = presences(RACINE / "data" / "wttj" / "actives", cle="url")
    if not pres:
        return [], 0
    versions, premiere = charger_versions("wttj", sorted((RACINE / "data" / "wttj" / "brut").glob("*.jsonl")), cle="url")
    offres, doublons = [], 0
    for url, v in versions.items():
        if url not in pres or (not historique and pres[url][1] != dernier):
            continue
        o = v["offre"]
        cp = o.get("code_postal") or ""
        dep = departement({"codePostal": cp})
        p = geo.code_postal(cp) if cp else None
        lat, lon, precision = (p[0], p[1], "commune") if p else (None, None, None)
        if lat is None and dep:
            p = geo.departement(dep)
            lat, lon, precision = (p[0], p[1], "departement") if p else (None, None, None)
        smin, smax = o.get("smin"), o.get("smax")
        offre = {
            "id": "WJ" + url.rsplit("/jobs/", 1)[1],
            "rome": v["rome"],
            "source": "Welcome to the Jungle",
            "intitule": o.get("titre"),
            "entreprise": o.get("entreprise"),
            "lieu": " ".join(x for x in (cp[:2], "-", o.get("ville")) if x) if cp else o.get("ville"),
            "dep": dep,
            "lat": lat, "lon": lon, "prec": precision,
            "contrat": o.get("contrat"),
            "experience": None,
            "alternance": bool(o.get("alternance")),
            "salaire": f"Annuel de {smin}.0 Euros à {smax}.0 Euros" if smin else None,
            "smin": smin, "smax": smax,
            "date": o.get("publiee"),
            "vu_le": v["vu_le"],
            "premiere_vue": bornes(url, pres, premiere, v)[0], "derniere_vue": bornes(url, pres, premiere, v)[1],
            "url": url,
            "outils": o.get("outils") or [],
            "teletravail": bool(o.get("teletravail")),
            "competences": [],
            "niveau": niveau(o.get("titre")),
            "nature": "autre",
            "exp_exige": None,
            "exp_ans": None,
            "qualification": None,
            "formation": o.get("formation"),
            "secteur": o.get("secteur"),
            "temps": None,
            "postes": 1,
        }
        if offre in cles_connues:
            doublons += 1
            continue
        offres.append(offre)
    return offres, doublons


def offres_france_travail(geo, historique=False):
    """Offres France Travail : celles de la dernière liste du jour (actuelles), ou toutes celles
    vues depuis le début (historique), avec la dernière version connue de chaque annonce."""
    pres, dernier = presences(RACINE / "data" / "actives")
    versions, premiere = charger_versions("france_travail", sorted((RACINE / "data" / "brut").glob("*/*.jsonl")))
    offres = []
    for oid, (debut_liste, fin_liste, rome) in pres.items():
        if not historique and fin_liste != dernier:
            continue
        v = versions.get(oid)
        if not v:
            continue
        o = v["offre"]
        lieu = o.get("lieuTravail") or {}
        texte = (o.get("intitule") or "") + " " + (o.get("description") or "")
        t = texte.lower()
        smin, smax = salaire_min_max((o.get("salaire") or {}).get("libelle"))
        lat, lon, precision = geo.position(lieu)
        debut, fin = bornes(oid, pres, premiere, v)
        offres.append({
            "id": oid,
            "rome": rome,
            "source": "France Travail",
            "intitule": o.get("intitule"),
            "entreprise": (o.get("entreprise") or {}).get("nom"),
            "lieu": lieu.get("libelle"),
            "dep": departement(lieu),
            "lat": lat, "lon": lon, "prec": precision,
            "contrat": o.get("typeContrat"),
            "experience": o.get("experienceLibelle"),
            "alternance": bool(o.get("alternance")),
            "salaire": (o.get("salaire") or {}).get("libelle"),
            "smin": smin, "smax": smax,
            "date": (o.get("dateCreation") or "")[:10],
            "vu_le": v["vu_le"],
            "premiere_vue": debut, "derniere_vue": fin,
            "url": (o.get("origineOffre") or {}).get("urlOrigine"),
            "outils": [nom for nom, rx in REGEX_OUTILS.items() if rx.search(t)],
            "teletravail": "télétravail" in t,
            "competences": [c.get("libelle") for c in o.get("competences") or [] if c.get("libelle")],
            "niveau": niveau(o.get("intitule")),
            "nature": nature(o),
            "exp_exige": o.get("experienceExige") or None,
            "exp_ans": exp_ans(o.get("experienceLibelle")),
            "qualification": o.get("qualificationLibelle") or None,
            "formation": formation(o),
            "secteur": o.get("secteurActiviteLibelle") or None,
            "temps": temps_travail(o),
            "postes": int(o.get("nombrePostes") or 1),
        })
    return offres


def construire(jour, geo, historique):
    """Toutes les sources, doublons entre canaux écartés (France Travail d'abord, puis les autres)."""
    # Chaque source republie aussi ses propres annonces sous un nouvel identifiant (vérifié le
    # 26/11/2026 sur l'historique : 5 933 republications chez France Travail, 543 chez Jooble) :
    # on n'en garde qu'une par source, avant de comparer les sources entre elles.
    offres, republiees = sans_republications(offres_france_travail(geo, historique))
    stats = {"France Travail": (len(offres), 0, republiees)}
    connues = Connues(offres)          # une seule liste, enrichie canal après canal
    for nom, f in (("Adzuna", lambda: offres_adzuna(jour, geo, connues, historique)),
                   ("Welcome to the Jungle", lambda: offres_wttj(geo, connues, historique)),
                   ("La bonne alternance", lambda: offres_lba(connues, historique)),
                   ("Jooble", lambda: offres_jooble(geo, connues, historique))):
        lot, doublons = f()
        lot, republiees = sans_republications(lot)
        offres.extend(lot)
        for o in lot:
            connues.ajouter(o)
        stats[nom] = (len(lot), doublons, republiees)
    return offres, stats


def resume_de(offres, jour, serie, nb_versions, historique, debut):
    present = lambda s: any(o["source"] == s for o in offres)
    return {
        "date": jour,
        "periode": "historique" if historique else "actuelles",
        "debut": debut,
        "source": "France Travail — API Offres d'emploi v2" + (" + The Adzuna API" if present("Adzuna") else "")
                  + (" + Welcome to the Jungle" if present("Welcome to the Jungle") else "")
                  + (" + La bonne alternance" if present("La bonne alternance") else "")
                  + (" + Jooble" if present("Jooble") else ""),
        "requete": ("toutes les offres vues au moins une fois depuis le début de la collecte, doublons écartés"
                    if historique else
                    "une requête codeROME par métier, France entière"
                    f" ; Adzuna : offres de moins de {JOURS_ADZUNA} jours"
                    " ; Welcome to the Jungle : offres retail du plan du site"
                    " ; La bonne alternance : offres en alternance hors France Travail"
                    " ; Jooble : offres vues à la dernière collecte, doublons écartés"),
        "sources": [s for s in ("France Travail", "Adzuna", "Welcome to the Jungle", "La bonne alternance", "Jooble")
                    if present(s)],
        "metiers": [{"code": c, "libelle": l, "groupe": g, "coche": k,
                     "actives": sum(1 for o in offres if o["rome"] == c)}
                    for c, (l, g, k) in METIERS.items()],
        "outils": list(OUTILS),
        "contrats": {c: contrat_libelle(c) for c in sorted({o["contrat"] for o in offres if o["contrat"]})},
        "niveaux": NIVEAUX_LIBELLES,
        "formations": FORMATIONS,
        "versions_conservees": nb_versions,
        "sans_position": sum(1 for o in offres if o["lat"] is None),
        "serie": [{"date": d, "par_metier": m} for d, m in sorted(serie.items())],
    }


# Format compact de l'historique : 45 000 offres et plus ne tiennent pas dans le format de
# resume.json (16 Mo pour 23 000 offres). Une ligne par offre, colonnes dans l'ordre de COLONNES ;
# outils et compétences codés par leur numéro dans les listes « outils » et « competences ».
# Le site (assets/commun.js) redéplie chaque ligne en objet au chargement.
COLONNES = ["id", "rome", "source", "intitule", "entreprise", "lieu", "dep", "lat", "lon", "prec", "contrat",
            "alternance", "salaire", "smin", "smax", "date", "premiere_vue", "derniere_vue", "url", "outils",
            "competences", "niveau", "nature", "exp_exige", "exp_ans", "qualification", "formation", "secteur"]


def compacter(resume, offres):
    index_outils = {nom: i for i, nom in enumerate(resume["outils"])}
    vocab, index_comp = [], {}
    lignes = []
    for o in offres:
        ligne = []
        for c in COLONNES:
            x = o.get(c)
            if c == "outils":
                x = [index_outils[n] for n in x]
            elif c == "competences":
                codes = []
                for n in x or []:
                    if n not in index_comp:
                        index_comp[n] = len(vocab)
                        vocab.append(n)
                    codes.append(index_comp[n])
                x = codes
            elif c in ("lat", "lon") and x is not None:
                x = round(x, 4)
            ligne.append(x)
        lignes.append(ligne)
    return dict(resume, colonnes=COLONNES, competences=vocab, lignes=lignes)


def main():
    jours = sorted((RACINE / "data" / "actives").glob("*.csv"))
    if not jours:
        raise SystemExit("Aucune extraction : lancez d'abord scripts/extraire.py")
    jour, debut = jours[-1].stem, jours[0].stem
    nb_versions = sum(1 for f in (RACINE / "data" / "brut").glob("*/*.jsonl")
                      for l in f.open(encoding="utf-8") if l.strip())

    serie = defaultdict(dict)          # série : par jour et par métier
    with (RACINE / "data" / "serie.csv").open(encoding="utf-8") as f:
        for r in csv.DictReader(f):
            serie[r["date"]][r["rome"]] = int(r["total"])

    geo = Geocodeur()
    for historique in (False, True):
        offres, stats = construire(jour, geo, historique)
        resume = resume_de(offres, jour, serie, nb_versions, historique, debut)
        if historique:
            contenu = compacter(resume, offres)
            sortie = RACINE / "data" / "historique.json"
        else:
            contenu = dict(resume, offres=offres)
            sortie = RACINE / "data" / "resume.json"
        sortie.write_text(json.dumps(contenu, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        print(f"\nÉcrit : {sortie.relative_to(RACINE)} — {len(offres)} offres "
              f"({'historique depuis le ' + debut if historique else 'actuelles au ' + jour}), "
              f"{sortie.stat().st_size // 1024} Ko")
        print("Sources : " + ", ".join(f"{n} {k} ({d} doublons entre sources, {r} republications écartés)"
                                       for n, (k, d, r) in stats.items()))
        avec = [o for o in offres if o["smin"] is not None]
        print(f"Salaire affiché par {len(avec)} offres sur {len(offres)} "
              f"({100 * len(avec) // len(offres) if offres else 0} %)")
    geo.sauver()
    print(f"Positions : {geo.appels} appels geo.api.gouv.fr")


if __name__ == "__main__":
    main()