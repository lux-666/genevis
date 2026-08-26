"""Minimal initialization for project-owned Gene Agent configuration."""

from __future__ import annotations

import os
import re
import stat
import tempfile
from copy import deepcopy
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import yaml

from app.execution.env_resolver import EnvResolver

_SHARED_TARGET_KEY = "shared_conda_target"
_AUTOHIC_TARGET_KEY = "autohic_conda_target"
_CONDA_BIN_KEY = "conda_bin"
_BUSCO_LINEAGE_PATH_KEY = "busco_lineage_path"
_NCBI_REFERENCE_DIR_KEY = "ncbi_metazoa_reference_dir"
_SHARED_TARGET_UPDATE_KEY = "default.fallback_target"
_AUTOHIC_TARGET_UPDATE_KEY = "methods.hic_scaffolding.candidate.autohic.target"
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
_LLM_ENV_KEYS = (
    "GENE_AGENT_LLM_API_KEY",
    "GENE_AGENT_LLM_BASE_URL",
    "GENE_AGENT_LLM_MODEL",
)
_UPDATE_CONFIG_FIELDS = frozenset(
    {
        "default.conda_bin",
        "default.fallback_env",
        "default.fallback_prefix",
        "methods.hic_scaffolding.candidate.autohic.conda_env",
        "methods.hic_scaffolding.candidate.autohic.conda_prefix",
        *(
            f"packages.{package}.install_root"
            for package in _PACKAGE_VALUE_KEYS.values()
        ),
        *(f"packages.autohic.models.{model}" for model in _MODEL_VALUE_KEYS.values()),
    }
)
_TARGET_FIELD_GROUPS = (
    frozenset({"default.fallback_env", "default.fallback_prefix"}),
    frozenset(
        {
            "methods.hic_scaffolding.candidate.autohic.conda_env",
            "methods.hic_scaffolding.candidate.autohic.conda_prefix",
        }
    ),
)


@dataclass(frozen=True)
class ProjectConfigChange:
    """One user-visible field change; secret values stay out of repr/output."""

    file: Path
    field: str
    value: str = dataclass_field(repr=False)
    secret: bool = False
    previous_value: str | None = dataclass_field(default=None, repr=False)

    def display_value(self) -> str:
        return "<redacted>" if self.secret else self.value

    def display_previous_value(self) -> str:
        if self.previous_value is None:
            return "<unset>"
        return "<redacted>" if self.secret else self.previous_value


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
    config_source_exists: bool = False
    config_source_text: str | None = dataclass_field(default=None, repr=False)
    config_source_data: dict[str, Any] = dataclass_field(
        default_factory=dict,
        repr=False,
    )
    dotenv_source_text: str | None = dataclass_field(default=None, repr=False)
    conformance_packages: set[str] = dataclass_field(default_factory=set)
    update_mode: bool = False
    changes: list[ProjectConfigChange] = dataclass_field(default_factory=list)
    unresolved_fields: list[str] = dataclass_field(default_factory=list)
    config_changed: bool = False

    @property
    def has_changes(self) -> bool:
        return self.config_changed or bool(self.dotenv_updates)


@dataclass(frozen=True)
class _UpdateChoice:
    """One explicitly selectable bootstrap-owned setting."""

    approval_key: str
    label: str
    current_value: str | None
    config_value_key: str | None = None
    dotenv_key: str | None = None
    secret: bool = False


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
    explicit_update = approved_overwrites is not None
    approved = set(approved_overwrites or set())
    dotenv_path = project_root_for_config(config_path) / ".env"

    source_exists = config_path.exists()
    config_source_text = _read_text_if_exists(config_path)
    original = (
        _parse_yaml_text(config_source_text or "", source=config_path)
        if source_exists
        else {}
    )
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

    default = _ensure_mapping(draft, "default")
    conda_bin = values.get(_CONDA_BIN_KEY)
    current_conda_bin = default.get("conda_bin")
    if not current_conda_bin and (
        not explicit_update
        or ("default.conda_bin" in approved and conda_bin is not None)
    ):
        default["conda_bin"] = conda_bin or "conda"
        _record(
            changes,
            config_path,
            "default.conda_bin",
            str(default["conda_bin"]),
        )
    elif conda_bin and "default.conda_bin" in approved:
        if str(current_conda_bin) != conda_bin:
            default["conda_bin"] = conda_bin
            _record(
                changes,
                config_path,
                "default.conda_bin",
                conda_bin,
                previous_value=str(current_conda_bin),
            )

    current_shared_target = _target_value(default, fallback=True)
    if _target_missing(default, fallback=True) and (
        not explicit_update
        or (
            _shared_target_overwrite_approved(approved)
            and values.get(_SHARED_TARGET_KEY)
        )
    ):
        shared_target = values.get(_SHARED_TARGET_KEY, "env_geno_exec")
        _set_target(default, shared_target, fallback=True)
        _record(
            changes,
            config_path,
            _target_field_name(shared_target, fallback=True),
            shared_target,
            previous_value=current_shared_target,
        )
    elif _shared_target_overwrite_approved(approved) and values.get(_SHARED_TARGET_KEY):
        shared_target = values[_SHARED_TARGET_KEY]
        if current_shared_target != shared_target:
            _set_target(default, shared_target, fallback=True)
            _record(
                changes,
                config_path,
                _target_field_name(shared_target, fallback=True),
                shared_target,
                previous_value=current_shared_target,
            )
    else:
        shared_target = current_shared_target or "env_geno_exec"
    if not explicit_update:
        _replace_placeholder_targets(draft, shared_target)
    _configure_databases(
        draft,
        values=values,
        config_path=config_path,
        changes=changes,
        unresolved=unresolved,
        explicit_update=explicit_update,
    )

    if not source_exists and not configure_autohic:
        # Package paths are optional project setup. Do not leave an invalid
        # collection of example placeholders in a newly initialized project.
        draft["packages"] = {}
    elif configure_autohic:
        if not explicit_update:
            _ensure_autohic_shape(draft)
        _configure_autohic(
            draft,
            values=values,
            approved=approved,
            config_path=config_path,
            changes=changes,
            unresolved=unresolved,
            explicit_update=explicit_update,
        )

    config_changed = not source_exists or draft != original
    if not source_exists:
        changes.insert(0, ProjectConfigChange(config_path, "<file>", "create"))

    dotenv_source_text = _read_text_if_exists(dotenv_path)
    updates = _dotenv_updates(
        dotenv_path,
        dotenv_values or {},
        existing=_parse_dotenv_text(dotenv_source_text or ""),
        approved=approved,
        changes=changes,
    )
    return ProjectBootstrapPlan(
        config_path=config_path,
        dotenv_path=dotenv_path,
        config_data=draft,
        dotenv_updates=updates,
        config_source_exists=source_exists,
        config_source_text=config_source_text,
        config_source_data=original,
        dotenv_source_text=dotenv_source_text,
        conformance_packages=_conformance_packages_for_changes(changes),
        update_mode=explicit_update,
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
    update: bool = False,
) -> ProjectBootstrapPlan:
    """Collect a repair plan, or explicitly selected field replacements."""
    config_path = Path(config_path)
    source_exists = config_path.exists()
    if update and not source_exists:
        raise FileNotFoundError(
            "bootstrap --update requires an existing config/envs.yaml; "
            "initialize it first with interactive bootstrap"
        )
    source_path = (
        config_path if source_exists else _resolve_template(config_path, template_path)
    )
    source = _read_yaml(source_path) if source_path is not None else _minimal_config()
    inspect = deepcopy(source)
    if update and source_exists:
        return _collect_project_bootstrap_update_plan(
            config_path,
            inspect=inspect,
            prompt=prompt,
            secret_prompt=secret_prompt,
            template_path=template_path,
        )

    values: dict[str, str] = {}

    default = _mapping(inspect.get("default"))
    if _target_missing(default, fallback=True):
        values[_SHARED_TARGET_KEY] = _ask_text(
            prompt,
            "Shared bioinformatics conda environment or prefix",
            default="env_geno_exec",
        )

    # An existing pre-database envs.yaml is a migration input, not a reason to
    # skip the new host contract. Prompt for the two database locations in the
    # same compact group used for a fresh config.
    databases = _mapping(inspect.get("databases"))
    busco = _mapping(databases.get("busco"))
    default_lineage = str(busco.get("default_lineage") or "").strip()
    lineages = _mapping(busco.get("lineages"))
    current_lineage = _mapping(lineages.get(default_lineage))
    current_lineage_path = current_lineage.get("path")
    if not current_lineage_path or _is_placeholder(current_lineage_path):
        entered = _ask_text(
            prompt,
            "Default BUSCO lineage directory (basename must end in _odbN)",
        )
        if entered:
            values[_BUSCO_LINEAGE_PATH_KEY] = entered
    ncbi = _mapping(databases.get("ncbi_metazoa"))
    current_ncbi_dir = ncbi.get("reference_dir")
    if not current_ncbi_dir or _is_placeholder(current_ncbi_dir):
        entered = _ask_text(prompt, "NCBI Metazoa reference directory")
        if entered:
            values[_NCBI_REFERENCE_DIR_KEY] = entered

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


def _collect_project_bootstrap_update_plan(
    config_path: Path,
    *,
    inspect: dict[str, Any],
    prompt: Callable[[str], str],
    secret_prompt: Callable[[str], str] | None,
    template_path: Path | None,
) -> ProjectBootstrapPlan:
    """Collect only explicit replacements for bootstrap-owned settings."""
    default = _mapping(inspect.get("default"))
    packages = _mapping(inspect.get("packages"))
    methods = _mapping(inspect.get("methods"))
    autohic_method = _mapping(methods.get("hic_scaffolding.candidate.autohic"))
    dotenv_path = project_root_for_config(config_path) / ".env"
    dotenv_existing = _read_dotenv(dotenv_path)

    choices = [
        _UpdateChoice(
            approval_key="default.conda_bin",
            label="Conda executable",
            current_value=_text_or_none(default.get("conda_bin")),
            config_value_key=_CONDA_BIN_KEY,
        ),
        _UpdateChoice(
            approval_key=_SHARED_TARGET_UPDATE_KEY,
            label="Shared bioinformatics conda environment or prefix",
            current_value=_target_value(default, fallback=True),
            config_value_key=_SHARED_TARGET_KEY,
        ),
    ]
    has_autohic = bool(packages.get("autohic"))
    if has_autohic:
        choices.append(
            _UpdateChoice(
                approval_key=_AUTOHIC_TARGET_UPDATE_KEY,
                label="AutoHiC conda environment or prefix",
                current_value=_target_value(autohic_method, fallback=False),
                config_value_key=_AUTOHIC_TARGET_KEY,
            )
        )
        package_labels = {
            "autohic_root": "AutoHiC installation root",
            "juicer_root": "Juicer installation root",
            "three_d_dna_root": "3D-DNA installation root",
            "juicer_tools_root": "Juicer tools directory",
        }
        for value_key, package_name in _PACKAGE_VALUE_KEYS.items():
            choices.append(
                _UpdateChoice(
                    approval_key=f"packages.{package_name}.install_root",
                    label=package_labels[value_key],
                    current_value=_text_or_none(
                        _mapping(packages.get(package_name)).get("install_root")
                    ),
                    config_value_key=value_key,
                )
            )

        models = _mapping(_mapping(packages.get("autohic")).get("models"))
        model_labels = {
            "error_model_path": "AutoHiC error model path",
            "chromosome_model_path": "AutoHiC chromosome model path",
        }
        for value_key, model_name in _MODEL_VALUE_KEYS.items():
            choices.append(
                _UpdateChoice(
                    approval_key=f"packages.autohic.models.{model_name}",
                    label=model_labels[value_key],
                    current_value=_text_or_none(models.get(model_name)),
                    config_value_key=value_key,
                )
            )

    for dotenv_key in _LLM_ENV_KEYS:
        choices.append(
            _UpdateChoice(
                approval_key=f".env.{dotenv_key}",
                label=dotenv_key,
                current_value=dotenv_existing.get(dotenv_key),
                dotenv_key=dotenv_key,
                secret=dotenv_key in _SECRET_ENV_KEYS,
            )
        )

    selected = _ask_update_choices(prompt, choices)
    values: dict[str, str] = {}
    dotenv_values: dict[str, str] = {}
    approved: set[str] = set()
    for choice in selected:
        if choice.secret:
            entered = _ask_secret_text(secret_prompt, choice.label)
        else:
            entered = prompt(f"{choice.label} (blank keeps current value): ").strip()
        if not entered:
            continue
        approved.add(choice.approval_key)
        if choice.config_value_key is not None:
            values[choice.config_value_key] = entered
        elif choice.dotenv_key is not None:
            dotenv_values[choice.dotenv_key] = entered

    autohic_selected = any(
        key == _AUTOHIC_TARGET_UPDATE_KEY or key.startswith("packages.")
        for key in approved
    )
    configure_autohic = has_autohic and autohic_selected

    return build_project_bootstrap_plan(
        config_path,
        config_values=values,
        dotenv_values=dotenv_values,
        configure_autohic=configure_autohic,
        approved_overwrites=approved,
        template_path=template_path,
    )


def apply_project_bootstrap_plan(plan: ProjectBootstrapPlan) -> EnvResolver:
    """Revalidate and atomically publish an already-approved plan."""
    validate_project_bootstrap_plan(plan)

    staged: list[tuple[Path, Path]] = []
    try:
        if plan.config_changed:
            rendered = yaml.safe_dump(
                plan.config_data,
                sort_keys=False,
                allow_unicode=True,
            )
            mode = (
                stat.S_IMODE(plan.config_path.stat().st_mode)
                if plan.config_path.exists()
                else 0o644
            )
            staged.append(
                (_stage_text(plan.config_path, rendered, mode=mode), plan.config_path)
            )
        if plan.dotenv_updates:
            rendered = _render_dotenv(plan.dotenv_path, plan.dotenv_updates)
            staged.append(
                (_stage_text(plan.dotenv_path, rendered, mode=0o600), plan.dotenv_path)
            )
        for temporary, destination in staged:
            os.replace(temporary, destination)
            _fsync_directory(destination.parent)
    finally:
        for temporary, _destination in staged:
            temporary.unlink(missing_ok=True)

    return EnvResolver(plan.config_path)


def validate_project_bootstrap_plan(plan: ProjectBootstrapPlan) -> EnvResolver:
    """Validate the complete candidate without publishing any file."""
    if plan.unresolved_fields:
        fields = ", ".join(plan.unresolved_fields)
        raise ValueError(f"bootstrap values are still missing: {fields}")

    if _read_text_if_exists(plan.config_path) != plan.config_source_text:
        raise ValueError("bootstrap plan source config changed before publication")
    if _read_text_if_exists(plan.dotenv_path) != plan.dotenv_source_text:
        raise ValueError("bootstrap plan source .env changed before publication")

    source_changed = not plan.config_source_exists or (
        plan.config_source_data != plan.config_data
    )
    if source_changed != plan.config_changed:
        raise ValueError("bootstrap plan config_changed does not match config_data")
    config_changes = [
        change for change in plan.changes if change.file == plan.config_path
    ]
    if plan.config_changed and not config_changes:
        raise ValueError("bootstrap plan changed config without recording a change")
    if plan.update_mode:
        _validate_update_config_changes(plan, config_changes)
    _validate_dotenv_plan(plan)

    expected_conformance = _conformance_packages_for_changes(plan.changes)
    if plan.conformance_packages != expected_conformance:
        raise ValueError(
            "bootstrap plan conformance_packages do not match recorded changes"
        )

    from app.execution.package_bootstrap import verify_envs_yaml

    issues = verify_envs_yaml(
        plan.config_path,
        config_data=plan.config_data,
        conformance_packages=plan.conformance_packages,
    )
    if issues:
        raise ValueError("invalid project configuration: " + "; ".join(issues))

    return EnvResolver(plan.config_path, config_data=plan.config_data)


def _validate_update_config_changes(
    plan: ProjectBootstrapPlan,
    config_changes: Sequence[ProjectConfigChange],
) -> None:
    """Require an explicit update plan to describe its complete candidate diff."""

    if not plan.config_source_exists:
        raise ValueError("bootstrap update plan requires an existing config source")
    actual_fields = _changed_config_fields(
        plan.config_source_data,
        plan.config_data,
    )
    recorded_fields: set[str] = set()
    for change in config_changes:
        if change.field not in _UPDATE_CONFIG_FIELDS:
            raise ValueError(
                f"bootstrap update contains unmanaged config change: {change.field}"
            )
        if change.field in recorded_fields:
            raise ValueError(
                f"bootstrap update records config field twice: {change.field}"
            )
        recorded_fields.add(change.field)
        source_value, candidate_value = _recorded_config_values(
            plan.config_source_data,
            plan.config_data,
            change.field,
        )
        if candidate_value is None or str(candidate_value) != change.value:
            raise ValueError(
                f"bootstrap update record does not match candidate: {change.field}"
            )
        expected_previous = None if source_value is None else str(source_value)
        if change.previous_value != expected_previous:
            raise ValueError(
                f"bootstrap update record does not match source: {change.field}"
            )

    for field_name in sorted(actual_fields):
        if field_name not in _UPDATE_CONFIG_FIELDS:
            raise ValueError(
                f"bootstrap update has unrecorded config change: {field_name}"
            )
        if field_name in recorded_fields:
            continue
        target_group = _target_field_group(field_name)
        if target_group is not None and target_group & recorded_fields:
            continue
        raise ValueError(f"bootstrap update has unrecorded config change: {field_name}")

    for field_name in sorted(recorded_fields):
        if field_name in actual_fields:
            continue
        raise ValueError(
            f"bootstrap update records unchanged config field: {field_name}"
        )


def _validate_dotenv_plan(plan: ProjectBootstrapPlan) -> None:
    """Reapply the lower-level .env ownership rules at publication time."""

    existing = _parse_dotenv_text(plan.dotenv_source_text or "")
    dotenv_changes = [
        change for change in plan.changes if change.file == plan.dotenv_path
    ]
    recorded: dict[str, ProjectConfigChange] = {}
    for change in dotenv_changes:
        if change.field in recorded:
            raise ValueError(f"bootstrap plan records .env field twice: {change.field}")
        recorded[change.field] = change

    for key, value in plan.dotenv_updates.items():
        if not key.startswith("GENE_AGENT_"):
            raise ValueError(
                f"bootstrap only writes GENE_AGENT_* values to .env: {key}"
            )
        if plan.update_mode and key not in _LLM_ENV_KEYS:
            raise ValueError(f"bootstrap update does not manage .env field: {key}")
        if not value or "\n" in value or "\r" in value:
            raise ValueError(f"invalid .env value for bootstrap field: {key}")
        record = recorded.get(key)
        if record is None or record.value != value:
            raise ValueError(
                f"bootstrap plan changed .env {key} without recording a change"
            )
        if record.previous_value != existing.get(key):
            raise ValueError(f"bootstrap .env change does not match source: {key}")
        if record.secret != (key in _SECRET_ENV_KEYS):
            raise ValueError(f"bootstrap .env secret metadata is invalid: {key}")

    extra = sorted(set(recorded).difference(plan.dotenv_updates))
    if extra:
        raise ValueError(
            "bootstrap plan records unchanged .env fields: " + ", ".join(extra)
        )


def _changed_config_fields(
    source: object,
    candidate: object,
    *,
    prefix: tuple[str, ...] = (),
) -> set[str]:
    if isinstance(source, Mapping) and isinstance(candidate, Mapping):
        changed: set[str] = set()
        for key in set(source) | set(candidate):
            key_text = str(key)
            if key not in source or key not in candidate:
                changed.update(
                    _config_leaf_fields(
                        candidate.get(key) if key in candidate else source.get(key),
                        prefix=(*prefix, key_text),
                    )
                )
                continue
            changed.update(
                _changed_config_fields(
                    source[key],
                    candidate[key],
                    prefix=(*prefix, key_text),
                )
            )
        return changed
    if source == candidate:
        return set()
    return {".".join(prefix)}


def _config_leaf_fields(value: object, *, prefix: tuple[str, ...]) -> set[str]:
    if isinstance(value, Mapping) and value:
        fields: set[str] = set()
        for key, child in value.items():
            fields.update(_config_leaf_fields(child, prefix=(*prefix, str(key))))
        return fields
    return {".".join(prefix)}


def _target_field_group(field_name: str) -> frozenset[str] | None:
    return next(
        (group for group in _TARGET_FIELD_GROUPS if field_name in group),
        None,
    )


def _recorded_config_values(
    source: Mapping[str, Any],
    candidate: Mapping[str, Any],
    field_name: str,
) -> tuple[object | None, object | None]:
    target_group = _target_field_group(field_name)
    if target_group == _TARGET_FIELD_GROUPS[0]:
        return (
            _target_value(_mapping(source.get("default")), fallback=True),
            _target_value(_mapping(candidate.get("default")), fallback=True),
        )
    if target_group == _TARGET_FIELD_GROUPS[1]:
        source_methods = _mapping(source.get("methods"))
        candidate_methods = _mapping(candidate.get("methods"))
        method_name = "hic_scaffolding.candidate.autohic"
        return (
            _target_value(_mapping(source_methods.get(method_name)), fallback=False),
            _target_value(
                _mapping(candidate_methods.get(method_name)),
                fallback=False,
            ),
        )
    if field_name == "default.conda_bin":
        return (
            _mapping(source.get("default")).get("conda_bin"),
            _mapping(candidate.get("default")).get("conda_bin"),
        )
    parts = field_name.split(".")
    package_name = parts[1]
    source_package = _mapping(_mapping(source.get("packages")).get(package_name))
    candidate_package = _mapping(_mapping(candidate.get("packages")).get(package_name))
    if parts[2] == "install_root":
        return source_package.get("install_root"), candidate_package.get("install_root")
    model_name = parts[3]
    return (
        _mapping(source_package.get("models")).get(model_name),
        _mapping(candidate_package.get("models")).get(model_name),
    )


def _resolve_template(config_path: Path, explicit: Path | None) -> Path | None:
    candidates = []
    if explicit is not None:
        candidates.append(Path(explicit))
    else:
        candidates.extend(
            [
                config_path.with_name(f"{config_path.name}.example"),
                Path(__file__).resolve().parents[2]
                / "docs"
                / "examples"
                / "envs.yaml.example",
            ]
        )
    return next((path for path in candidates if path.exists()), None)


def _read_yaml(path: Path) -> dict[str, Any]:
    return _parse_yaml_text(Path(path).read_text(encoding="utf-8"), source=path)


def _parse_yaml_text(text: str, *, source: Path) -> dict[str, Any]:
    raw = yaml.safe_load(text) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"env config must be a mapping: {source}")
    return raw


def _minimal_config() -> dict[str, Any]:
    return {
        "default": {"conda_bin": "conda", "fallback_env": "env_geno_exec"},
        "packages": {},
        "stages": {},
        "methods": {},
    }


def _configure_databases(
    draft: dict[str, Any],
    *,
    values: Mapping[str, str],
    config_path: Path,
    changes: list[ProjectConfigChange],
    unresolved: list[str],
    explicit_update: bool,
) -> None:
    """Fill the two host database locations from one compact prompt group."""

    if explicit_update:
        return

    databases = _ensure_mapping(draft, "databases")
    busco = _ensure_mapping(databases, "busco")
    lineages = _ensure_mapping(busco, "lineages")
    current_name = str(busco.get("default_lineage") or "").strip()
    current_entry = _mapping(lineages.get(current_name))
    current_path = current_entry.get("path")
    candidate = values.get(_BUSCO_LINEAGE_PATH_KEY)
    if candidate:
        lineage_name = Path(candidate.rstrip("/")).name
        version_match = re.search(r"_(odb\d+(?:\.\d+)?)$", lineage_name)
        if not lineage_name or version_match is None:
            raise ValueError(
                "default BUSCO lineage directory basename must end in _odbN or _odbN.N"
            )
        version = version_match.group(1)
        if (
            current_name
            and current_name != lineage_name
            and _is_placeholder(current_path)
        ):
            lineages.pop(current_name, None)
        if current_name != lineage_name:
            busco["default_lineage"] = lineage_name
            _record(
                changes,
                config_path,
                "databases.busco.default_lineage",
                lineage_name,
                previous_value=current_name or None,
            )
        entry = _ensure_mapping(lineages, lineage_name)
        for key, value in (
            ("path", candidate),
            ("version", version),
            ("source", str(entry.get("source") or "busco_downloads")),
        ):
            field = f"databases.busco.lineages.{lineage_name}.{key}"
            previous = entry.get(key)
            if previous != value:
                entry[key] = value
                _record(
                    changes,
                    config_path,
                    field,
                    value,
                    previous_value=str(previous) if previous is not None else None,
                )
    elif not current_path or _is_placeholder(current_path):
        unresolved.append(
            f"databases.busco.lineages.{current_name or '<default>'}.path"
        )

    ncbi = _ensure_mapping(databases, "ncbi_metazoa")
    _fill_field(
        ncbi,
        "reference_dir",
        "databases.ncbi_metazoa.reference_dir",
        values.get(_NCBI_REFERENCE_DIR_KEY),
        set(),
        config_path,
        changes,
        unresolved,
    )


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


def _target_value(entry: Mapping[str, Any], *, fallback: bool) -> str | None:
    env_key = "fallback_env" if fallback else "conda_env"
    prefix_key = "fallback_prefix" if fallback else "conda_prefix"
    return _text_or_none(entry.get(prefix_key) or entry.get(env_key))


def _target_field_name(value: str, *, fallback: bool) -> str:
    prefix = "default" if fallback else "methods.hic_scaffolding.candidate.autohic"
    field = "fallback_prefix" if fallback else "conda_prefix"
    if not _is_prefix(value):
        field = "fallback_env" if fallback else "conda_env"
    return f"{prefix}.{field}"


def _shared_target_overwrite_approved(approved: set[str]) -> bool:
    return bool(
        {
            _SHARED_TARGET_UPDATE_KEY,
            "default.fallback_env",
            "default.fallback_prefix",
        }
        & approved
    )


def _autohic_target_overwrite_approved(approved: set[str]) -> bool:
    return bool(
        {
            _AUTOHIC_TARGET_UPDATE_KEY,
            "methods.hic_scaffolding.candidate.autohic.conda_env",
            "methods.hic_scaffolding.candidate.autohic.conda_prefix",
        }
        & approved
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
    explicit_update: bool,
) -> None:
    target_approved = _autohic_target_overwrite_approved(approved)
    methods = (
        _ensure_mapping(draft, "methods")
        if target_approved
        else _mapping(draft.get("methods"))
    )
    method = (
        _ensure_mapping(methods, "hic_scaffolding.candidate.autohic")
        if target_approved
        else _mapping(methods.get("hic_scaffolding.candidate.autohic"))
    )
    current_target = _target_value(method, fallback=False)
    if (_target_missing(method, fallback=False) and not explicit_update) or (
        target_approved and values.get(_AUTOHIC_TARGET_KEY)
    ):
        target = values.get(_AUTOHIC_TARGET_KEY, "autohic")
        _set_target(method, target, fallback=False)
        if current_target != target:
            _record(
                changes,
                config_path,
                _target_field_name(target, fallback=False),
                target,
                previous_value=current_target,
            )

    packages = _mapping(draft.get("packages"))
    for value_key, package_name in _PACKAGE_VALUE_KEYS.items():
        field_name = f"packages.{package_name}.install_root"
        candidate = values.get(value_key)
        if explicit_update and field_name not in approved:
            continue
        entry = (
            _ensure_mapping(packages, package_name)
            if candidate
            else _mapping(packages.get(package_name))
        )
        _fill_field(
            entry,
            "install_root",
            field_name,
            candidate,
            approved,
            config_path,
            changes,
            unresolved,
        )

    autohic = _mapping(packages.get("autohic"))
    selected_model_update = any(
        f"packages.autohic.models.{model_name}" in approved
        for model_name in _MODEL_VALUE_KEYS.values()
    )
    models = (
        _ensure_mapping(autohic, "models")
        if not explicit_update or selected_model_update
        else _mapping(autohic.get("models"))
    )
    for value_key, model_name in _MODEL_VALUE_KEYS.items():
        field_name = f"packages.autohic.models.{model_name}"
        if explicit_update and field_name not in approved:
            continue
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
            _record(
                changes,
                config_path,
                field_name,
                candidate,
                previous_value=str(current) if current is not None else None,
            )
        return
    if missing:
        unresolved.append(field_name)


def _dotenv_updates(
    dotenv_path: Path,
    supplied: Mapping[str, str],
    *,
    existing: Mapping[str, str] | None = None,
    approved: set[str],
    changes: list[ProjectConfigChange],
) -> dict[str, str]:
    existing = dict(existing) if existing is not None else _read_dotenv(dotenv_path)
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
                previous_value=existing.get(key),
                secret=key in _SECRET_ENV_KEYS,
            )
        )
    return updates


def _read_dotenv(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    return _parse_dotenv_text(path.read_text(encoding="utf-8"))


def _parse_dotenv_text(text: str) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        parsed[key.strip()] = value.strip().strip('"').strip("'")
    return parsed


def _read_text_if_exists(path: Path) -> str | None:
    if not path.exists():
        return None
    return path.read_text(encoding="utf-8")


def _conformance_packages_for_changes(
    changes: list[ProjectConfigChange],
) -> set[str]:
    fields = {change.field for change in changes}
    if {
        "packages.juicer.install_root",
        "packages.juicer_tools.install_root",
    } & fields:
        return {"juicer"}
    return set()


def _render_dotenv(path: Path, updates: Mapping[str, str]) -> str:
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
        return "\n".join(rendered).rstrip() + "\n"

    return "".join(f"{key}={value}\n" for key, value in updates.items())


def _stage_text(path: Path, text: str, *, mode: int) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        try:
            os.close(descriptor)
        except OSError:
            pass
        temporary.unlink(missing_ok=True)
        raise
    return temporary


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _record(
    changes: list[ProjectConfigChange],
    path: Path,
    field_name: str,
    value: str,
    *,
    previous_value: str | None = None,
) -> None:
    changes.append(
        ProjectConfigChange(
            path,
            field_name,
            str(value),
            previous_value=previous_value,
        )
    )


def _ask_text(
    prompt: Callable[[str], str], label: str, *, default: str | None = None
) -> str:
    suffix = f" [{default}]" if default is not None else ""
    try:
        answer = prompt(f"{label}{suffix}: ").strip()
    except (EOFError, KeyboardInterrupt):
        answer = ""
    return answer or (default or "")


def _ask_secret_text(
    secret_prompt: Callable[[str], str] | None,
    label: str,
) -> str:
    if secret_prompt is None:
        return ""
    return secret_prompt(f"{label} (hidden, blank keeps current value): ").strip()


def _ask_update_choices(
    prompt: Callable[[str], str], choices: list[_UpdateChoice]
) -> list[_UpdateChoice]:
    lines = ["Managed bootstrap fields:"]
    for index, choice in enumerate(choices, start=1):
        current = "<unset>" if choice.current_value is None else choice.current_value
        if choice.secret and choice.current_value is not None:
            current = "<redacted>"
        lines.append(f"  {index}. {choice.label} [{current}]")
    lines.append("Select fields to update (comma-separated numbers, blank keeps all): ")
    answer = prompt("\n".join(lines)).strip()
    if not answer:
        return []

    selected_indexes: set[int] = set()
    for token in answer.replace(",", " ").split():
        try:
            index = int(token)
        except ValueError as exc:
            raise ValueError(f"invalid bootstrap update selection: {token!r}") from exc
        if index < 1 or index > len(choices):
            raise ValueError(f"invalid bootstrap update selection: {index}")
        selected_indexes.add(index)
    return [
        choice
        for index, choice in enumerate(choices, start=1)
        if index in selected_indexes
    ]


def _text_or_none(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None


def _ask_yes_no(prompt: Callable[[str], str], label: str) -> bool:
    try:
        answer = prompt(f"{label} [y/N]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    return answer in {"y", "yes"}
