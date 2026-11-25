#!/usr/bin/env python3
"""Convert a PlantUML class diagram into a JSON Schema document.

The PlantUML model is the input. An optional JSON configuration file supplies
the root class and explicit entity and relationship names. The generator parses
the small subset of PlantUML that the example model uses and emits a draft
2020-12 schema that describes the shape of the root entity. It deliberately
emits no validation keywords: value constraints belong to the API layer.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

SCHEMA_DIALECT = "https://json-schema.org/draft/2020-12/schema"

ROOT_ENTITY = "Library"

TYPE_MAP = {
    "string": {"type": "string"},
    "boolean": {"type": "boolean"},
    "integer": {"type": "integer"},
    "decimal": {"type": "number"},
    "date": {"type": "string", "format": "date"},
    "datetime": {"type": "string", "format": "date-time"},
}

# Built-in defaults for the example model. A configuration file overrides them;
# a class or relationship they do not cover falls back to a name derived from
# the class name or relationship label.
ENTITY_NAMES = {
    "LIBRARY": "Library",
    "MEMBER": "Member",
    "BOOK": "Book",
    "SHELF": "Shelf",
    "LOAN": "Loan",
    "FINE": "Fine",
}

RELATIONSHIP_NAMES = {
    ("LIBRARY", "SHELF"): "shelves",
    ("LIBRARY", "BOOK"): "books",
    ("SHELF", "BOOK"): "books",
    ("MEMBER", "LOAN"): "loans",
    ("BOOK", "LOAN"): "loans",
    ("LOAN", "FINE"): "fine",
}

CARDINALITIES = {"||", "o|", "o{"}
ATTRIBUTE_MARKERS = {"PK", "FK"}
IGNORED_LINES = {"left to right direction", "hide methods", "hide stereotypes"}

CLASS_PATTERN = re.compile(
    r"^class\s+([A-Za-z_][A-Za-z0-9_]*)(?:\s+<<\s*([^<>]+?)\s*>>)?\s*\{\s*$"
)
ATTRIBUTE_PATTERN = re.compile(
    r"^\*?\s*([A-Za-z_][A-Za-z0-9_]*)\s*:\s*([A-Za-z_][A-Za-z0-9_]*)"
    r"\s*(?:<<\s*([^<>]+?)\s*>>)?\s*$"
)
SEPARATOR_PATTERN = re.compile(r"-{2,}$")


class PumlError(Exception):
    """A problem with the input file or the model it describes."""

    def __init__(self, message: str, line: int) -> None:
        self.message = message
        self.line = line
        super().__init__(message)

    def __str__(self) -> str:
        return f"line {self.line}: {self.message}"


class ConfigError(Exception):
    """A problem with the configuration file or the mappings it declares."""


@dataclass(frozen=True)
class Attribute:
    name: str
    puml_type: str
    marker: str | None


@dataclass
class ClassDef:
    name: str
    line: int
    attributes: list[Attribute] = field(default_factory=list)


@dataclass(frozen=True)
class Relationship:
    parent: str
    child: str
    left_card: str
    right_card: str
    label: str
    line: int


@dataclass
class Model:
    classes: dict[str, ClassDef]
    relationships: list[Relationship]


@dataclass(frozen=True)
class Config:
    """Naming overrides read from an optional JSON configuration file.

    ``root_entity`` names the class, or the resolved entity name, that the
    top-level ``$ref`` points at. ``entity_names`` maps a class name to its
    ``$defs`` name. ``relationship_names`` maps a parent and child class pair
    to the property name that carries the reference.
    """

    root_entity: str | None = None
    entity_names: dict[str, str] = field(default_factory=dict)
    relationship_names: dict[tuple[str, str], str] = field(default_factory=dict)


CONFIG_KEYS = {"root_entity", "entity_names", "relationship_names"}
NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def parse_puml(text: str) -> Model:
    """Parse the supported PlantUML subset and return the model."""
    lines = text.splitlines()
    classes: dict[str, ClassDef] = {}
    relationships: list[Relationship] = []
    state = "preamble"
    current: ClassDef | None = None
    skinparam_line = 0
    skinparam_depth = 0
    ended = False

    for number, raw in enumerate(lines, start=1):
        line = raw.strip()

        if state == "preamble":
            if not line or line.startswith("'"):
                continue
            if line == "@startuml":
                state = "body"
                continue
            raise PumlError("missing @startuml before model content", number)

        if ended:
            if not line or line.startswith("'"):
                continue
            raise PumlError("content after @enduml", number)

        if state == "skinparam":
            if not line or line.startswith("'"):
                continue
            if line in {"@enduml", "@startuml"}:
                raise PumlError("skinparam block is not closed", skinparam_line)
            if CLASS_PATTERN.match(line) or "--" in line:
                raise PumlError("skinparam block is not closed", skinparam_line)
            skinparam_depth += line.count("{") - line.count("}")
            if skinparam_depth <= 0:
                state = "body"
            continue

        if state == "class":
            if not line or line.startswith("'"):
                continue
            if line == "}":
                assert current is not None
                classes[current.name] = current
                current = None
                state = "body"
                continue
            if line in {"@enduml", "@startuml"}:
                assert current is not None
                raise PumlError(
                    f"class body for '{current.name}' is not closed", current.line
                )
            if SEPARATOR_PATTERN.match(line):
                continue
            match = ATTRIBUTE_PATTERN.match(line)
            if match is None:
                raise PumlError("unsupported structural syntax", number)
            assert current is not None
            _add_attribute(current, match, number)
            continue

        # state == "body"
        if not line or line.startswith("'"):
            continue
        if line == "@enduml":
            ended = True
            continue
        if line == "@startuml":
            raise PumlError("@startuml may appear only once", number)
        if line == "}":
            raise PumlError("unmatched closing brace", number)
        if line in IGNORED_LINES:
            continue
        if line == "skinparam" or line.startswith("skinparam "):
            if "{" in line:
                depth = line.count("{") - line.count("}")
                if depth > 0:
                    state = "skinparam"
                    skinparam_line = number
                    skinparam_depth = depth
            continue
        match = CLASS_PATTERN.match(line)
        if match is not None:
            name = match.group(1)
            if name in classes:
                raise PumlError(f"duplicate class '{name}'", number)
            current = ClassDef(name=name, line=number)
            state = "class"
            continue
        if "--" in line:
            relationships.append(_parse_relationship(line, number))
            continue
        raise PumlError("unsupported structural syntax", number)

    if state == "class":
        assert current is not None
        raise PumlError(f"class body for '{current.name}' is not closed", current.line)
    if state == "skinparam":
        raise PumlError("skinparam block is not closed", skinparam_line)
    if state == "preamble":
        raise PumlError("missing @startuml", 1)
    if not ended:
        raise PumlError("missing @enduml", max(len(lines), 1))

    _validate_endpoints(classes, relationships)
    _check_containment_cycles(classes, relationships)
    return Model(classes=classes, relationships=relationships)


def _add_attribute(definition: ClassDef, match: re.Match[str], number: int) -> None:
    name, puml_type, marker = match.group(1), match.group(2), match.group(3)
    if puml_type not in TYPE_MAP:
        raise PumlError(
            f"unknown attribute type '{puml_type}' for attribute '{name}'", number
        )
    if marker is not None and marker not in ATTRIBUTE_MARKERS:
        raise PumlError(f"unsupported attribute marker '{marker}'", number)
    if any(attribute.name == name for attribute in definition.attributes):
        raise PumlError(
            f"duplicate attribute '{name}' in class '{definition.name}'", number
        )
    definition.attributes.append(Attribute(name=name, puml_type=puml_type, marker=marker))


def _parse_relationship(line: str, number: int) -> Relationship:
    left, _separator, right = line.partition("--")
    left_tokens = left.split()
    right_tokens = right.split()
    if len(left_tokens) != 2 or len(right_tokens) < 2:
        raise PumlError("unsupported structural syntax", number)

    parent, left_card = left_tokens
    right_card, child = right_tokens[0], right_tokens[1]
    if len(right_tokens) < 4 or right_tokens[2] != ":":
        raise PumlError("unsupported structural syntax", number)
    label = " ".join(right_tokens[3:])

    for card in (left_card, right_card):
        if card not in CARDINALITIES:
            raise PumlError(f"unsupported cardinality '{card}'", number)

    return Relationship(
        parent=parent,
        child=child,
        left_card=left_card,
        right_card=right_card,
        label=label,
        line=number,
    )


def _validate_endpoints(
    classes: dict[str, ClassDef], relationships: list[Relationship]
) -> None:
    for relationship in relationships:
        for endpoint in (relationship.parent, relationship.child):
            if endpoint not in classes:
                raise PumlError(
                    f"relationship endpoint '{endpoint}' is not a declared class",
                    relationship.line,
                )


def _check_containment_cycles(
    classes: dict[str, ClassDef], relationships: list[Relationship]
) -> None:
    children: dict[str, list[Relationship]] = {name: [] for name in classes}
    for relationship in relationships:
        children[relationship.parent].append(relationship)

    white, grey, black = 0, 1, 2
    colour = {name: white for name in classes}
    path: list[str] = []

    def visit(name: str) -> None:
        colour[name] = grey
        path.append(name)
        for relationship in children[name]:
            child = relationship.child
            if colour[child] == grey:
                start = path.index(child)
                cycle = path[start:] + [child]
                raise PumlError(
                    "containment cycle: " + " -> ".join(cycle), relationship.line
                )
            if colour[child] == white:
                visit(child)
        path.pop()
        colour[name] = black

    for name in sorted(children):
        if colour[name] == white:
            visit(name)


def build_schema(model: Model, config: Config | None = None) -> dict:
    """Turn the parsed model into a JSON Schema document.

    ``config`` supplies naming overrides. Without it the generator keeps the
    built-in names for the example model and derives a name deterministically
    for any other class or relationship.
    """
    if config is not None:
        _validate_config(model, config)

    entity_names = _resolve_entity_names(model, config)
    root_entity = _resolve_root(model, config, entity_names)

    relationships_by_parent: dict[str, list[Relationship]] = {}
    for relationship in model.relationships:
        relationships_by_parent.setdefault(relationship.parent, []).append(relationship)

    definitions: dict[str, dict] = {}
    for definition in sorted(model.classes.values(), key=lambda item: entity_names[item.name]):
        properties: dict[str, dict] = {}
        origins: dict[str, str] = {}
        for attribute in definition.attributes:
            property_name = _lower_first(attribute.name)
            origins[property_name] = f"attribute '{attribute.name}'"
            properties[property_name] = dict(TYPE_MAP[attribute.puml_type])

        required: list[str] = []
        for relationship in relationships_by_parent.get(definition.name, []):
            property_name, from_config = _relationship_name(relationship, config)
            origin = (
                f"relationship '{relationship.parent} -> {relationship.child}'"
            )
            if property_name in origins:
                message = (
                    f"property '{property_name}' in class '{definition.name}' "
                    f"is produced by both {origins[property_name]} and {origin}"
                )
                if from_config:
                    raise ConfigError(
                        f"{message}; change relationship_names to a distinct name"
                    )
                raise PumlError(
                    f"{message}; set relationship_names to a distinct name",
                    relationship.line,
                )
            origins[property_name] = origin

            child_entity = entity_names[relationship.child]
            if relationship.right_card == "o{":
                properties[property_name] = {
                    "type": "array",
                    "items": {"$ref": f"#/$defs/{child_entity}"},
                }
            else:
                properties[property_name] = {"$ref": f"#/$defs/{child_entity}"}
            if relationship.left_card == "||" and relationship.right_card == "||":
                required.append(property_name)

        entry: dict = {"type": "object", "properties": properties}
        if required:
            entry["required"] = sorted(required)
        definitions[entity_names[definition.name]] = entry

    return {
        "$schema": SCHEMA_DIALECT,
        "$ref": f"#/$defs/{root_entity}",
        "$defs": definitions,
    }


def read_config(path: Path) -> Config:
    """Read a configuration file, reporting I/O problems as ConfigError."""
    try:
        data = path.read_bytes()
    except OSError as error:
        raise ConfigError(error.strerror or str(error))
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ConfigError(f"not valid UTF-8 (byte offset {error.start})")
    return parse_config(text)


def parse_config(text: str) -> Config:
    """Parse and shape-check a JSON configuration document."""
    try:
        raw = json.loads(text, object_pairs_hook=_unique_keys)
    except json.JSONDecodeError as error:
        raise ConfigError(
            f"invalid JSON at line {error.lineno} column {error.colno}: {error.msg}"
        )
    if not isinstance(raw, dict):
        raise ConfigError("configuration must be a JSON object")

    unknown = sorted(set(raw) - CONFIG_KEYS)
    if unknown:
        raise ConfigError(f"unknown configuration key '{unknown[0]}'")

    root_entity = None
    if "root_entity" in raw:
        root_entity = _config_name(raw["root_entity"], "root_entity")

    raw_entities = raw.get("entity_names", {})
    if not isinstance(raw_entities, dict):
        raise ConfigError("entity_names must be a JSON object")
    entity_names: dict[str, str] = {}
    for class_name, entity_name in raw_entities.items():
        _config_name(class_name, f"entity_names key '{class_name}'")
        entity_names[class_name] = _config_name(
            entity_name, f"entity_names['{class_name}']"
        )

    raw_relationships = raw.get("relationship_names", {})
    if not isinstance(raw_relationships, dict):
        raise ConfigError("relationship_names must be a JSON object")
    relationship_names: dict[tuple[str, str], str] = {}
    for parent, children in raw_relationships.items():
        if not isinstance(children, dict):
            raise ConfigError(f"relationship_names['{parent}'] must be a JSON object")
        for child, property_name in children.items():
            relationship_names[(parent, child)] = _config_name(
                property_name, f"relationship_names['{parent}']['{child}']"
            )

    return Config(
        root_entity=root_entity,
        entity_names=entity_names,
        relationship_names=relationship_names,
    )


def _unique_keys(pairs: list[tuple[str, object]]) -> dict:
    result: dict = {}
    for key, value in pairs:
        if key in result:
            raise ConfigError(f"duplicate key '{key}' in configuration")
        result[key] = value
    return result


def _config_name(value: object, where: str) -> str:
    if not isinstance(value, str) or not NAME_PATTERN.match(value):
        shown = repr(value) if not isinstance(value, str) else f"'{value}'"
        raise ConfigError(
            f"{where} must be an identifier (letters, digits and underscores, "
            f"not starting with a digit), got {shown}"
        )
    return value


def _validate_config(model: Model, config: Config) -> None:
    declared = set(model.classes)
    for class_name in config.entity_names:
        if class_name not in declared:
            raise ConfigError(
                f"entity_names refers to unknown class '{class_name}'; "
                f"the model declares: {_class_list(model)}"
            )

    pairs = {
        (relationship.parent, relationship.child)
        for relationship in model.relationships
    }
    for parent, child in config.relationship_names:
        for endpoint in (parent, child):
            if endpoint not in declared:
                raise ConfigError(
                    f"relationship_names refers to unknown class '{endpoint}'; "
                    f"the model declares: {_class_list(model)}"
                )
        if (parent, child) not in pairs:
            raise ConfigError(
                f"relationship_names['{parent}']['{child}'] matches no relationship; "
                f"the model declares no relationship from '{parent}' to '{child}'"
            )


def _class_list(model: Model) -> str:
    return ", ".join(sorted(model.classes)) or "no classes"


def _resolve_entity_names(
    model: Model, config: Config | None
) -> dict[str, str]:
    entity_names: dict[str, str] = {}
    owner: dict[str, tuple[str, bool]] = {}
    for name, definition in model.classes.items():
        if config is not None and name in config.entity_names:
            entity = config.entity_names[name]
            from_config = True
        else:
            from_config = False
            if name in ENTITY_NAMES:
                entity = ENTITY_NAMES[name]
            else:
                entity = _default_entity_name(name)
                if not NAME_PATTERN.match(entity):
                    raise PumlError(
                        f"class '{name}' does not yield a valid entity name; "
                        f"set entity_names['{name}'] to choose one",
                        definition.line,
                    )

        if entity in owner:
            other, other_from_config = owner[entity]
            message = (
                f"classes '{other}' and '{name}' both map to entity "
                f"name '{entity}'; give them distinct names in entity_names"
            )
            if from_config or other_from_config:
                raise ConfigError(message)
            raise PumlError(message, definition.line)

        owner[entity] = (name, from_config)
        entity_names[name] = entity

    return entity_names


def _resolve_root(
    model: Model, config: Config | None, entity_names: dict[str, str]
) -> str:
    if config is not None and config.root_entity is not None:
        if config.root_entity in model.classes:
            return entity_names[config.root_entity]
        if config.root_entity in entity_names.values():
            return config.root_entity
        raise ConfigError(
            f"root_entity '{config.root_entity}' is not a class or entity name "
            f"in the model; the model declares: {_class_list(model)}"
        )

    # The example model's root is the historical default. A model without a
    # LIBRARY class keeps the legacy root, which the README lists as a known
    # limitation.
    if "LIBRARY" in model.classes:
        return entity_names["LIBRARY"]
    return ROOT_ENTITY


def _default_entity_name(class_name: str) -> str:
    """Derive a PascalCase entity name from a class name."""
    parts = [part for part in re.split(r"[^A-Za-z0-9]+", class_name) if part]
    name = ""
    for part in parts:
        if part.isupper():
            name += part[:1] + part[1:].lower()
        else:
            name += part[:1].upper() + part[1:]
    return name


def _default_relationship_name(relationship: Relationship) -> str:
    """Derive a lowerCamelCase property name from a relationship label."""
    parts = [
        part for part in re.split(r"[^A-Za-z0-9]+", relationship.label) if part
    ]
    if parts:
        name = parts[0][:1].lower() + parts[0][1:]
        for part in parts[1:]:
            name += part[:1].upper() + part[1:]
        if NAME_PATTERN.match(name):
            return name

    raise PumlError(
        f"relationship label '{relationship.label}' does not yield a valid "
        f"property name; set relationship_names for "
        f"'{relationship.parent} -> {relationship.child}'",
        relationship.line,
    )


def _relationship_name(
    relationship: Relationship, config: Config | None
) -> tuple[str, bool]:
    """Return the property name for a relationship and whether it was configured."""
    key = (relationship.parent, relationship.child)
    if config is not None and key in config.relationship_names:
        return config.relationship_names[key], True
    if key in RELATIONSHIP_NAMES:
        return RELATIONSHIP_NAMES[key], False
    return _default_relationship_name(relationship), False


def _lower_first(name: str) -> str:
    return name[:1].lower() + name[1:]


def render(document: dict) -> str:
    """Serialise the document deterministically."""
    return json.dumps(document, indent=2, ensure_ascii=False) + "\n"


def _same_path(first: Path, second: Path) -> bool:
    return os.path.normcase(os.path.realpath(first)) == os.path.normcase(
        os.path.realpath(second)
    )


def _fail(message: str) -> int:
    sys.stderr.write(message + "\n")
    return 2


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        sys.stderr.write(f"{self.prog}: {message}\n")
        raise SystemExit(2)


def main(argv: list[str] | None = None) -> int:
    parser = _ArgumentParser(
        prog="puml_to_json_schema.py",
        description="Generate a JSON Schema from a PlantUML class diagram.",
    )
    parser.add_argument("input", help="PlantUML model to read")
    parser.add_argument("--output", required=True, help="JSON Schema file to write")
    parser.add_argument(
        "--config",
        help="optional JSON file with root, entity and relationship name overrides",
    )
    args = parser.parse_args(argv)

    input_path = Path(args.input)
    output_path = Path(args.output)

    config: Config | None = None
    if args.config is not None:
        try:
            config = read_config(Path(args.config))
        except ConfigError as error:
            return _fail(f"{args.config}: {error}")

    try:
        if _same_path(input_path, output_path):
            return _fail(f"refusing to overwrite the input file: {args.output}")
        data = input_path.read_bytes()
    except OSError as error:
        return _fail(f"{args.input}: {error.strerror or error}")

    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as error:
        return _fail(f"{args.input}: not valid UTF-8 (byte offset {error.start})")

    try:
        document = build_schema(parse_puml(text), config)
    except PumlError as error:
        return _fail(f"{args.input}: line {error.line}: {error.message}")
    except ConfigError as error:
        return _fail(f"{args.config}: {error}")

    try:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(render(document), encoding="utf-8", newline="\n")
    except OSError as error:
        return _fail(f"{args.output}: {error.strerror or error}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
