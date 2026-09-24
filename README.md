# Le marché de mon métier — responsable retail

Projet de groupe, M1 Marketing, IAE Clermont Auvergne (cours d'analyse de données).
Adapté du dépôt de démonstration de Vincent Favarin
([VincentFavarin/metier](https://github.com/VincentFavarin/metier)) : même chaîne,
autre métier.

Chaque matin, une Action GitHub interroge l'API France Travail, enregistre les offres
du jour dans `data/` et met à jour le site (GitHub Pages).

| Page | Contenu |
|---|---|
| `index.html` | les filtres, les chiffres, la carte de France |
| `salaires.html` | ce que ça paie : fourchettes par niveau, métier, contrat, territoire |
| `exigences.html` | ce qu'on vous demande : expérience, diplôme, outils, compétences |
| `recruteurs.html` | qui recrute : entreprises, secteurs, employeurs ouverts aux débutants |
| `mouvement.html` | le marché bouge : extractions successives, fraîcheur, limites |

## Le métier

- **Intitulé** : responsable retail, c'est-à-dire responsable de magasin / de point de vente.
- **Variantes rencontrées dans les offres** : responsable de boutique, store manager,
  directeur de magasin, gérant de magasin, chef de secteur, responsable de rayon,
  adjoint de magasin.
- **Codes ROME principaux** : **D1302** Responsable de boutique et **D1504** Directeur /
  Directrice de magasin de grande distribution.

## Les métiers suivis

18 codes ROME 4.0 (vérifiés sur MétierScope, France Travail). La liste se trouve dans
`scripts/extraire.py` (`METIERS`) :

- **Magasin** (diriger un point de vente) : D1302 responsable de boutique, D1504
  directeur de magasin GD, D1301 gérant de magasin d'alimentation générale, D1305
  responsable de magasin cycles, D1512 directeur régional d'hyper/supermarché.
- **Rayon** (encadrer un rayon, un secteur) : D1509 responsable de département GD,
  D1510 chef de secteur magasin, D1517 chef de secteur distribution, D1503 / D1502 /
  D1513 chefs de rayon (non alimentaire, alimentaire, frais), D1508 responsable de caisses.
- **Frontière** (décochés par défaut) : D1516 responsable merchandising, D1506 chargé
  de merchandising, M1720 category manager, E1113 responsable e-commerce, D1417 chef
  des ventes, K1816 manager de commerce et territoire.

## Les questions que nous posons à ce marché

1. Combien d'offres, et où : Clermont / Puy-de-Dôme, Auvergne-Rhône-Alpes, France ?
2. Quels contrats et quels salaires affichés, selon le niveau de poste ?
3. Quelles compétences reviennent le plus : management, pilotage du CA, merchandising,
   omnicanal… ?
4. Quelles enseignes recrutent le plus ?

## La chaîne

```
API France Travail  →  scripts/extraire.py  →  data/brut/<mois>/<ROME>.jsonl   chaque version d'annonce, une seule fois
                                            →  data/actives/<date>.csv         les offres actives du jour (rome, id)
                                            →  data/serie.csv                  par jour et par métier : total, nouvelles, modifiées
                       scripts/resumer.py   →  data/resume.json                ce que les pages affichent (+ data/geo/, cache des positions)
                       index.html + 4 pages →  GitHub Pages
                       .github/workflows/veille.yml : GitHub relance tout ça chaque matin à 7 h
```

- `scripts/extraire.py` : une requête `codeROME` par métier (token OAuth, pagination
  150 / 1 150). Le brut est conservé intégralement, version par version (empreinte SHA-1).
- `scripts/resumer.py` : salaires (libellé → min/max annuels bruts), compétences citées
  (grille `OUTILS`, adaptée au retail), niveau de poste, position sur la carte.

## Mise en route sur GitHub (sans rien installer)

1. Créer un dépôt **public** et y envoyer le contenu de ce dossier.
2. Settings → Secrets and variables → Actions → *New repository secret* :
   `FT_CLIENT_ID` et `FT_CLIENT_SECRET` (identifiants de l'application créée sur
   [francetravail.io](https://francetravail.io), abonnée à l'API « Offres d'emploi v2 »).
3. Settings → Pages → Source « Deploy from a branch », branche `main`, dossier `/ (root)`.
4. Actions → veille → *Run workflow* : le premier commit du bot arrive dans `data/`,
   et le site s'affiche quelques minutes après.

## Règles

- Les identifiants sont dans les secrets du dépôt : jamais dans un fichier versionné.
- Un canal, une requête, une date : chaque chiffre du site les affiche.
- Pas de scraping de LinkedIn, APEC ou Indeed (interdit par leurs CGU).
