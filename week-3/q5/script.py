import json
import numpy as np

with open('q-cosine-similarity-server.json') as f:
    data = json.load(f)

docs = data['documents']
queries = data['queries']

doc_ids = [d['doc_id'] for d in docs]
doc_matrix = np.array([d['embedding'] for d in docs])

results = {}
for q in queries:
    qvec = np.array(q['embedding'])
    doc_norms = doc_matrix / np.linalg.norm(doc_matrix, axis=1, keepdims=True)
    qnorm = qvec / np.linalg.norm(qvec)
    sims = doc_norms @ qnorm
    order = sorted(range(len(doc_ids)), key=lambda i: (-sims[i], doc_ids[i]))
    results[q['query_id']] = [doc_ids[i] for i in order[:5]]

print(json.dumps(results, indent=2))