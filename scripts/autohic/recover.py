"""Recover one AutoHiC correction pass from an existing matched .hic/.assembly pair.

Run from the repository with the AutoHiC Python: python -m scripts.autohic.recover.
This does not modify Gene Agent checkpoints or replace its full AutoHiC method.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from .device import DevicePolicy
from .validation import assembly_info, available_disk, sha256, validate_images


def write_json(path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w") as handle:
        json.dump(value, handle, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--autohic-root", type=Path, required=True)
    result.add_argument("--hic", type=Path, required=True)
    result.add_argument("--assembly", type=Path, required=True)
    result.add_argument("--output", type=Path, required=True)
    result.add_argument(
        "--pair-provenance",
        required=True,
        help="Evidence that map and assembly belong to the same producer round",
    )
    result.add_argument(
        "--images", type=Path, help="Reuse a complete official png/info.txt directory"
    )
    result.add_argument(
        "--expected-inference-span-bp",
        type=int,
        required=True,
        help="Explicitly confirm official first-placement-group filter extent",
    )
    result.add_argument("--device", choices=["auto", "cpu", "cuda"], default="auto")
    result.add_argument("--threads", type=int, default=8)
    result.add_argument(
        "--min-free-gib",
        type=float,
        default=350,
        help="Operational storage reserve, not a biological parameter",
    )
    result.add_argument("--score", type=float, default=0.9)
    result.add_argument("--iou-score", type=float, default=0.8)
    result.add_argument("--min-length", type=int, default=15000)
    result.add_argument("--max-length", type=int, default=20000000)
    result.add_argument("--no-translocation", action="store_true")
    result.add_argument("--no-inversion", action="store_true")
    result.add_argument("--debris", action="store_true")
    result.add_argument("--post-review-script", type=Path)
    result.add_argument("--reference", type=Path)
    result.add_argument("--contacts", type=Path)
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    if not args.pair_provenance.strip() or args.expected_inference_span_bp <= 0:
        raise ValueError(
            "Pair provenance and a positive expected inference span are required"
        )
    if args.threads < 1 or args.min_free_gib < 0:
        raise ValueError("threads must be positive; disk reserve must be nonnegative")
    if not (0 <= args.score <= 1 and 0 <= args.iou_score <= 1):
        raise ValueError("score and iou-score must be within [0,1]")
    if not 0 < args.min_length <= args.max_length:
        raise ValueError("Invalid error length interval")
    if bool(args.post_review_script) != bool(args.reference and args.contacts):
        raise ValueError(
            "Post-review needs --post-review-script, --reference and --contacts"
        )
    if (args.reference is None) != (args.contacts is None):
        raise ValueError("reference and contacts must be provided together")
    args.output = args.output.expanduser().resolve()
    args.output.mkdir(parents=True, exist_ok=True)
    with (args.output / ".lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if any(path.name != ".lock" for path in args.output.iterdir()):
            raise RuntimeError(
                "Output is not empty; preserve it and choose a fresh directory"
            )
        phase = "preflight"

        def status(state, **extra):
            write_json(
                args.output / "status.json",
                dict(
                    state=state,
                    phase=phase,
                    pid=os.getpid(),
                    updated_at=datetime.now(timezone.utc).isoformat(),
                    **extra,
                ),
            )

        try:
            status("running")
            os.environ.setdefault("MPLBACKEND", "Agg")
            os.environ.setdefault("OMP_NUM_THREADS", "4")
            os.environ.setdefault("MKL_NUM_THREADS", "4")
            available_disk(args.output, args.min_free_gib)
            args.autohic_root = args.autohic_root.expanduser().resolve(strict=True)
            for name in [
                "hic",
                "assembly",
                "reference",
                "contacts",
                "post_review_script",
                "images",
            ]:
                value = getattr(args, name)
                if value is not None:
                    setattr(args, name, value.expanduser().resolve(strict=True))
            sys.path.insert(0, str(args.autohic_root))
            import hicstraw
            from src.assembly.adjust_all_error import adjust_all_error
            from src.common import error_pd
            from src.common.mul_gen_png import mul_process
            from src.utils import get_cfg

            info = assembly_info(args.assembly)
            hic = hicstraw.HiCFile(str(args.hic))
            hic_length = next(
                c.length for c in hic.getChromosomes() if c.name == "assembly"
            )
            resolutions = hic.getResolutions()
            span = get_cfg.get_hic_real_len(str(args.hic), str(args.assembly))
            if span != args.expected_inference_span_bp:
                raise ValueError(
                    f"Official inference span {span} differs from the expected value"
                )
            matrix = hic.getMatrixZoomData(
                "assembly", "assembly", "observed", "NONE", "BP", resolutions[0]
            )
            if (
                matrix.getRecordsAsMatrix(
                    0,
                    min(hic_length, resolutions[0] * 100),
                    0,
                    min(hic_length, resolutions[0] * 100),
                ).sum()
                <= 0
            ):
                raise ValueError("Contact map probe has no data")
            files = {}
            for name in ["hic", "assembly", "reference", "contacts"]:
                path = getattr(args, name)
                if path is not None:
                    files[name] = dict(
                        path=str(path),
                        bytes=path.stat().st_size,
                        mtime_ns=path.stat().st_mtime_ns,
                    )
                    if name != "contacts":
                        files[name]["sha256"] = sha256(path)
            write_json(
                args.output / "input_manifest.json",
                dict(
                    files=files,
                    assembly=info,
                    inference_span_bp=span,
                    pair_provenance=args.pair_provenance,
                    method=(
                        "single official AutoHiC correction pass; "
                        "optional official post-review"
                    ),
                    gene_agent_attempt_imported=False,
                    limits=(
                        "No epoch selection, iterative correction, "
                        "chromosome assignment or HTML report"
                    ),
                ),
            )
            phase = "rendering"
            status("running")
            images = args.images
            if images is None:
                mul_process(str(args.hic), "png", str(args.output), "dia", args.threads)
                images = args.output / "png"
            count = validate_images(images, hic_length, resolutions)
            policy = DevicePolicy(
                args.device, args.output, "error_detection", total=count
            )
            policy.install(error_pd)
            device = policy.select()
            phase = "error_detection"
            status("running", device=device, images=count)
            no_error = error_pd.infer_error(
                str(args.autohic_root / "src/models/cfgs/error_model.py"),
                str(args.autohic_root / "src/models/cfgs/error_model.pth"),
                str(images),
                str(args.output),
                device=device,
                score=args.score,
                error_min_len=args.min_length,
                error_max_len=args.max_length,
                iou_score=args.iou_score,
                chr_len=span,
            )
            if policy.completed != count:
                raise RuntimeError(
                    "Official inference did not process the complete image set"
                )
            if no_error:
                get_cfg.write_no_error_json(str(args.output / "error_summary.json"))
            if args.images:
                (args.output / "png").symlink_to(images, target_is_directory=True)
            phase = "assembly_adjustment"
            status("running")
            adjusted = args.output / "adjusted.assembly"
            adjust_all_error(
                str(args.hic),
                str(args.assembly),
                str(args.output),
                str(adjusted),
                tran_flag=not args.no_translocation,
                inv_flag=not args.no_inversion,
                deb_flag=args.debris,
                black_list=None,
            )
            if assembly_info(adjusted)["total_bp"] != info["total_bp"]:
                raise ValueError("Correction changed assembly total length")
            json.loads((args.output / "error_summary.json").read_text())
            if args.post_review_script:
                phase = "post_review"
                available_disk(args.output, args.min_free_gib)
                post = args.output / "post_review"
                post.mkdir()
                command = [
                    "bash",
                    "-e",
                    str(args.post_review_script),
                    "-r",
                    str(adjusted),
                    str(args.reference),
                    str(args.contacts),
                ]
                with (args.output / "post_review.log").open("wb", buffering=0) as log:
                    child = subprocess.Popen(
                        command, cwd=post, stdout=log, stderr=subprocess.STDOUT
                    )
                    status("running", child_pid=child.pid, command=command)
                    code = child.wait()
                    if code:
                        raise RuntimeError(f"3D-DNA post-review exited {code}")
                text = (args.output / "post_review.log").read_text(errors="replace")
                for failure in [
                    "No space left on device",
                    "Assembly file does not match",
                    "does not contain any reads",
                    "cannot be opened",
                ]:
                    if failure in text:
                        raise RuntimeError("Internal post-review failure: " + failure)
                prefix = args.reference.stem
                for suffix in [
                    "FINAL.fasta",
                    "FINAL.assembly",
                    "final.hic",
                    "final.assembly",
                ]:
                    path = post / (prefix + "." + suffix)
                    if not path.is_file() or not path.stat().st_size:
                        raise RuntimeError("Missing post-review product: " + str(path))
                final_layout = post / (prefix + ".final.assembly")
                if assembly_info(final_layout)["total_bp"] != info["total_bp"]:
                    raise ValueError("Post-review changed assembly total length")
                # FINAL.assembly reuses one hic_gap id at each scaffold join;
                # its gap placements are not unique like the ungapped layout.
                final_map = hicstraw.HiCFile(str(post / (prefix + ".final.hic")))
                if not final_map.getResolutions():
                    raise ValueError("Final contact map has no resolutions")
                bases = 0
                with (post / (prefix + ".FINAL.fasta")).open("rb") as handle:
                    for line in handle:
                        if not line.startswith(b">"):
                            bases += len(
                                line.strip().replace(b"N", b"").replace(b"n", b"")
                            )
                reference_bases = 0
                with args.reference.open("rb") as handle:
                    for line in handle:
                        if not line.startswith(b">"):
                            reference_bases += len(
                                line.strip().replace(b"N", b"").replace(b"n", b"")
                            )
                if bases != reference_bases:
                    raise ValueError("Final FASTA lost or duplicated non-gap bases")
            phase = "complete"
            status(
                "completed",
                adjusted_assembly=str(adjusted),
                adjusted_sha256=sha256(adjusted),
                note="External recovery, not a Gene Agent method success",
            )
        except BaseException as exc:
            status("failed", error=str(exc))
            raise


if __name__ == "__main__":
    main()
