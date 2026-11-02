"""The /vrbo/* endpoint functions (route_glue's route_table calls them
with the schema's loaded kwargs).

The four property endpoints share ONE cached page parse: cache.cached_call
stores parse_property_page's record under vrbo.property keyed on the
`property` reference, so details + reviews + calendar + price on the same
listing cost one listing-page fetch (the id mapping Vrbo listing id ->
Expedia property id lives only on that page; GraphQL speaks Expedia ids).
"""
import math

from cache import cached_call
from vrbo import cache_config as ttl
from vrbo import parsers, refs
from vrbo.fetch import (REVIEWS_MAX_SIZE, SEARCH_PAGE_SIZE, VrboNotFound, VrboUpstreamError,
                        fetch_autocomplete, fetch_calendar, fetch_offer, fetch_property_page,
                        fetch_reviews, fetch_search, fetch_search_page)


# ---- locations/autocomplete --------------------------------------------------------

def _autocomplete(query, limit):
    results = parsers.parse_autocomplete(fetch_autocomplete(query, limit))[:limit]
    return {"query": query, "count": len(results), "results": results}


def locations_autocomplete(**data):
    return cached_call("vrbo.autocomplete", data, _autocomplete, ttl.AUTOCOMPLETE_CACHE)


# ---- the search area ----------------------------------------------------------------

def _destination(location, latitude, longitude, bounds):
    """The GraphQL destination input from the three ways to pick an area."""
    if bounds:
        return {"mapBounds": bounds}
    if latitude is not None and longitude is not None:
        return {"coordinates": {"latitude": latitude, "longitude": longitude}}
    location = location or {}
    if location.get("region_id"):
        return {"regionId": location["region_id"]}
    if location.get("coordinates"):
        return {"coordinates": location["coordinates"]}
    if location.get("bounds"):
        return {"mapBounds": location["bounds"]}
    return {"regionName": location.get("region_name")}


def _token(value):
    """The site's filter tokens are lower_snake_case ("hot_tub"), bar the
    one upper-case property type ("RV")."""
    token = str(value).strip().replace(" ", "_").replace("-", "_").lower()
    return "RV" if token == "rv" else token


def _criteria(data):
    """Filters -> the site's criteria selections / ranges / booleans."""
    selections, ranges, booleans = [], [], []
    for token in data.get("amenities") or []:
        selections.append({"id": refs.AMENITIES_GROUP, "value": _token(token)})
    for token in data.get("property_type") or []:
        selections.append({"id": refs.PROPERTY_TYPE_GROUP, "value": _token(token)})
    for token in data.get("house_rules") or []:
        selections.append({"id": refs.HOUSE_RULES_GROUP, "value": _token(token)})
    for token in data.get("location_type") or []:
        selections.append({"id": refs.LOCATION_GROUP, "value": _token(token)})
    for token in data.get("neighborhood") or []:
        selections.append({"id": refs.NEIGHBORHOOD_GROUP, "value": token})
    if data.get("guest_rating"):
        selections.append({"id": refs.GUEST_RATING_GROUP, "value": data["guest_rating"]})
    if data.get("premier_host"):
        selections.append({"id": refs.PREMIER_HOST_GROUP, "value": "premier_host"})
    if data.get("instant_confirmation"):
        selections.append({"id": refs.BOOKING_OPTIONS_GROUP, "value": "instant_confirmation"})
    for key, sel_id in (("bedrooms", refs.MIN_BEDROOMS), ("bathrooms", refs.MIN_BATHROOMS), ("beds", refs.MIN_BEDS)):
        if data.get(key):
            selections.append({"id": sel_id, "value": str(data[key])})
    lo, hi = data.get("min_price"), data.get("max_price")
    if lo is not None or hi is not None:
        ranges.append({"id": data.get("price_type") or "nightly_price",
                       "min": int(lo) if lo is not None else 0,
                       "max": int(hi) if hi is not None else -2})   # -2 = the site's "and above"
    if data.get("vacation_rentals_only"):
        booleans.append({"id": "showVacationRentalsOnly", "value": True})
    return selections, ranges, booleans


# ---- properties/search ----------------------------------------------------------------

def _search(**data):
    page = data["page"]
    start = (page - 1) * SEARCH_PAGE_SIZE
    destination = _destination(data.get("location"), data.get("latitude"), data.get("longitude"), data.get("bounds"))
    selections, ranges, booleans = _criteria(data)
    parsed = parsers.parse_search(fetch_search(
        destination, start, checkin=data.get("checkin"), checkout=data.get("checkout"),
        adults=data["adults"], children=data.get("children"), sort=data["sort"],
        selections=selections, ranges=ranges, booleans=booleans))
    total = parsed["total_count"]
    total_pages = max(1, math.ceil(total / SEARCH_PAGE_SIZE)) if isinstance(total, (int, float)) and total else None
    return {
        "location": parsed["location"],
        "checkin": parsed["checkin"] or data.get("checkin"),
        "checkout": parsed["checkout"] or data.get("checkout"),
        "adults": data["adults"],
        "children": data.get("children") or [],
        "sort": next((k for k, v in refs.SORTS.items() if v == data["sort"]), data["sort"].lower()),
        "results": parsed["results"],
        "pagination": {
            "page": page,
            "items_per_page": SEARCH_PAGE_SIZE,
            "has_next": parsed["has_next"],
            "total_pages": total_pages,
            "total_count": int(total) if isinstance(total, (int, float)) else None,
        },
    }


def properties_search(**data):
    return cached_call("vrbo.search", data, _search, ttl.SEARCH_CACHE)


# ---- properties/filters ---------------------------------------------------------------

def _filters(location, checkin, checkout, adults):
    destination = _destination(location, None, None, None)
    html = fetch_search_page(destination, checkin, checkout, adults)
    state = parsers.apollo_state(html)
    parsed = parsers.parse_filters(state or {})
    if not parsed["filters"]:
        raise VrboUpstreamError("search page without a filter block")
    return {"checkin": checkin, "checkout": checkout, "adults": adults, **parsed}


def properties_filters(**data):
    return cached_call("vrbo.filters", data, _filters, ttl.FILTERS_CACHE)


# ---- the shared property record ----------------------------------------------------------

def _property_record(property):
    html, listing_id = fetch_property_page(property)
    record = parsers.parse_property_page(html, listing_id)
    if not record or not record.get("expedia_id"):
        raise VrboUpstreamError(f"property {property}: page without a property record")
    return record


def _record(property):
    return cached_call("vrbo.property", {"property": property}, _property_record, ttl.PROPERTY_CACHE)


def _identity(record):
    return {"id": record["id"], "expedia_id": record["expedia_id"], "name": record["name"], "link": record["link"]}


# ---- properties/details ------------------------------------------------------------------

def property_details(property, checkin=None, checkout=None, adults=2, children=None):
    """Everything on the public listing page; with dates, the quote for
    that stay (same as /vrbo/properties/price) instead of the site's
    default-dates headline price."""
    record = dict(_record(property))
    out = {**_identity(record), **{k: v for k, v in record.items() if k not in ("id", "expedia_id", "name", "link")}}
    if checkin and checkout:
        out["price"] = _price(property, checkin, checkout, adults, children or [])
    else:
        out["price"] = None
    return out


# ---- properties/reviews ------------------------------------------------------------------

def _reviews(property, page, limit, sort, traveler_type, query):
    record = _record(property)
    start = (page - 1) * limit
    parsed = parsers.parse_reviews(fetch_reviews(record["expedia_id"], start=start, size=limit, sort=sort,
                                                 traveler_type=traveler_type, search_term=query))
    total = parsed["summary"]["review_count"]
    filtered = bool(traveler_type or query)
    if filtered or total is None:
        # the summary count is the property's total, not the filtered one
        total_pages, total_count = None, None if filtered else total
        has_next = len(parsed["reviews"]) >= limit
    else:
        total_pages, total_count = max(1, math.ceil(total / limit)), total
        has_next = start + len(parsed["reviews"]) < total
    return {
        **_identity(record),
        "sort": next((k for k, v in refs.REVIEW_SORTS.items() if v == sort), sort),
        "traveler_type": traveler_type.lower() if traveler_type else None,
        "query": query,
        "summary": parsed["summary"],
        "reviews": parsed["reviews"],
        "sort_options": parsed["sort_options"],
        "pagination": {"page": page, "items_per_page": limit, "has_next": has_next,
                       "total_pages": total_pages, "total_count": total_count},
    }


def property_reviews(**data):
    return cached_call("vrbo.reviews", data, _reviews, ttl.REVIEWS_CACHE)


# ---- properties/calendar ------------------------------------------------------------------

def _calendar(property, start_date, end_date):
    record = _record(property)
    parsed = parsers.parse_calendar(fetch_calendar(record["expedia_id"]), start_date, end_date)
    return {**_identity(record), "start_date": start_date, "end_date": end_date, **parsed}


def property_calendar(**data):
    return cached_call("vrbo.calendar", data, _calendar, ttl.CALENDAR_CACHE)


# ---- properties/price ---------------------------------------------------------------------

def _quote(property, checkin, checkout, adults, children):
    record = _record(property)
    offers = fetch_offer(record["expedia_id"], checkin, checkout, adults, children)
    return {**parsers.parse_offer(offers, checkin, checkout), "adults": adults, "children": children or []}


def _price(property, checkin, checkout, adults, children):
    return cached_call("vrbo.price", {"property": property, "checkin": checkin, "checkout": checkout,
                                      "adults": adults, "children": children or []}, _quote, ttl.PRICE_CACHE)


def property_price(property, checkin, checkout, adults=2, children=None):
    record = _record(property)
    return {**_identity(record), **_price(property, checkin, checkout, adults, children or [])}
