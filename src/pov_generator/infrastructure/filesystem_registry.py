from __future__ import annotations

from pathlib import Path

import yaml

from ..common.errors import ValidationError
from ..domain.registry import (
    RegistrySnapshot,
    parse_domain_pack,
    parse_recipe,
    parse_recipe_fragment,
    parse_template,
    parse_vocabulary,
)


class FilesystemRegistryLoader:
    def __init__(self, root: Path) -> None:
        self._root = root

    def load(self) -> RegistrySnapshot:
        vocabularies = {}
        templates = {}
        recipes = {}
        recipe_fragments = {}
        domain_packs = {}

        for path in sorted((self._root / "vocabularies").glob("*.yaml")):
            raw = self._load_yaml(path)
            vocabulary = parse_vocabulary(raw, path)
            vocabularies[vocabulary.identifier] = vocabulary

        for path in sorted((self._root / "templates").rglob("*.yaml")):
            raw = self._load_yaml(path)
            template = parse_template(raw, path)
            templates[template.ref.as_string()] = template

        for path in sorted((self._root / "recipes").rglob("*.yaml")):
            raw = self._load_yaml(path)
            recipe = parse_recipe(raw, path)
            recipes[recipe.ref.as_string()] = recipe

        for path in sorted((self._root / "recipe_fragments").rglob("*.yaml")):
            raw = self._load_yaml(path)
            fragment = parse_recipe_fragment(raw, path)
            recipe_fragments[fragment.ref.as_string()] = fragment

        for path in sorted((self._root / "domain_packs").rglob("*.yaml")):
            raw = self._load_yaml(path)
            pack = parse_domain_pack(raw, path)
            domain_packs[pack.ref.as_string()] = pack

        return RegistrySnapshot(
            vocabularies=vocabularies,
            templates=templates,
            recipes=recipes,
            recipe_fragments=recipe_fragments,
            domain_packs=domain_packs,
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
