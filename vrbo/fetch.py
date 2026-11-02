"""Vrbo transport: plain curl_cffi with browser-impersonated TLS, no
browser. Vrbo is Expedia Group's shopping-pwa (siteId 9001001); every
surface this package reads is public and anonymous:

  * PROPERTY   GET /<listingId>  (bare path, no query string!) — the
               server-rendered listing page whose `window.__APOLLO_STATE__`
               carries PropertyInfo:<expediaId> (description, amenities,
               rules, host, gallery, rating, location, the headline price
               for the site's default dates). Any query string on that URL
               (?adults=, ?chkin=, ?dateless=) yields a client-rendered
               shell with no PropertyInfo, so the page is always fetched
               bare. GET /pdp/lo/<expediaId> 302s to the listing URL (the
               reverse id mapping); an unknown id 302s to the homepage.

  * SEARCH     POST /graphql  propertySearch — 50 LodgingCards per page by
               resultsStartingIndex, map markers with coordinates, the
               matched count; destination by regionId / free text /
               coordinates / mapBounds, filters as criteria selections.
               (The /search HTML page server-renders page 1 only; its URL
               paging params are ignored, so paging is GraphQL only.)
  * FILTERS    GET /search?…  — page 1's universalSortAndFilter block: the
               destination's filter groups with counts and the sort list.
  * REVIEWS    POST /graphql  propertyInfo.reviewInfo — up to 50 per call
               by startIndex / size, sorted and filtered.
  * CALENDAR   POST /graphql  propertyRatesDateSelector — ~2 years of
               per-day availability, price and stay constraints.
  * PRICE      POST /graphql  propertyOffers — the headline quote for a
               stay (nightly, total, cancellation summary, sold-out).
  * TYPEAHEAD  GET /api/v4/typeahead/<q> — region suggestions (gaiaId =
               regionId, type, coordinates).

GraphQL takes the full query text (no persisted hashes) with the
`client-info` / `x-page-id` headers and the cookie jar of ONE earlier page
load on the same session (DUAID etc.): without it the endpoint answers the
captcha page. A session therefore warms itself with the homepage before
its first GraphQL call.

Anti-bot (research 2026-10-30): the block is Expedia's own "Bot or Not?"
reCAPTCHA page served as HTTP 429 (not an Akamai sensor check), keyed on
the exit IP's reputation and the endpoint: the bare listing page passes on
nearly every exit, /search, /graphql and the typeahead only on exits the
risk engine trusts. Only the chrome TLS profile passes (safari17_0 and
edge101 are refused on the same clean exit; since 2026-11-02 the generic
chrome target is refused too and only chrome150 / safari184_ios pass, see
IMPERSONATE_TARGETS). Rotating cookie jars changes nothing, rotating the
exit and the fingerprint does, so a block drops the session and the retry
rides a fresh exit on the other fingerprint — up to config.VRBO_EXIT_ATTEMPTS times, since a
clean exit is a lottery on the residential tier.

Egress (config.VRBO_EXIT, pod probe 2026-10-31 in config.py): the pod's
own IP passes all four surfaces; after a block new sessions ride a US
residential exit for config.VRBO_BLOCK_COOLDOWN seconds.

Failure taxonomy (scraper_errors, mapped to HTTP by route_glue):
  VrboUpstreamError   transport failure / 5xx / malformed payload  — retryable
  VrboBlocked         401/403/429 or the "Bot or Not?" page          — retryable
  VrboBadRequest      upstream 400                                  — never retried
  VrboNotFound        unknown listing id / GraphQL "Invalid PropertyId" — never retried
"""
import json
import os
import re
import sys
import threading
import time
import uuid
from urllib.parse import quote, urlencode

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
import egress
from scraper_errors import BadRequest, Blocked, NotFound, UpstreamError
from vrbo.refs import LISTING_ID_RE, SITE

# curl_cffi impersonation targets, in the order a thread tries them: a block
# drops the session and its replacement rides the next one. Pod probe
# 2026-11-02 (curl_cffi 0.17.0, 14 targets on the pod's own IP): the generic
# "chrome" target and every chrome131-146 / safari / firefox / edge build drew
# the 429 captcha on the homepage, the listing page and /graphql; chrome150 and
# safari184_ios passed all three, 8/8 GraphQL calls in a burst. Exits disagree
# per fingerprint (a US residential exit refused chrome150 and passed
# safari184_ios), hence the alternation.
IMPERSONATE_TARGETS = ("chrome150", "safari184_ios")
IMPERSONATE = IMPERSONATE_TARGETS[0]
PAGE_TIMEOUT = 45       # listing pages are ~1.1 MB, search pages ~1.9 MB
GRAPHQL_TIMEOUT = 40    # a 50-card search answers in ~2 s, the calendar in ~1 s
TYPEAHEAD_TIMEOUT = 15
BLOCK_RETRY_PAUSE = 0.3   # a block is cured by a fresh exit, not by waiting

# Expedia point of sale for vrbo.com (US). Prices follow the POS, not the
# context's currency field (EUR/GBP in the context still price in USD;
# verified 2026-10-30), so the US site is the only one served.
SITE_ID = 9001001
TPID = 9001
EAPID = 1
LOCALE = "en_US"
CURRENCY = "USD"

PAGE_HEADERS = {
    "accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
               "image/avif,image/webp,*/*;q=0.8"),
    "upgrade-insecure-requests": "1",
}
GRAPHQL_HEADERS = {
    "accept": "*/*",
    "content-type": "application/json",
    "origin": SITE,
    "client-info": "shopping-pwa,unknown,unknown",
}
PAGE_ID_PROPERTY = "page.Hotels.Infosite.Information,H,20"
PAGE_ID_SEARCH = "page.Hotel-Search,H,20"

_TITLE_RE = re.compile(r"<title>([^<]*)</title>", re.I)
_CHALLENGE_TITLE = "Bot or Not?"


class VrboUpstreamError(UpstreamError):
    """Transport failure, 5xx or a malformed payload — retryable."""


class VrboBlocked(VrboUpstreamError, Blocked):
    """401/403/429 or the "Bot or Not?" page — retryable on a fresh exit."""


class VrboBadRequest(BadRequest):
    """Upstream 400 — never retried."""


class VrboNotFound(NotFound):
    """The listing does not exist — never retried."""


# ---- sessions ----------------------------------------------------------------
# One session per cheroot worker thread: a curl handle must not be shared
# across threads. The session keeps its cookie jar (the GraphQL endpoint
# needs one page load's cookies) and its sticky exit for its lifetime; a
# retryable failure drops it so the retry rides a fresh exit.
_local = threading.local()
# Process-wide: after a block on the direct tier every new session rides the
# residential fallback (config.vrbo_fallback_proxy()) until this instant.
_fallback_until = 0.0
_fallback_lock = threading.Lock()


def _proxy():
    proxy = config.vrbo_proxy()
    if proxy is None and time.time() < _fallback_until:
        proxy = config.vrbo_fallback_proxy()
    return proxy


def _session():
    sess = getattr(_local, "session", None)
    if sess is None:
        from curl_cffi import requests as curl_requests
        proxy = _proxy()
        target = IMPERSONATE_TARGETS[getattr(_local, "target_index", 0) % len(IMPERSONATE_TARGETS)]
        sess = curl_requests.Session(impersonate=target)
        if proxy:
            sess.proxies = {"http": proxy, "https": proxy}
        sess.headers["accept-language"] = egress.accept_language(proxy)
        sess.vrbo_warm = False
        sess.vrbo_proxy = proxy
        _local.session = sess
    return sess


def _drop_session(blocked=False):
    """Close the thread's session so the retry rides a fresh exit. A block
    moves the thread to the next fingerprint; once every fingerprint has
    been refused on the direct tier, new sessions ride the residential
    fallback for VRBO_BLOCK_COOLDOWN seconds (a single refused fingerprint
    is not enough: on 2026-11-02 the pod IP refused one and passed the other,
    while most residential exits refused both)."""
    global _fallback_until
    sess = getattr(_local, "session", None)
    _local.session = None
    was_direct = sess is not None and getattr(sess, "vrbo_proxy", None) is None
    if sess is not None:
        try:
            sess.close()
        except Exception:
            pass
    if not blocked:
        return
    _local.target_index = getattr(_local, "target_index", 0) + 1
    every_fingerprint_refused = _local.target_index % len(IMPERSONATE_TARGETS) == 0
    if (was_direct and every_fingerprint_refused and config.vrbo_proxy() is None
            and config.VRBO_BLOCK_COOLDOWN):
        with _fallback_lock:
            _fallback_until = max(_fallback_until, time.time() + config.VRBO_BLOCK_COOLDOWN)


def _retrying(fn):
    """Run fn() under the retry policy: a block gets config.VRBO_EXIT_ATTEMPTS
    tries on fresh exits with no backoff (the cure is the exit, not time),
    any other upstream failure config.MAX_RETRIES with backoff; NotFound /
    BadRequest surface immediately."""
    last = None
    attempts = max(config.VRBO_EXIT_ATTEMPTS, config.MAX_RETRIES)
    plain_failures = 0
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except VrboBlocked as e:
            last = e
            _drop_session(blocked=True)
            if attempt < attempts:
                time.sleep(BLOCK_RETRY_PAUSE)
        except VrboUpstreamError as e:
            last = e
            plain_failures += 1
            _drop_session()
            if plain_failures >= config.MAX_RETRIES:
                break
            time.sleep(config.RETRY_BACKOFF * plain_failures)
    raise last


def page_title(text):
    match = _TITLE_RE.search(text or "")
    return match.group(1).strip() if match else ""


def is_challenge(text):
    return page_title(text) == _CHALLENGE_TITLE


def _classify(resp, label):
    """Map an HTTP response to the error taxonomy; return the body text."""
    if resp.status_code == 404:
        raise VrboNotFound(f"{label} not found")
    if resp.status_code == 400:
        raise VrboBadRequest(resp.text[:200])
    if resp.status_code in (401, 403, 429):
        raise VrboBlocked(f"HTTP {resp.status_code} for {label}")
    if resp.status_code != 200:
        raise VrboUpstreamError(f"HTTP {resp.status_code} for {label}")
    if is_challenge(resp.text):
        raise VrboBlocked(f"challenge page for {label}")
    return resp.text


def _get(path, label, headers, timeout):
    sess = _session()
    try:
        resp = sess.get(SITE + path, headers=headers, timeout=timeout, allow_redirects=True)
    except Exception as e:
        raise VrboUpstreamError(f"request failed: {type(e).__name__}: {e}")
    return resp, _classify(resp, label)


def _warm(sess):
    """One page load so the session holds the site's cookies (DUAID, the
    POS cookies) before its first GraphQL call."""
    if getattr(sess, "vrbo_warm", False):
        return
    try:
        resp = sess.get(SITE + "/", headers=PAGE_HEADERS, timeout=PAGE_TIMEOUT)
    except Exception as e:
        raise VrboUpstreamError(f"warm-up failed: {type(e).__name__}: {e}")
    _classify(resp, "homepage")
    sess.vrbo_warm = True


def _duaid(sess):
    return sess.cookies.get("DUAID") or str(uuid.uuid4())


def _context(sess):
    return {
        "siteId": SITE_ID, "locale": LOCALE, "eapid": EAPID, "tpid": TPID, "currency": CURRENCY,
        "device": {"type": "DESKTOP"},
        "identity": {"duaid": _duaid(sess), "authState": "ANONYMOUS"},
        "privacyTrackingState": "CAN_TRACK",
        "debugContext": {"abacusOverrides": []},
    }


_NOT_FOUND_MARKERS = ("Invalid PropertyId", "Invalid parameter [parameterName=propertyId")


def _graphql(operation, query, variables, page_id, referer):
    """POST one operation; returns its `data`. GraphQL errors naming the
    property id are NotFound, others upstream failures."""
    def once():
        sess = _session()
        _warm(sess)
        variables["context"] = _context(sess)
        body = [{"operationName": operation, "variables": variables, "query": query}]
        headers = dict(GRAPHQL_HEADERS, **{"x-page-id": page_id, "referer": referer})
        try:
            resp = sess.post(SITE + "/graphql", json=body, headers=headers, timeout=GRAPHQL_TIMEOUT)
        except Exception as e:
            raise VrboUpstreamError(f"request failed: {type(e).__name__}: {e}")
        text = _classify(resp, operation)
        try:
            payload = resp.json()
        except Exception:
            raise VrboBlocked(f"non-JSON {operation} response: {text[:120]}")
        if isinstance(payload, list):
            payload = payload[0] if payload else {}
        if not isinstance(payload, dict):
            raise VrboUpstreamError(f"unexpected {operation} payload: {text[:120]}")
        errors = payload.get("errors") or []
        data = payload.get("data") or {}
        if errors:
            messages = "; ".join(str(e.get("message", ""))[:200] for e in errors if isinstance(e, dict))
            if any(marker in messages for marker in _NOT_FOUND_MARKERS):
                raise VrboNotFound("property not found")
            if not data or all(v is None for v in data.values()):
                raise VrboUpstreamError(f"{operation}: {messages[:300]}")
        return data
    return _retrying(once)


# ---- property page ----------------------------------------------------------------

def _final_path(resp):
    url = getattr(resp, "url", "") or ""
    return url.split("vrbo.com", 1)[1] if "vrbo.com" in url else url


def _candidate_paths(ref):
    """A listing-shaped id ("2165911", "4193579ha") is a listing URL; a bare
    number may also be an Expedia property id, reached through /pdp/lo/
    (which 302s to the listing URL)."""
    if LISTING_ID_RE.match(ref) and not ref.endswith("ha"):
        return [f"/{ref}", f"/pdp/lo/{ref}"]
    if LISTING_ID_RE.match(ref):
        return [f"/{ref}"]
    return [f"/pdp/lo/{ref}"]


def fetch_property_page(ref):
    """GET the bare listing page for a listing id / Expedia id; returns
    (html, listing_id). Unknown ids 302 to the homepage -> VrboNotFound."""
    def once():
        paths = _candidate_paths(ref)
        for index, path in enumerate(paths):
            try:
                resp, text = _get(path, f"property {ref}", PAGE_HEADERS, PAGE_TIMEOUT)
            except VrboNotFound:
                if index == len(paths) - 1:
                    raise
                continue          # a 404 on the listing path; the id may still be an Expedia id
            final = _final_path(resp)
            match = re.match(r"^/([0-9]+(?:ha)?)(?:[/?#]|$)", final, re.I)
            if not match:          # redirected to the homepage or elsewhere
                continue
            if "PropertyInfo:" not in text:
                raise VrboUpstreamError(f"property {ref}: page without server-rendered state")
            return text, match.group(1).lower()
        raise VrboNotFound(f"property {ref} not found")
    return _retrying(once)


# ---- typeahead ----------------------------------------------------------------------

def fetch_autocomplete(query, limit=10):
    """GET /api/v4/typeahead/<q> -> the raw JSON (sr[] suggestions).
    regiontype=2047 (every region type) keeps the ranking sane: without it
    "Bali" leads with Baltimore (verified 2026-10-30)."""
    params = {"client": "SearchForm", "lob": "HOTELS", "locale": LOCALE, "siteid": SITE_ID,
              "maxresults": limit, "format": "json", "regiontype": 2047}
    path = f"/api/v4/typeahead/{quote(query, safe='')}?{urlencode(params)}"

    def once():
        resp, text = _get(path, "typeahead", {"accept": "application/json, text/plain, */*"}, TYPEAHEAD_TIMEOUT)
        try:
            payload = resp.json()
        except Exception:
            raise VrboBlocked(f"non-JSON typeahead response: {text[:120]}")
        if not isinstance(payload, dict):
            raise VrboUpstreamError(f"unexpected typeahead payload: {text[:120]}")
        return payload
    return _retrying(once)


# ---- search ---------------------------------------------------------------------------

SEARCH_QUERY = """
query PropertySearchQuery($context: ContextInput!, $criteria: PropertySearchCriteriaInput!) {
  propertySearch(context: $context, criteria: $criteria) {
    summary { matchedPropertiesSize resultsHeading }
    pagination { paginationLabel subSets { nextSubSet { size startingIndex } previousSubset { size startingIndex } } }
    criteria { primary { destination { regionId regionName } dateRange { checkInDate { day month year } checkOutDate { day month year } } rooms { adults children { age } } } }
    propertySearchListings {
      __typename
      ... on LodgingCard {
        id
        headingSection { heading messages { text } featuredMessages { text } }
        summarySections { ... on LodgingCardProductSummarySection { reviewSummary { graphic { ... on EGDSStandardBadge { text accessibility } } title { shoppingProductTitle { ... on EGDSStylizedText { text accessibility } } } subtexts { ... on ShoppingProductContentStylizedTexts { shoppingProductTitle { ... on EGDSStylizedText { text accessibility } } } ... on ProductTextInfoSection { title { text } accessibilityText } } } } }
        priceSection { priceSummary { displayMessages { lineItems { __typename ... on DisplayPrice { role price { formatted } } ... on LodgingEnrichedMessage { value state } } } options { displayPrice { formatted } strikeOut { formatted } accessibilityLabel } priceMessaging { value } } standardBadge { standardBadge { text } } }
        cardLink { resource { value } }
        mediaSection { badges { primaryBadge { text } secondaryBadge { text } tertiaryBadge { text } } gallery { media { ... on LodgingMediaItem { id media { ... on Image { url description } } } } } }
      }
    }
    dynamicMap { map { markers { ... on EGDSMapFeature { id type markerStatus text name qualifiers markerPosition { latitude longitude } } } } }
  }
}"""

SEARCH_PAGE_SIZE = 50   # the site's own page size


def _date_input(iso):
    year, month, day = (int(x) for x in iso.split("-"))
    return {"day": day, "month": month, "year": year}


def date_range(checkin, checkout):
    if not (checkin and checkout):
        return None
    return {"checkInDate": _date_input(checkin), "checkOutDate": _date_input(checkout)}


def rooms(adults, children):
    return [{"adults": int(adults or 2), "children": [{"age": int(a)} for a in (children or [])]}]


def fetch_search(destination, start, checkin=None, checkout=None, adults=2, children=None,
                 sort="RECOMMENDED", selections=None, ranges=None, booleans=None,
                 size=SEARCH_PAGE_SIZE):
    """POST propertySearch for one page; `destination` is the GraphQL
    destination input ({regionId} | {regionName} | {coordinates} |
    {mapBounds}); `selections` [{id, value}], `ranges` [{id, min, max}],
    `booleans` [{id, value}] are the site's own criteria tokens."""
    criteria = {
        "primary": {"dateRange": date_range(checkin, checkout), "destination": destination,
                    "rooms": rooms(adults, children)},
        "secondary": {
            "booleans": list(booleans or []),
            "counts": [{"id": "resultsStartingIndex", "value": int(start)},
                       {"id": "resultsSize", "value": int(size)}],
            "ranges": list(ranges or []),
            "selections": [{"id": "sort", "value": sort}] + list(selections or []),
        },
    }
    data = _graphql("PropertySearchQuery", SEARCH_QUERY, {"criteria": criteria},
                    PAGE_ID_SEARCH, SITE + "/search")
    result = data.get("propertySearch")
    if not isinstance(result, dict):
        raise VrboUpstreamError("search: no propertySearch in the response")
    return result


def search_page_path(destination, checkin=None, checkout=None, adults=2):
    """The /search URL the filters endpoint loads (page 1 is server-rendered
    with the destination's filter vocabulary)."""
    params = {}
    if destination.get("regionId"):
        params["regionId"] = destination["regionId"]
    elif destination.get("regionName"):
        params["destination"] = destination["regionName"]
    elif destination.get("coordinates"):
        c = destination["coordinates"]
        params["latLong"] = f"{c['latitude']},{c['longitude']}"
    if checkin and checkout:
        params["startDate"], params["endDate"] = checkin, checkout
    params["adults"] = int(adults or 2)
    query = urlencode(params)
    if destination.get("mapBounds"):
        sw, ne = destination["mapBounds"]
        query += f"&mapBounds={sw['latitude']},{sw['longitude']}&mapBounds={ne['latitude']},{ne['longitude']}"
    return "/search?" + query


def fetch_search_page(destination, checkin=None, checkout=None, adults=2):
    """GET the server-rendered search page (filters + sort vocabulary)."""
    path = search_page_path(destination, checkin, checkout, adults)

    def once():
        _, text = _get(path, "search page", PAGE_HEADERS, PAGE_TIMEOUT)
        if "__APOLLO_STATE__" not in text:
            raise VrboUpstreamError("search page without server-rendered state")
        return text
    return _retrying(once)


# ---- reviews ------------------------------------------------------------------------------

REVIEWS_QUERY = """
query ReviewsQuery($context: ContextInput!, $propertyId: String!, $searchCriteria: PropertySearchCriteriaInput!) {
  propertyInfo(propertyId: $propertyId, context: $context) {
    id
    reviewInfo(searchCriteria: $searchCriteria) {
      summary { totalCount { raw formatted } averageOverallRating { raw formatted } superlative paginateAction { text } reviewSummaryDetails { label ratingPercentage formattedRatingOutOfMax } propertyReviewCountDetails { shortDescription } }
      reviews {
        id superlative locale title brandType disclaimer text submissionTimeLocalized stayDuration travelers
        reviewScoreWithDescription { label value }
        reviewAuthorAttribution { text }
        managementResponses { id header { text } response }
        photos { url description }
        themes { label }
        reviewInteractionSections { primaryDisplayString reviewInteractionType }
        propertyReviewSource { text { value } }
        translationInfo { targetLocale }
      }
      sortAndFilter { sortAndFilter { name label options { label optionValue isSelected } } }
    }
  }
}"""

REVIEWS_MAX_SIZE = 50   # the endpoint caps a call at 50 (verified 2026-10-30)


def fetch_reviews(expedia_id, start=0, size=20, sort="NEWEST_TO_OLDEST", traveler_type=None, search_term=None):
    selections = [{"id": "sortBy", "value": sort}]
    if traveler_type:
        selections.append({"id": "travelerType", "value": traveler_type})
    if search_term:
        selections.append({"id": "searchTerm", "value": search_term})
    variables = {"propertyId": str(expedia_id), "searchCriteria": {
        "primary": {"dateRange": None, "destination": {"regionId": None}, "rooms": [{"adults": 2}]},
        "secondary": {
            "booleans": [{"id": "includeRecentReviews", "value": True},
                         {"id": "includeRatingsOnlyReviews", "value": True}],
            "counts": [{"id": "startIndex", "value": int(start)}, {"id": "size", "value": int(size)}],
            "selections": selections,
        }}}
    data = _graphql("ReviewsQuery", REVIEWS_QUERY, variables, PAGE_ID_PROPERTY, f"{SITE}/pdp/lo/{expedia_id}")
    info = (data.get("propertyInfo") or {}).get("reviewInfo")
    if not isinstance(info, dict):
        raise VrboUpstreamError("reviews: no reviewInfo in the response")
    return info


# ---- calendar ------------------------------------------------------------------------------

CALENDAR_QUERY = """
query PropertyRatesDateSelectorQuery($context: ContextInput!, $eid: ID!, $dateRange: DateRangeInput) {
  propertyRatesDateSelector(context: $context, dateRange: $dateRange, eid: $eid) {
    configuration { currentDate { day month year } beginDate { day month year } endDate { day month year } availableByDefault calendarTemplateType }
    days { date { day month year } displayPrice available checkinValidity checkoutValidity stayConstraints { stayIncrementInDays minimumStayInDays maximumStayInDays } }
    validationMessage
  }
}"""


def fetch_calendar(expedia_id):
    data = _graphql("PropertyRatesDateSelectorQuery", CALENDAR_QUERY,
                    {"eid": str(expedia_id), "dateRange": None}, PAGE_ID_PROPERTY, f"{SITE}/pdp/lo/{expedia_id}")
    selector = data.get("propertyRatesDateSelector")
    if not isinstance(selector, dict):
        raise VrboUpstreamError("calendar: no propertyRatesDateSelector in the response")
    return selector


# ---- dated offer ----------------------------------------------------------------------------

OFFER_QUERY = """
query SingleOfferQuery($context: ContextInput!, $propertyId: String!, $searchCriteria: PropertySearchCriteriaInput) {
  propertyOffers(context: $context, propertyId: $propertyId, searchCriteria: $searchCriteria) {
    id soldOut
    errorMessage { title { text } }
    stickyBar { price { formattedDisplayPrice } propertyPrice { displayMessages { lineItems { __typename ... on DisplayPrice { role price { formatted } } ... on LodgingEnrichedMessage { value state } } } } }
    singleUnitOffer { accessibilityLabel totalPrice { label { value } amount { value } } }
    singleUnitOfferDialog { content { ratePlans { id name
      paymentPolicy { heading descriptions { header { text } infoItems { ... on PropertyInfoItem { text } } } }
      priceDetails { availability { available scarcityMessage } totalPriceMessage hotelCollect paymentModel noCreditCard
        propertyNaturalKeys { id checkIn { day month year } checkOut { day month year } inventoryType ratePlanId roomTypeId petsIncluded }
        price { lead { formatted } displayMessages { lineItems { __typename ... on DisplayPrice { role price { formatted } } ... on LodgingEnrichedMessage { value state } } } options { displayPrice { formatted } strikeOut { formatted } accessibilityLabel } priceMessaging { value } }
      }
      highlightedMessages { __typename ... on LodgingEnrichedMessage { value } ... on LodgingPlainMessage { value } ... on LodgingPlainDialog { trigger { value } } ... on LodgingPolicyDialog { trigger { value } toolbar { title } } }
      reserveCallToAction { __typename ... on LodgingForm { action } }
    } } }
  }
}"""


def fetch_offer(expedia_id, checkin, checkout, adults=2, children=None):
    variables = {"propertyId": str(expedia_id), "searchCriteria": {
        "primary": {"dateRange": date_range(checkin, checkout), "destination": {"regionId": None},
                    "rooms": rooms(adults, children)},
        "secondary": {"booleans": [], "counts": [], "ranges": [], "selections": []}}}
    data = _graphql("SingleOfferQuery", OFFER_QUERY, variables, PAGE_ID_PROPERTY, f"{SITE}/pdp/lo/{expedia_id}")
    offers = data.get("propertyOffers")
    if not isinstance(offers, dict):
        raise VrboUpstreamError("price: no propertyOffers in the response")
    return offers


__all__ = [name for name in dir() if name.startswith(("fetch_", "Vrbo"))] + ["SEARCH_PAGE_SIZE", "REVIEWS_MAX_SIZE"]

if __name__ == "__main__":   # python vrbo/fetch.py <listing id>
    ref = sys.argv[1] if len(sys.argv) > 1 else "2165911"
    html, listing = fetch_property_page(ref)
    print(listing, len(html), page_title(html))
    print(json.dumps(fetch_autocomplete("Miami", 3), indent=1)[:800])
