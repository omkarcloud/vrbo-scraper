"""Reference resolvers + the site's filter / sort vocabulary for /vrbo/*.

ONE param per input (tripadvisor QueryOrIdField convention, never a
sibling `url` / `id` pair):

  * `property`   the listing id as it appears in a vrbo.com URL
                 ("2165911", "4193579ha"), Expedia's numeric property id
                 for the same listing ("63066336", the id GraphQL and the
                 search cards use), OR any vrbo.com property link
                 (https://www.vrbo.com/2165911?chkin=…,
                 https://www.vrbo.com/pdp/lo/63066336, a link carrying
                 ?expediaPropertyId=…). A bare number is taken as the
                 listing id first and as an Expedia id when no listing
                 page answers to it (vrbo/fetch.py tries both).
  * `location`   a destination name ("Miami, Florida"), a Vrbo region id
                 ("2297", from /vrbo/locations/autocomplete) OR a vrbo.com
                 search link, whose destination / regionId / latLong /
                 mapBounds are taken over.

Resolvers raise ValueError with a user-facing message (RefField -> 400).

Two ids, one listing (verified 2026-10-30): the public URL carries the
Vrbo listing id; the Expedia property id drives GraphQL and is what a
search card's `id` holds. `/pdp/lo/<expediaId>` 302s to `/<listingId>`,
and the bare listing page server-renders `PropertyInfo:<expediaId>`.

Filter tokens are vrbo.com's own (the search page's universalSortAndFilter
groups, captured 2026-10-30 on a Miami search): the values below are
what the site accepts in the criteria selections; a token the site does
not know is ignored upstream, never an error.
"""
import re
from urllib.parse import parse_qs, urlparse

SITE = "https://www.vrbo.com"

LISTING_ID_RE = re.compile(r"^[0-9]{4,10}(?:ha)?$", re.I)
EXPEDIA_ID_RE = re.compile(r"^[0-9]{4,12}$")
REGION_ID_RE = re.compile(r"^[0-9]{1,20}$")

_LINK_LISTING_RE = re.compile(r"^/([0-9]{4,10}(?:ha)?)(?:/|$)", re.I)
_LINK_EXPEDIA_RE = re.compile(r"^/pdp/lo/([0-9]{4,12})(?:/|$)", re.I)


def _host_is_vrbo(url):
    host = (urlparse(url).hostname or "").lower()
    return host == "vrbo.com" or host.endswith(".vrbo.com")


def _as_url(value):
    if value.startswith("//"):
        return "https:" + value
    return value


def is_link(value):
    return (value or "").startswith(("http://", "https://", "//"))


def resolve_property(value):
    """Bare listing id / Expedia id or a vrbo.com property link -> the id
    string vrbo/fetch.py resolves ("2165911", "4193579ha" or an Expedia
    id such as "63066336")."""
    value = (value or "").strip()
    if LISTING_ID_RE.match(value):
        return value.lower()
    if is_link(value):
        url = _as_url(value)
        if not _host_is_vrbo(url):
            raise ValueError("Must be a vrbo.com link.")
        parsed = urlparse(url)
        query = parse_qs(parsed.query)
        expedia = (query.get("expediaPropertyId") or [None])[0]
        match = _LINK_LISTING_RE.match(parsed.path)
        if match:
            return match.group(1).lower()
        match = _LINK_EXPEDIA_RE.match(parsed.path)
        if match:
            return match.group(1)
        if expedia and EXPEDIA_ID_RE.match(expedia):
            return expedia
        raise ValueError("Must be a vrbo.com property link "
                         "(https://www.vrbo.com/2165911 or https://www.vrbo.com/pdp/lo/63066336).")
    raise ValueError("Must be a Vrbo listing id (2165911, 4193579ha), an Expedia "
                     "property id (63066336) or a vrbo.com property link.")


def resolve_location(value):
    """Destination text / region id / vrbo.com search link -> a dict with
    exactly one of `region_name`, `region_id`, `coordinates`
    ({latitude, longitude}) or `bounds` ([sw, ne] as {latitude,
    longitude})."""
    value = " ".join((value or "").split())
    if not value:
        raise ValueError("Must not be empty.")
    if REGION_ID_RE.match(value):
        return {"region_id": value}
    if is_link(value):
        url = _as_url(value)
        if not _host_is_vrbo(url):
            raise ValueError("Must be a vrbo.com link.")
        query = parse_qs(urlparse(url).query)
        first = lambda key: (query.get(key) or [None])[0]   # noqa: E731
        region_id = first("regionId")
        if region_id and REGION_ID_RE.match(region_id):
            return {"region_id": region_id}
        bounds = query.get("mapBounds") or []
        if len(bounds) == 2:
            try:
                sw, ne = (tuple(float(x) for x in b.split(",")) for b in bounds)
                return {"bounds": [{"latitude": sw[0], "longitude": sw[1]},
                                   {"latitude": ne[0], "longitude": ne[1]}]}
            except (TypeError, ValueError):
                pass
        lat_long = first("latLong")
        if lat_long and "," in lat_long:
            try:
                lat, lng = (float(x) for x in lat_long.split(",", 1))
                return {"coordinates": {"latitude": lat, "longitude": lng}}
            except ValueError:
                pass
        destination = first("destination")
        if destination and destination.strip():
            return {"region_name": " ".join(destination.split())}
        raise ValueError("Must be a vrbo.com search link with a destination, regionId, "
                         "latLong or mapBounds.")
    return {"region_name": value}


def parse_bounds(value):
    """'sw_lat,sw_lng,ne_lat,ne_lng' -> [sw, ne] as {latitude, longitude}."""
    parts = [p.strip() for p in str(value or "").split(",")]
    if len(parts) != 4:
        raise ValueError("Must be four numbers: south_lat,west_lng,north_lat,east_lng.")
    try:
        sw_lat, sw_lng, ne_lat, ne_lng = (float(p) for p in parts)
    except ValueError:
        raise ValueError("Must be four numbers: south_lat,west_lng,north_lat,east_lng.")
    for lat in (sw_lat, ne_lat):
        if not -90 <= lat <= 90:
            raise ValueError("Latitudes must be between -90 and 90.")
    for lng in (sw_lng, ne_lng):
        if not -180 <= lng <= 180:
            raise ValueError("Longitudes must be between -180 and 180.")
    if sw_lat >= ne_lat:
        raise ValueError("south_lat must be below north_lat.")
    return [{"latitude": sw_lat, "longitude": sw_lng}, {"latitude": ne_lat, "longitude": ne_lng}]


def property_link(listing_id):
    return f"{SITE}/{listing_id}"


def expedia_link(expedia_id):
    return f"{SITE}/pdp/lo/{expedia_id}"


# ---- search vocabulary (the site's own tokens) -------------------------------------

SORTS = {
    "recommended": "RECOMMENDED",
    "price_low_to_high": "PRICE_LOW_TO_HIGH",
    "price_high_to_low": "PRICE_HIGH_TO_LOW",
    "guest_rating": "REVIEW",
    "review_count": "REVIEW_COUNT",
}

GUEST_RATINGS = {
    "loved_by_guests": "loved_by_guests",
    "9+": "45_stars",
    "8+": "4_stars",
    "7+": "35_stars",
}

PRICE_TYPES = {"nightly": "nightly_price", "total": "total_price"}

# criteria selection ids the site's URL -> criteria mapper uses
# (shared-ui-shopping-search-criteria-providers bundle, 2026-10-30)
AMENITIES_GROUP = "amenities_facilities_group"
PROPERTY_TYPE_GROUP = "property_type_group"
HOUSE_RULES_GROUP = "house_rules_group"
GUEST_RATING_GROUP = "property_reviews_group"
NEIGHBORHOOD_GROUP = "neighborhood_group"
PREMIER_HOST_GROUP = "premier_host_group"
BOOKING_OPTIONS_GROUP = "booking_options_group"
LOCATION_GROUP = "location_group"
MIN_BEDROOMS = "bedroom_count_gt"
MIN_BATHROOMS = "us_bathroom_count_gt"
MIN_BEDS = "bed_count_ge"

REVIEW_SORTS = {
    "newest": "NEWEST_TO_OLDEST",
    "highest_rating": "HIGHEST_TO_LOWEST_RATED",
    "lowest_rating": "LOWEST_TO_HIGHEST_RATED",
    "relevant": "NEWEST_TO_OLDEST_BY_LANGUAGE",
}

# the review overlay's traveler-type filter values (the site's own enum)
REVIEW_TRAVELER_TYPES = {
    "family": "FAMILY", "couple": "COUPLE", "group": "GROUP", "solo": "SOLO",
    "business": "BUSINESS", "pets": "PETS",
}
