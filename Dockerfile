FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY *.py ./
COPY config.yaml entrypoint.sh ./
COPY sources ./sources

CMD ["sh", "/app/entrypoint.sh"]
