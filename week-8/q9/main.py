import json
import math
import numpy as np
import torch
import torch.nn as nn
import torch.optim as optim
import mlflow

# -----------------------
# Load config
# -----------------------
with open("mlflow_config_24f1002052.json", "r") as f:
    config = json.load(f)

X = torch.tensor(config["dataset"]["X"], dtype=torch.float32)
y = torch.tensor(config["dataset"]["y"], dtype=torch.float32).view(-1, 1)

N = X.shape[0]

# -----------------------
# Model
# -----------------------
model = nn.Linear(8, 1)

# Initial weights
init = config["initialization"]["initial_weights"]

model.weight.data = torch.tensor([init["weight"]], dtype=torch.float32)
model.bias.data = torch.tensor([init["bias"]], dtype=torch.float32)

# -----------------------
# Hyperparameters
# -----------------------
hp = config["hyperparameters"]

lr_base = hp["lr"]
weight_decay = hp["weight_decay"]

optimizer_name = hp["optimizer"].lower()

if optimizer_name == "sgd":
    optimizer = optim.SGD(
        model.parameters(),
        lr=lr_base,
        momentum=hp["momentum"],
        weight_decay=weight_decay,
    )

elif optimizer_name == "adamw":
    optimizer = optim.AdamW(
        model.parameters(),
        lr=lr_base,
        weight_decay=weight_decay,
        betas=(hp["beta1"], hp["beta2"]),
        eps=hp["eps"],
    )

elif optimizer_name == "rmsprop":
    optimizer = optim.RMSprop(
        model.parameters(),
        lr=lr_base,
        weight_decay=weight_decay,
        momentum=hp["momentum"],
        alpha=hp["alpha"],
        eps=hp["eps"],
    )

else:
    raise ValueError(optimizer_name)

criterion = nn.MSELoss()

batch_size = hp["batch_size"]
num_steps = hp["num_steps"]

schedule = config["lr_schedule"]

losses = []

mlflow.set_tracking_uri("file:./mlruns")

with mlflow.start_run() as run:

    for i in range(num_steps):

        # -----------------------
        # LR schedule
        # -----------------------
        if schedule["type"] == "cosine":
            lr = (
                schedule["lr_min"]
                + 0.5
                * (lr_base - schedule["lr_min"])
                * (1 + math.cos(i * math.pi / num_steps))
            )

        elif schedule["type"] == "step":
            lr = lr_base * (
                schedule["gamma"]
                ** (i // schedule["step_size"])
            )

        else:
            lr = lr_base

        for g in optimizer.param_groups:
            g["lr"] = lr

        # -----------------------
        # Cyclic batch
        # -----------------------
        idx = (i * batch_size) % N
        indices = [(idx + j) % N for j in range(batch_size)]

        xb = X[indices]
        yb = y[indices]

        optimizer.zero_grad()

        pred = model(xb)

        loss = criterion(pred, yb)

        loss.backward()

        optimizer.step()

        value = float(loss.item())
        losses.append(value)

        mlflow.log_metric("loss", value, step=i)

    print("=" * 50)
    print("Run ID:", run.info.run_id)
    print("Final loss:", losses[-1])
    print("Mean last 10:", np.mean(losses[-10:]))
    print("=" * 50)

    print({
        "final_loss": losses[-1],
        "run_id": run.info.run_id,
        "mean_last_10_loss": float(np.mean(losses[-10:])),
    })