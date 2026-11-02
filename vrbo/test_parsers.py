"""Offline tests for the Vrbo parsers, reference resolvers and value
helpers.

    python -m pytest vrbo/test_parsers.py -q

Fixtures are real www.vrbo.com pages / GraphQL payloads captured
2026-10-30 (vrbo/fixtures/, listed in parsers.py's docstring). No network.
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from vrbo import parsers as P, refs  # noqa: E402
from vrbo import api  # noqa: E402
from vrbo.fetch import is_challenge, page_title  # noqa: E402

FX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


def fixture(name):
    with open(os.path.join(FX, name), encoding="utf-8") as f:
        return json.load(f) if name.endswith(".json") else f.read()


def gql(name, *path):
    data = fixture(name)
    data = data[0] if isinstance(data, list) else data
    data = data["data"]
    for key in path:
        data = data[key]
    return data


@pytest.fixture(scope="module")
def villa():
    return P.parse_property_page(fixture("property_2165911.html"))


@pytest.fixture(scope="module")
def bandung():
    return P.parse_property_page(fixture("property_4193579ha.html"))


# ---- refs ---------------------------------------------------------------------------------

@pytest.mark.parametrize("value,expected", [
    ("2165911", "2165911"),
    ("4193579ha", "4193579ha"),
    ("4193579HA", "4193579ha"),
    ("63066336", "63066336"),
    ("https://www.vrbo.com/2165911", "2165911"),
    ("https://www.vrbo.com/2165911?chkin=2026-12-04&chkout=2026-12-08&adults=2", "2165911"),
    ("https://www.vrbo.com/4193579ha/", "4193579ha"),
    ("https://www.vrbo.com/2430840?expediaPropertyId=70477069", "2430840"),
    ("https://www.vrbo.com/pdp/lo/63066336?dateless=true", "63066336"),
    ("https://www.vrbo.com/search?expediaPropertyId=70477069", "70477069"),
    ("//vrbo.com/2165911", "2165911"),
])
def test_resolve_property(value, expected):
    assert refs.resolve_property(value) == expected


@pytest.mark.parametrize("value", [
    "", "villa", "216", "2165911hb",
    "https://www.airbnb.com/rooms/2165911",
    "https://www.vrbo.com/search?destination=Miami",
])
def test_resolve_property_invalid(value):
    with pytest.raises(ValueError):
        refs.resolve_property(value)


@pytest.mark.parametrize("value,expected", [
    ("Miami, Florida", {"region_name": "Miami, Florida"}),
    ("  Lake   Tahoe ", {"region_name": "Lake Tahoe"}),
    ("2297", {"region_id": "2297"}),
    ("https://www.vrbo.com/search?destination=Miami&regionId=2297&adults=2", {"region_id": "2297"}),
    ("https://www.vrbo.com/search?destination=Miami%2C%20Florida&adults=2", {"region_name": "Miami, Florida"}),
    ("https://www.vrbo.com/search?latLong=25.79,-80.13", {"coordinates": {"latitude": 25.79, "longitude": -80.13}}),
    ("https://www.vrbo.com/search?mapBounds=25.76,-80.145&mapBounds=25.815,-80.115",
     {"bounds": [{"latitude": 25.76, "longitude": -80.145}, {"latitude": 25.815, "longitude": -80.115}]}),
])
def test_resolve_location(value, expected):
    assert refs.resolve_location(value) == expected


@pytest.mark.parametrize("value", ["", "https://example.com/search?destination=Miami", "https://www.vrbo.com/search?adults=2"])
def test_resolve_location_invalid(value):
    with pytest.raises(ValueError):
        refs.resolve_location(value)


def test_parse_bounds():
    assert refs.parse_bounds("25.76,-80.145,25.815,-80.115") == [
        {"latitude": 25.76, "longitude": -80.145}, {"latitude": 25.815, "longitude": -80.115}]
    for bad in ("1,2,3", "a,b,c,d", "26,-80,25,-80", "95,0,96,1"):
        with pytest.raises(ValueError):
            refs.parse_bounds(bad)


# ---- value helpers ------------------------------------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("$1,199", (1199.0, "USD")), ("A$981", (981.0, "AUD")), ("€45", (45.0, "EUR")), ("", (None, None)),
])
def test_money(text, expected):
    assert P._money(text) == expected


def test_summary_facts():
    facts = P._summary_facts(["Apartment · 1 bedroom · 1 Queen Bed", "9 min drive to the beach"])
    assert facts["property_type"] == "Apartment"
    assert facts["bedrooms"] == 1 and facts["beds"] == 1 and facts["bed_types"] == ["1 Queen Bed"]
    assert facts["distance_text"] == "9 min drive to the beach"
    facts = P._summary_facts(["5 bedrooms · 9 beds · Hot tub on site · Patio",
                              "Pay $0 today · Free cancellation until Nov 29 · Within Little Havana"])
    assert facts["property_type"] is None and facts["bedrooms"] == 5 and facts["beds"] == 9
    assert facts["amenity_highlights"] == ["Hot tub on site", "Patio"]
    assert facts["neighborhood"] == "Little Havana" and facts["is_pay_later"] is True
    assert facts["free_cancellation_text"] == "Free cancellation until Nov 29"


def test_price_lines_dateless_vs_dated():
    dateless = {"displayMessages": [
        {"lineItems": [{"__typename": "DisplayPrice", "role": "LEAD", "price": {"formatted": "$229"}}]},
        {"lineItems": [{"__typename": "LodgingEnrichedMessage", "value": "for 1 night"}]},
        {"lineItems": [{"__typename": "LodgingEnrichedMessage", "value": "All fees included"}]},
        {"lineItems": [{"__typename": "LodgingEnrichedMessage", "value": "Oct 31 - Nov 1"}]}]}
    out = P._price_lines(dateless)
    assert out == {"nightly": 229.0, "original_nightly": None, "total": None, "nights": 1, "currency": "USD",
                   "is_fees_included": True, "dates_text": "Oct 31 - Nov 1"}
    assert P._price_lines(None)["nightly"] is None


def test_page_helpers():
    assert page_title(fixture("challenge_bot_or_not.html")) == "Bot or Not?"
    assert is_challenge(fixture("challenge_bot_or_not.html"))
    assert not is_challenge(fixture("property_2165911.html"))
    assert P.apollo_state("<html></html>") is None
    assert P.apollo_state(fixture("property_shell_with_dates.html"))["ROOT_QUERY"]


# ---- listing page -------------------------------------------------------------------------

def test_property_identity(villa):
    assert villa["id"] == "2165911"
    assert villa["expedia_id"] == "63066336"
    assert villa["link"] == "https://www.vrbo.com/2165911"
    assert villa["name"].startswith("Piccolo Ponte 3 BR Caribbean Villa")
    assert villa["property_type"] == "Villa"
    assert villa["capacity"] == {"sleeps": 6, "bedrooms": 3, "beds": 4, "bathrooms": None}
    assert villa["max_adults"] == 6 and villa["max_children"] == 5


def test_property_rating_and_location(villa):
    assert villa["rating"] == {"score": 9.8, "max": 10, "label": "Loved by Guests", "review_count": 41,
                               "note": "Top 10% of guest reviews in this area"}
    loc = villa["location"]
    assert loc["city"] == "Leeward Settlement" and loc["region"] == "Caicos Islands" and loc["country_code"] == "TCA"
    assert loc["region_id"] == "2822"
    assert loc["coordinates"] == {"latitude": 21.816695, "longitude": -72.151978}
    assert loc["address"] == ["Leeward Settlement", "Caicos Islands"]
    assert loc["neighborhood_text"].startswith("Located on the waterfront")
    categories = [g["category"] for g in loc["nearby"]]
    assert categories == ["What's nearby", "Getting around", "Restaurants"]
    assert loc["nearby"][0]["places"][0] == {"name": "Providenciales Beaches", "distance": "1.4 km", "travel_time": "17 min walk"}
    assert loc["nearby"][2]["places"][0] == {"name": "Sharkie's Bar", "distance": None, "travel_time": "4 min drive"}


def test_property_content(villa):
    assert villa["description"].startswith("Piccolo Ponte is an elegant 3 bedroom waterfront")
    assert "No hidden fees." in villa["description"]
    assert villa["popular_amenities"][:3] == ["Pool", "Hot Tub", "Kitchen"]
    assert len(villa["amenities"]) == 15
    assert villa["amenities"][0]["category"] == "Essentials"
    assert "Free wifi" in villa["amenities"][0]["items"]
    assert [h["title"] for h in villa["highlights"]][:2] == ["Dive into relaxation", "Your own private chef"]
    assert len(villa["important_info"]) == 8
    assert not any("Property #" in t for t in villa["important_info"])


def test_property_rules_host_price_photos(villa):
    rules = villa["house_rules"]
    assert rules["check_in_after"] == "4:00 PM" and rules["check_out_before"] == "11:00 AM"
    assert {"topic": "Pets", "text": "No pets allowed"} in rules["rules"]
    assert rules["check_out_instructions"][1] == "Load and start the dishwasher"
    assert villa["host"]["name"] == "Janice" and villa["host"]["languages"] == ["English"]
    assert villa["host"]["is_premier_host"] is True and villa["host"]["image"].startswith("https://")
    assert villa["default_price"] == {"nightly": 1115.0, "total": 4460.0, "nights": 4, "currency": "USD",
                                      "is_fees_included": True, "dates_text": "Nov 25 - Nov 29"}
    assert len(villa["photos"]) == 46
    assert villa["photos"][0]["category"] == "Exterior" and villa["photos"][0]["thumbnail"].endswith("_b.jpg")
    assert villa["breadcrumbs"][2] == {"name": "Turks and Caicos",
                                       "link": "https://www.vrbo.com/vacation-rentals/caribbean/turks-and-caicos"}


def test_property_bandung(bandung):
    assert bandung["id"] == "4193579ha" and bandung["expedia_id"] == "38859744"
    assert bandung["property_type"] == "Vacation Home"
    assert bandung["capacity"]["sleeps"] == 10
    assert bandung["host"] == {"name": "YenYen A", "image": bandung["host"]["image"], "languages": ["English", "Indonesian"],
                               "is_premier_host": False, "premier_host_text": None, "about": None}
    assert bandung["default_price"]["nightly"] == 105.0 and bandung["default_price"]["total"] is None
    assert bandung["default_price"]["nights"] == 1
    assert bandung["location"]["city"] == "Bandung"
    assert {"topic": "Smoking", "text": "Smoking allowed: in designated areas"} in bandung["house_rules"]["rules"]


def test_property_page_without_record():
    assert P.parse_property_page(fixture("property_shell_with_dates.html")) is None
    assert P.parse_property_page(fixture("homepage_after_unknown_id_redirect.html")) is None
    assert P.parse_property_page("<html><title>x</title></html>") is None


# ---- search -------------------------------------------------------------------------------

def test_search_page():
    out = P.parse_search(gql("graphql_search_miami_page2.json", "propertySearch"))
    assert out["location"] == {"region_id": "2297", "name": "Miami, Florida, United States of America"}
    assert out["checkin"] == "2026-12-04" and out["checkout"] == "2026-12-08"
    assert out["total_count"] == 398 and out["has_next"] is True and out["next_start"] == 100
    assert len(out["results"]) == 50
    row = out["results"][0]
    assert row["id"] == "5469756" and row["expedia_id"] == "135641360"
    assert row["link"] == "https://www.vrbo.com/5469756"
    assert row["name"] == "Casita Havana Vieja with Hot Tub & Games"
    assert row["bedrooms"] == 5 and row["beds"] == 9
    assert row["neighborhood"] == "Little Havana" and row["is_pay_later"] is True
    assert row["rating"] == {"score": 9.4, "max": 10, "label": None, "review_count": None, "is_new": False}
    assert row["price"]["nightly"] == 780.0 and row["price"]["total"] == 3118.0 and row["price"]["nights"] == 4
    assert row["price"]["currency"] == "USD" and row["price"]["is_fees_included"] is True
    assert row["coordinates"] == {"latitude": 25.774118, "longitude": -80.228408}
    assert row["is_available"] is True and len(row["photos"]) == 4
    assert all(r["coordinates"] for r in out["results"])
    strike = next(r for r in out["results"] if r["price"]["original_nightly"])
    assert strike["price"]["original_nightly"] > strike["price"]["nightly"]
    assert strike["price"]["discount_badge"].startswith("New listing")
    rated = next(r for r in out["results"] if r["rating"]["review_count"])
    assert rated["rating"]["label"] and rated["rating"]["score"]


def test_search_filtered_and_past_end():
    out = P.parse_search(gql("graphql_search_miami_filtered.json", "propertySearch"))
    assert out["total_count"] == 5 and len(out["results"]) == 5 and out["has_next"] is False
    out = P.parse_search(gql("graphql_search_past_end.json", "propertySearch"))
    assert out["results"] == [] and out["has_next"] is False and out["total_count"] == 398
    assert P.parse_search({})["results"] == []


def test_search_dateless_free_text():
    out = P.parse_search(gql("graphql_search_bali_dateless.json", "propertySearch"))
    assert out["location"] == {"region_id": "602651", "name": "Bali, Indonesia"}
    assert out["checkin"] is None
    row = out["results"][0]
    assert row["price"]["nightly"] == 229.0 and row["price"]["total"] is None and row["price"]["nights"] == 1
    assert row["price"]["dates_text"] == "Oct 31 - Nov 1"
    assert row["rating"]["score"] is None
    assert any(r["id"].endswith("ha") for r in out["results"])


# ---- filters ------------------------------------------------------------------------------

def test_filters():
    out = P.parse_filters(P.apollo_state(fixture("search_page_miami_dated.html")))
    assert out["location"]["region_id"] == "2297" and out["total_count"] == 397
    ids = {f["id"]: f for f in out["filters"]}
    assert ids["amenities_facilities_group"]["type"] == "multi_select"
    pool = next(o for o in ids["amenities_facilities_group"]["options"] if o["value"] == "pool")
    assert pool == {"value": "pool", "label": "Pool", "count": 3030, "is_selected": False}
    assert ids["property_type_group"]["options"][0]["value"] == "house"
    assert ids["bedroom_count_gt"]["type"] == "minimum_count"
    price = ids["pricing_group"]
    assert price["type"] == "price_range" and price["options"][0]["value"] == "nightly_price"
    assert price["options"][0]["max_label"] == "$1,000+" and len(price["options"][0]["histogram"]) == 50
    assert [s["value"] for s in out["sort_options"]] == ["RECOMMENDED", "PRICE_LOW_TO_HIGH", "PRICE_HIGH_TO_LOW",
                                                         "REVIEW", "REVIEW_COUNT"]
    assert P.parse_filters({})["filters"] == []


# ---- reviews ------------------------------------------------------------------------------

def test_reviews():
    out = P.parse_reviews(gql("graphql_reviews_2165911.json", "propertyInfo", "reviewInfo"))
    assert out["summary"]["score"] == 9.8 and out["summary"]["review_count"] == 41
    assert out["summary"]["label"] == "Exceptional"
    assert out["summary"]["categories"][0] == {"label": "Cleanliness", "score": 10.0, "percentage": 100}
    assert len(out["reviews"]) == 41
    review = next(r for r in out["reviews"] if r["host_responses"])
    assert review["score"] == 10.0 and review["label"] == "Excellent" and review["title"] is None
    assert review["author"] == "Justin S." and review["date"] == "2026-06-21"
    assert review["stay"] == {"text": "Stayed 8 nights in Jun 2026", "nights": 8, "month": "2026-06"}
    assert review["is_verified"] is True and review["language"] == "en_US"
    assert review["host_responses"][0]["responder"] == "VrboOwner"
    assert review["host_responses"][0]["date"] == "2026-06-23"
    assert review["helpful_votes"] == 0
    assert [s["value"] for s in out["sort_options"]][2] == "NEWEST_TO_OLDEST"
    assert P.parse_reviews({})["reviews"] == []


# ---- calendar -----------------------------------------------------------------------------

def test_calendar():
    selector = gql("graphql_calendar_2165911.json", "propertyRatesDateSelector")
    out = P.parse_calendar(selector)
    assert out["range"]["from"] == "2026-10-01" and out["range"]["to"] == "2028-11-02"
    assert out["summary"]["days"] == 764 and out["summary"]["available_days"] == 290
    assert out["summary"]["min_price"] == 1075.0 and out["summary"]["currency"] == "USD"
    first = out["days"][0]
    assert first["date"] == "2026-10-01" and first["is_available"] is False and first["price"] is None
    assert first["check_in_blocked_by"] == "being_in_past" and first["min_stay"] == 4 and first["max_stay"] is None
    open_day = next(d for d in out["days"] if d["is_available"])
    assert open_day["price"] == 1123.0 and open_day["can_check_in"] is True
    assert open_day["check_out_blocked_by"] == "changeover_exclusion" and open_day["max_stay"] == 30
    narrowed = P.parse_calendar(selector, "2026-12-01", "2026-12-10")
    assert [d["date"] for d in narrowed["days"]][::9] == ["2026-12-01", "2026-12-10"]
    assert narrowed["summary"]["days"] == 10
    assert P.parse_calendar({})["days"] == []


# ---- offer --------------------------------------------------------------------------------

def test_offer_available():
    out = P.parse_offer(gql("graphql_offer_available.json", "propertyOffers"), "2026-12-04", "2026-12-08")
    assert out["is_available"] is True and out["message"] is None
    assert out["checkin"] == "2026-12-04" and out["checkout"] == "2026-12-08" and out["nights"] == 4
    assert out["price"] == {"nightly": 1199.0, "original_nightly": None, "total": 4794.0, "currency": "USD",
                            "is_fees_included": None}
    assert out["cancellation_policy"] == "Partially refundable before Wed, Nov 4"
    assert out["payment_model"] == "PAY_NOW" and out["is_card_required"] is True
    assert out["rate_plan_id"] == "00047d1f856d72194b3183536dad86996d89"


def test_offer_sold_out():
    out = P.parse_offer(gql("graphql_offer_sold_out.json", "propertyOffers"), "2026-11-01", "2026-11-02")
    assert out["is_available"] is False
    assert out["message"].startswith("This property is not available for your dates")
    assert out["price"]["nightly"] is None and out["nights"] == 1
    assert P.parse_offer({})["is_available"] is False


# ---- typeahead ----------------------------------------------------------------------------

def test_autocomplete():
    out = P.parse_autocomplete(fixture("typeahead_miami.json"))
    assert len(out) == 10
    assert out[0] == {"region_id": "2297", "name": "Miami, Florida, United States of America", "short_name": "Miami",
                      "type": "city", "country": {"name": "United States", "code": "US"},
                      "coordinates": {"latitude": 25.77178, "longitude": -80.19009}, "is_major_airport": None}
    airport = next(o for o in out if o["type"] == "airport")
    assert airport["is_major_airport"] is True
    assert P.parse_autocomplete({}) == []


# ---- api helpers --------------------------------------------------------------------------

def test_criteria_tokens():
    selections, ranges, booleans = api._criteria({
        "amenities": ["Pool", "hot tub"], "property_type": ["rv", "house"], "guest_rating": "4_stars",
        "premier_host": True, "bedrooms": 2, "min_price": 100.0, "max_price": None, "price_type": "nightly_price",
        "vacation_rentals_only": True})
    assert {"id": "amenities_facilities_group", "value": "hot_tub"} in selections
    assert {"id": "property_type_group", "value": "RV"} in selections
    assert {"id": "property_reviews_group", "value": "4_stars"} in selections
    assert {"id": "premier_host_group", "value": "premier_host"} in selections
    assert {"id": "bedroom_count_gt", "value": "2"} in selections
    assert ranges == [{"id": "nightly_price", "min": 100, "max": -2}]
    assert booleans == [{"id": "showVacationRentalsOnly", "value": True}]


def test_destination():
    assert api._destination({"region_id": "2297"}, None, None, None) == {"regionId": "2297"}
    assert api._destination({"region_name": "Bali"}, None, None, None) == {"regionName": "Bali"}
    assert api._destination(None, 25.79, -80.13, None) == {"coordinates": {"latitude": 25.79, "longitude": -80.13}}
    bounds = [{"latitude": 25.76, "longitude": -80.145}, {"latitude": 25.815, "longitude": -80.115}]
    assert api._destination(None, None, None, bounds) == {"mapBounds": bounds}


def test_card_generic_heading_variant():
    """The "<Type> in <City>" heading variant: the name comes from the map
    marker (or the photo caption) and the heading yields type + city."""
    card = {"__typename": "LodgingCard", "id": "113891154",
            "headingSection": {"heading": "Apartment in Miami", "messages": [{"text": "2 bedrooms · 3 beds · AC"}]},
            "cardLink": {"resource": {"value": "https://www.vrbo.com/4482100?chkin=2026-12-04"}},
            "mediaSection": {"gallery": {"media": [{"media": {"url": "https://media.vrbo.com/x.jpg",
                                                              "description": "Image of Trendy Brickell Apt 5"}}]}}}
    row = P.parse_card(card, {"113891154": {"id": "113891154", "name": "Trendy Brickell Apt 5", "markerStatus": "AVAILABLE",
                                            "markerPosition": {"latitude": 25.77, "longitude": -80.21}}})
    assert row["name"] == "Trendy Brickell Apt 5" and row["property_type"] == "Apartment" and row["city"] == "Miami"
    assert row["id"] == "4482100" and row["bedrooms"] == 2 and row["is_available"] is True
    row = P.parse_card(card, {})
    assert row["name"] == "Trendy Brickell Apt 5" and row["city"] == "Miami" and row["coordinates"] is None
    named = dict(card, headingSection={"heading": "Casita Havana Vieja", "messages": []})
    row = P.parse_card(named, {})
    assert row["name"] == "Trendy Brickell Apt 5" and row["city"] is None and row["property_type"] is None


def test_block_alternates_fingerprint_before_the_fallback(monkeypatch):
    """A block moves the thread to the next impersonation target; the
    residential fallback starts only after every target was refused on the
    direct tier."""
    from vrbo import fetch

    class FakeSession:
        def __init__(self, proxy=None):
            self.vrbo_proxy = proxy

        def close(self):
            pass

    monkeypatch.setattr(fetch.config, "vrbo_proxy", lambda: None)
    monkeypatch.setattr(fetch.config, "VRBO_BLOCK_COOLDOWN", 600)
    monkeypatch.setattr(fetch, "_fallback_until", 0.0)
    fetch._local.target_index = 0
    fetch._local.session = FakeSession()
    fetch._drop_session(blocked=True)
    assert fetch._local.target_index == 1 and fetch._fallback_until == 0.0
    fetch._local.session = FakeSession()
    fetch._drop_session(blocked=True)
    assert fetch._local.target_index == 2 and fetch._fallback_until > 0
    fetch._local.target_index = 0
    fetch._local.session = None


def test_card_review_count_from_accessibility_or_subtexts():
    base = {"__typename": "LodgingCard", "id": "1", "headingSection": {"heading": "Beach House"},
            "cardLink": {"resource": {"value": "https://www.vrbo.com/123"}}}
    compact = dict(base, summarySections=[{"reviewSummary": {
        "graphic": {"text": "9.6"},
        "title": {"shoppingProductTitle": {"text": "Exceptional", "accessibility": "exceptional, (173 reviews)"}}}}])
    assert P.parse_card(compact)["rating"]["review_count"] == 173
    sub = dict(base, summarySections=[{"reviewSummary": {
        "graphic": {"text": "9.6"}, "title": {"shoppingProductTitle": {"text": "Exceptional"}},
        "subtexts": [{"shoppingProductTitle": {"text": "1,204 reviews"}}]}}])
    assert P.parse_card(sub)["rating"]["review_count"] == 1204
    bare = dict(base, summarySections=[{"reviewSummary": {"graphic": {"text": "9.6"},
                                                          "title": {"shoppingProductTitle": {"text": "Exceptional"}}}}])
    assert P.parse_card(bare)["rating"] == {"score": 9.6, "max": 10, "label": "Exceptional", "review_count": None, "is_new": False}
