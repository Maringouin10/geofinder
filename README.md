# GeoFinder

Géolocalisation d'une photo **par reconnaissance d'image**. Tu donnes une ville,
GeoFinder télécharge et indexe son imagerie de rue, puis tu déposes une photo,
tu appuies sur **FIND**, et il te dit où elle a été prise — sur une carte.

Quatre sources d'images sont supportées, dont **deux qui ne coûtent rien** —
Google n'est pas obligatoire.

Aucune donnée EXIF n'est utilisée : la position est déduite **uniquement du
contenu visuel** de l'image.

```
photo ──► CLIP ──► recherche cosinus ──► top-40 candidats ──► ORB + RANSAC ──► carte
              (empreinte)      (index de la ville)      (vérification géométrique)
```

---

## Démarrage rapide

```bash
docker compose up --build
```

Ça suffit : sans aucune clé, GeoFinder utilise **KartaView**, qui n'en demande
pas. Pour une bien meilleure couverture, mets un jeton Mapillary (gratuit,
2 minutes) dans un `.env` :

```bash
cp .env.example .env          # puis renseigne MAPILLARY_ACCESS_TOKEN
docker compose up --build
```

Ouvre <http://localhost:8000>.

1. **Étape 1 — Préparer une ville.** Tape `Bordeaux`, clique sur *Indexer*, attends
   la barre de progression. À faire une seule fois par ville : l'index est
   persisté dans le volume `geofinder-data`.
2. **Étape 2 — Trouver.** Dépose une photo (glisser-déposer, coller, ou clic),
   choisis la ville, clique sur **FIND**.

Sans `docker compose` :

```bash
docker build -t geofinder .
docker run -p 8000:8000 -v geofinder-data:/data \
  -e MAPILLARY_ACCESS_TOKEN=MLY... geofinder
```

---

## Les sources d'images

Choisies dans l'interface, ou via `GEOFINDER_PROVIDER`. En `auto`, GeoFinder
prend la première utilisable : **mapillary → kartaview → google → demo**.

| Source | Clé | Coût | Couverture | Remarque |
|---|---|---|---|---|
| **Mapillary** | jeton gratuit | gratuit | très large, mondiale | **recommandé** |
| **KartaView** | aucune | gratuit | plus clairsemée | fonctionne sans rien configurer |
| **Google Street View** | clé + facturation | payant | excellente | compte Google Cloud requis |
| **Démo** | aucune | — | aucune | images synthétiques, hors ligne |

### Mapillary (recommandé)

Imagerie de rue participative, propriété de Meta, mais l'API v4 est ouverte et
le jeton est **gratuit et immédiat** :

1. compte sur [mapillary.com](https://www.mapillary.com) ;
2. *Developers* → *Register application* ;
3. copie le **client token** (format `MLY|...`) dans `MAPILLARY_ACCESS_TOKEN`.

Chaque photo arrive avec sa position **et son cap réels** : contrairement à
Google, il n'y a rien à « rendre », la vue existe déjà. Les séquences étant
continues (des dizaines de clichés à quelques mètres d'écart), GeoFinder
regroupe les photos par cellule de 15 m et n'en garde que `headings` par lieu —
sinon l'index serait saturé de quasi-doublons.

La ville est interrogée par **petites tuiles de 400 m, en parallèle**, et la
collecte s'arrête dès qu'assez de lieux sont réunis. C'est ce qui rend la
découverte rapide : une requête couvrant plusieurs kilomètres carrés dépasse
la minute côté serveur et finit en dépassement de délai. Une tuile qui échoue
est simplement ignorée — il y en a des centaines ; l'indexation n'échoue que
si elles échouent toutes, et l'erreur cite alors le motif réseau exact.

### KartaView (aucune clé)

La seule source utilisable **sans aucune inscription**. Couverture nettement
plus clairsemée que Mapillary : parfait pour essayer tout de suite, insuffisant
pour couvrir sérieusement une ville moyenne.

⚠️ Le service a changé de nom, d'hôte et de version d'API au fil du temps, et
son point d'accès public n'est pas toujours stable. GeoFinder **sonde donc les
formes connues** au démarrage (`api.openstreetcam.org` et `api.kartaview.org`,
en v1 « nearby-photos » puis en v2 « bbox ») et garde celle qui répond. Si
aucune ne répond, l'erreur affichée cite ce que chaque tentative a obtenu.
En cas de souci, Mapillary est la solution : plus fiable et mieux couverte.

### Google Street View

Toujours supporté, mais nécessite une clé Cloud avec l'API **Street View
Static** activée et un compte de facturation. GeoFinder sonde d'abord
l'endpoint `metadata` (gratuit) pour ne télécharger que des panoramas existants
— seules les images téléchargées sont facturées.

### Mode démo

Si aucune source réelle n'est disponible, les vues sont générées localement.
Tout le pipeline fonctionne, mais **les positions retournées ne correspondent à
rien de réel** ; un bandeau orange le rappelle dans l'interface.

---

## Comment ça marche

### 1. Indexation d'une ville

| Étape | Détail |
|---|---|
| Géocodage | Nominatim (OSM) → centre + bounding box, bornée à `radius_km` |
| Découverte | dépend de la source (voir ci-dessous) |
| Regroupement | les vues sont groupées par lieu ; `max_panos` lieux × `headings` vues |
| Empreintes | CLIP ViT-B/32 (`laion2b_s34b_b79k`) → vecteurs 512-D L2-normalisés |
| Stockage | `/data/cities/<slug>/` : `index.json`, `embeddings.npy`, `images/` |

Deux familles de sources cohabitent derrière la même interface :

- **rendu à la demande** (Google, démo) — on choisit un point et un cap, le
  serveur produit l'image ; la découverte sonde une grille régulière
  (`spacing_m`), mélangée avec une graine fixe pour rester homogène si l'on
  s'arrête tôt ;
- **photos existantes** (Mapillary, KartaView) — les clichés sont déjà là, avec
  leur position et leur cap propres ; la découverte interroge l'emprise de la
  ville, tuile par tuile, et regroupe ce qui revient.

Le job tourne en tâche de fond, avec progression et annulation.

### 2. Recherche

1. La photo est encodée en **plusieurs recadrages** (entier, 70 %, 50 %). Un
   cliché de touriste couvre un champ bien plus large qu'une vue de rue ;
   chaque vue de la base est jugée sous le cadrage qui lui est le plus
   favorable.
2. Produit scalaire contre la matrice d'embeddings — les vecteurs étant
   normalisés, c'est directement le cosinus. Les 48 meilleurs sont retenus.
3. **Vérification géométrique** : appariement SIFT, test du ratio de Lowe,
   vérification croisée, puis **matrice fondamentale** estimée par RANSAC. Une
   homographie supposerait une scène plane ou une rotation pure — faux dans une
   rue, où les plans ont des profondeurs différentes ; la matrice fondamentale
   n'exige qu'une géométrie épipolaire cohérente.
4. **Vote de voisinage** : un lieu corroboré par d'autres bonnes réponses dans
   un rayon de 150 m est bonifié. C'est le garde-fou décisif — une confusion
   produit un pic isolé, un vrai lieu produit un groupe.
5. Score final `0.6 × visuel + 0.4 × géométrie`, puis vote de voisinage.

#### Savoir dire « je ne sais pas »

La similarité est jugée sur une **échelle absolue** (`SIM_FLOOR` → `SIM_CEIL`),
jamais relative au lot de candidats. Une normalisation relative — l'erreur des
premières versions — donne toujours la note maximale au premier candidat, même
quand rien ne correspond : le système désigne alors un lieu au hasard avec
aplomb. En dessous de `GEOFINDER_MIN_CONFIDENCE`, l'interface annonce
explicitement que la photo n'est probablement pas dans la zone couverte, au lieu
de trancher.

La marge de confiance se mesure contre le meilleur concurrent situé à plus de
250 m : les résultats suivants sont le plus souvent d'autres vues du **même**
endroit, et les compter comme rivaux faisait douter de bonnes réponses.

#### Cohérence du modèle

L'index enregistre le modèle qui l'a produit. Deux modèles produisent des
vecteurs incomparables : un index construit avec un autre est marqué
**PÉRIMÉ** et écarté de la recherche, plutôt que de fournir un classement
arbitraire d'apparence sérieuse. Il faut alors réindexer la ville.

---

## Configuration

Toutes les variables sont optionnelles : sans aucune, GeoFinder utilise KartaView.

| Variable | Défaut | Rôle |
|---|---|---|
| `GEOFINDER_PROVIDER` | `auto` | `auto`, `mapillary`, `kartaview`, `google`, `demo` |
| `MAPILLARY_ACCESS_TOKEN` | — | Jeton Mapillary gratuit (`MLY\|...`) |
| `GOOGLE_MAPS_API_KEY` | — | Clé Street View Static (facturation requise) |
| `GEOFINDER_DATA` | `/data` | Répertoire de persistance |
| `GEOFINDER_MAX_PANOS` | `250` | Lieux indexés par ville |
| `GEOFINDER_SPACING_M` | `120` | Pas de la grille de sondage, m (Google/démo) |
| `GEOFINDER_SV_SIZE` | `512x512` | Taille des vues rendues (Google/démo) |
| `GEOFINDER_SV_FOV` | `90` | Champ de vision, ° (Google/démo) |
| `GEOFINDER_WORKERS` | `8` | Requêtes en parallèle |
| `GEOFINDER_CONNECT_TIMEOUT` | `8` | Timeout d'établissement de connexion (s) |
| `GEOFINDER_DISCOVERY_TIMEOUT` | `45` | Timeout de lecture, découverte (s) |
| `GEOFINDER_DOWNLOAD_TIMEOUT` | `30` | Timeout de lecture, image (s) |
| `GEOFINDER_HTTP_RETRIES` | `2` | Reprises sur 429/5xx et coupures |
| `GEOFINDER_DISCOVERY_BUDGET` | `300` | Temps max de la phase de recherche (s) |
| `GEOFINDER_LOG_LEVEL` | `INFO` | `DEBUG` pour tout tracer |
| `GEOFINDER_RERANK` | `48` | Candidats passés à la vérification géométrique |
| `GEOFINDER_CLIP_WEIGHT` | `0.6` | Poids du visuel dans le score |
| `GEOFINDER_GEOMETRY_WEIGHT` | `0.4` | Poids de la vérification géométrique |
| `GEOFINDER_SIM_FLOOR` | `0.55` | Cosinus en dessous duquel c'est du bruit |
| `GEOFINDER_SIM_CEIL` | `0.90` | Cosinus valant une certitude visuelle |
| `GEOFINDER_INLIER_TARGET` | `25` | Inliers valant une certitude géométrique |
| `GEOFINDER_CONSENSUS_RADIUS_M` | `150` | Rayon du vote de voisinage |
| `GEOFINDER_CONSENSUS_WEIGHT` | `0.35` | Poids du vote de voisinage |
| `GEOFINDER_RIVAL_SEPARATION_M` | `250` | Distance à partir de laquelle un résultat est un vrai concurrent |
| `GEOFINDER_MIN_CONFIDENCE` | `0.35` | En dessous, GeoFinder annonce son incertitude |
| `GEOFINDER_QUERY_CROPS` | `1.0,0.7,0.5` | Recadrages de la requête |
| `GEOFINDER_CLIP_MODEL` | `ViT-B-16` | Architecture open_clip |
| `GEOFINDER_CLIP_PRETRAINED` | `laion2b_s34b_b88k` | Poids pré-entraînés |

---

## API

| Méthode | Route | Rôle |
|---|---|---|
| `GET` | `/api/health` | État, sources disponibles, modèle chargé |
| `GET` | `/api/cities` | Villes indexées |
| `POST` | `/api/cities` | Lance une indexation → `{job_id}` |
| `DELETE` | `/api/cities/{slug}` | Supprime un index |
| `GET` | `/api/cities/{slug}/images/{nom}` | Vignette d'une vue |
| `GET` | `/api/jobs/{id}` | Progression d'une indexation |
| `POST` | `/api/jobs/{id}/cancel` | Annule une indexation |
| `POST` | `/api/find` | `multipart` : `file`, `city`, `top_k`, `rerank` |

```bash
# indexer
curl -X POST localhost:8000/api/cities \
  -H 'Content-Type: application/json' \
  -d '{"city":"Bordeaux","provider":"mapillary","max_panos":300,"headings":4,"radius_km":5}'

# chercher
curl -X POST localhost:8000/api/find \
  -F file=@photo.jpg -F city=bordeaux -F top_k=5
```

---

## Développement

```bash
pip install -r backend/requirements.txt
GEOFINDER_DATA=./data uvicorn backend.app.main:app --reload
```

Tests — ils tournent **hors ligne** : mode démo pour Street View et encodeur de
substitution pour CLIP, ce qui permet d'exercer tout le pipeline (job
d'indexation, écriture disque, index numpy, recherche, ORB, routes HTTP) sans
clé API ni téléchargement de poids.

```bash
python -m pytest backend/tests -q
```

---

## Dépannage

Une source qui refuse de répondre ? L'outil de diagnostic montre chaque requête
et la réponse brute du serveur :

```bash
# dans le conteneur
docker compose exec geofinder python -m backend.tools.probe all --city Bordeaux

# ou en local
python -m backend.tools.probe kartaview --city Bordeaux
python -m backend.tools.probe mapillary --lat 44.8378 --lng -0.5792   # sans géocodage
```

Il affiche l'état de chaque source, les points d'accès essayés, le message
d'erreur exact de l'API, puis teste le téléchargement d'une image.

L'indexation journalise chaque étape dans les logs du conteneur, et toute
erreur y arrive avec sa trace complète :

```bash
docker compose -p geofinder logs -f geofinder
```

```
INFO  backend.app.indexer: [job 21eb…] démarrage — ville='Bordeaux' source=kartaview …
INFO  backend.app.indexer: [job 21eb…] Localisation de la ville…
ERROR backend.app.indexer: [job 21eb…] échec après 3s : Géocodage impossible (…)
```

Le même message s'affiche aussi, en entier et de façon persistante, sous la
barre de progression dans l'interface.

| Symptôme | Cause probable |
|---|---|
| `Conflict. The container name "/geofinder" is already in use` | Un conteneur est resté en place. `docker rm -f geofinder`, puis relance. Ne devrait plus se reproduire : le nom fixe a été retiré du compose |
| Le job reste bloqué sans fin | Ne devrait plus arriver : la découverte a un budget de temps (`GEOFINDER_DISCOVERY_BUDGET`, 5 min par défaut) et échoue avec un message explicite |
| `KartaView HTTP 400` / aucun point d'accès ne fonctionne | API publique instable ou changée — passe à Mapillary |
| `Mapillary a refusé le jeton (401/403)` | `MAPILLARY_ACCESS_TOKEN` absent ou mal copié (format `MLY\|...`) |
| `Aucune photo … dans cette zone` | La source répond mais ne couvre pas le secteur : élargis `radius_km` ou change de source |
| `Géocodage impossible` | Nominatim injoignable ou limite de débit atteinte — réessaie, ou utilise `--lat/--lng` avec l'outil de diagnostic. Le conteneur doit avoir un accès réseau sortant |
| `Budget de temps dépassé` | La source répond trop lentement : réduis `radius_km` et `max_panos`, ou change de source |
| `Read timed out` sur une source | Requête acceptée mais trop lourde : augmente `GEOFINDER_DISCOVERY_TIMEOUT`, ou réduis `radius_km` |
| `Modèle de reconnaissance indisponible` | Poids CLIP absents du cache et réseau coupé (l'image Docker les embarque) |
| Ville marquée **PÉRIMÉ**, `/api/find` renvoie 409 | Index construit avec un autre modèle : supprime-la et réindexe |
| « Localisation incertaine » systématique | La zone photographiée n'est pas couverte : réindexe avec un rayon plus petit et `max_panos` plus élevé |

---

## Limites connues

- **La couverture fait la précision, et c'est la limite dominante.** Une photo
  ne peut être localisée que si une vue proche a été indexée. 250 lieux sur un
  rayon de 6 km, c'est environ un point tous les 450 m : la plupart des rues
  sont absentes. Pour un résultat exploitable, vise plutôt **2 km de rayon et
  500 à 1000 lieux**. Aucun réglage d'algorithme ne compense une zone non
  couverte — au mieux, GeoFinder le reconnaît au lieu de deviner.
- **Les monuments sont un cas défavorable.** Une photo de la Tour Eiffel prise
  du Champ-de-Mars ressemble, pour un descripteur global, à toute esplanade
  dégagée avec une structure verticale. La vérification géométrique et le vote
  de voisinage corrigent une partie de ces confusions, à condition que le lieu
  réel soit dans l'index.
- **La couverture dépend de la source.** Mapillary et KartaView sont
  participatifs : les grands axes sont bien couverts, les ruelles beaucoup
  moins, et la qualité des photos varie (caméras d'action, pare-brise, vélos).
  Google est plus régulier, mais payant.
- **Intérieurs, gros plans, nature** : CLIP n'a rien à raccrocher à une façade ou
  à une géométrie de rue. Le système est fait pour des scènes de voirie.
- **Saisons, météo, travaux, heure de la journée** dégradent l'appariement ; les
  panoramas Street View peuvent dater de plusieurs années.
- **Recherche exhaustive** : le produit scalaire est fait sur toute la matrice.
  C'est instantané jusqu'à quelques dizaines de milliers de vues ; au-delà, il
  faudrait un index ANN (FAISS, HNSW).
- Le suivi des jobs est **en mémoire** : une indexation en cours ne survit pas à
  un redémarrage du conteneur (les index déjà écrits, eux, sont persistés).

## Crédits

Images © contributeurs [Mapillary](https://www.mapillary.com) /
[KartaView](https://kartaview.org) (CC BY-SA) ou © Google Street View selon la
source choisie · Fond de carte © OpenStreetMap · Géocodage Nominatim ·
Modèle [OpenCLIP](https://github.com/mlfoundations/open_clip)
