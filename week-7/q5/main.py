from fastapi import FastAPI
from datetime import datetime, timezone

app = FastAPI()


VALID_TYPES = {
    "dns",
    "ct_log",
    "registry",
    "archive",
    "scan"
}


def response(verdict, confidence, ids):
    return {
        "verdict": verdict,
        "confidence": confidence,
        "corroboratingSources": ids
    }



@app.post("/corroborate")
def corroborate(data: dict):


    # -------------------
    # 1. Invalid
    # -------------------

    if not isinstance(data, dict):
        return response(
            "invalid","low",[]
        )


    try:

        claim=data["claim"]

        if not isinstance(claim,dict):
            return response(
                "invalid","low",[]
            )


        if not isinstance(
            claim.get("value"),
            str
        ):
            return response(
                "invalid","low",[]
            )


        asof=datetime.fromisoformat(
            data["asOf"].replace(
                "Z","+00:00"
            )
        )


        stale=data["stalenessDays"]

        if not isinstance(
            stale,
            (int,float)
        ):
            return response(
                "invalid","low",[]
            )


        sources=data["sources"]

        if not isinstance(
            sources,list
        ):
            return response(
                "invalid","low",[]
            )


    except Exception:

        return response(
            "invalid",
            "low",
            []
        )



    claim_value=claim["value"]


    fresh=[]


    # -------------------
    # Prepare sources
    # -------------------

    for s in sources:


        if not isinstance(s,dict):
            continue


        required=[
            "id",
            "origin",
            "value",
            "observedAt"
        ]


        if any(
            x not in s for x in required
        ):
            continue


        if not all(
            isinstance(s[x],str)
            for x in required
        ):
            continue


        if s.get("type") not in VALID_TYPES:
            continue


        try:

            observed=datetime.fromisoformat(
                s["observedAt"]
                .replace("Z","+00:00")
            )

        except:
            continue


        age=(asof-observed).total_seconds()/86400


        if age <= stale:

            fresh.append(s)



    # -------------------
    # 2. Contradiction
    # -------------------

    bad=[]

    for s in fresh:

        if (
            s.get("authoritative") is True
            and s["value"] != claim_value
        ):
            bad.append(s["id"])


    if bad:

        return response(
            "contradicted",
            "low",
            sorted(bad)
        )



    # -------------------
    # 3. Supported
    # -------------------

    matching=[
        s for s in fresh
        if s["value"] == claim_value
    ]


    # choose smallest id per origin

    representatives={}


    for s in matching:

        origin=s["origin"]

        if (
            origin not in representatives
            or s["id"] < representatives[origin]["id"]
        ):
            representatives[origin]=s



    reps=list(representatives.values())


    if len(reps)>=2:

        ids=sorted(
            s["id"] for s in reps
        )


        types=set(
            s["type"]
            for s in reps
        )


        confidence=(
            "high"
            if len(types)>=2
            else "medium"
        )


        return response(
            "supported",
            confidence,
            ids
        )



    # -------------------
    # 4. Unverified
    # -------------------

    return response(
        "unverified",
        "low",
        []
    )