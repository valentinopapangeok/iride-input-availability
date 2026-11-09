"""Provider timestamps are evidence, not interchangeable publication dates."""
import requests


def nasa_dates(concept_id):
    try:
        response = requests.get(
            "https://cmr.earthdata.nasa.gov/search/granules.umm_json",
            params={"concept_id": concept_id, "page_size": 1}, timeout=30)
        response.raise_for_status()
        item = response.json()["items"][0]
        result = {}
        production = item["umm"].get("DataGranule", {}).get("ProductionDateTime")
        if production:
            result["production_date"] = production
        meta = item.get("meta", {})
        if meta.get("revision-date"):
            result["catalogue_date"] = meta["revision-date"]
            result["catalogue_date_source"] = (
                "NASA CMR initial ingestion" if meta.get("revision-id") == 1
                else "NASA CMR metadata revision (not initial publication)")
        return result
    except Exception:
        return {"timestamp_note": "NASA timestamp lookup failed; availability unaffected"}
