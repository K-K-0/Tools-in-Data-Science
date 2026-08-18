import re

from fastapi import FastAPI

app = FastAPI()

SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")


@app.post("/release-gate")
def release_gate(data: dict):

    violations = []

    ##################################
    # 1. Least privilege permissions
    ##################################

    expected_permissions = {
        "contents": "read",
        "packages": "write",
        "id-token": "none"
    }

    permissions = data["workflow"]["permissions"]

    if permissions != expected_permissions:
        violations.append("EXCESS_PERMISSION")

    ##################################
    # 2. PR trigger
    ##################################

    if data["event"] == "pull_request":
        if data["workflow"]["trigger"] != "pull_request":
            violations.append("UNSAFE_PR_TRIGGER")

    ##################################
    # 3. Tests
    ##################################

    workflow = data["workflow"]

    if (
        workflow["testsPassed"] is False
        or workflow["matrixComplete"] is False
        or workflow["failFast"] is True
    ):
        violations.append("TESTS_INCOMPLETE")

    ##################################
    # 4. Action pinning
    ##################################

    for action in workflow["actions"]:

        if action["owner"] != "actions":

            if not SHA_PATTERN.fullmatch(action["ref"]):
                violations.append("MUTABLE_ACTION")
                break

    ##################################
    # 5. Image rules
    ##################################

    image = data["image"]

    if not image["multiStage"]:
        violations.append("SINGLE_STAGE_IMAGE")

    if image["runsAsRoot"]:
        violations.append("ROOT_RUNTIME")

    if image["secretMode"] not in ("none", "buildkit"):
        violations.append("SECRET_IN_LAYER")

    if image["criticalVulnerabilities"] > 0:
        violations.append("CRITICAL_CVE")

    if not image["digestPinned"]:
        violations.append("UNPINNED_IMAGE")

    ##################################
    # 6. Production rules
    ##################################

    if data["target"] == "production":

        if (
            data["event"] != "push"
            or data["ref"] != "refs/heads/main"
        ):
            violations.append("INVALID_PRODUCTION_REF")

        if workflow.get("environmentApproval") is not True:
            violations.append("APPROVAL_REQUIRED")

    ##################################

    return {
        "decision": "promote" if not violations else "block",
        "violations": violations
    }