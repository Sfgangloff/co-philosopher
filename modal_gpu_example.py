"""Minimal Modal GPU example.

Run it with:   modal run modal_gpu_example.py
Modal spins up a T4 container, runs gpu_check(), then tears it down.
You are billed only for the seconds the function actually runs.
"""

import modal

# An image with PyTorch so we can verify CUDA works.
image = modal.Image.debian_slim().pip_install("torch")

app = modal.App("gpu-example", image=image)


@app.function(gpu="T4")  # try "L4", "A10G", "A100", "H100" instead
def gpu_check() -> str:
    import subprocess

    import torch

    smi = subprocess.run(["nvidia-smi"], capture_output=True, text=True).stdout
    info = (
        f"CUDA available: {torch.cuda.is_available()}\n"
        f"Device: {torch.cuda.get_device_name(0)}\n"
        f"---\n{smi}"
    )
    print(info)
    return info


@app.local_entrypoint()
def main():
    # Runs on Modal's GPU, result comes back to your terminal.
    print(gpu_check.remote())
