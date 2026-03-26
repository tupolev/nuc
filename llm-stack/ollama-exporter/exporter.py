from fastapi import FastAPI
from fastapi.responses import PlainTextResponse
import requests
import time

app = FastAPI()

OLLAMA = "http://host.docker.internal:11434"

@app.get("/metrics")
def metrics():
    try:
        tags = requests.get(f"{OLLAMA}/api/tags", timeout=5).json()
        models = len(tags.get("models", []))
    except:
        models = -1

    try:
        ps = requests.get(f"{OLLAMA}/api/ps", timeout=5).json()
        running = len(ps.get("models", []))
    except:
        running = -1

    return PlainTextResponse(
        f"""
ollama_models_total {models}
ollama_models_running {running}
""".strip()
    )

