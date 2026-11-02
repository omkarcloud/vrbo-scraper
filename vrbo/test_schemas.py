"""Offline tests for the /vrbo/* request schemas.

    python -m pytest vrbo/test_schemas.py -q
"""
import os
import sys
from datetime import date, timedelta

import pytest
from marshmallow import ValidationError

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from vrbo import schemas  # noqa: E402

IN = (date.today() + timedelta(days=30)).isoformat()
OUT = (date.today() + timedelta(days=34)).isoformat()


def test_autocomplete_schema():
    assert schemas.AutocompleteSchema().load({"query": " Miami "}) == {"query": "Miami", "limit": 10}
    with pytest.raises(ValidationError):
        schemas.AutocompleteSchema().load({})
    with pytest.raises(ValidationError):
        schemas.AutocompleteSchema().load({"query": "x", "limit": "21"})


def test_search_schema_location_forms():
    loaded = schemas.SearchSchema().load({"location": "Miami, Florida"})
    assert loaded["location"] == {"region_name": "Miami, Florida"}
    assert loaded["sort"] == "RECOMMENDED" and loaded["page"] == 1 and loaded["adults"] == 2
    assert loaded["price_type"] == "nightly_price" and loaded["checkin"] is None
    assert schemas.SearchSchema().load({"location": "2297"})["location"] == {"region_id": "2297"}
    loaded = schemas.SearchSchema().load({"location": "https://www.vrbo.com/search?regionId=2297&destination=Miami"})
    assert loaded["location"] == {"region_id": "2297"}
    loaded = schemas.SearchSchema().load({"latitude": "25.79", "longitude": "-80.13"})
    assert loaded["latitude"] == 25.79 and loaded["longitude"] == -80.13 and loaded["location"] is None
    loaded = schemas.SearchSchema().load({"bounds": "25.76,-80.145,25.815,-80.115"})
    assert loaded["bounds"][1] == {"latitude": 25.815, "longitude": -80.115}


def test_search_schema_filters():
    loaded = schemas.SearchSchema().load({
        "location": "Miami", "checkin": IN, "checkout": OUT, "adults": "4", "children": "5, 9",
        "sort": "Price_Low_To_High", "page": "3", "min_price": "100", "max_price": "400", "price_type": "total",
        "bedrooms": "2", "amenities": "pool,hot_tub", "property_type": "house,villa", "house_rules": "pets_allowed",
        "guest_rating": "8+", "neighborhood": "6240961", "premier_host": "yes", "instant_confirmation": "0",
        "vacation_rentals_only": "true"})
    assert loaded["children"] == [5, 9] and loaded["adults"] == 4
    assert loaded["sort"] == "PRICE_LOW_TO_HIGH" and loaded["page"] == 3
    assert loaded["min_price"] == 100.0 and loaded["price_type"] == "total_price"
    assert loaded["amenities"] == ["pool", "hot_tub"] and loaded["property_type"] == ["house", "villa"]
    assert loaded["guest_rating"] == "4_stars" and loaded["neighborhood"] == ["6240961"]
    assert loaded["premier_host"] is True and loaded["instant_confirmation"] is False
    assert loaded["vacation_rentals_only"] is True


@pytest.mark.parametrize("query,field", [
    ({}, "location"),
    ({"location": "Miami", "latitude": "25"}, "latitude"),
    ({"location": "Miami", "latitude": "25", "longitude": "-80"}, "location"),
    ({"location": "Miami", "bounds": "25.76,-80.145,25.815,-80.115"}, "location"),
    ({"location": "Miami", "checkin": IN}, "checkout"),
    ({"location": "Miami", "checkin": OUT, "checkout": IN}, "checkout"),
    ({"location": "Miami", "checkin": IN, "checkout": (date.today() + timedelta(days=70)).isoformat()}, "checkout"),
    ({"location": "Miami", "checkin": "2020-01-01", "checkout": "2020-01-03"}, "checkin"),
    ({"location": "Miami", "children": "5,19"}, "children"),
    ({"location": "Miami", "children": "a"}, "children"),
    ({"location": "Miami", "sort": "cheapest"}, "sort"),
    ({"location": "Miami", "guest_rating": "5+"}, "guest_rating"),
    ({"location": "Miami", "page": "21"}, "page"),
    ({"location": "Miami", "min_price": "500", "max_price": "100"}, "max_price"),
    ({"location": "https://www.airbnb.com/s/Miami"}, "location"),
    ({"bounds": "1,2,3"}, "bounds"),
    ({"location": "Miami", "url": "x"}, "url"),
])
def test_search_schema_rejects(query, field):
    with pytest.raises(ValidationError) as err:
        schemas.SearchSchema().load(query)
    assert field in err.value.messages


def test_filters_schema():
    loaded = schemas.FiltersSchema().load({"location": "Miami, Florida", "checkin": IN, "checkout": OUT})
    assert loaded == {"location": {"region_name": "Miami, Florida"}, "checkin": IN, "checkout": OUT, "adults": 2}
    with pytest.raises(ValidationError):
        schemas.FiltersSchema().load({})
    with pytest.raises(ValidationError):
        schemas.FiltersSchema().load({"location": "Miami", "checkin": IN})


def test_property_details_schema():
    loaded = schemas.PropertyDetailsSchema().load({"property": "https://www.vrbo.com/2165911?adults=4"})
    assert loaded == {"property": "2165911", "checkin": None, "checkout": None, "adults": 2, "children": None}
    loaded = schemas.PropertyDetailsSchema().load({"property": "4193579HA", "checkin": IN, "checkout": OUT, "children": "3"})
    assert loaded["property"] == "4193579ha" and loaded["children"] == [3]
    assert schemas.PropertyDetailsSchema().load({"property": "63066336"})["property"] == "63066336"
    for bad in ({}, {"property": "villa"}, {"property": "https://example.com/2165911"},
                {"property": "2165911", "checkout": OUT}, {"property": "2165911", "id": "2165911"}):
        with pytest.raises(ValidationError):
            schemas.PropertyDetailsSchema().load(bad)


def test_reviews_schema():
    loaded = schemas.PropertyReviewsSchema().load({"property": "2165911"})
    assert loaded == {"property": "2165911", "page": 1, "limit": 20, "sort": "NEWEST_TO_OLDEST",
                      "traveler_type": None, "query": None}
    loaded = schemas.PropertyReviewsSchema().load({"property": "2165911", "sort": "Highest_Rating", "traveler_type": "Family",
                                                   "query": " pool ", "limit": "50", "page": "2"})
    assert loaded["sort"] == "HIGHEST_TO_LOWEST_RATED" and loaded["traveler_type"] == "FAMILY"
    assert loaded["query"] == "pool" and loaded["limit"] == 50 and loaded["page"] == 2
    for bad in ({"property": "2165911", "limit": "51"}, {"property": "2165911", "sort": "oldest"},
                {"property": "2165911", "traveler_type": "robots"}):
        with pytest.raises(ValidationError):
            schemas.PropertyReviewsSchema().load(bad)


def test_calendar_schema():
    loaded = schemas.PropertyCalendarSchema().load({"property": "2165911", "start_date": "2026-12-01", "end_date": "2026-12-31"})
    assert loaded == {"property": "2165911", "start_date": "2026-12-01", "end_date": "2026-12-31"}
    assert schemas.PropertyCalendarSchema().load({"property": "2165911"})["start_date"] is None
    with pytest.raises(ValidationError) as err:
        schemas.PropertyCalendarSchema().load({"property": "2165911", "start_date": "2026-12-31", "end_date": "2026-12-01"})
    assert "end_date" in err.value.messages
    with pytest.raises(ValidationError):
        schemas.PropertyCalendarSchema().load({"property": "2165911", "start_date": "12/01/2026"})


def test_price_schema():
    loaded = schemas.PropertyPriceSchema().load({"property": "2165911", "checkin": IN, "checkout": OUT, "adults": "3"})
    assert loaded == {"property": "2165911", "checkin": IN, "checkout": OUT, "adults": 3, "children": None}
    with pytest.raises(ValidationError) as err:
        schemas.PropertyPriceSchema().load({"property": "2165911"})
    assert "checkin" in err.value.messages
    with pytest.raises(ValidationError):
        schemas.PropertyPriceSchema().load({"property": "2165911", "checkin": IN, "checkout": IN})
    with pytest.raises(ValidationError):
        schemas.PropertyPriceSchema().load({"property": "2165911", "checkin": IN, "checkout": OUT, "adults": "0"})
