"""Score each notice against profile.json. Fit, not timing.

Every score carries the rules that produced it, so a surprise is traceable.
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from typing import Any

# profile.json lives in the project root.
PROFILE_PATH = Path(__file__).resolve().parent.parent / "profile.json"

# Commodity titles lead with their supply class: "59--CONNECTOR,RECEPTACL".
FSC_PREFIX = re.compile(r"^\s*([0-9]{2}|[0-9][A-Z][0-9]{2}|[A-Z][A-Z][0-9]{2})\s*--")


def load_profile(path: Path | str = PROFILE_PATH) -> dict:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


# Fixed list, not "any letters": `data` must not reach `database`.
_SUFFIX = r"(?:s|es|ed|d|ing|ion|ions|ize|izes|ized|izing|ation|ations|ment|ments|er|ers|al|ally)?"


def _stem_variants(word: str) -> list[str]:
    """Forms a word can take before a suffix is attached."""
    variants = {word}
    if word.endswith("e"):
        variants.add(word[:-1])          # migrate -> migrat(ion), modernize -> moderniz(ation)
    if len(word) > 3 and word[-1].isalpha() and word[-1] not in "aeiouy":
        variants.add(word + word[-1])    # model -> modell(ing)
    return sorted(variants, key=len, reverse=True)


def _compile_terms(terms: list[str]) -> list[tuple[str, re.Pattern]]:
    """Match a term and its inflections, never across a word boundary."""
    compiled = []
    for term in terms:
        words = term.lower().split()
        head = [re.escape(w) for w in words[:-1]]
        stems = "|".join(re.escape(v) for v in _stem_variants(words[-1]))
        tail = rf"(?:{stems}){_SUFFIX}"
        pattern = r"\s+".join([*head, tail])
        compiled.append((term, re.compile(rf"(?<![a-z0-9]){pattern}(?![a-z0-9])")))
    return compiled


class Scorer:
    def __init__(self, profile: dict) -> None:
        self.profile = profile
        self.keywords = {
            tier: {
                "points": cfg["points"],
                "max_hits": cfg.get("max_hits", 99),
                # 1.0 = description hits count as much as title hits
                "description_weight": cfg.get("description_weight", 1.0),
                "terms": _compile_terms(cfg["terms"]),
            }
            for tier, cfg in profile["keywords"].items()
            if not tier.startswith("_")
        }
        psc = profile["psc"]
        self.psc_codes: dict[str, tuple[int, str]] = {}
        for tier in ("strong", "moderate", "weak"):
            for code in psc[tier]["codes"]:
                self.psc_codes[code.upper()] = (psc[tier]["points"], tier)
        self.psc_penalty_prefixes = tuple(p.upper() for p in psc["penalty"]["prefixes"])
        self.psc_penalty_points = psc["penalty"]["points"]
        self.naics = set(profile["naics"]["codes"])

    # -- individual signals -------------------------------------------------

    def _score_psc(self, code: str | None, reasons: list) -> int:
        if not code:
            return 0
        code = code.strip().upper()
        if code in self.psc_codes:
            points, tier = self.psc_codes[code]
            reasons.append([f"PSC {code} ({tier} match)", points])
            return points
        if code.startswith(self.psc_penalty_prefixes):
            reasons.append([f"PSC {code} (hardware/telecom, not our work)", self.psc_penalty_points])
            return self.psc_penalty_points
        return 0

    def _score_keywords(self, title: str, description: str | None, reasons: list) -> int:
        """Title and description weighted separately via `description_weight`."""
        head = title.lower()
        body = (description or "").lower()
        total = 0
        for tier, cfg in self.keywords.items():   # order follows profile.json
            weight = cfg["description_weight"]
            in_title, in_body = [], []
            for term, pattern in cfg["terms"]:
                if pattern.search(head):
                    in_title.append(term)
                elif body and pattern.search(body):
                    in_body.append(term)
            if not in_title and not in_body:
                continue
            # Cap spans both; title hits go first.
            room = cfg["max_hits"]
            counted_title = in_title[:room]
            counted_body = in_body[: max(0, room - len(counted_title))]
            points = cfg["points"] * len(counted_title)
            points += round(cfg["points"] * weight) * len(counted_body)
            if not points:
                continue
            parts = counted_title + [f"{t} (in description)" for t in counted_body]
            reasons.append([f"{tier} terms: {', '.join(parts)}", points])
            total += points
        return total

    def _score_naics(self, naics: str | None, reasons: list) -> int:
        if naics and naics in self.naics:
            points = self.profile["naics"]["points"]
            reasons.append([f"NAICS {naics}", points])
            return points
        return 0

    def _score_set_aside(self, set_aside: str | None, reasons: list) -> int:
        if not set_aside:
            return 0
        text = set_aside.lower()
        cfg = self.profile["set_aside"]
        for pattern in cfg["disqualified"]["patterns"]:
            if pattern.lower() in text:
                points = cfg["disqualified"]["points"]
                reasons.append([f"set-aside we can't hold: {set_aside}", points])
                return points
        for pattern in cfg["qualified"]["patterns"]:
            if pattern.lower() in text:
                points = cfg["qualified"]["points"]
                reasons.append([f"small business set-aside", points])
                return points
        for pattern in cfg.get("unrestricted", {}).get("patterns", []):
            if pattern.lower() in text:
                points = cfg["unrestricted"]["points"]
                reasons.append(["unrestricted — open to firms of any size", points])
                return points
        return 0

    def _score_notice_type(self, notice_type: str | None, reasons: list) -> int:
        points = self.profile["notice_type"]["points"].get(notice_type or "", 0)
        if points:
            reasons.append([notice_type, points])
        return points

    def _score_fsc_prefix(self, title: str, reasons: list) -> int:
        """A title's own FSC prefix, when it has one."""
        match = FSC_PREFIX.match(title)
        if not match:
            return 0
        code = match.group(1).upper()
        if code in self.psc_codes:
            return 0        # already counted via the classification code
        if code.startswith(self.psc_penalty_prefixes):
            reasons.append([f"title tagged {code}-- (commodity item)", self.psc_penalty_points])
            return self.psc_penalty_points
        return 0

    # -- public -------------------------------------------------------------

    def score(
        self, row: dict[str, Any] | sqlite3.Row, description: str | None = None
    ) -> tuple[float, list]:
        reasons: list = []
        title = row["title"] or ""
        if description:
            reasons.append(["read with description", 0])
        total = (
            self._score_psc(row["classification_code"], reasons)
            + self._score_keywords(title, description, reasons)
            + self._score_naics(row["naics_code"], reasons)
            + self._score_set_aside(row["set_aside"], reasons)
            + self._score_notice_type(row["notice_type"], reasons)
            + self._score_fsc_prefix(title, reasons)
        )
        return max(0.0, min(100.0, float(total))), reasons
