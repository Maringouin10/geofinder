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

1. La photo est encodée par le même modèle CLIP.
2. Produit scalaire contre la matrice d'embeddings de la ville — les vecteurs
   étant normalisés, c'est directement le cosinus. Les 40 meilleurs candidats
   sont retenus.
3. **Revérification géométrique** : pour chaque candidat, appariement ORB puis
   homographie RANSAC. Le nombre d'inliers distingue « deux rues qui se
   ressemblent » de « la même rue » — c'est ce qui fait la différence entre un
   classement plausible et une vraie identification.
4. Score final `0.75 × CLIP + 0.25 × ORB`, agrégé par panorama, affiché sur une
   carte Leaflet avec vignettes et lien vers la source de l'image.

La **confiance** affichée combine la similarité absolue du premier résultat, sa
marge sur les suivants, et le nombre de points géométriquement vérifiés. En
dessous de 60 %, l'interface avertit que la photo n'est probablement pas dans la
zone indexée.

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
| `GEOFINDER_RERANK` | `40` | Candidats passés à la vérification ORB |
| `GEOFINDER_CLIP_WEIGHT` | `0.75` | Poids CLIP vs ORB dans le score final |
| `GEOFINDER_CLIP_MODEL` | `ViT-B-32` | Architecture open_clip |
| `GEOFINDER_CLIP_PRETRAINED` | `laion2b_s34b_b79k` | Poids pré-entraînés |

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

---

## Limites connues

- **La couverture fait la précision.** Une photo ne peut être localisée que si
  une vue proche a été indexée. Avec 250 lieux sur 6 km de rayon, la grille est
  lâche : augmente `max_panos` et réduis le rayon pour une zone dense.
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
