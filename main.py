"""Use the scraper straight from Python — no server needed.

    python main.py

Every function returns the same JSON the API does; results are written to
output/*.json. Params go through the same schemas the API uses, so defaults
are filled in and a listing ID or a vrbo.com link both work.
"""
import json
import os

from vrbo import api, schemas

os.makedirs("output", exist_ok=True)


def save(name, data):
    path = os.path.join("output", name)
    with open(path, "w") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"saved {path}")


def run(schema_cls, fn, **params):
    return fn(**schema_cls().load(params))


if __name__ == "__main__":
    # a listing ID or any vrbo.com property link — the whole listing in one call
    save("property_363759ha.json",
         run(schemas.PropertyDetailsSchema, api.property_details, property="363759ha"))

    # 50 rentals per page with live prices for your dates
    save("search_malibu.json",
         run(schemas.SearchSchema, api.properties_search, location="Malibu, California",
             checkin="2027-06-14", checkout="2027-06-18"))

    # guest reviews, newest first, 20 per page
    save("reviews_363759ha.json",
         run(schemas.PropertyReviewsSchema, api.property_reviews, property="363759ha"))
