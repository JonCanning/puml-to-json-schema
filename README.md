# PlantUML to JSON Schema

`puml_to_json_schema.py` reads a PlantUML class diagram and writes a draft
2020-12 JSON Schema that describes the shape of the root entity. The PlantUML
model is the only input. The generator emits structure only: property names,
property types, and which relationships are required. It emits no value
constraints, because those belong to the layer that consumes the schema.

`example-model.puml` is a fictional library-lending model. It exists so the
tests and the golden fixture carry no real data.

## Requirements

Python 3.10 or later. The generator uses the standard library only, so it has no
dependencies to install. One test needs the `jsonschema` package and skips when
it is absent.

## Generate the schema

```powershell
python puml_to_json_schema.py example-model.puml --output generated/example.schema.json
```

The command prints nothing on success and exits 0. It creates the output
directory when that directory does not exist.

## The document

The top level holds `$schema`, a `$ref` to the root entity, and `$defs`. `$defs`
holds one entry per model class, sorted by name. Every entry is an object whose
properties follow the attribute order in the model.

The excerpt below is condensed. The real file indents by two spaces and puts
each keyword on its own line.

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$ref": "#/$defs/Library",
  "$defs": {
    "Library": {
      "type": "object",
      "properties": {
        "libraryId": { "type": "string" },
        "openedOn": { "type": "string", "format": "date" },
        "isOpen": { "type": "boolean" },
        "shelves": { "type": "array", "items": { "$ref": "#/$defs/Shelf" } },
        "books": { "type": "array", "items": { "$ref": "#/$defs/Book" } }
      }
    },
    "Loan": {
      "type": "object",
      "properties": {
        "borrowedAt": { "type": "string", "format": "date-time" },
        "fine": { "$ref": "#/$defs/Fine" }
      },
      "required": ["fine"]
    }
  }
}
```

A relationship that ends in `o{` becomes an array of `$ref`. A relationship that
ends in `o|` or `||` becomes a single `$ref`. A property appears in `required`
only when both ends of the relationship are `||`, so `required` lists the
relationships the model marks as mandatory and nothing else.

Types map as follows:

| PlantUML   | JSON Schema                                 |
| ---------- | ------------------------------------------- |
| `string`   | `{"type": "string"}`                        |
| `boolean`  | `{"type": "boolean"}`                       |
| `integer`  | `{"type": "integer"}`                       |
| `decimal`  | `{"type": "number"}`                        |
| `date`     | `{"type": "string", "format": "date"}`      |
| `datetime` | `{"type": "string", "format": "date-time"}` |

The document contains no other keywords. There is no `pattern`, `enum`,
`minLength`, `default`, `description`, or `additionalProperties`. The emitted
keywords are `$schema`, `$ref`, `$defs`, `type`, `properties`, `items`,
`format`, and `required`.

## The supported PlantUML subset

The parser accepts the following and rejects everything else with a line number:

- `@startuml` and `@enduml`, each exactly once, with nothing after `@enduml`
- comments that start with `'`
- the styling lines `left to right direction`, `hide methods`, and `hide stereotypes`
- `skinparam` lines, and `skinparam` blocks closed with `}`
- `class NAME {` or `class NAME <<Stereotype>> {`, followed by one attribute per line and a closing `}`
- attributes of the form `name : type`, with an optional leading `*` and an optional `<<PK>>` or `<<FK>>` marker
- relationships of the form `PARENT ||--o{ CHILD : label`, where each cardinality is `||`, `o|`, or `o{`

A class body must span several lines. `class LOAN { loanId : string }` on one
line is rejected, because the parser only recognises a class line that ends with
the opening brace.

## Extending the model

Three tables at the top of the script hold the naming and type rules:
`TYPE_MAP`, `ENTITY_NAMES`, and `RELATIONSHIP_NAMES`. `ENTITY_NAMES` maps a
PlantUML class name to the name used in `$defs`. `RELATIONSHIP_NAMES` maps a
parent and child pair to the property name that carries the reference.

A class or relationship without an entry is an error. The message names the
class, or the parent and child pair, and reports the line where it was declared.
This keeps a new class from slipping into the output under a guessed name.

`ROOT_ENTITY` names the entity that the top-level `$ref` points at. Change it
when a different class becomes the entry point.

`PK` and `FK` markers are accepted and then ignored. They do not change the
emitted property, and they do not add or remove anything from `required`.

## Command line contract

`--output` is required. The generator refuses an output path that resolves to
the input file, including a path that differs only in letter case.

Every failure prints one line to stderr and exits 2. The forms are:

- `path: line N: message` for a problem in the model
- `path: message` for an unreadable input, invalid UTF-8, or an unwritable output
- `puml_to_json_schema.py: message` for a missing or invalid argument

A failure never prints a traceback.

## Determinism

The output is byte-identical across runs and across `PYTHONHASHSEED` values.
`$defs` and `required` are sorted, attribute order comes from the model, and the
file is written as UTF-8 with LF line endings and two-space indentation.

`tests/fixtures/example.schema.golden.json` pins that output. The repository sets
`* -text` in `.gitattributes` so Git does not convert line endings on checkout,
which would otherwise break the byte comparison on Windows.

## Verify

```powershell
python -m unittest -v
```

The suite runs 55 tests. It covers the parser errors, the emission shapes, the
example model against the golden fixture, the command line contract, and the
determinism checks.

## Layout

```
example-model.puml                 the input model
puml_to_json_schema.py             the generator
generated/example.schema.json      the generated schema
tests/fixtures/                    the golden schema and the parser fixtures
test_puml_to_json_schema.py        the tests
```

## Known limitations

These are defects in the current generator rather than intended behaviour:

- A relationship declared twice produces a duplicate entry in `required`, which fails the 2020-12 meta-schema.
- A left-hand `o{` is validated and then ignored, so write relationships from the parent to the child.
- A model whose root entity is missing emits a top-level `$ref` that cannot resolve.
- An output path that is a hardlink to the input is not recognised as the same file, so the input is overwritten.
- An attribute and a relationship that resolve to the same property name collide, and the relationship silently wins.
