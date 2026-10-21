import os
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.db import DEFAULT_DATABASE_PATH, database, initialize_database


@asynccontextmanager
async def lifespan(app: FastAPI):
    with database(DEFAULT_DATABASE_PATH) as connection:
        initialize_database(connection)
    yield


app = FastAPI(lifespan=lifespan)


@app.get("/api/health")
def get_health():
    api_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()

    return {
        "status": "ok",
        "model_configured": bool(api_key),
    }
