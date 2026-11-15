import ipaddress
import html.parser
import math
import re
import sys
from dataclasses import dataclass
from functools import lru_cache
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


SOURCE_TIMEOUT_SECONDS = 12
MAX_SOURCE_BYTES = 5 * 1024 * 1024
SOURCE_USER_AGENT = "SourceFactCheck/1.0"
SEMANTIC_MODEL_NAME = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
SEMANTIC_MATCH_THRESHOLD = 0.32
SEMANTIC_SUPPORT_THRESHOLD = 0.72
MAX_SEMANTIC_SOURCE_SENTENCES = 12000
NLI_MODEL_NAME = "MoritzLaurer/multilingual-MiniLMv2-L6-mnli-xnli"
NLI_DECISION_THRESHOLD = 0.70
NLI_MAX_CANDIDATES = 6
NLI_MAX_CANDIDATES_PER_SOURCE = 2
NLI_BATCH_SIZE = 4
BLOCKED_SOURCE_TAGS = {"script", "style", "noscript", "svg"}
SOURCE_BLOCK_TAGS = {"article", "br", "div", "h1", "h2", "h3", "li", "p", "section"}


class SourceUnavailableError(RuntimeError):
    pass


class SourceEvaluationError(RuntimeError):
    pass


@dataclass(frozen=True)
class OfficialSourceTexts:
    primary_text: str
    context_text: str
    pages: tuple = ()
    source_urls: tuple = ()


@dataclass(frozen=True)
class SemanticSourceIndex:
    entries: tuple
    embeddings: tuple
    model: object


class OfficialSourceRedirectHandler(HTTPRedirectHandler):
    def __init__(self, allowed_hosts):
        super().__init__()
        self.allowed_hosts = frozenset(allowed_hosts)

    def redirect_request(self, request, response, code, message, headers, new_url):
        parsed_url = urlsplit(new_url)
        if (
            parsed_url.scheme != "https"
            or parsed_url.hostname not in self.allowed_hosts
        ):
            raise SourceUnavailableError(
                "Die Quelle hat auf eine nicht zugelassene Adresse weitergeleitet."
            )
        return super().redirect_request(
            request, response, code, message, headers, new_url
        )


class OfficialSourceParser(html.parser.HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title_parts = []
        self.text_parts = []
        self.title_depth = 0
        self.blocked_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag == "title":
            self.title_depth += 1
        if tag in BLOCKED_SOURCE_TAGS:
            self.blocked_depth += 1
        if tag in SOURCE_BLOCK_TAGS and not self.blocked_depth and not self.title_depth:
            self.text_parts.append("\n")

    def handle_endtag(self, tag):
        if tag == "title" and self.title_depth:
            self.title_depth -= 1
        if tag in BLOCKED_SOURCE_TAGS and self.blocked_depth:
            self.blocked_depth -= 1
        if tag in SOURCE_BLOCK_TAGS and not self.blocked_depth and not self.title_depth:
            self.text_parts.append("\n")

    def handle_data(self, data):
        if self.title_depth:
            self.title_parts.append(data)
        elif not self.blocked_depth:
            self.text_parts.append(data)


def parse_official_source_html(page_html):
    parser = OfficialSourceParser()
    parser.feed(page_html)
    parser.close()
    title = re.sub(r"\s+", " ", " ".join(parser.title_parts)).strip()
    text = "".join(parser.text_parts)
    text = re.sub(r"[ \t\f\v]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{2,}", "\n", text).strip()
    if not title or not text:
        raise SourceUnavailableError(
            "Die geladene Quelle enthält keinen lesbaren Titel oder Text."
        )
    return text


def _validate_source_urls(source_urls):
    urls = []
    for source_url in source_urls or ():
        source_url = source_url.strip()
        if not source_url:
            continue
        try:
            parsed_url = urlsplit(source_url)
            hostname = (parsed_url.hostname or "").casefold().rstrip(".")
            port = parsed_url.port
        except ValueError as exc:
            raise SourceUnavailableError(
                "Ungültige HTTPS-Quelladresse: {}".format(source_url)
            ) from exc
        if (
            parsed_url.scheme != "https"
            or not hostname
            or parsed_url.username
            or parsed_url.password
            or port == 0
            or hostname == "localhost"
            or hostname.endswith(".localhost")
        ):
            raise SourceUnavailableError(
                "Quellen müssen gültige HTTPS-Adressen ohne Zugangsdaten sein."
            )
        try:
            address = ipaddress.ip_address(hostname)
        except ValueError:
            address = None
        if address is not None and not address.is_global:
            raise SourceUnavailableError(
                "Lokale oder private IP-Adressen sind als Quelle nicht erlaubt."
            )
        urls.append(source_url)

    unique_urls = tuple(dict.fromkeys(urls))
    if not unique_urls:
        raise SourceUnavailableError("Bitte mindestens eine HTTPS-Quelle angeben.")
    return unique_urls


def _fetch_official_source_page_text(source_url, opener, allowed_hosts):
    request = Request(
        source_url,
        headers={
            "Accept": "text/html",
            "User-Agent": SOURCE_USER_AGENT,
        },
    )
    try:
        with opener.open(request, timeout=SOURCE_TIMEOUT_SECONDS) as response:
            final_url = urlsplit(response.geturl())
            if (
                final_url.scheme != "https"
                or final_url.hostname not in allowed_hosts
            ):
                raise SourceUnavailableError(
                    "Die geladene Seite stammt nicht von der ausgewählten Quelldomain."
                )
            if response.headers.get_content_type() not in {
                "text/html",
                "application/xhtml+xml",
            }:
                raise SourceUnavailableError(
                    "Die Quelle lieferte keinen HTML-Seitentext."
                )
            raw_content = response.read(MAX_SOURCE_BYTES + 1)
            if len(raw_content) > MAX_SOURCE_BYTES:
                raise SourceUnavailableError(
                    "Die Quelle überschreitet das Limit von 5 MB."
                )
            charset = response.headers.get_content_charset() or "utf-8"
    except HTTPError as exc:
        raise SourceUnavailableError(
            "Die Quelle antwortet mit HTTP {}.".format(exc.code)
        ) from exc
    except (OSError, TimeoutError, URLError) as exc:
        raise SourceUnavailableError(
            "Die Quelle konnte nicht geladen werden: {}".format(exc)
        ) from exc

    try:
        page_html = raw_content.decode(charset)
    except (LookupError, UnicodeDecodeError) as exc:
        raise SourceUnavailableError(
            "Der Quelltext konnte nicht decodiert werden."
        ) from exc
    return parse_official_source_html(page_html)


def fetch_official_source_text(source_url, opener=None):
    source_urls = _validate_source_urls((source_url,))
    allowed_hosts = {
        urlsplit(url).hostname.casefold().rstrip(".")
        for url in source_urls
    }
    if opener is None:
        opener = build_opener(OfficialSourceRedirectHandler(allowed_hosts))
    return _fetch_official_source_page_text(
        source_urls[0], opener, allowed_hosts
    )


def fetch_official_sources(opener=None, source_urls=None):
    source_urls = _validate_source_urls(source_urls)
    allowed_hosts = {
        urlsplit(source_url).hostname.casefold().rstrip(".")
        for source_url in source_urls
    }
    if opener is None:
        opener = build_opener(OfficialSourceRedirectHandler(allowed_hosts))

    pages = []
    for source_url in source_urls:
        try:
            pages.append(
                (
                    source_url,
                    _fetch_official_source_page_text(
                        source_url, opener, allowed_hosts
                    ),
                )
            )
        except SourceUnavailableError as exc:
            raise SourceUnavailableError(
                "{}\nQuelle: {}".format(exc, source_url)
            ) from exc

    return OfficialSourceTexts(
        primary_text=pages[0][1],
        context_text="\n\n".join(
            "{}\n{}".format(source_url, text) for source_url, text in pages
        ),
        pages=tuple(pages),
        source_urls=source_urls,
    )


@dataclass(frozen=True)
class CheckRule:
    topic: re.Pattern
    expected: str
    quantity: re.Pattern = None
    accepted_values: tuple = ()
    affirmation: bool = False
    contradiction: re.Pattern = None
    source_evidence: re.Pattern = None
    value_judgment: re.Pattern = None


@dataclass(frozen=True)
class OpinionContextRule:
    topic: re.Pattern
    source_evidence: tuple
    response: str


def _rule(topic, expected, quantity=None, accepted_values=(), affirmation=False,
          contradiction=None, source_evidence=None, value_judgment=None):
    return CheckRule(
        re.compile(topic, re.IGNORECASE),
        expected,
        re.compile(quantity, re.IGNORECASE) if quantity else None,
        accepted_values,
        affirmation,
        re.compile(contradiction, re.IGNORECASE) if contradiction else None,
        re.compile(source_evidence, re.IGNORECASE) if source_evidence else None,
        re.compile(value_judgment, re.IGNORECASE) if value_judgment else None,
    )


RULES = (
    _rule(
        r"\b(strecke|trasse|kilometer|km)\b",
        "Die geplante Strecke ist rund 4,7 km lang (in der bevorzugten Variante etwa 5 km).",
        r"(?P<number>\d+(?:[.,]\d+)?)\s*(?:km|kilometer)\b",
        (4.7, 5.0),
        source_evidence=r"\b(?:4[,.]7\s*km|5\s*km)\b",
    ),
    _rule(
        r"\b(stationen|haltestellen)\b",
        "Geplant sind sechs Stationen.",
        r"(?P<number>\d+|sechs)\s*(?:stationen|haltestellen|halte)\b",
        (6,),
        source_evidence=r"\b(?:sechs|6)\s+stationen\b",
    ),
    _rule(
        r"\b(fahrzeit|fahrt|fahrtzeit|dauert|minuten|min)\b",
        "Die geplante Fahrt von der Innenstadt bis zum IPAI dauert etwa 15 Minuten.",
        r"(?P<number>\d+(?:[.,]\d+)?)\s*(?:minuten|min)\b",
        (15,),
        source_evidence=r"\b15\s*(?:minuten|min)\b",
    ),
    _rule(
        r"\b(gondel|kabine)\b",
        "In eine Gondel passen maximal zehn Personen.",
        r"(?P<number>\d+|zehn)\s*(?:personen|fahrgäste|plaetze|plätze)\b",
        (10,),
        source_evidence=r"\b(?:10|zehn)\s+personen\b",
    ),
    _rule(
        r"\b(alle\s+\d|sekunden|takt|intervall|wartezeit)\b",
        "Etwa alle 20 bis 30 Sekunden soll eine Gondel an einer Station eintreffen.",
        r"(?P<number>\d+)\s*(?:-|–|bis)?\s*(?P<second>\d+)?\s*sekunden\b",
        (20, 30),
        source_evidence=r"\b20\s*(?:-|–|bis)\s*30\s*sekunden\b",
    ),
    _rule(
        r"\b(kapazität|beförder|transportiert|pro richtung|pro stunde)\b",
        "Die geplante Kapazität liegt bei bis zu 1.500 Fahrgästen pro Stunde und Richtung.",
        r"(?P<number>\d[\d.]*)\s*(?:fahrgäste|personen|menschen)?\s*(?:pro|je)\s*(?:fahrtrichtung|richtung|stunde)\b|(?P<other>\d[\d.]*)\s*(?:fahrgäste|personen|menschen)\s*(?:pro|je)\s*(?:stunde|richtung)\b",
        (1500,),
        source_evidence=r"\b1[.]?500\b.{0,100}\b(?:richtung|stunde)\b",
    ),
    _rule(
        r"\b(stützpfeiler|pfeiler|stützen|masten)\b",
        "Die Stützpfeiler sollen nach aktuellem Planungsstand maximal 55 Meter hoch werden.",
        r"(?P<number>\d+(?:[.,]\d+)?)\s*(?:meter|metern|m)\b",
        (55,),
        source_evidence=r"\b55\s*(?:meter|metern)\b",
    ),
    _rule(
        r"\b(kosten|kostet|investition|millionen|million|mio\.?|euro|€)\b",
        "Die erste Kostenschätzung für die Investition liegt bei 157 Millionen Euro (Preisstand 2026); sie muss noch validiert werden.",
        r"(?P<number>\d+(?:[.,]\d+)?)\s*(?:millionen|million|mio\.?|€|euro)\b|€\s*(?P<other>\d+(?:[.,]\d+)?)",
        (157,),
        source_evidence=r"\b157\s*(?:millionen|million|mio\.?)\b",
        value_judgment=(
            r"\b(prestige(?:projekt)?|teu(?:er|r)\w*|zu hoch|"
            r"steuer(?:geld)?verschwendung|"
            r"geldverschwendung|verschwendung|dringend\w*|priorität|"
            r"unnötig|überflüssig|sinnlos|lohnt sich|nicht gerechtfertigt)\b"
        ),
    ),
    _rule(
        r"\b(förderung|gefördert|zuschuss|fördermittel)\b",
        "Laut Stadtseite soll die Investition bis zu 90 % durch öffentliche Förderung von Land und Bund abgedeckt werden; das ist keine zugesagte Fördersumme.",
        r"(?P<number>\d+(?:[.,]\d+)?)\s*(?:%|\bprozent\b)",
        (90,),
        source_evidence=r"\b90\s*(?:%|prozent)\b",
    ),
    _rule(
        r"\bschien\w*|bahnanschluss\b",
        "Die Stadt plant, die Seilbahn in den ÖPNV einzubinden und mit üblichen "
        "Nahverkehrstickets nutzbar zu machen. Die Quellen vergleichen das nicht "
        "mit einer Schienenanbindung und belegen daher nicht, dass diese besser "
        "in den regionalen Nahverkehr integrierbar wäre.",
        source_evidence=r"\b(?:deutschlandticket|öpnv|öffentlichen personennahverkehr)\b",
    ),
    _rule(
        r"\b(öpnv|nahverkehr|ticket|deutschlandticket)\b",
        "Laut Stadtseite soll die Seilbahn Teil des ÖPNV sein und mit üblichen Nahverkehrstickets nutzbar werden.",
        affirmation=True,
        contradiction=r"\b(kein(?:e|en|er)?|nicht|ohne|separat(?:es|e|en)?\s+ticket)\b",
        source_evidence=r"\b(?:deutschlandticket|öffentlichen personennahverkehr|öpnv)\b",
    ),
    _rule(
        r"\b(barrierefrei|barrierefreiheit|rollstuhl|mobilitätseinschränk)\b",
        "Die Anlagen sollen barrierefrei geplant werden, unter anderem für Rollstühle und Kinderwagen.",
        affirmation=True,
        contradiction=r"\b(nicht|kein(?:e|en|er)?)\s*(?:barrierefrei|zugänglich|nutzbar)\b",
        source_evidence=r"\bbarrierefrei\b",
    ),
    _rule(
        r"\b(bau|baubeginn|fertigstellung|inbetriebnahme|eröffnung|betriebsstart|in betrieb|gebaut)\b",
        "Ein konkreter Eröffnungstermin ist nicht genannt. Die Stadt hält einen "
        "Betriebsbeginn in den frühen Jahren des kommenden Jahrzehnts für realistisch. "
        "Die reine Bauzeit wird auf 1 bis 1,5 Jahre geschätzt; Planung und Genehmigung "
        "kommen hinzu.",
        r"(?P<number>\d+(?:[.,]\d+)?)\s*(?:jahre|jahr)\b",
        (1, 1.5),
        contradiction=r"\b(bereits\s+(?:gebaut|eröffnet|in betrieb)|ist\s+(?:gebaut|eröffnet|in betrieb)|fährt\s+(?:schon|bereits))\b",
        source_evidence=(
            r"(?=.*\b1\s+bis\s+1[,.]5\s+jahre\b)"
            r"(?=.*\brealistischer beginn des betriebes liegt .*?frühen jahren des "
            r"kommenden jahrzehnts\b)"
        ),
    ),
    _rule(
        r"\b(machbarkeitsstudie|machbarkeit|gemeinderat|vorplanung|detailplanung|projekt(?:status)?|genehmigung)\b",
        "Die Machbarkeitsstudie wurde im 2. Quartal 2026 abgeschlossen. Am 27. Juli 2026 beschloss der Gemeinderat die Vorplanung und externe Planungsbüros; als Nächstes folgt die Detailplanung.",
        contradiction=r"\b(unmöglich|nicht machbar|abgelehnt|gestoppt|abgesagt|eingestellt)\b",
        source_evidence=r"\b(?:machbarkeitsstudie|vorplanung|detailplanung)\b",
    ),
    _rule(
        r"\b(klima|umwelt|co2|feinstaub|stickoxide|emission|energieeffizient)\b",
        "Die Stadtseite beschreibt die Seilbahn als energieeffizient und lokal emissionsfrei. Das ist die Aussage der Projektseite, kein unabhängiger Umweltvergleich.",
        affirmation=True,
        contradiction=r"\b(stößt|verursacht|produziert)\b.{0,30}\b(co2|feinstaub|stickoxide|emissionen?)\b",
        source_evidence=r"\b(?:co2|feinstaub|stickoxide|energieeffizient)\b",
    ),
)


def _opinion_context(topic, source_evidence, response):
    return OpinionContextRule(
        re.compile(topic, re.IGNORECASE),
        tuple(re.compile(pattern, re.IGNORECASE) for pattern in source_evidence),
        response,
    )


OPINION_CONTEXT_RULES = (
    _opinion_context(
        r"\b(kosten|folgekosten|betriebskosten|wartung\w*|revision\w*|"
        r"fahrgastzahlen|preis|teu(?:er|r)\w*|lohnt sich|kosten.?nutzen|"
        r"wirtschaftlich(?:keit)?|"
        r"geldverschwendung|steuergeldverschwendung|prestige(?:projekt)?|"
        r"verschwendung|dringend\w*|priorität|finanzier|subvention|steuer|"
        r"wer bezahlt|förderung)\b",
        (
            r"\b157\s*millionen\b",
            r"\bbis zu 90\s*prozent\b",
            r"\bmuss .*? zunächst validiert werden\b",
            r"\bbetriebskosten berechnen sich im wesentlichen aus dem personalbedarf",
            r"\bfahrgasteinnahmen des hnv-gemeinschaftstarifes\b",
            r"\banteiligen einnahmen des deutschlandtickets\b",
            r"\bfestgelegten schlüssel, der die bereit gestellten wegekilometer "
            r"und die erlöse aus ticketeinnahmen berücksichtigt\b",
            r"\bpositiven volkswirtschaftlichen nutzen\b",
            r"\bnutzen-kosten-untersuchung der fachberater mit einem wert von 1,27\b",
            r"\bkosten-nutzen-indikator von über 1,0\b",
            r"\bplanungskosten mit einer vorfinanzierung von vier millionen euro\b",
            r"\b15\s*min(?:uten)?\b",
            r"\b1[.]?500 personen pro richtung und stunde\b|"
            r"\b1[.]?500 fahrgäste transportieren\b|"
            r"\bpro stunde und fahrtrichtung 1[.]?500 fahrgäste\b",
            r"\bklimaschonende alternative\b",
            r"\bdurch die nutzung des luftraums wird der bodenverkehr entlastet "
            r"und wertvoller urbaner raum geschont\b",
            r"\bdie seilbahn kann stark frequentierte verkehrswege entlasten und "
            r"pendlern eine zuverlässige alternative bieten\b",
            r"\bdie seilbahn soll wichtige zukunftsstandorte wie den bildungscampus, "
            r"den zukunftspark wohlgelegen und den ipai campus schnell, nachhaltig "
            r"und zuverlässig mit der innenstadt verbinden\b",
            r"\betwa alle 20-30 sekunden soll in jeder station eine gondel einfahren\b",
            r"\bgängige nahverkehrstickets wie das deutschlandticket sowie einzel- "
            r"und tageskarten sollen auch in der seilbahn gültig sein\b",
        ),
        (
            "Die Stadtseite nennt eine erste Investitionsschätzung von 157 Millionen "
            "Euro (Preisstand 2026). Sie muss in der Vor- und Detailplanung noch "
            "validiert werden; bis zu 90 Prozent öffentliche Förderung sind vorgesehen, "
            "aber keine bereits zugesagte Fördersumme. Zu den Folgekosten nennt die "
            "Stadt Personal, Energie, Pflege und Wartung; konkrete Euro-Beträge für "
            "jährliche Betriebs-, Wartungs- oder Revisionskosten stehen nicht auf der "
            "Seite. Dagegen stehen Einnahmen aus dem HNV-Gemeinschaftstarif und "
            "anteilige Deutschlandticket-Einnahmen, verteilt nach einem Schlüssel "
            "unter anderem anhand der Wegkilometer und Ticketerlöse. Als "
            "Nutzen nennt die Seite die Verbindung von Innenstadt, Bildungscampus, "
            "Zukunftspark Wohlgelegen und IPAI; die Fahrt bis zum IPAI soll 15 Minuten "
            "dauern. Die Seilbahn soll den Luftraum nutzen, dadurch Bodenverkehr und "
            "stark frequentierte Wege entlasten, wertvollen Stadtraum schonen und eine "
            "klimaschonende, zuverlässige Alternative bieten. Geplant sind Gondeln "
            "alle 20 bis 30 Sekunden, bis zu 1.500 Personen pro Stunde und Richtung "
            "sowie die Nutzung gängiger Nahverkehrstickets. Diese Kapazität ist keine "
            "Fahrgastprognose. Die Machbarkeitsstudie "
            "bescheinigt einen positiven volkswirtschaftlichen Nutzen. Das sind "
            "Planungsangaben und von der Stadt erwartete Vorteile, keine bereits "
            "gemessenen Wirkungen. Ob die Investition gegenüber anderen dringenden "
            "Aufgaben der Stadt Priorität haben sollte, kann die Projektseite nicht "
            "entscheiden; sie liefert auch keine detaillierte Geldwert-Rechnung, "
            "die abschließend beweist, ob sich die Investition finanziell lohnt. "
            "Weitere städtische Veröffentlichungen nennen eine erste Nutzen-Kosten-"
            "Untersuchung mit 1,27 und später einen Kosten-Nutzen-Indikator von über "
            "1,0 als Fördervoraussetzung. Das sind Angaben aus unterschiedlichen "
            "Planungsständen und Prüfungen, die nicht als dieselbe endgültige "
            "Kennzahl verglichen werden sollten. Eine Pressemitteilung nennt außerdem "
            "eine Vorfinanzierung von 4 Millionen Euro für die Hälfte der "
            "Planungskosten; das ist nicht die Bauinvestition."
        ),
    ),
    _opinion_context(
        r"\b(tram|straßenbahn|stadtbahn|parkhaus|parkhäuser|straße|straßen|"
        r"engpass|bodenverkehr|potenzial|ignoriert|ignorieren)\b",
        (
            r"\bzahlreiche verkehrssysteme geprüft\b",
            r"\bparameter wie kosten, verkehrliche erschließung, fahrtzeit, "
            r"taktung, personalbedarf und energieaufwand miteinander verglichen\b",
            r"\bseilbahn erwies sich für die geplante verbindung als besonders "
            r"flächensparend, effizient und verhältnismäßig zeitnah umsetzbar\b",
            r"\bdurch die nutzung des luftraums wird der bodenverkehr entlastet "
            r"und wertvoller urbaner raum geschont\b",
            r"\bdie seilbahn kann stark frequentierte verkehrswege entlasten und "
            r"pendlern eine zuverlässige alternative bieten\b",
            r"\bdie seilbahn soll wichtige zukunftsstandorte wie den bildungscampus, "
            r"den zukunftspark wohlgelegen und den ipai campus schnell, nachhaltig "
            r"und zuverlässig mit der innenstadt verbinden\b",
            r"\bfahrtzeit mit der seilbahn von der innenstadt über alle stationen "
            r"bis hin zum ipai campus soll künftig 15 min betragen\b",
            r"\betwa alle 20-30 sekunden soll in jeder station eine gondel einfahren\b",
            r"\b1[.]?500 personen pro richtung und stunde\b",
        ),
        (
            "Die Stadtseite belegt nicht, dass neue Straßen und Parkhäuser alle "
            "Engpässe beseitigen. Als mögliche Vorteile der Seilbahn nennt sie, "
            "dass der Luftraum genutzt, der Bodenverkehr entlastet und stark "
            "frequentierte Verkehrswege entlastet werden können. Geplant ist, "
            "Innenstadt, Bildungscampus, Zukunftspark Wohlgelegen und IPAI zu "
            "verbinden: Die Fahrt bis zum IPAI soll 15 Minuten dauern; Gondeln "
            "sollen etwa alle 20 bis 30 Sekunden fahren und bis zu 1.500 Personen "
            "pro Stunde und Richtung befördern. Das sind Planungsangaben und "
            "erwartete Vorteile, keine bereits gemessene Verkehrswirkung. Zur "
            "Straßenbahn sagt die Stadt, dass zahlreiche Verkehrssysteme nach "
            "Kosten, Erschließung, Fahrzeit, Taktung, Personalbedarf und Energie "
            "verglichen wurden. Sie begründet ihre Wahl mit Flächeneffizienz, "
            "Effizienz und vergleichsweise zeitnaher Umsetzbarkeit. Die Seite "
            "veröffentlicht jedoch keine detaillierte Straßenbahn-Alternativplanung; "
            "ob deren Potenzial ausreichend berücksichtigt wurde, lässt sich "
            "allein daraus nicht abschließend beurteilen."
        ),
    ),
    _opinion_context(
        r"(?=.*\b(?:seilbahn|schien\w*|bahnanschluss|tram\w*|straßenbahn\w*|"
        r"stadtbahn\w*|s-?bahn\w*|bus\w*)\b)"
        r"(?=.*\b(?:regional\w*|überregional\w*|gesamt\w*|nahverkehr|öpnv|"
        r"verkehrsverbund)\b)"
        r"(?=.*\b(?:integrier\w*|vernetz\w*|einbind\w*|anschließ\w*)\b)",
        (
            r"\bvollständige integration in den bestehenden öpnv-tarif\b",
            r"\bdeutschlandticket\b",
        ),
        (
            "Die Stadtquellen sagen, dass die Seilbahn in den bestehenden ÖPNV-Tarif "
            "integriert werden und zum Beispiel mit dem Deutschlandticket nutzbar "
            "sein soll. Sie vergleichen das nicht mit einer Schienenanbindung "
            "hinsichtlich der regionalen Vernetzung. Ob eine Schienenanbindung "
            "besser integrierbar wäre, bleibt durch diese Quellen unbelegt."
        ),
    ),
    _opinion_context(
        r"\b(bus|straßenbahn|stadtbahn|s-bahn|alternative|alternativ|"
        r"warum keine|warum nicht|verkehrsmittel|besser als)\b",
        (
            r"\bzahlreiche verkehrssysteme .*?geprüft\b",
            r"\bkosten, verkehrliche erschließung, fahrtzeit, taktung, personalbedarf "
            r"und energieaufwand\b",
            r"\bbesonders flächensparend, effizient und verhältnismäßig zeitnah umsetzbar\b",
            r"\bausbau des s-bahnnetzes .*? enorm kosten- und zeitintensiv\b",
            r"\bfahrtzeit des busses (?:liegt )?deutlich über der der seilbahn\b",
        ),
        (
            "Laut Stadt wurden mehrere Verkehrssysteme anhand von Kosten, Erschließung, "
            "Fahrzeit, Taktung, Personalbedarf und Energieaufwand verglichen. Die "
            "Stadt begründet ihre Wahl damit, dass die Seilbahn flächensparend, "
            "effizient und vergleichsweise zeitnah umsetzbar sei; sie bewertet den "
            "S-Bahn-Ausbau als kosten- und zeitintensiv und die Busfahrt als länger "
            "und stauanfällig. Das ist die Begründung der Stadtseite, kein unabhängiger "
            "Nachweis, dass die Seilbahn in jedem Vergleich besser ist."
        ),
    ),
    _opinion_context(
        r"\b(bedarf|auslast\w*|fahrgast\w*|passagier\w*|leer|überdimension|"
        r"niemand nutzt|keiner nutzt|zu groß|zu viele gondeln)\b",
        (
            r"\b1[.]?500 personen pro richtung und stunde\b|"
            r"\b1[.]?500 fahrgäste transportieren\b|"
            r"\bpro stunde und fahrtrichtung 1[.]?500 fahrgäste\b",
            r"\b(?:10|zehn) personen\b",
            r"\b20\s*(?:-|–|bis)\s*30\s*sekunden\b",
        ),
        (
            "Die Stadtseite nennt eine mögliche Kapazität von bis zu 1.500 Fahrgästen "
            "pro Stunde und Richtung, bis zu zehn Personen je Gondel und Gondeln etwa "
            "alle 20 bis 30 Sekunden. Das beschreibt die geplante Leistungsfähigkeit, "
            "nicht die tatsächliche Nachfrage oder Auslastung. Eine belastbare "
            "Fahrgastprognose lässt sich daraus allein nicht ableiten."
        ),
    ),
    _opinion_context(
        r"\b(klima|umwelt|co2|emission|feinstaub|stickoxide|versiegel|"
        r"fläche|flächenverbrauch|natur|bäume)\b",
        (
            r"\bklimaschonende alternative\b",
            r"\blokal weder co(?:2|₂) noch feinstaub oder stickoxide\b",
            r"\bgering(?:en|er) flächenverbrauch\b",
        ),
        (
            "Die Stadtseite beschreibt die Seilbahn als klimaschonend, lokal emissionsfrei "
            "(CO2, Feinstaub und Stickoxide) und flächensparend. Das sind Angaben der "
            "Projektseite; eine unabhängige Umweltbilanz einschließlich Bau und "
            "Energieversorgung wird damit nicht belegt."
        ),
    ),
    _opinion_context(
        r"\b(lärm|laut|geräusch|ruhestörung)\b",
        (
            r"\bmoderne urbane seilbahnen gelten als sehr leise\b",
            r"\bgeräuschentwicklung beschränkt sich hauptsächlich auf die stationen\b",
            r"\bliegt unter der von straßenverkehr oder schienenverkehr\b",
        ),
        (
            "Laut Stadtseite gelten moderne urbane Seilbahnen als leise; Geräusche "
            "entstünden vor allem an den Stationen und lägen unter denen von Straßen- "
            "oder Schienenverkehr. Die Seite nennt hier keine konkreten Messwerte für "
            "die geplante Heilbronner Strecke."
        ),
    ),
    _opinion_context(
        r"\b(privatsphäre|privat|garten|wohnung|fenster|spion|glotz|sichtschutz)\b",
        (
            r"\b(?:keine wohnbebauung überquert|überquert keine wohnbebauung)\b",
            r"\bhorizontale abstände zu wohnbebauung (?:werden )?ausreichend "
            r"(?:eingehalten|einhält)\b",
        ),
        (
            "Die Stadtseite sagt, die Trasse überquere keine Wohnbebauung und halte "
            "ausreichende horizontale Abstände zu Wohngebäuden ein. Das ist der "
            "Planungsstand der Stadt; konkrete Sichtachsen für einzelne Grundstücke "
            "werden auf der Seite nicht dargestellt."
        ),
    ),
    _opinion_context(
        r"\b(baustelle|bauzeit|baubeginn|fertigstellung|eröffnung|"
        r"betriebsbeginn|in betrieb|verzöger|wann)\b",
        (
            r"\b1\s+bis\s+1[,.]5\s+jahre\b",
            r"\bplanungs- und genehmigungsprozess (?:ist )?sehr aufwendig und komplex\b",
            r"\brealistischer beginn des betriebes liegt .*?frühen jahren des kommenden jahrzehnts\b",
        ),
        (
            "Die Stadt schätzt die reine Bauzeit auf 1 bis 1,5 Jahre. Planung und "
            "Genehmigung dauern zusätzlich und werden als aufwendig und komplex "
            "beschrieben. Als realistischen Betriebsbeginn nennt die Seite die frühen "
            "Jahre des kommenden Jahrzehnts; ein konkretes Eröffnungsdatum ist das nicht."
        ),
    ),
    _opinion_context(
        r"\b(stau|verkehr|berufsverkehr|verkehrsentlast|pendler|neckartalstraße|"
        r"böckingen|frankenbach|sontheim|bewohner|stadtteil|insellösung|"
        r"ki.?campus|künstliche intelligenz)\b",
        (
            r"\bziel des projekts ist es, die stadtteile mit einer effizienten und "
            r"umweltfreundlichen mobilitätslösung noch besser zu erschließen\b",
            r"\bverkehrsströmen auf der sogenannten \+1 ebene .*?entlastet die "
            r"seilbahn .*?stark überlasteten straßen in der innenstadt .*?"
            r"neckartalstraße\b",
            r"\bverwaltung prüft zusätzlich den bau großflächiger parkmöglichkeiten "
            r"in autobahnnähe, so dass studierende, pendler oder touristen am stadtrand "
            r"parken können und mit der seilbahn zuverlässig und staufrei in die "
            r"innenstadt gelangen\b",
            r"\bstation 1 neckarturm\b",
            r"\bstation 2 bildungscampus \(west\)",
            r"\bstation 3 zukunftspark wohlgelegen\b",
            r"\bstation 4 neckargartacher brücke\b",
            r"\bstation 5 neckarau\s*[–-]\s*alte molkerei\b",
            r"\bstation 6 ipai campus\b",
        ),
        (
            "Die Stadt begründet den Nutzen nicht nur mit Fahrten zum IPAI: Sie erwartet, "
            "dass Verkehrsströme auf der +1-Ebene stark belastete Straßen in der "
            "Innenstadt und die Neckartalstraße, besonders zu Berufszeiten, entlasten. "
            "Zusätzlich prüft die Verwaltung Parkmöglichkeiten in Autobahnnähe, damit "
            "Pendler, Studierende und Touristen am Stadtrand parken und staufrei mit "
            "der Seilbahn in die Innenstadt fahren können. Das zielt auf eine Entlastung "
            "des städtischen Verkehrs über die unmittelbare Seilbahnstrecke hinaus und "
            "könnte auch Bewohner anderer Stadtteile indirekt betreffen. Die geplanten "
            "Stationen liegen jedoch am Neckarturm/Kurt-Schumacher-Platz, Bildungscampus "
            "West, Zukunftspark Wohlgelegen, Neckargartacher Brücke, Neckarau-Alte "
            "Molkerei und IPAI; Böckingen, Frankenbach und Sontheim sind in dieser Liste "
            "keine eigenen Stationen. Die Stadtseite quantifiziert keine Entlastung für "
            "diese Stadtteile und beschreibt erwartete, nicht bereits gemessene Wirkungen."
        ),
    ),
    _opinion_context(
        r"\b(neue wege in der mobilität|neuer weg in die mobilität|"
        r"neuartig\w*|konzept.{0,80}(?:deutschland|zuvor|gab es)|"
        r"zuvor.{0,80}deutschland|gab es.{0,80}deutschland|"
        r"noch nie.{0,80}deutschland|einzigartig|erste kommune|"
        r"neu in deutschland|deutschland)\b",
        (
            r"\bals erste kommune in baden-württemberg eine urbane seilbahn in ihr "
            r"öffentliches nahverkehrsnetz zu integrieren\b",
            r"\burbane seilbahnen gibt es z\.b\. in london, ankara, lissabon oder toulouse\b",
            r"\bim dezember 2025 eröffnete in paris die ebenfalls in den nahverkehr "
            r"integrierte seilbahn\b",
            r"\bwäre die heilbronner seilbahn mit fertigstellung die längste urbane "
            r"seilbahn europas\b",
        ),
        (
            "Die Stadtseite beschreibt Heilbronn als erste Kommune in "
            "Baden-Württemberg, die eine urbane Seilbahn in den ÖPNV integrieren "
            "möchte. Das stützt die Aussage, dass es für Heilbronn und das Land ein "
            "neuer Mobilitätsweg wäre. Die Seite behauptet jedoch nicht, dass es ein "
            "solches Konzept zuvor in ganz Deutschland nicht gab. Sie nennt urbane "
            "Seilbahnen in London, Ankara, Lissabon und Toulouse sowie die in den "
            "Nahverkehr integrierte Seilbahn Cable C1 in Paris. Für Heilbronn wäre "
            "laut Planung die längste urbane Seilbahn Europas vorgesehen. Die "
            "deutschlandweite Einzigartigkeit lässt sich aus dieser Stadtseite allein "
            "nicht bestätigen."
        ),
    ),
    _opinion_context(
        r"\b(sicherheit|sicher|wind|sturm|unwetter|notfall|rett|evaku|unfall)\b",
        (
            r"\bnotfall- und rettungskonzepte\b",
            r"\bnotfall-knöpfen\b",
            r"\bbei extremen wetterlagen .*?betriebsunterbrechungen notwendig\b",
        ),
        (
            "Die Stadtseite nennt Notfall- und Rettungskonzepte sowie Notfallknöpfe "
            "und beschreibt, dass extreme Wetterlagen zu Einschränkungen oder "
            "Betriebsunterbrechungen führen können. Das sind Planungsangaben; konkrete "
            "örtliche Sicherheitsgutachten sind auf der Seite nicht enthalten."
        ),
    ),
    _opinion_context(
        r"\b(barrierefrei|barrierefreiheit|rollstuhl|kinderwagen|fahrrad|"
        r"ticket|deutschlandticket|öpnv)\b",
        (
            r"\bmenschen mit mobilitätseinschränkungen\b",
            r"\bdeutschlandticket\b",
            r"\beinzel- und tageskarten\b",
        ),
        (
            "Laut Stadtseite sollen die Anlagen so geplant werden, dass Menschen mit "
            "Mobilitätseinschränkungen, Kinderwagen, Rollstühle und Fahrräder sie "
            "komfortabel nutzen können. Außerdem sollen Deutschlandticket sowie "
            "Einzel- und Tageskarten gelten."
        ),
    ),
)


def _number_value(raw):
    normalized = raw.casefold()
    if normalized == "sechs":
        return 6
    if normalized == "zehn":
        return 10
    if "," in normalized and "." in normalized:
        if normalized.rfind(",") > normalized.rfind("."):
            normalized = normalized.replace(".", "").replace(",", ".")
        else:
            normalized = normalized.replace(",", "")
    elif "," in normalized:
        normalized = normalized.replace(",", ".")
    elif "." in normalized and len(normalized.rsplit(".", 1)[1]) == 3:
        normalized = normalized.replace(".", "")
    try:
        return float(normalized)
    except ValueError:
        return None


def _statement_parts(text):
    claims = []
    for line in text.splitlines():
        for claim in re.split(
            r"(?<=[!?])\s+|(?<=\.)\s+(?!\d)", line.strip()
        ):
            claim = claim.strip(" \t-•")
            if claim:
                claims.append(claim)
    return claims


def _check_benefit_cost_claim(statement, context_source_text):
    if not re.search(r"\b(nutzen.?kosten|kosten.?nutzen)\b", statement, re.IGNORECASE):
        return None

    numbers = [
        _number_value(match.group("number"))
        for match in re.finditer(
            r"(?P<number>\d+(?:[.,]\d+)?)",
            statement,
        )
    ]
    numbers = [number for number in numbers if number is not None]
    if not numbers:
        return None

    first_analysis_available = re.search(
        r"\bnutzen-kosten-untersuchung der fachberater mit einem wert von 1,27\b",
        context_source_text,
        re.IGNORECASE,
    )
    indicator_available = re.search(
        r"\bkosten-nutzen-indikator von über 1,0\b",
        context_source_text,
        re.IGNORECASE,
    )
    mentions_first_analysis = re.search(
        r"\b(erste|früh\w*)\b",
        statement,
        re.IGNORECASE,
    )
    denies_first_value = re.search(
        r"\b(nicht|kein(?:e|en|er)?)\b.{0,30}\b1[,.]27\b",
        statement,
        re.IGNORECASE,
    )

    if first_analysis_available and any(number == 1.27 for number in numbers):
        if denies_first_value:
            return {
                "status": "contradicted",
                "statement": statement,
                "explanation": (
                    "Die städtische Veröffentlichung nennt 1,27 für die erste "
                    "Nutzen-Kosten-Untersuchung."
                ),
                "correction": (
                    "Der Wert 1,27 gehört zu der in der Veröffentlichung genannten "
                    "ersten Untersuchung und ist nicht automatisch der aktuelle "
                    "oder endgültige Planungsstand."
                ),
            }
        return {
            "status": "supported",
            "statement": statement,
            "explanation": (
                "Die städtische Veröffentlichung nennt 1,27 für eine erste "
                "Nutzen-Kosten-Untersuchung."
            ),
            "correction": (
                "Der Wert 1,27 gehört zu der in der Veröffentlichung genannten "
                "ersten Untersuchung und ist nicht automatisch der aktuelle "
                "oder endgültige Planungsstand."
            ),
        }
    if (
        first_analysis_available
        and mentions_first_analysis
        and any(number != 1.27 for number in numbers)
    ):
        return {
            "status": "contradicted",
            "statement": statement,
            "explanation": (
                "Der Wert weicht von der städtischen Angabe zur ersten "
                "Nutzen-Kosten-Untersuchung ab."
            ),
            "correction": (
                "Die Stadt nennt für diese erste Untersuchung einen Wert von 1,27."
            ),
        }

    if indicator_available and any(number == 1.0 for number in numbers):
        says_greater_than_one = re.search(
            r"(?:\büber\b|\bmehr als\b|\bgrößer als\b|>)",
            statement,
            re.IGNORECASE,
        )
        denies_greater_than_one = re.search(
            r"\b(nicht|kein(?:e|en|er)?)\b.{0,30}"
            r"(?:\büber\b|\bmehr als\b|\bgrößer als\b|>)",
            statement,
            re.IGNORECASE,
        )
        if says_greater_than_one and not denies_greater_than_one:
            return {
                "status": "supported",
                "statement": statement,
                "explanation": (
                    "Die städtische Pressemitteilung nennt einen Kosten-Nutzen-"
                    "Indikator von über 1,0."
                ),
                "correction": (
                    "Die Angabe bezieht sich auf den in der Pressemitteilung "
                    "beschriebenen Prüfstand und ist keine Förderzusage."
                ),
            }
        return {
            "status": "contradicted",
            "statement": statement,
            "explanation": (
                "Die Stadt nennt einen Indikator von über 1,0, nicht genau 1,0."
            ),
            "correction": (
                "Laut städtischer Pressemitteilung liegt der Kosten-Nutzen-Indikator "
                "über 1,0."
            ),
        }

    return {
        "status": "unclear",
        "statement": statement,
        "explanation": (
        "Die Stadt veröffentlicht Werte aus unterschiedlichen Prüfständen; "
        "die konkrete Kennzahl lässt sich damit nicht eindeutig bestätigen."
        ),
        "correction": (
        "Eine städtische Veröffentlichung nennt 1,27 für eine erste "
        "Nutzen-Kosten-Untersuchung; eine spätere Pressemitteilung nennt "
        "einen Kosten-Nutzen-Indikator von über 1,0. Die Werte sind nicht "
        "als identische oder endgültige Kennzahl zu behandeln."
        ),
    }


def _check_regional_integration_comparison(statement, context_source_text):
    transit_mode = re.search(
        r"\b(?:seilbahn|schien\w*|bahnanschluss|tram\w*|straßenbahn\w*|"
        r"stadtbahn\w*|s-?bahn\w*|bus\w*)\b",
        statement,
        re.IGNORECASE,
    )
    regional_scope = re.search(
        r"\b(?:regional\w*|überregional\w*|gesamt\w*|nahverkehr|öpnv|"
        r"verkehrsverbund)\b",
        statement,
        re.IGNORECASE,
    )
    comparative = re.search(
        r"\b(?:besser|leichter|einfacher|direkter|effizienter|sinnvoller)\b",
        statement,
        re.IGNORECASE,
    )
    integration = re.search(
        r"\b(?:integrier\w*|vernetz\w*|einbind\w*|anschließ\w*)\b",
        statement,
        re.IGNORECASE,
    )
    if not all((transit_mode, regional_scope, comparative, integration)):
        return None

    tariff_integration_is_documented = (
        re.search(
            r"\bvollständige integration in den bestehenden öpnv-tarif\b",
            context_source_text,
            re.IGNORECASE,
        )
        and re.search(r"\bdeutschlandticket\b", context_source_text, re.IGNORECASE)
    )
    if tariff_integration_is_documented:
        explanation = (
            "Die Stadtquellen belegen die geplante Tarifintegration der Seilbahn, "
            "aber keinen Vergleich zur regionalen Vernetzung."
        )
        correction = (
            "Die Seilbahn soll in den bestehenden ÖPNV-Tarif integriert werden "
            "und mit dem Deutschlandticket nutzbar sein. Daraus folgt nicht, dass "
            "sie oder eine Schienen-, Tram- oder Busanbindung regional besser "
            "integrierbar wäre; dazu geben die Quellen keinen Vergleich."
        )
    else:
        explanation = (
            "Die geladenen Stadtquellen enthalten keinen belegten Vergleich zur "
            "regionalen Integration dieser Verkehrsmittel."
        )
        correction = (
            "Bitte die Aussage als ungeprüft behandeln. Ein Vergleich der "
            "regionalen Vernetzung ist in den geladenen Quellen nicht belegt."
        )
    return {
        "status": "unclear",
        "statement": statement,
        "explanation": explanation,
        "correction": correction,
    }


def _check_statement_fact(statement, source_text, context_source_text=None):
    """Compare claims with the current project page and relevant city releases."""
    if context_source_text is not None:
        integration_result = _check_regional_integration_comparison(
            statement, context_source_text
        )
        if integration_result is not None:
            return integration_result

        benefit_cost_result = _check_benefit_cost_claim(
            statement, context_source_text
        )
        if benefit_cost_result is not None:
            return benefit_cost_result

    for rule in RULES:
        if not rule.topic.search(statement):
            continue

        if not rule.source_evidence.search(source_text):
            return {
                "status": "unclear",
                "statement": statement,
                "explanation": (
                    "Die aktuelle Stadtseite bestätigt den gespeicherten Quellenbeleg "
                    "nicht mehr. Deshalb gibt es kein automatisches Urteil."
                ),
                "correction": (
                    "Bitte die aktuelle Angabe direkt auf der offiziellen Projektseite "
                    "prüfen."
                ),
            }

        if rule.quantity:
            numbers = []
            for match in rule.quantity.finditer(statement):
                for raw in match.groupdict().values():
                    if not raw:
                        continue
                    value = _number_value(raw)
                    if value is not None:
                        numbers.append(value)
            if numbers:
                supported = all(value in rule.accepted_values for value in numbers)
                if (
                    supported
                    and rule.value_judgment
                    and rule.value_judgment.search(statement)
                ):
                    return {
                        "status": "unclear",
                        "statement": statement,
                        "explanation": (
                            "Die genannte Zahl stimmt mit der Stadtseite überein; "
                            "die Bewertung als Verschwendung oder die Priorität "
                            "gegenüber anderen Aufgaben lässt sich daraus nicht "
                            "als Fakt bestätigen."
                        ),
                        "correction": rule.expected,
                    }
                return {
                    "status": "supported" if supported else "contradicted",
                    "statement": statement,
                    "explanation": (
                        "Die Zahl stimmt mit der veröffentlichten Planung überein."
                        if supported
                        else "Die genannte Zahl weicht von der veröffentlichten Planung ab."
                    ),
                    "correction": rule.expected,
                }

        if rule.contradiction and rule.contradiction.search(statement):
            return {
                "status": "contradicted",
                "statement": statement,
                "explanation": "Diese Aussage widerspricht dem Stand der Stadtseite.",
                "correction": rule.expected,
            }
        if rule.affirmation:
            return {
                "status": "supported",
                "statement": statement,
                "explanation": "Das entspricht der Aussage der offiziellen Projektseite.",
                "correction": rule.expected,
            }

        return {
            "status": "unclear",
            "statement": statement,
            "explanation": "Die geprüften Stadtquellen belegen diese konkrete Aussage nicht.",
            "correction": rule.expected,
        }

    return {
        "status": "unclear",
        "statement": statement,
        "explanation": "Dazu finde ich auf der verlinkten Projektseite keine belastbare Angabe.",
        "correction": (
            "Bitte als ungeprüft behandeln. Der Faktencheck nutzt die aktuelle "
            "Projektseite und ergänzende offizielle Veröffentlichungen der Stadt."
        ),
    }


def check_statement(statement, source_text, context_source_text=None):
    source_pages = [("Ausgewählte Quelle", source_text)]
    if context_source_text and context_source_text != source_text:
        source_pages.append(("Weitere ausgewählte Fundstellen", context_source_text))
    return check_statement_against_sources(statement, source_pages)


def check_text(text, source_text, context_source_text=None):
    source_pages = [("Ausgewählte Quelle", source_text)]
    if context_source_text and context_source_text != source_text:
        source_pages.append(("Weitere ausgewählte Fundstellen", context_source_text))
    return check_text_against_sources(text, source_pages)


GENERIC_STOP_WORDS = {
    "aber", "als", "am", "an", "auch", "auf", "aus", "bei", "bin", "bis",
    "das", "dass", "dem", "den", "der", "des", "die", "dies", "diese",
    "ein", "eine", "einer", "eines", "er", "es", "für", "hat", "haben",
    "im", "in", "ist", "kann", "mit", "oder", "sich", "sie", "sind", "und",
    "von", "war", "wie", "wird", "zu", "zum", "zur", "the", "and", "for",
    "from", "has", "is", "it", "of", "on", "or", "that", "this", "to", "was",
    "with",
}

SOURCE_CONCEPT_TERMS = {
    "finance": (
        "kost", "preis", "teuer", "invest", "finanz", "förder", "steuer",
        "geld", "budget", "ausgab", "aufwend", "euro",
        "eigenanteil", "eigenbeitrag", "eigenmittel",
    ),
    "economic_value": (
        "wirtschaft", "nutzen", "vorteil", "rentabel", "verschwend",
        "prestige", "nutzen kosten", "kosten nutzen", "fahrgast", "fahrgäst",
        "auslast", "prognos", "nachfrage",
    ),
    "operations": (
        "betrieb", "wart", "revision", "instand", "personal", "energie",
        "unterhalt", "aufwand", "wartung",
    ),
    "ridership": (
        "fahrgast", "fahrgäst", "passagier", "kapazität", "auslast", "prognos",
        "nachfrage", "takt", "kabine", "gondel",
    ),
    "transit_network": (
        "tram", "straßenbahn", "schienen", "öpnv", "nahverkehr",
        "regional", "integration", "anschluss", "umstieg", "netz", "bus",
        "stadtbahn", "s bahn", "ticket", "tarif", "integrier",
    ),
    "traffic": (
        "verkehr", "stau", "straß", "auto", "entlast", "bodenverkehr",
        "parken", "parkhaus", "engpass", "pendler",
    ),
    "access": (
        "stadtteil", "bezirk", "bewohner", "bürger", "insellösung",
        "campus", "standort", "erreich", "route", "station", "verbind",
        "anschluss",
    ),
    "environment": (
        "umwelt", "klima", "co2", "emission", "baum", "natur", "fläche",
        "lärm",
    ),
    "planning": (
        "plan", "genehmig", "bau", "zeitplan", "fertigstellung",
        "inbetriebnahme", "start", "verzöger",
    ),
}

MEASURE_SUBJECT_TERMS = {
    "investment": (
        "investition", "investitionssumme", "gesamtinvestition", "investment",
    ),
    "annual_cost": (
        "jährliche aufwendungen", "jährlichen aufwendungen",
        "jährliche kosten", "jährlichen kosten", "annual operating cost",
    ),
    "operating_cost": (
        "betriebskosten", "betriebs und wartungskosten", "wartungskosten",
        "revisionskosten", "instandhaltungskosten",
    ),
    "capital_service": ("kapitaldienst",),
    "equity_share": ("eigenanteil", "eigenbeitrag", "eigenmittel"),
    "ridership": (
        "fahrgast", "fahrgäste", "passagier", "passagiere",
        "person", "personen", "mensch", "menschen", "leute",
        "gondel", "kabine", "kapazität",
    ),
    "route_length": ("strecke", "trasse", "routenlänge", "route length"),
    "station_count": ("station", "haltestelle", "haltepunkt"),
    "support_count": ("stütze", "stützen", "pfeiler"),
    "frequency": ("takt", "sekundentakt", "sekunden", "intervall"),
    "benefit_cost": ("nutzen kosten", "kosten nutzen", "indikator", "index"),
    "travel_time": (
        "fahrzeit", "fahrtzeit", "reisezeit", "fahrtdauer", "fahrt",
        "dauert", "braucht",
    ),
    "construction_duration": ("bauzeit", "bauphase", "baudauer"),
}
NUMBER_TOKEN_PATTERN = (
    r"(?:\d{1,3}(?:\.\d{3})+(?:,\d+)?|"
    r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:[.,]\d+)?)|"
    r"ein(?:s|e|en|em|er|es)?|zwei|drei|vier|fünf|sechs|sieben|acht|"
    r"neun|zehn|elf|zwölf|dreizehn|vierzehn|fünfzehn|sechzehn|"
    r"siebzehn|achtzehn|neunzehn|zwanzig|dreißig|vierzig|fünfzig|"
    r"sechzig|siebzig|achtzig|neunzig|hundert|tausend"
)
MEASUREMENT_UNIT_PATTERN = (
    r"euro|€|kilometer\w*|km|fahrgäste|fahrgast|"
    r"passagier\w*|personen?|menschen?|manschen?|leute|"
    r"station\w*|haltepunkt\w*|"
    r"stützen?|pfeiler\w*|sekunden?|sek\.?|minuten?|min|"
    r"stunden?|std\.?|h|"
    r"tage?|jahre?|prozent|%"
)
MEASUREMENT_RANGE_PATTERN = re.compile(
    r"(?<![\w])(?P<number>{})\s*(?P<scale>million(?:en)?|mio\.?)?\s*"
    r"(?:-|–|—|\bbis\b)\s*(?P<second>{})\s*"
    r"(?P<second_scale>million(?:en)?|mio\.?)?\s*"
    r"(?P<unit>{})(?!\w)".format(
        NUMBER_TOKEN_PATTERN, NUMBER_TOKEN_PATTERN, MEASUREMENT_UNIT_PATTERN
    ),
    re.IGNORECASE,
)
MEASUREMENT_PATTERN = re.compile(
    r"(?<![\w])(?P<number>{})\s*"
    r"(?P<scale>million(?:en)?|mio\.?)?\s*"
    r"(?P<unit>{})?(?!\w)".format(NUMBER_TOKEN_PATTERN, MEASUREMENT_UNIT_PATTERN),
    re.IGNORECASE,
)
NUMBER_WORD_VALUES = {
    "ein": 1, "eins": 1, "eine": 1, "einen": 1, "einem": 1,
    "einer": 1, "eines": 1, "zwei": 2, "drei": 3, "vier": 4,
    "fünf": 5, "sechs": 6, "sieben": 7, "acht": 8, "neun": 9,
    "zehn": 10, "elf": 11, "zwölf": 12, "dreizehn": 13,
    "vierzehn": 14, "fünfzehn": 15, "sechzehn": 16,
    "siebzehn": 17, "achtzehn": 18, "neunzehn": 19,
    "zwanzig": 20, "dreißig": 30, "vierzig": 40, "fünfzig": 50,
    "sechzig": 60, "siebzig": 70, "achtzig": 80, "neunzig": 90,
    "hundert": 100, "tausend": 1000,
}
VALUE_JUDGMENT_TERMS = (
    "prestige", "sinnvoll", "sinnlos", "unsinnig", "besser", "schlecht",
    "zweifel", "vermiss", "überflüss", "notwendig", "teuer", "erschütter",
    "fragwürdig", "unwirtschaftlich", "waste",
)
VALUE_JUDGMENT_WORDS = {
    "gut", "gute", "guter", "gutes", "guten", "wirtschaftlich",
    "wirtschaftliche", "wirtschaftlicher", "wirtschaftliches",
}
NEGATION_WORDS = {"kein", "keine", "keinen", "keinem", "keiner", "nicht", "nie", "ohne", "no", "not"}
QUALIFICATION_WORDS = {
    "soll", "sollen", "geplant", "plant", "prognose", "prognostiziert",
    "erwartet", "voraussichtlich", "würde", "würden", "könnte", "können",
    "will", "wollen", "wird", "werden", "vorgesehen",
}
SEMANTIC_CONCEPT_EXPANSIONS = {
    "finance": "Investitionssumme Kosten Preis Finanzierung Fördermittel Eigenanteil Euro",
    "economic_value": "Wirtschaftlichkeit Nutzen Kosten Fahrgastprognose Nachfrage",
    "operations": "Betrieb Wartung Revision Instandhaltung jährliche Aufwendungen",
    "ridership": "Fahrgäste Personen pro Tag Kapazität Prognose Nachfrage",
    "transit_network": "öffentlicher Nahverkehr ÖPNV Umstieg Stadtbahn S-Bahn Tarif",
    "traffic": "Straßenverkehr Stau Entlastung Autos Pendler Verkehrsbelastung",
    "access": "Erreichbarkeit Verbindung Standort Stadtteile Haltestelle",
    "environment": "Umwelt Klima Emissionen Flächenverbrauch Lärm",
    "planning": "Zeitplan Termin Vorplanung Genehmigung Baubeginn Inbetriebnahme",
}
NON_LOCATION_CAPITALIZED_WORDS = {
    "betrieb", "jahr", "kosten", "investition", "projekt", "seilbahn",
    "straßenbahn", "verkehr", "nahverkehr", "öpnv", "station", "strecke",
    "trasse", "planung", "inbetriebnahme", "fahrgast", "fahrgäste",
}
FINANCIAL_CONCEPTS = {
    "finance", "investment", "annual_cost", "operating_cost",
    "capital_service", "equity_share",
}


def _normalized_source_phrase(text):
    return " ".join(re.findall(r"[^\W_]+", text.casefold()))


def _source_terms(text):
    terms = set()
    suffixes = (
        "ungen", "keiten", "heiten", "ung", "heit", "keit", "lich", "isch",
        "ern", "en", "et", "te", "er", "es", "em", "e", "s", "n", "t",
    )
    for word in re.findall(r"[^\W_]+", text.casefold()):
        if (
            len(word) < 4
            or word in GENERIC_STOP_WORDS
            or word in NEGATION_WORDS
        ):
            continue
        for suffix in suffixes:
            if len(word) - len(suffix) >= 3 and word.endswith(suffix):
                word = word[: -len(suffix)]
                break
        terms.add(word)
    return terms


def _source_concepts(text):
    normalized_text = _normalized_source_phrase(text)
    concepts = {
        concept
        for concept, terms in SOURCE_CONCEPT_TERMS.items()
        if any(term in normalized_text for term in terms)
    }
    if re.search(r"\b(?:zahl\w*|aufbring\w*|belast\w*|finanzier\w*)\b", normalized_text):
        concepts.add("finance")
    for concept, terms in MEASURE_SUBJECT_TERMS.items():
        if any(term in normalized_text for term in terms):
            concepts.add(concept)
    if (
        re.search(r"\b(?:jedes jahr|pro jahr|jährlich\w*)\b", normalized_text)
        and re.search(
            r"\b(?:aufwendung\w*|kosten|euro|geld|zahl\w*|"
            r"betrieb\w*|wart\w*|budget|ausgabe\w*|finanzier\w*)\b",
            normalized_text,
        )
    ):
        concepts.add("annual_cost")
    return concepts


def _numeric_qualifier(text, number_start):
    prefix = _normalized_source_phrase(text[max(0, number_start - 40):number_start])
    qualifiers = (
        ("at_most", r"\b(?:bis zu|höchstens|maximal|nur platz für)\s*$"),
        ("below", r"\b(?:unter|weniger als|kleiner als)\s*$"),
        ("at_least", r"\b(?:mindestens|wenigstens|ab)\s*$"),
        ("above", r"\b(?:über|mehr als|größer als)\s*$"),
        ("approximate", r"\b(?:rund|etwa|ca|ungefähr|circa|knapp|fast)\s*$"),
    )
    for qualifier, pattern in qualifiers:
        match = re.search(pattern, prefix)
        if match:
            return qualifier, match.group(0).strip()
    return None, None


def _numeric_bound_conflict(qualifier, value_measure, bound_measure):
    if qualifier in {"at_most", "below"}:
        limit = bound_measure["maximum"]
        return (
            value_measure["maximum"] >= limit
            if qualifier == "below"
            else value_measure["maximum"] > limit
        )
    if qualifier in {"at_least", "above"}:
        limit = bound_measure["minimum"]
        return (
            value_measure["minimum"] <= limit
            if qualifier == "above"
            else value_measure["minimum"] < limit
        )
    return False


DURATION_FACTORS = {
    "seconds": 1,
    "minutes": 60,
    "hours": 3600,
    "days": 86400,
}


def _transport_mode(text):
    normalized_text = _normalized_source_phrase(text)
    if re.search(r"\b(?:seilbahn|gondel\w*|kabine\w*)\b", normalized_text):
        return "cable_car"
    if re.search(
        r"\b(?:zug\w*|bahn\w*|s bahn|stadtbahn\w*|straßenbahn\w*|tram\w*)\b",
        normalized_text,
    ):
        return "rail"
    if re.search(r"\bbus\w*\b", normalized_text):
        return "bus"
    return None


def _measurements(text):
    measurements = []

    def add_measurement(match, is_range=False):
        scale = (match.group("scale") or "").casefold()
        second_scale = (
            (match.group("second_scale") or "").casefold()
            if is_range
            else ""
        )
        unit = (match.group("unit") or "").casefold()
        window = text[max(0, match.start() - 90):match.end() + 60]
        normalized_window = _normalized_source_phrase(window)
        subjects = {
            subject
            for subject, terms in MEASURE_SUBJECT_TERMS.items()
            if any(term in normalized_window for term in terms)
        }
        if (
            unit in {"fahrgast", "fahrgäste", "leute"}
            or unit.startswith(
                ("passagier", "person", "mensch", "mansch")
            )
        ):
            subjects.add("ridership")
        if not subjects:
            return

        if unit in {"euro", "€"}:
            unit = "currency"
        elif unit in {"km"} or unit.startswith("kilometer"):
            unit = "distance"
        elif (
            unit in {"fahrgast", "fahrgäste", "leute"}
            or unit.startswith("passagier")
            or unit.startswith("person")
            or unit.startswith("mensch")
            or unit.startswith("mansch")
        ):
            unit = "passengers"
        elif unit.startswith("station") or unit.startswith("haltepunkt"):
            unit = "stations"
        elif unit.startswith("stütz") or unit.startswith("pfeiler"):
            unit = "supports"
        elif unit.startswith("sekund") or unit in {"sek", "sek."}:
            unit = "seconds"
        elif unit in {"prozent", "%"}:
            unit = "percent"
        elif unit.startswith("minute") or unit in {"min", "min."}:
            unit = "minutes"
        elif unit.startswith("stunde") or unit in {"std", "std.", "h"}:
            unit = "hours"
        elif unit.startswith("tag"):
            unit = "days"
        elif unit.startswith("jahr"):
            unit = "years"
        elif scale:
            currency_subjects = {
                "investment", "annual_cost", "operating_cost",
                "capital_service", "equity_share",
            }
            unit = (
                "currency"
                if subjects & currency_subjects
                else "scaled"
            )
        elif "benefit_cost" in subjects:
            unit = "index"
        else:
            return
        if unit in {"minutes", "hours"} and "travel_time" not in subjects:
            return
        if unit == "years" and "construction_duration" not in subjects:
            return

        raw_number = match.group("number").casefold()
        value = NUMBER_WORD_VALUES.get(raw_number)
        if value is None:
            value = _number_value(raw_number)
        if value is None:
            return
        if scale:
            value *= 1_000_000

        minimum = value
        maximum = value
        if is_range:
            raw_second = match.group("second").casefold()
            second_value = NUMBER_WORD_VALUES.get(raw_second)
            if second_value is None:
                second_value = _number_value(raw_second)
            if second_value is None:
                return
            if second_scale or scale:
                second_value *= 1_000_000
            minimum, maximum = sorted((value, second_value))

        context = text[max(0, match.start() - 60):match.end() + 60]
        normalized_context = _normalized_source_phrase(context)
        normalized_context = re.sub(
            r"\b(?:stuinde|stundne)\b",
            "stunde",
            normalized_context,
        )
        qualifier, qualifier_display = _numeric_qualifier(text, match.start())
        time_basis = None
        for basis, pattern in (
            (
                "hour",
                r"\b(?:pro|je)\s+stunde\b|"
                r"\b(?:pro|je)\s+(?:fahrtrichtung|richtung)\s+und\s+stunde\b|"
                r"\bstündlich\b",
            ),
            ("day", r"\b(?:pro|je)\s+tag\b|\btäglich\b|\bam tag\b"),
            ("month", r"\b(?:pro|je)\s+monat\b|\bmonatlich\b"),
            ("year", r"\b(?:pro|je)\s+jahr\b|\bjährlich\w*\b"),
        ):
            if re.search(pattern, normalized_context):
                time_basis = basis
                break
        direction_scope = None
        if re.search(
            r"\b(?:(?:pro|je|und)\s+(?:fahrtrichtung|richtung)|"
            r"in\s+(?:eine[nr]?\s+)?(?:fahrtrichtung|richtung))\b",
            normalized_context,
        ):
            direction_scope = "per_direction"
        elif re.search(r"\b(?:beide richtungen|insgesamt|gesamt)\b", normalized_context):
            direction_scope = "all_directions"

        display = match.group(0).strip()
        if qualifier_display:
            display = "{} {}".format(qualifier_display, display)
        suffix = text[match.end():match.end() + 45]
        basis_suffix = re.match(
            r"\s+(?:(?:pro|je)\s+(?:stunde|tag|monat|jahr)"
            r"(?:\s+und\s+(?:fahrtrichtung|richtung))?"
            r"|(?:pro|je|und)\s+(?:fahrtrichtung|richtung)"
            r"(?:\s+und\s+(?:pro\s+)?stunde)?"
            r"|in\s+(?:eine[nr]?\s+)?(?:fahrtrichtung|richtung))\b",
            suffix,
            re.IGNORECASE,
        )
        if basis_suffix:
            display += basis_suffix.group(0)

        measurements.append(
            {
                "subjects": subjects,
                "unit": unit,
                "value": minimum if minimum == maximum else None,
                "minimum": minimum,
                "maximum": maximum,
                "time_basis": time_basis,
                "direction_scope": direction_scope,
                "transport_mode": _transport_mode(context),
                "qualifier": qualifier,
                "is_range": is_range,
                "display": display,
            }
        )

    ranges = tuple(MEASUREMENT_RANGE_PATTERN.finditer(text))
    for match in ranges:
        add_measurement(match, is_range=True)

    for match in MEASUREMENT_PATTERN.finditer(text):
        if any(
            range_match.start() <= match.start()
            and match.end() <= range_match.end()
            for range_match in ranges
        ):
            continue
        add_measurement(match)
    return measurements


def _statement_has_value_judgment(statement):
    words = _normalized_source_phrase(statement).split()
    return any(
        word in VALUE_JUDGMENT_WORDS
        or "verschwend" in word
        or any(word.startswith(term) for term in VALUE_JUDGMENT_TERMS)
        for word in words
    )


def _compare_measured_claim(statement, selected):
    statement_measurements = _measurements(statement)
    if len(statement_measurements) != 1:
        return None

    claim_measure = statement_measurements[0]
    context_mismatch = None
    range_context_result = None
    qualifier_context_result = None
    contradiction_result = None
    for source_url, sentence in selected:
        for source_measure in _measurements(sentence):
            shared_subjects = claim_measure["subjects"] & source_measure["subjects"]
            claim_unit = claim_measure["unit"]
            source_unit = source_measure["unit"]
            same_unit = claim_unit == source_unit
            convertible_duration = (
                claim_unit in DURATION_FACTORS
                and source_unit in DURATION_FACTORS
            )
            if not shared_subjects or not (same_unit or convertible_duration):
                continue
            if any(
                claim_measure[key] != source_measure[key]
                for key in ("time_basis", "direction_scope")
                if claim_measure[key] is not None or source_measure[key] is not None
            ):
                if context_mismatch is None:
                    context_mismatch = (source_url, sentence, source_measure)
                continue
            if (
                "travel_time" in shared_subjects
                and claim_measure["transport_mode"] is not None
                and source_measure["transport_mode"] is not None
                and claim_measure["transport_mode"] != source_measure["transport_mode"]
            ):
                if context_mismatch is None:
                    context_mismatch = (source_url, sentence, source_measure)
                continue

            claim_factor = DURATION_FACTORS.get(claim_unit, 1)
            source_factor = DURATION_FACTORS.get(source_unit, 1)
            claim_values = {
                **claim_measure,
                "minimum": claim_measure["minimum"] * claim_factor,
                "maximum": claim_measure["maximum"] * claim_factor,
            }
            source_values = {
                **source_measure,
                "minimum": source_measure["minimum"] * source_factor,
                "maximum": source_measure["maximum"] * source_factor,
            }
            claim_qualifier = claim_measure["qualifier"]
            source_qualifier = source_measure["qualifier"]
            if claim_qualifier != source_qualifier:
                bound_conflict = (
                    _numeric_bound_conflict(
                        source_qualifier, claim_values, source_values
                    )
                    or _numeric_bound_conflict(
                        claim_qualifier, source_values, claim_values
                    )
                )
                qualifier_result = {
                    "status": "contradicted" if bound_conflict else "contextualized",
                    "explanation": (
                        "Die genannte Zahl widerspricht der Grenze in der Quelle."
                        if bound_conflict
                        else "Die Quelle schränkt die Zahl mit „{}“ ein; "
                        "daraus folgt kein exakter Einzelwert.".format(
                            source_measure["display"]
                        )
                    ),
                    "correction": "Quellenangabe im Kontext: {}.".format(
                        source_measure["display"]
                    ),
                    "source_url": source_url,
                    "source_sentence": sentence,
                    "conflicting_source": (
                        {"sentence": sentence, "url": source_url}
                        if bound_conflict
                        else None
                    ),
                }
                if bound_conflict:
                    if contradiction_result is None:
                        contradiction_result = qualifier_result
                elif qualifier_context_result is None:
                    qualifier_context_result = qualifier_result
                continue

            same_range = (
                abs(claim_values["minimum"] - source_values["minimum"]) < 1e-9
                and abs(claim_values["maximum"] - source_values["maximum"]) < 1e-9
            )
            ranges_overlap = (
                claim_values["minimum"] <= source_values["maximum"]
                and source_values["minimum"] <= claim_values["maximum"]
            )
            if same_range:
                negation_conflict = (
                    _has_negation(statement) != _has_negation(sentence)
                )
                less_precise_than_source = (
                    source_measure["is_range"] != claim_measure["is_range"]
                )
                status = (
                    "contradicted"
                    if negation_conflict
                    else (
                        "contextualized"
                        if less_precise_than_source
                        or _statement_has_value_judgment(statement)
                        else "supported"
                    )
                )
                explanation = (
                    "Die messbare Angabe stimmt mit der passenden Quellenangabe "
                    "überein."
                )
                if negation_conflict:
                    explanation = (
                        "Die Zahl stimmt, aber die Aussage verneint oder bestätigt "
                        "sie im Gegensatz zur Quellenstelle."
                    )
                if status == "contextualized":
                    if less_precise_than_source:
                        explanation += (
                            " Die Quelle belegt dabei nicht dieselbe Genauigkeit "
                            "wie die Aussage."
                        )
                    elif _statement_has_value_judgment(statement):
                        explanation += (
                            " Das zusätzliche Werturteil lässt sich daraus nicht "
                            "als Fakt bestätigen."
                        )
                return {
                    "status": status,
                    "explanation": explanation,
                    "correction": "Quellenwert: {}.".format(source_measure["display"]),
                    "source_url": source_url,
                    "source_sentence": sentence,
                    "conflicting_source": {"sentence": sentence, "url": source_url}
                    if status == "contradicted"
                    else None,
                }

            if ranges_overlap:
                if range_context_result is None:
                    range_context_result = {
                        "status": "contextualized",
                        "explanation": (
                            "Die genannte Zahl liegt im Quellenbereich, aber die "
                            "Quelle bestätigt keinen genau diesen Einzelwert."
                        ),
                        "correction": "Quellenbereich: {}.".format(
                            source_measure["display"]
                        ),
                        "source_url": source_url,
                        "source_sentence": sentence,
                        "conflicting_source": None,
                    }
                continue

            status = "contradicted"
            explanation = (
                "Die messbare Angabe weicht von der passenden Quellenangabe ab: "
                "genannt werden {}, die Quelle nennt {}.".format(
                    claim_measure["display"],
                    source_measure["display"],
                )
            )
            if _statement_has_value_judgment(statement):
                explanation += (
                    " Der Zahlenanteil ist damit falsch; das zusätzliche "
                    "Werturteil wird nicht als eigene Tatsachenbehauptung bewertet."
                )
            if contradiction_result is None:
                contradiction_result = {
                    "status": status,
                    "explanation": explanation,
                    "correction": "Quellenwert: {}.".format(source_measure["display"]),
                    "source_url": source_url,
                    "source_sentence": sentence,
                    "conflicting_source": (
                        {"sentence": sentence, "url": source_url}
                        if status == "contradicted"
                        else None
                    ),
                }
    if range_context_result is not None:
        return range_context_result
    if qualifier_context_result is not None:
        return qualifier_context_result
    if contradiction_result is not None:
        return contradiction_result
    if context_mismatch is not None:
        source_url, sentence, source_measure = context_mismatch
        return {
            "status": "contextualized",
            "explanation": (
                "Die Zahl allein reicht für einen Vergleich nicht aus: "
                "Zeitbezug, Verkehrsmittel oder Richtungsangabe unterscheiden "
                "sich zwischen Aussage und Quelle."
            ),
            "correction": "Quellenangabe im Kontext: {}.".format(
                source_measure["display"]
            ),
            "source_url": source_url,
            "source_sentence": sentence,
            "conflicting_source": None,
        }
    return None


def _has_comparable_measurement_context(claim_measure, sentence):
    for source_measure in _measurements(sentence):
        shared_subjects = claim_measure["subjects"] & source_measure["subjects"]
        same_unit = claim_measure["unit"] == source_measure["unit"]
        convertible_duration = (
            claim_measure["unit"] in DURATION_FACTORS
            and source_measure["unit"] in DURATION_FACTORS
        )
        if not shared_subjects or not (same_unit or convertible_duration):
            continue
        if any(
            claim_measure[key] != source_measure[key]
            for key in ("time_basis", "direction_scope")
            if claim_measure[key] is not None or source_measure[key] is not None
        ):
            continue
        if (
            "travel_time" in shared_subjects
            and claim_measure["transport_mode"] is not None
            and source_measure["transport_mode"] is not None
            and claim_measure["transport_mode"] != source_measure["transport_mode"]
        ):
            continue
        return True
    return False


def _has_negation(text):
    words = _normalized_source_phrase(text).split()
    return any(
        word in NEGATION_WORDS and not (word == "nicht" and index + 1 < len(words) and words[index + 1] == "nur")
        for index, word in enumerate(words)
    )


def _has_qualification(text):
    return bool(set(_normalized_source_phrase(text).split()) & QUALIFICATION_WORDS)


def _named_location_terms(text):
    patterns = (
        r"\b(?:[Ii]n|[Nn]ach|[Aa]us|[Vv]on|[Bb]ei|[Gg]egenüber)\s+"
        r"(?:der\s+|dem\s+|den\s+)?"
        r"([A-ZÄÖÜ][a-zäöüß-]{2,})",
        r"\b(?:[Ss]tadt|[Ll]andkreis)\s+([A-ZÄÖÜ][a-zäöüß-]{2,})",
        r"\b(?:[Bb]ekommt|[Ee]rhält|[Pp]lant|[Bb]aut|[Ee]ntwickelt)\s+"
        r"([A-ZÄÖÜ][a-zäöüß-]{2,})",
        r"^\s*([A-ZÄÖÜ][a-zäöüß-]{2,})\s+"
        r"(?:[Bb]ekommt|[Ee]rhält|[Pp]lant|[Bb]aut|[Ee]ntwickelt)\b",
    )
    terms = set()
    for pattern in patterns:
        terms.update(
            match.group(1).casefold()
            for match in re.finditer(pattern, text)
            if match.group(1).casefold() not in NON_LOCATION_CAPITALIZED_WORDS
        )
    return {
        term[:-2] if term.endswith("er") and len(term) > 5 else term
        for term in terms
    }


def _has_location_mismatch(statement, source_url, sentence):
    claimed_locations = _named_location_terms(statement)
    if not claimed_locations:
        return False
    available_terms = _normalized_source_phrase(
        "{} {}".format(source_url, sentence)
    ).split()
    available_locations = set(available_terms)
    for term in tuple(available_terms):
        if term.endswith("er") and len(term) > 5:
            available_locations.add(term[:-2])
    return any(
        not any(
            available == claimed
            or available.startswith(claimed)
            or claimed.startswith(available)
            for available in available_locations
        )
        for claimed in claimed_locations
    )


def _semantic_query_variants(statement):
    normalized_statement = _normalized_source_phrase(statement)
    variants = [statement]
    concepts = _source_concepts(statement)
    for concept in sorted(concepts):
        expansion = SEMANTIC_CONCEPT_EXPANSIONS.get(concept)
        if expansion:
            variants.append("{} {}".format(statement, expansion))

    if re.search(r"\b(wann|ab wann|in welchem jahr|welcher termin)\b", normalized_statement):
        variants.append(
            "{} Zeitplan geplanter Start Inbetriebnahme Termin Jahr".format(statement)
        )
    if re.search(r"\b(wie hoch|wie viel|welche kosten|was kostet)\b", normalized_statement):
        variants.append(
            "{} Betrag Kosten Investitionssumme jährliche Aufwendungen Euro".format(statement)
        )
    if re.search(r"\b(wie viele|wieviel|welche anzahl)\b", normalized_statement):
        variants.append(
            "{} Anzahl Menge Fahrgäste Stationen Kapazität".format(statement)
        )
    if re.search(r"\b(wo|welcher ort|welche strecke)\b", normalized_statement):
        variants.append(
            "{} Ort Standort Streckenverlauf Haltestelle".format(statement)
        )

    return tuple(dict.fromkeys(variants))


def _semantic_query_concepts(statement):
    normalized_statement = _normalized_source_phrase(statement)
    concepts = set(_source_concepts(statement))
    if re.search(r"\b(wann|ab wann|in welchem jahr|welcher termin)\b", normalized_statement):
        concepts.add("planning")
    if re.search(r"\b(wie hoch|wie viel|welche kosten|was kostet)\b", normalized_statement):
        concepts.add("finance")
        if re.search(
            r"\b(?:projekt|bau|investition|gesamt(?:kosten|summe))\b",
            normalized_statement,
        ):
            concepts.add("investment")
    if re.search(r"\b(wie viele|wieviel|welche anzahl)\b", normalized_statement):
        concepts.add("ridership")
    if re.search(r"\b(wo|welcher ort|welche strecke)\b", normalized_statement):
        concepts.add("access")
    return concepts


def _compare_semantic_claim(statement, selected, similarity_score):
    if (
        not selected
        or similarity_score < SEMANTIC_SUPPORT_THRESHOLD
        or _statement_has_value_judgment(statement)
        or _measurements(statement)
    ):
        return None

    source_url, sentence = selected[0]
    statement_terms = _source_terms(statement) - NEGATION_WORDS
    source_terms = _source_terms(sentence) - NEGATION_WORDS
    matching_terms = statement_terms & source_terms
    coverage = len(matching_terms) / len(statement_terms)
    if len(matching_terms) < 2 or coverage < 0.7:
        return {
            "status": "contextualized",
            "explanation": (
                "Die semantische Ähnlichkeit und die wenigen gemeinsamen "
                "Schlüsselbegriffe reichen nicht aus, um die Aussage zu bestätigen."
            ),
            "correction": "Fundstelle zur Einordnung: {}".format(sentence[:360]),
            "source_url": source_url,
            "source_sentence": sentence,
        }

    if _has_negation(statement) != _has_negation(sentence):
        return {
            "status": "contradicted",
            "explanation": (
                "Die sinngemäße Aussage widerspricht der Bestätigung oder "
                "Verneinung in der Quelle."
            ),
            "correction": "Quellenangabe: {}".format(sentence[:360]),
            "source_url": source_url,
            "source_sentence": sentence,
            "conflicting_source": {"sentence": sentence, "url": source_url},
        }
    if _has_qualification(sentence) and not _has_qualification(statement):
        return {
            "status": "contextualized",
            "explanation": (
                "Die Quelle beschreibt einen Plan oder eine Prognose, nicht ein "
                "bereits eingetretenes Ergebnis."
            ),
            "correction": "Planungsstand laut Quelle: {}".format(sentence[:360]),
            "source_url": source_url,
            "source_sentence": sentence,
        }
    return {
        "status": "supported",
        "explanation": (
            "Die inhaltlich sinngleiche Aussage wird durch die Quelle gestützt."
        ),
        "correction": "Quellenangabe: {}".format(sentence[:360]),
        "source_url": source_url,
        "source_sentence": sentence,
    }


def _compare_text_claim(statement, selected):
    if _statement_has_value_judgment(statement) or _measurements(statement):
        return None

    statement_terms = _source_terms(statement) - NEGATION_WORDS
    if not statement_terms:
        return None
    statement_concepts = _source_concepts(statement)
    statement_is_negated = _has_negation(statement)

    for source_url, sentence in selected:
        source_terms = _source_terms(sentence) - NEGATION_WORDS
        matching_terms = statement_terms & source_terms
        matching_concepts = statement_concepts & _source_concepts(sentence)
        coverage = len(matching_terms) / len(statement_terms)
        concepts_cover_claim = (
            bool(statement_concepts)
            and matching_concepts == statement_concepts
        )
        if not (
            len(matching_terms) >= 2
            and (coverage >= 0.7 or concepts_cover_claim)
        ):
            continue

        if statement_is_negated != _has_negation(sentence):
            if _has_qualification(sentence) and not _has_qualification(statement):
                return {
                    "status": "contextualized",
                    "explanation": (
                        "Die Quelle beschreibt einen Plan oder eine Erwartung. "
                        "Das widerspricht der Aussage nicht als bereits "
                        "eingetretener Tatsache."
                    ),
                    "correction": "Planungsstand laut Quelle: {}".format(sentence[:360]),
                    "source_url": source_url,
                    "source_sentence": sentence,
                }
            return {
                "status": "contradicted",
                "explanation": (
                    "Die Aussage steht im Widerspruch zur ausdrücklichen "
                    "Bestätigung oder Verneinung in der Quelle."
                ),
                "correction": "Quellenangabe: {}".format(sentence[:360]),
                "source_url": source_url,
                "source_sentence": sentence,
                "conflicting_source": {"sentence": sentence, "url": source_url},
            }

        if _has_qualification(sentence) and not _has_qualification(statement):
            return {
                "status": "contextualized",
                "explanation": (
                    "Die Quelle beschreibt einen Plan, eine Prognose oder eine "
                    "Erwartung; sie bestätigt damit nicht, dass es bereits "
                    "eingetreten ist."
                ),
                "correction": "Planungsstand laut Quelle: {}".format(sentence[:360]),
                "source_url": source_url,
                "source_sentence": sentence,
            }

        return {
            "status": "supported",
            "explanation": (
                "Die Aussage wird durch eine inhaltlich passende Quellenangabe "
                "gestützt."
            ),
            "correction": "Quellenangabe: {}".format(sentence[:360]),
            "source_url": source_url,
            "source_sentence": sentence,
        }
    return None


def _generic_source_sentences(source_pages):
    for source_url, page_text in source_pages:
        for block in page_text.splitlines():
            sentences = re.split(r"(?<=[.!?])\s+", block)
            for sentence in sentences:
                sentence = sentence.strip()
                if sentence:
                    yield source_url, sentence


@lru_cache(maxsize=1)
def _load_semantic_model():
    try:
        from sentence_transformers import SentenceTransformer
    except ImportError as exc:
        raise SourceEvaluationError(
            "Im verwendeten Python-Interpreter '{}' fehlt sentence-transformers. "
            "Wähle den Projekt-Interpreter aus .venv oder installiere dort "
            "requirements-ml.txt.".format(sys.executable)
        ) from exc

    try:
        return SentenceTransformer(
            SEMANTIC_MODEL_NAME,
            local_files_only=True,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        raise SourceEvaluationError(
            "Das benötigte Sprachmodell '{}' ist nicht lokal verfügbar. "
            "Es wurde kein Urteil erstellt. Installiere die Abhängigkeiten aus "
            "requirements-ml.txt und lade das Modell einmalig lokal herunter."
            .format(SEMANTIC_MODEL_NAME)
        ) from exc


def _semantic_source_entries(source_pages):
    entries = []
    for source_url, page_text in source_pages:
        seen_contexts = set()
        for block in page_text.splitlines():
            sentences = [
                sentence.strip()
                for sentence in re.split(r"(?<=[.!?])\s+", block)
                if sentence.strip()
            ]
            eligible_sentences = []
            for sentence in sentences:
                word_count = len(re.findall(r"[^\W_]+", sentence))
                if word_count >= 6 or sentence.rstrip().endswith((".", "!", "?")):
                    eligible_sentences.append(sentence)
            for index, sentence in enumerate(eligible_sentences):
                start = max(0, index - 1)
                end = min(len(eligible_sentences), index + 2)
                context = " ".join(eligible_sentences[start:end])
                if context not in seen_contexts:
                    entries.append((source_url, sentence, context))
                    seen_contexts.add(context)
    return tuple(entries)


def _build_semantic_source_index(source_pages):
    entries = _semantic_source_entries(source_pages)
    if not entries:
        return SemanticSourceIndex((), (), None)
    if len(entries) > MAX_SEMANTIC_SOURCE_SENTENCES:
        raise SourceEvaluationError(
            "Die ausgewählten Quellen enthalten zu viele Textabschnitte für "
            "einen zuverlässigen lokalen Quellenabgleich ({}). Bitte reduziere "
            "die Anzahl oder Länge der Quellseiten.".format(len(entries))
        )

    model = _load_semantic_model()
    try:
        vectors = model.encode(
            [context for _, _, context in entries],
            convert_to_numpy=True,
            normalize_embeddings=True,
            show_progress_bar=False,
            batch_size=32,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        raise SourceEvaluationError(
            "Das Sprachmodell konnte die Quelltexte nicht inhaltlich vergleichen: "
            "{}".format(exc)
        ) from exc
    return SemanticSourceIndex(
        entries,
        tuple(tuple(float(value) for value in vector) for vector in vectors),
        model,
    )


def _rank_semantic_source_entries(statement, semantic_index):
    if not semantic_index.entries:
        return []

    try:
        query_vectors = semantic_index.model.encode(
            list(_semantic_query_variants(statement)),
            convert_to_numpy=True,
            normalize_embeddings=True,
            show_progress_bar=False,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        raise SourceEvaluationError(
            "Die Aussage konnte nicht mit den Quellen verglichen werden: "
            "{}".format(exc)
        ) from exc

    ranked = []
    query_concepts = _semantic_query_concepts(statement)
    for (source_url, sentence, context), source_vector in zip(
        semantic_index.entries, semantic_index.embeddings
    ):
        score = max(
            sum(
                float(query_value) * source_value
                for query_value, source_value in zip(query_vector, source_vector)
            )
            for query_vector in query_vectors
        )
        if _has_location_mismatch(statement, source_url, sentence):
            continue
        sentence_concepts = _source_concepts(context)
        shared_concepts = query_concepts & sentence_concepts
        query_financial_concepts = query_concepts & FINANCIAL_CONCEPTS
        sentence_financial_concepts = sentence_concepts & FINANCIAL_CONCEPTS
        has_shared_economic_value = (
            "economic_value" in query_concepts
            and "economic_value" in sentence_concepts
        )
        if query_financial_concepts and not (
            query_financial_concepts & sentence_financial_concepts
            or has_shared_economic_value
        ):
            continue
        specific_query_concepts = query_concepts & set(MEASURE_SUBJECT_TERMS)
        if (
            len(specific_query_concepts) == 1
            and not _statement_has_value_judgment(statement)
            and not (specific_query_concepts & sentence_concepts)
        ):
            continue
        if score >= SEMANTIC_MATCH_THRESHOLD or (
            shared_concepts and score >= 0.20
        ):
            ranked.append((score, source_url, sentence, context))
    ranked.sort(key=lambda item: item[0], reverse=True)
    return ranked


@lru_cache(maxsize=1)
def _load_nli_components():
    try:
        import torch
        from transformers import AutoModelForSequenceClassification, AutoTokenizer
    except ImportError as exc:
        raise SourceEvaluationError(
            "Für den kontextuellen Quellenvergleich fehlen ML-Abhängigkeiten. "
            "Installiere requirements-ml.txt im verwendeten Python-Interpreter."
        ) from exc

    try:
        tokenizer = AutoTokenizer.from_pretrained(
            NLI_MODEL_NAME,
            local_files_only=True,
        )
        model = AutoModelForSequenceClassification.from_pretrained(
            NLI_MODEL_NAME,
            local_files_only=True,
        )
        device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        model.to(device)
        model.eval()
    except (OSError, RuntimeError, ValueError) as exc:
        raise SourceEvaluationError(
            "Das lokale NLI-Modell '{}' fehlt oder konnte nicht geladen werden. "
            "Lade es einmalig lokal herunter; ohne das Modell wird kein "
            "inhaltliches Urteil erstellt.".format(NLI_MODEL_NAME)
        ) from exc

    label_indices = {}
    for index, label in model.config.id2label.items():
        normalized_label = re.sub(r"[^a-z]", "", str(label).casefold())
        for expected_label in ("entailment", "neutral", "contradiction"):
            if expected_label in normalized_label:
                label_indices[expected_label] = int(index)
    if set(label_indices) != {"entailment", "neutral", "contradiction"}:
        raise SourceEvaluationError(
            "Das NLI-Modell '{}' weist keine eindeutigen "
            "Entailment-/Neutral-/Contradiction-Labels aus.".format(NLI_MODEL_NAME)
        )
    return torch, tokenizer, model, device, label_indices


def _classify_nli_pairs(premises, hypothesis):
    if not premises:
        return []

    torch, tokenizer, model, device, label_indices = _load_nli_components()
    predictions = []
    try:
        for offset in range(0, len(premises), NLI_BATCH_SIZE):
            batch_premises = premises[offset : offset + NLI_BATCH_SIZE]
            encoded = tokenizer(
                batch_premises,
                [hypothesis] * len(batch_premises),
                padding=True,
                truncation=True,
                max_length=512,
                return_tensors="pt",
            )
            encoded = {name: tensor.to(device) for name, tensor in encoded.items()}
            with torch.inference_mode():
                logits = model(**encoded).logits
                batch_probabilities = torch.softmax(logits, dim=-1).cpu().tolist()
            predictions.extend(
                {
                    label: float(probabilities[index])
                    for label, index in label_indices.items()
                }
                for probabilities in batch_probabilities
            )
    except (OSError, RuntimeError, ValueError) as exc:
        raise SourceEvaluationError(
            "Das NLI-Modell konnte die Aussage nicht mit den Quellpassagen "
            "vergleichen: {}".format(exc)
        ) from exc
    return predictions


def _compare_nli_claim(statement, ranked_candidates):
    candidates = []
    source_counts = {}
    seen_contexts = set()
    for score, source_url, sentence, context in ranked_candidates:
        normalized_context = _normalized_source_phrase(context)
        if not normalized_context or normalized_context in seen_contexts:
            continue
        if source_counts.get(source_url, 0) >= NLI_MAX_CANDIDATES_PER_SOURCE:
            continue
        candidates.append((score, source_url, sentence, context))
        seen_contexts.add(normalized_context)
        source_counts[source_url] = source_counts.get(source_url, 0) + 1
        if len(candidates) >= NLI_MAX_CANDIDATES:
            break
    if not candidates:
        return None

    predictions = _classify_nli_pairs(
        [context for _, _, _, context in candidates],
        statement,
    )
    if len(predictions) != len(candidates):
        raise SourceEvaluationError(
            "Das NLI-Modell hat nicht für alle ausgewählten Quellpassagen "
            "eine Einordnung geliefert."
        )

    supporting = []
    contradicting = []
    for candidate, prediction in zip(candidates, predictions):
        entailment = prediction["entailment"]
        contradiction = prediction["contradiction"]
        if (
            entailment >= NLI_DECISION_THRESHOLD
            and entailment >= contradiction + 0.15
        ):
            supporting.append((candidate, prediction))
        if (
            contradiction >= NLI_DECISION_THRESHOLD
            and contradiction >= entailment + 0.15
        ):
            contradicting.append((candidate, prediction))

    best_support = max(
        supporting,
        key=lambda item: item[1]["entailment"] * item[0][0],
        default=None,
    )
    best_contradiction = max(
        contradicting,
        key=lambda item: item[1]["contradiction"] * item[0][0],
        default=None,
    )
    if best_support and best_contradiction:
        primary = max(
            (best_support, best_contradiction),
            key=lambda item: max(
                item[1]["entailment"],
                item[1]["contradiction"],
            ) * item[0][0],
        )[0]
        conflicting = (
            best_contradiction[0]
            if primary is best_support[0]
            else best_support[0]
        )
        return {
            "status": "contextualized",
            "explanation": (
                "Die ausgewählten Quellenpassagen stützen und widersprechen "
                "der Aussage zugleich. Deshalb ist kein eindeutiges Urteil möglich."
            ),
            "correction": "Die Quellenlage ist widersprüchlich.",
            "source_url": primary[1],
            "source_sentence": primary[3],
            "conflicting_source": {
                "sentence": conflicting[3],
                "url": conflicting[1],
            },
        }

    if best_support:
        candidate = best_support[0]
        return {
            "status": "supported",
            "explanation": (
                "Der Zusammenhang in der vollständigen Quellenpassage stützt "
                "die Aussage inhaltlich."
            ),
            "correction": "Gestützt durch die Quellenpassage.",
            "source_url": candidate[1],
            "source_sentence": candidate[3],
        }

    if best_contradiction:
        candidate = best_contradiction[0]
        return {
            "status": "contradicted",
            "explanation": (
                "Der Zusammenhang in der vollständigen Quellenpassage "
                "widerspricht der Aussage inhaltlich."
            ),
            "correction": "Widerspruch in der Quellenpassage.",
            "source_url": candidate[1],
            "source_sentence": candidate[3],
            "conflicting_source": {
                "sentence": candidate[3],
                "url": candidate[1],
            },
        }

    candidate = max(candidates, key=lambda item: item[0])
    return {
        "status": "unclear",
        "explanation": (
            "Die Fundstelle ist thematisch passend, stützt oder widerlegt die "
            "Aussage im Zusammenhang aber nicht eindeutig."
        ),
        "correction": "Mit diesen Fundstellen nicht eindeutig bewertbar.",
        "source_url": candidate[1],
        "source_sentence": candidate[3],
    }


def _question_has_relevant_evidence(statement, selected, similarity_score):
    if not selected:
        return False

    _, sentence = selected[0]
    query_terms = _source_terms(statement) - NEGATION_WORDS
    source_terms = _source_terms(sentence) - NEGATION_WORDS
    matching_terms = query_terms & source_terms
    coverage = len(matching_terms) / len(query_terms) if query_terms else 0
    if len(matching_terms) >= 2 and coverage >= 0.5 and similarity_score >= 0.45:
        return True

    query_concepts = _semantic_query_concepts(statement)
    source_concepts = _source_concepts(sentence)
    if (
        "planning" in query_concepts
        and "planning" in source_concepts
        and re.search(r"\b(?:19|20)\d{2}\b", sentence)
    ):
        return True

    specific_concepts = query_concepts & set(MEASURE_SUBJECT_TERMS)
    if specific_concepts and similarity_score >= 0.35:
        return any(
            specific_concepts & measurement["subjects"]
            for measurement in _measurements(sentence)
        )
    return False


def check_statement_against_sources(statement, source_pages, semantic_index=None):
    if semantic_index is None:
        semantic_index = _build_semantic_source_index(source_pages)
    normalized_statement = _normalized_source_phrase(statement)
    ranked_candidates = _rank_semantic_source_entries(statement, semantic_index)
    statement_measurements = _measurements(statement)
    if len(statement_measurements) == 1:
        claim_measure = statement_measurements[0]
        ranked_candidates = sorted(
            ranked_candidates,
            key=lambda candidate: not _has_comparable_measurement_context(
                claim_measure, candidate[2]
            ),
        )

    for _, source_url, sentence, _ in ranked_candidates:
        if normalized_statement and normalized_statement == _normalized_source_phrase(sentence):
            if statement.rstrip().endswith("?"):
                continue
            status = (
                "contextualized"
                if _statement_has_value_judgment(statement)
                else "supported"
            )
            return {
                "status": status,
                "statement": statement,
                "explanation": (
                    "Die Aussage stimmt wörtlich mit einer Fundstelle der "
                    "ausgewählten Quelle überein."
                    if status == "supported"
                    else "Die Quelle enthält das Werturteil wörtlich; das bestätigt "
                    "nicht dessen objektive Richtigkeit."
                ),
                "correction": "Wörtliche Fundstelle in: {}".format(source_url),
                "contexts": [],
            }
        if statement.rstrip().endswith("?") and sentence.rstrip().endswith("?"):
            continue

    selected = []
    selected_scores = []
    source_counts = {}
    for score, source_url, sentence, _ in ranked_candidates:
        if len(selected) >= 4:
            break
        if source_counts.get(source_url, 0) >= 3:
            continue
        normalized_sentence = _normalized_source_phrase(sentence)
        if any(
            normalized_sentence == _normalized_source_phrase(selected_sentence)
            for _, selected_sentence in selected
        ):
            continue
        selected.append((source_url, sentence))
        selected_scores.append(score)
        source_counts[source_url] = source_counts.get(source_url, 0) + 1

    if selected:
        evidence = "\n".join(
            "• {}\n  Quelle: {}".format(sentence[:360], source_url)
            for source_url, sentence in selected
        )
        conflicting_source = None
        measured_result = _compare_measured_claim(statement, selected)
        if measured_result:
            status = measured_result["status"]
            explanation = measured_result["explanation"]
            correction = measured_result["correction"]
            conflicting_source = measured_result.get("conflicting_source")
            answer = "Zu deinem Punkt „{}“: {}".format(statement, explanation)
            if conflicting_source:
                answer += "\nDie widersprechende Quellenstelle ist unten markiert."
            else:
                answer += "\nQuelle: {}".format(measured_result["source_url"])
        elif (
            statement.rstrip().endswith("?")
            and not _statement_has_value_judgment(statement)
            and _question_has_relevant_evidence(
                statement,
                selected,
                selected_scores[0] if selected_scores else 0.0,
            )
        ):
            status = "answered"
            explanation = (
                "Die ausgewählten Quellen enthalten eine passende Antwort auf "
                "diese Frage."
            )
            correction = "Antwort aus den ausgewählten Quellen; siehe Belege."
            answer = (
                "Antwort auf deine Frage „{}“:\n{}\n"
                "Die Antwort ist auf die Angaben dieser Quellen beschränkt."
            ).format(statement, evidence)
        elif statement.rstrip().endswith("?"):
            status = "contextualized"
            explanation = (
                "Die Fundstellen behandeln ein ähnliches Thema, enthalten aber "
                "keine ausreichend passende Antwort auf diese konkrete Frage."
            )
            correction = "Die Frage bleibt mit diesen Fundstellen unbeantwortet."
            answer = (
                "Einordnung zu deiner Frage „{}“:\n{}\n"
                "Die Fundstellen sind thematisch verwandt, belegen aber keine "
                "ausreichend konkrete Antwort."
            ).format(statement, evidence)
        else:
            text_result = None
            if (
                not _statement_has_value_judgment(statement)
                and not _measurements(statement)
            ):
                text_result = _compare_nli_claim(statement, ranked_candidates)
            if text_result is None:
                text_result = _compare_text_claim(statement, selected)
            if text_result is None:
                text_result = _compare_semantic_claim(
                    statement,
                    selected,
                    selected_scores[0] if selected_scores else 0.0,
                )
            if text_result:
                status = text_result["status"]
                explanation = text_result["explanation"]
                correction = text_result["correction"]
                conflicting_source = text_result.get("conflicting_source")
                answer = "Zu deinem Punkt „{}“: {}".format(statement, explanation)
                if conflicting_source:
                    answer += "\nDie widersprechende Quellenstelle ist unten markiert."
                else:
                    answer += (
                        "\nBeleg: {}\nQuelle: {}".format(
                            text_result["source_sentence"][:360],
                            text_result["source_url"],
                        )
                    )
            else:
                status = "contextualized"
                explanation = (
                    "Die Quellen liefern passende Fakten zur Einordnung, aber "
                    "keinen eindeutigen Beleg oder Widerspruch für die gesamte Aussage."
                )
                correction = "Die Einordnung bezieht sich nur auf diese Aussage."
                answer = (
                    "Einordnung zu deinem Punkt „{}“:\n{}\n"
                    "Die Fundstellen betreffen diesen Punkt; weitergehende "
                    "Schlussfolgerungen sind daraus nicht automatisch ableitbar."
                ).format(statement, evidence)
    else:
        status = "unclear"
        answer = (
            "Zu deinem Punkt „{}“: Die ausgewählten Quellen enthalten keine "
            "passende Angabe für eine konkrete Antwort. Deshalb bleibt sie mit "
            "diesen Quellen nicht belegt; das bedeutet nicht automatisch, dass "
            "sie falsch ist."
        ).format(statement)
        explanation = (
            "In den ausgewählten Quellen wurde keine passende Angabe für eine "
            "konkrete Antwort gefunden."
        )
        correction = "Nicht belegt in den ausgewählten Quellen."

    result = {
        "status": status,
        "statement": statement,
        "explanation": explanation,
        "correction": correction,
        "contexts": [answer],
    }
    if selected and conflicting_source:
        result["conflicting_source"] = conflicting_source
    return result


def check_text_against_sources(text, source_pages, semantic_index=None):
    if semantic_index is None:
        semantic_index = _build_semantic_source_index(source_pages)
    return [
        check_statement_against_sources(
            claim,
            source_pages,
            semantic_index=semantic_index,
        )
        for claim in _statement_parts(text)
    ]
