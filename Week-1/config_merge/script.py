# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "requests",
#     "rich",
# ]
# ///

import json

with open("base.json") as f:
    base = json.load(f)

with open("branch_a.json") as f:
    a = json.load(f)

with open("branch_b.json") as f:
    b = json.load(f)

conflicts = 0

for key in base:
    base_val = base[key]["value"]
    a_val = a[key]["value"]
    b_val = b[key]["value"]

    if a_val != base_val and b_val != base_val and a_val != b_val:
        conflicts += 1

print(conflicts)
