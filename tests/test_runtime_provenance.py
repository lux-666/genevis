"""Code identity is captured once in the frozen dependency evidence."""

from importlib.metadata import PackageNotFoundError
from subprocess import CompletedProcess
from unittest.mock import patch

import pytest

from app.execution.dependency_snapshot import (
    DependencySnapshot,
    capture_dependency_snapshot,
    capture_runtime_provenance,
)
from app.version import __version__
from tests.busco_fixture import ensure_test_host_envs


def test_source_and_installed_versions_are_distinct_and_dirty_includes_untracked():
    with (
        patch(
            "app.execution.dependency_snapshot.installed_version", return_value="0.0.7"
        ),
        patch(
            "app.execution.dependency_snapshot.subprocess.run",
            side_effect=[
                CompletedProcess([], 0, "a" * 40 + "\n", ""),
                CompletedProcess([], 0, "?? app/new_module.py\n", ""),
            ],
        ) as git,
    ):
        identity = capture_runtime_provenance()
    assert identity.source_version == __version__
    assert identity.installed_version == "0.0.7"
    assert identity.git_revision == "a" * 40
    assert identity.git_dirty is True
    assert "--untracked-files=normal" in git.call_args.args[0]
    assert "new_module" not in identity.model_dump_json()


def test_package_without_checkout_does_not_inherit_parent_repository(tmp_path):
    with (
        patch(
            "app.execution.dependency_snapshot.__file__",
            str(tmp_path / "app" / "execution" / "dependency_snapshot.py"),
        ),
        patch(
            "app.execution.dependency_snapshot.installed_version",
            side_effect=PackageNotFoundError,
        ),
        patch("app.execution.dependency_snapshot.subprocess.run") as git,
    ):
        identity = capture_runtime_provenance()
    git.assert_not_called()
    assert identity.git_revision is None
    assert identity.git_dirty is None
    assert identity.installed_version is None
    assert identity.source_version == __version__


def test_freeze_records_runtime_once_and_reload_does_not_recompute(tmp_path):
    identity = capture_runtime_provenance()
    with patch(
        "app.execution.dependency_snapshot.capture_runtime_provenance",
        return_value=identity,
    ) as capture:
        snapshot = capture_dependency_snapshot(
            config_path=ensure_test_host_envs(tmp_path),
            stages=[],
            methods=[],
            probe_environments=False,
        )
        restored = DependencySnapshot.model_validate_json(snapshot.model_dump_json())
    capture.assert_called_once_with()
    assert restored.runtime == identity
    payload = snapshot.model_dump(mode="json")
    del payload["runtime"]
    with pytest.raises(ValueError, match="runtime"):
        DependencySnapshot.model_validate(payload)
