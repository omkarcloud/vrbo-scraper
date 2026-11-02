"""Marshmallow request schemas for every /vrbo/* route.

ONE param per input (tripadvisor QueryOrIdField convention, never a sibling
`url` / `id` pair):

    property     Vrbo listing id ("2165911", "4193579ha"), Expedia property
                 id ("63066336") OR any vrbo.com property link
    location     destination name, Vrbo region id OR a vrbo.com search link
    latitude + longitude / bounds   the two other ways to pick a search area

Filter tokens are vrbo.com's own (vrbo/refs.py; the live list per
destination comes from /vrbo/properties/filters).
"""
from datetime import date, datetime

from marshmallow import ValidationError, fields, missing, validate, validates_schema

from schema_fields import (BaseSchema, ChoiceField, CommaListField, DateField, Flag, LimitField,
                           PageField, PageSizeField, PositiveInt, Price, QueryField, RefField,
                           StrippedString)
from vrbo import refs
from vrbo.fetch import REVIEWS_MAX_SIZE, SEARCH_PAGE_SIZE

MAX_SEARCH_PAGE = 20      # the site caps a destination at ~400 results (8 pages of 50)
MAX_REVIEW_PAGE = 500
MAX_NIGHTS = 28           # the site's own stay limit
MAX_ADULTS = 20
MAX_CHILDREN = 8


class PropertyRefField(RefField):
    resolver = staticmethod(refs.resolve_property)


class LocationRefField(RefField):
    resolver = staticmethod(refs.resolve_location)


class BoundsField(StrippedString):
    """'south_lat,west_lng,north_lat,east_lng' -> [sw, ne] coordinates."""

    def __init__(self, **kwargs):
        kwargs.setdefault("required", False)
        kwargs.setdefault("load_default", None)
        super().__init__(**kwargs)

    def _deserialize(self, value, attr, data, **kwargs):
        value = super()._deserialize(value, attr, data, **kwargs)
        if value is None:
            return None
        try:
            return refs.parse_bounds(value)
        except ValueError as e:
            raise ValidationError(str(e))


class ChildAgesField(CommaListField):
    """Comma-separated child ages (0-17), one per child, up to 8."""

    def __init__(self, **kwargs):
        super().__init__(upper=False, max_items=MAX_CHILDREN, **kwargs)

    def _deserialize(self, value, attr, data, **kwargs):
        items = super()._deserialize(value, attr, data, **kwargs)
        if items is None:
            return None
        ages = []
        for item in items:
            if not item.isdigit() or not 0 <= int(item) <= 17:
                raise ValidationError("Child ages must be whole numbers from 0 to 17 (e.g. 5,9).")
            ages.append(int(item))
        return ages


class Coordinate(fields.Float):
    def __init__(self, limit, **kwargs):
        kwargs.setdefault("load_default", None)
        kwargs.setdefault("validate", validate.Range(min=-limit, max=limit))
        super().__init__(**kwargs)


def _check_stay(data, required=False):
    checkin, checkout = data.get("checkin"), data.get("checkout")
    if required and not (checkin and checkout):
        raise ValidationError("checkin and checkout are both required.", "checkin")
    if bool(checkin) != bool(checkout):
        raise ValidationError("checkin and checkout go together.", "checkout" if checkin else "checkin")
    if checkin and checkout:
        nights = (date.fromisoformat(checkout) - date.fromisoformat(checkin)).days
        if nights < 1:
            raise ValidationError("checkout must be after checkin.", "checkout")
        if nights > MAX_NIGHTS:
            raise ValidationError(f"Stays are at most {MAX_NIGHTS} nights on Vrbo.", "checkout")


_CHECKIN = dict(bound="future", metadata={"description": "check-in date, YYYY-MM-DD (today or later)"})
_CHECKOUT = dict(bound="future", metadata={"description": "check-out date, YYYY-MM-DD, at most 28 nights after check-in"})
_ADULTS = dict(max_value=MAX_ADULTS, load_default=2, metadata={"description": "adult guests (default 2)"})
_CHILDREN = dict(metadata={"description": "child ages, comma-separated, one per child (e.g. 5,9)"})


# ---- locations/autocomplete -------------------------------------------------------------

class AutocompleteSchema(BaseSchema):
    query = QueryField(max_length=100, metadata={
        "description": "place name as a traveler would type it (Miami, Lake Tahoe, Bali)"})
    limit = LimitField(default=10, max_size=20, metadata={"description": "suggestions to return, 1-20"})


# ---- properties/search ------------------------------------------------------------------

class SearchSchema(BaseSchema):
    location = LocationRefField(required=False, load_default=None, metadata={
        "description": "destination name (Miami, Florida), a Vrbo region id from "
                       "/vrbo/locations/autocomplete, or a vrbo.com search link"})
    latitude = Coordinate(90, metadata={"description": "search around this point (with longitude) instead of a location"})
    longitude = Coordinate(180, metadata={"description": "search around this point (with latitude)"})
    bounds = BoundsField(metadata={
        "description": "map area to search instead of a location: south_lat,west_lng,north_lat,east_lng"})
    checkin = DateField(**_CHECKIN)
    checkout = DateField(**_CHECKOUT)
    adults = PositiveInt(**_ADULTS)
    children = ChildAgesField(**_CHILDREN)
    sort = ChoiceField(refs.SORTS, load_default="RECOMMENDED", metadata={
        "description": "recommended (default), price_low_to_high, price_high_to_low, guest_rating, review_count"})
    page = PageField(max_page=MAX_SEARCH_PAGE, metadata={"description": "result page, 50 per page"})
    min_price = Price(metadata={"description": "lowest price to keep (USD)"})
    max_price = Price(metadata={"description": "highest price to keep (USD)"})
    price_type = ChoiceField(refs.PRICE_TYPES, load_default="nightly_price", metadata={
        "description": "what min_price / max_price compare against: nightly (default) or total for the stay"})
    bedrooms = PositiveInt(max_value=50, metadata={"description": "minimum bedrooms"})
    bathrooms = PositiveInt(max_value=50, metadata={"description": "minimum bathrooms"})
    beds = PositiveInt(max_value=50, metadata={"description": "minimum beds"})
    property_type = CommaListField(upper=False, metadata={
        "description": "comma-separated property types, Vrbo's own tokens (house, apartment_or_condo, "
                       "villa, cabin, cottage, estate, hotel, resort, boat, ...); see /vrbo/properties/filters"})
    amenities = CommaListField(upper=False, max_items=20, metadata={
        "description": "comma-separated amenity tokens every result must have (pool, hot_tub, "
                       "air_conditioning, internet_available, washer, fireplace, ...); see /vrbo/properties/filters"})
    house_rules = CommaListField(upper=False, metadata={
        "description": "comma-separated house-rule tokens (pets_allowed, pet_free, children_allowed, "
                       "events_allowed, smoking_allowed)"})
    location_type = CommaListField(upper=False, metadata={
        "description": "comma-separated setting tokens (oceanfront, waterfront, downtown_location, "
                       "lake_location, golf_course, mountains_location, rural_location, village_location, skiin_skiout)"})
    guest_rating = ChoiceField(refs.GUEST_RATINGS, metadata={
        "description": "minimum guest rating: 9+, 8+, 7+ or loved_by_guests"})
    neighborhood = CommaListField(upper=False, metadata={
        "description": "comma-separated neighborhood region ids (from /vrbo/properties/filters)"})
    premier_host = Flag(metadata={"description": "true keeps only Premier Host listings"})
    instant_confirmation = Flag(metadata={"description": "true keeps only instantly bookable listings"})
    vacation_rentals_only = Flag(metadata={"description": "true drops hotels and resorts"})

    @validates_schema
    def _one_area(self, data, **kwargs):
        has_point = data.get("latitude") is not None or data.get("longitude") is not None
        if has_point and (data.get("latitude") is None or data.get("longitude") is None):
            raise ValidationError("latitude and longitude go together.", "latitude")
        ways = sum(1 for present in (data.get("location"), has_point, data.get("bounds")) if present)
        if ways == 0:
            raise ValidationError("Give a location, or latitude + longitude, or bounds.", "location")
        if ways > 1:
            raise ValidationError("Give only one of location, latitude + longitude, or bounds.", "location")
        _check_stay(data)
        lo, hi = data.get("min_price"), data.get("max_price")
        if lo is not None and hi is not None and lo > hi:
            raise ValidationError("min_price must not exceed max_price.", "max_price")


# ---- properties/filters -----------------------------------------------------------------

class FiltersSchema(BaseSchema):
    location = LocationRefField(metadata={
        "description": "destination name, a Vrbo region id, or a vrbo.com search link"})
    checkin = DateField(**_CHECKIN)
    checkout = DateField(**_CHECKOUT)
    adults = PositiveInt(**_ADULTS)

    @validates_schema
    def _stay(self, data, **kwargs):
        _check_stay(data)


# ---- properties/details -----------------------------------------------------------------

_PROPERTY = dict(metadata={
    "description": "Vrbo listing id (2165911, 4193579ha), Expedia property id (63066336) or a vrbo.com property link"})


class PropertyDetailsSchema(BaseSchema):
    property = PropertyRefField(**_PROPERTY)
    checkin = DateField(**_CHECKIN)
    checkout = DateField(**_CHECKOUT)
    adults = PositiveInt(**_ADULTS)
    children = ChildAgesField(**_CHILDREN)

    @validates_schema
    def _stay(self, data, **kwargs):
        _check_stay(data)


# ---- properties/reviews -----------------------------------------------------------------

class PropertyReviewsSchema(BaseSchema):
    property = PropertyRefField(**_PROPERTY)
    page = PageField(max_page=MAX_REVIEW_PAGE, metadata={"description": "review page"})
    limit = PageSizeField(default=20, max_size=REVIEWS_MAX_SIZE, metadata={
        "description": "reviews per page, 1-50 (default 20)"})
    sort = ChoiceField(refs.REVIEW_SORTS, load_default="NEWEST_TO_OLDEST", metadata={
        "description": "newest (default), highest_rating, lowest_rating or relevant"})
    traveler_type = ChoiceField(refs.REVIEW_TRAVELER_TYPES, metadata={
        "description": "only reviews by this kind of traveler: family, couple, group, solo, business, pets"})
    query = StrippedString(required=False, load_default=None, validate=validate.Length(max=100), metadata={
        "description": "only reviews mentioning this word or phrase"})


# ---- properties/calendar ----------------------------------------------------------------

class PropertyCalendarSchema(BaseSchema):
    property = PropertyRefField(**_PROPERTY)
    start_date = DateField(metadata={"description": "first day to return, YYYY-MM-DD (default: the calendar's start, today)"})
    end_date = DateField(metadata={"description": "last day to return, YYYY-MM-DD (default: the calendar's end, ~2 years out)"})

    @validates_schema
    def _order(self, data, **kwargs):
        start, end = data.get("start_date"), data.get("end_date")
        if start and end and end < start:
            raise ValidationError("end_date must not be before start_date.", "end_date")


# ---- properties/price -------------------------------------------------------------------

class PropertyPriceSchema(BaseSchema):
    property = PropertyRefField(**_PROPERTY)
    checkin = DateField(required=True, load_default=missing, **_CHECKIN)
    checkout = DateField(required=True, load_default=missing, **_CHECKOUT)
    adults = PositiveInt(**_ADULTS)
    children = ChildAgesField(**_CHILDREN)

    @validates_schema
    def _stay(self, data, **kwargs):
        _check_stay(data, required=True)
