FROM python:3.11-slim

WORKDIR /app
COPY exporter.py .

RUN pip install fastapi uvicorn requests

CMD ["uvicorn", "exporter:app", "--host", "0.0.0.0", "--port", "9101"]

