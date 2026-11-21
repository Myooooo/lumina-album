"""HTTP layer split out of :mod:`photo_reviewer.server`.

``create_app`` keeps the Flask setup and the request guard; the routes
register themselves from the modules in this package, and the helpers they
share live in :mod:`photo_reviewer.web.helpers`.
"""
