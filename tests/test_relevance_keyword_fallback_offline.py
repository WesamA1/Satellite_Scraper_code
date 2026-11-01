"""Offline tests for the relevance-matching fix: europeana, dpla, smithsonian,
and flickr previously required the literal search term in an item's title,
ignoring the domain_keywords fallback wikimedia already had. That meant real
satellite family names (catalog designations like "3CAT") almost never
matched anything, even when the underlying API genuinely had relevant items
titled with a generic space-related word instead of the exact family name.

No network calls -- these call relevant_results() directly against
hand-built fixtures shaped like each API's real response.
"""

from scraper.sources import dpla_digital_public_library_of_america as dpla
from scraper.sources import europeana, flickr_official_agency_photostreams, smithsonian_open_access, wikimedia

# Confirmed live: this exact title (two variants) recurred dozens of times
# in one family's real Europeana hits. "Satellite" here is the mechanical
# engineering term for a type of gear/linkage, from a 19th-century
# kinematic-models collection -- nothing to do with spacecraft, but it still
# satisfied the bare "satellite" domain-keyword fallback match.
KINEMATIC_MODEL_TITLE = "HAIN LINKWORK SATELLITE MECHANISM WITH A FLEXIBLE LINK"
KINEMATIC_MODEL_TITLE_VARIANT = "HAIN LEVER-TYPE SATELLITE MECHANISM WITH A FLEXIBLE LINK"


def test_europeana_matches_on_domain_keyword_without_the_literal_family_name():
    raw = {"items": [{"title": ["A satellite in orbit over Spain"]}]}
    assert europeana.relevant_results(raw, "3CAT") == raw["items"]


def test_europeana_excludes_the_kinematic_models_false_positive():
    raw = {"items": [{"title": [KINEMATIC_MODEL_TITLE]}, {"title": [KINEMATIC_MODEL_TITLE_VARIANT]}]}
    assert europeana.relevant_results(raw, "A-1") == []


def test_europeana_still_excludes_unrelated_items():
    raw = {"items": [{"title": ["A 19th century oil painting of a harbor"]}]}
    assert europeana.relevant_results(raw, "3CAT") == []


def test_europeana_pure_term_matching_when_domain_keywords_explicitly_empty():
    raw = {"items": [{"title": ["A satellite in orbit over Spain"]}]}
    assert europeana.relevant_results(raw, "3CAT", domain_keywords=[]) == []


def test_dpla_matches_on_domain_keyword_without_the_literal_family_name():
    raw = {"docs": [{"sourceResource": {"title": ["Image of a spacecraft launch"]}}]}
    assert dpla.relevant_results(raw, "6GSTARLAB") == raw["docs"]


def test_dpla_still_excludes_unrelated_items():
    raw = {"docs": [{"sourceResource": {"title": ["A quilt from the 1800s"]}}]}
    assert dpla.relevant_results(raw, "6GSTARLAB") == []


def test_smithsonian_matches_on_domain_keyword_without_the_literal_family_name():
    raw = {
        "response": {
            "rows": [{"content": {"descriptiveNonRepeating": {"title": {"content": "Satellite model, 1:4 scale"}}}}]
        }
    }
    assert smithsonian_open_access.relevant_results(raw, "3CAT") == raw["response"]["rows"]


def test_smithsonian_still_excludes_unrelated_items():
    raw = {
        "response": {
            "rows": [{"content": {"descriptiveNonRepeating": {"title": {"content": "Ceramic vase, Ming dynasty"}}}}]
        }
    }
    assert smithsonian_open_access.relevant_results(raw, "3CAT") == []


def test_flickr_matches_on_domain_keyword_without_the_literal_family_name():
    raw = {"photos": {"photo": [{"title": "Rocket launch carrying a new spacecraft"}]}}
    assert flickr_official_agency_photostreams.relevant_results(raw, "6GSTARLAB") == raw["photos"]["photo"]


def test_flickr_still_excludes_unrelated_items():
    raw = {"photos": {"photo": [{"title": "Team photo at the annual picnic"}]}}
    assert flickr_official_agency_photostreams.relevant_results(raw, "6GSTARLAB") == []


def test_dpla_excludes_the_kinematic_models_false_positive():
    raw = {"docs": [{"sourceResource": {"title": [KINEMATIC_MODEL_TITLE]}}]}
    assert dpla.relevant_results(raw, "A-1") == []


def test_smithsonian_excludes_the_kinematic_models_false_positive():
    raw = {
        "response": {"rows": [{"content": {"descriptiveNonRepeating": {"title": {"content": KINEMATIC_MODEL_TITLE}}}}]}
    }
    assert smithsonian_open_access.relevant_results(raw, "A-1") == []


def test_flickr_excludes_the_kinematic_models_false_positive():
    raw = {"photos": {"photo": [{"title": KINEMATIC_MODEL_TITLE_VARIANT}]}}
    assert flickr_official_agency_photostreams.relevant_results(raw, "A-1") == []


def test_wikimedia_excludes_the_kinematic_models_false_positive():
    pages = {
        "1": {"title": KINEMATIC_MODEL_TITLE, "imageinfo": [{"mime": "image/jpeg"}]},
    }
    assert wikimedia.relevant_pages(pages, "A-1") == []
