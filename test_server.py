import csv
import errno
import io
import json
import sqlite3
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import server


class MediaMonitorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_data_dir = server.DATA_DIR
        self.original_db_path = server.DB_PATH
        server.DATA_DIR = Path(self.temp_dir.name)
        server.DB_PATH = server.DATA_DIR / "test.sqlite3"
        server.initialize_db()

    def tearDown(self) -> None:
        server.DATA_DIR = self.original_data_dir
        server.DB_PATH = self.original_db_path
        self.temp_dir.cleanup()

    def test_uses_next_port_when_preferred_port_is_unavailable(self) -> None:
        fake_server = object()
        with patch.object(
            server,
            "DashboardHTTPServer",
            side_effect=[OSError(errno.EADDRINUSE, "address already in use"), fake_server],
        ) as server_factory:
            result = server.create_dashboard_server(8765)
        self.assertIs(result, fake_server)
        self.assertEqual(server_factory.call_args_list[0].args[0], ("127.0.0.1", 8765))
        self.assertEqual(server_factory.call_args_list[1].args[0], ("127.0.0.1", 8766))

    def test_normalizes_media_export_fields_and_unclassified_sentiment(self) -> None:
        normalized = server.normalize_record(
            {
                "mention_id": "m-1",
                "published_at": "2026-04-23T17:03:55+0000",
                "source_name": "example.de",
                "media_type_name": "News",
                "sentiment": -99,
                "reach": "3,143",
                "title": "Ein Titel",
                "content": "Ein Beitrag",
            }
        )
        self.assertIsNotNone(normalized)
        assert normalized is not None
        self.assertEqual(normalized["external_id"], "m-1")
        self.assertEqual(normalized["source"], "example.de")
        self.assertEqual(normalized["media_type"], "News")
        self.assertEqual(normalized["sentiment"], -99)
        self.assertEqual(normalized["published_at"], "2026-04-23T17:03:55+00:00")
        self.assertEqual(normalized["reach"], 3.143)

    def test_uses_tagged_sentiment_and_reason_without_classifying_text(self) -> None:
        for label, expected in (("positiv", 1), ("neutral", 0), ("negativ", -1)):
            with self.subTest(label=label):
                normalized = server.normalize_record(
                    {
                        "title": "Ein Beitrag",
                        "content": "Dieser Text wird nicht für eine Einordnung ausgewertet.",
                        "sentiment": label,
                        "reason": "Nicht ausdrücklich genannt",
                    }
                )
                self.assertIsNotNone(normalized)
                assert normalized is not None
                self.assertEqual(normalized["sentiment"], expected)
                self.assertEqual(normalized["reason"], "Nicht ausdrücklich genannt")

        untagged = server.normalize_record(
            {"title": "Ein Beitrag", "content": "Positiv klingender Text ohne Sentiment-Tag."}
        )
        self.assertIsNotNone(untagged)
        assert untagged is not None
        self.assertEqual(untagged["sentiment"], -99)

    def test_maps_csv_headers_and_city_project_aliases(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "articles.csv"
            path.write_text(
                "Überschrift;Beitrag;Stadt;Projekt;Datum\nTitel;Text;Heilbronn;Platzgestaltung;2026-01-02\n",
                encoding="utf-8",
            )
            records = list(server.iter_records(path, path.name))
        normalized = server.normalize_record(records[0])
        self.assertIsNotNone(normalized)
        assert normalized is not None
        self.assertEqual(normalized["title"], "Titel")
        self.assertEqual(normalized["content"], "Text")
        self.assertEqual(normalized["city"], "Heilbronn")
        self.assertEqual(normalized["project"], "Platzgestaltung")

    def test_reads_during_uncommitted_import_transaction(self) -> None:
        writer = sqlite3.connect(server.DB_PATH)
        reader = sqlite3.connect(server.DB_PATH, timeout=0.1)
        try:
            writer.execute("BEGIN EXCLUSIVE")
            writer.execute(
                "INSERT INTO datasets (id, name, filename, created_at) VALUES (?, ?, ?, ?)",
                ("pending", "Pending", "pending.json", "2026-01-01"),
            )
            count = reader.execute("SELECT COUNT(*) FROM datasets").fetchone()[0]
            self.assertEqual(count, 0)
            self.assertEqual(reader.execute("PRAGMA journal_mode").fetchone()[0], "wal")
        finally:
            writer.rollback()
            reader.close()
            writer.close()

    def test_potential_reach_counts_each_website_once(self) -> None:
        with server.connect_db() as db:
            db.execute(
                "INSERT INTO datasets (id, name, filename, created_at) VALUES (?, ?, ?, ?)",
                ("reach-test", "Reach test", "reach-test.csv", "2026-01-01"),
            )
            for index, (source, url, reach) in enumerate(
                (
                    ("Publisher A", "https://www.example.de/articles/one", 1500),
                    ("Publisher B", "https://example.de/articles/two", 1200),
                    ("Publisher B", "https://example.org/article", 400),
                    ("", "", 200),
                )
            ):
                db.execute(
                    "INSERT INTO records (dataset_id, source, url, reach, fingerprint) VALUES (?, ?, ?, ?, ?)",
                    ("reach-test", source, url, reach, f"reach-{index}"),
                )

        payload: dict[str, object] = {}
        handler = object.__new__(server.DashboardHandler)
        handler.send_json = lambda status, data: payload.update(status=status, data=data)
        handler.send_analytics({})

        analytics = payload["data"]
        self.assertEqual(analytics["reach"], 2100)
        self.assertEqual(analytics["reach_count"], 4)

    def test_streams_json_array_records(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "articles.json"
            path.write_text(
                '[{"title":"Erster","content":"Text"},{"title":"Zweiter","content":"Text 2"}]',
                encoding="utf-8",
            )
            records = list(server.iter_records(path, path.name))
        self.assertEqual([item["title"] for item in records], ["Erster", "Zweiter"])

    def test_retains_rows_with_unrecognized_column_names(self) -> None:
        normalized = server.normalize_record({"case_ref": "x-1", "summary_text": "Wertvolle Daten", "score": 4})
        self.assertIsNotNone(normalized)
        assert normalized is not None
        self.assertEqual(normalized["title"], "Datensatzzeile")
        self.assertIn("Wertvolle Daten", normalized["content"])

    def test_deduplicates_unchanged_web_page_content(self) -> None:
        record = {
            "mention_id": "one",
            "title": "Page",
            "content": "Same page content",
            "url": "https://example.org/page",
            "media_type_name": "Webseite",
        }
        with server.connect_db() as db:
            db.execute("INSERT INTO datasets (id, name, filename, created_at) VALUES ('d', 'Test', 'x', '2026-01-01')")
            first = server.add_records(db, "d", iter((record,)))
            record["mention_id"] = "two"
            record["published_at"] = "2026-01-02T00:00:00+00:00"
            second = server.add_records(db, "d", iter((record,)))
            count = db.execute("SELECT COUNT(*) FROM records").fetchone()[0]
        self.assertEqual(first, 1)
        self.assertEqual(second, 0)
        self.assertEqual(count, 1)

    def test_filter_values_are_parameterized_and_percent_is_literal(self) -> None:
        where, values = server.query_filters(
            {"q": ["100%_match"], "city": ["Heilbronn"], "date_to": ["2026-01-31"]}
        )
        self.assertIn("LIKE ? ESCAPE", where)
        self.assertNotIn("city = ?", where)
        self.assertNotIn("Heilbronn", values)
        self.assertIn("%100\\%\\_match%", values)
        self.assertEqual(values[-1], "2026-01-31T23:59:59.999999+00:00")

    def test_removes_dataset_via_delete_endpoint(self) -> None:
        dataset_id = "5d6ff0d1-bc29-49cb-8d6a-c749e326fd93"
        http_server = ThreadingHTTPServer(("127.0.0.1", 0), server.DashboardHandler)
        thread = threading.Thread(target=http_server.serve_forever)
        thread.start()
        base_url = f"http://127.0.0.1:{http_server.server_address[1]}"
        try:
            with server.connect_db() as db:
                db.execute(
                    "INSERT INTO datasets (id, name, filename, created_at, record_count) VALUES (?, ?, ?, ?, 1)",
                    (dataset_id, "Datensatz zum Löschen", "delete.json", "2026-01-01T00:00:00+00:00"),
                )
                db.execute(
                    "INSERT INTO records (dataset_id, external_id, published_at, title, content, fingerprint) VALUES (?, ?, ?, ?, ?, ?)",
                    (dataset_id, "r-1", "2026-01-01T00:00:00+00:00", "Zu löschender Beitrag", "Inhalt", "fp-delete")
                )
            request = urllib.request.Request(f"{base_url}/api/datasets/{dataset_id}", method="DELETE")
            with urllib.request.urlopen(request) as response:
                self.assertEqual(response.status, 200)
                payload = json.loads(response.read())
            self.assertTrue(payload["ok"])
            with server.connect_db() as db:
                self.assertIsNone(db.execute("SELECT 1 FROM datasets WHERE id = ?", (dataset_id,)).fetchone())
                self.assertEqual(db.execute("SELECT COUNT(*) FROM records WHERE dataset_id = ?", (dataset_id,)).fetchone()[0], 0)
        finally:
            http_server.shutdown()
            http_server.server_close()
            thread.join(timeout=5)

    def test_removes_all_projects_but_keeps_datasets_and_imported_project_tags(self) -> None:
        dataset_id = "5d6ff0d1-bc29-49cb-8d6a-c749e326fd93"
        project_id = "b18bead3-9b99-4f20-a674-c4d2d3f879bc"
        http_server = ThreadingHTTPServer(("127.0.0.1", 0), server.DashboardHandler)
        thread = threading.Thread(target=http_server.serve_forever)
        thread.start()
        base_url = f"http://127.0.0.1:{http_server.server_address[1]}"
        try:
            with server.connect_db() as db:
                db.execute(
                    "INSERT INTO datasets (id, name, filename, created_at) VALUES (?, ?, ?, ?)",
                    (dataset_id, "Datensatz", "data.json", "2026-01-01"),
                )
                db.execute(
                    "INSERT INTO projects (id, name, created_at) VALUES (?, ?, ?)",
                    (project_id, "Projekt", "2026-01-01"),
                )
                db.execute(
                    "INSERT INTO project_reference_urls (project_id, position, url) VALUES (?, ?, ?)",
                    (project_id, 0, "https://example.test/reference"),
                )
                db.execute(
                    "INSERT INTO project_datasets (project_id, dataset_id) VALUES (?, ?)",
                    (project_id, dataset_id),
                )
                db.execute(
                    "INSERT INTO records (dataset_id, project, title, fingerprint) VALUES (?, ?, ?, ?)",
                    (dataset_id, "Importiertes Projekt", "Beitrag", "fp-project-delete"),
                )
            request = urllib.request.Request(f"{base_url}/api/projects", method="DELETE")
            with urllib.request.urlopen(request) as response:
                self.assertEqual(response.status, 200)
                payload = json.loads(response.read())
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["deleted"], 1)
            with server.connect_db() as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM projects").fetchone()[0], 0)
                self.assertEqual(db.execute("SELECT COUNT(*) FROM project_reference_urls").fetchone()[0], 0)
                self.assertEqual(db.execute("SELECT COUNT(*) FROM project_datasets").fetchone()[0], 0)
                self.assertIsNotNone(db.execute("SELECT 1 FROM datasets WHERE id = ?", (dataset_id,)).fetchone())
                record = db.execute(
                    "SELECT project FROM records WHERE dataset_id = ?",
                    (dataset_id,),
                ).fetchone()
                self.assertEqual(record["project"], "Importiertes Projekt")
        finally:
            http_server.shutdown()
            http_server.server_close()
            thread.join(timeout=5)

    def test_removes_all_datasets_and_cascades_to_records_and_sources(self) -> None:
        dataset_ids = (
            "5d6ff0d1-bc29-49cb-8d6a-c749e326fd93",
            "b18bead3-9b99-4f20-a674-c4d2d3f879bc",
        )
        project_id = "add21f49-30c3-46a9-9fbe-5a0cc9394d4a"
        http_server = ThreadingHTTPServer(("127.0.0.1", 0), server.DashboardHandler)
        thread = threading.Thread(target=http_server.serve_forever)
        thread.start()
        base_url = f"http://127.0.0.1:{http_server.server_address[1]}"
        try:
            with server.connect_db() as db:
                for index, dataset_id in enumerate(dataset_ids):
                    db.execute(
                        "INSERT INTO datasets (id, name, filename, created_at) VALUES (?, ?, ?, ?)",
                        (dataset_id, f"Datensatz {index}", f"data-{index}.json", "2026-01-01"),
                    )
                    db.execute(
                        "INSERT INTO records (dataset_id, title, fingerprint) VALUES (?, ?, ?)",
                        (dataset_id, f"Beitrag {index}", f"fp-all-delete-{index}"),
                    )
                db.execute(
                    "INSERT INTO projects (id, name, created_at) VALUES (?, ?, ?)",
                    (project_id, "Projekt bleibt", "2026-01-01"),
                )
                db.execute(
                    "INSERT INTO project_reference_urls (project_id, position, url) VALUES (?, ?, ?)",
                    (project_id, 0, "https://example.test/reference"),
                )
                db.execute(
                    "INSERT INTO project_datasets (project_id, dataset_id) VALUES (?, ?)",
                    (project_id, dataset_ids[0]),
                )
                db.execute(
                    "INSERT INTO sources (id, dataset_id, url, name, created_at) VALUES (?, ?, ?, ?, ?)",
                    ("source-for-delete", dataset_ids[0], "https://example.test/feed", "Quelle", "2026-01-01"),
                )
            request = urllib.request.Request(f"{base_url}/api/datasets", method="DELETE")
            with urllib.request.urlopen(request) as response:
                self.assertEqual(response.status, 200)
                payload = json.loads(response.read())
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["deleted"], 2)
            with server.connect_db() as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM datasets").fetchone()[0], 0)
                self.assertEqual(db.execute("SELECT COUNT(*) FROM records").fetchone()[0], 0)
                self.assertEqual(db.execute("SELECT COUNT(*) FROM sources").fetchone()[0], 0)
                self.assertEqual(db.execute("SELECT COUNT(*) FROM project_datasets").fetchone()[0], 0)
                self.assertIsNotNone(db.execute("SELECT 1 FROM projects WHERE id = ?", (project_id,)).fetchone())
                self.assertEqual(db.execute("SELECT COUNT(*) FROM project_reference_urls").fetchone()[0], 1)
        finally:
            http_server.shutdown()
            http_server.server_close()
            thread.join(timeout=5)

    def test_creates_project_and_filters_linked_datasets(self) -> None:
        dataset_ids = (
            "5d6ff0d1-bc29-49cb-8d6a-c749e326fd93",
            "b18bead3-9b99-4f20-a674-c4d2d3f879bc",
        )
        http_server = ThreadingHTTPServer(("127.0.0.1", 0), server.DashboardHandler)
        thread = threading.Thread(target=http_server.serve_forever)
        thread.start()
        base_url = f"http://127.0.0.1:{http_server.server_address[1]}"
        try:
            with server.connect_db() as db:
                for index, dataset_id in enumerate(dataset_ids):
                    db.execute(
                        "INSERT INTO datasets (id, name, filename, created_at, record_count) VALUES (?, ?, ?, ?, 1)",
                        (dataset_id, f"Datensatz {index + 1}", f"dataset-{index + 1}.json", "2026-01-01"),
                    )
                    db.execute(
                        "INSERT INTO records (dataset_id, title, content, project, fingerprint) VALUES (?, ?, ?, ?, ?)",
                        (
                            dataset_id,
                            f"Beitrag {index + 1}",
                            "Projektbeitrag",
                            "Vorhandenes Teilprojekt" if index == 0 else "",
                            f"project-test-{index}",
                        ),
                    )

            uploaded_records = [
                {"mention_id": "uploaded-1", "title": "Direkt hochgeladen", "content": "Neuer Projektbeitrag"}
            ]
            upload_request = urllib.request.Request(
                f"{base_url}/api/datasets",
                data=json.dumps(uploaded_records).encode("utf-8"),
                headers={"X-File-Name": "project-upload.json", "Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(upload_request) as response:
                uploaded_dataset = json.loads(response.read())
            linked_dataset_ids = (*dataset_ids, uploaded_dataset["id"])

            request = urllib.request.Request(
                f"{base_url}/api/projects",
                data=json.dumps(
                    {"name": "Neues Gesamtprojekt", "dataset_ids": [*linked_dataset_ids, dataset_ids[0]]}
                ).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(request) as response:
                self.assertEqual(response.status, 201)
                project = json.loads(response.read())
            self.assertEqual(project["name"], "Neues Gesamtprojekt")
            self.assertEqual(project["dataset_count"], 3)

            with urllib.request.urlopen(f"{base_url}/api/records?project=Neues%20Gesamtprojekt") as response:
                records = json.loads(response.read())
            self.assertEqual(records["total"], 3)

            multi_dataset_query = urllib.parse.urlencode(
                [("dataset_id", dataset_ids[0]), ("dataset_id", dataset_ids[1])]
            )
            with urllib.request.urlopen(f"{base_url}/api/records?{multi_dataset_query}") as response:
                multi_dataset_records = json.loads(response.read())
            self.assertEqual(multi_dataset_records["total"], 2)

            multi_filter_query = urllib.parse.urlencode(
                [
                    ("dataset_id", dataset_ids[0]),
                    ("dataset_id", dataset_ids[1]),
                    ("project", "Neues Gesamtprojekt"),
                    ("project", "Vorhandenes Teilprojekt"),
                ]
            )
            with urllib.request.urlopen(f"{base_url}/api/records?{multi_filter_query}") as response:
                multi_filter_records = json.loads(response.read())
            self.assertEqual(multi_filter_records["total"], 2)

            with urllib.request.urlopen(f"{base_url}/api/export.csv?{multi_filter_query}") as response:
                exported_rows = list(csv.DictReader(io.StringIO(response.read().decode("utf-8-sig")), delimiter=";"))
            self.assertEqual(len(exported_rows), 2)
            self.assertEqual({row["Titel"] for row in exported_rows}, {"Beitrag 1", "Beitrag 2"})

            with urllib.request.urlopen(f"{base_url}/api/facets") as response:
                facets = json.loads(response.read())
            self.assertEqual(facets["projects"], ["Neues Gesamtprojekt", "Vorhandenes Teilprojekt"])

            with urllib.request.urlopen(f"{base_url}/api/facets?dataset_id={dataset_ids[1]}") as response:
                dataset_facets = json.loads(response.read())
            self.assertEqual(dataset_facets["projects"], ["Neues Gesamtprojekt"])
            with urllib.request.urlopen(f"{base_url}/api/facets?dataset_id={uploaded_dataset['id']}") as response:
                uploaded_facets = json.loads(response.read())
            self.assertEqual(uploaded_facets["projects"], ["Neues Gesamtprojekt"])
            with urllib.request.urlopen(f"{base_url}/api/facets?{multi_dataset_query}") as response:
                multi_dataset_facets = json.loads(response.read())
            self.assertEqual(multi_dataset_facets["projects"], ["Neues Gesamtprojekt", "Vorhandenes Teilprojekt"])

            with server.connect_db() as db:
                original_tag = db.execute(
                    "SELECT project FROM records WHERE dataset_id = ?", (dataset_ids[0],)
                ).fetchone()[0]
            self.assertEqual(original_tag, "Vorhandenes Teilprojekt")

            duplicate = urllib.request.Request(
                f"{base_url}/api/projects",
                data=json.dumps({"name": "neues gesamtprojekt", "dataset_ids": [dataset_ids[0]]}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with self.assertRaises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(duplicate)
            self.assertEqual(error.exception.code, 409)

            missing_dataset = urllib.request.Request(
                f"{base_url}/api/projects",
                data=json.dumps({"name": "Ungültiges Projekt", "dataset_ids": ["missing"]}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with self.assertRaises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(missing_dataset)
            self.assertEqual(error.exception.code, 404)
        finally:
            http_server.shutdown()
            http_server.server_close()
            thread.join(timeout=5)

    def test_links_datasets_when_all_record_tags_match_an_existing_project(self) -> None:
        project_id = "project-a"
        http_server = ThreadingHTTPServer(("127.0.0.1", 0), server.DashboardHandler)
        thread = threading.Thread(target=http_server.serve_forever)
        thread.start()
        base_url = f"http://127.0.0.1:{http_server.server_address[1]}"
        try:
            with server.connect_db() as db:
                db.execute(
                    "INSERT INTO projects (id, name, created_at) VALUES (?, ?, ?)",
                    (project_id, "Project A", "2026-01-01"),
                )
                for dataset_id, name in (("tagged", "Tagged dataset"), ("mixed", "Mixed dataset")):
                    db.execute(
                        "INSERT INTO datasets (id, name, filename, created_at) VALUES (?, ?, ?, ?)",
                        (dataset_id, name, f"{dataset_id}.json", "2026-01-01"),
                    )
                db.executemany(
                    "INSERT INTO records (dataset_id, project, title, content, fingerprint) VALUES (?, ?, ?, ?, ?)",
                    (
                        ("tagged", "Project A", "Tagged", "Content", "tagged-1"),
                        ("tagged", "", "Untagged", "Content", "tagged-2"),
                        ("mixed", "Project A", "First", "Content", "mixed-1"),
                        ("mixed", "Other project", "Second", "Content", "mixed-2"),
                    ),
                )

            server.initialize_db()

            upload = urllib.request.Request(
                f"{base_url}/api/datasets",
                data=json.dumps(
                    [{"title": "New tagged dataset", "content": "Content", "project": "Project A"}]
                ).encode("utf-8"),
                headers={"X-File-Name": "new-tagged.json", "Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(upload) as response:
                uploaded_dataset = json.loads(response.read())

            with urllib.request.urlopen(f"{base_url}/api/datasets") as response:
                datasets = {item["id"]: item for item in json.loads(response.read())["items"]}
            self.assertEqual(datasets["tagged"]["project_id"], project_id)
            self.assertEqual(datasets["tagged"]["project_name"], "Project A")
            self.assertIsNone(datasets["mixed"]["project_id"])
            self.assertEqual(datasets[uploaded_dataset["id"]]["project_name"], "Project A")

            with server.connect_db() as db:
                preserved_tag = db.execute(
                    "SELECT project FROM records WHERE dataset_id = 'tagged' AND fingerprint = 'tagged-1'"
                ).fetchone()[0]
            self.assertEqual(preserved_tag, "Project A")
        finally:
            http_server.shutdown()
            http_server.server_close()
            thread.join(timeout=5)

    def test_project_creation_stores_multiple_https_reference_urls(self) -> None:
        dataset_id = "reference-dataset"
        http_server = ThreadingHTTPServer(("127.0.0.1", 0), server.DashboardHandler)
        thread = threading.Thread(target=http_server.serve_forever)
        thread.start()
        base_url = f"http://127.0.0.1:{http_server.server_address[1]}"

        def post_project(reference_urls: list[str], name: str = "Reference project") -> urllib.request.Request:
            return urllib.request.Request(
                f"{base_url}/api/projects",
                data=json.dumps(
                    {
                        "name": name,
                        "dataset_ids": [dataset_id],
                        "reference_urls": reference_urls,
                    }
                ).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )

        try:
            with server.connect_db() as db:
                db.execute(
                    "INSERT INTO datasets (id, name, filename, created_at) VALUES (?, ?, ?, ?)",
                    (dataset_id, "Reference dataset", "reference.json", "2026-01-01"),
                )

            def validate_https_url(url: str) -> tuple[urllib.parse.SplitResult, tuple[object, ...]]:
                return urllib.parse.urlsplit(url), ()

            urls = ["https://city.example/project", "https://city.example/plan"]
            with patch.object(server, "validate_public_url", side_effect=validate_https_url):
                with urllib.request.urlopen(post_project([*urls, urls[0]])) as response:
                    self.assertEqual(response.status, 201)
                    project = json.loads(response.read())

                with self.assertRaises(urllib.error.HTTPError) as error:
                    urllib.request.urlopen(post_project(["http://city.example/project"], "HTTP project"))
                self.assertEqual(error.exception.code, 400)

            with urllib.request.urlopen(f"{base_url}/api/projects") as response:
                projects = json.loads(response.read())["items"]
            self.assertEqual(projects[0]["id"], project["id"])
            self.assertEqual(projects[0]["reference_urls"], urls)
            self.assertEqual(project["reference_count"], 2)
        finally:
            http_server.shutdown()
            http_server.server_close()
            thread.join(timeout=5)

    def test_project_reference_urls_survive_database_initialization_and_can_be_cleared(self) -> None:
        dataset_id = "reference-update-dataset"
        http_server = ThreadingHTTPServer(("127.0.0.1", 0), server.DashboardHandler)
        thread = threading.Thread(target=http_server.serve_forever)
        thread.start()
        base_url = f"http://127.0.0.1:{http_server.server_address[1]}"
        try:
            with server.connect_db() as db:
                db.execute(
                    "INSERT INTO datasets (id, name, filename, created_at) VALUES (?, ?, ?, ?)",
                    (dataset_id, "Reference update dataset", "reference.json", "2026-01-01"),
                )

            project_request = urllib.request.Request(
                f"{base_url}/api/projects",
                data=json.dumps(
                    {"name": "Reference update project", "dataset_ids": [dataset_id]}
                ).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(project_request) as response:
                project = json.loads(response.read())

            def validate_https_url(url: str) -> tuple[urllib.parse.SplitResult, tuple[object, ...]]:
                return urllib.parse.urlsplit(url), ()

            def put_references(project_id: str, urls: list[str]) -> urllib.request.Request:
                return urllib.request.Request(
                    f"{base_url}/api/projects/{project_id}/references",
                    data=json.dumps({"reference_urls": urls}).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="PUT",
                )

            urls = ["https://city.example/project", "https://city.example/plan"]
            with patch.object(server, "validate_public_url", side_effect=validate_https_url):
                with urllib.request.urlopen(put_references(project["id"], urls)) as response:
                    updated = json.loads(response.read())
                self.assertEqual(updated["reference_urls"], urls)
                self.assertEqual(updated["reference_count"], 2)

                server.initialize_db()
                with urllib.request.urlopen(f"{base_url}/api/projects") as response:
                    projects_after_restart = json.loads(response.read())["items"]
                self.assertEqual(projects_after_restart[0]["reference_urls"], urls)

                with self.assertRaises(urllib.error.HTTPError) as invalid_url:
                    urllib.request.urlopen(
                        put_references(project["id"], ["http://city.example/project"])
                    )
                self.assertEqual(invalid_url.exception.code, 400)

                missing_urls_request = urllib.request.Request(
                    f"{base_url}/api/projects/{project['id']}/references",
                    data=b"{}",
                    headers={"Content-Type": "application/json"},
                    method="PUT",
                )
                with self.assertRaises(urllib.error.HTTPError) as missing_urls:
                    urllib.request.urlopen(missing_urls_request)
                self.assertEqual(missing_urls.exception.code, 400)

                with urllib.request.urlopen(put_references(project["id"], [])) as response:
                    cleared = json.loads(response.read())
                self.assertEqual(cleared["reference_urls"], [])
                self.assertEqual(cleared["reference_count"], 0)

                with self.assertRaises(urllib.error.HTTPError) as missing_project:
                    urllib.request.urlopen(put_references("00000000-0000-0000-0000-000000000000", urls))
                self.assertEqual(missing_project.exception.code, 404)

            with urllib.request.urlopen(f"{base_url}/api/projects") as response:
                projects = json.loads(response.read())["items"]
            self.assertEqual(projects[0]["id"], project["id"])
            self.assertEqual(projects[0]["reference_urls"], [])
        finally:
            http_server.shutdown()
            http_server.server_close()
            thread.join(timeout=5)

    def test_records_are_assessed_using_only_comment_text_and_project_references(self) -> None:
        dataset_id = "assessment-dataset"
        comment = "Der Kommentar, der eingeordnet werden soll."
        http_server = ThreadingHTTPServer(("127.0.0.1", 0), server.DashboardHandler)
        thread = threading.Thread(target=http_server.serve_forever)
        thread.start()
        base_url = f"http://127.0.0.1:{http_server.server_address[1]}"
        try:
            with server.connect_db() as db:
                db.execute(
                    "INSERT INTO datasets (id, name, filename, created_at) VALUES (?, ?, ?, ?)",
                    (dataset_id, "Assessment dataset", "assessment.json", "2026-01-01"),
                )
                db.execute(
                    """
                    INSERT INTO records
                        (dataset_id, title, content, reason, sentiment, fingerprint)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        dataset_id,
                        "Titel darf nicht geprüft werden",
                        comment,
                        "Importierter Grund bleibt unverändert",
                        -1,
                        "assessment-record",
                    ),
                )

            def validate_https_url(url: str) -> tuple[urllib.parse.SplitResult, tuple[object, ...]]:
                return urllib.parse.urlsplit(url), ()

            project_request = urllib.request.Request(
                f"{base_url}/api/projects",
                data=json.dumps(
                    {
                        "name": "Assessment project",
                        "dataset_ids": [dataset_id],
                        "reference_urls": ["https://city.example/official"],
                    }
                ).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with patch.object(server, "validate_public_url", side_effect=validate_https_url):
                with urllib.request.urlopen(project_request) as response:
                    self.assertEqual(response.status, 201)

            source_pages = (("https://city.example/official", "Offizielle Projektangaben."),)
            assessment = [
                {
                    "status": "supported",
                    "statement": "Aussage eins.",
                    "explanation": "Eine Aussage wird gestützt.",
                },
                {
                    "status": "contradicted",
                    "statement": "Aussage zwei.",
                    "explanation": "Eine Aussage widerspricht der Quelle.",
                },
                {
                    "status": "unclear",
                    "statement": "Aussage drei.",
                    "explanation": "Eine Aussage lässt sich mit den Quellen nicht bewerten.",
                },
            ]
            with (
                patch.object(server, "fetch_project_reference_pages", return_value=source_pages),
                patch.object(server, "_semantic_index_for_pages", return_value=object()),
                patch.object(server.source_fact_check, "check_text_against_sources", return_value=assessment) as check_text,
            ):
                deadline = time.monotonic() + 5
                while True:
                    with urllib.request.urlopen(f"{base_url}/api/records") as response:
                        records = json.loads(response.read())
                    fact_check = records["items"][0].get("fact_check", {})
                    if fact_check.get("status") != "pending":
                        break
                    if time.monotonic() >= deadline:
                        self.fail("Background source assessment did not finish in time.")
                    time.sleep(0.02)

            self.assertEqual(
                records["items"][0]["fact_check"],
                {
                    "status": "contradicted",
                    "explanation": (
                        "Eine Aussage wird gestützt. Eine Aussage widerspricht der Quelle. "
                        "Eine Aussage lässt sich mit den Quellen nicht bewerten."
                    ),
                    "claims": [
                        {
                            "status": "supported",
                            "statement": "Aussage eins.",
                            "explanation": "Eine Aussage wird gestützt.",
                        },
                        {
                            "status": "contradicted",
                            "statement": "Aussage zwei.",
                            "explanation": "Eine Aussage widerspricht der Quelle.",
                        },
                        {
                            "status": "unclear",
                            "statement": "Aussage drei.",
                            "explanation": "Eine Aussage lässt sich mit den Quellen nicht bewerten.",
                        },
                    ],
                },
            )
            self.assertEqual(check_text.call_args.args[0], comment)
            self.assertEqual(check_text.call_args.args[1], source_pages)
            self.assertEqual(check_text.call_count, 1)
            self.assertEqual(records["items"][0]["sentiment"], -1)
            self.assertEqual(records["items"][0]["reason"], "Importierter Grund bleibt unverändert")
        finally:
            http_server.shutdown()
            http_server.server_close()
            thread.join(timeout=5)

    def test_records_are_returned_while_source_assessment_runs(self) -> None:
        dataset_id = "background-assessment-dataset"
        comment = "Der Kommentar wird im Hintergrund geprüft."
        http_server = ThreadingHTTPServer(("127.0.0.1", 0), server.DashboardHandler)
        thread = threading.Thread(target=http_server.serve_forever)
        thread.start()
        base_url = f"http://127.0.0.1:{http_server.server_address[1]}"
        assessment_started = threading.Event()
        release_assessment = threading.Event()
        try:
            with server.connect_db() as db:
                db.execute(
                    "INSERT INTO datasets (id, name, filename, created_at) VALUES (?, ?, ?, ?)",
                    (dataset_id, "Background assessment dataset", "assessment.json", "2026-01-01"),
                )
                db.execute(
                    """
                    INSERT INTO records
                        (dataset_id, title, content, sentiment, fingerprint)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (dataset_id, "Comment", comment, 1, "background-assessment-record"),
                )

            project_request = urllib.request.Request(
                f"{base_url}/api/projects",
                data=json.dumps(
                    {
                        "name": "Background assessment project",
                        "dataset_ids": [dataset_id],
                        "reference_urls": ["https://city.example/official"],
                    }
                ).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )

            def validate_https_url(url: str) -> tuple[urllib.parse.SplitResult, tuple[object, ...]]:
                return urllib.parse.urlsplit(url), ()

            with patch.object(server, "validate_public_url", side_effect=validate_https_url):
                with urllib.request.urlopen(project_request) as response:
                    self.assertEqual(response.status, 201)

            def wait_for_assessment(items: list[dict], reference_urls: tuple[str, ...]) -> None:
                self.assertEqual(reference_urls, ("https://city.example/official",))
                assessment_started.set()
                if not release_assessment.wait(5):
                    raise TimeoutError("Test source assessment was not released.")
                for item in items:
                    item["fact_check"] = {
                        "status": "supported",
                        "explanation": "Durch die Testquelle gestützt.",
                    }

            with patch.object(
                server,
                "annotate_project_comments",
                side_effect=wait_for_assessment,
            ) as annotate:
                with urllib.request.urlopen(
                    f"{base_url}/api/records?page_size=1",
                    timeout=3,
                ) as response:
                    records = json.loads(response.read())

                self.assertEqual(len(records["items"]), 1)
                self.assertEqual(records["items"][0]["content"], comment)
                self.assertEqual(records["items"][0]["fact_check"]["status"], "pending")
                self.assertTrue(assessment_started.wait(1))

                release_assessment.set()
                deadline = time.monotonic() + 5
                while True:
                    with urllib.request.urlopen(
                        f"{base_url}/api/records?page_size=1",
                        timeout=3,
                    ) as response:
                        records = json.loads(response.read())
                    fact_check = records["items"][0].get("fact_check", {})
                    if fact_check.get("status") != "pending":
                        break
                    if time.monotonic() >= deadline:
                        self.fail("Background source assessment result was not cached.")
                    time.sleep(0.02)
                self.assertEqual(annotate.call_count, 1)

            self.assertEqual(records["items"][0]["fact_check"]["status"], "supported")
        finally:
            release_assessment.set()
            http_server.shutdown()
            http_server.server_close()
            thread.join(timeout=5)

    def test_project_comments_without_reference_links_show_unavailable_assessment(self) -> None:
        dataset_id = "project-without-references"
        http_server = ThreadingHTTPServer(("127.0.0.1", 0), server.DashboardHandler)
        thread = threading.Thread(target=http_server.serve_forever)
        thread.start()
        base_url = f"http://127.0.0.1:{http_server.server_address[1]}"
        try:
            with server.connect_db() as db:
                db.execute(
                    "INSERT INTO datasets (id, name, filename, created_at) VALUES (?, ?, ?, ?)",
                    (dataset_id, "Dataset without references", "comments.json", "2026-01-01"),
                )
                db.execute(
                    """
                    INSERT INTO records
                        (dataset_id, title, content, sentiment, fingerprint)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (dataset_id, "Comment", "Kommentar ohne Quellenlinks.", 1, "comment-without-references"),
                )

            project_request = urllib.request.Request(
                f"{base_url}/api/projects",
                data=json.dumps(
                    {"name": "Project without references", "dataset_ids": [dataset_id]}
                ).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(project_request) as response:
                self.assertEqual(response.status, 201)
            with urllib.request.urlopen(f"{base_url}/api/records") as response:
                records = json.loads(response.read())
            self.assertEqual(
                records["items"][0]["fact_check"],
                {
                    "status": "error",
                    "explanation": "Für dieses Projekt sind keine Referenzlinks hinterlegt.",
                },
            )
        finally:
            http_server.shutdown()
            http_server.server_close()
            thread.join(timeout=5)

    def test_contextual_nli_distinguishes_support_contradiction_and_conflict(self) -> None:
        statement = "Die Stadt eröffnet den Tunnel."
        supporting_context = (
            "Die Stadt wird den Tunnel eröffnen. Die Finanzierung ist gesichert."
        )
        contradicting_context = (
            "Die Stadt wird den Tunnel nicht eröffnen. Das Vorhaben wurde abgesagt."
        )

        def assess(candidates: list[tuple], predictions: list[dict[str, float]]) -> dict:
            with (
                patch.object(
                    server.source_fact_check,
                    "_rank_semantic_source_entries",
                    return_value=candidates,
                ),
                patch.object(
                    server.source_fact_check,
                    "_classify_nli_pairs",
                    return_value=predictions,
                ) as classify,
            ):
                result = server.source_fact_check.check_statement_against_sources(
                    statement,
                    (),
                    semantic_index=object(),
                )
            self.assertEqual(
                classify.call_args.args[0],
                [candidate[3] for candidate in candidates],
            )
            return result

        support = assess(
            [(0.84, "https://city.example/plan", "Die Stadt wird den Tunnel eröffnen.", supporting_context)],
            [{"entailment": 0.88, "neutral": 0.08, "contradiction": 0.04}],
        )
        self.assertEqual(support["status"], "supported")
        self.assertIn(supporting_context, support["contexts"][0])

        contradiction = assess(
            [(0.84, "https://city.example/plan", "Die Stadt wird den Tunnel eröffnen.", supporting_context)],
            [{"entailment": 0.04, "neutral": 0.08, "contradiction": 0.88}],
        )
        self.assertEqual(contradiction["status"], "contradicted")

        neutral = assess(
            [(0.84, "https://city.example/plan", "Die Stadt wird den Tunnel eröffnen.", supporting_context)],
            [{"entailment": 0.12, "neutral": 0.76, "contradiction": 0.12}],
        )
        self.assertEqual(neutral["status"], "unclear")
        self.assertIn("nicht eindeutig", neutral["explanation"])

        conflict = assess(
            [
                (0.84, "https://city.example/plan", "Die Stadt wird den Tunnel eröffnen.", supporting_context),
                (0.76, "https://city.example/update", "Die Stadt wird den Tunnel eröffnen.", contradicting_context),
            ],
            [
                {"entailment": 0.88, "neutral": 0.08, "contradiction": 0.04},
                {"entailment": 0.04, "neutral": 0.08, "contradiction": 0.88},
            ],
        )
        self.assertEqual(conflict["status"], "contextualized")
        self.assertIn("widersprechen", conflict["explanation"])
        self.assertEqual(conflict["conflicting_source"]["sentence"], contradicting_context)

    def test_semantic_source_entries_keep_neighboring_sentences_as_context(self) -> None:
        entries = server.source_fact_check._semantic_source_entries(
            (
                (
                    "https://city.example/plan",
                    "Der Bau soll 2028 beginnen. Dafür fehlt jedoch noch die Finanzierung. "
                    "Die Stadt erwartet den Baubeginn im Frühjahr.",
                ),
            )
        )
        contextual_entries = [entry for entry in entries if "Finanzierung" in entry[1]]
        self.assertTrue(contextual_entries)
        self.assertIn("Der Bau soll 2028 beginnen.", contextual_entries[0][2])
        self.assertIn("Die Stadt erwartet den Baubeginn im Frühjahr.", contextual_entries[0][2])

    def test_assigns_each_dataset_to_at_most_one_project_and_supports_project_edits(self) -> None:
        dataset_ids = ("dataset-one", "dataset-two", "dataset-three")
        http_server = ThreadingHTTPServer(("127.0.0.1", 0), server.DashboardHandler)
        thread = threading.Thread(target=http_server.serve_forever)
        thread.start()
        base_url = f"http://127.0.0.1:{http_server.server_address[1]}"

        def post_json(path: str, payload: dict[str, object]) -> urllib.request.Request:
            return urllib.request.Request(
                f"{base_url}{path}",
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )

        try:
            with server.connect_db() as db:
                for index, dataset_id in enumerate(dataset_ids):
                    db.execute(
                        "INSERT INTO datasets (id, name, filename, created_at) VALUES (?, ?, ?, ?)",
                        (dataset_id, f"Dataset {index + 1}", f"dataset-{index + 1}.json", "2026-01-01"),
                    )

            with urllib.request.urlopen(
                post_json("/api/projects", {"name": "Project A", "dataset_ids": [dataset_ids[0]]})
            ) as response:
                project_a = json.loads(response.read())
            with urllib.request.urlopen(
                post_json(f"/api/projects/{project_a['id']}/datasets", {"dataset_ids": [dataset_ids[1]]})
            ) as response:
                added = json.loads(response.read())
            self.assertEqual(added["dataset_count"], 1)

            with urllib.request.urlopen(
                post_json(f"/api/projects/{project_a['id']}/datasets", {"dataset_ids": [dataset_ids[1]]})
            ) as response:
                repeated_add = json.loads(response.read())
            self.assertEqual(repeated_add["dataset_count"], 0)

            with urllib.request.urlopen(
                post_json("/api/projects", {"name": "Project B", "dataset_ids": [dataset_ids[2]]})
            ) as response:
                project_b = json.loads(response.read())
            with self.assertRaises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(
                    post_json(f"/api/projects/{project_b['id']}/datasets", {"dataset_ids": [dataset_ids[0]]})
                )
            self.assertEqual(error.exception.code, 409)

            with urllib.request.urlopen(f"{base_url}/api/datasets") as response:
                datasets = {item["id"]: item for item in json.loads(response.read())["items"]}
            self.assertEqual(datasets[dataset_ids[0]]["project_id"], project_a["id"])
            self.assertEqual(datasets[dataset_ids[0]]["project_name"], "Project A")
            self.assertEqual(datasets[dataset_ids[1]]["project_name"], "Project A")
            self.assertEqual(datasets[dataset_ids[2]]["project_name"], "Project B")
        finally:
            http_server.shutdown()
            http_server.server_close()
            thread.join(timeout=5)

    def test_completes_project_assignment_from_dataset_quick_action(self) -> None:
        dataset_id = "00000000-0000-0000-0000-000000000001"
        http_server = ThreadingHTTPServer(("127.0.0.1", 0), server.DashboardHandler)
        thread = threading.Thread(target=http_server.serve_forever)
        thread.start()
        base_url = f"http://127.0.0.1:{http_server.server_address[1]}"

        def post_json(path: str, payload: dict[str, object]) -> urllib.request.Request:
            return urllib.request.Request(
                f"{base_url}{path}",
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )

        try:
            with server.connect_db() as db:
                db.execute(
                    "INSERT INTO projects (id, name, created_at) VALUES (?, ?, ?)",
                    ("00000000-0000-0000-0000-000000000002", "Project Quick", "2026-01-01"),
                )
                db.execute(
                    "INSERT INTO datasets (id, name, filename, created_at) VALUES (?, ?, ?, ?)",
                    (dataset_id, "Quick assignment dataset", "quick.json", "2026-01-01"),
                )
                db.executemany(
                    """
                    INSERT INTO records (dataset_id, title, content, project, fingerprint)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        (dataset_id, "Tagged record", "Content", "Other tag", "quick-tagged"),
                        (dataset_id, "Blank record", "Content", "", "quick-blank"),
                    ),
                )

            with urllib.request.urlopen(
                post_json(f"/api/datasets/{dataset_id}/tags", {"project": "project quick"})
            ) as response:
                result = json.loads(response.read())
            self.assertEqual(
                result,
                {
                    "project_updated": 1,
                    "dataset_project_name": "Project Quick",
                    "project_matched": True,
                },
            )

            with urllib.request.urlopen(f"{base_url}/api/datasets") as response:
                datasets = json.loads(response.read())["items"]
            assigned_dataset = next(item for item in datasets if item["id"] == dataset_id)
            self.assertEqual(assigned_dataset["project_name"], "Project Quick")

            with server.connect_db() as db:
                records = db.execute(
                    "SELECT title, project FROM records WHERE dataset_id = ? ORDER BY title",
                    (dataset_id,),
                ).fetchall()
            self.assertEqual(
                [(row["title"], row["project"]) for row in records],
                [("Blank record", "project quick"), ("Tagged record", "Other tag")],
            )

            with server.connect_db() as db:
                db.execute(
                    "INSERT INTO projects (id, name, created_at) VALUES (?, ?, ?)",
                    ("00000000-0000-0000-0000-000000000003", "Other project", "2026-01-01"),
                )
                db.execute(
                    "INSERT INTO datasets (id, name, filename, created_at) VALUES (?, ?, ?, ?)",
                    ("00000000-0000-0000-0000-000000000004", "Assigned dataset", "assigned.json", "2026-01-01"),
                )
                db.execute(
                    "INSERT INTO project_datasets (project_id, dataset_id) VALUES (?, ?)",
                    ("00000000-0000-0000-0000-000000000003", "00000000-0000-0000-0000-000000000004"),
                )
                db.execute(
                    "INSERT INTO records (dataset_id, title, content, project, fingerprint) VALUES (?, ?, ?, ?, ?)",
                    ("00000000-0000-0000-0000-000000000004", "Untouched", "Content", "", "quick-conflict"),
                )
            with self.assertRaises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(
                    post_json(
                        "/api/datasets/00000000-0000-0000-0000-000000000004/tags",
                        {"project": "Project Quick"},
                    )
                )
            self.assertEqual(error.exception.code, 409)
            with server.connect_db() as db:
                unchanged_tag = db.execute(
                    "SELECT project FROM records WHERE dataset_id = ?",
                    ("00000000-0000-0000-0000-000000000004",),
                ).fetchone()["project"]
            self.assertEqual(unchanged_tag, "")
        finally:
            http_server.shutdown()
            http_server.server_close()
            thread.join(timeout=5)

    def test_rejects_non_public_source_targets(self) -> None:
        for url in (
            "file:///etc/passwd",
            "http://localhost/",
            "http://127.0.0.1/",
            "http://192.168.1.20/",
            "https://example.org:8443/",
        ):
            with self.subTest(url=url), self.assertRaises(ValueError):
                server.validate_public_url(url)

    def test_page_parser_ignores_script_and_navigation_text(self) -> None:
        parser = server.PageTextParser()
        parser.feed(
            "<html><title>Stadtprojekt</title><nav>Navigation</nav>"
            "<main><h1>Planung</h1><p>Mehr Grün im Viertel.</p></main>"
            "<script>secret()</script></html>"
        )
        self.assertEqual(parser.title, "Stadtprojekt")
        self.assertIn("Mehr Grün im Viertel.", parser.text())
        self.assertNotIn("Navigation", parser.text())
        self.assertNotIn("secret", parser.text())

    def test_http_upload_filter_analytics_and_private_source_rejection(self) -> None:
        http_server = ThreadingHTTPServer(("127.0.0.1", 0), server.DashboardHandler)
        thread = threading.Thread(target=http_server.serve_forever)
        thread.start()
        base_url = f"http://127.0.0.1:{http_server.server_address[1]}"
        try:
            with urllib.request.urlopen(base_url) as response:
                self.assertEqual(response.status, 200)
                self.assertIn(b"scoover", response.read())

            with urllib.request.urlopen(f"{base_url}/hero-city.png") as response:
                self.assertEqual(response.status, 200)
                self.assertEqual(response.headers.get_content_type(), "image/png")
                image = response.read()
            self.assertTrue(image.startswith(b"\x89PNG\r\n\x1a\n"))
            self.assertEqual(len(image), (server.ROOT / "hero-city.png").stat().st_size)

            with urllib.request.urlopen(f"{base_url}/scoover-wappen.png") as response:
                self.assertEqual(response.status, 200)
                self.assertEqual(response.headers.get_content_type(), "image/png")
                crest = response.read()
            self.assertTrue(crest.startswith(b"\x89PNG\r\n\x1a\n"))
            self.assertEqual(len(crest), (server.ROOT / "scoover-wappen.png").stat().st_size)

            sample = [
                {
                    "mention_id": "test-1",
                    "published_at": "2026-01-03T12:00:00+0000",
                    "title": "Planung für Heilbronn",
                    "content": "Das Projekt findet breite Zustimmung.",
                    "source_name": "example.de",
                    "media_type_name": "News",
                    "sentiment": "positiv",
                    "reason": "Nicht ausdrücklich genannt",
                    "reach": 1500,
                    "city": "Heilbronn",
                    "project": "Stadtplatz",
                }
            ]
            request = urllib.request.Request(
                f"{base_url}/api/datasets",
                data=json.dumps(sample).encode("utf-8"),
                headers={"X-File-Name": "sample.json", "Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(request) as response:
                imported = json.loads(response.read())
                self.assertEqual(response.status, 201)
            self.assertEqual(imported["record_count"], 1)

            with urllib.request.urlopen(
                f"{base_url}/api/analytics?project=Stadtplatz&bucket=day&range_days=7&range_end=2026-01-03"
            ) as response:
                analytics = json.loads(response.read())
            self.assertEqual(analytics["counts"]["positive"], 1)
            self.assertFalse(analytics["terms_sampled"])
            self.assertEqual(analytics["terms_sample_size"], server.TERM_SAMPLE_SIZE)
            self.assertEqual(len(analytics["series"]), 7)
            positive_terms = [item["term"] for item in analytics["terms"]["positive"]]
            self.assertIn("planung", positive_terms)

            with urllib.request.urlopen(f"{base_url}/api/records?project=Stadtplatz&sort=title&order=asc") as response:
                records = json.loads(response.read())
            self.assertEqual(records["total"], 1)
            self.assertEqual(records["items"][0]["title"], "Planung für Heilbronn")
            self.assertEqual(records["items"][0]["sentiment"], 1)
            self.assertEqual(records["items"][0]["sentiment_label"], "Positiv")
            self.assertEqual(records["items"][0]["reason"], "Nicht ausdrücklich genannt")
            self.assertNotIn("city", records["items"][0])

            with urllib.request.urlopen(f"{base_url}/api/facets?dataset_id={imported['id']}") as response:
                facets = json.loads(response.read())
            self.assertEqual(facets["projects"], ["Stadtplatz"])
            self.assertNotIn("cities", facets)
            self.assertEqual(facets["date_min"], "2026-01-03T12:00:00+00:00")
            self.assertEqual(facets["date_max"], "2026-01-03T12:00:00+00:00")

            with urllib.request.urlopen(
                f"{base_url}/api/records?record_q=Planung&record_media_type=News&record_sentiment=1"
            ) as response:
                filtered_records = json.loads(response.read())
            self.assertEqual(filtered_records["total"], 1)

            unmapped = [
                {
                    "mention_id": "unmapped-1",
                    "title": "Unzugeordneter Beitrag",
                    "content": "Ein zusätzlicher Testbeitrag.",
                    "published_at": "2026-01-04T12:00:00+00:00",
                }
            ]
            request = urllib.request.Request(
                f"{base_url}/api/datasets",
                data=json.dumps(unmapped).encode("utf-8"),
                headers={"X-File-Name": "unmapped.json", "Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(request) as response:
                unmapped_dataset = json.loads(response.read())
            request = urllib.request.Request(
                f"{base_url}/api/datasets/{unmapped_dataset['id']}/tags",
                data=json.dumps({"project": "Neues Projekt"}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(request) as response:
                assigned = json.loads(response.read())
            self.assertEqual(
                assigned,
                {
                    "project_updated": 1,
                    "dataset_project_name": None,
                    "project_matched": False,
                },
            )
            with urllib.request.urlopen(f"{base_url}/api/facets?dataset_id={unmapped_dataset['id']}") as response:
                tagged_facets = json.loads(response.read())
            self.assertEqual(tagged_facets["projects"], ["Neues Projekt"])

            with urllib.request.urlopen(f"{base_url}/api/export.csv") as response:
                exported = response.read().decode("utf-8-sig")
            self.assertIn("Planung für Heilbronn", exported)
            self.assertIn("Grund", exported.splitlines()[0])
            self.assertIn("Nicht ausdrücklich genannt", exported)
            self.assertNotIn("Stadt", exported.splitlines()[0])

            request = urllib.request.Request(
                f"{base_url}/api/sources",
                data=json.dumps({"url": "http://127.0.0.1/"}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with self.assertRaises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(request)
            self.assertEqual(error.exception.code, 400)
        finally:
            http_server.shutdown()
            http_server.server_close()
            thread.join()


if __name__ == "__main__":
    unittest.main()
