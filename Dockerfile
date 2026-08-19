FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HF_HOME=/models \
    GEOFINDER_DATA=/data \
    PYTHONPATH=/app

# libglib2.0-0 : requis par opencv-python-headless. curl : healthcheck.
RUN apt-get update \
 && apt-get install -y --no-install-recommends libglib2.0-0 curl \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Torch CPU depuis l'index PyTorch : ~5x plus léger que la roue CUDA par défaut.
RUN pip install --index-url https://download.pytorch.org/whl/cpu \
    torch==2.5.1 torchvision==0.20.1

COPY backend/requirements.txt /app/backend/requirements.txt
RUN pip install -r /app/backend/requirements.txt

# Poids CLIP embarqués dans l'image : le conteneur démarre sans rien télécharger.
# ViT-B/16 découpe l'image en patches deux fois plus fins que ViT-B/32 et
# distingue nettement mieux deux façades semblables.
ARG CLIP_MODEL=ViT-B-16
ARG CLIP_PRETRAINED=laion2b_s34b_b88k
RUN M="$CLIP_MODEL" P="$CLIP_PRETRAINED" python -c \
      "import open_clip, os; open_clip.create_model_and_transforms(os.environ['M'], pretrained=os.environ['P'])" \
    && echo "modele $CLIP_MODEL/$CLIP_PRETRAINED embarque"
ENV GEOFINDER_CLIP_MODEL=$CLIP_MODEL GEOFINDER_CLIP_PRETRAINED=$CLIP_PRETRAINED

COPY backend/ /app/backend/
COPY frontend/ /app/frontend/

RUN mkdir -p /data
VOLUME ["/data"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD curl -fsS http://localhost:8000/api/health || exit 1

CMD ["uvicorn", "backend.app.main:app", "--host", "0.0.0.0", "--port", "8000"]
