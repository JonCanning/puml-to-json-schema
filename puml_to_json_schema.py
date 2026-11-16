#!/usr/bin/env python3
"""Convert a PlantUML class diagram into a JSON Schema document.

The PlantUML model is the only input. The generator parses the small subset of
PlantUML that the example model uses and emits a draft 2020-12 schema that
describes the shape of the root entity. It deliberately emits no validation
keywords: value constraints belong to the API layer.
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


def build_schema(model: Model) -> dict:
    """Turn the parsed model into a JSON Schema document."""
    entity_names = {
        name: _entity_name(name, definition.line)
        for name, definition in model.classes.items()
    }

    relationships_by_parent: dict[str, list[Relationship]] = {}
    for relationship in model.relationships:
        relationships_by_parent.setdefault(relationship.parent, []).append(relationship)

    definitions: dict[str, dict] = {}
    for definition in sorted(model.classes.values(), key=lambda item: entity_names[item.name]):
        properties: dict[str, dict] = {}
        for attribute in definition.attributes:
            properties[_lower_first(attribute.name)] = dict(TYPE_MAP[attribute.puml_type])

        required: list[str] = []
        for relationship in relationships_by_parent.get(definition.name, []):
            property_name = _relationship_name(relationship)
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
        "$ref": f"#/$defs/{ROOT_ENTITY}",
        "$defs": definitions,
    }


def _entity_name(class_name: str, line: int) -> str:
    try:
        return ENTITY_NAMES[class_name]
    except KeyError:
        raise PumlError(f"no entity name defined for class '{class_name}'", line)


def _relationship_name(relationship: Relationship) -> str:
    key = (relationship.parent, relationship.child)
    try:
        return RELATIONSHIP_NAMES[key]
    except KeyError:
        raise PumlError(
            f"no relationship property name defined for "
            f"'{relationship.parent} -> {relationship.child}'",
            relationship.line,
        )


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
    args = parser.parse_args(argv)

    input_path = Path(args.input)
    output_path = Path(args.output)

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
        document = build_schema(parse_puml(text))
    except PumlError as error:
        return _fail(f"{args.input}: line {error.line}: {error.message}")

    try:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(render(document), encoding="utf-8", newline="\n")
    except OSError as error:
        return _fail(f"{args.output}: {error.strerror or error}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
