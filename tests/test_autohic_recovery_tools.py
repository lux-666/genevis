"""Standalone recovery checks without importing torch or running installed tools."""

import fcntl
import json
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.autohic.device import DevicePolicy
from scripts.autohic.recover import main
from scripts.autohic.validation import assembly_info, available_disk, validate_images


class Model:
    def __init__(self, device):
        self.device = device

    def parameters(self):
        yield SimpleNamespace(device=SimpleNamespace(type=self.device))

    def to(self, device):
        self.device = device
        return self


class API:
    def __init__(self, init_error=None, inference_error=None):
        self.init_error, self.inference_error = init_error, inference_error
        self.initialized, self.inferred = [], []

    def init_detector(self, *args, device):
        self.initialized.append(device)
        if self.init_error is not None and device.startswith("cuda"):
            raise self.init_error
        return Model("cuda" if device.startswith("cuda") else "cpu")

    def inference_detector(self, model, image):
        self.inferred.append((model.device, image))
        if self.inference_error is not None and model.device == "cuda":
            raise self.inference_error
        return image


def policy(
    tmp_path, monkeypatch, mode="auto", available=True, api=None, probe_error=None
):
    cuda = SimpleNamespace(
        is_available=lambda: available,
        empty_cache=lambda: None,
        get_device_name=lambda _: "test GPU",
    )
    result = DevicePolicy(
        mode,
        tmp_path,
        "test",
        total=2,
        backend=SimpleNamespace(cuda=cuda),
        api=api or API(),
    )

    def probe():
        if probe_error is not None:
            raise probe_error

    monkeypatch.setattr(result, "_probe_cuda", probe)
    return result


@pytest.mark.parametrize(
    "mode,available,expected",
    [("auto", True, "cuda:0"), ("auto", False, "cpu"), ("cpu", True, "cpu")],
)
def test_device_selection(tmp_path, monkeypatch, mode, available, expected):
    chosen = policy(tmp_path, monkeypatch, mode, available)
    assert chosen.select() == expected
    assert (tmp_path / "device_events.jsonl").is_file()


def test_forced_cuda_does_not_fallback(tmp_path, monkeypatch):
    chosen = policy(tmp_path, monkeypatch, mode="cuda", available=False)
    with pytest.raises(RuntimeError, match="required but unavailable"):
        chosen.select()
    chosen = policy(
        tmp_path,
        monkeypatch,
        mode="cuda",
        probe_error=RuntimeError("CUDA error: operator failed"),
    )
    with pytest.raises(RuntimeError, match="operator failed"):
        chosen.select()


@pytest.mark.parametrize(
    "error",
    [
        RuntimeError("CUDA out of memory"),
        RuntimeError("nms_impl: implementation for device cuda:0 not found."),
    ],
)
def test_probe_failure_selects_cpu(tmp_path, monkeypatch, error):
    assert policy(tmp_path, monkeypatch, probe_error=error).select() == "cpu"


def test_unrelated_probe_error_propagates(tmp_path, monkeypatch):
    chosen = policy(tmp_path, monkeypatch, probe_error=RuntimeError("invalid shape"))
    with pytest.raises(RuntimeError, match="invalid shape"):
        chosen.select()


def test_initialization_oom_retries_cpu(tmp_path, monkeypatch):
    api = API(init_error=RuntimeError("CUDA out of memory"))
    chosen = policy(tmp_path, monkeypatch, api=api)
    chosen.init_detector("config", "weights", device=chosen.select())
    chosen.init_detector("config", "weights", device="cuda:0")
    assert api.initialized == ["cuda:0", "cpu", "cpu"]


def test_inference_oom_retries_same_image_and_stays_on_cpu(tmp_path, monkeypatch):
    api = API(inference_error=RuntimeError("CUDA out of memory"))
    chosen = policy(tmp_path, monkeypatch, api=api)
    model = chosen.init_detector("config", "weights", device=chosen.select())
    assert chosen.inference_detector(model, "first") == "first"
    assert chosen.inference_detector(model, "second") == "second"
    assert api.inferred == [("cuda", "first"), ("cpu", "first"), ("cpu", "second")]
    progress = json.loads((tmp_path / "inference_progress.json").read_text())
    assert progress["completed_images"] == 2
    assert progress["total_images"] == 2
    assert chosen.force_cpu
    assert "retry current image" in (tmp_path / "device_events.jsonl").read_text()


@pytest.mark.parametrize(
    "mode,error",
    [
        ("auto", RuntimeError("invalid image")),
        ("cuda", RuntimeError("CUDA out of memory")),
    ],
)
def test_inference_errors_are_not_swallowed(tmp_path, monkeypatch, mode, error):
    api = API(inference_error=error)
    chosen = policy(tmp_path, monkeypatch, mode=mode, api=api)
    model = chosen.init_detector("config", "weights", device=chosen.select())
    with pytest.raises(RuntimeError, match=str(error)):
        chosen.inference_detector(model, "first")
    assert chosen.completed == 0
    assert len(api.inferred) == 1


def test_cpu_retry_failure_propagates(tmp_path, monkeypatch):
    api = API()

    def infer(model, image):
        raise RuntimeError(
            "CUDA out of memory" if model.device == "cuda" else "CPU failed"
        )

    monkeypatch.setattr(api, "inference_detector", infer)
    chosen = policy(tmp_path, monkeypatch, api=api)
    model = chosen.init_detector("config", "weights", device=chosen.select())
    with pytest.raises(RuntimeError, match="CPU failed"):
        chosen.inference_detector(model, "first")
    assert chosen.completed == 0


@pytest.mark.parametrize(
    "content",
    [
        ">a 1 10\n>b 2 20\n1\n",  # missing placement
        ">a 1 10\n>b 1 20\n1\n",  # duplicate header
        ">a 1 10\n1 1\n",  # duplicate placement
        ">a 1 10\n2\n",  # unknown placement
        ">a 1 0\n1\n",  # zero length
    ],
)
def test_reject_invalid_assembly(tmp_path, content):
    path = tmp_path / "test.assembly"
    path.write_text(content)
    with pytest.raises(ValueError):
        assembly_info(path)


def test_assembly_span_tracks_first_group(tmp_path):
    path = tmp_path / "test.assembly"
    path.write_text(">a 1 10\n>b 2 20\n>c 3 30\n-2 1\n3\n")
    assert assembly_info(path) == dict(
        total_bp=60, fragments=3, groups=2, main_group_bp=30
    )


def image_index(tmp_path, count=2, windows=None):
    rows = []
    if windows is None:
        windows = [(500, 0, 300000)] * count
    for i, (resolution, start, end) in enumerate(windows):
        path = tmp_path / (str(i) + ".jpg")
        path.write_bytes(b"test image")
        rows.append(
            {
                str(path): dict(
                    resolution=resolution,
                    chr_A="assembly",
                    chr_A_start=start,
                    chr_A_end=end,
                    chr_B="assembly",
                    chr_B_start=start,
                    chr_B_end=end,
                )
            }
        )
    (tmp_path / "info.txt").write_text("".join(json.dumps(row) + "\n" for row in rows))
    return rows


def test_complete_image_set_includes_overlapping_last_window(tmp_path):
    image_index(tmp_path)
    assert validate_images(tmp_path, 300000, [250, 500]) == 2


@pytest.mark.parametrize(
    "total_bp,resolutions,windows",
    [
        (10, [500], [(500, 0, 10)]),
        (400000, [500], [(500, 0, 350000), (500, 50000, 400000)]),
        (
            1000000,
            [250, 500, 1000],
            [
                (500, 0, 350000),
                (500, 200000, 550000),
                (500, 400000, 750000),
                (500, 600000, 950000),
                (500, 650000, 1000000),
                (1000, 0, 700000),
                (1000, 300000, 1000000),
                (1000, 300000, 1000000),
            ],
        ),
    ],
)
def test_complete_official_windows_are_accepted(
    tmp_path, total_bp, resolutions, windows
):
    rows = image_index(tmp_path, windows=windows)
    # Rendering callbacks can finish in any order.
    (tmp_path / "info.txt").write_text(
        "".join(json.dumps(row) + "\n" for row in reversed(rows))
    )
    assert validate_images(tmp_path, total_bp, resolutions) == len(windows)


def test_same_count_with_missing_windows_warns_and_continues(tmp_path):
    image_index(tmp_path, windows=[(500, 0, 350000)] * 5)
    with pytest.warns(RuntimeWarning, match="continuing with 5 indexed images"):
        assert validate_images(tmp_path, 1000000, [500]) == 5


@pytest.mark.parametrize(
    "field,value",
    [
        ("chr_A", "chr1"),
        ("chr_B", "chr1"),
        ("chr_A_start", 1),
        ("chr_A_end", 299999),
        ("chr_B_start", 1),
        ("chr_B_end", 299999),
        ("chr_A_start", None),
    ],
)
def test_incorrect_window_metadata_warns_and_continues(tmp_path, field, value):
    rows = image_index(tmp_path)
    meta = next(iter(rows[0].values()))
    if value is None:
        del meta[field]
    else:
        meta[field] = value
    (tmp_path / "info.txt").write_text("".join(json.dumps(row) + "\n" for row in rows))
    with pytest.warns(RuntimeWarning, match="Image coverage differs"):
        assert validate_images(tmp_path, 300000, [500]) == 2


def test_missing_rendered_job_warns_and_continues(tmp_path):
    image_index(tmp_path, count=1)
    with pytest.warns(RuntimeWarning, match="continuing with 1 indexed images"):
        assert validate_images(tmp_path, 300000, [500]) == 1


def test_missing_image_file_is_rejected(tmp_path):
    image_index(tmp_path)
    (tmp_path / "1.jpg").unlink()
    with pytest.raises(ValueError, match="nonempty files"):
        validate_images(tmp_path, 300000, [500])


def test_duplicate_image_path_is_rejected(tmp_path):
    rows = image_index(tmp_path)
    (tmp_path / "info.txt").write_text((json.dumps(rows[0]) + "\n") * 2)
    with pytest.raises(ValueError, match="duplicate image paths"):
        validate_images(tmp_path, 300000, [500])


def test_disk_reserve_enforced(tmp_path, monkeypatch):
    monkeypatch.setattr("shutil.disk_usage", lambda _: SimpleNamespace(free=2**30))
    assert available_disk(tmp_path, 1) == 2**30
    with pytest.raises(RuntimeError, match="Need 2 GiB"):
        available_disk(tmp_path, 2)


def recovery_args(tmp_path):
    return [
        "--autohic-root",
        str(tmp_path),
        "--hic",
        str(tmp_path / "map.hic"),
        "--assembly",
        str(tmp_path / "input.assembly"),
        "--output",
        str(tmp_path),
        "--expected-inference-span-bp",
        "10",
        "--pair-provenance",
        "verified producer round",
    ]


def test_recovery_refuses_existing_output(tmp_path):
    (tmp_path / "retained.txt").write_text("keep")
    with pytest.raises(RuntimeError, match="Output is not empty"):
        main(recovery_args(tmp_path))
    assert (tmp_path / "retained.txt").read_text() == "keep"


def test_recovery_refuses_concurrent_writer(tmp_path):
    with (tmp_path / ".lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(BlockingIOError):
            main(recovery_args(tmp_path))


def test_recovery_records_preflight_failure(tmp_path, monkeypatch):
    def no_disk(*_):
        raise RuntimeError("disk reserve unavailable")

    monkeypatch.setattr("scripts.autohic.recover.available_disk", no_disk)
    with pytest.raises(RuntimeError, match="disk reserve unavailable"):
        main(recovery_args(tmp_path))
    status = json.loads((tmp_path / "status.json").read_text())
    assert status["state"] == "failed"
    assert status["phase"] == "preflight"


def fake_upstream(tmp_path, monkeypatch, child_code=0):
    """Exercise the CLI's boundary contracts with explicit official-tool fakes."""
    monkeypatch.syspath_prepend(str(tmp_path))
    (tmp_path / "input.assembly").write_text(">a 1 10\n1\n")
    (tmp_path / "map.hic").write_bytes(b"contact map")
    images = tmp_path / "images"
    images.mkdir()
    image_index(images, windows=[(500, 0, 10)])
    calls = []

    class HiC:
        def __init__(self, path):
            pass

        def getChromosomes(self):
            return [SimpleNamespace(name="assembly", length=10)]

        def getResolutions(self):
            return [500]

        def getMatrixZoomData(self, *args):
            return SimpleNamespace(
                getRecordsAsMatrix=lambda *a: SimpleNamespace(sum=lambda: 1)
            )

    def infer_error(*args, **kwargs):
        calls.append(("inference", kwargs["chr_len"]))
        error_pd.inference_detector(Model("cpu"), "image")
        return True

    def adjust(_hic, assembly, _output, result, **kwargs):
        calls.append(("adjust", kwargs))
        Path(result).write_text(Path(assembly).read_text())

    def child(command, cwd, stdout, **kwargs):
        calls.append(("post_review", command))
        (cwd / "source.final.assembly").write_text(">a 1 10\n1\n")
        # Official gapped FINAL layout may place the same synthetic gap repeatedly.
        (cwd / "source.FINAL.assembly").write_text(">a 1 10\n>hic_gap_2 2 500\n1 2 2\n")
        (cwd / "source.FINAL.fasta").write_text(">a\nAAAAAAAAAA\n")
        (cwd / "source.final.hic").write_bytes(b"contact map")
        return SimpleNamespace(pid=123, wait=lambda: child_code)

    def module(name, **attrs):
        result = types.ModuleType(name)
        result.__dict__.update(attrs)
        monkeypatch.setitem(sys.modules, name, result)
        return result

    module("hicstraw", HiCFile=HiC)
    module("src")
    module("src.assembly")
    module("src.assembly.adjust_all_error", adjust_all_error=adjust)
    error_pd = module("src.common.error_pd", infer_error=infer_error)
    module("src.common", error_pd=error_pd)
    module(
        "src.common.mul_gen_png",
        mul_process=lambda *a: pytest.fail("Should reuse images"),
    )
    module(
        "src.utils",
        get_cfg=SimpleNamespace(
            get_hic_real_len=lambda *a: 10,
            write_no_error_json=lambda p: Path(p).write_text("{}\n"),
        ),
    )

    class CPUDevicePolicy(DevicePolicy):
        def __init__(self, policy, output, phase, total):
            super().__init__(
                policy, output, phase, total, api=API(), backend=SimpleNamespace()
            )

        def select(self):
            self.force_cpu = True
            return "cpu"

    monkeypatch.setattr("scripts.autohic.recover.DevicePolicy", CPUDevicePolicy)
    monkeypatch.setattr("scripts.autohic.recover.subprocess.Popen", child)
    output = tmp_path / "output"
    args = recovery_args(tmp_path)
    args[args.index("--output") + 1] = str(output)
    args.extend(["--images", str(images), "--min-free-gib", "0"])
    return args, output, calls


def test_recovery_single_pass_reuses_images_and_records_provenance(
    tmp_path, monkeypatch
):
    args, output, calls = fake_upstream(tmp_path, monkeypatch)
    main(args)
    assert json.loads((output / "status.json").read_text())["state"] == "completed"
    manifest = json.loads((output / "input_manifest.json").read_text())
    assert manifest["gene_agent_attempt_imported"] is False
    assert manifest["files"]["hic"]["sha256"]
    assert manifest["pair_provenance"] == "verified producer round"
    assert calls[0] == ("inference", 10)
    assert (output / "png").resolve() == tmp_path / "images"


def test_recovery_completes_after_image_coverage_warning(tmp_path, monkeypatch):
    args, output, calls = fake_upstream(tmp_path, monkeypatch)
    image_index(tmp_path / "images", windows=[(500, 0, 9)])
    with pytest.warns(RuntimeWarning, match="Image coverage differs"):
        main(args)
    assert json.loads((output / "status.json").read_text())["state"] == "completed"
    assert [call[0] for call in calls] == ["inference", "adjust"]


def test_recovery_rejects_unexpected_official_span(tmp_path, monkeypatch):
    args, output, calls = fake_upstream(tmp_path, monkeypatch)
    args[args.index("--expected-inference-span-bp") + 1] = "11"
    with pytest.raises(ValueError, match="differs from the expected"):
        main(args)
    assert not calls
    assert json.loads((output / "status.json").read_text())["state"] == "failed"


@pytest.mark.parametrize("child_code", [0, 42])
def test_post_review_exit_code_and_product_validation(
    tmp_path, monkeypatch, child_code
):
    args, output, calls = fake_upstream(tmp_path, monkeypatch, child_code)
    reference = tmp_path / "source.fasta"
    reference.write_text(">a\nAAAAAAAAAA\n")
    contacts = tmp_path / "contacts.txt"
    contacts.write_text("read pair\n")
    script = tmp_path / "post-review.sh"
    script.write_text("#!/bin/bash\n")
    args.extend(
        [
            "--post-review-script",
            str(script),
            "--reference",
            str(reference),
            "--contacts",
            str(contacts),
        ]
    )
    if child_code:
        with pytest.raises(RuntimeError, match="exited 42"):
            main(args)
    else:
        main(args)
    state = json.loads((output / "status.json").read_text())["state"]
    assert state == ("failed" if child_code else "completed")
    assert calls[-1][0] == "post_review"
