import os

from fastapi import FastAPI


app = FastAPI()


@app.get("/api/health")
def get_health():
    api_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()

    return {
        "status": "ok",
        "model_configured": bool(api_key),
    }
