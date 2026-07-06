import numpy as np
import faiss


# -----------------------------------
# Initialize OpenAI client
# -----------------------------------


# -----------------------------------
# Sample Documents
# -----------------------------------
documents = [
    "Machine learning is a subset of Artificial Intelligence.",
    "Python is one of the most popular programming languages.",
    "FAISS is a library developed by Meta for efficient vector similarity search.",
    "Deep learning uses neural networks with many layers.",
    "Retrieval-Augmented Generation (RAG) combines retrieval with LLMs.",
    "LangChain is a framework for building LLM applications.",
    "Vector databases store embeddings for semantic search.",
    "Natural Language Processing helps computers understand text."
]

# -----------------------------------
# Generate Document Embeddings
# -----------------------------------
print("Generating document embeddings...")

response = client.embeddings.create(
    model="text-embedding-3-small",
    input=documents
)

embeddings = [item.embedding for item in response.data]

# -----------------------------------
# Build FAISS Index
# -----------------------------------
dim = len(embeddings[0])  # 1536 for text-embedding-3-small

index = faiss.IndexFlatIP(dim)

corpus_np = np.array(embeddings, dtype=np.float32)

# Normalize vectors (required for cosine similarity)
faiss.normalize_L2(corpus_np)

index.add(corpus_np)

print(f"FAISS index built with {index.ntotal} vectors.\n")

# -----------------------------------
# User Query
# -----------------------------------
query = "What is FAISS used for?"

query_response = client.embeddings.create(
    model="text-embedding-3-small",
    input=query
)

query_embedding = query_response.data[0].embedding

query_np = np.array([query_embedding], dtype=np.float32)
faiss.normalize_L2(query_np)

# -----------------------------------
# Search
# -----------------------------------
k = 5

distances, indices = index.search(query_np, k)

print("=" * 60)
print("Top Matching Documents")
print("=" * 60)

for rank, (score, idx) in enumerate(zip(distances[0], indices[0]), start=1):
    print(f"\nRank {rank}")
    print(f"Similarity Score : {score:.4f}")
    print(f"Document ID      : {idx}")
    print(f"Document         : {documents[idx]}")