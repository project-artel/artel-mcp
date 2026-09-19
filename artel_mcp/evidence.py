"""Reads the evidence document the SDK uploads and renders it for the agent.

The document is what the IL analysis produced at build time plus the scene walk at startup:
`scenes[]`, `objects[]` (what sits in each scene, with what each Button is wired to), and
`types{}` (records per behaviour type: what triggers a method, what it changes, and the
condition it passed through).

This is the content map's raw material. The hosted server loads it into scene → screen →
capability rows; here we render straight from the document, which is enough for Claude to
know what a scene offers before it plays.
"""

from __future__ import annotations

import re
from collections import defaultdict
from typing import Any

_SIG = re.compile(r"^(?:\S+\s+)?(?:[\w.]+::)?(?P<name>\w+)\((?P<args>[^)]*)\)$")


def _method(sig: str) -> str:
    """`System.Void Scenes.TitleSceneManager::InitPlayerData()` → `InitPlayerData()`."""
    m = _SIG.match(sig or "")
    if not m:
        return sig
    args = m.group("args")
    return f"{m.group('name')}({'…' if args else ''})"


def _short(type_name: str) -> str:
    return (type_name or "").split(".")[-1]


def _cond(node: Any) -> str:
    """One line for a condition tree: always / test / gesture / group."""
    if not isinstance(node, dict) or not node:
        return "always"
    kind = str(node.get("kind", "")).lower()
    if kind == "always":
        return "always"
    if kind == "test" or ("left" in node and "operator" in node):
        return f"{node.get('left')} {node.get('operator')} {node.get('right')}"
    if kind == "gesture" or "input" in node:
        inp = node.get("input")
        if isinstance(inp, dict):
            return f"gesture {inp.get('kind', '')} {inp.get('control', '')} {inp.get('phase', '')}".strip()
        return f"gesture {inp}"
    parts = node.get("parts")
    if isinstance(parts, list):
        joiner = " AND " if kind == "every" else " OR "
        return "(" + joiner.join(_cond(p) for p in parts) + ")"
    return f"unknown({node.get('kind')})"


def _effect(effect: dict[str, Any]) -> str:
    kind = effect.get("kind") or ""
    target = effect.get("target") or ""
    detail = effect.get("detail")
    line = f"{kind} {target}".strip()
    if detail not in (None, ""):
        line += f" = {detail}"
    return line


def scenes(doc: dict[str, Any]) -> list[str]:
    return [str(s) for s in doc.get("scenes", [])]


def objects_in(doc: dict[str, Any], scene: str) -> list[dict[str, Any]]:
    out = [o for o in doc.get("objects", []) if o.get("scene") == scene]
    out += [o for o in doc.get("persistentObjects", []) if o.get("scene") == scene]
    return out


def summary(doc: dict[str, Any]) -> dict[str, Any]:
    build = doc.get("build", {}) or {}
    types = doc.get("types", {}) or {}
    return {
        "schema": doc.get("schema"),
        "capture": doc.get("capture"),
        "unity": build.get("unity"),
        "sdk": build.get("sdk"),
        "scenes": scenes(doc),
        "objects": len(doc.get("objects", [])) + len(doc.get("persistentObjects", [])),
        "behaviour_types": len(types),
        "records": sum(len(v or []) for v in types.values()),
        "gaps": len(doc.get("gaps", [])),
    }


def _records_by_method(doc: dict[str, Any], type_names: set[str]) -> dict[tuple[str, str], list[dict[str, Any]]]:
    """(type, method) → records. Several records per method are normal: one per condition branch."""
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for name in type_names:
        for rec in (doc.get("types", {}) or {}).get(name, []) or []:
            grouped[(name, _method(rec.get("entry", "")))].append(rec)
            # The analyser folds paths that reach the same fact: only the first path is `entry`,
            # the others sit in `alsoReachedBy`. A button wired to one of those would otherwise
            # show no outcome at all (Canvas/continue → LoadStoryScene in the sample game).
            for other in rec.get("alsoReachedBy", []) or []:
                grouped[(name, _method(other.get("entry", "")))].append(rec)
    return grouped


def _outcomes(records: list[dict[str, Any]]) -> list[str]:
    """One line per record: effects, and the condition when it is not `always`."""
    out = []
    for rec in records:
        effects = [_effect(e) for e in rec.get("effects", []) or []]
        if not effects:
            continue
        cond = _cond(rec.get("condition"))
        out.append("; ".join(effects) + (f"   when {cond}" if cond != "always" else ""))
    return out or ["(no observable effect recorded)"]


def render_scene(doc: dict[str, Any], scene: str, max_objects: int = 60) -> str:
    """What a scene holds and what pressing things does, as the agent should read it.

    Buttons come first with the method they are wired to and that method's recorded
    outcomes. Then the behaviours that run on their own (Start/Update and the like)."""
    objs = objects_in(doc, scene)
    if not objs and scene not in scenes(doc):
        return f"scene {scene!r} is not in this build's evidence. Scenes: {', '.join(scenes(doc))}"

    type_names: set[str] = set()
    for o in objs:
        for c in o.get("components", []) or []:
            type_names.add(c.get("type", ""))
    by_method = _records_by_method(doc, type_names | {c.get("targetType", "") for o in objs for comp in (o.get("components") or []) for c in (comp.get("calls") or [])})

    lines = [f"# {scene}", f"objects: {len(objs)}", "", "## controls (what pressing does)"]
    wired_methods: set[tuple[str, str]] = set()
    n_controls = 0
    for o in objs[:max_objects]:
        for comp in o.get("components", []) or []:
            calls = comp.get("calls") or []
            if not calls and _short(comp.get("type", "")) not in ("Button", "Toggle", "Slider", "InputField", "TMP_InputField"):
                continue
            n_controls += 1
            sel = o.get("selector") or o.get("path")
            flag = "" if o.get("active", True) else " (inactive)"
            label = f'  label="{o["label"]}"' if o.get("label") else ""
            lines.append(f"- {sel}{flag} [{_short(comp.get('type',''))}]{label}")
            for call in calls:
                key = (call.get("targetType", ""), f"{call.get('method', '')}()")
                wired_methods.add(key)
                recs = by_method.get(key, [])
                lines.append(f"    {call.get('event','onClick')} → {_short(call.get('targetType',''))}.{call.get('method','')}()")
                for line in _outcomes(recs):
                    lines.append(f"      → {line}")
    if n_controls == 0:
        lines.append("- (no wired controls in this scene's objects)")

    auto = []
    for (type_name, method), recs in sorted(by_method.items()):
        if (type_name, method) in wired_methods:
            continue
        trig = {r.get("triggerKind") for r in recs}
        outs = _outcomes(recs)
        if outs == ["(no observable effect recorded)"]:
            continue
        auto.append(f"- [{'/'.join(sorted(t for t in trig if t))}] {_short(type_name)}.{method}")
        auto.extend(f"    → {line}" for line in outs)
    if auto:
        lines += ["", "## other behaviours (run on their own or are called from elsewhere)"] + auto

    others = [o for o in objs if not any((c.get("calls") or []) for c in (o.get("components") or []))]
    if others:
        lines += ["", "## other objects"]
        for o in others[:max_objects]:
            comps = ", ".join(_short(c.get("type", "")) for c in (o.get("components") or []) if c.get("type"))
            lines.append(f"- {o.get('selector') or o.get('path')}" + (f"  [{comps}]" if comps else ""))
    return "\n".join(lines)


def find_capabilities(doc: dict[str, Any], query: str, limit: int = 30) -> str:
    """Records whose method, type, effect target, or condition mention `query`."""
    q = query.lower()
    hits: list[str] = []
    for type_name, recs in (doc.get("types", {}) or {}).items():
        for rec in recs or []:
            eff = "; ".join(_effect(e) for e in rec.get("effects", []) or [])
            cond = _cond(rec.get("condition"))
            hay = f"{type_name} {rec.get('entry','')} {eff} {cond}".lower()
            if q in hay:
                hits.append(
                    f"- [{rec.get('triggerKind','')}] {_short(type_name)}.{_method(rec.get('entry',''))} → {eff or '(no observable effect)'}"
                    + (f"   when {cond}" if cond != "always" else "")
                )
                if len(hits) >= limit:
                    return "\n".join(hits) + f"\n… (stopped at {limit})"
    return "\n".join(hits) if hits else f"nothing mentions {query!r}"
