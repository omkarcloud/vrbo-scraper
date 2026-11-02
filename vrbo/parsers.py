"""Raw vrbo.com pages / GraphQL payloads -> clean dicts. Pure functions,
no I/O.

Fixtures (vrbo/fixtures/, captured 2026-10-30 from www.vrbo.com with
curl_cffi chrome):
  property_2165911.html            bare listing page (Providenciales villa,
                                   41 reviews, 46 photos, Premier Host)
  property_4193579ha.html          bare listing page with an "ha" id (Bandung)
  property_shell_with_dates.html   the client-rendered shell a listing URL
                                   with a query string answers (no PropertyInfo)
  homepage_after_unknown_id_redirect.html  where an unknown id 302s to
  challenge_bot_or_not.html        Expedia's "Bot or Not?" captcha page (HTTP 429)
  search_page_miami_dated.html     server-rendered /search page 1 (filters)
  graphql_search_miami_page2.json  propertySearch, 50 cards + 50 markers
  graphql_search_miami_filtered.json  5 matches, pagination null
  graphql_search_past_end.json     resultsStartingIndex past the end
  graphql_search_bali_dateless.json  free-text destination, no dates
  graphql_reviews_2165911.json     reviewInfo, 41 reviews in one call
  graphql_calendar_2165911.json    propertyRatesDateSelector, 764 days
  graphql_offer_available.json     propertyOffers for an available stay
  graphql_offer_sold_out.json      propertyOffers for an unavailable stay
  graphql_invalid_property_id.json  the error an "ha" id gets from GraphQL
  typeahead_miami.json             /api/v4/typeahead/Miami

Page state: every server-rendered page embeds
`window.__APOLLO_STATE__ = JSON.parse("<JSON-escaped JSON>")`; apollo_state()
unescapes it. Entities are keyed "<Type>:<id>" and referenced as
{"__ref": key}; ROOT_QUERY holds the operation results keyed by
"<field>(<args json>)" or "<field>:<args json>".
"""
import json
import math
import re
from datetime import datetime
from html import unescape
from urllib.parse import urlparse

from vrbo.refs import SITE, property_link

# ---- generic helpers -------------------------------------------------------------
# Localized price text -> float. A copy of airbnb/parsers.parse_amount kept
# here so the vrbo package stands on its own (the open-source kit ships it
# without the other sites).
_AMOUNT_START_RE = re.compile(r"\d[\d.,  \s]*")


def parse_amount(text):
    """Localized price text -> float: "₹40,524" -> 40524.0,
    "8,104.64" -> 8104.64, "1 353 €" -> 1353.0, "8.104,64" -> 8104.64.
    None when no number is present."""
    if not text:
        return None
    text = str(text)
    m = _AMOUNT_START_RE.search(text)
    if not m:
        return None
    negative = bool(re.search(r"[-−–]\s*[^\d]*$", text[:m.start()]))
    s = re.sub(r"[\s  ]", "", m.group())
    if "," in s and "." in s:
        if s.rfind(",") > s.rfind("."):          # 8.104,64 (comma decimal)
            s = s.replace(".", "").replace(",", ".")
        else:                                     # 8,104.64
            s = s.replace(",", "")
    elif "," in s:
        head, _, tail = s.rpartition(",")
        if "," not in head and len(tail) in (1, 2):   # 1353,5 (comma decimal)
            s = head + "." + tail
        else:                                     # 40,524 / 1,00,524 (grouping)
            s = s.replace(",", "")
    elif s.count(".") > 1 or (s.count(".") == 1 and len(s.rpartition(".")[2]) == 3):
        s = s.replace(".", "")                    # 8.104 (dot grouping)
    try:
        value = float(s)
    except ValueError:
        return None
    return -value if negative else value



_APOLLO_RE = re.compile(r'window\.__APOLLO_STATE__\s*=\s*JSON\.parse\("(.*?)"\);', re.S)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t ]+")


def apollo_state(html):
    """The page's Apollo cache as a dict, or None when the page carries none."""
    match = _APOLLO_RE.search(html or "")
    if not match:
        return None
    try:
        return json.loads(json.loads('"' + match.group(1) + '"'))
    except ValueError:
        return None


def _deref(state, value):
    if isinstance(value, dict) and "__ref" in value and len(value) == 1:
        return state.get(value["__ref"])
    return value


def _root(state, field):
    """ROOT_QUERY entries whose field name is `field` (any arguments)."""
    out = []
    for key, value in (state.get("ROOT_QUERY") or {}).items():
        name = re.split(r"[(:]", key, 1)[0]
        if name == field:
            out.append(_deref(state, value))
    return out


def _first_root(state, field, predicate=None):
    for value in _root(state, field):
        if value is not None and (predicate is None or predicate(value)):
            return value
    return None


def _get(obj, *path, default=None):
    for key in path:
        if isinstance(obj, dict):
            obj = obj.get(key)
        elif isinstance(obj, list) and isinstance(key, int) and -len(obj) <= key < len(obj):
            obj = obj[key]
        else:
            return default
        if obj is None:
            return default
    return obj


def _text(markup):
    """HTML markup -> plain text with newlines kept for <br> / </p>."""
    if not markup:
        return None
    text = re.sub(r"(?i)<br\s*/?>|</p>|</li>", "\n", str(markup))
    text = unescape(_TAG_RE.sub("", text))
    text = "\n".join(_WS_RE.sub(" ", line).strip() for line in text.split("\n"))
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text or None


_BIDI_RE = re.compile(r"[‎‏‪-‮⁦-⁩]")


def _clean(value):
    if value is None:
        return None
    value = _WS_RE.sub(" ", _BIDI_RE.sub("", str(value))).strip()
    return value or None


def _int(text):
    match = re.search(r"\d[\d,]*", str(text or ""))
    return int(match.group().replace(",", "")) if match else None


def _float(text):
    match = re.search(r"-?\d+(?:\.\d+)?", str(text or ""))
    return float(match.group()) if match else None


_CURRENCY_SYMBOLS = (("A$", "AUD"), ("C$", "CAD"), ("NZ$", "NZD"), ("S$", "SGD"), ("R$", "BRL"),
                     ("MX$", "MXN"), ("US$", "USD"), ("$", "USD"), ("€", "EUR"), ("£", "GBP"),
                     ("¥", "JPY"), ("kr", "SEK"), ("CHF", "CHF"))


def currency_of(text, default="USD"):
    text = str(text or "")
    for symbol, code in _CURRENCY_SYMBOLS:
        if symbol in text:
            return code
    return default


def _money(text, default_currency="USD"):
    """"$1,199" -> (1199.0, "USD"); None when no number."""
    amount = parse_amount(text)
    if amount is None:
        return None, None
    return amount, currency_of(text, default_currency)


def _date(parts):
    """{day, month, year} -> ISO date."""
    try:
        return f"{int(parts['year']):04d}-{int(parts['month']):02d}-{int(parts['day']):02d}"
    except (TypeError, KeyError, ValueError):
        return None


def _date_text(text):
    """"Sep 2, 2026" -> "2026-09-02"."""
    for fmt in ("%b %d, %Y", "%B %d, %Y"):
        try:
            return datetime.strptime(_clean(text) or "", fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _nights(text):
    match = re.search(r"(\d+)\s*night", str(text or ""))
    return int(match.group(1)) if match else None


def _listing_id_from_link(link):
    match = re.match(r"^/([0-9]+(?:ha)?)(?:[/?#]|$)", urlparse(link or "").path, re.I)
    return match.group(1).lower() if match else None


# ---- typeahead ---------------------------------------------------------------------

def parse_autocomplete(payload):
    """typeahead JSON -> [{region_id, name, short_name, type, country, coordinates}]."""
    out = []
    for item in (payload or {}).get("sr") or []:
        if not isinstance(item, dict):
            continue
        names = item.get("regionNames") or {}
        coords = item.get("coordinates") or {}
        country = _get(item, "hierarchyInfo", "country") or {}
        lat, lng = _float(coords.get("lat")), _float(coords.get("long"))
        out.append({
            "region_id": _clean(item.get("gaiaId")),
            "name": _clean(names.get("fullName")),
            "short_name": _clean(names.get("shortName")),
            "type": (_clean(item.get("type")) or "").lower() or None,
            "country": {"name": _clean(country.get("name")), "code": _clean(country.get("isoCode2"))},
            "coordinates": {"latitude": lat, "longitude": lng} if lat is not None and lng is not None else None,
            "is_major_airport": (item.get("isMinorAirport") == "false") if item.get("type") == "AIRPORT" else None,
        })
    return out


# ---- search cards ------------------------------------------------------------------

_SEP = "·"
_PAY_LATER = "Pay $0 today"
_REVIEW_COUNT_RE = re.compile(r"(\d[\d,]*)\s+reviews?\b", re.I)


def _split_line(text):
    return [p.strip() for p in str(text or "").split(_SEP) if p.strip()]


def _summary_facts(lines):
    """The card's two message lines -> property type, rooms, highlights,
    neighborhood, free-cancellation text."""
    facts = {"property_type": None, "bedrooms": None, "beds": None, "bathrooms": None,
             "amenity_highlights": [], "neighborhood": None, "distance_text": None,
             "free_cancellation_text": None, "is_pay_later": False, "bed_types": []}
    for line in lines:
        for part in _split_line(line):
            low = part.lower()
            if re.search(r"\bbedrooms?\b", low) and re.match(r"^\d", low):
                facts["bedrooms"] = _int(part)
            elif re.search(r"\bbathrooms?\b", low) and re.match(r"^\d", low):
                facts["bathrooms"] = _float(part)
            elif re.search(r"\bbeds?\b", low) and re.match(r"^\d", low):
                if re.search(r"\b(king|queen|double|single|twin|full|bunk|sofa)\b", low):
                    facts["bed_types"].append(part)
                    facts["beds"] = (facts["beds"] or 0) + (_int(part) or 0)
                else:
                    facts["beds"] = _int(part)
            elif low.startswith("within "):
                facts["neighborhood"] = part[len("Within "):]
            elif low.startswith("free cancellation"):
                facts["free_cancellation_text"] = part
            elif part == _PAY_LATER:
                facts["is_pay_later"] = True
            elif re.search(r"\b(walk|drive|min|km|mi)\b", low) and re.search(r"\d|beach|center|centre", low):
                facts["distance_text"] = part
            elif low in ("studio",) or (facts["property_type"] is None and not re.search(r"\d", part)
                                       and lines and part == _split_line(lines[0])[0]):
                facts["property_type"] = part
            else:
                facts["amenity_highlights"].append(part)
    if not facts["beds"]:
        facts["beds"] = None
    return facts


def _price_lines(price_summary, default_currency="USD"):
    """A PropertyPrice block -> {nightly, original_nightly, total, nights,
    currency, is_fees_included, dates_text, raw lines}."""
    out = {"nightly": None, "original_nightly": None, "total": None, "nights": None,
           "currency": None, "is_fees_included": None, "dates_text": None}
    if not isinstance(price_summary, dict):
        return out
    lines = []
    for message in price_summary.get("displayMessages") or []:
        for item in (message or {}).get("lineItems") or []:
            if not isinstance(item, dict):
                continue
            if item.get("__typename") == "DisplayPrice" or "role" in item:
                formatted = _get(item, "price", "formatted")
                role = item.get("role")
                amount, cur = _money(formatted, default_currency)
                if role == "LEAD" and amount is not None:
                    out["nightly"], out["currency"] = amount, cur
                elif role == "STRIKEOUT" and amount is not None:
                    out["original_nightly"] = amount
                elif formatted:
                    lines.append(formatted)
            else:
                value = item.get("value")
                if value:
                    lines.append(value)
    for option in price_summary.get("options") or []:
        amount, cur = _money(_get(option, "displayPrice", "formatted"), default_currency)
        if amount is not None and out["nightly"] is None:
            out["nightly"], out["currency"] = amount, cur
        strike, _ = _money(_get(option, "strikeOut", "formatted"), default_currency)
        if strike is not None and out["original_nightly"] is None:
            out["original_nightly"] = strike
    for value in (price_summary.get("priceMessaging") or []):
        if isinstance(value, dict) and value.get("value"):
            lines.append(value["value"])
    for line in lines:
        low = line.lower()
        if "fees included" in low:
            out["is_fees_included"] = True
        elif re.match(r"^\D{0,4}\d[\d,.]*\s+for\s+\d+\s+nights?", line):   # "$3,118 for 4 nights"
            out["total"], _ = _money(line, default_currency)
            out["nights"] = _nights(line)
        elif "night" in low:                                               # "for 1 night"
            out["nights"] = out["nights"] or _nights(line)
        elif re.search(r"\b[A-Z][a-z]{2} \d{1,2}\s*-\s*[A-Z][a-z]{2} \d{1,2}\b", line):
            out["dates_text"] = line
    if out["nights"] is None and out["total"] is not None and out["nightly"]:
        out["nights"] = int(round(out["total"] / out["nightly"])) or None
    return out


def parse_card(card, markers=None):
    """One LodgingCard (+ its map marker) -> a result row."""
    markers = markers or {}
    expedia_id = _clean(card.get("id"))
    link = _get(card, "cardLink", "resource", "value")
    listing_id = _listing_id_from_link(link)
    heading = card.get("headingSection") or {}
    lines = [_clean(m.get("text")) for m in heading.get("messages") or [] if isinstance(m, dict) and m.get("text")]
    featured = [_clean(m.get("text")) for m in heading.get("featuredMessages") or [] if isinstance(m, dict) and m.get("text")]
    facts = _summary_facts(lines + featured)
    review = None
    for section in card.get("summarySections") or []:
        if isinstance(section, dict) and section.get("reviewSummary"):
            review = section["reviewSummary"]
            break
    score_text = _get(review, "graphic", "text")
    title_text = _get(review, "title", "shoppingProductTitle", "text")
    score = _float(score_text) if score_text and score_text.lower() != "new" else None
    label, review_count = None, None
    if title_text:
        parts = _split_line(title_text)
        label = parts[0] if parts else None
        review_count = _int(next((p for p in parts if "review" in p.lower()), ""))
    if review_count is None:
        # The compact card variant (seen on the iOS fingerprint) drops the
        # count from the title text; take it from the accessibility label or
        # the subtexts when either carries "(173 reviews)".
        texts = [_get(review, "title", "shoppingProductTitle", "accessibility")]
        for sub in (review or {}).get("subtexts") or []:
            texts += [_get(sub, "shoppingProductTitle", "text"), _get(sub, "shoppingProductTitle", "accessibility"),
                      _get(sub, "title", "text"), _get(sub, "accessibilityText")]
        match = next((m for m in (_REVIEW_COUNT_RE.search(t or "") for t in texts) if m), None)
        review_count = int(match.group(1).replace(",", "")) if match else None
    price = _price_lines(_get(card, "priceSection", "priceSummary"))
    discount_badge = _get(card, "priceSection", "standardBadge", "standardBadge", "text")
    badges = [b.get("text") for b in ((_get(card, "mediaSection", "badges") or {}).values())
              if isinstance(b, dict) and b.get("text")]
    photos = []
    for item in _get(card, "mediaSection", "gallery", "media") or []:
        url = _get(item, "media", "url")
        if url:
            photos.append({"link": url, "caption": _clean(_get(item, "media", "description"))})
    marker = markers.get(expedia_id) or {}
    position = marker.get("markerPosition") or {}
    # Two card headings exist (an A/B split seen 2026-10-31): the listing
    # name, or "<Type> in <City>" with the name only on the map marker and
    # in the photo captions ("Image of <name>").
    heading_text = _clean(heading.get("heading"))
    name = _clean(marker.get("name"))
    if not name:
        caption = next((p["caption"] for p in photos if (p["caption"] or "").startswith("Image of ")), None)
        name = caption[len("Image of "):].strip() if caption else None
    type_city = re.match(r"^([A-Z][A-Za-z\-' ]{1,40}?) in (.+)$", heading_text or "")
    is_generic = bool(type_city) and (name is None or name != heading_text)
    return {
        "id": listing_id,
        "expedia_id": expedia_id,
        "name": name or heading_text,
        "link": property_link(listing_id) if listing_id else link,
        "property_type": facts["property_type"] or (type_city.group(1) if is_generic else None),
        "city": type_city.group(2) if is_generic else None,
        "bedrooms": facts["bedrooms"],
        "beds": facts["beds"],
        "bathrooms": facts["bathrooms"],
        "bed_types": facts["bed_types"] or None,
        "amenity_highlights": facts["amenity_highlights"],
        "neighborhood": facts["neighborhood"],
        "distance_text": facts["distance_text"],
        "rating": {"score": score, "max": 10, "label": label, "review_count": review_count,
                   "is_new": (score_text or "").lower() == "new"},
        "price": {"nightly": price["nightly"], "original_nightly": price["original_nightly"],
                  "total": price["total"], "nights": price["nights"], "currency": price["currency"],
                  "is_fees_included": price["is_fees_included"], "dates_text": price["dates_text"],
                  "discount_badge": _clean(discount_badge)},
        "free_cancellation_text": facts["free_cancellation_text"],
        "is_pay_later": facts["is_pay_later"],
        "is_premier_host": any("premier host" in b.lower() for b in badges),
        "badges": badges,
        "coordinates": ({"latitude": position.get("latitude"), "longitude": position.get("longitude")}
                        if position.get("latitude") is not None else None),
        "is_available": (marker.get("markerStatus") == "AVAILABLE") if marker.get("markerStatus") else None,
        "photos": photos,
        "summary_lines": lines + featured,
    }


def parse_search(result):
    """propertySearch payload -> {location, total_count, has_next,
    next_start, results}."""
    result = result or {}
    markers = {}
    for marker in _get(result, "dynamicMap", "map", "markers") or []:
        if isinstance(marker, dict) and marker.get("id"):
            markers[str(marker["id"])] = marker
    rows = [parse_card(card, markers) for card in result.get("propertySearchListings") or []
            if isinstance(card, dict) and card.get("__typename") == "LodgingCard"]
    criteria = _get(result, "criteria", "primary") or {}
    destination = criteria.get("destination") or {}
    dates = criteria.get("dateRange") or {}
    pagination = result.get("pagination") or {}
    next_subset = _get(pagination, "subSets", "nextSubSet") or {}
    return {
        "location": {"region_id": _clean(destination.get("regionId")),
                     "name": _clean(destination.get("regionName"))},
        "checkin": _date(dates.get("checkInDate")) if dates else None,
        "checkout": _date(dates.get("checkOutDate")) if dates else None,
        "total_count": _get(result, "summary", "matchedPropertiesSize"),
        "has_next": bool(next_subset.get("startingIndex") is not None),
        "next_start": next_subset.get("startingIndex"),
        "results": rows,
    }


# ---- filters (server-rendered search page) ------------------------------------------

def _search_root(state):
    return _first_root(state, "propertySearch", lambda v: isinstance(v, dict) and v.get("universalSortAndFilter"))


def _option(state, opt):
    opt = _deref(state, opt) or {}
    label = _clean(opt.get("primary"))
    count = None
    match = re.search(r"\((\d[\d,]*)\)\s*$", label or "")
    if match:
        count = int(match.group(1).replace(",", ""))
        label = label[:match.start()].strip()
    return {"value": _clean(opt.get("value")), "label": label, "count": count,
            "is_selected": bool(opt.get("selected"))}


def parse_filters(state):
    """The search page's universalSortAndFilter -> {location, total_count,
    filters: [...], sort_options: [...]}."""
    root = _search_root(state) or {}
    usf = root.get("universalSortAndFilter") or {}
    filters, sorts = [], []
    for section in usf.get("filterSections") or []:
        for field in (section or {}).get("fields") or []:
            field = _deref(state, field) or {}
            kind = field.get("__typename")
            if kind == "ShoppingMultiSelectionField":
                filters.append({"id": field.get("id"), "label": _clean(field.get("primary")),
                                "type": "multi_select",
                                "options": [_option(state, o) for o in field.get("options") or []]})
            elif kind == "ShoppingStepInputField":
                filters.append({"id": field.get("id"), "label": _clean(field.get("primary")),
                                "type": "minimum_count", "min": field.get("min"), "max": field.get("max"),
                                "step": field.get("step"), "options": []})
            elif kind == "ShoppingMutexField":
                options = []
                for choice in field.get("options") or []:
                    option = (choice or {}).get("option") or {}
                    rng = option.get("range") or {}
                    chars = rng.get("characteristics") or {}
                    options.append({"value": option.get("id"), "label": None,
                                    "min": chars.get("min"), "max": chars.get("max"),
                                    "step": chars.get("step"),
                                    "max_label": _clean(chars.get("maxLabel")),
                                    "histogram": _get(option, "histogram", "buckets")})
                filters.append({"id": field.get("id"), "label": _clean(field.get("primary")),
                                "type": "price_range", "options": options})
            elif kind == "ShoppingDropdownField":
                for opt in field.get("options") or []:
                    sorts.append({"value": opt.get("value"), "label": _clean(opt.get("primary")),
                                  "is_default": bool(opt.get("default"))})
    criteria = _get(root, "criteria", "primary") or {}
    destination = criteria.get("destination") or {}
    return {
        "location": {"region_id": _clean(destination.get("regionId")),
                     "name": _clean(destination.get("regionName"))},
        "total_count": _get(root, "summary", "matchedPropertiesSize"),
        "filters": filters,
        "sort_options": sorts,
    }


# ---- reviews --------------------------------------------------------------------------

_RESPONSE_HEADER_RE = re.compile(r"^Response from (.+?) on (.+)$")
_STAY_RE = re.compile(r"Stayed (\d+) nights? in ([A-Z][a-z]{2} \d{4})")


def _score(value):
    """"10/10 Excellent" -> (10.0, "Excellent")."""
    match = re.match(r"\s*(\d+(?:\.\d+)?)\s*/\s*(\d+)\s*(.*)$", str(value or ""))
    if not match:
        return None, _clean(value)
    return float(match.group(1)), _clean(match.group(3)) or None


def parse_review(review):
    score, label = _score(_get(review, "reviewScoreWithDescription", "value"))
    responses = []
    for resp in review.get("managementResponses") or []:
        if not isinstance(resp, dict) or not resp.get("response"):
            continue
        header = _clean(_get(resp, "header", "text")) or ""
        match = _RESPONSE_HEADER_RE.match(header)
        responses.append({"responder": match.group(1) if match else None,
                          "date": _date_text(match.group(2)) if match else None,
                          "text": _text(resp.get("response"))})
    stay = _clean(review.get("stayDuration"))
    stay_match = _STAY_RE.search(stay or "")
    helpful = None
    for section in review.get("reviewInteractionSections") or []:
        if (section or {}).get("reviewInteractionType") == "HELPFUL_REVIEW":
            helpful = _int(section.get("primaryDisplayString"))
    return {
        "id": review.get("id"),
        "score": score,
        "max": 10,
        "label": review.get("superlative") or label,
        "title": _clean(review.get("title")),
        "text": _text(review.get("text")),
        "author": _clean(_get(review, "reviewAuthorAttribution", "text")),
        "date": _date_text(review.get("submissionTimeLocalized")),
        "stay": {"text": stay, "nights": int(stay_match.group(1)) if stay_match else None,
                 "month": (datetime.strptime(stay_match.group(2), "%b %Y").strftime("%Y-%m")
                           if stay_match else None)},
        "travelers": [t for t in review.get("travelers") or [] if t],
        "themes": [_clean(t.get("label")) for t in review.get("themes") or [] if isinstance(t, dict) and t.get("label")],
        "is_verified": (review.get("disclaimer") or "").lower() == "verified review",
        "language": review.get("locale"),
        "source": _clean(_get(review, "propertyReviewSource", "text", "value")),
        "photos": [{"link": p.get("url"), "caption": _clean(p.get("description"))}
                   for p in review.get("photos") or [] if isinstance(p, dict) and p.get("url")],
        "host_responses": responses,
        "helpful_votes": helpful,
    }


def parse_reviews(info):
    """reviewInfo payload -> {summary, reviews, sort_options}."""
    info = info or {}
    summary = info.get("summary") or {}
    categories = []
    for detail in summary.get("reviewSummaryDetails") or []:
        if not isinstance(detail, dict):
            continue
        score, _ = _score(detail.get("formattedRatingOutOfMax"))
        categories.append({"label": _clean(detail.get("label")), "score": score,
                           "percentage": detail.get("ratingPercentage")})
    total = _get(summary, "totalCount", "raw")
    avg = _get(summary, "averageOverallRating", "raw")
    sorts = []
    for block in _get(info, "sortAndFilter", "sortAndFilter") or []:
        if (block or {}).get("name") == "sortBy":
            sorts = [{"value": o.get("optionValue"), "label": _clean(o.get("label"))}
                     for o in block.get("options") or [] if isinstance(o, dict)]
    return {
        "summary": {
            "score": round(float(avg), 1) if avg is not None else None,
            "max": 10,
            "label": _clean(summary.get("superlative")),
            "review_count": int(total) if total is not None else None,
            "categories": categories,
        },
        "reviews": [parse_review(r) for r in info.get("reviews") or [] if isinstance(r, dict)],
        "sort_options": sorts,
    }


# ---- calendar -----------------------------------------------------------------------------

def _validity(value):
    """"INVALID_DUE_TO_MIN_STAY_NOT_ACHIEVABLE" -> "min_stay_not_achievable"."""
    value = str(value or "")
    if value == "VALID":
        return None
    return value.replace("INVALID_DUE_TO_", "").lower() or None


def parse_calendar(selector, start_date=None, end_date=None, default_currency="USD"):
    """propertyRatesDateSelector -> {range, days[], summary}; `start_date`
    / `end_date` (ISO) narrow the day list."""
    selector = selector or {}
    conf = selector.get("configuration") or {}
    days = []
    for day in selector.get("days") or []:
        if not isinstance(day, dict):
            continue
        iso = _date(day.get("date") or {})
        if not iso or (start_date and iso < start_date) or (end_date and iso > end_date):
            continue
        amount, cur = _money(day.get("displayPrice"), default_currency)
        constraints = day.get("stayConstraints") or {}
        max_stay = constraints.get("maximumStayInDays")
        days.append({
            "date": iso,
            "is_available": bool(day.get("available")),
            "price": amount,
            "currency": cur if amount is not None else None,
            "can_check_in": day.get("checkinValidity") == "VALID",
            "can_check_out": day.get("checkoutValidity") == "VALID",
            "check_in_blocked_by": _validity(day.get("checkinValidity")),
            "check_out_blocked_by": _validity(day.get("checkoutValidity")),
            "min_stay": constraints.get("minimumStayInDays"),
            "max_stay": max_stay if max_stay else None,
            "stay_increment": constraints.get("stayIncrementInDays"),
        })
    available = [d for d in days if d["is_available"]]
    prices = [d["price"] for d in available if d["price"] is not None]
    return {
        "range": {"from": _date(conf.get("beginDate") or {}), "to": _date(conf.get("endDate") or {}),
                  "today": _date(conf.get("currentDate") or {})},
        "summary": {
            "days": len(days),
            "available_days": len(available),
            "min_price": min(prices) if prices else None,
            "max_price": max(prices) if prices else None,
            "currency": next((d["currency"] for d in available if d["currency"]), None),
        },
        "message": _clean(selector.get("validationMessage")),
        "days": days,
    }


# ---- dated offer ----------------------------------------------------------------------------

def parse_offer(offers, checkin=None, checkout=None, default_currency="USD"):
    """propertyOffers -> the headline quote for the stay."""
    offers = offers or {}
    sold_out = bool(offers.get("soldOut"))
    message = _clean(_get(offers, "errorMessage", "title", "text"))
    sticky = _get(offers, "stickyBar", "propertyPrice")
    price = _price_lines(sticky, default_currency)
    if price["nightly"] is None:
        price["nightly"], price["currency"] = _money(_get(offers, "stickyBar", "price", "formattedDisplayPrice"),
                                                     default_currency)
    nights = price["nights"]
    if nights is None and checkin and checkout:
        try:
            nights = (datetime.fromisoformat(checkout) - datetime.fromisoformat(checkin)).days or None
        except ValueError:
            nights = None
    plans = _get(offers, "singleUnitOfferDialog", "content", "ratePlans") or []
    plan = next((p for p in plans if isinstance(p, dict)), None) or {}
    detail = next((d for d in plan.get("priceDetails") or [] if isinstance(d, dict)), None) or {}
    key = next((k for k in detail.get("propertyNaturalKeys") or [] if isinstance(k, dict)), None) or {}
    cancellation = None
    notes = []
    for item in plan.get("highlightedMessages") or []:
        if not isinstance(item, dict):
            continue
        text = _clean(_get(item, "trigger", "value") or item.get("value"))
        if not text:
            continue
        if "refund" in text.lower() or "cancell" in text.lower() or _get(item, "toolbar", "title") == "Cancellation policy":
            cancellation = cancellation or text
        else:
            notes.append(text)
    available = (detail.get("availability") or {}).get("available")
    is_available = (not sold_out) and (available is not False) and price["nightly"] is not None
    return {
        "is_available": is_available,
        "message": message,
        "checkin": _date(key.get("checkIn")) if key.get("checkIn") else checkin,
        "checkout": _date(key.get("checkOut")) if key.get("checkOut") else checkout,
        "nights": nights,
        "price": {
            "nightly": price["nightly"],
            "original_nightly": price["original_nightly"],
            "total": price["total"],
            "currency": price["currency"] if price["nightly"] is not None else None,
            "is_fees_included": price["is_fees_included"],
        },
        "cancellation_policy": cancellation,
        "scarcity_message": _clean((detail.get("availability") or {}).get("scarcityMessage")),
        "payment_model": _clean(detail.get("paymentModel")),
        "is_pay_at_property": bool(detail.get("hotelCollect")) if detail else None,
        "is_card_required": (not detail.get("noCreditCard")) if detail else None,
        "is_pets_included": key.get("petsIncluded") if key else None,
        "rate_plan_id": _clean(detail.get("ratePlanId") if detail.get("ratePlanId") else key.get("ratePlanId")) if (detail or key) else None,
        "notes": notes,
    }


# ---- listing page ---------------------------------------------------------------------------

def _property_info(state):
    for key, value in state.items():
        if key.startswith("PropertyInfo:") and isinstance(value, dict):
            return key.split(":", 1)[1], value
    return None, None


def _content_groups(info):
    """The propertyContentSectionGroups entry that carries the content
    groups (policies / importantInfo / aboutThisProperty / aboutThisHost)."""
    best = None
    for key, value in (info or {}).items():
        if key.startswith("propertyContentSectionGroups") and isinstance(value, dict):
            if value.get("aboutThisProperty") or value.get("policies"):
                return value
            best = best or value
    return best or {}


def _elements(group):
    """Every PropertyContent element of a content group, in order, as
    (header text, [item texts], [markup texts])."""
    out = []
    for section in (group or {}).get("sections") or []:
        for sub in (section or {}).get("bodySubSections") or []:
            for egroup in (sub or {}).get("elementsV2") or []:
                for element in (egroup or {}).get("elements") or []:
                    header = _clean(_get(element, "header", "text"))
                    image = _get(element, "header", "headerImage", "url")
                    texts = []
                    for item in element.get("items") or []:
                        content = (item or {}).get("content") or {}
                        if content.get("markupType"):
                            texts.append(_text(content.get("text")))
                        else:
                            texts.append(_clean(_get(content, "primary", "value")))
                        for part in (item or {}).get("contents") or []:
                            texts.append(_clean(_get(part, "primary", "value")))
                    out.append({"header": header, "image": image, "texts": [t for t in texts if t]})
    return out


def _house_rules(group):
    rules, check_in, check_out, instructions = [], None, None, []
    for element in _elements(group):
        header = element["header"]
        texts = element["texts"]
        if header and header.lower().startswith("check-out instructions"):
            instructions = texts
            continue
        if header:
            rules.append({"topic": header, "text": " ".join(texts) or None})
            continue
        for text in texts:
            low = text.lower()
            if low.startswith("check in"):
                check_in = re.sub(r"(?i)^check in (?:after|from)\s*", "", text) or text
            elif low.startswith("check out"):
                check_out = re.sub(r"(?i)^check out (?:before|by)\s*", "", text) or text
            else:
                rules.append({"topic": None, "text": text})
    return {"check_in_after": check_in, "check_out_before": check_out, "rules": rules,
            "check_out_instructions": instructions}


def _host(group):
    host = {"name": None, "image": None, "languages": [], "is_premier_host": False,
            "premier_host_text": None, "about": None}
    for element in _elements(group):
        header = element["header"] or ""
        if header.lower().startswith("hosted by"):
            host["name"] = header[len("Hosted by"):].strip() or None
            host["image"] = element["image"]
            host["about"] = "\n".join(element["texts"]) or None
        elif header.lower().startswith("languages"):
            host["languages"] = [p.strip() for t in element["texts"] for p in t.split(",") if p.strip()]
        elif header.lower() == "premier host":
            host["is_premier_host"] = True
            host["premier_host_text"] = " ".join(element["texts"]) or None
    return host


def _description(group):
    paragraphs = []
    for element in _elements(group):
        paragraphs.extend(element["texts"])
    return "\n\n".join(paragraphs) or None


def _important_info(group):
    out = []
    for element in _elements(group):
        out.extend(t for t in element["texts"] if not t.lower().startswith("property #"))
    return out


def _capacity(info):
    out = {"sleeps": None, "bedrooms": None, "beds": None, "bathrooms": None}
    for item in _get(info, "summary", "spaceOverview", "infoItems") or []:
        text = _clean((item or {}).get("text")) or ""
        low = text.lower()
        if low.startswith("sleeps"):
            out["sleeps"] = _int(text)
        elif "bedroom" in low:
            out["bedrooms"] = _int(text)
        elif "bathroom" in low or low.endswith("bath") or " baths" in low:
            out["bathrooms"] = _float(text)
        elif re.search(r"\bbeds?\b", low):
            out["beds"] = _int(text)
    return out


def _amenities(info):
    block = None
    for key, value in (_get(info, "summary") or {}).items():
        if key.startswith("amenities") and isinstance(value, dict):
            block = value
            break
    groups, popular = [], []
    for section in (block or {}).get("amenities") or []:
        for content in (section or {}).get("contents") or []:
            items = [_clean(i.get("text")) for i in (content or {}).get("infoItems") or [] if isinstance(i, dict) and i.get("text")]
            if items:
                groups.append({"category": _clean(_get(content, "header", "text")), "items": items})
    for item in _get(block, "topAmenities", "infoItems") or []:
        if isinstance(item, dict) and item.get("text"):
            popular.append(_clean(item["text"]))
    return groups, popular


def _photos(state):
    gallery = _first_root(state, "productGallery")
    categories = {}
    flat = []
    for cat in (gallery or {}).get("categorizedImages") or []:
        name = _clean(cat.get("categoryName"))
        images = cat.get("images") or []
        if cat.get("categoryId") == "all":
            flat = images
            continue
        for image in images:
            url = _get(image, "image", "url")
            if url:
                categories.setdefault(url, name)
    out = []
    for image in flat or []:
        url = _get(image, "image", "url")
        if not url:
            continue
        out.append({"link": url, "caption": _clean(_get(image, "image", "description")),
                    "category": categories.get(url),
                    "thumbnail": _get(image, "thumbnail", "url")})
    return out


def _nearby(info):
    out = []
    for group in _get(info, "summary", "location", "whatsAround", "nearbyPOIs") or []:
        places = []
        for item in (group or {}).get("infoItems") or []:
            if not isinstance(item, dict) or not item.get("text"):
                continue
            name = _clean(item["text"])
            travel = _clean(item.get("moreInfo"))
            if travel is None and " - " in name:
                name, travel = (p.strip() for p in name.rsplit(" - ", 1))
            places.append({"name": name, "distance": _clean(item.get("distance")), "travel_time": travel})
        if places:
            out.append({"category": _clean(group.get("title")), "places": places})
    return out


def _seo(state):
    headline = _first_root(state, "productHeadline") or {}
    try:
        return json.loads(headline.get("seoStructuredData") or "{}") or {}
    except ValueError:
        return {}


def _region_id(state):
    for key in state.get("ROOT_QUERY") or {}:
        if key.startswith("propertyInfo"):
            match = re.search(r'"regionId":"(\d+)"', key)
            if match:
                return match.group(1)
    return None


def _listing_id(state, html):
    match = re.search(r'canonicalUrl[^}]{0,40}?vrbo\.com/([0-9]+(?:ha)?)', json.dumps(state), re.I)
    if match:
        return match.group(1).lower()
    match = re.search(r"Property #\s*</b>\s*([0-9]+(?:ha)?)", html or "", re.I)
    return match.group(1).lower() if match else None


def _property_type(info, breadcrumbs):
    for link in _get(info, "summary", "location", "whatsAround", "editorial", "crossLinks") or []:
        text = _clean((link or {}).get("value")) or ""
        match = re.match(r"View more (.+?) in ", text)
        if match:
            kind = match.group(1)
            return kind[:-1] if kind.endswith("s") and not kind.endswith("ss") else kind
    return None


def _breadcrumbs(state):
    crumbs = _first_root(state, "breadcrumbs") or {}
    out = []
    for item in crumbs.get("breadcrumbItems") or []:
        if not isinstance(item, dict):
            continue
        out.append({"name": _clean(item.get("text")), "link": _get(item, "uiLinkAction", "resource", "value")})
    return out


def parse_property_page(html, listing_id=None, default_currency="USD"):
    """The bare listing page -> the full property record (None when the
    page carries no PropertyInfo, i.e. a shell or the homepage)."""
    state = apollo_state(html)
    if not state:
        return None
    expedia_id, info = _property_info(state)
    if not info:
        return None
    listing_id = listing_id or _listing_id(state, html)
    seo = _seo(state)
    address = seo.get("address") or {}
    aggregate = seo.get("aggregateRating") or {}
    groups = _content_groups(info)
    rating_block = _first_root(state, "productRatingSummary") or {}
    rating_summary = rating_block.get("summary") or {}
    score, _ = _score(rating_summary.get("primary"))
    if score is None:
        score = _float(aggregate.get("ratingValue"))
    amenities, popular = _amenities(info)
    offers = _first_root(state, "propertyOffers", lambda v: isinstance(v, dict) and v.get("stickyBar")) or {}
    default_price = _price_lines(_get(offers, "stickyBar", "propertyPrice"), default_currency)
    travelers = _get(_first_root(state, "offersTravelerSelector") or {}, "travelers", "action",
                     "travelerSelector", "travelers") or {}
    coordinates = None
    loc = _first_root(state, "location") or {}
    lat = _get(loc, "address", "coordinates", "latitude")
    lng = _get(loc, "address", "coordinates", "longitude")
    if lat is None:
        lat, lng = seo.get("latitude"), seo.get("longitude")
    if lat is not None and lng is not None:
        coordinates = {"latitude": lat, "longitude": lng}
    highlights = [{"title": _clean(i.get("text")), "text": _clean(i.get("subText"))}
                  for i in _get(_first_root(state, "productHighlights") or {}, "content", "items") or []
                  if isinstance(i, dict) and i.get("text")]
    editorial = _get(info, "summary", "location", "whatsAround", "editorial", "content") or []
    breadcrumbs = _breadcrumbs(state)
    return {
        "id": listing_id,
        "expedia_id": expedia_id,
        "name": _clean(_get(_first_root(state, "productHeadline") or {}, "primary")) or _clean(seo.get("name")),
        "link": property_link(listing_id) if listing_id else None,
        "property_type": _property_type(info, breadcrumbs),
        "description": _description(groups.get("aboutThisProperty")),
        "capacity": _capacity(info),
        "max_adults": _get(travelers, "adults", "max"),
        "max_children": _get(travelers, "children", "count", "max"),
        "rating": {
            "score": score, "max": 10,
            "label": _clean(rating_summary.get("secondary")),
            "review_count": _int(aggregate.get("reviewCount")),
            "note": _clean(_get(rating_summary, "reviewSummary", "subtexts", 0, "text")),
        },
        "location": {
            "address": [_clean(a) for a in (_get(loc, "address", "address") or []) if _clean(a)] or None,
            "city": _clean(address.get("addressLocality")),
            "region": _clean(address.get("addressRegion")),
            "country_code": _clean(address.get("addressCountry")),
            "region_id": _region_id(state),
            "coordinates": coordinates,
            "neighborhood_text": "\n".join(_clean(t) for t in editorial if _clean(t)) or None,
            "nearby": _nearby(info),
        },
        "highlights": highlights,
        "popular_amenities": popular,
        "amenities": amenities,
        "house_rules": _house_rules(groups.get("policies")),
        "important_info": _important_info(groups.get("importantInfo")),
        "host": _host(groups.get("aboutThisHost")),
        "default_price": {
            "nightly": default_price["nightly"], "total": default_price["total"],
            "nights": default_price["nights"], "currency": default_price["currency"],
            "is_fees_included": default_price["is_fees_included"], "dates_text": default_price["dates_text"],
        },
        "photos": _photos(state),
        "breadcrumbs": breadcrumbs,
    }
