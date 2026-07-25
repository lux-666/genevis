"""Minimal initialization for project-owned Gene Agent configuration."""

from __future__ import annotations

import os
from copy import deepcopy
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from pathlib import Path
from typing import Any, Callable, Mapping

import yaml

from app.execution.env_resolver import EnvResolver

_SHARED_TARGET_KEY = "shared_conda_target"
_AUTOHIC_TARGET_KEY = "autohic_conda_target"
_PACKAGE_VALUE_KEYS = {
    "autohic_root": "autohic",
    "juicer_root": "juicer",
    "three_d_dna_root": "three_d_dna",
    "juicer_tools_root": "juicer_tools",
}
_MODEL_VALUE_KEYS = {
    "error_model_path": "error",
    "chromosome_model_path": "chromosome",
}
_SECRET_ENV_KEYS = {"GENE_AGENT_LLM_API_KEY"}


@dataclass(frozen=True)
class ProjectConfigChange:
    """One user-visible field change; secret values stay out of repr/output."""

    file: Path
    field: str
    value: str = dataclass_field(repr=False)
    secret: bool = False

    def display_value(self) -> str:
        return "<redacted>" if self.secret else self.value


@dataclass
class ProjectBootstrapPlan:
    """A deterministic project-config write plan."""

    config_path: Path
    dotenv_path: Path
    config_data: dict[str, Any] = dataclass_field(repr=False)
    dotenv_updates: dict[str, str] = dataclass_field(
        default_factory=dict,
        repr=False,
    )
    changes: list[ProjectConfigChange] = dataclass_field(default_factory=list)
    unresolved_fields: list[str] = dataclass_field(default_factory=list)
    config_changed: bool = False

    @property
    def has_changes(self) -> bool:
        return self.config_changed or bool(self.dotenv_updates)


def project_root_for_config(config_path: Path) -> Path:
    """Resolve the project root associated with an env config path."""
    config_path = Path(config_path)
    if config_path.parent.name == "config":
        return config_path.parent.parent
    return config_path.parent


def build_project_bootstrap_plan(
    config_path: Path,
    *,
    config_values: Mapping[str, str] | None = None,
    dotenv_values: Mapping[str, str] | None = None,
    configure_autohic: bool | None = None,
    approved_overwrites: set[str] | None = None,
    template_path: Path | None = None,
) -> ProjectBootstrapPlan:
    """Build a minimal, idempotent plan without writing any files.

    Existing non-placeholder values are retained unless their field name is in
    ``approved_overwrites``. Missing files use ``envs.yaml.example`` when it is
    available and otherwise fall back to the smallest mapping accepted by
    :class:`EnvResolver`.
    """
    config_path = Path(config_path)
    values = {
        str(key): str(value)
        for key, value in (config_values or {}).items()
        if str(value).strip()
    }
    approved = set(approved_overwrites or set())
    dotenv_path = project_root_for_config(config_path) / ".env"

    source_exists = config_path.exists()
    original = _read_yaml(config_path) if source_exists else {}
    if source_exists:
        draft = deepcopy(original)
    else:
        template = _resolve_template(config_path, template_path)
        draft = _read_yaml(template) if template is not None else _minimal_config()

    if configure_autohic is None:
        # A newly generated project config must already contain the setup that
        # the AutoHiC execution path consumes. Existing projects remain
        # unchanged unless they already declare AutoHiC or explicitly opt in.
        configure_autohic = (
            bool(_mapping(draft.get("packages")).get("autohic"))
            if source_exists
            else True
        )

    changes: list[ProjectConfigChange] = []
    unresolved: list[str] = []
    shared_target = values.get(_SHARED_TARGET_KEY, "env_geno_exec")

    default = _ensure_mapping(draft, "default")
    if not default.get("conda_bin"):
        default["conda_bin"] = "conda"
        _record(changes, config_path, "default.conda_bin", "conda")
    if _target_missing(default, fallback=True):
        _set_target(default, shared_target, fallback=True)
        field_name = (
            "default.fallback_prefix"
            if _is_prefix(shared_target)
            else "default.fallback_env"
        )
        _record(changes, config_path, field_name, shared_target)
    _replace_placeholder_targets(draft, shared_target)

    if not source_exists and not configure_autohic:
        # Package paths are optional project setup. Do not leave an invalid
        # collection of example placeholders in a newly initialized project.
        draft["packages"] = {}
    elif configure_autohic:
        _ensure_autohic_shape(draft)
        _configure_autohic(
            draft,
            values=values,
            approved=approved,
            config_path=config_path,
            changes=changes,
            unresolved=unresolved,
        )

    config_changed = not source_exists or draft != original
    if not source_exists:
        changes.insert(0, ProjectConfigChange(config_path, "<file>", "create"))

    updates = _dotenv_updates(
        dotenv_path,
        dotenv_values or {},
        approved=approved,
        changes=changes,
    )
    return ProjectBootstrapPlan(
        config_path=config_path,
        dotenv_path=dotenv_path,
        config_data=draft,
        dotenv_updates=updates,
        changes=changes,
        unresolved_fields=sorted(set(unresolved)),
        config_changed=config_changed,
    )


def collect_project_bootstrap_plan(
    config_path: Path,
    *,
    prompt: Callable[[str], str] = input,
    secret_prompt: Callable[[str], str] | None = None,
    template_path: Path | None = None,
) -> ProjectBootstrapPlan:
    """Ask only for currently missing project values and return a write plan."""
    config_path = Path(config_path)
    source_exists = config_path.exists()
    source_path = (
        config_path if source_exists else _resolve_template(config_path, template_path)
    )
    source = _read_yaml(source_path) if source_path is not None else _minimal_config()
    inspect = deepcopy(source)
    values: dict[str, str] = {}

    default = _mapping(inspect.get("default"))
    if _target_missing(default, fallback=True):
        values[_SHARED_TARGET_KEY] = _ask_text(
            prompt,
            "Shared bioinformatics conda environment or prefix",
            default="env_geno_exec",
        )

    packages = _mapping(inspect.get("packages"))
    configure_autohic = bool(packages.get("autohic")) if source_exists else True
    if source_exists and not configure_autohic:
        configure_autohic = _ask_yes_no(
            prompt,
            "Configure AutoHiC, Juicer, and 3D-DNA paths now?",
        )
    if configure_autohic:
        _ensure_autohic_shape(inspect)
        methods = _mapping(inspect.get("methods"))
        autohic_method = _mapping(methods.get("hic_scaffolding.candidate.autohic"))
        if _target_missing(autohic_method, fallback=False):
            values[_AUTOHIC_TARGET_KEY] = _ask_text(
                prompt,
                "AutoHiC conda environment or prefix",
                default="autohic",
            )

        packages = _mapping(inspect.get("packages"))
        labels = {
            "autohic_root": "AutoHiC installation root",
            "juicer_root": "Juicer installation root",
            "three_d_dna_root": "3D-DNA installation root",
            "juicer_tools_root": "Juicer tools directory",
        }
        for value_key, package_name in _PACKAGE_VALUE_KEYS.items():
            current = _mapping(packages.get(package_name)).get("install_root")
            if not current or _is_placeholder(current):
                entered = _ask_text(prompt, labels[value_key])
                if entered:
                    values[value_key] = entered

        autohic = _mapping(packages.get("autohic"))
        models = _mapping(autohic.get("models"))
        model_labels = {
            "error_model_path": "AutoHiC error model path",
            "chromosome_model_path": "AutoHiC chromosome model path",
        }
        for value_key, model_name in _MODEL_VALUE_KEYS.items():
            current = models.get(model_name)
            if not current or _is_placeholder(current):
                entered = _ask_text(prompt, model_labels[value_key])
                if entered:
                    values[value_key] = entered

    dotenv_path = project_root_for_config(config_path) / ".env"
    dotenv_existing = _read_dotenv(dotenv_path)
    dotenv_values: dict[str, str] = {}
    if "GENE_AGENT_LLM_API_KEY" not in dotenv_existing and _ask_yes_no(
        prompt,
        "Add optional LLM settings to .env?",
    ):
        if secret_prompt is not None:
            try:
                api_key = secret_prompt(
                    "GENE_AGENT_LLM_API_KEY (hidden, blank to skip): "
                ).strip()
            except (EOFError, KeyboardInterrupt):
                api_key = ""
            if api_key:
                dotenv_values["GENE_AGENT_LLM_API_KEY"] = api_key
        base_url = _ask_text(prompt, "GENE_AGENT_LLM_BASE_URL (blank for default)")
        model = _ask_text(prompt, "GENE_AGENT_LLM_MODEL (blank for default)")
        if base_url:
            dotenv_values["GENE_AGENT_LLM_BASE_URL"] = base_url
        if model:
            dotenv_values["GENE_AGENT_LLM_MODEL"] = model

    return build_project_bootstrap_plan(
        config_path,
        config_values=values,
        dotenv_values=dotenv_values,
        configure_autohic=configure_autohic,
        template_path=template_path,
    )


def apply_project_bootstrap_plan(plan: ProjectBootstrapPlan) -> EnvResolver:
    """Apply an already-approved plan and validate it with ``EnvResolver``."""
    if plan.unresolved_fields:
        fields = ", ".join(plan.unresolved_fields)
        raise ValueError(f"bootstrap values are still missing: {fields}")

    if plan.config_changed:
        plan.config_path.parent.mkdir(parents=True, exist_ok=True)
        rendered = yaml.safe_dump(plan.config_data, sort_keys=False, allow_unicode=True)
        plan.config_path.write_text(rendered, encoding="utf-8")
    if plan.dotenv_updates:
        _write_dotenv(plan.dotenv_path, plan.dotenv_updates)

    return EnvResolver(plan.config_path)


def _resolve_template(config_path: Path, explicit: Path | None) -> Path | None:
    candidates = []
    if explicit is not None:
        candidates.append(Path(explicit))
    else:
        candidates.extend(
            [
                config_path.with_name(f"{config_path.name}.example"),
                Path(__file__).resolve().parents[2] / "config" / "envs.yaml.example",
            ]
        )
    return next((path for path in candidates if path.exists()), None)


def _read_yaml(path: Path) -> dict[str, Any]:
    with Path(path).open("r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"env config must be a mapping: {path}")
    return raw


def _minimal_config() -> dict[str, Any]:
    return {
        "default": {"conda_bin": "conda", "fallback_env": "env_geno_exec"},
        "packages": {},
        "stages": {},
        "methods": {},
    }


def _mapping(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _ensure_mapping(parent: dict[str, Any], key: str) -> dict[str, Any]:
    value = parent.get(key)
    if value is None:
        value = {}
        parent[key] = value
    if not isinstance(value, dict):
        raise ValueError(f"{key} must be a mapping")
    return value


def _is_placeholder(value: object) -> bool:
    text = str(value or "")
    return text == "your_tool_env" or text.startswith("/path/to/")


def _is_prefix(value: str) -> bool:
    return value.startswith(("/", "~", "."))


def _target_missing(entry: dict[str, Any], *, fallback: bool) -> bool:
    env_key = "fallback_env" if fallback else "conda_env"
    prefix_key = "fallback_prefix" if fallback else "conda_prefix"
    return (
        not entry.get(env_key)
        and not entry.get(prefix_key)
        or any(
            _is_placeholder(entry.get(key))
            for key in (env_key, prefix_key)
            if entry.get(key)
        )
    )


def _set_target(entry: dict[str, Any], value: str, *, fallback: bool) -> None:
    env_key = "fallback_env" if fallback else "conda_env"
    prefix_key = "fallback_prefix" if fallback else "conda_prefix"
    entry.pop(env_key, None)
    entry.pop(prefix_key, None)
    entry[prefix_key if _is_prefix(value) else env_key] = value


def _replace_placeholder_targets(node: object, target: str) -> None:
    if isinstance(node, dict):
        for key, value in list(node.items()):
            if key in {"conda_env", "fallback_env"} and value == "your_tool_env":
                node.pop(key)
                replacement = (
                    "conda_prefix" if key == "conda_env" else "fallback_prefix"
                )
                node[replacement if _is_prefix(target) else key] = target
            else:
                _replace_placeholder_targets(value, target)
    elif isinstance(node, list):
        for value in node:
            _replace_placeholder_targets(value, target)


def _ensure_autohic_shape(draft: dict[str, Any]) -> None:
    packages = _ensure_mapping(draft, "packages")
    package_defaults: dict[str, dict[str, Any]] = {
        "autohic": {
            "entrypoints": ["autohic.py"],
            "canary_paths": [
                "autohic.py",
                "src/common/run.sh",
                "src/models/cfgs/error_model.py",
            ],
        },
        "juicer": {
            "conform_on_bootstrap": True,
            "jar_root_ref": "juicer_tools",
            "entrypoints": ["CPU/juicer.sh"],
            "canary_paths": [
                "CPU/juicer.sh",
                "CPU/common/chimeric_blacklist.awk",
                "misc/generate_site_positions.py",
                "scripts/common/juicer_tools.jar",
            ],
        },
        "three_d_dna": {
            "entrypoints": ["run-asm-pipeline.sh", "run-asm-pipeline-post-review.sh"],
            "canary_paths": [
                "run-asm-pipeline.sh",
                "run-asm-pipeline-post-review.sh",
                "edit/apply-edits-prep-for-next-round.sh",
                "visualize/run-assembly-visualizer.sh",
            ],
        },
        "juicer_tools": {"canary_paths": ["juicer_tools.jar"]},
    }
    for package_name, defaults in package_defaults.items():
        entry = packages.setdefault(package_name, {})
        if not isinstance(entry, dict):
            raise ValueError(f"packages.{package_name} must be a mapping")
        for key, value in defaults.items():
            entry.setdefault(key, deepcopy(value))

    methods = _ensure_mapping(draft, "methods")
    method = methods.setdefault("hic_scaffolding.candidate.autohic", {})
    if not isinstance(method, dict):
        raise ValueError("methods.hic_scaffolding.candidate.autohic must be a mapping")
    method.setdefault("required_tools", ["python3.9", "bwa", "java", "quast.py"])
    method.setdefault("package_tools", ["autohic", "juicer", "three_d_dna"])


def _configure_autohic(
    draft: dict[str, Any],
    *,
    values: Mapping[str, str],
    approved: set[str],
    config_path: Path,
    changes: list[ProjectConfigChange],
    unresolved: list[str],
) -> None:
    methods = _mapping(draft.get("methods"))
    method = _mapping(methods.get("hic_scaffolding.candidate.autohic"))
    target_field = "methods.hic_scaffolding.candidate.autohic.target"
    if _target_missing(method, fallback=False):
        target = values.get(_AUTOHIC_TARGET_KEY, "autohic")
        _set_target(method, target, fallback=False)
        _record(changes, config_path, target_field, target)

    packages = _mapping(draft.get("packages"))
    for value_key, package_name in _PACKAGE_VALUE_KEYS.items():
        entry = _mapping(packages.get(package_name))
        field_name = f"packages.{package_name}.install_root"
        _fill_field(
            entry,
            "install_root",
            field_name,
            values.get(value_key),
            approved,
            config_path,
            changes,
            unresolved,
        )

    autohic = _mapping(packages.get("autohic"))
    models = _ensure_mapping(autohic, "models")
    for value_key, model_name in _MODEL_VALUE_KEYS.items():
        field_name = f"packages.autohic.models.{model_name}"
        _fill_field(
            models,
            model_name,
            field_name,
            values.get(value_key),
            approved,
            config_path,
            changes,
            unresolved,
        )


def _fill_field(
    entry: dict[str, Any],
    key: str,
    field_name: str,
    candidate: str | None,
    approved: set[str],
    config_path: Path,
    changes: list[ProjectConfigChange],
    unresolved: list[str],
) -> None:
    current = entry.get(key)
    missing = not current or _is_placeholder(current)
    if candidate and (missing or field_name in approved):
        if str(current or "") != candidate:
            entry[key] = candidate
            _record(changes, config_path, field_name, candidate)
        return
    if missing:
        unresolved.append(field_name)


def _dotenv_updates(
    dotenv_path: Path,
    supplied: Mapping[str, str],
    *,
    approved: set[str],
    changes: list[ProjectConfigChange],
) -> dict[str, str]:
    existing = _read_dotenv(dotenv_path)
    updates: dict[str, str] = {}
    for raw_key, raw_value in supplied.items():
        key = str(raw_key).strip()
        value = str(raw_value)
        if not key.startswith("GENE_AGENT_"):
            raise ValueError(
                f"bootstrap only writes GENE_AGENT_* values to .env: {key}"
            )
        if "\n" in value or "\r" in value:
            raise ValueError(f".env value cannot contain a newline: {key}")
        if not value or existing.get(key) == value:
            continue
        field_name = f".env.{key}"
        if key in existing and field_name not in approved:
            continue
        updates[key] = value
        changes.append(
            ProjectConfigChange(
                dotenv_path,
                key,
                value,
                secret=key in _SECRET_ENV_KEYS,
            )
        )
    return updates


def _read_dotenv(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    parsed: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        parsed[key.strip()] = value.strip().strip('"').strip("'")
    return parsed


def _write_dotenv(path: Path, updates: Mapping[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        lines = path.read_text(encoding="utf-8").splitlines()
        remaining = dict(updates)
        rendered: list[str] = []
        for line in lines:
            stripped = line.strip()
            if stripped and not stripped.startswith("#") and "=" in stripped:
                key = stripped.split("=", 1)[0].strip()
                if key in remaining:
                    rendered.append(f"{key}={remaining.pop(key)}")
                    continue
            rendered.append(line)
        rendered.extend(f"{key}={value}" for key, value in remaining.items())
        path.write_text("\n".join(rendered).rstrip() + "\n", encoding="utf-8")
        return

    payload = "".join(f"{key}={value}\n" for key, value in updates.items())
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(payload)
    os.chmod(path, 0o600)


def _record(
    changes: list[ProjectConfigChange], path: Path, field_name: str, value: str
) -> None:
    changes.append(ProjectConfigChange(path, field_name, str(value)))


def _ask_text(
    prompt: Callable[[str], str], label: str, *, default: str | None = None
) -> str:
    suffix = f" [{default}]" if default is not None else ""
    try:
        answer = prompt(f"{label}{suffix}: ").strip()
    except (EOFError, KeyboardInterrupt):
        answer = ""
    return answer or (default or "")


def _ask_yes_no(prompt: Callable[[str], str], label: str) -> bool:
    try:
        answer = prompt(f"{label} [y/N]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    return answer in {"y", "yes"}
