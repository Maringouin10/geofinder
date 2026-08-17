# GeoFinder

Géolocalisation d'une photo **par reconnaissance d'image**. Tu donnes une ville,
GeoFinder télécharge et indexe ses panoramas Google Street View, puis tu déposes
une photo, tu appuies sur **FIND**, et il te dit où elle a été prise — sur une carte.

Aucune donnée EXIF n'est utilisée : la position est déduite **uniquement du
contenu visuel** de l'image.

```
photo ──► CLIP ──► recherche cosinus ──► top-40 candidats ──► ORB + RANSAC ──► carte
              (empreinte)      (index de la ville)      (vérification géométrique)
```

---

## Démarrage rapide

```bash
cp .env.example .env          # puis renseigne GOOGLE_MAPS_API_KEY
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
  -e GOOGLE_MAPS_API_KEY=... geofinder
```

### Mode démo (sans clé API)

Si `GOOGLE_MAPS_API_KEY` est vide, GeoFinder démarre en **mode démo** : les
panoramas sont des images synthétiques générées localement. Tout le pipeline
fonctionne (indexation, recherche, carte), ce qui permet de tester l'application
sans compte Google — mais **les positions retournées ne correspondent à rien de
réel**. Un bandeau orange le rappelle dans l'interface.

### Clé Google

Il faut une clé Google Cloud avec l'API **Street View Static** activée.
GeoFinder sonde d'abord l'endpoint `metadata` (gratuit) pour ne télécharger que
des panoramas qui existent réellement — seules les images téléchargées sont
facturées. Le coût d'une indexation est donc `panoramas × vues par panorama`
(250 × 4 = 1 000 images par défaut), affiché dans les options de l'interface.

---

## Comment ça marche

### 1. Indexation d'une ville

| Étape | Détail |
|---|---|
| Géocodage | Nominatim (OSM) → centre + bounding box, bornée à `radius_km` |
| Échantillonnage | Grille régulière (`spacing_m`), mélangée avec une graine fixe pour une couverture homogène même si l'on s'arrête tôt |
| Sondage | `streetview/metadata` sur chaque point → panorama le plus proche, dédupliqué par `pano_id` |
| Collecte | `streetview` image pour chaque cap (0°/90°/180°/270° par défaut) |
| Empreintes | CLIP ViT-B/32 (`laion2b_s34b_b79k`) → vecteurs 512-D L2-normalisés |
| Stockage | `/data/cities/<slug>/` : `index.json`, `embeddings.npy`, `images/` |

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
   carte Leaflet avec vignettes et lien Street View.

La **confiance** affichée combine la similarité absolue du premier résultat, sa
marge sur les suivants, et le nombre de points géométriquement vérifiés. En
dessous de 60 %, l'interface avertit que la photo n'est probablement pas dans la
zone indexée.

---

## Configuration

Toutes les variables sont optionnelles sauf la clé API.

| Variable | Défaut | Rôle |
|---|---|---|
| `GOOGLE_MAPS_API_KEY` | — | Clé Street View Static. Vide → mode démo |
| `GEOFINDER_DATA` | `/data` | Répertoire de persistance |
| `GEOFINDER_MAX_PANOS` | `250` | Panoramas par ville |
| `GEOFINDER_SPACING_M` | `120` | Pas de la grille de sondage (m) |
| `GEOFINDER_SV_SIZE` | `512x512` | Taille des vues téléchargées |
| `GEOFINDER_SV_FOV` | `90` | Champ de vision (°) |
| `GEOFINDER_WORKERS` | `8` | Requêtes Street View en parallèle |
| `GEOFINDER_RERANK` | `40` | Candidats passés à la vérification ORB |
| `GEOFINDER_CLIP_WEIGHT` | `0.75` | Poids CLIP vs ORB dans le score final |
| `GEOFINDER_CLIP_MODEL` | `ViT-B-32` | Architecture open_clip |
| `GEOFINDER_CLIP_PRETRAINED` | `laion2b_s34b_b79k` | Poids pré-entraînés |

---

## API

| Méthode | Route | Rôle |
|---|---|---|
| `GET` | `/api/health` | État, mode démo, modèle chargé |
| `GET` | `/api/cities` | Villes indexées |
| `POST` | `/api/cities` | Lance une indexation → `{job_id}` |
| `DELETE` | `/api/cities/{slug}` | Supprime un index |
| `GET` | `/api/cities/{slug}/images/{nom}` | Vignette d'un panorama |
| `GET` | `/api/jobs/{id}` | Progression d'une indexation |
| `POST` | `/api/jobs/{id}/cancel` | Annule une indexation |
| `POST` | `/api/find` | `multipart` : `file`, `city`, `top_k`, `rerank` |

```bash
# indexer
curl -X POST localhost:8000/api/cities \
  -H 'Content-Type: application/json' \
  -d '{"city":"Bordeaux","max_panos":300,"headings":4,"radius_km":5}'

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

## Limites connues

- **La couverture fait la précision.** Une photo ne peut être localisée que si un
  panorama proche a été indexé. Avec 250 panoramas et un rayon de 6 km, la
  grille est lâche : augmente `max_panos` et réduis `spacing_m` pour une zone
  dense, quitte à indexer un rayon plus petit.
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

Panoramas © Google Street View · Fond de carte © OpenStreetMap ·
Géocodage Nominatim · Modèle [OpenCLIP](https://github.com/mlfoundations/open_clip)
