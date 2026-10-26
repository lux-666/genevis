"""Probe real AutoHiC operators and both models with its own Python environment."""

from __future__ import annotations

import argparse
import gc
import importlib.metadata
import json
import sys
import time
from pathlib import Path

from .device import DevicePolicy


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--autohic-root", type=Path, required=True)
    parser.add_argument("--image", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--device", choices=["auto", "cpu", "cuda"], default="auto")
    args = parser.parse_args(argv)
    root = args.autohic_root.expanduser().resolve(strict=True)
    image = args.image.expanduser().resolve(strict=True)
    output = args.output.expanduser().resolve()
    output.mkdir(parents=True, exist_ok=False)
    sys.path.insert(0, str(root))
    import torch
    from mmcv.ops import nms

    # CPU operators must also work in a CUDA environment for fallback to be useful.
    nms(torch.tensor([[0.0, 0.0, 10.0, 10.0]]), torch.tensor([0.9]), 0.5)
    policy = DevicePolicy(args.device, output, "model_probe", total=2)
    device = policy.select()
    versions = {}
    for name in ["torch", "torchvision", "mmcv-full", "mmdet", "numpy", "timm"]:
        versions[name] = importlib.metadata.version(name)
    results = []
    for kind in ["error", "chr"]:
        started = time.monotonic()
        model = policy.init_detector(
            str(root / "src/models/cfgs" / (kind + "_model.py")),
            str(root / "src/models/cfgs" / (kind + "_model.pth")),
            device=device,
        )
        result = policy.inference_detector(model, str(image))
        actual_device = str(next(model.parameters()).device)
        if actual_device.startswith("cuda"):
            torch.cuda.synchronize()
        results.append(
            dict(
                model=kind,
                device=actual_device,
                elapsed_seconds=time.monotonic() - started,
                output_type=type(result).__name__,
            )
        )
        del result, model
        gc.collect()
        torch.cuda.empty_cache()
    value = dict(
        python=sys.executable,
        versions=versions,
        torch_cuda=torch.version.cuda,
        cuda_available=torch.cuda.is_available(),
        models=results,
    )
    (output / "environment.json").write_text(json.dumps(value, indent=2) + "\n")
    print(json.dumps(value, indent=2), flush=True)


if __name__ == "__main__":
    main()
