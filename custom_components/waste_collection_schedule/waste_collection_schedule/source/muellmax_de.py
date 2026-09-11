import logging
import re
from html.parser import HTMLParser

import requests
from waste_collection_schedule import Collection  # type: ignore[attr-defined]
from waste_collection_schedule.exceptions import SourceArgumentNotFoundWithSuggestions
from waste_collection_schedule.service.ICS import ICS
from waste_collection_schedule.service.MuellmaxDe import SERVICE_MAP

_LOGGER = logging.getLogger(__name__)

TITLE = "Müllmax"
DESCRIPTION = "Source for Müllmax waste collection."
URL = "https://www.muellmax.de"
COUNTRY = "de"


def EXTRA_INFO():
    return [
        {
            "title": s["title"],
            "url": s["url"],
            "default_params": {"service": s["service_id"]},
        }
        for s in SERVICE_MAP
    ]


TEST_CASES = {
    # "Münster, Achatiusweg": {"service": "Awm", "mm_frm_str_sel": "Achatiusweg"},
    # "Hal, Postweg": {"service": "Hal", "mm_frm_str_sel": "Postweg"},
    # "giessen": {
    #     "service": "Lkg",
    #     "mm_frm_ort_sel": "Langgöns",
    #     "mm_frm_str_sel": "Hauptstraße",
    # },
    "USB Freiligrathstraße 55": {
        "service": "Usb",
        "mm_frm_str_sel": "Freiligrathstraße",
        "mm_frm_hnr_sel": "44791;Innenstadt;55;",
    },
    "ASH Schäferstraße 49 (plain number)": {
        "service": "Ash",
        "mm_frm_str_sel": "Schäferstraße",
        "mm_frm_hnr_sel": "49",
    },
}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "de-DE,de;q=0.9,en;q=0.8",
}

PARAM_TRANSLATIONS = {
    "de": {
        "service": "Service",
        "mm_frm_ort_sel": "Ort",
        "mm_frm_str_sel": "Straße",
        "mm_frm_hnr_sel": "Hausnummer",
    },
}


class InputCheckboxParser(HTMLParser):
    def __init__(self, startswith):
        super().__init__()
        self._startswith = startswith
        self._value = {}

    @property
    def value(self):
        return self._value

    def handle_starttag(self, tag, attrs):
        if tag == "input":
            d = dict(attrs)
            if d.get("name", "").startswith(self._startswith):
                self._value[d["name"]] = d.get("value")


class SelectOptionParser(HTMLParser):
    def __init__(self, select_name):
        super().__init__()
        self._select_name = select_name
        self._in_select = False
        self._options = []

    @property
    def options(self):
        return self._options

    def handle_starttag(self, tag, attrs):
        d = dict(attrs)
        if tag == "select" and d.get("name") == self._select_name:
            self._in_select = True
        if tag == "option" and self._in_select:
            value = d.get("value", "")
            if value:
                self._options.append(value)

    def handle_endtag(self, tag):
        if tag == "select":
            self._in_select = False


class InputTextParser(HTMLParser):
    def __init__(self, **identifiers):
        super().__init__()
        self._identifiers = identifiers
        self._value = None

    @property
    def value(self):
        return self._value

    def handle_starttag(self, tag, attrs):
        if tag == "input":
            d = dict(attrs)
            for key, value in self._identifiers.items():
                if key not in d or d[key] != value:
                    return
            self._value = d.get("value")


class SubmitButtonParser(HTMLParser):
    """Extract all submit/image input names and values."""

    def __init__(self):
        super().__init__()
        self.buttons = {}

    def handle_starttag(self, tag, attrs):
        if tag == "input":
            d = dict(attrs)
            input_type = d.get("type", "").lower()
            name = d.get("name", "")
            if input_type in ("submit", "image") and name:
                self.buttons[name] = d.get("value", "")


def _extract_session(html: str) -> str | None:
    """Extract mm_ses value from HTML response."""
    p = InputTextParser(name="mm_ses")
    p.feed(html)
    return p.value


def _accept_privacy(session: requests.Session, url: str, html: str) -> str:
    """Detect and accept a Müllmax privacy/cookie consent page.

    Returns the HTML of the actual start page (either the original html
    if no consent page was detected, or the response after accepting).
    """
    if "mm_ses" in html:
        return html

    bp = SubmitButtonParser()
    bp.feed(html)

    consent_button = None
    for name, value in bp.buttons.items():
        if re.search(r"mm_dse|mm_.*ok|mm_.*accept|mm_.*weiter", name, re.I):
            consent_button = (name, value)
            break

    if consent_button is None:
        for name, value in bp.buttons.items():
            if name.startswith("mm_"):
                consent_button = (name, value)
                break

    if consent_button is None:
        return html

    _LOGGER.debug("Detected consent page, accepting via %s", consent_button[0])

    ses = _extract_session(html)
    args = {}
    if ses:
        args["mm_ses"] = ses

    if consent_button[0].endswith((".x", ".y")):
        base = consent_button[0].rsplit(".", 1)[0]
        args[f"{base}.x"] = 0
        args[f"{base}.y"] = 0
    else:
        args[consent_button[0]] = consent_button[1]

    r = session.post(url, data=args, headers={**HEADERS, "Referer": url})
    r.raise_for_status()
    return r.text


class Source:
    def __init__(
        self,
        service,
        mm_frm_ort_sel=None,
        mm_frm_str_sel=None,
        mm_frm_hnr_sel=None,
    ):
        self._service = service
        self._mm_frm_ort_sel = mm_frm_ort_sel
        self._mm_frm_str_sel = mm_frm_str_sel
        self._mm_frm_hnr_sel = mm_frm_hnr_sel
        self._ics = ICS()

    def _post(self, session, url, args):
        r = session.post(url, data=args, headers={**HEADERS, "Referer": url})
        r.raise_for_status()
        return r

    def fetch(self):
        url = (
            f"https://www.muellmax.de/abfallkalender/"
            f"{self._service.lower()}/res/"
            f"{self._service}Start.php"
        )
        session = requests.Session()

        r = session.get(url, headers=HEADERS)
        r.raise_for_status()

        html = _accept_privacy(session, url, r.text)

        mm_ses_val = _extract_session(html)
        if mm_ses_val is None:
            raise ValueError(
                "Could not find session token on the start page. "
                "The Müllmax website may have changed its layout."
            )

        # select "Abfuhrtermine"
        args = {"mm_ses": mm_ses_val, "mm_aus_ort.x": 0, "mm_aus_ort.y": 0}
        r = self._post(session, url, args)
        mm_ses_val = _extract_session(r.text) or mm_ses_val

        if self._mm_frm_ort_sel is not None:
            args = {
                "mm_ses": mm_ses_val,
                "xxx": 1,
                "mm_frm_ort_sel": self._mm_frm_ort_sel,
                "mm_aus_ort_submit": "weiter",
            }
            r = self._post(session, url, args)
            mm_ses_val = _extract_session(r.text) or mm_ses_val

        if self._mm_frm_str_sel is not None:
            # search for street
            args = {
                "mm_ses": mm_ses_val,
                "xxx": 1,
                "mm_frm_str_name": self._mm_frm_str_sel,
                "mm_aus_str_txt_submit": "suchen",
            }
            r = self._post(session, url, args)
            mm_ses_val = _extract_session(r.text) or mm_ses_val

            # select street
            args = {
                "mm_ses": mm_ses_val,
                "xxx": 1,
                "mm_frm_str_sel": self._mm_frm_str_sel,
                "mm_aus_str_sel_submit": "weiter",
            }
            r = self._post(session, url, args)
            mm_ses_val = _extract_session(r.text) or mm_ses_val

        # auto-detect if house number selection is required
        if "mm_frm_hnr_sel" in r.text:
            hnr_value = self._mm_frm_hnr_sel
            if hnr_value is None:
                op = SelectOptionParser("mm_frm_hnr_sel")
                op.feed(r.text)
                raise SourceArgumentNotFoundWithSuggestions(
                    "mm_frm_hnr_sel", "", op.options
                )

            if ";" not in str(hnr_value):
                op = SelectOptionParser("mm_frm_hnr_sel")
                op.feed(r.text)
                matches = [o for o in op.options if o.split(";")[2] == str(hnr_value)]
                if len(matches) == 1:
                    hnr_value = matches[0]
                elif len(matches) == 0:
                    raise SourceArgumentNotFoundWithSuggestions(
                        "mm_frm_hnr_sel", str(hnr_value), op.options
                    )
                else:
                    raise SourceArgumentNotFoundWithSuggestions(
                        "mm_frm_hnr_sel", str(hnr_value), matches
                    )

            args = {
                "mm_ses": mm_ses_val,
                "xxx": 1,
                "mm_frm_hnr_sel": hnr_value,
                "mm_aus_hnr_sel_submit": "weiter",
            }
            r = self._post(session, url, args)
            mm_ses_val = _extract_session(r.text) or mm_ses_val

        # select iCal output
        args = {
            "mm_ses": mm_ses_val,
            "xxx": 1,
            "mm_ica_auswahl": "iCalendar-Datei",
        }
        r = self._post(session, url, args)
        mm_ses_val = _extract_session(r.text) or mm_ses_val

        mm_frm_fra = InputCheckboxParser(startswith="mm_frm_fra")
        mm_frm_fra.feed(r.text)

        # download ICS file
        args = {"mm_ses": mm_ses_val, "xxx": 1, "mm_frm_type": "termine"}
        args.update(mm_frm_fra.value)
        args.update({"mm_ica_gen": "iCalendar-Datei laden"})
        r = self._post(session, url, args)

        content_type = r.headers.get("content-type", "")
        if "text/calendar" in content_type or r.text.strip().startswith("BEGIN:"):
            dates = self._ics.convert(r.text)
            return [Collection(d[0], d[1]) for d in dates]

        # response is HTML, not ICS — diagnose what went wrong
        _LOGGER.debug(
            "ICS download returned HTML (Content-Type: %s), length=%d",
            content_type,
            len(r.text),
        )
        raise ValueError(
            "Got invalid response from the server, please recheck your arguments"
        )
