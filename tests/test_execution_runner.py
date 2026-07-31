from app.execution.fake_runner import FakeRunner
from app.execution.local_runner import LocalRunner
from app.execution.method_runner import build_frozen_attempt
from app.execution.runner import Runner


def test_fake_runner_is_explicitly_injectable(tmp_path) -> None:
    work_dir = tmp_path / "work"
    script_path = work_dir / "run.sh"
    output_path = tmp_path / "artifacts" / "output.txt"
    work_dir.mkdir()
    script_path.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    attempt = build_frozen_attempt(
        attempt_id="s1:test:fake",
        session_id="s1",
        stage="assembly",
        method_name="assembly.candidate.flye",
        script_path=script_path,
        working_dir=work_dir,
        expected_outputs=[output_path],
        env=None,
        env_spec=None,
    )
    runner = FakeRunner()

    result = runner.run(attempt)

    assert isinstance(runner, Runner)
    assert result.success
    assert output_path.is_file()
    assert runner.executed_commands[0]["attempt_id"] == "s1:test:fake"


def test_local_runner_executes_exact_declared_absolute_script_path(tmp_path) -> None:
    work_dir = tmp_path / "work"
    declared_dir = work_dir / "declared"
    declared_dir.mkdir(parents=True)
    decoy_output = work_dir / "decoy.txt"
    expected_output = work_dir / "expected.txt"
    decoy_script = work_dir / "run.sh"
    declared_script = declared_dir / "run.sh"
    decoy_script.write_text(
        f"#!/bin/sh\necho decoy > {decoy_output}\n",
        encoding="utf-8",
    )
    declared_script.write_text(
        f"#!/bin/sh\necho expected > {expected_output}\n",
        encoding="utf-8",
    )
    attempt = build_frozen_attempt(
        attempt_id="s1:test:exact-script",
        session_id="s1",
        stage="read_qc",
        method_name="read_qc.long_read_stats.seqkit_stats",
        script_path=declared_script,
        working_dir=work_dir,
        expected_outputs=[expected_output],
        env=None,
        env_spec=None,
    )

    result = LocalRunner().run(attempt)

    assert result.success
    assert attempt.script_path == declared_script
    assert expected_output.read_text(encoding="utf-8").strip() == "expected"
    assert not decoy_output.exists()
