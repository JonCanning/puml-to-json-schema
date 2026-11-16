"""Tests for the PlantUML to JSON Schema generator.

The tests are organised by the four things the generator must get right:
parsing (including every required error), emission shape, the full library
model, and the command line contract.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import puml_to_json_schema as generator
from puml_to_json_schema import PumlError, build_schema, parse_puml

ROOT = Path(__file__).resolve().parent
MODEL_PATH = ROOT / "example-model.puml"
FIXTURES = ROOT / "tests" / "fixtures"
GOLDEN_PATH = FIXTURES / "example.schema.golden.json"
SCRIPT = ROOT / "puml_to_json_schema.py"

EXPECTED_ENTITIES = {
    "Book",
    "Fine",
    "Library",
    "Loan",
    "Member",
    "Shelf",
}

# Attribute counts read straight from example-model.puml. Relationship
# properties are counted separately, so these stay stable when the
# relationship table changes.
EXPECTED_ATTRIBUTE_COUNTS = {
    "Library": 4,
    "Member": 5,
    "Book": 5,
    "Shelf": 3,
    "Loan": 7,
    "Fine": 5,
}

FORBIDDEN_KEYWORDS = {
    "nullable",
    "enum",
    "minLength",
    "maxLength",
    "pattern",
    "minimum",
    "maximum",
    "multipleOf",
    "description",
    "example",
    "default",
    "additionalProperties",
}


def parse_text(text):
    return parse_puml(text)


def schema_for(text):
    return build_schema(parse_text(text))


def read_fixture(name):
    return (FIXTURES / name).read_text(encoding="utf-8")


def iter_dicts(node):
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from iter_dicts(value)
    elif isinstance(node, list):
        for value in node:
            yield from iter_dicts(value)


def run_cli(*args, env=None, cwd=None):
    command = [sys.executable, str(SCRIPT), *args]
    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        env=env,
        cwd=cwd,
    )


class ParserTests(unittest.TestCase):
    def test_parses_class_attributes_markers_and_relationships(self):
        model = parse_text(read_fixture("schema-minimal.puml"))

        self.assertEqual(list(model.classes), ["LOAN", "FINE"])
        loan = model.classes["LOAN"]
        self.assertEqual([a.name for a in loan.attributes], ["loanId", "dueAt"])
        self.assertEqual(loan.attributes[0].marker, "PK")
        self.assertIsNone(loan.attributes[1].marker)
        self.assertEqual(loan.attributes[0].puml_type, "string")

        self.assertEqual(len(model.relationships), 1)
        relationship = model.relationships[0]
        self.assertEqual(relationship.parent, "LOAN")
        self.assertEqual(relationship.child, "FINE")
        self.assertEqual(relationship.left_card, "||")
        self.assertEqual(relationship.right_card, "||")

    def test_accepts_styling_directives_from_supplied_model(self):
        model = parse_text(MODEL_PATH.read_text(encoding="utf-8"))

        self.assertEqual(len(model.classes), 6)
        self.assertEqual(len(model.relationships), 6)

    def test_unmatched_closing_brace_reports_its_line(self):
        with self.assertRaises(PumlError) as caught:
            parse_text(read_fixture("schema-unmatched-brace.puml"))

        self.assertEqual(caught.exception.line, 4)
        self.assertIn("unmatched closing brace", str(caught.exception))

    def test_unknown_structural_syntax_is_an_error(self):
        with self.assertRaises(PumlError) as caught:
            parse_text(read_fixture("schema-unknown-syntax.puml"))

        self.assertEqual(caught.exception.line, 3)
        self.assertIn("unsupported structural syntax", str(caught.exception))

    def test_duplicate_class_is_an_error(self):
        with self.assertRaises(PumlError) as caught:
            parse_text(read_fixture("schema-duplicate-class.puml"))

        self.assertEqual(caught.exception.line, 6)
        self.assertIn("duplicate class 'LOAN'", str(caught.exception))

    def test_duplicate_attribute_is_an_error(self):
        with self.assertRaises(PumlError) as caught:
            parse_text(read_fixture("schema-duplicate-attribute.puml"))

        self.assertEqual(caught.exception.line, 4)
        self.assertIn("duplicate attribute 'dueAt'", str(caught.exception))

    def test_relationship_to_undeclared_class_is_an_error(self):
        with self.assertRaises(PumlError) as caught:
            parse_text(read_fixture("schema-undeclared-endpoint.puml"))

        self.assertEqual(caught.exception.line, 5)
        self.assertIn("GHOST", str(caught.exception))
        self.assertIn("not a declared class", str(caught.exception))

    def test_invalid_cardinality_is_an_error(self):
        with self.assertRaises(PumlError) as caught:
            parse_text(read_fixture("schema-invalid-cardinality.puml"))

        self.assertEqual(caught.exception.line, 5)
        self.assertIn("unsupported cardinality", str(caught.exception))

    def test_containment_cycle_is_an_error(self):
        with self.assertRaises(PumlError) as caught:
            parse_text(read_fixture("schema-cycle.puml"))

        self.assertIn("containment cycle", str(caught.exception))
        self.assertIn("LOAN", str(caught.exception))
        self.assertIn("FINE", str(caught.exception))

    def test_unclosed_class_body_is_an_error(self):
        with self.assertRaises(PumlError) as caught:
            parse_text(read_fixture("schema-unclosed-class.puml"))

        self.assertEqual(caught.exception.line, 2)
        self.assertIn("not closed", str(caught.exception))

    def test_unclosed_skinparam_block_is_an_error(self):
        with self.assertRaises(PumlError) as caught:
            parse_text(read_fixture("schema-unclosed-skinparam.puml"))

        self.assertEqual(caught.exception.line, 2)
        self.assertIn("skinparam block", str(caught.exception))

    def test_skinparam_prefix_must_be_a_directive(self):
        text = "@startuml\nskinparamFoo bar\nclass LOAN {\n}\n@enduml\n"

        with self.assertRaises(PumlError) as caught:
            parse_text(text)

        self.assertEqual(caught.exception.line, 2)
        self.assertIn("unsupported structural syntax", str(caught.exception))

    def test_one_line_skinparam_block_is_ignored(self):
        text = (
            "@startuml\n"
            "skinparam class { BackgroundColor #FFFFFF }\n"
            "class LOAN {\n  a : string\n}\n"
            "@enduml\n"
        )

        model = parse_text(text)

        self.assertEqual(list(model.classes), ["LOAN"])

    def test_class_inside_an_unclosed_skinparam_block_is_an_error(self):
        text = (
            "@startuml\n"
            "skinparam class {\n"
            "  BackgroundColor #FFFFFF\n"
            "class LOAN {\n}\n"
            "@enduml\n"
        )

        with self.assertRaises(PumlError) as caught:
            parse_text(text)

        self.assertEqual(caught.exception.line, 2)
        self.assertIn("skinparam block", str(caught.exception))

    def test_relationship_without_a_label_is_an_error(self):
        text = (
            "@startuml\n"
            "class LOAN {\n}\n"
            "class FINE {\n}\n"
            "LOAN ||--|| FINE\n"
            "@enduml\n"
        )

        with self.assertRaises(PumlError) as caught:
            parse_text(text)

        self.assertEqual(caught.exception.line, 6)
        self.assertIn("unsupported structural syntax", str(caught.exception))

    def test_content_after_enduml_is_an_error(self):
        with self.assertRaises(PumlError) as caught:
            parse_text(read_fixture("schema-content-after-enduml.puml"))

        self.assertEqual(caught.exception.line, 3)
        self.assertIn("after @enduml", str(caught.exception))

    def test_missing_startuml_is_an_error(self):
        with self.assertRaises(PumlError) as caught:
            parse_text(read_fixture("schema-missing-startuml.puml"))

        self.assertEqual(caught.exception.line, 1)
        self.assertIn("@startuml", str(caught.exception))

    def test_missing_enduml_is_an_error(self):
        with self.assertRaises(PumlError) as caught:
            parse_text(read_fixture("schema-missing-enduml.puml"))

        self.assertIn("@enduml", str(caught.exception))

    def test_unknown_attribute_type_is_an_error(self):
        with self.assertRaises(PumlError) as caught:
            parse_text(read_fixture("schema-unknown-type.puml"))

        self.assertEqual(caught.exception.line, 3)
        self.assertIn("unknown attribute type 'money'", str(caught.exception))

    def test_unsupported_attribute_marker_is_an_error(self):
        with self.assertRaises(PumlError) as caught:
            parse_text(read_fixture("schema-unsupported-marker.puml"))

        self.assertEqual(caught.exception.line, 3)
        self.assertIn("unsupported attribute marker 'UK'", str(caught.exception))

    def test_unmapped_class_name_is_an_error(self):
        text = "@startuml\nclass UNKNOWN_ENTITY {\n  a : string\n}\n@enduml\n"
        model = parse_text(text)

        with self.assertRaises(PumlError) as caught:
            build_schema(model)

        self.assertIn("UNKNOWN_ENTITY", str(caught.exception))

    def test_unmapped_relationship_name_is_an_error(self):
        text = (
            "@startuml\n"
            "class LIBRARY {\n  a : string\n}\n"
            "class MEMBER {\n  b : string\n}\n"
            "LIBRARY ||--o| MEMBER : back\n"
            "@enduml\n"
        )

        with self.assertRaises(PumlError) as caught:
            build_schema(parse_text(text))

        self.assertIn("LIBRARY -> MEMBER", str(caught.exception))


class EmissionTests(unittest.TestCase):
    def test_type_mapping_handles_all_six_types(self):
        text = (
            "@startuml\n"
            "class LOAN {\n"
            "  s : string\n"
            "  b : boolean\n"
            "  i : integer\n"
            "  d : decimal\n"
            "  dt : date\n"
            "  ts : datetime\n"
            "}\n"
            "class FINE {\n  e : string\n}\n"
            "LOAN ||--o| FINE : incurs\n"
            "@enduml\n"
        )
        properties = schema_for(text)["$defs"]["Loan"]["properties"]

        self.assertEqual(properties["s"], {"type": "string"})
        self.assertEqual(properties["b"], {"type": "boolean"})
        self.assertEqual(properties["i"], {"type": "integer"})
        self.assertEqual(properties["d"], {"type": "number"})
        self.assertEqual(properties["dt"], {"type": "string", "format": "date"})
        self.assertEqual(properties["ts"], {"type": "string", "format": "date-time"})

    def test_to_many_relationship_is_an_array_of_ref(self):
        schema = schema_for(read_fixture("schema-minimal-relationships.puml"))
        library = schema["$defs"]["Library"]["properties"]

        self.assertEqual(
            library["shelves"],
            {"type": "array", "items": {"$ref": "#/$defs/Shelf"}},
        )

    def test_to_one_relationship_is_a_ref(self):
        schema = schema_for(read_fixture("schema-minimal-relationships.puml"))
        library = schema["$defs"]["Library"]["properties"]

        self.assertEqual(library["books"], {"$ref": "#/$defs/Book"})

    def test_both_ends_required_adds_one_required_entry(self):
        schema = schema_for(read_fixture("schema-minimal.puml"))

        self.assertEqual(schema["$defs"]["Loan"]["required"], ["fine"])
        self.assertNotIn("required", schema["$defs"]["Fine"])

    def test_required_is_absent_when_no_relationship_is_both_ends(self):
        schema = schema_for(read_fixture("schema-minimal-relationships.puml"))

        self.assertNotIn("required", schema["$defs"]["Library"])

    def test_required_entries_are_sorted(self):
        text = (
            "@startuml\n"
            "class LIBRARY {\n  a : string\n}\n"
            "class SHELF {\n  b : string\n}\n"
            "class BOOK {\n  c : string\n}\n"
            "LIBRARY ||--|| SHELF : has\n"
            "LIBRARY ||--|| BOOK : holds\n"
            "@enduml\n"
        )

        schema = schema_for(text)

        self.assertEqual(
            schema["$defs"]["Library"]["required"], ["books", "shelves"]
        )

    def test_output_has_no_validation_keywords(self):
        schema = schema_for(MODEL_PATH.read_text(encoding="utf-8"))

        for node in iter_dicts(schema):
            for keyword in FORBIDDEN_KEYWORDS:
                self.assertNotIn(keyword, node)

    def test_every_property_uses_a_permitted_shape(self):
        schema = schema_for(MODEL_PATH.read_text(encoding="utf-8"))

        for definition in schema["$defs"].values():
            for property_schema in definition["properties"].values():
                keys = set(property_schema)
                self.assertIn(
                    keys,
                    [
                        {"type"},
                        {"type", "format"},
                        {"$ref"},
                        {"type", "items"},
                    ],
                )

    def test_pk_and_fk_attributes_become_plain_properties(self):
        text = (
            "@startuml\n"
            "class LOAN {\n"
            "  * loanId : string <<PK>>\n"
            "  memberId : string <<FK>>\n"
            "}\n"
            "@enduml\n"
        )
        properties = schema_for(text)["$defs"]["Loan"]["properties"]

        self.assertEqual(set(properties), {"loanId", "memberId"})
        self.assertEqual(properties["loanId"], {"type": "string"})
        self.assertEqual(properties["memberId"], {"type": "string"})

    def test_defs_are_sorted_by_name(self):
        schema = schema_for(MODEL_PATH.read_text(encoding="utf-8"))

        names = list(schema["$defs"])
        self.assertEqual(names, sorted(names))

    def test_attribute_order_follows_the_model(self):
        text = (
            "@startuml\n"
            "class LOAN {\n"
            "  zebra : string\n"
            "  alpha : string\n"
            "  middle : string\n"
            "}\n"
            "@enduml\n"
        )
        schema = schema_for(text)

        self.assertEqual(
            list(schema["$defs"]["Loan"]["properties"]),
            ["zebra", "alpha", "middle"],
        )

    def test_first_character_is_lowercased_without_other_changes(self):
        text = (
            "@startuml\n"
            "class LIBRARY {\n"
            "  LibraryName : string\n"
            "  shelfCodeSector : string\n"
            "  bookIsbnCode : string\n"
            "  OpeningHour : string\n"
            "}\n"
            "@enduml\n"
        )
        schema = schema_for(text)

        self.assertEqual(
            list(schema["$defs"]["Library"]["properties"]),
            ["libraryName", "shelfCodeSector", "bookIsbnCode", "openingHour"],
        )


class FullModelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = MODEL_PATH.read_text(encoding="utf-8")
        cls.schema = schema_for(cls.text)

    def test_matches_golden_fixture(self):
        generated = generator.render(self.schema)
        self.assertEqual(
            generated.encode("utf-8"),
            GOLDEN_PATH.read_bytes(),
        )

    def test_top_level_document_is_a_library_ref(self):
        self.assertEqual(
            self.schema["$schema"],
            "https://json-schema.org/draft/2020-12/schema",
        )
        self.assertEqual(self.schema["$ref"], "#/$defs/Library")

    def test_all_six_entities_are_defined(self):
        self.assertEqual(set(self.schema["$defs"]), EXPECTED_ENTITIES)
        self.assertEqual(len(self.schema["$defs"]), 6)

    def test_loan_required_is_exactly_fine(self):
        self.assertEqual(self.schema["$defs"]["Loan"]["required"], ["fine"])

    def test_library_shelves_is_an_array_of_shelf_refs(self):
        self.assertEqual(
            self.schema["$defs"]["Library"]["properties"]["shelves"],
            {"type": "array", "items": {"$ref": "#/$defs/Shelf"}},
        )

    def test_shelf_books_is_an_array_of_book_refs(self):
        self.assertEqual(
            self.schema["$defs"]["Shelf"]["properties"]["books"],
            {"type": "array", "items": {"$ref": "#/$defs/Book"}},
        )

    def test_member_loans_is_an_array_of_loan_refs(self):
        self.assertEqual(
            self.schema["$defs"]["Member"]["properties"]["loans"],
            {"type": "array", "items": {"$ref": "#/$defs/Loan"}},
        )

    def test_book_loans_is_an_array_of_loan_refs(self):
        self.assertEqual(
            self.schema["$defs"]["Book"]["properties"]["loans"],
            {"type": "array", "items": {"$ref": "#/$defs/Loan"}},
        )

    def test_attribute_counts_match_the_model(self):
        relationship_names = set(generator.RELATIONSHIP_NAMES.values())

        for entity, expected in EXPECTED_ATTRIBUTE_COUNTS.items():
            properties = self.schema["$defs"][entity]["properties"]
            attributes = [name for name in properties if name not in relationship_names]
            self.assertEqual(
                len(attributes),
                expected,
                f"{entity} has {len(attributes)} attributes, expected {expected}",
            )

    def test_relationship_counts_match_the_model(self):
        model = parse_puml(self.text)
        expected = {}
        for relationship in model.relationships:
            entity = generator.ENTITY_NAMES[relationship.parent]
            expected[entity] = expected.get(entity, 0) + 1

        relationship_names = set(generator.RELATIONSHIP_NAMES.values())
        for entity, definition in self.schema["$defs"].items():
            relationships = [
                name for name in definition["properties"] if name in relationship_names
            ]
            self.assertEqual(
                len(relationships),
                expected.get(entity, 0),
                f"{entity} has {len(relationships)} relationships, "
                f"expected {expected.get(entity, 0)}",
            )

    def test_required_lists_only_relationship_properties(self):
        relationship_names = set(generator.RELATIONSHIP_NAMES.values())

        for entity, definition in self.schema["$defs"].items():
            for name in definition.get("required", []):
                self.assertIn(name, relationship_names, f"{entity} requires {name}")

    def test_named_field_spellings_are_preserved(self):
        library = self.schema["$defs"]["Library"]["properties"]
        book = self.schema["$defs"]["Book"]["properties"]
        member = self.schema["$defs"]["Member"]["properties"]

        self.assertIn("libraryId", library)
        self.assertIn("openedOn", library)
        self.assertIn("replacementCost", book)
        self.assertIn("outstandingFees", member)


class CliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.dir = Path(self.temp.name)

    def test_omitted_output_exits_2_with_one_line(self):
        result = run_cli(str(MODEL_PATH))

        self.assertEqual(result.returncode, 2)
        self.assertEqual(len(result.stderr.strip().splitlines()), 1)
        self.assertNotIn("Traceback", result.stderr)

    def test_output_equal_to_input_is_refused(self):
        target = self.dir / "model.puml"
        target.write_text(read_fixture("schema-minimal.puml"), encoding="utf-8")

        result = run_cli(str(target), "--output", str(target))

        self.assertEqual(result.returncode, 2)
        self.assertIn("input", result.stderr)
        self.assertEqual(target.read_text(encoding="utf-8"), read_fixture("schema-minimal.puml"))

    def test_output_resolving_to_input_is_refused(self):
        target = self.dir / "model.puml"
        target.write_text(read_fixture("schema-minimal.puml"), encoding="utf-8")
        indirect = self.dir / "sub" / ".." / "model.puml"

        result = run_cli(str(target), "--output", str(indirect))

        self.assertEqual(result.returncode, 2)

    def test_non_utf8_input_reports_a_clean_error(self):
        broken = self.dir / "broken.puml"
        broken.write_bytes(b"@startuml\nclass LOAN {\n\xff\xfe\n}\n@enduml\n")
        output = self.dir / "out.json"

        result = run_cli(str(broken), "--output", str(output))

        self.assertEqual(result.returncode, 2)
        self.assertIn("UTF-8", result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertEqual(len(result.stderr.strip().splitlines()), 1)
        self.assertFalse(output.exists())

    def test_model_error_reports_file_line_and_message(self):
        output = self.dir / "out.json"
        broken = FIXTURES / "schema-unmatched-brace.puml"

        result = run_cli(str(broken), "--output", str(output))

        self.assertEqual(result.returncode, 2)
        self.assertNotIn("Traceback", result.stderr)
        self.assertEqual(len(result.stderr.strip().splitlines()), 1)
        self.assertIn(f"{broken}: line 4:", result.stderr)
        self.assertFalse(output.exists())

    def test_successful_run_writes_the_document_and_exits_0(self):
        output = self.dir / "nested" / "library.schema.json"

        result = run_cli(str(MODEL_PATH), "--output", str(output))

        self.assertEqual(result.returncode, 0, result.stderr)
        document = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(len(document["$defs"]), 6)

    def test_jsonschema_accepts_a_library_and_rejects_a_wrong_type(self):
        try:
            import jsonschema  # noqa: F401
        except ImportError:
            self.skipTest("jsonschema is not installed")

        schema_path = self.dir / "library.schema.json"
        self.assertEqual(run_cli(str(MODEL_PATH), "--output", str(schema_path)).returncode, 0)

        valid = self.dir / "valid.json"
        valid.write_text(
            json.dumps(
                {
                    "libraryId": "L-1",
                    "name": "Central Branch",
                    "openedOn": "1998-04-02",
                    "isOpen": True,
                    "shelves": [{"shelfCode": "A-1", "floor": 2, "capacity": 40}],
                    "books": [{"isbn": "978-1", "title": "The Long Field", "pageCount": 320}],
                }
            ),
            encoding="utf-8",
        )
        invalid = self.dir / "invalid.json"
        invalid.write_text(
            json.dumps({"libraryId": 123, "shelves": [{"shelfCode": "A-1"}]}),
            encoding="utf-8",
        )

        def validate(path):
            return subprocess.run(
                [sys.executable, "-m", "jsonschema", "-i", str(path), str(schema_path)],
                capture_output=True,
                text=True,
            )

        accepted = validate(valid)
        rejected = validate(invalid)

        self.assertEqual(accepted.returncode, 0, accepted.stderr)
        self.assertNotEqual(rejected.returncode, 0)


class DeterminismTests(unittest.TestCase):
    def test_two_runs_are_byte_identical(self):
        with tempfile.TemporaryDirectory() as temp:
            first = Path(temp) / "a.json"
            second = Path(temp) / "b.json"

            self.assertEqual(run_cli(str(MODEL_PATH), "--output", str(first)).returncode, 0)
            self.assertEqual(run_cli(str(MODEL_PATH), "--output", str(second)).returncode, 0)

            self.assertEqual(first.read_bytes(), second.read_bytes())

    def test_output_is_independent_of_python_hash_seed(self):
        with tempfile.TemporaryDirectory() as temp:
            first = Path(temp) / "seed0.json"
            second = Path(temp) / "seed1.json"

            for seed, target in (("0", first), ("1", second)):
                env = dict(os.environ, PYTHONHASHSEED=seed)
                result = run_cli(str(MODEL_PATH), "--output", str(target), env=env)
                self.assertEqual(result.returncode, 0, result.stderr)

            self.assertEqual(first.read_bytes(), second.read_bytes())


if __name__ == "__main__":
    unittest.main()
