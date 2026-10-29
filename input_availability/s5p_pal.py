"""Public PAL monthly CH4 catalogue, with observation-based dates."""
import datetime as dt
from functools import lru_cache
from urllib.parse import urljoin, urlparse

import requests

URL = "https://data-portal.s5p-pal.com/api/s5p-l3/collections/ch4/items"


def observation_start(feature):
    """Never substitute publication time or the exclusive interval end."""
    value = feature["properties"]["start_datetime"]
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))


@lru_cache(maxsize=1)
def monthly_ch4():
    """Cache only within this audit process; fail rather than truncate pages."""
    url = URL
    seen = set()
    features = {}
    while url:
        if url in seen:
            raise ValueError("PAL catalogue pagination cycle")
        if urlparse(url).scheme != "https" or urlparse(url).netloc != urlparse(URL).netloc:
            raise ValueError("Unexpected PAL pagination host")
        seen.add(url)
        response = requests.get(
            url,
            params={"limit": 100, "filter": "l3:period='month'", "filter-lang": "cql2-text"} if len(seen) == 1 else None,
            timeout=60,
        )
        response.raise_for_status()
        page = response.json()
        for feature in page["features"]:
            if feature["properties"].get("l3:period") == "month":
                observation_start(feature)  # Reject malformed coverage metadata.
                features[feature["id"]] = feature
        link = next((link["href"] for link in page.get("links", []) if link["rel"] == "next"), None)
        url = urljoin(url, link) if link else None
    return tuple(features.values())


def latest_monthly_ch4():
    return max(
        monthly_ch4(),
        key=lambda f: (observation_start(f), f["properties"].get("archive_date", "")),
        default=None,
    )
