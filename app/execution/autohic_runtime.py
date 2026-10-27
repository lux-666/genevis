"""Python 3.9-compatible AutoHiC launcher and GPU-first MMDetection policy."""

from __future__ import annotations

import argparse
import gc
import importlib
import json
import os
import runpy
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def cuda_error(exc: BaseException) -> bool:
    """Recognize device failures, not arbitrary model/input RuntimeErrors."""
    message = str(exc).lower()
    return any(
        token in message
        for token in (
            "cuda out of memory",
            "cuda error:",
            "cuda driver",
            "cuda initialization",
            "cudnn_status_",
            "cublas_status_",
            "no kernel image",
            "not compiled with gpu support",
            "not compiled with cuda",
            "could not run 'mmcv::nms' with arguments from the 'cuda' backend",
            "nms_impl: implementation for device cuda",
        )
    )


class DevicePolicy:
    def __init__(
        self,
        policy: str,
        output: str | Path,
        phase: str,
        total: int | None = None,
        backend: Any = None,
        api: Any = None,
    ) -> None:
        if policy not in {"auto", "cpu", "cuda"}:
            raise ValueError("device must be auto, cpu or cuda")
        if backend is None:
            backend = importlib.import_module("torch")
        if api is None:
            api = importlib.import_module("mmdet.apis")
        self.policy, self.output, self.phase = policy, Path(output), phase
        self.backend, self.api = backend, api
        self.total, self.completed, self.started = total, 0, time.monotonic()
        self.force_cpu = policy == "cpu"
        self.output.mkdir(parents=True, exist_ok=True)

    def event(self, message: str, **extra: Any) -> None:
        value = dict(
            at=datetime.now(timezone.utc).isoformat(),
            phase=self.phase,
            message=message,
            **extra,
        )
        with (self.output / "device_events.jsonl").open("a") as handle:
            handle.write(json.dumps(value) + "\n")
        print("DEVICE " + json.dumps(value), flush=True)

    def _release(self) -> None:
        gc.collect()
        self.backend.cuda.empty_cache()

    def _probe_cuda(self) -> None:
        nms = importlib.import_module("mmcv.ops").nms

        boxes = self.backend.tensor([[0.0, 0.0, 10.0, 10.0]], device="cuda:0")
        scores = self.backend.tensor([0.9], device="cuda:0")
        nms(boxes, scores, 0.5)
        self.backend.cuda.synchronize()

    def select(self) -> str:
        if self.force_cpu:
            self.event("CPU selected", device="cpu")
            return "cpu"
        if not self.backend.cuda.is_available():
            if self.policy == "cuda":
                raise RuntimeError("CUDA required but unavailable")
            self.force_cpu = True
            self.event("CUDA unavailable; CPU fallback", device="cpu")
            return "cpu"
        fallback = None
        try:
            self._probe_cuda()
        except (RuntimeError, NotImplementedError) as exc:
            if self.policy != "auto" or not cuda_error(exc):
                raise
            fallback = str(exc)
        if fallback is not None:
            self.force_cpu = True
            self._release()
            self.event(
                "CUDA operator probe failed; CPU fallback",
                reason=fallback,
                device="cpu",
            )
            return "cpu"
        self.event(
            "CUDA operator probe passed",
            device="cuda:0",
            gpu=self.backend.cuda.get_device_name(0),
        )
        return "cuda:0"

    def init_detector(self, *args: Any, **kwargs: Any) -> Any:
        if self.force_cpu:
            kwargs["device"] = "cpu"
        fallback = None
        try:
            return self.api.init_detector(*args, **kwargs)
        except (RuntimeError, NotImplementedError) as exc:
            if (
                self.policy != "auto"
                or not str(kwargs.get("device", "")).startswith("cuda")
                or not cuda_error(exc)
            ):
                raise
            fallback = str(exc)
        # Leave the except block before releasing CUDA allocations: its traceback
        # can otherwise keep the failed model initialization alive.
        self.force_cpu = True
        self._release()
        self.event(
            "GPU model initialization failed; CPU fallback",
            reason=fallback,
            device="cpu",
        )
        kwargs["device"] = "cpu"
        return self.api.init_detector(*args, **kwargs)

    def inference_detector(
        self, model: Any, image: Any, *args: Any, **kwargs: Any
    ) -> Any:
        fallback = None
        try:
            result = self.api.inference_detector(model, image, *args, **kwargs)
        except (RuntimeError, NotImplementedError) as exc:
            if (
                self.policy != "auto"
                or next(model.parameters()).device.type != "cuda"
                or not cuda_error(exc)
            ):
                raise
            fallback = str(exc)
        if fallback is not None:
            self.force_cpu = True
            model.to("cpu")
            self._release()
            self.event(
                "GPU inference failed; retry current image on CPU",
                device="cpu",
                reason=fallback,
            )
            result = self.api.inference_detector(model, image, *args, **kwargs)
        self.completed += 1
        if (
            self.completed == 1
            or self.completed % 10 == 0
            or self.completed == self.total
        ):
            value = dict(
                phase=self.phase,
                completed_images=self.completed,
                total_images=self.total,
                device=str(next(model.parameters()).device),
                elapsed_seconds=time.monotonic() - self.started,
                updated_at=datetime.now(timezone.utc).isoformat(),
            )
            temporary = self.output / "inference_progress.json.tmp"
            temporary.write_text(json.dumps(value, indent=2) + "\n")
            temporary.replace(self.output / "inference_progress.json")
            print("INFERENCE " + json.dumps(value), flush=True)
        return result

    def install(self, module: Any) -> None:
        """Adapt one official module in this process only; no source patching."""
        module.init_detector = self.init_detector
        module.inference_detector = self.inference_detector


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("entry", type=Path)
    parser.add_argument("-c", "--config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    entry = args.entry.expanduser().resolve(strict=True)
    config = args.config.expanduser().resolve(strict=True)
    output = args.output.expanduser().resolve()
    sys.path.insert(0, str(entry.parent))
    os.chdir(entry.parent)
    policy = DevicePolicy("auto", output, "autohic")
    policy.select()
    # Install before autohic.py imports the official functions by name.
    for name in ("src.common.error_pd", "src.utils.get_chr_data"):
        policy.install(importlib.import_module(name))
    sys.argv = [str(entry), "-c", str(config)]
    runpy.run_path(str(entry), run_name="__main__")


if __name__ == "__main__":
    main()
