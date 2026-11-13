FROM python:3.12-slim

# HF Spaces run containers as uid 1000
RUN useradd -m -u 1000 user
ENV HOME=/home/user HF_HOME=/home/user/.cache/huggingface PYTHONUNBUFFERED=1 \
    DATA_DIR=/home/user/index LOG_DIR=/home/user/usage_logs
WORKDIR /home/user/app

COPY --chown=user requirements.txt .
RUN pip install --no-cache-dir torch==2.15.0 --index-url https://download.pytorch.org/whl/cpu && \
    pip install --no-cache-dir -r requirements.txt

USER user
# Bake the public models into the image so restarts only download the index
RUN python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('BAAI/bge-m3'); \
from transformers import pipeline; pipeline('token-classification', model='Davlan/distilbert-base-multilingual-cased-ner-hrl')"

COPY --chown=user . .
EXPOSE 7860
CMD ["python", "app.py"]
