"""Loads form_config.yaml and normalises it into a render-ready structure.

The raw YAML uses a compact `partner_template` + `partners` shorthand. This
module expands that into concrete sections, gives every field/upload a globally
unique `name` (``<section>__<key>``), and precomputes:

  * ordered `sections` for rendering
  * `slots`  : upload-slot name -> {extractor, accept, section, upload_key}
  * each upload's `fills` : the field names it should populate after extraction
"""
import os
from functools import lru_cache

import yaml

_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "form_config.yaml")


def _qualify(section_key: str, key: str) -> str:
    return f"{section_key}__{key}"


def _build_section(section_key: str, title: str, raw: dict) -> dict:
    fields = []
    for f in raw.get("fields", []) or []:
        f = dict(f)
        f["name"] = _qualify(section_key, f["key"])
        fields.append(f)

    uploads = []
    for u in raw.get("uploads", []) or []:
        u = dict(u)
        u["name"] = _qualify(section_key, u["key"])
        # fields in this section that this upload fills once extracted
        u["fills"] = [f["name"] for f in fields if f.get("fill_from") == u["key"]]
        uploads.append(u)

    return {"key": section_key, "title": title, "fields": fields, "uploads": uploads}


def _normalise(cfg: dict) -> dict:
    template = cfg.get("partner_template", {}) or {}
    partners = cfg.get("partners", []) or []
    raw_sections = cfg.get("sections", []) or []

    top, bottom = [], []
    for s in raw_sections:
        built = _build_section(s["key"], s.get("title", ""), s)
        (top if s.get("position") == "top" else bottom).append(built)

    partner_sections = [
        _build_section(p["key"], p.get("title", p["key"]), template) for p in partners
    ]

    sections = top + partner_sections + bottom

    # slot lookup for the extract endpoint
    slots = {}
    for section in sections:
        for up in section["uploads"]:
            slots[up["name"]] = {
                "extractor": up.get("extractor", "generic"),
                "accept": up.get("accept", ""),
                "section": section["key"],
                "upload_key": up["key"],
                "fills": up["fills"],
            }

    return {
        "title": cfg.get("title", "Client Details"),
        "description": cfg.get("description", ""),
        "sections": sections,
        "slots": slots,
    }


@lru_cache(maxsize=1)
def get_form_config() -> dict:
    with open(_CONFIG_PATH, "r", encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh) or {}
    return _normalise(cfg)


def iter_fields(cfg: dict):
    for section in cfg["sections"]:
        for field in section["fields"]:
            yield section, field


def iter_uploads(cfg: dict):
    for section in cfg["sections"]:
        for up in section["uploads"]:
            yield section, up
