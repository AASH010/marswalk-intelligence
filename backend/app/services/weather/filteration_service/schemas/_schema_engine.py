"""Minimal, dependency-free JSON Schema (draft 2020-12 subset) engine.

Purpose
-------
The two schema validators must *apply the JSON Schema files themselves*
(``normalized_schema.json`` / ``filtered_schema.json``) rather than duplicate
their rules in Python.  The third-party ``jsonschema`` package is not required
by this package, so this module implements exactly the keyword subset the two
schemas use.

This module is generic: it contains NO MEDA knowledge, NO filtering logic and
never modifies the instance it validates.

Fail-closed behaviour
---------------------
A schema keyword that is neither implemented nor a known annotation raises
``SchemaDefinitionError``.  An unsupported keyword therefore can never be
silently ignored (which would make a schema look stricter than it is).

Supported validation keywords
-----------------------------
type, enum, const, pattern, minimum, minLength, minItems, minProperties, required,
properties, additionalProperties, items, $ref (local ``#/...`` only),
allOf, anyOf, if / then / else, not.
"""
from __future__ import annotations

import json
import math
import re
from pathlib import Path
from typing import Any, Iterator, Mapping

__all__ = ["SchemaDefinitionError", "SchemaEngine", "load_schema"]


class SchemaDefinitionError(ValueError):
    """The schema itself is malformed or uses an unsupported keyword."""


_ANNOTATIONS = frozenset({
    "$schema", "$id", "$comment", "$defs", "title", "description",
    "default", "examples", "deprecated",
})
_VALIDATION = frozenset({
    "type", "enum", "const", "pattern", "minimum", "minLength", "minItems", "minProperties",
    "required", "properties", "additionalProperties", "items", "$ref",
    "allOf", "anyOf", "if", "then", "else", "not",
})
_TYPE_NAMES = frozenset({"object", "array", "string", "integer", "number", "boolean", "null"})


def load_schema(path: str | Path) -> dict:
    """Read a JSON Schema file (strict JSON, UTF-8)."""
    def _reject(token: str) -> None:
        raise ValueError(f"non-finite JSON constant {token}")

    text = Path(path).read_text(encoding="utf-8")
    schema = json.loads(text, parse_constant=_reject)
    if not isinstance(schema, dict):
        raise SchemaDefinitionError(f"{path}: schema root must be a JSON object")
    return schema


def _is_type(value: Any, name: str) -> bool:
    if name == "null":
        return value is None
    if name == "boolean":
        return isinstance(value, bool)
    if name == "string":
        return isinstance(value, str)
    if name == "array":
        return isinstance(value, list)
    if name == "object":
        return isinstance(value, dict)
    if name == "integer":
        if isinstance(value, bool):
            return False
        if isinstance(value, int):
            return True
        return isinstance(value, float) and math.isfinite(value) and value.is_integer()
    if name == "number":
        if isinstance(value, bool):
            return False
        if isinstance(value, int):
            return True
        return isinstance(value, float) and math.isfinite(value)
    raise SchemaDefinitionError(f"unknown type name {name!r}")


def _json_equal(a: Any, b: Any) -> bool:
    """Equality that does not conflate True with 1 or False with 0."""
    if isinstance(a, bool) or isinstance(b, bool):
        return isinstance(a, bool) and isinstance(b, bool) and a == b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return a == b
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(_json_equal(x, y) for x, y in zip(a, b))
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(_json_equal(a[k], b[k]) for k in a)
    return type(a) is type(b) and a == b


def _fmt_path(path: tuple) -> str:
    out = ""
    for part in path:
        if isinstance(part, int):
            out += f"[{part}]"
        else:
            out += ("." if out else "") + str(part)
    return out


class SchemaEngine:
    """Validate instances against one loaded schema document."""

    def __init__(self, schema: Mapping[str, Any]) -> None:
        self.schema = dict(schema)
        self._pattern_cache: dict[str, re.Pattern[str]] = {}
        self._check_schema(self.schema, "#")

    # -- schema self-check (keyword support only; not full meta-validation) --

    def _check_schema(self, node: Any, where: str) -> None:
        if isinstance(node, bool):
            return
        if not isinstance(node, dict):
            raise SchemaDefinitionError(f"{where}: schema must be an object or boolean")
        for key, value in node.items():
            if key not in _ANNOTATIONS and key not in _VALIDATION:
                raise SchemaDefinitionError(f"{where}: unsupported schema keyword {key!r}")
            if key == "type":
                names = [value] if isinstance(value, str) else value
                if not isinstance(names, list) or not names or any(n not in _TYPE_NAMES for n in names):
                    raise SchemaDefinitionError(f"{where}: invalid 'type' {value!r}")
            elif key == "$ref":
                self._resolve(value)  # raises when unresolvable
            elif key == "pattern":
                try:
                    re.compile(value)
                except re.error as exc:
                    raise SchemaDefinitionError(f"{where}: invalid pattern {value!r}: {exc}") from exc
        for key in ("properties", "$defs"):
            if key in node:
                for name, sub in node[key].items():
                    self._check_schema(sub, f"{where}/{key}/{name}")
        for key in ("additionalProperties", "items", "if", "then", "else", "not"):
            if key in node:
                self._check_schema(node[key], f"{where}/{key}")
        for key in ("allOf", "anyOf"):
            if key in node:
                for i, sub in enumerate(node[key]):
                    self._check_schema(sub, f"{where}/{key}/{i}")

    def _resolve(self, ref: str) -> Any:
        if ref == "#":
            return self.schema
        if not isinstance(ref, str) or not ref.startswith("#/"):
            raise SchemaDefinitionError(f"only local '#/...' references are supported: {ref!r}")
        node: Any = self.schema
        for part in ref[2:].split("/"):
            part = part.replace("~1", "/").replace("~0", "~")
            if not isinstance(node, dict) or part not in node:
                raise SchemaDefinitionError(f"unresolvable $ref {ref!r}")
            node = node[part]
        return node

    # -- validation --

    def iter_errors(self, instance: Any) -> Iterator[tuple[str, str]]:
        """Yield ``(field_path, message)`` for every violation (never mutates)."""
        for path, message in self._validate(instance, self.schema, ()):
            yield _fmt_path(path), message

    def first_error(self, instance: Any) -> tuple[str, str] | None:
        return next(self.iter_errors(instance), None)

    def _validate(self, inst: Any, schema: Any, path: tuple) -> Iterator[tuple[tuple, str]]:
        if schema is True:
            return
        if schema is False:
            yield path, "no value is allowed here"
            return

        if "$ref" in schema:
            yield from self._validate(inst, self._resolve(schema["$ref"]), path)

        if "type" in schema:
            names = [schema["type"]] if isinstance(schema["type"], str) else schema["type"]
            if not any(_is_type(inst, n) for n in names):
                yield path, f"expected type {'|'.join(names)}, got {_describe(inst)}"
                return  # further keyword checks would only add noise

        if "enum" in schema and not any(_json_equal(inst, e) for e in schema["enum"]):
            yield path, f"value {inst!r} is not one of {schema['enum']!r}"
        if "const" in schema and not _json_equal(inst, schema["const"]):
            yield path, f"value {inst!r} must equal {schema['const']!r}"

        if isinstance(inst, str):
            if "pattern" in schema:
                pat = self._pattern_cache.setdefault(schema["pattern"], re.compile(schema["pattern"]))
                if pat.search(inst) is None:
                    yield path, f"string {inst!r} does not match pattern {schema['pattern']!r}"
            if "minLength" in schema and len(inst) < schema["minLength"]:
                yield path, f"string shorter than minLength {schema['minLength']}"

        if _is_type(inst, "number") and "minimum" in schema and inst < schema["minimum"]:
            yield path, f"value {inst!r} is below minimum {schema['minimum']!r}"

        if isinstance(inst, dict):
            if "minProperties" in schema and len(inst) < schema["minProperties"]:
                yield path, f"object has {len(inst)} propert(ies); minProperties is {schema['minProperties']}"
            for req in schema.get("required", ()):
                if req not in inst:
                    yield path, f"missing required property {req!r}"
            props = schema.get("properties", {})
            for key, sub in props.items():
                if key in inst:
                    yield from self._validate(inst[key], sub, path + (key,))
            if "additionalProperties" in schema:
                extra = schema["additionalProperties"]
                for key in inst:
                    if key in props:
                        continue
                    if extra is False:
                        yield path + (key,), f"unexpected property {key!r}"
                    elif extra is not True:
                        yield from self._validate(inst[key], extra, path + (key,))

        if isinstance(inst, list):
            if "minItems" in schema and len(inst) < schema["minItems"]:
                yield path, f"array has {len(inst)} item(s); minItems is {schema['minItems']}"
            if "items" in schema:
                for i, item in enumerate(inst):
                    yield from self._validate(item, schema["items"], path + (i,))

        for sub in schema.get("allOf", ()):
            yield from self._validate(inst, sub, path)
        if "anyOf" in schema:
            if not any(next(self._validate(inst, sub, path), None) is None for sub in schema["anyOf"]):
                yield path, "value does not satisfy any alternative of anyOf"
        if "not" in schema and next(self._validate(inst, schema["not"], path), None) is None:
            yield path, "value must not satisfy the 'not' subschema"
        if "if" in schema:
            matches = next(self._validate(inst, schema["if"], path), None) is None
            branch = schema.get("then") if matches else schema.get("else")
            if branch is not None:
                yield from self._validate(inst, branch, path)


def _describe(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    return type(value).__name__
