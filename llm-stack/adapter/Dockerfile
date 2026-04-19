FROM python:3.11-slim

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        apache2 \
        bash \
        ca-certificates \
        composer \
        git \
        nginx \
        nodejs \
        npm \
        php-cli \
        php-zip \
        phpunit \
        unzip \
    && rm -rf /var/lib/apt/lists/*

COPY *.py .

RUN pip install --no-cache-dir \
    fastapi \
    uvicorn \
    requests \
    httpx \
    numpy

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "4000"]
