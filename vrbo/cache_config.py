"""Cache TTL per /vrbo/* endpoint (cache.py, keyed on the validated
params). The property endpoints share ONE cached page parse
(api._record, vrbo.property): details + reviews + calendar + price on the
same listing cost one property-page fetch for the id mapping and the
static content.
"""
from datetime import timedelta

# The listing page: description, amenities, rules, host, photos. Hosts
# edit listings rarely; the default-dates headline price is refreshed by
# the dated endpoints instead.
PROPERTY_CACHE = timedelta(hours=12)
# Region ids never change; the suggestion ranking barely does.
AUTOCOMPLETE_CACHE = timedelta(days=30)
# Search results carry live availability and nightly prices for the dates.
SEARCH_CACHE = timedelta(hours=6)
# The filter vocabulary + counts for a destination.
FILTERS_CACHE = timedelta(hours=24)
# Reviews only grow; a new one waits at most a day.
REVIEWS_CACHE = timedelta(hours=24)
# The 2-year calendar moves with every booking.
CALENDAR_CACHE = timedelta(hours=6)
# A dated quote is the most perishable thing here.
PRICE_CACHE = timedelta(hours=3)
