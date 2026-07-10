import base64
import requests

with open("invoice.png", "rb") as f:
    img = base64.b64encode(f.read()).decode()

response = requests.post(
    "http://127.0.0.1:8003/answer-image",
    json={
        "image_base64": img,
        "question": "What is the invoice grand total?"
    }
)

print(response.json())