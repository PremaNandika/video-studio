"""Brand Studio helpers — brand-kit loader + compliance checker.

Subsystem-helper module for ``routes/brand.py`` (B13 S5, the Brand
Studio tab). Both helpers are pure (no ``current_app``): they take the
brand-kit path / dicts explicitly, so they're testable and usable from
anywhere. Mirrors the services/helpers/library.py + qc.py pattern.

Pure relocation of server.py's ``load_brand_kit`` (L79-83) and
``_brand_compliance_errors`` (L3225-3235).

server.py is unchanged (Rule 16).
"""
from __future__ import annotations

import json
import re
from pathlib import Path


def load_brand_kit(brand_kit_path: Path) -> dict:
    """Load the brand kit JSON (liitt-brand-kit.json), {} on any error.
    Pure relocation of server.py L79-83; takes the path explicitly
    (server.py read the module-level BRAND_KIT_PATH)."""
    try:
        return json.loads(Path(brand_kit_path).read_text(encoding="utf-8"))
    except Exception:
        return {}


def brand_compliance_errors(kit: dict, copy: dict) -> list[str]:
    """Return a list of compliance violations in ``copy`` (banned words +
    banned-claims regex from the kit). Empty list = compliant.

    Pure relocation of server.py's ``_brand_compliance_errors``
    (L3225-3235). Renamed to a public name (no leading underscore) since
    it's now imported across modules.
    """
    comp = kit.get("compliance", {})
    banned = [w.lower() for w in comp.get("banned_words", [])]
    claims_re = re.compile(comp.get("banned_claims_regex", r"(?!x)x"), re.I)
    blob = " ".join(str(copy.get(k, "")) for k in ("eyebrow", "headline", "subhead", "cta", "price_line"))
    low = blob.lower()
    errs = [f"banned word '{w}'" for w in banned if re.search(rf"\b{re.escape(w)}\b", low)]
    m = claims_re.search(blob)
    if m:
        errs.append(f"medical/absolute claim '{m.group(0)}'")
    return errs
