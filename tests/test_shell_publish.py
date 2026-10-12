"""Shell publication treats paths as data in every branch."""

import shlex
import subprocess

import pytest

from app.storage.publish import shell_publish_file, shell_publish_variable


@pytest.mark.parametrize("variable", [False, True])
@pytest.mark.parametrize(
    "name", ["space name", "single'quote", "`printf INJECTED`", "$(printf INJECTED)"]
)
@pytest.mark.parametrize("existing", [None, "new", "old"])
def test_publish_paths_are_literal(tmp_path, variable, name, existing):
    directory = tmp_path / name
    directory.mkdir()
    source, destination = directory / "source", directory / "destination"
    source.write_text("new")
    if existing is not None:
        destination.write_text(existing)
    command = (
        f"source={shlex.quote(str(source))}; "
        + shell_publish_variable("source", destination)
        if variable
        else shell_publish_file(source, destination)
    )
    result = subprocess.run(["sh", "-c", command], capture_output=True, text=True)
    if existing == "old":
        assert result.returncode != 0
        assert (
            result.stderr
            == f"published artifact conflicts with existing snapshot: {destination}\n"
        )
        assert destination.read_text() == "old"
        assert source.read_text() == "new"
    else:
        assert result.returncode == 0, result.stderr
        assert destination.read_text() == "new"
        assert not source.exists()


def test_variable_name_is_not_shell_code():
    with pytest.raises(ValueError):
        shell_publish_variable("x; printf INJECTED", "destination")
