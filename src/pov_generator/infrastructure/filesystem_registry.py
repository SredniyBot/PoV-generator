from __future__ import annotations

from pathlib import Path

import yaml

from ..common.errors import ValidationError
from ..domain.registry import (
    RegistrySnapshot,
    parse_artifact_contract,
    parse_domain_pack,
    parse_methodology_pack,
    parse_objective,
    parse_quality_gate,
    parse_task_template,
    parse_vocabulary,
)


class FilesystemRegistryLoader:
    def __init__(self, root: Path) -> None:
        self._root = root

    def load(self) -> RegistrySnapshot:
        vocabularies = {}
        objectives = {}
        templates = {}
        artifact_contracts = {}
        domain_packs = {}
        methodology_packs = {}
        quality_gates = {}

        for path in sorted((self._root / "vocabularies").glob("*.yaml")):
            raw = self._load_yaml(path)
            vocabulary = parse_vocabulary(raw, path)
            vocabularies[vocabulary.identifier] = vocabulary

        for path in sorted((self._root / "objectives").rglob("*.yaml")):
            raw = self._load_yaml(path)
            objective = parse_objective(raw, path)
            objectives[objective.ref.as_string()] = objective

        for path in sorted((self._root / "tasks").rglob("*.yaml")):
            raw = self._load_yaml(path)
            template = parse_task_template(raw, path)
            templates[template.ref.as_string()] = template

        for path in sorted((self._root / "artifacts").rglob("*.yaml")):
            raw = self._load_yaml(path)
            contract = parse_artifact_contract(raw, path)
            artifact_contracts[contract.ref.as_string()] = contract

        for path in sorted((self._root / "domains").rglob("*.yaml")):
            raw = self._load_yaml(path)
            pack = parse_domain_pack(raw, path)
            domain_packs[pack.ref.as_string()] = pack

        methodologies_root = self._root / "methodologies"
        if methodologies_root.exists():
            for path in sorted(methodologies_root.rglob("*.yaml")):
                raw = self._load_yaml(path)
                methodology = parse_methodology_pack(raw, path)
                methodology_packs[methodology.ref.as_string()] = methodology

        for path in sorted((self._root / "gates").rglob("*.yaml")):
            raw = self._load_yaml(path)
            gate = parse_quality_gate(raw, path)
            quality_gates[gate.ref.as_string()] = gate

        return RegistrySnapshot(
            vocabularies=vocabularies,
            objectives=objectives,
            templates=templates,
            artifact_contracts=artifact_contracts,
            domain_packs=domain_packs,
            methodology_packs=methodology_packs,
            quality_gates=quality_gates,
        )

    def _load_yaml(self, path: Path) -> dict:
        with path.open("r", encoding="utf-8") as handle:
            data = yaml.safe_load(handle) or {}
        if not isinstance(data, dict):
            raise ValidationError(f"YAML document must be a mapping: {path}")
        kind = data.get("kind")
        if kind is None:
            raise ValidationError(f"Missing 'kind' field in {path}")
        return data
