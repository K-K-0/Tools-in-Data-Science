import hashlib
import json
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

app = FastAPI()

# In-memory session store. 
# NOTE: Use a persistent store (like Redis or a Database) for production deployments across multiple workers.
sessions: dict[str, dict] = {}

NODES = ['verify_data', 'prepare', 'train', 'evaluate', 'register', 'publish']
REQUIRED_INPUTS = [
    'generation', 'checksum', 'canonicalData', 'prepareCode', 'prepareConfig',
    'trainCode', 'trainConfig', 'runtime', 'evaluateCode', 'evaluateConfig',
    'schemaDigest', 'publishConfig'
]
VALID_STATUSES = {'started', 'succeeded', 'retryable_failed', 'terminal_failed'}


def sha256_hash(s: str) -> str:
    return hashlib.sha256(s.encode('utf-8')).hexdigest()

def compact_json(obj) -> str:
    return json.dumps(obj, separators=(',', ':'), ensure_ascii=False)

def error(code: str) -> JSONResponse:
    return JSONResponse(status_code=409, content={"error": code})

def get_cache_key(node: str, inputs: dict, node_states: dict[str, dict]) -> str | None:
    ni = node_states.get(node, {})
    if ni.get('key') is not None:
        return ni['key']
    
    if node == 'verify_data':
        return sha256_hash(compact_json([inputs['generation'], inputs['checksum']]))
    if node == 'prepare':
        return sha256_hash(compact_json([inputs['canonicalData'], inputs['prepareCode'], inputs['prepareConfig']]))
    
    idx = NODES.index(node)
    parent_node = NODES[idx - 1]
    parent_artifact = node_states.get(parent_node, {}).get('artifactDigest')
    if not parent_artifact:
        return None
        
    if node == 'train':
        return sha256_hash(compact_json([parent_artifact, inputs['trainCode'], inputs['trainConfig'], inputs['runtime']]))
    if node == 'evaluate':
        return sha256_hash(compact_json([parent_artifact, inputs['canonicalData'], inputs['evaluateCode'], inputs['evaluateConfig']]))
    if node == 'register':
        return sha256_hash(compact_json([parent_artifact, inputs['schemaDigest']]))
    if node == 'publish':
        return sha256_hash(compact_json([parent_artifact, inputs['publishConfig']]))
    return None

@app.post("/pipeline")
async def handle_pipeline(request: Request):
    try:
        body = await request.json()
    except Exception:
        return error("INVALID_REQUEST")
        
    if not isinstance(body, dict):
        return error("INVALID_REQUEST")

    session = body.get("session")
    revision = body.get("revision")
    inputs = body.get("inputs")
    events = body.get("events")

    if not isinstance(session, str) or not session.strip() or \
       not isinstance(revision, int) or revision < 1 or revision > 2**53 - 1 or \
       not isinstance(inputs, dict) or not isinstance(events, list):
        return error("INVALID_REQUEST")

    for k in REQUIRED_INPUTS:
        if not isinstance(inputs.get(k), str) or not inputs[k].strip():
            return error("INVALID_REQUEST")

    S = sessions.get(session)
    if not S or S['revision'] != revision:
        S = {
            "revision": revision,
            "inputs": json.loads(compact_json(inputs)),
            "nodes": {n: {"status": "none", "attempt": 0, "key": None, "artifactDigest": None, "eventId": None, "receiptId": None, "triggeredEventId": None} for n in NODES}
        }
        sessions[session] = S
    else:
        if compact_json(S['inputs']) != compact_json(inputs):
            return error("REVISION_CONFLICT")

    accepted_ids = []
    ignored_ids = []
    event_id_cache = {}
    seen_ids_batch = set()

    for ev in events:
        if not isinstance(ev, dict):
            return error("INVALID_EVENT")

        ev_id = ev.get("eventId")
        ev_rev = ev.get("revision")
        ev_node = ev.get("node")
        ev_attempt = ev.get("attempt")
        ev_status = ev.get("status")
        ev_key = ev.get("key")
        ev_art = ev.get("artifactDigest")
        ev_rcpt = ev.get("receiptId")

        # Basic Type and Value Validation
        if not isinstance(ev_id, str) or not ev_id.strip() or \
           not isinstance(ev_rev, int) or \
           not isinstance(ev_node, str) or ev_node not in NODES or \
           not isinstance(ev_attempt, int) or ev_attempt < 1 or ev_attempt > 2**53 - 1 or \
           not isinstance(ev_status, str) or ev_status not in VALID_STATUSES or \
           (ev_key is not None and not isinstance(ev_key, str)) or \
           (ev_art is not None and not isinstance(ev_art, str)) or \
           (ev_rcpt is not None and not isinstance(ev_rcpt, str)):
            return error("INVALID_EVENT")

        # Ignore based on immutable properties
        if ev_rev != revision or ev_node not in S['nodes']:
            ignored_ids.append(ev_id)
            continue

        expected_key = get_cache_key(ev_node, inputs, S['nodes'])
        if expected_key is None or ev_key != expected_key:
            ignored_ids.append(ev_id)
            continue

        # Artifact / Receipt constraints
        if ev_art is not None and ev_status != 'succeeded':
            ignored_ids.append(ev_id)
            continue
        if ev_art is None and ev_status == 'succeeded':
            ignored_ids.append(ev_id)
            continue

        needs_receipt = ev_node in ('register', 'publish')
        if needs_receipt and ev_status == 'succeeded':
            if not isinstance(ev_rcpt, str) or not ev_rcpt.startswith(f"receipt:{ev_node}:{ev_key}"):
                ignored_ids.append(ev_id)
                continue
        else:
            if ev_rcpt is not None:
                ignored_ids.append(ev_id)
                continue

        # Event ID Uniqueness & Immutability
        ev_json = compact_json(ev)
        if ev_id in seen_ids_batch:
            ignored_ids.append(ev_id)
            continue

        prev_ev_json = event_id_cache.get(ev_id)
        if prev_ev_json is not None:
            if prev_ev_json == ev_json:
                ignored_ids.append(ev_id)
                continue
            else:
                return error("EVENT_ID_CONFLICT")

        ni = S['nodes'][ev_node]
        existing_ev_json = None
        if ni['eventId'] == ev_id:
            existing_ev_json = compact_json({
                "eventId": ni['eventId'], "revision": revision, "node": ev_node, "attempt": ni['attempt'],
                "status": ni['status'], "key": ni['key'], "artifactDigest": ni['artifactDigest'], "receiptId": ni['receiptId']
            })

        if existing_ev_json is not None:
            if existing_ev_json == ev_json:
                ignored_ids.append(ev_id)
                continue
            else:
                return error("EVENT_ID_CONFLICT")

        event_id_cache[ev_id] = ev_json
        seen_ids_batch.add(ev_id)

        # State Machine Transitions
        status = ni['status']
        attempt = ni['attempt']

        if status == 'none':
            if ev_status == 'started' and ev_attempt == 1:
                ni['status'] = 'started'
                ni['attempt'] = 1
                ni['triggeredEventId'] = ev_id
                accepted_ids.append(ev_id)
            else:
                ignored_ids.append(ev_id)
        elif status == 'started':
            if ev_status in ('succeeded', 'retryable_failed', 'terminal_failed') and ev_attempt == attempt:
                ni['status'] = ev_status
                if ev_status == 'succeeded':
                    ni['artifactDigest'] = ev_art
                    ni['receiptId'] = ev_rcpt
                    ni['eventId'] = ev_id
                accepted_ids.append(ev_id)
            else:
                return error("STATUS_CONFLICT")
        elif status == 'retryable_failed':
            if ev_status == 'started' and ev_attempt == attempt + 1:
                ni['status'] = 'started'
                ni['attempt'] = ev_attempt
                ni['triggeredEventId'] = ev_id
                accepted_ids.append(ev_id)
            else:
                return error("STATUS_CONFLICT")
        elif status in ('succeeded', 'terminal_failed'):
            return error("STATUS_CONFLICT")

    # Construct Response
    res_nodes = []
    for i, name in enumerate(NODES):
        ni = S['nodes'][name]
        c_key = get_cache_key(name, inputs, S['nodes'])
        deps = {"cacheKey": c_key}

        if name == 'verify_data':
            deps['generation'] = inputs['generation']
            deps['checksum'] = inputs['checksum']
        elif name == 'prepare':
            deps['canonicalData'] = inputs['canonicalData']
            deps['prepareCode'] = inputs['prepareCode']
            deps['prepareConfig'] = inputs['prepareConfig']
        elif name == 'train':
            deps['prepareArtifact'] = S['nodes']['verify_data'].get('artifactDigest')
            deps['trainCode'] = inputs['trainCode']
            deps['trainConfig'] = inputs['trainConfig']
            deps['runtime'] = inputs['runtime']
        elif name == 'evaluate':
            deps['trainArtifact'] = S['nodes']['train'].get('artifactDigest')
            deps['canonicalData'] = inputs['canonicalData']
            deps['evaluateCode'] = inputs['evaluateCode']
            deps['evaluateConfig'] = inputs['evaluateConfig']
        elif name == 'register':
            deps['evaluateArtifact'] = S['nodes']['evaluate'].get('artifactDigest')
            deps['schemaDigest'] = inputs['schemaDigest']
        elif name == 'publish':
            deps['registerArtifact'] = S['nodes']['register'].get('artifactDigest')
            deps['publishConfig'] = inputs['publishConfig']

        te_ids = []
        if ni['triggeredEventId']:
            te_ids.append(ni['triggeredEventId'])
        if ni['status'] == 'succeeded' and ni['eventId']:
            te_ids.append(ni['eventId'])

        if ni['status'] == 'succeeded':
            action, reason = 'reuse', 'CACHE_HIT'
        elif ni['status'] in ('started', 'retryable_failed'):
            action, reason = 'block', 'RUNNING'
        elif ni['status'] == 'terminal_failed':
            action, reason = 'block', 'TERMINAL_FAILURE'
        else:
            p_status = S['nodes'][NODES[i-1]]['status'] if i > 0 else None
            if p_status == 'terminal_failed':
                action, reason = 'block', 'UPSTREAM_TERMINAL'
            elif p_status != 'succeeded' and i > 0:
                action, reason = 'block', 'UPSTREAM_PENDING'
            else:
                action, reason = 'rerun', 'CACHE_MISS'

        res_nodes.append({
            "node": name,
            "action": action,
            "reasonCodes": [reason],
            "dependencyDigests": deps,
            "triggeringEventIds": te_ids
        })

    return JSONResponse(content={
        "revision": revision,
        "acceptedEventIds": accepted_ids,
        "ignoredEventIds": ignored_ids,
        "nodes": res_nodes
    })