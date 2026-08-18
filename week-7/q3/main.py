from fastapi import FastAPI

app = FastAPI()

WORKSPACE = "prod-xvt19t"

REQUIRED_LABELS = {
    "owner": "student-duwdw",
    "environment": "production",
    "cost_center": "cc-mtaz"
}


def reject(reason):
    return {
        "decision": "reject",
        "reason": reason
    }


@app.post("/terraform/plan")
def terraform_plan(data: dict):

    # -------------------------
    # 1. Schema validation
    # -------------------------

    if not isinstance(data, dict):
        return reject("INVALID_PLAN")

    top_required = {
        "environment",
        "state",
        "providerVersion",
        "destroyApproved",
        "resource"
    }

    if set(data.keys()) != top_required:
        return reject("INVALID_PLAN")


    if not isinstance(data["environment"], str):
        return reject("INVALID_PLAN")

    if not isinstance(data["providerVersion"], str):
        return reject("INVALID_PLAN")

    if not isinstance(data["destroyApproved"], bool):
        return reject("INVALID_PLAN")


    # state validation

    state = data["state"]

    if not isinstance(state, dict):
        return reject("INVALID_PLAN")

    if set(state.keys()) != {"backend", "locked"}:
        return reject("INVALID_PLAN")

    if not isinstance(state["backend"], str):
        return reject("INVALID_PLAN")

    if not isinstance(state["locked"], bool):
        return reject("INVALID_PLAN")


    # resource validation

    resource = data["resource"]

    if not isinstance(resource, dict):
        return reject("INVALID_PLAN")


    required_resource = {
        "address",
        "type",
        "action",
        "labels",
        "secret",
        "forceDestroy"
    }

    if set(resource.keys()) != required_resource:
        return reject("INVALID_PLAN")


    if not isinstance(resource["address"], str):
        return reject("INVALID_PLAN")

    if not isinstance(resource["type"], str):
        return reject("INVALID_PLAN")

    if resource["action"] not in [
        "create",
        "update",
        "delete"
    ]:
        return reject("INVALID_PLAN")


    if not isinstance(resource["labels"], dict):
        return reject("INVALID_PLAN")


    if resource["secret"] is not None and not isinstance(resource["secret"], str):
        return reject("INVALID_PLAN")


    if not isinstance(resource["forceDestroy"], bool):
        return reject("INVALID_PLAN")


    # -------------------------
    # 2. Environment
    # -------------------------

    if data["environment"] != WORKSPACE:
        return reject("ENVIRONMENT_MISMATCH")


    # -------------------------
    # 3. State safety
    # -------------------------

    if (
        state["backend"] not in
        ["gcs", "s3", "azurerm", "remote"]
        or state["locked"] is not True
    ):
        return reject("STATE_UNSAFE")


    # -------------------------
    # 4. Provider pinning
    # -------------------------

    if data["providerVersion"] not in [
        "6.2.1",
        "= 6.2.1",
        "~> 6.0"
    ]:
        return reject("UNPINNED_PROVIDER")


    # -------------------------
    # 5. Labels
    # -------------------------

    for key, value in REQUIRED_LABELS.items():

        if resource["labels"].get(key) != value:
            return reject("MISSING_LABELS")


    # -------------------------
    # 6. Secret
    # -------------------------

    secret = resource["secret"]

    if secret is not None:

        if (
            not secret.startswith("secret://")
            or len(secret) <= len("secret://")
        ):
            return reject("PLAINTEXT_SECRET")


    # -------------------------
    # 7. Delete approval
    # -------------------------

    dangerous = [
        "storage_bucket",
        "sql_database",
        "persistent_disk"
    ]

    if (
        resource["action"] == "delete"
        and resource["type"] in dangerous
        and data["destroyApproved"] is not True
    ):
        return reject("DELETE_NOT_APPROVED")


    # -------------------------
    # 8. Force destroy
    # -------------------------

    if (
        data["environment"] == WORKSPACE
        and resource["type"] == "storage_bucket"
        and resource["forceDestroy"] is True
    ):
        return reject("FORCE_DESTROY")


    return {
        "decision": "approve",
        "reason": "APPROVE"
    }