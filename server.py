from __future__ import annotations

import contextlib
import csv
import errno
import hashlib
import html.parser
import http.client
import ipaddress
import io
import json
import logging
import os
import re
import socket
import sqlite3
import ssl
import tempfile
import threading
import time
import urllib.parse
import uuid
import webbrowser
import xml.etree.ElementTree as ET
from collections import Counter, OrderedDict
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Generator, Iterator

import source_fact_check


ROOT = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("MEDIENMONITOR_DATA_DIR", ROOT / "data")).resolve()
DB_PATH = DATA_DIR / "medienmonitor.sqlite3"
MAX_UPLOAD_BYTES = 300 * 1024 * 1024
MAX_PAGE_BYTES = 5 * 1024 * 1024
MAX_PROJECT_REQUEST_BYTES = 1024 * 1024
HTTP_TIMEOUT_SECONDS = 12
PORT = int(os.environ.get("PORT", "8765"))
PORT_ATTEMPTS = 20
TERM_SAMPLE_SIZE = 2500
PROJECT_REFERENCE_CACHE_SECONDS = 15 * 60
FACT_CHECK_CACHE_MAX_ENTRIES = 5000
FACT_CHECK_PENDING_RESULT = {
    "status": "pending",
    "explanation": "Die Quellen werden im Hintergrund geprüft.",
}
_fact_check_cache: OrderedDict[
    tuple[Any, ...],
    tuple[float, dict[str, Any]],
] = OrderedDict()
_fact_check_pending: set[tuple[Any, ...]] = set()
_fact_check_lock = threading.Lock()
_fact_check_executor = ThreadPoolExecutor(
    max_workers=1,
    thread_name_prefix="scoover-source-check",
)
_logger = logging.getLogger(__name__)
STOP_WORDS = set(
    """
    aber alle allem allen aller alles als also am an andere anderen auch auf aus bei beim bereits bzw da dadurch daher dann das dass davon dazu dein deine dem den denn der des deshalb die dies diese diesem diesen dieser dieses doch dort du durch ein eine einem einen einer eines einige er es etwas für gegen gewesen gibt hatte hatten hier hin hinter ich ihm ihn ihre im immer in ist ja jede jedem jeden jeder jedes jetzt kann kein keine können könnte man mehr mein meine mit muss nach nicht nichts noch nun nur ob oder ohne sehr sein seine selbst sich sie sind so solche soll sollte sowie über um und uns unter vom von vor war waren warum was weil weiter welche welchem welchen welcher welches wenn werden wie wieder wir wo zu zum zur zwischen
    the and for with from that this have has are was were will would their there about into than then them they you your our his her its not but can all any while where when who what how as by at on of in is it an or do no up use new according page update privacy said more than its
    les des une dans pour qui que par avec sur aux est sont son sa ses comme plus pas mais cette ces leur leurs été être avoir après très tout tous toute aussi alors nous vous elle elles lui donc sans sous depuis entre ou ce cet en de et
    """.split()
)
TERM_PATTERN = re.compile(r"[^\W_]{4,}", re.UNICODE)

ALIASES: dict[str, tuple[str, ...]] = {
    "title": ("title", "headline", "subject", "überschrift", "titel", "name"),
    "content": ("content", "text", "body", "description", "article", "message", "inhalt", "beitrag", "textkörper"),
    "source": ("source_name", "source", "publisher", "domain", "quelle", "medium", "herausgeber"),
    "url": ("url", "link", "permalink", "source_url"),
    "published_at": ("published_at", "published", "date", "datetime", "created_at", "datum", "veröffentlicht", "timestamp"),
    "media_type": ("media_type_name", "media_type", "type", "kind", "medientyp", "format"),
    "sentiment": ("sentiment", "tone", "polarity", "stimmung", "tonalität"),
    "reason": ("reason", "cause", "grund", "ursache"),
    "reach": ("reach", "views", "impressions", "audience", "reichweite"),
    "engagements": ("engagements", "interactions", "likes", "engagement", "interaktionen"),
    "language": ("language_code", "language", "lang", "sprache"),
    "city": ("city", "town", "location_city", "stadt", "ort"),
    "project": ("project", "project_name", "initiative", "projekt", "thema"),
}
JSON_ARRAY_KEYS = ("data", "records", "items", "articles", "results", "mentions", "beiträge")
SORT_COLUMNS = {
    "published_at": "published_at",
    "reach": "reach",
    "source": "source",
    "title": "title",
    "sentiment": "sentiment",
    "media_type": "media_type",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextlib.contextmanager
def connect_db() -> Generator[sqlite3.Connection, None, None]:
    connection = sqlite3.connect(DB_PATH, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 30000")
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def link_datasets_by_project_tags(
    db: sqlite3.Connection,
    dataset_ids: Iterable[str] | None = None,
) -> None:
    dataset_filter = ""
    parameters: list[str] = []
    if dataset_ids is not None:
        selected_ids = list(dict.fromkeys(dataset_ids))
        if not selected_ids:
            return
        placeholders = ",".join("?" for _ in selected_ids)
        dataset_filter = f" AND dataset_id IN ({placeholders})"
        parameters = selected_ids

    db.execute(
        f"""
        INSERT OR IGNORE INTO project_datasets (project_id, dataset_id)
        SELECT p.id, tagged.dataset_id
        FROM (
            SELECT dataset_id, MIN(TRIM(project)) AS project_name
            FROM records
            WHERE TRIM(project) <> ''{dataset_filter}
            GROUP BY dataset_id
            HAVING COUNT(DISTINCT TRIM(project) COLLATE NOCASE) = 1
        ) AS tagged
        JOIN projects AS p ON p.name = tagged.project_name COLLATE NOCASE
        WHERE NOT EXISTS (
            SELECT 1 FROM project_datasets AS pd WHERE pd.dataset_id = tagged.dataset_id
        )
        """,
        parameters,
    )


def initialize_db() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DB_PATH, timeout=30)
    try:
        connection.execute("PRAGMA journal_mode = WAL")
    finally:
        connection.close()
    with connect_db() as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS datasets (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                filename TEXT NOT NULL,
                created_at TEXT NOT NULL,
                record_count INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS projects (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL COLLATE NOCASE UNIQUE,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS project_reference_urls (
                project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                position INTEGER NOT NULL,
                url TEXT NOT NULL,
                PRIMARY KEY (project_id, position),
                UNIQUE (project_id, url)
            );
            CREATE TABLE IF NOT EXISTS project_datasets (
                project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                dataset_id TEXT NOT NULL REFERENCES datasets(id) ON DELETE CASCADE,
                PRIMARY KEY (project_id, dataset_id)
            );
            CREATE INDEX IF NOT EXISTS idx_project_datasets_dataset ON project_datasets(dataset_id);
            CREATE TABLE IF NOT EXISTS records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                dataset_id TEXT NOT NULL REFERENCES datasets(id) ON DELETE CASCADE,
                external_id TEXT NOT NULL DEFAULT '',
                published_at TEXT NOT NULL DEFAULT '',
                source TEXT NOT NULL DEFAULT '',
                url TEXT NOT NULL DEFAULT '',
                media_type TEXT NOT NULL DEFAULT 'Unbekannt',
                language TEXT NOT NULL DEFAULT '',
                sentiment REAL NOT NULL DEFAULT -99,
                reason TEXT NOT NULL DEFAULT '',
                reach REAL NOT NULL DEFAULT 0,
                engagements REAL NOT NULL DEFAULT 0,
                city TEXT NOT NULL DEFAULT '',
                project TEXT NOT NULL DEFAULT '',
                title TEXT NOT NULL DEFAULT '',
                content TEXT NOT NULL DEFAULT '',
                raw_json TEXT NOT NULL DEFAULT '{}',
                fingerprint TEXT NOT NULL,
                UNIQUE(dataset_id, fingerprint)
            );
            CREATE INDEX IF NOT EXISTS idx_records_dataset_date ON records(dataset_id, published_at);
            CREATE INDEX IF NOT EXISTS idx_records_sentiment ON records(sentiment);
            CREATE INDEX IF NOT EXISTS idx_records_type ON records(media_type);
            CREATE INDEX IF NOT EXISTS idx_records_city_project ON records(city, project);
            CREATE TABLE IF NOT EXISTS sources (
                id TEXT PRIMARY KEY,
                dataset_id TEXT NOT NULL REFERENCES datasets(id) ON DELETE CASCADE,
                url TEXT NOT NULL UNIQUE,
                name TEXT NOT NULL,
                city TEXT NOT NULL DEFAULT '',
                project TEXT NOT NULL DEFAULT '',
                interval_minutes INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                last_fetch TEXT NOT NULL DEFAULT '',
                next_fetch REAL NOT NULL DEFAULT 0,
                last_error TEXT NOT NULL DEFAULT ''
            );
            """
        )
        record_columns = {row[1] for row in db.execute("PRAGMA table_info(records)")}
        if "reason" not in record_columns:
            db.execute("ALTER TABLE records ADD COLUMN reason TEXT NOT NULL DEFAULT ''")
        link_datasets_by_project_tags(db)
        conflicting_datasets = db.execute(
            """
            SELECT d.name, GROUP_CONCAT(p.name, ', ') AS project_names
            FROM project_datasets AS pd
            JOIN datasets AS d ON d.id = pd.dataset_id
            JOIN projects AS p ON p.id = pd.project_id
            GROUP BY pd.dataset_id
            HAVING COUNT(*) > 1
            """
        ).fetchall()
        if conflicting_datasets:
            conflicts = "; ".join(
                f"{row['name']}: {row['project_names']}" for row in conflicting_datasets
            )
            raise RuntimeError(
                "Datensätze dürfen nur einem Projekt zugeordnet sein. "
                f"Bitte bereinige zuerst diese Mehrfachzuordnungen: {conflicts}"
            )
        db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_project_datasets_dataset_unique "
            "ON project_datasets(dataset_id)"
        )


def value_for(row: dict[str, Any], canonical_name: str, default: Any = "") -> Any:
    lowered = {str(key).strip().lower(): value for key, value in row.items()}
    for alias in ALIASES[canonical_name]:
        if alias in lowered:
            return lowered[alias]
    return default


def as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value).strip()


def as_number(value: Any, default: float = 0.0) -> float:
    if value is None or value == "":
        return default
    try:
        if isinstance(value, str):
            value = value.strip().replace(",", ".")
        number = float(value)
        if number != number or abs(number) == float("inf"):
            return default
        return number
    except (TypeError, ValueError):
        return default


def as_sentiment(value: Any) -> float:
    if isinstance(value, str):
        labels = {
            "positiv": 1,
            "positive": 1,
            "neutral": 0,
            "negativ": -1,
            "negative": -1,
            "unentschlossen": -99,
            "unclassified": -99,
            "unknown": -99,
        }
        normalized = value.strip().casefold()
        if normalized in labels:
            return float(labels[normalized])
    return as_number(value, -99)


def normalize_date(value: Any) -> str:
    raw = as_text(value)
    if not raw:
        return ""
    normalized = raw.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        for date_format in ("%d.%m.%Y", "%Y/%m/%d", "%d/%m/%Y", "%Y-%m-%d"):
            try:
                parsed = datetime.strptime(raw[:10], date_format)
                break
            except ValueError:
                continue
        else:
            return raw[:80]
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds")


def normalize_record(record: dict[str, Any]) -> dict[str, Any] | None:
    title = as_text(value_for(record, "title"))
    content = as_text(value_for(record, "content"))
    if not title and not content:
        if not any(as_text(value) for value in record.values()):
            return None
        title = "Datensatzzeile"
        content = json.dumps(record, ensure_ascii=False, separators=(",", ":"), default=str)
    return {
        "external_id": as_text(record.get("mention_id", record.get("id", record.get("identifier", "")))),
        "published_at": normalize_date(value_for(record, "published_at")),
        "source": as_text(value_for(record, "source")),
        "url": as_text(value_for(record, "url")),
        "media_type": as_text(value_for(record, "media_type", "Unbekannt")) or "Unbekannt",
        "language": as_text(value_for(record, "language")),
        "sentiment": as_sentiment(value_for(record, "sentiment", -99)),
        "reason": as_text(value_for(record, "reason")),
        "reach": as_number(value_for(record, "reach")),
        "engagements": as_number(value_for(record, "engagements")),
        "city": as_text(value_for(record, "city")),
        "project": as_text(value_for(record, "project")),
        "title": title,
        "content": content,
        "raw_json": json.dumps(record, ensure_ascii=False, separators=(",", ":"), default=str),
    }


def iter_json_array(path: Path) -> Iterator[dict[str, Any]]:
    decoder = json.JSONDecoder()
    with path.open("r", encoding="utf-8-sig") as file:
        first = ""
        while not first:
            char = file.read(1)
            if not char:
                raise ValueError("Die JSON-Datei ist leer.")
            if not char.isspace():
                first = char
        if first != "[":
            file.seek(0)
            try:
                root = json.load(file)
            except json.JSONDecodeError as error:
                raise ValueError(f"Ungültiges JSON: {error}") from error
            if isinstance(root, dict):
                found_array = False
                for key in JSON_ARRAY_KEYS:
                    value = root.get(key)
                    if isinstance(value, list):
                        found_array = True
                        for item in value:
                            if isinstance(item, dict):
                                yield item
                        break
                if not found_array:
                    yield root
            elif isinstance(root, list):
                for item in root:
                    if isinstance(item, dict):
                        yield item
            else:
                raise ValueError("Die JSON-Datei muss ein Array oder ein Objekt mit Beiträgen enthalten.")
            return

        buffer = ""
        eof = False
        need_separator = False
        while True:
            while True:
                buffer = buffer.lstrip()
                if buffer and buffer[0] == ",":
                    buffer = buffer[1:]
                    need_separator = False
                    continue
                if buffer and buffer[0] == "]":
                    return
                if buffer:
                    break
                if eof:
                    raise ValueError("Das JSON-Array wurde nicht vollständig geschlossen.")
                chunk = file.read(1024 * 1024)
                if not chunk:
                    eof = True
                buffer += chunk
            if need_separator:
                raise ValueError("Zwischen JSON-Einträgen fehlt ein Komma.")
            try:
                item, end = decoder.raw_decode(buffer)
            except json.JSONDecodeError as error:
                if eof:
                    raise ValueError(f"Ungültiges JSON: {error}") from error
                chunk = file.read(1024 * 1024)
                if not chunk:
                    eof = True
                buffer += chunk
                continue
            buffer = buffer[end:]
            need_separator = True
            if isinstance(item, dict):
                yield item
            elif item is not None:
                raise ValueError("Das JSON-Array enthält einen Eintrag, der kein Objekt ist.")


def iter_records(path: Path, filename: str) -> Iterator[dict[str, Any]]:
    suffix = Path(filename).suffix.lower()
    if suffix in (".json", ".jsonl", ".ndjson"):
        if suffix in (".jsonl", ".ndjson"):
            with path.open("r", encoding="utf-8-sig") as file:
                for line_number, line in enumerate(file, start=1):
                    if not line.strip():
                        continue
                    try:
                        item = json.loads(line)
                    except json.JSONDecodeError as error:
                        raise ValueError(f"Fehler in JSONL-Zeile {line_number}: {error}") from error
                    if isinstance(item, dict):
                        yield item
            return
        yield from iter_json_array(path)
        return

    if suffix in (".csv", ".tsv"):
        with path.open("r", encoding="utf-8-sig", newline="") as file:
            sample = file.read(8192)
            file.seek(0)
            if suffix == ".tsv":
                reader = csv.DictReader(file, delimiter="\t")
            else:
                try:
                    dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
                    reader = csv.DictReader(file, dialect=dialect)
                except csv.Error:
                    reader = csv.DictReader(file)
            if not reader.fieldnames:
                raise ValueError("Die CSV-Datei enthält keine Kopfzeile.")
            for record in reader:
                yield dict(record)
        return

    if suffix in (".txt", ".text"):
        with path.open("r", encoding="utf-8-sig") as file:
            paragraph: list[str] = []
            index = 0
            for line in file:
                if line.strip():
                    paragraph.append(line.rstrip())
                elif paragraph:
                    index += 1
                    content = "\n".join(paragraph).strip()
                    yield {"title": content.splitlines()[0][:180], "content": content, "id": str(index)}
                    paragraph.clear()
            if paragraph:
                index += 1
                content = "\n".join(paragraph).strip()
                yield {"title": content.splitlines()[0][:180], "content": content, "id": str(index)}
        return

    if suffix in (".html", ".htm"):
        parser = PageTextParser()
        with path.open("r", encoding="utf-8-sig", errors="replace") as file:
            for chunk in iter(lambda: file.read(65536), ""):
                parser.feed(chunk)
        yield {"title": parser.title or Path(filename).stem, "content": parser.text(), "source": filename, "media_type": "Webseite"}
        return

    if suffix == ".xml":
        try:
            for _, element in ET.iterparse(path, events=("end",)):
                if element.tag.lower().split("}")[-1] in ("article", "item", "record", "entry", "beitrag"):
                    record = {
                        child.tag.lower().split("}")[-1]: "".join(child.itertext()).strip()
                        for child in list(element)
                    }
                    if record:
                        yield record
                    element.clear()
        except ET.ParseError as error:
            raise ValueError(f"Ungültiges XML: {error}") from error
        return

    raise ValueError("Dateiformat nicht unterstützt. Erlaubt sind JSON, JSONL, CSV, TSV, TXT, HTML und XML.")


class PageTextParser(html.parser.HTMLParser):
    BLOCKED_TAGS = {"script", "style", "noscript", "svg", "nav", "footer", "header"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._blocked = 0
        self._in_title = False
        self._title_parts: list[str] = []
        self._text_parts: list[str] = []

    @property
    def title(self) -> str:
        return " ".join(" ".join(self._title_parts).split())

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self.BLOCKED_TAGS:
            self._blocked += 1
        if tag == "title":
            self._in_title = True
        if tag in {"p", "article", "main", "section", "br", "h1", "h2", "li"}:
            self._text_parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self.BLOCKED_TAGS and self._blocked:
            self._blocked -= 1
        if tag == "title":
            self._in_title = False
        if tag in {"p", "article", "main", "section", "br", "h1", "h2", "li"}:
            self._text_parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self._title_parts.append(data)
        if not self._blocked:
            text = data.strip()
            if text:
                self._text_parts.append(text)

    def text(self) -> str:
        return re.sub(r"\n[ \t]*\n+", "\n\n", "\n".join(self._text_parts)).strip()


def record_fingerprint(dataset_id: str, record: dict[str, Any]) -> str:
    if record["media_type"] == "Webseite":
        basis = "\0".join((dataset_id, record["url"], record["content"]))
        return hashlib.sha256(basis.encode("utf-8", errors="replace")).hexdigest()
    basis = "\0".join(
        (
            dataset_id,
            record["external_id"],
            record["url"],
            record["published_at"],
            record["title"],
            record["content"],
        )
    )
    return hashlib.sha256(basis.encode("utf-8", errors="replace")).hexdigest()


def add_records(db: sqlite3.Connection, dataset_id: str, records: Iterator[dict[str, Any]]) -> int:
    statement = """
        INSERT OR IGNORE INTO records (
            dataset_id, external_id, published_at, source, url, media_type, language,
            sentiment, reason, reach, engagements, city, project, title, content, raw_json, fingerprint
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """
    batch: list[tuple[Any, ...]] = []
    count = 0
    for raw in records:
        normalized = normalize_record(raw)
        if normalized is None:
            continue
        batch.append(
            (
                dataset_id,
                normalized["external_id"],
                normalized["published_at"],
                normalized["source"],
                normalized["url"],
                normalized["media_type"],
                normalized["language"],
                normalized["sentiment"],
                normalized["reason"],
                normalized["reach"],
                normalized["engagements"],
                normalized["city"],
                normalized["project"],
                normalized["title"],
                normalized["content"],
                normalized["raw_json"],
                record_fingerprint(dataset_id, normalized),
            )
        )
        if len(batch) >= 500:
            previous_changes = db.total_changes
            db.executemany(statement, batch)
            count += db.total_changes - previous_changes
            batch.clear()
    if batch:
        previous_changes = db.total_changes
        db.executemany(statement, batch)
        count += db.total_changes - previous_changes
    link_datasets_by_project_tags(db, (dataset_id,))
    return count


def selected_query_values(params: dict[str, list[str]], key: str) -> list[str]:
    return list(
        dict.fromkeys(
            value.strip()
            for value in params.get(key, [])
            if value.strip() and value.strip() != "all"
        )
    )


def query_filters(
    params: dict[str, list[str]], include_display_filters: bool = False
) -> tuple[str, list[Any]]:
    clauses: list[str] = []
    values: list[Any] = []

    def first(key: str) -> str:
        return params.get(key, [""])[0].strip()

    dataset_ids = selected_query_values(params, "dataset_id")
    if dataset_ids:
        placeholders = ",".join("?" for _ in dataset_ids)
        clauses.append(f"dataset_id IN ({placeholders})")
        values.extend(dataset_ids)
    projects = selected_query_values(params, "project")
    if projects:
        placeholders = ",".join("?" for _ in projects)
        clauses.append(
            f"(project IN ({placeholders}) OR EXISTS ("
            "SELECT 1 FROM project_datasets AS pd "
            "JOIN projects AS p ON p.id = pd.project_id "
            f"WHERE pd.dataset_id = records.dataset_id AND p.name IN ({placeholders})))"
        )
        values.extend((*projects, *projects))
    for param, column in (("media_type", "media_type"),):
        value = first(param)
        if value:
            clauses.append(f"{column} = ?")
            values.append(value)
    if include_display_filters:
        for param, column in (("record_media_type", "media_type"), ("record_sentiment", "sentiment")):
            value = first(param)
            if value:
                clauses.append(f"{column} = ?")
                if column == "sentiment":
                    try:
                        values.append(float(value))
                    except ValueError as error:
                        raise ValueError("Ungültiger Sentiment-Filter.") from error
                else:
                    values.append(value)
    for sentiment in (first("sentiment"),):
        if not sentiment:
            continue
        try:
            clauses.append("sentiment = ?")
            values.append(float(sentiment))
        except ValueError as error:
            raise ValueError("Ungültiger Sentiment-Filter.") from error
    queries = [first("q")]
    if include_display_filters:
        queries.append(first("record_q"))
    for query in queries:
        query = query[:250]
        if query:
            escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            clauses.append("(title LIKE ? ESCAPE '\\' OR content LIKE ? ESCAPE '\\' OR source LIKE ? ESCAPE '\\')")
            pattern = f"%{escaped}%"
            values.extend((pattern, pattern, pattern))
    start = first("date_from")
    end = first("date_to")
    if start:
        clauses.append("published_at >= ?")
        values.append(start[:40])
    if end:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", end):
            end = f"{end}T23:59:59.999999+00:00"
        clauses.append("published_at <= ?")
        values.append(end[:40])
    return (" WHERE " + " AND ".join(clauses) if clauses else "", values)


def sentiment_label(value: float) -> str:
    if value == 1:
        return "Positiv"
    if value == -1:
        return "Negativ"
    if value == 0:
        return "Neutral"
    return "Unentschlossen"


def validate_public_url(raw_url: str) -> tuple[urllib.parse.SplitResult, tuple[Any, ...]]:
    if len(raw_url) > 2048:
        raise ValueError("Die URL ist zu lang.")
    parsed = urllib.parse.urlsplit(raw_url.strip())
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("Nur vollständige HTTP- oder HTTPS-URLs sind erlaubt.")
    if parsed.username or parsed.password:
        raise ValueError("URLs mit eingebetteten Zugangsdaten sind nicht erlaubt.")
    host = parsed.hostname.rstrip(".").lower()
    if host in {"localhost", "localhost.localdomain"} or host.endswith(".localhost") or host.endswith(".local"):
        raise ValueError("Lokale Hostnamen sind für Quellen gesperrt.")
    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError as error:
        raise ValueError("Die URL enthält einen ungültigen Port.") from error
    if port not in (80, 443):
        raise ValueError("Quellen müssen über den Standard-HTTP(S)-Port erreichbar sein.")
    try:
        literal = ipaddress.ip_address(host)
        addresses = [(socket.AF_INET6 if literal.version == 6 else socket.AF_INET, socket.SOCK_STREAM, 0, "", (str(literal), port))]
    except ValueError:
        try:
            addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except socket.gaierror as error:
            raise ValueError(f"Der Hostname konnte nicht aufgelöst werden: {error}") from error
    if not addresses:
        raise ValueError("Für den Hostnamen wurden keine IP-Adressen gefunden.")
    for address in addresses:
        ip = ipaddress.ip_address(address[4][0].split("%")[0])
        if not ip.is_global:
            raise ValueError("Private oder nicht öffentliche IP-Adressen sind als Quelle gesperrt.")
    return parsed, addresses[0]


class PinnedHTTPConnection(http.client.HTTPConnection):
    def __init__(self, host: str, port: int, address: tuple[Any, ...]) -> None:
        super().__init__(host, port, timeout=HTTP_TIMEOUT_SECONDS)
        self._resolved_address = address

    def connect(self) -> None:
        family, socktype, protocol, _, sockaddr = self._resolved_address
        self.sock = socket.socket(family, socktype, protocol)
        self.sock.settimeout(self.timeout)
        self.sock.connect(sockaddr)


class PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host: str, port: int, address: tuple[Any, ...]) -> None:
        super().__init__(host, port, timeout=HTTP_TIMEOUT_SECONDS)
        self._resolved_address = address

    def connect(self) -> None:
        family, socktype, protocol, _, sockaddr = self._resolved_address
        raw_socket = socket.socket(family, socktype, protocol)
        raw_socket.settimeout(self.timeout)
        raw_socket.connect(sockaddr)
        self.sock = self._context.wrap_socket(raw_socket, server_hostname=self.host)


def fetch_public_page(raw_url: str) -> tuple[str, str, str]:
    parsed, address = validate_public_url(raw_url)
    host = parsed.hostname or ""
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    path = urllib.parse.urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
    connection: http.client.HTTPConnection
    if parsed.scheme == "https":
        connection = PinnedHTTPSConnection(host, port, address)
    else:
        connection = PinnedHTTPConnection(host, port, address)
    try:
        connection.request("GET", path, headers={"User-Agent": "Scoover/1.0 (user-selected single-page fetch)", "Accept": "text/html,text/plain;q=0.9"})
        response = connection.getresponse()
        if response.status >= 300 and response.status < 400:
            raise ValueError("Weiterleitungen werden nicht automatisch verfolgt. Bitte die Ziel-URL direkt speichern.")
        if response.status != 200:
            raise ValueError(f"Die Seite antwortete mit HTTP {response.status}.")
        content_type = response.getheader("Content-Type", "").lower()
        if not any(kind in content_type for kind in ("text/html", "application/xhtml+xml", "text/plain")):
            raise ValueError("Die ausgewählte URL liefert keine HTML- oder Textseite.")
        length = response.getheader("Content-Length")
        if length and int(length) > MAX_PAGE_BYTES:
            raise ValueError("Die Seite überschreitet das Limit von 5 MB.")
        body = response.read(MAX_PAGE_BYTES + 1)
        if len(body) > MAX_PAGE_BYTES:
            raise ValueError("Die Seite überschreitet das Limit von 5 MB.")
        charset_match = re.search(r"charset=([A-Za-z0-9._-]+)", content_type)
        encoding = charset_match.group(1) if charset_match else "utf-8"
        try:
            source = body.decode(encoding, errors="replace")
        except LookupError:
            source = body.decode("utf-8", errors="replace")
        if "text/plain" in content_type:
            title = urllib.parse.urlsplit(raw_url).path.rsplit("/", 1)[-1] or host
            text = source
        else:
            parser = PageTextParser()
            parser.feed(source)
            title = parser.title or host
            text = parser.text()
        if not text:
            raise ValueError("Auf der ausgewählten Seite wurde kein lesbarer Inhalt gefunden.")
        return title[:500], text[:2_000_000], host
    finally:
        connection.close()


def validated_project_reference_urls(payload: dict[str, Any]) -> list[str]:
    raw_urls = payload.get("reference_urls", [])
    if not isinstance(raw_urls, list):
        raise ValueError("Die Projekt-Referenzlinks müssen eine Liste von HTTPS-URLs sein.")

    reference_urls: list[str] = []
    for raw_url in raw_urls:
        if not isinstance(raw_url, str):
            raise ValueError("Ein Projekt-Referenzlink ist ungültig.")
        url = raw_url.strip()
        if not url:
            continue
        if urllib.parse.urlsplit(url).scheme.lower() != "https":
            raise ValueError("Projekt-Referenzlinks müssen HTTPS-URLs sein.")
        parsed, _ = validate_public_url(url)
        normalized_url = parsed.geturl()
        if normalized_url not in reference_urls:
            reference_urls.append(normalized_url)
    return reference_urls


@lru_cache(maxsize=8)
def _fetch_project_reference_pages(
    reference_urls: tuple[str, ...],
    cache_period: int,
) -> tuple[tuple[str, str], ...]:
    pages = []
    for url in reference_urls:
        if urllib.parse.urlsplit(url).scheme.lower() != "https":
            raise ValueError("Projekt-Referenzlinks müssen HTTPS-URLs sein.")
        parsed, _ = validate_public_url(url)
        _, text, _ = fetch_public_page(url)
        pages.append((url, text))
    return tuple(pages)


def fetch_project_reference_pages(
    reference_urls: tuple[str, ...],
) -> tuple[tuple[str, str], ...]:
    cache_period = int(time.monotonic() // PROJECT_REFERENCE_CACHE_SECONDS)
    return _fetch_project_reference_pages(reference_urls, cache_period)


@lru_cache(maxsize=2)
def _semantic_index_for_pages(
    source_pages: tuple[tuple[str, str], ...],
) -> source_fact_check.SemanticSourceIndex:
    return source_fact_check._build_semantic_source_index(source_pages)


def summarize_comment_check(results: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not results:
        return None

    statuses = [result["status"] for result in results]
    if "contradicted" in statuses:
        status = "contradicted"
    elif all(value == "supported" for value in statuses):
        status = "supported"
    elif all(value == "answered" for value in statuses):
        status = "answered"
    elif all(value == "unclear" for value in statuses):
        status = "unclear"
    else:
        status = "contextualized"

    explanations = list(dict.fromkeys(
        result["explanation"].strip()
        for result in results
        if isinstance(result.get("explanation"), str) and result["explanation"].strip()
    ))
    claims = [
        {
            "status": result["status"],
            "statement": result.get("statement", ""),
            "explanation": result.get("explanation", ""),
        }
        for result in results
    ]
    return {
        "status": status,
        "explanation": " ".join(explanations),
        "claims": claims,
    }


def annotate_project_comments(
    items: list[dict[str, Any]],
    reference_urls: tuple[str, ...],
) -> None:
    comments = [item for item in items if item["content"].strip()]
    if not comments:
        return
    try:
        source_pages = fetch_project_reference_pages(reference_urls)
        semantic_index = _semantic_index_for_pages(source_pages)
    except (
        OSError,
        http.client.HTTPException,
        ValueError,
        source_fact_check.SourceEvaluationError,
    ) as error:
        for item in comments:
            item["fact_check"] = {"status": "error", "explanation": str(error)}
        return

    for item in comments:
        try:
            results = source_fact_check.check_text_against_sources(
                item["content"],
                source_pages,
                semantic_index=semantic_index,
            )
        except source_fact_check.SourceEvaluationError as error:
            item["fact_check"] = {"status": "error", "explanation": str(error)}
            continue
        summary = summarize_comment_check(results)
        if summary:
            item["fact_check"] = summary


def _fact_check_cache_key(
    item: dict[str, Any],
    reference_urls: tuple[str, ...],
) -> tuple[Any, ...]:
    content_hash = hashlib.sha256(item["content"].encode("utf-8")).hexdigest()
    return (
        str(DB_PATH),
        item["dataset_id"],
        item["id"],
        reference_urls,
        content_hash,
    )


def _complete_fact_check_batch(
    future: Future[None],
    work_items: list[dict[str, Any]],
    cache_keys: list[tuple[Any, ...]],
) -> None:
    error = future.exception()
    if error is not None:
        _logger.error(
            "Background source assessment failed.",
            exc_info=(type(error), error, error.__traceback__),
        )
        results = [
            {"status": "error", "explanation": f"Quellenprüfung fehlgeschlagen: {error}"}
            for _ in cache_keys
        ]
    else:
        results = [
            item.get("fact_check")
            or {
                "status": "error",
                "explanation": "Die Quellenprüfung hat keine Einordnung zurückgegeben.",
            }
            for item in work_items
        ]

    with _fact_check_lock:
        for cache_key, result in zip(cache_keys, results):
            _fact_check_pending.discard(cache_key)
            ttl = 60 if result["status"] == "error" else PROJECT_REFERENCE_CACHE_SECONDS
            _fact_check_cache[cache_key] = (time.monotonic() + ttl, result)
            _fact_check_cache.move_to_end(cache_key)
        while len(_fact_check_cache) > FACT_CHECK_CACHE_MAX_ENTRIES:
            _fact_check_cache.popitem(last=False)


def queue_project_comment_checks(
    items: list[dict[str, Any]],
    reference_urls: tuple[str, ...],
) -> None:
    work_items: list[dict[str, Any]] = []
    cache_keys: list[tuple[Any, ...]] = []
    queued_items: list[dict[str, Any]] = []
    current_time = time.monotonic()

    with _fact_check_lock:
        for item in items:
            if not item["content"].strip():
                continue
            cache_key = _fact_check_cache_key(item, reference_urls)
            cached = _fact_check_cache.get(cache_key)
            if cached is not None and cached[0] > current_time:
                _fact_check_cache.move_to_end(cache_key)
                item["fact_check"] = dict(cached[1])
                continue
            if cached is not None:
                del _fact_check_cache[cache_key]

            item["fact_check"] = dict(FACT_CHECK_PENDING_RESULT)
            if cache_key in _fact_check_pending:
                continue
            _fact_check_pending.add(cache_key)
            cache_keys.append(cache_key)
            queued_items.append(item)
            work_items.append({key: value for key, value in item.items() if key != "fact_check"})

    if not work_items:
        return

    try:
        future = _fact_check_executor.submit(
            annotate_project_comments,
            work_items,
            reference_urls,
        )
    except RuntimeError as error:
        _logger.error("Could not start background source assessment: %s", error)
        with _fact_check_lock:
            for cache_key, item in zip(cache_keys, queued_items):
                _fact_check_pending.discard(cache_key)
                item["fact_check"] = {
                    "status": "error",
                    "explanation": f"Quellenprüfung konnte nicht gestartet werden: {error}",
                }
        return

    future.add_done_callback(
        lambda completed: _complete_fact_check_batch(completed, work_items, cache_keys)
    )


def fetch_source(source_id: str) -> dict[str, Any]:
    with connect_db() as db:
        source = db.execute("SELECT * FROM sources WHERE id = ?", (source_id,)).fetchone()
        if source is None:
            raise ValueError("Die Quelle wurde nicht gefunden.")
    try:
        title, content, host = fetch_public_page(source["url"])
        record = {
            "mention_id": f"web-{uuid.uuid4().hex}",
            "title": title,
            "content": content,
            "source_name": host,
            "url": source["url"],
            "published_at": utc_now(),
            "media_type_name": "Webseite",
            "language_code": "",
            "sentiment": -99,
            "reach": None,
            "engagements": 0,
            "city": source["city"],
            "project": source["project"],
        }
        with connect_db() as db:
            added = add_records(db, source["dataset_id"], iter((record,)))
            if added:
                db.execute(
                    "UPDATE datasets SET record_count = record_count + ? WHERE id = ?",
                    (added, source["dataset_id"]),
                )
            db.execute(
                "UPDATE sources SET last_fetch = ?, next_fetch = ?, last_error = '' WHERE id = ?",
                (utc_now(), time.time() + source["interval_minutes"] * 60 if source["interval_minutes"] else 0, source_id),
            )
        return {"ok": True, "added": added, "title": title}
    except (OSError, ValueError, http.client.HTTPException, ssl.SSLError) as error:
        message = str(error)[:500]
        with connect_db() as db:
            db.execute(
                "UPDATE sources SET last_fetch = ?, next_fetch = ?, last_error = ? WHERE id = ?",
                (utc_now(), time.time() + source["interval_minutes"] * 60 if source["interval_minutes"] else 0, message, source_id),
            )
        raise ValueError(message) from error

def source_scheduler(stop_event: threading.Event) -> None:
    while not stop_event.wait(30):
        with connect_db() as db:
            due_sources = db.execute(
                "SELECT id FROM sources WHERE interval_minutes > 0 AND next_fetch <= ? ORDER BY next_fetch LIMIT 3",
                (time.time(),),
            ).fetchall()
        for source in due_sources:
            try:
                fetch_source(source["id"])
            except ValueError as error:
                print(f"Quellenabruf fehlgeschlagen ({source['id']}): {error}", flush=True)


def safe_dataset_name(filename: str) -> str:
    stem = Path(filename).stem.strip()
    clean = re.sub(r"[^\w .()\-]", "_", stem, flags=re.UNICODE).strip(" .-_")
    return clean[:120] or "Import"


def validated_dataset_ids(payload: dict[str, Any], empty_message: str) -> list[str]:
    raw_dataset_ids = payload.get("dataset_ids")
    if not isinstance(raw_dataset_ids, list) or not raw_dataset_ids:
        raise ValueError(empty_message)
    if any(not isinstance(dataset_id, str) or not dataset_id.strip() for dataset_id in raw_dataset_ids):
        raise ValueError("Die Datensatz-Auswahl ist ungültig.")
    dataset_ids = list(dict.fromkeys(dataset_id.strip() for dataset_id in raw_dataset_ids))
    if len(dataset_ids) > 500:
        raise ValueError("Es können höchstens 500 Datensätze gleichzeitig verknüpft werden.")
    return dataset_ids


class DashboardHandler(BaseHTTPRequestHandler):
    server_version = "Scoover/1.0"
    protocol_version = "HTTP/1.0"

    def log_message(self, message: str, *args: Any) -> None:
        print(f"{self.address_string()} - {message % args}", flush=True)

    def send_json(self, status: int, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def read_json_body(self, limit: int = 64 * 1024) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0 or length > limit:
            raise ValueError("Ungültige oder zu große Anfrage.")
        try:
            value = json.loads(self.rfile.read(length))
        except json.JSONDecodeError as error:
            raise ValueError(f"Ungültiges JSON: {error}") from error
        if not isinstance(value, dict):
            raise ValueError("Die Anfrage muss ein JSON-Objekt enthalten.")
        return value

    def do_GET(self) -> None:
        parsed = urllib.parse.urlsplit(self.path)
        params = urllib.parse.parse_qs(parsed.query)
        if parsed.path in ("/", "/dashboard.html"):
            try:
                body = (ROOT / "dashboard.html").read_bytes()
            except OSError:
                self.send_error(404, "dashboard.html fehlt")
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if parsed.path in ("/hero-city.png", "/scoover-wappen.png"):
            try:
                body = (ROOT / parsed.path.lstrip("/")).read_bytes()
            except OSError:
                self.send_error(404, "Das angeforderte Bild fehlt.")
                return
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if parsed.path == "/api/health":
            self.send_json(200, {"ok": True, "version": self.server_version})
            return
        if parsed.path == "/api/datasets":
            with connect_db() as db:
                rows = db.execute(
                    """
                    SELECT d.id, d.name, d.filename, d.created_at, COUNT(r.id) AS record_count,
                           (SELECT pd.project_id FROM project_datasets AS pd
                            WHERE pd.dataset_id = d.id LIMIT 1) AS project_id,
                           (SELECT p.name FROM project_datasets AS pd
                            JOIN projects AS p ON p.id = pd.project_id
                            WHERE pd.dataset_id = d.id LIMIT 1) AS project_name
                    FROM datasets d LEFT JOIN records r ON r.dataset_id = d.id
                    GROUP BY d.id ORDER BY d.created_at DESC
                    """
                ).fetchall()
            self.send_json(200, {"items": [dict(row) for row in rows]})
            return
        if parsed.path == "/api/projects":
            with connect_db() as db:
                rows = db.execute(
                    """
                    SELECT p.id, p.name, COUNT(pd.dataset_id) AS dataset_count
                    FROM projects AS p
                    LEFT JOIN project_datasets AS pd ON pd.project_id = p.id
                    GROUP BY p.id
                    ORDER BY p.name COLLATE NOCASE
                    """
                ).fetchall()
                references = db.execute(
                    """
                    SELECT project_id, url
                    FROM project_reference_urls
                    ORDER BY project_id, position
                    """
                ).fetchall()
            urls_by_project: dict[str, list[str]] = {}
            for reference in references:
                urls_by_project.setdefault(reference["project_id"], []).append(reference["url"])
            items = []
            for row in rows:
                item = dict(row)
                item["reference_urls"] = urls_by_project.get(item["id"], [])
                items.append(item)
            self.send_json(200, {"items": items})
            return
        if parsed.path == "/api/sources":
            with connect_db() as db:
                rows = db.execute(
                    """
                    SELECT s.id, s.dataset_id, s.url, s.name, s.city, s.project,
                           s.interval_minutes, s.created_at, s.last_fetch, s.last_error,
                           d.record_count
                    FROM sources s JOIN datasets d ON d.id = s.dataset_id ORDER BY s.created_at DESC
                    """
                ).fetchall()
            self.send_json(200, {"items": [dict(row) for row in rows]})
            return
        if parsed.path == "/api/facets":
            self.send_facets(params)
            return
        if parsed.path == "/api/records":
            self.send_records(params)
            return
        if parsed.path == "/api/analytics":
            self.send_analytics(params)
            return
        if parsed.path == "/api/export.csv":
            self.send_csv(params)
            return
        self.send_json(404, {"error": "API-Endpunkt nicht gefunden."})

    def send_facets(self, params: dict[str, list[str]]) -> None:
        dataset_ids = selected_query_values(params, "dataset_id")
        dataset_filter = ""
        dataset_values: list[str] = []
        project_dataset_filter = ""
        project_dataset_values: list[str] = []
        if dataset_ids:
            placeholders = ",".join("?" for _ in dataset_ids)
            dataset_filter = f" WHERE dataset_id IN ({placeholders})"
            dataset_values = dataset_ids
            project_dataset_filter = f" WHERE pd.dataset_id IN ({placeholders})"
            project_dataset_values = dataset_ids
        with connect_db() as db:
            projects = db.execute(
                f"SELECT DISTINCT project FROM records{dataset_filter + (' AND' if dataset_filter else ' WHERE')} project != '' ORDER BY project COLLATE NOCASE",
                dataset_values,
            ).fetchall()
            linked_projects = db.execute(
                "SELECT DISTINCT p.name FROM projects AS p "
                "JOIN project_datasets AS pd ON pd.project_id = p.id"
                f"{project_dataset_filter}",
                project_dataset_values,
            ).fetchall()
            media_types = db.execute(
                f"SELECT DISTINCT media_type FROM records{dataset_filter + (' AND' if dataset_filter else ' WHERE')} media_type != '' ORDER BY media_type COLLATE NOCASE",
                dataset_values,
            ).fetchall()
            date_min, date_max = db.execute(
                f"SELECT MIN(NULLIF(published_at, '')), MAX(NULLIF(published_at, '')) FROM records{dataset_filter}",
                dataset_values,
            ).fetchone()
        self.send_json(
            200,
            {
                "projects": sorted({row[0] for row in projects + linked_projects}, key=str.casefold),
                "media_types": [row[0] for row in media_types],
                "date_min": date_min or "",
                "date_max": date_max or "",
            },
        )

    def send_records(self, params: dict[str, list[str]]) -> None:
        where, values = query_filters(params, include_display_filters=True)
        try:
            page = max(1, int(params.get("page", ["1"])[0]))
            page_size = min(200, max(1, int(params.get("page_size", ["30"])[0])))
        except ValueError as error:
            raise ValueError("Seite und Seitengröße müssen Zahlen sein.") from error
        sort = params.get("sort", ["published_at"])[0]
        order = params.get("order", ["desc"])[0].lower()
        if sort not in SORT_COLUMNS or order not in ("asc", "desc"):
            raise ValueError("Ungültige Sortierung.")
        column = SORT_COLUMNS[sort]
        with connect_db() as db:
            total = db.execute(f"SELECT COUNT(*) FROM records{where}", values).fetchone()[0]
            rows = db.execute(
                f"SELECT id, dataset_id, external_id, published_at, source, url, media_type, language, sentiment, reason, reach, engagements, project, title, content FROM records{where} ORDER BY {column} {order.upper()}, id DESC LIMIT ? OFFSET ?",
                [*values, page_size, (page - 1) * page_size],
            ).fetchall()
            datasets = {
                row["id"]: row["name"]
                for row in db.execute("SELECT id, name FROM datasets").fetchall()
            }
            dataset_ids = list(dict.fromkeys(row["dataset_id"] for row in rows))
            references_by_dataset: dict[str, list[str]] = {}
            project_dataset_ids: set[str] = set()
            if dataset_ids:
                placeholders = ",".join("?" for _ in dataset_ids)
                references = db.execute(
                    f"""
                    SELECT pd.dataset_id, pr.url
                    FROM project_datasets AS pd
                    LEFT JOIN project_reference_urls AS pr ON pr.project_id = pd.project_id
                    WHERE pd.dataset_id IN ({placeholders})
                    ORDER BY pd.dataset_id, pr.position
                    """,
                    dataset_ids,
                ).fetchall()
                for reference in references:
                    project_dataset_ids.add(reference["dataset_id"])
                    if reference["url"]:
                        references_by_dataset.setdefault(reference["dataset_id"], []).append(reference["url"])
        items = []
        for row in rows:
            item = dict(row)
            item["sentiment_label"] = sentiment_label(item["sentiment"])
            item["dataset_name"] = datasets.get(item["dataset_id"], "")
            items.append(item)
        grouped_items: dict[tuple[str, ...], list[dict[str, Any]]] = {}
        for item in items:
            reference_urls = tuple(references_by_dataset.get(item["dataset_id"], ()))
            if reference_urls:
                grouped_items.setdefault(reference_urls, []).append(item)
            elif item["dataset_id"] in project_dataset_ids and item["content"].strip():
                item["fact_check"] = {
                    "status": "error",
                    "explanation": "Für dieses Projekt sind keine Referenzlinks hinterlegt.",
                }
        for reference_urls, project_items in grouped_items.items():
            queue_project_comment_checks(project_items, reference_urls)
        self.send_json(200, {"items": items, "total": total, "page": page, "page_size": page_size})

    def send_analytics(self, params: dict[str, list[str]]) -> None:
        where, values = query_filters(params)
        bucket_kind = params.get("bucket", ["month"])[0].lower()
        if bucket_kind not in ("day", "week", "month"):
            raise ValueError("Ungültige Zeitraumauswahl.")
        counts = {"positive": 0, "negative": 0, "neutral": 0, "unknown": 0}
        terms = {"positive": Counter(), "negative": Counter()}
        series: dict[str, dict[str, int]] = {}
        total = 0
        reach_by_website: dict[str, float] = {}
        reach_without_website = 0.0
        reach_count = 0
        sources: set[str] = set()
        dates: list[str] = []
        with connect_db() as db:
            rows = db.execute(
                f"SELECT published_at, source, url, sentiment, reach FROM records{where}",
                values,
            )
            for row in rows:
                total += 1
                source = row["source"]
                if source:
                    sources.add(source)
                if row["reach"] > 0:
                    reach_count += 1
                    url = (row["url"] or "").strip()
                    try:
                        website = urllib.parse.urlsplit(url if "://" in url else f"//{url}").hostname if url else None
                    except ValueError:
                        website = None
                    website_key = (website or source or "").strip().casefold().rstrip(".")
                    if website_key.startswith("www."):
                        website_key = website_key[4:]
                    if website_key:
                        reach_by_website[website_key] = max(
                            reach_by_website.get(website_key, 0.0),
                            row["reach"],
                        )
                    else:
                        reach_without_website += row["reach"]
                sentiment = row["sentiment"]
                key = "positive" if sentiment == 1 else "negative" if sentiment == -1 else "neutral" if sentiment == 0 else "unknown"
                counts[key] += 1
                published_at = row["published_at"]
                if published_at:
                    dates.append(published_at)
                    if bucket_kind == "month":
                        period = published_at[:7]
                    else:
                        try:
                            published_date = datetime.fromisoformat(published_at.replace("Z", "+00:00")).date()
                        except ValueError:
                            continue
                        period = published_date.isoformat()
                        if bucket_kind == "week":
                            period = (published_date - timedelta(days=published_date.weekday())).isoformat()
                    if period:
                        bucket = series.setdefault(period, {"positive": 0, "negative": 0})
                        if key in ("positive", "negative"):
                            bucket[key] += 1
            for key, sentiment in (("positive", 1), ("negative", -1)):
                term_where = f"{where} AND sentiment = ?" if where else " WHERE sentiment = ?"
                term_values = [*values, sentiment, TERM_SAMPLE_SIZE]
                term_rows = db.execute(
                    f"SELECT title, content FROM records{term_where} ORDER BY published_at DESC, id DESC LIMIT ?",
                    term_values,
                )
                for row in term_rows:
                    text = f"{row['title']} {row['content']}".lower()
                    words = {
                        word for word in TERM_PATTERN.findall(text)
                        if word not in STOP_WORDS and not word.isdigit()
                    }
                    terms[key].update(words)
        reach = reach_without_website + sum(reach_by_website.values())
        top_terms = {
            key: [{"term": term, "count": count} for term, count in counter.most_common(9)]
            for key, counter in terms.items()
        }
        range_days = params.get("range_days", [""])[0]
        if bucket_kind == "day" and range_days:
            try:
                day_count = min(366, max(1, int(range_days)))
                range_end = params.get("range_end", [""])[0] or (max(dates)[:10] if dates else "")
                end_date = datetime.fromisoformat(range_end[:10]).date()
            except ValueError as error:
                raise ValueError("Ungültiger Zeitraum.") from error
            start_date = end_date - timedelta(days=day_count - 1)
            for offset in range(day_count):
                period = (start_date + timedelta(days=offset)).isoformat()
                series.setdefault(period, {"positive": 0, "negative": 0})
        self.send_json(
            200,
            {
                "total": total,
                "counts": counts,
                "reach": round(reach, 2),
                "reach_count": reach_count,
                "sources": len(sources),
                "date_min": min(dates) if dates else "",
                "date_max": max(dates) if dates else "",
                "series": [{"month": month, **series[month]} for month in sorted(series)],
                "terms": top_terms,
                "terms_sampled": counts["positive"] > TERM_SAMPLE_SIZE or counts["negative"] > TERM_SAMPLE_SIZE,
                "terms_sample_size": TERM_SAMPLE_SIZE,
            },
        )

    def send_csv(self, params: dict[str, list[str]]) -> None:
        where, values = query_filters(params, include_display_filters=True)
        self.send_response(200)
        self.send_header("Content-Type", "text/csv; charset=utf-8")
        self.send_header("Content-Disposition", 'attachment; filename="medienmonitor-auswahl.csv"')
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        output = io.StringIO(newline="")
        writer = csv.writer(output, delimiter=";")
        writer.writerow(("Datum", "Titel", "Quelle", "URL", "Medientyp", "Stimmung", "Grund", "Reichweite", "Interaktionen", "Projekt", "Sprache", "Inhalt"))
        first_chunk = True
        with connect_db() as db:
            cursor = db.execute(
                f"SELECT published_at, title, source, url, media_type, sentiment, reason, reach, engagements, project, language, content FROM records{where} ORDER BY published_at DESC",
                values,
            )
            for index, row in enumerate(cursor, start=1):
                writer.writerow(tuple(row))
                if index % 250 == 0:
                    self.wfile.write(output.getvalue().encode("utf-8-sig" if first_chunk else "utf-8"))
                    output.seek(0)
                    output.truncate(0)
                    first_chunk = False
        if output.tell():
            self.wfile.write(output.getvalue().encode("utf-8-sig" if first_chunk else "utf-8"))

    def do_POST(self) -> None:
        parsed = urllib.parse.urlsplit(self.path)
        if parsed.path == "/api/datasets":
            self.upload_dataset()
            return
        if parsed.path == "/api/projects":
            self.create_project()
            return
        match = re.fullmatch(r"/api/projects/([0-9a-f-]+)/datasets", parsed.path)
        if match:
            self.add_datasets_to_project(match.group(1))
            return
        match = re.fullmatch(r"/api/datasets/([0-9a-f-]+)/tags", parsed.path)
        if match:
            self.assign_dataset_tags(match.group(1))
            return
        if parsed.path == "/api/sources":
            self.create_source()
            return
        match = re.fullmatch(r"/api/sources/([0-9a-f-]+)/sync", parsed.path)
        if match:
            try:
                result = fetch_source(match.group(1))
            except ValueError as error:
                self.send_json(400, {"error": str(error)})
                return
            self.send_json(200, result)
            return
        self.send_json(404, {"error": "API-Endpunkt nicht gefunden."})

    def do_PUT(self) -> None:
        parsed = urllib.parse.urlsplit(self.path)
        match = re.fullmatch(r"/api/projects/([0-9a-f-]+)/references", parsed.path)
        if match:
            self.update_project_references(match.group(1))
            return
        self.send_json(404, {"error": "API-Endpunkt nicht gefunden."})

    def upload_dataset(self) -> None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self.send_json(400, {"error": "Ungültige Upload-Größe."})
            return
        if length <= 0 or length > MAX_UPLOAD_BYTES:
            self.send_json(413, {"error": "Die Datei ist leer oder überschreitet das Upload-Limit von 300 MB."})
            return
        filename = urllib.parse.unquote(self.headers.get("X-File-Name", "upload.json"))
        filename = Path(filename.replace("\\", "/")).name
        if not filename:
            self.send_json(400, {"error": "Der Dateiname fehlt."})
            return
        dataset_id = str(uuid.uuid4())
        dataset_name = safe_dataset_name(filename)
        temporary_path: Path | None = None
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(prefix="upload-", suffix=Path(filename).suffix, dir=DATA_DIR, delete=False) as upload:
                temporary_path = Path(upload.name)
                remaining = length
                while remaining:
                    chunk = self.rfile.read(min(1024 * 1024, remaining))
                    if not chunk:
                        raise ValueError("Der Upload wurde vorzeitig beendet.")
                    upload.write(chunk)
                    remaining -= len(chunk)
            with connect_db() as db:
                db.execute(
                    "INSERT INTO datasets (id, name, filename, created_at, record_count) VALUES (?, ?, ?, ?, 0)",
                    (dataset_id, dataset_name, filename, utc_now()),
                )
                count = add_records(db, dataset_id, iter_records(temporary_path, filename))
                if count == 0:
                    raise ValueError("In der Datei wurden keine Beiträge mit Titel oder Text gefunden.")
                db.execute("UPDATE datasets SET record_count = ? WHERE id = ?", (count, dataset_id))
        except (OSError, ValueError, csv.Error, ET.ParseError) as error:
            with connect_db() as db:
                db.execute("DELETE FROM datasets WHERE id = ?", (dataset_id,))
            self.send_json(400, {"error": str(error)[:1000]})
            return
        finally:
            if temporary_path and temporary_path.exists():
                temporary_path.unlink()
        self.send_json(201, {"id": dataset_id, "name": dataset_name, "filename": filename, "record_count": count})

    def create_project(self) -> None:
        try:
            payload = self.read_json_body(limit=MAX_PROJECT_REQUEST_BYTES)
            name_value = payload.get("name")
            if not isinstance(name_value, str):
                raise ValueError("Gib einen Projektnamen an.")
            name = name_value.strip()
            if not name or len(name) > 160:
                raise ValueError("Der Projektname muss zwischen 1 und 160 Zeichen lang sein.")
            reference_urls = validated_project_reference_urls(payload)
            dataset_ids = validated_dataset_ids(
                payload,
                "Verknüpfe mindestens einen Datensatz mit dem Projekt.",
            )

            project_id = str(uuid.uuid4())
            created_at = utc_now()
            with connect_db() as db:
                placeholders = ",".join("?" for _ in dataset_ids)
                existing_ids = {
                    row["id"]
                    for row in db.execute(
                        f"SELECT id FROM datasets WHERE id IN ({placeholders})",
                        dataset_ids,
                    )
                }
                if existing_ids != set(dataset_ids):
                    self.send_json(404, {"error": "Einer oder mehrere ausgewählte Datensätze wurden nicht gefunden."})
                    return
                assigned_rows = db.execute(
                    f"""
                    SELECT d.name
                    FROM project_datasets AS pd
                    JOIN datasets AS d ON d.id = pd.dataset_id
                    WHERE pd.dataset_id IN ({placeholders})
                    ORDER BY d.name COLLATE NOCASE
                    """,
                    dataset_ids,
                ).fetchall()
                if assigned_rows:
                    names = ", ".join(row["name"] for row in assigned_rows)
                    self.send_json(
                        409,
                        {"error": f"Diese Datensätze gehören bereits zu einem Projekt: {names}"},
                    )
                    return
                db.execute(
                    "INSERT INTO projects (id, name, created_at) VALUES (?, ?, ?)",
                    (project_id, name, created_at),
                )
                db.executemany(
                    "INSERT INTO project_datasets (project_id, dataset_id) VALUES (?, ?)",
                    ((project_id, dataset_id) for dataset_id in dataset_ids),
                )
                db.executemany(
                    """
                    INSERT INTO project_reference_urls (project_id, position, url)
                    VALUES (?, ?, ?)
                    """,
                    (
                        (project_id, position, url)
                        for position, url in enumerate(reference_urls)
                    ),
                )
        except ValueError as error:
            self.send_json(400, {"error": str(error)})
            return
        except sqlite3.IntegrityError as error:
            message = (
                "Mindestens ein Datensatz ist bereits einem Projekt zugeordnet."
                if "project_datasets.dataset_id" in str(error)
                else "Ein Projekt mit diesem Namen existiert bereits."
            )
            self.send_json(409, {"error": message})
            return
        self.send_json(
            201,
            {
                "id": project_id,
                "name": name,
                "dataset_count": len(dataset_ids),
                "reference_count": len(reference_urls),
            },
        )

    def add_datasets_to_project(self, project_id: str) -> None:
        try:
            payload = self.read_json_body()
            dataset_ids = validated_dataset_ids(
                payload,
                "Wähle mindestens einen Datensatz aus.",
            )
            with connect_db() as db:
                project = db.execute(
                    "SELECT id, name FROM projects WHERE id = ?",
                    (project_id,),
                ).fetchone()
                if project is None:
                    self.send_json(404, {"error": "Das Projekt wurde nicht gefunden."})
                    return

                placeholders = ",".join("?" for _ in dataset_ids)
                existing_ids = {
                    row["id"]
                    for row in db.execute(
                        f"SELECT id FROM datasets WHERE id IN ({placeholders})",
                        dataset_ids,
                    )
                }
                if existing_ids != set(dataset_ids):
                    self.send_json(404, {"error": "Einer oder mehrere ausgewählte Datensätze wurden nicht gefunden."})
                    return

                assigned_rows = db.execute(
                    f"""
                    SELECT pd.dataset_id, d.name, p.id AS project_id, p.name AS project_name
                    FROM project_datasets AS pd
                    JOIN datasets AS d ON d.id = pd.dataset_id
                    JOIN projects AS p ON p.id = pd.project_id
                    WHERE pd.dataset_id IN ({placeholders})
                    ORDER BY d.name COLLATE NOCASE
                    """,
                    dataset_ids,
                ).fetchall()
                conflicts = [row for row in assigned_rows if row["project_id"] != project_id]
                if conflicts:
                    names = ", ".join(
                        f"{row['name']} ({row['project_name']})" for row in conflicts
                    )
                    self.send_json(
                        409,
                        {"error": f"Diese Datensätze gehören bereits zu einem anderen Projekt: {names}"},
                    )
                    return

                assigned_to_project = {
                    row["dataset_id"] for row in assigned_rows if row["project_id"] == project_id
                }
                new_dataset_ids = [
                    dataset_id
                    for dataset_id in dataset_ids
                    if dataset_id not in assigned_to_project
                ]
                db.executemany(
                    "INSERT INTO project_datasets (project_id, dataset_id) VALUES (?, ?)",
                    ((project_id, dataset_id) for dataset_id in new_dataset_ids),
                )
        except ValueError as error:
            self.send_json(400, {"error": str(error)})
            return
        except sqlite3.IntegrityError:
            self.send_json(409, {"error": "Mindestens ein Datensatz ist bereits einem anderen Projekt zugeordnet."})
            return
        self.send_json(
            200,
            {
                "project_id": project_id,
                "project_name": project["name"],
                "dataset_count": len(new_dataset_ids),
            },
        )

    def update_project_references(self, project_id: str) -> None:
        try:
            payload = self.read_json_body(limit=MAX_PROJECT_REQUEST_BYTES)
            if "reference_urls" not in payload:
                raise ValueError("Die Projekt-Referenzlinks müssen explizit angegeben werden.")
            reference_urls = validated_project_reference_urls(payload)
            with connect_db() as db:
                project = db.execute(
                    "SELECT id FROM projects WHERE id = ?",
                    (project_id,),
                ).fetchone()
                if project is None:
                    self.send_json(404, {"error": "Das Projekt wurde nicht gefunden."})
                    return

                db.execute(
                    "DELETE FROM project_reference_urls WHERE project_id = ?",
                    (project_id,),
                )
                db.executemany(
                    """
                    INSERT INTO project_reference_urls (project_id, position, url)
                    VALUES (?, ?, ?)
                    """,
                    (
                        (project_id, position, url)
                        for position, url in enumerate(reference_urls)
                    ),
                )
        except ValueError as error:
            self.send_json(400, {"error": str(error)})
            return

        self.send_json(
            200,
            {
                "id": project_id,
                "reference_count": len(reference_urls),
                "reference_urls": reference_urls,
            },
        )

    def assign_dataset_tags(self, dataset_id: str) -> None:
        try:
            payload = self.read_json_body()
            project = str(payload.get("project", "")).strip()[:160]
            if not project:
                raise ValueError("Gib ein Projekt an.")
            with connect_db() as db:
                if db.execute("SELECT 1 FROM datasets WHERE id = ?", (dataset_id,)).fetchone() is None:
                    self.send_json(404, {"error": "Der Datensatz wurde nicht gefunden."})
                    return
                project_row = db.execute(
                    "SELECT id, name FROM projects WHERE name = ? COLLATE NOCASE",
                    (project,),
                ).fetchone()
                assigned_project = db.execute(
                    """
                    SELECT p.id, p.name
                    FROM project_datasets AS pd
                    JOIN projects AS p ON p.id = pd.project_id
                    WHERE pd.dataset_id = ?
                    """,
                    (dataset_id,),
                ).fetchone()
                if project_row is not None:
                    if assigned_project is not None and assigned_project["id"] != project_row["id"]:
                        self.send_json(
                            409,
                            {"error": "Dieser Datensatz gehört bereits zu einem anderen Projekt."},
                        )
                        return
                    db.execute(
                        "INSERT OR IGNORE INTO project_datasets (project_id, dataset_id) VALUES (?, ?)",
                        (project_row["id"], dataset_id),
                    )
                project_count = db.execute(
                    """
                    UPDATE records SET project = ?
                    WHERE dataset_id = ? AND (project IS NULL OR TRIM(project) = '')
                    """,
                    (project, dataset_id),
                ).rowcount
            self.send_json(
                200,
                {
                    "project_updated": project_count,
                    "dataset_project_name": (
                        project_row["name"] if project_row is not None
                        else assigned_project["name"] if assigned_project is not None
                        else None
                    ),
                    "project_matched": project_row is not None,
                },
            )
        except ValueError as error:
            self.send_json(400, {"error": str(error)})

    def create_source(self) -> None:
        try:
            payload = self.read_json_body()
            url = str(payload.get("url", "")).strip()
            parsed, _ = validate_public_url(url)
            interval = int(payload.get("interval_minutes", 0))
            if interval not in (0, 15, 60, 360):
                raise ValueError("Abrufintervalle müssen manuell, 15 Minuten, 1 Stunde oder 6 Stunden sein.")
            city = str(payload.get("city", "")).strip()[:120]
            project = str(payload.get("project", "")).strip()[:160]
            name = str(payload.get("name", "")).strip()[:160] or parsed.hostname or url
            source_id = str(uuid.uuid4())
            dataset_id = str(uuid.uuid4())
            created = utc_now()
            with connect_db() as db:
                db.execute(
                    "INSERT INTO datasets (id, name, filename, created_at, record_count) VALUES (?, ?, ?, ?, 0)",
                    (dataset_id, f"Web · {name}"[:120], url, created),
                )
                db.execute(
                    """
                    INSERT INTO sources (id, dataset_id, url, name, city, project, interval_minutes, created_at, next_fetch)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (source_id, dataset_id, url, name, city, project, interval, created, time.time()),
                )
            self.send_json(201, {"id": source_id, "dataset_id": dataset_id, "name": name, "url": url})
        except (ValueError, sqlite3.IntegrityError) as error:
            self.send_json(400, {"error": "Diese URL ist bereits angelegt." if isinstance(error, sqlite3.IntegrityError) else str(error)})

    def do_DELETE(self) -> None:
        path = urllib.parse.urlsplit(self.path).path
        if path == "/api/projects":
            with connect_db() as db:
                deleted = db.execute("DELETE FROM projects").rowcount
            self.send_json(200, {"ok": True, "deleted": deleted})
            return
        if path == "/api/datasets":
            with connect_db() as db:
                deleted = db.execute("DELETE FROM datasets").rowcount
            self.send_json(200, {"ok": True, "deleted": deleted})
            return
        match = re.fullmatch(r"/api/datasets/([0-9a-f-]+)", path)
        if match:
            dataset_id = match.group(1)
            with connect_db() as db:
                if db.execute("SELECT 1 FROM datasets WHERE id = ?", (dataset_id,)).fetchone() is None:
                    self.send_json(404, {"error": "Der Datensatz wurde nicht gefunden."})
                    return
                db.execute("DELETE FROM datasets WHERE id = ?", (dataset_id,))
            self.send_json(200, {"ok": True})
            return
        match = re.fullmatch(r"/api/sources/([0-9a-f-]+)", path)
        if not match:
            self.send_json(404, {"error": "API-Endpunkt nicht gefunden."})
            return
        with connect_db() as db:
            row = db.execute("SELECT dataset_id FROM sources WHERE id = ?", (match.group(1),)).fetchone()
            if row is None:
                self.send_json(404, {"error": "Die Quelle wurde nicht gefunden."})
                return
            db.execute("DELETE FROM sources WHERE id = ?", (match.group(1),))
            db.execute("DELETE FROM datasets WHERE id = ?", (row["dataset_id"],))
        self.send_json(200, {"ok": True})

    def handle_one_request(self) -> None:
        try:
            super().handle_one_request()
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            return
        except ValueError as error:
            if not self.wfile.closed:
                self.send_json(400, {"error": str(error)})


class DashboardHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = False


def create_dashboard_server(preferred_port: int) -> ThreadingHTTPServer:
    last_error: OSError | None = None
    for port in range(preferred_port, min(preferred_port + PORT_ATTEMPTS, 65536)):
        try:
            return DashboardHTTPServer(("127.0.0.1", port), DashboardHandler)
        except OSError as error:
            if error.errno not in (errno.EADDRINUSE, errno.EACCES) and getattr(error, "winerror", None) not in (10013, 10048):
                raise
            last_error = error
    raise OSError(
        f"Kein freier Port zwischen {preferred_port} und "
        f"{min(preferred_port + PORT_ATTEMPTS - 1, 65535)} gefunden."
    ) from last_error


def main() -> None:
    initialize_db()
    server = create_dashboard_server(PORT)
    server.daemon_threads = True
    stop_event = threading.Event()
    worker = threading.Thread(target=source_scheduler, args=(stop_event,), daemon=True)
    worker.start()
    url = f"http://127.0.0.1:{server.server_port}"
    print(f"Scoover laeuft lokal auf {url}", flush=True)
    try:
        try:
            if not webbrowser.open(url, new=2):
                print("Browser konnte nicht automatisch starten. Adresse oben manuell oeffnen.", flush=True)
        except (OSError, webbrowser.Error) as error:
            print(f"Browser konnte nicht automatisch starten: {error}. Adresse oben manuell oeffnen.", flush=True)
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServer wird beendet …", flush=True)
    finally:
        stop_event.set()
        server.server_close()


if __name__ == "__main__":
    main()
