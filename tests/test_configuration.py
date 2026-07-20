from app.configuration import load_descriptor_registry


def test_descriptor_registry_loading_is_cwd_independent(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)

    registry = load_descriptor_registry()

    assert "assembly.candidate.flye" in registry.methods
    assert registry.root_dir.name == "descriptors"
