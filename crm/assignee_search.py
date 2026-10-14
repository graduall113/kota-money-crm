"""
Searchable "Assigned To" for All Leads / My Leads.

SOURCE OF TRUTH: ``Lead.assigned_to_name`` — the free-text "Assigned To" the person who files the lead types on the
Add Lead form (and the "Assigned To" column of the Google Sheet): the person the file is given to for future
nurturing. It is NOT ``Lead.assigned_to`` (the CRM owner FK, which defaults to the staff member who *created* the
lead and drives permissions; the Sheet calls it "Assigned To (CRM)"), NOT ``reference_by_name`` and NOT ``created_by``.

Matching is case-insensitive and whitespace-insensitive (leading/trailing/repeated spaces). The stored value is never
modified. Everything is bounded: one GROUP BY over the caller's *authorised* leads yields the distinct names (a few
dozen in practice, hard-capped), and ranking/fuzzy matching runs on that short list in memory - never per lead row.
"""
import difflib
import re
import unicodedata
from collections import Counter, defaultdict

from django.db.models import Count

MAX_DISTINCT_NAMES = 2000     # hard cap on how many distinct names are ever examined
MAX_SUGGESTIONS = 8
FUZZY_CUTOFF = 0.74           # SequenceMatcher ratio; keeps "anchl jian" -> "Anchal Jain" but not unrelated names


def normalize(value):
    """Collapse whitespace, strip, casefold (and fold Unicode compatibility forms)."""
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", str(value or ""))).strip().casefold()


def tidy(value):
    """Display form: whitespace collapsed/trimmed, original capitalisation kept."""
    return re.sub(r"\s+", " ", str(value or "")).strip()


def name_groups(qs):
    """
    {normalized_name: {"variants": {raw: count}, "count": total}} for the leads in `qs` (already authorised by the
    caller). One grouped query; `order_by()` clears Lead's default ordering which would otherwise defeat GROUP BY.
    """
    rows = (qs.exclude(assigned_to_name="").order_by().values("assigned_to_name")
            .annotate(n=Count("id")).order_by("-n")[:MAX_DISTINCT_NAMES])
    groups = defaultdict(lambda: {"variants": Counter(), "count": 0})
    for row in rows:
        key = normalize(row["assigned_to_name"])
        if not key:
            continue
        groups[key]["variants"][row["assigned_to_name"]] += row["n"]
        groups[key]["count"] += row["n"]
    return groups


def _display(group):
    # The most common raw spelling, tidied. Ties: shortest, then alphabetical (deterministic).
    best = sorted(group["variants"].items(), key=lambda kv: (-kv[1], len(kv[0]), kv[0]))[0][0]
    return tidy(best)


def _rank(query, key):
    """Lower is better; None = not a match. Tiers: exact, prefix, word-prefix, substring, fuzzy."""
    if key == query:
        return 0, 0.0
    if key.startswith(query):
        return 1, 0.0
    words = key.split(" ")
    if any(w.startswith(query) for w in words) or all(any(w.startswith(t) for w in words) for t in query.split(" ")):
        return 2, 0.0
    if query in key:
        return 3, 0.0
    # Close-spelling: compare the whole name and each query-token against each name-token. Only for queries of
    # 3+ characters so one or two letters never pull in unrelated names.
    if len(query) >= 3:
        best = difflib.SequenceMatcher(None, query, key).ratio()
        q_tokens = query.split(" ")
        if all(len(t) >= 3 for t in q_tokens):
            per_token = [max((difflib.SequenceMatcher(None, t, w).ratio() for w in words), default=0) for t in q_tokens]
            best = max(best, min(per_token))
        if best >= FUZZY_CUTOFF:
            return 4, -best
    return None


def suggest(qs, query, limit=MAX_SUGGESTIONS):
    """
    Ranked, de-duplicated suggestions: [{"name": "Anchal Jain", "count": 12, "match": "exact|prefix|partial|similar"}].
    Empty query -> the most-used assignees (bounded), so the box is useful on focus without a long dropdown.
    """
    groups = name_groups(qs)
    q = normalize(query)
    tiers = ("exact", "prefix", "partial", "partial", "similar")
    scored = []
    if not q:
        scored = [((0, -g["count"], k), k, "") for k, g in groups.items()]
    else:
        for key, g in groups.items():
            r = _rank(q, key)
            if r is not None:
                scored.append(((r[0], r[1], -g["count"], key), key, tiers[r[0]]))
    scored.sort(key=lambda t: t[0])
    return [{"name": _display(groups[k]), "count": groups[k]["count"], "match": m} for _, k, m in scored[:limit]]


def raw_variants(qs, query):
    """
    The stored spellings that a typed/selected assignee stands for, so the lead filter can use an exact-match
    ``__in`` on the real column (fast, case/whitespace-proof). Returns (variants, mode):
      * exact   - every stored spelling whose normalised form equals the query ("anchal  JAIN" -> "Anchal Jain");
      * partial - nothing matched exactly, so every name containing the query (typing just "anchal" and pressing Apply);
      * none    - nothing matches (the list is correctly empty - it must NOT silently show every lead).
    """
    q = normalize(query)
    if not q:
        return [], "empty"
    groups = name_groups(qs)
    if q in groups:
        return list(groups[q]["variants"]), "exact"
    partial = [raw for k, g in groups.items() if q in k for raw in g["variants"]]
    return (partial, "partial") if partial else ([], "none")
