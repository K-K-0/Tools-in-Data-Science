"""
Semantic Search Ranking API.

Endpoint: POST /rank
Request:  {"query_id": "...", "query": "...", "candidates": ["...", ...]}
Response: {"ranking": [i, j, k]}   -- indices of top-3 most similar candidates

Deploy with: uvicorn main:app --host 0.0.0.0 --port 8001
"""

import os
from typing import List

import numpy as np
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from openai import OpenAI

app = FastAPI(title="Semantic Search Ranking API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Use AIPIPE_API_KEY if set, otherwise fall back to a plain OpenAI key.
# AIPipe is OpenAI-compatible so the same client works for both.
API_KEY = "eyJhbGciOiJIUzI1NiJ9.eyJlbWFpbCI6IjI0ZjEwMDIwNTJAZHMuc3R1ZHkuaWl0bS5hYy5pbiIsImlhdCI6MTc4MzY2MTg2NSwiaXNzIjoiaHR0cHM6Ly9haXBpcGUub3JnIiwiYXVkIjoiYWlwaXBlLWFwaSIsImV4cCI6MTc4NDI2NjY2NX0.bz34zqEgjZ3fR69QWSjN60c7l-DAZgyiGzcVzCaDjBo"
BASE_URL = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")
EMBED_MODEL = "text-embedding-3-small"  # fixed by the task spec -- ranking is defined by this exact model

client = OpenAI(api_key=API_KEY, base_url=BASE_URL)


class RankRequest(BaseModel):
    query_id: str
    query: str
    candidates: List[str]


class RankResponse(BaseModel):
    ranking: List[int]


def embed_texts(texts: List[str]) -> np.ndarray:
    """Embed a batch of texts in a single API call. Returns array of shape (n, dim)."""
    response = client.embeddings.create(model=EMBED_MODEL, input=texts)
    # response.data is returned in the same order as the input list
    vectors = [d.embedding for d in response.data]
    return np.array(vectors)


@app.post("/rank", response_model=RankResponse)
def rank(req: RankRequest):
    if not req.query or not req.candidates:
        raise HTTPException(status_code=400, detail="query and candidates are required")

    try:
        # Batch query + all candidates into ONE embedding call for efficiency
        all_texts = [req.query] + req.candidates
        embeddings = embed_texts(all_texts)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Embedding call failed: {e}")

    query_vec = embeddings[0]
    candidate_vecs = embeddings[1:]

    # Cosine similarity: normalize then dot product
    query_norm = query_vec / np.linalg.norm(query_vec)
    candidate_norms = candidate_vecs / np.linalg.norm(candidate_vecs, axis=1, keepdims=True)
    sims = candidate_norms @ query_norm

    # Top 3 indices by similarity, descending
    top3 = np.argsort(-sims)[:3].tolist()

    return RankResponse(ranking=top3)


@app.get("/")
def health_check():
    return {"status": "ok"}