# alias_suggester.py
# Logica para sugerir alias de artista a partir del indice de audio.

import os
import json
import unicodedata
import difflib
import re
from collections import defaultdict, Counter
from typing import List, Dict, Tuple, Optional

_STOP_TOKENS = {
    "the", "and", "y", "con", "&", "+", "feat", "ft", "featuring", "vs"
}

_DIMINUTIVOS = {
    ("concha", "conchita"),
    ("paco", "paquito"),
    ("pepe", "pepi"),
    ("juan", "juanito"),
    ("luis", "luisito"),
    ("ana", "anita"),
    ("lola", "lolita"),
}

def _strip_accents(s: str) -> str:
    return unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode("ascii")

def _normalize(s: Optional[str]) -> str:
    if not s:
        return ""
    s = _strip_accents(s).lower()
    s = re.sub(r"[^\w\s&\+]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s

def _artist_signature(artist_norm: str) -> str:
    toks = [t for t in artist_norm.split() if t and t not in _STOP_TOKENS]
    return " ".join(sorted(toks))

def _tokens_jaccard(a: str, b: str) -> float:
    sa, sb = set(a.split()), set(b.split())
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / float(len(sa | sb))

def _is_diminutive(a: str, b: str) -> bool:
    a = a.split()[0] if a else ""
    b = b.split()[0] if b else ""
    if not a or not b:
        return False
    pair = (a, b)
    pair_rev = (b, a)
    if pair in _DIMINUTIVOS or pair_rev in _DIMINUTIVOS:
        return True
    if (a.endswith("ito") and a[:-3] == b) or (a.endswith("ita") and a[:-3] == b):
        return True
    if (b.endswith("ito") and b[:-3] == a) or (b.endswith("ita") and b[:-3] == a):
        return True
    return False

class ArtistAliasSuggester:
    def __init__(self) -> None:
        self._signature_variants: Dict[str, Counter] = defaultdict(Counter)
        self._variant_raw_forms: Dict[str, Counter] = defaultdict(Counter)
        self._variant_occurrences: Counter = Counter()
        self._variant_titles: Dict[str, Counter] = defaultdict(Counter)
        self._title_dur_to_artists: Dict[Tuple[str, int], set] = defaultdict(set)

    def add(self, artist: Optional[str], title: Optional[str], duration: Optional[float], path: str) -> None:
        if not artist:
            return
        a_norm = _normalize(artist)
        if not a_norm:
            return

        t_norm = _normalize(title or "")
        d_round = int(round(float(duration or 0.0)))

        self._variant_occurrences[a_norm] += 1
        self._variant_raw_forms[a_norm][artist] += 1
        if t_norm:
            self._variant_titles[a_norm][t_norm] += 1
            self._title_dur_to_artists[(t_norm, d_round)].add(a_norm)

        sig = _artist_signature(a_norm)
        if sig:
            self._signature_variants[sig][a_norm] += 1

    def _best_display_form(self, variant_norm: str) -> str:
        raw_forms = self._variant_raw_forms.get(variant_norm)
        if not raw_forms:
            return variant_norm
        return raw_forms.most_common(1)[0][0]

    def _pick_canonical(self, variants_counter: Counter) -> str:
        if not variants_counter:
            return ""
        top = variants_counter.most_common()
        best_norm = top[0][0]
        best_count = top[0][1]
        ties = [vn for vn, c in top if c == best_count]
        if len(ties) == 1:
            return best_norm

        def beauty_score(vn: str):
            disp = self._best_display_form(vn)
            non_ascii = sum(1 for ch in disp if ord(ch) > 127)
            words = len(disp.split())
            return (non_ascii, words)
        ties.sort(key=lambda vn: beauty_score(vn), reverse=True)
        return ties[0]

    def _confidence_and_reasons(self, a_norm: str, b_norm: str):
        reasons = []
        ratio = difflib.SequenceMatcher(None, a_norm, b_norm).ratio()
        if ratio >= 0.92:
            reasons.append("alta similitud de caracteres")

        jacc = _tokens_jaccard(a_norm, b_norm)
        if jacc >= 0.8:
            reasons.append("tokens muy similares")

        if _strip_accents(a_norm) == _strip_accents(b_norm) and a_norm != b_norm:
            reasons.append("diferencia solo por tildes/diacriticos")

        if _is_diminutive(a_norm, b_norm):
            reasons.append("diminutivo / variante comun")

        cooc = False
        titles_a = set(self._variant_titles.get(a_norm, {}))
        titles_b = set(self._variant_titles.get(b_norm, {}))
        if titles_a and titles_b:
            for (t, d), artists in self._title_dur_to_artists.items():
                if t in titles_a and t in titles_b and a_norm in artists and b_norm in artists:
                    cooc = True
                    break
        if cooc:
            reasons.append("coocurrencia en titulos/duraciones")

        score = 0.0
        score += min(0.6, ratio * 0.6)
        score += min(0.3, jacc * 0.3)
        if "diminutivo / variante comun" in reasons:
            score += 0.07
        if "diferencia solo por tildes/diacriticos" in reasons:
            score += 0.05
        if cooc:
            score += 0.08

        score = max(0.0, min(1.0, score))
        return score, reasons

    def build_suggestions(self) -> List[dict]:
        out = []
        for sig, variants_counter in self._signature_variants.items():
            if len(variants_counter) <= 1:
                continue

            canonical_norm = self._pick_canonical(variants_counter)
            canonical_disp = self._best_display_form(canonical_norm)

            for variant_norm, count in variants_counter.items():
                if variant_norm == canonical_norm:
                    continue
                conf, reasons = self._confidence_and_reasons(variant_norm, canonical_norm)
                examples = [t for t, _ in self._variant_titles.get(variant_norm, {}).most_common(3)]
                out.append({
                    "variant": self._best_display_form(variant_norm),
                    "variant_norm": variant_norm,
                    "canonical": canonical_disp,
                    "canonical_norm": canonical_norm,
                    "confidence": round(conf, 3),
                    "reasons": reasons,
                    "occurrences": int(count),
                    "examples": examples
                })
        out.sort(key=lambda x: (x["confidence"], x["occurrences"]), reverse=True)
        return out

    def save_suggestions(self, path_json: str, suggestions: Optional[List[dict]] = None) -> None:
        if suggestions is None:
            suggestions = self.build_suggestions()
        os.makedirs(os.path.dirname(path_json), exist_ok=True)
        with open(path_json, "w", encoding="utf-8") as f:
            json.dump(suggestions, f, ensure_ascii=False, indent=2)

    @staticmethod
    def _load_aliasconfig(path_cfg: str) -> dict:
        if not os.path.exists(path_cfg):
            return {"artist_alias": {}, "rules": {"normalize_diminutives": True}}
        with open(path_cfg, "r", encoding="utf-8") as f:
            try:
                data = json.load(f)
                if not isinstance(data, dict):
                    raise ValueError("aliasconfig mal formado")
            except Exception:
                data = {"artist_alias": {}, "rules": {"normalize_diminutives": True}}
        data.setdefault("artist_alias", {})
        data.setdefault("rules", {"normalize_diminutives": True})
        return data

    @staticmethod
    def _backup(path_cfg: str) -> None:
        if not os.path.exists(path_cfg):
            return
        base, ext = os.path.splitext(path_cfg)
        i = 1
        while True:
            bak = f"{base}.bak{i}{ext}"
            if not os.path.exists(bak):
                try:
                    with open(path_cfg, "rb") as src, open(bak, "wb") as dst:
                        dst.write(src.read())
                except Exception:
                    pass
                return
            i += 1

    @classmethod
    def apply_selected(cls, path_cfg: str, accepted_pairs: List[Tuple[str, str]]) -> None:
        cfg = cls._load_aliasconfig(path_cfg)
        alias_map = cfg["artist_alias"]

        def norm_key(s: str) -> str:
            return _normalize(s)

        for variant, canonical in accepted_pairs:
            if not variant or not canonical:
                continue
            if norm_key(variant) == norm_key(canonical):
                continue
            exists = False
            for k, v in list(alias_map.items()):
                if norm_key(k) == norm_key(variant) and norm_key(v) == norm_key(canonical):
                    exists = True
                    break
            if exists:
                continue
            alias_map[variant] = canonical

        os.makedirs(os.path.dirname(path_cfg), exist_ok=True)
        cls._backup(path_cfg)
        with open(path_cfg, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)

def prune_applied_suggestions(path_json: str, accepted_pairs: List[Tuple[str, str]]) -> None:
    try:
        with open(path_json, "r", encoding="utf-8") as f:
            sugs = json.load(f)
        if not isinstance(sugs, list):
            return
    except Exception:
        return

    acc_set = {(_normalize(a or ""), _normalize(b or "")) for (a, b) in accepted_pairs}
    out = []
    for s in sugs:
        var = _normalize(s.get("variant") or s.get("variant_norm") or "")
        can = _normalize(s.get("canonical") or s.get("canonical_norm") or "")
        if (var, can) in acc_set:
            continue
        out.append(s)

    try:
        with open(path_json, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
    except Exception:
        pass
