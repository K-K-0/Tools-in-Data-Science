from fastapi import FastAPI

app = FastAPI()


TENANT = "tenant-dbk7uxp"
EMAIL_DOMAIN = "notify-3vtvlmp.example"


def block(reason):
    return {
        "decision":"block",
        "reason":reason
    }


@app.post("/action-firewall")
def firewall(data:dict):

    # 1. Top-level schema

    required = [
        "provenance",
        "humanApproved",
        "action"
    ]

    if any(x not in data for x in required):
        return block("INVALID_SCHEMA")


    action=data["action"]

    if "tool" not in action or "args" not in action:
        return block("INVALID_SCHEMA")


    tool=action["tool"]
    args=action["args"]


    # 2. Tool allowlist

    allowed=[
        "search",
        "lookup_record",
        "send_email",
        "render_html"
    ]

    if tool not in allowed:
        return block("TOOL_NOT_ALLOWED")


    # 3. Argument schema

    if tool=="search":

        if set(args.keys()) != {"query"}:
            return block("INVALID_SCHEMA")

        if not isinstance(args["query"],str):
            return block("INVALID_SCHEMA")

        if len(args["query"]) < 1 or len(args["query"]) > 200:
            return block("INVALID_SCHEMA")


    elif tool=="lookup_record":

        if set(args.keys()) != {
            "tenantId",
            "recordId"
        }:
            return block("INVALID_SCHEMA")

        if not args["recordId"]:
            return block("INVALID_SCHEMA")


        # 4. tenant

        if args["tenantId"] != TENANT:
            return block("TENANT_SCOPE")


    elif tool=="send_email":

        if set(args.keys()) != {
            "to",
            "subject",
            "body"
        }:
            return block("INVALID_SCHEMA")


        email=args["to"]

        if not email.endswith("@"+EMAIL_DOMAIN):
            return block("EGRESS_DENIED")


        # approval

        if data["humanApproved"] is not True:
            return block("APPROVAL_REQUIRED")


    elif tool=="render_html":

        if set(args.keys()) != {"html"}:
            return block("INVALID_SCHEMA")


        html=args["html"].lower()


        bad=[
            "<script",
            "<iframe",
            "onerror=",
            "onclick=",
            "javascript:"
        ]

        for x in bad:
            if x in html:
                return block("UNSAFE_OUTPUT")


    return {
        "decision":"allow",
        "reason":"ALLOW"
    }