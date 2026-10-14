"""
Small, fail-safe wrapper around Django's cache framework.

Design rules
------------
* Uses whatever CACHES["default"] is configured (LocMemCache out of the box - no Redis, no paid
  service). LocMemCache is PER PROCESS: with several gunicorn workers each worker has its own copy.
  That is why only low-risk data is cached here and always with a SHORT time-to-live.
* NEVER cached: authorization decisions, permissions, lead/contact rows, lead statuses, assignments,
  attendance state, anything returned by access.visible_*().
* Cached: filter-dropdown option lists (lead source / import batch names) and the two heavy admin
  analytics aggregates. Keys always include the user (or "admin" for admin-only pages) and every
  query parameter that influences the result, so one person's data can never be served to another.
* A cache outage must not break a page: every operation is wrapped, and on any error the value is simply
  computed fresh.
* ``bump(namespace)`` changes a namespace "generation" so every key built from it stops matching at
  once (cheap invalidation without deleting keys one by one). It only affects THIS process; other
  workers fall back on the TTL.
"""
import hashlib
import logging

from django.core.cache import cache

logger = logging.getLogger("crm")

NAMESPACE_TTL = 24 * 3600


def _generation(namespace):
    try:
        gen = cache.get(f"crm:gen:{namespace}")
        if gen is None:
            gen = 1
            cache.set(f"crm:gen:{namespace}", gen, NAMESPACE_TTL)
        return gen
    except Exception:  # noqa: BLE001 - cache down: behave as "no cache"
        return 0


def bump(namespace):
    """Invalidate every cached value of a namespace (this process; others expire by TTL)."""
    try:
        cache.set(f"crm:gen:{namespace}", _generation(namespace) + 1, NAMESPACE_TTL)
    except Exception:  # noqa: BLE001
        logger.debug("cache bump failed for %s", namespace, exc_info=True)


def make_key(namespace, *parts):
    raw = "|".join(str(p) for p in parts)
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()
    return f"crm:{namespace}:{_generation(namespace)}:{digest}"


def cached(namespace, parts, ttl, producer):
    """Return the cached value for (namespace, parts) or compute it with producer() and store it for ttl seconds."""
    key = make_key(namespace, *parts)
    try:
        hit = cache.get(key)
        if hit is not None:
            return hit
    except Exception:  # noqa: BLE001
        return producer()
    value = producer()
    try:
        cache.set(key, value, ttl)
    except Exception:  # noqa: BLE001 - unpicklable / backend error: just don't cache
        logger.debug("cache set failed for %s", namespace, exc_info=True)
    return value


# Namespaces that depend on lead / contact / assignment / document data.
REPORT_NAMESPACES = ("analytics_perf", "analytics_docs")


def invalidate_reports():
    """Called whenever underlying data changes (model signals, finished bulk jobs and imports)."""
    for ns in REPORT_NAMESPACES:
        bump(ns)
