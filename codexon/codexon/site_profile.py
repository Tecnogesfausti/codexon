from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any

import yaml


class SiteProfileError(ValueError):
    pass


@dataclasses.dataclass(frozen=True)
class SiteProfile:
    path: Path | None
    roles: dict[str, dict[str, Any]]
    instructions: tuple[str, ...] = ()

    @classmethod
    def empty(cls) -> "SiteProfile":
        return cls(path=None, roles={})

    @classmethod
    def load(cls, path: Path) -> "SiteProfile":
        if not path.exists():
            return cls(path=path, roles={})
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        if data is None:
            return cls(path=path, roles={})
        if not isinstance(data, dict):
            raise SiteProfileError("El perfil debe ser un objeto YAML.")
        if data.get("version") != 1:
            raise SiteProfileError("El perfil debe declarar version: 1.")

        raw_roles = data.get("roles") or {}
        if not isinstance(raw_roles, dict):
            raise SiteProfileError("roles debe ser un objeto.")
        roles: dict[str, dict[str, Any]] = {}
        for name, raw_binding in raw_roles.items():
            if not isinstance(name, str) or not name.strip():
                raise SiteProfileError("Cada role debe tener un nombre no vacio.")
            if isinstance(raw_binding, str):
                binding: dict[str, Any] = {"entity_id": raw_binding}
            elif isinstance(raw_binding, dict):
                binding = dict(raw_binding)
            else:
                raise SiteProfileError(f"El role {name!r} debe ser texto u objeto.")
            cls._validate_binding(name, binding)
            roles[name.strip()] = binding

        raw_instructions = data.get("instructions") or []
        if not isinstance(raw_instructions, list) or not all(isinstance(item, str) for item in raw_instructions):
            raise SiteProfileError("instructions debe ser una lista de textos.")
        instructions = tuple(item.strip() for item in raw_instructions if item.strip())
        return cls(path=path, roles=roles, instructions=instructions)

    @staticmethod
    def _validate_binding(name: str, binding: dict[str, Any]) -> None:
        entity_id = binding.get("entity_id")
        entities = binding.get("entities")
        aliases = binding.get("aliases", [])
        if entity_id is not None and (not isinstance(entity_id, str) or "." not in entity_id):
            raise SiteProfileError(f"entity_id invalido en {name!r}.")
        if entities is not None and (
            not isinstance(entities, list)
            or not all(isinstance(item, str) and "." in item for item in entities)
        ):
            raise SiteProfileError(f"entities invalido en {name!r}.")
        if aliases is not None and (
            not isinstance(aliases, list) or not all(isinstance(item, str) for item in aliases)
        ):
            raise SiteProfileError(f"aliases invalido en {name!r}.")
        if entity_id is None and not entities:
            raise SiteProfileError(f"El role {name!r} necesita entity_id o entities.")

    def binding(self, role: str) -> dict[str, Any] | None:
        value = self.roles.get(role)
        return dict(value) if value else None

    def entity(self, role: str, default: str | None = None) -> str | None:
        binding = self.roles.get(role) or {}
        entity_id = binding.get("entity_id")
        if isinstance(entity_id, str) and entity_id:
            return entity_id
        entities = binding.get("entities") or []
        return str(entities[0]) if entities else default

    def entities(self, role: str, default: tuple[str, ...] = ()) -> tuple[str, ...]:
        binding = self.roles.get(role) or {}
        values = binding.get("entities")
        if isinstance(values, list) and values:
            return tuple(str(item) for item in values)
        entity_id = binding.get("entity_id")
        return (str(entity_id),) if entity_id else default

    def role_for_entity(self, entity_id: str, role_prefix: str = "") -> tuple[str, dict[str, Any]] | None:
        return next(
            (
                (role, dict(binding))
                for role, binding in self.roles.items()
                if role.startswith(role_prefix) and entity_id in self.entities(role)
            ),
            None,
        )

    def resolve_alias(self, text: str, role_prefix: str = "") -> tuple[str, str] | None:
        folded = _fold(text)
        candidates: list[tuple[int, str, str]] = []
        for role, binding in self.roles.items():
            if role_prefix and not role.startswith(role_prefix):
                continue
            entity_id = self.entity(role)
            if not entity_id:
                continue
            label = str(binding.get("label") or role)
            for alias in binding.get("aliases") or []:
                alias_text = str(alias).strip()
                if alias_text and _fold(alias_text) in folded:
                    candidates.append((len(alias_text), entity_id, label))
        if not candidates:
            return None
        _, entity_id, label = max(candidates, key=lambda item: item[0])
        return entity_id, label

    def search_bindings(
        self,
        query: str = "",
        *,
        role_prefix: str = "",
        kind: str = "",
    ) -> list[tuple[str, dict[str, Any]]]:
        terms = _search_terms(query)
        wanted_kind = _fold(kind).strip()
        matches: list[tuple[str, dict[str, Any]]] = []
        for role, binding in self.roles.items():
            if role_prefix and not role.startswith(role_prefix):
                continue
            binding_kind = _fold(str(binding.get("kind") or "")).strip()
            if wanted_kind and binding_kind != wanted_kind:
                continue
            haystack = _fold(
                " ".join(
                    (
                        role,
                        str(binding.get("label") or ""),
                        " ".join(str(item) for item in binding.get("aliases") or []),
                        " ".join(str(item) for item in binding.get("tags") or []),
                        str(binding.get("area") or ""),
                        binding_kind,
                    )
                )
            )
            haystack_terms = set(_search_terms(haystack))
            if terms and not all(term in haystack_terms for term in terms):
                continue
            matches.append((role, dict(binding)))
        return matches

    def prompt_context(self) -> str:
        if not self.roles and not self.instructions:
            return (
                "Perfil local: no configurado. Descubre entidades por nombre, area, dominio y "
                "device_class; pide confirmacion si hay mas de una candidata."
            )
        role_lines = []
        for role, binding in sorted(self.roles.items()):
            entities = ", ".join(self.entities(role))
            label = str(binding.get("label") or role)
            aliases = ", ".join(str(item) for item in binding.get("aliases") or [])
            suffix = f"; aliases={aliases}" if aliases else ""
            role_lines.append(f"- {role}: {entities} ({label}){suffix}")
        instruction_lines = [f"- {instruction}" for instruction in self.instructions]
        return "\n".join(
            ["Perfil local activo:", *role_lines, "Reglas locales:", *instruction_lines]
        )


def _fold(value: str) -> str:
    import unicodedata

    normalized = unicodedata.normalize("NFKD", value)
    return "".join(char for char in normalized if not unicodedata.combining(char)).casefold()


def _search_terms(value: str) -> list[str]:
    ignored = {
        "de",
        "del",
        "el",
        "en",
        "la",
        "las",
        "lista",
        "listar",
        "listado",
        "los",
        "me",
        "puedo",
        "que",
        "todos",
        "todas",
        "un",
        "una",
        "y",
    }
    terms: list[str] = []
    for token in __import__("re").split(r"[^a-z0-9]+", _fold(value)):
        if not token or token in ignored:
            continue
        if len(token) > 4 and token.endswith("es"):
            token = token[:-2]
        elif len(token) > 3 and token.endswith("s"):
            token = token[:-1]
        terms.append(token)
    return terms
