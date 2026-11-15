# scoover

Lokales, interaktives Dashboard für Medienbeiträge. Das Basis-Backend nutzt die Python-Standardbibliothek und SQLite. Für die optionale lokale Quellen-Einordnung wird zusätzlich `sentence-transformers` und ein lokal gespeichertes Sprachmodell benötigt.

## Start

1. Python 3.10 oder neuer installieren.
2. `dashboard-steuerung.bat` doppelklicken. Im Menü kannst du den Server starten, stoppen oder neu starten. Beim Start öffnet sich das Dashboard automatisch im Browser; die Servermeldungen erscheinen in einem eigenen Fenster.
3. Alternativ `start.bat` doppelklicken oder im Ordner `py -3 "%CD%\server.py"` ausführen. `dashboard.html` nicht direkt öffnen, da das Dashboard das Backend benötigt.
4. Bei direktem Start über `start.bat` oder die Kommandozeile den Server im Serverfenster mit `Strg+C` beenden.
5. Wenn Port 8765 bereits belegt ist, wird automatisch ein freier Folgeport gesucht; die verwendete Adresse erscheint im Serverfenster.

Die Anwendung bindet ausschließlich an `127.0.0.1`. Die Datenbank wird im Ordner `data` neben `server.py` gespeichert.

## Dashboard

Im Dateiimport-Widget lassen sich mehrere JSON-, JSONL/NDJSON- oder CSV-Dateien auswählen oder per Drag-and-drop ablegen. Pro Datei gilt ein Upload-Limit von 300 MB. In der Bearbeitungsansicht von „Widgets“ lassen sich Widgets mit Richtungspfeilen neu anordnen, mit einem Button ein- oder ausblenden und zwischen halber und voller Breite wechseln. Überschrift, Hintergrundbild und Filter bleiben fest. „Weitere Optionen“ enthält nur Abstand, Kennzahlen pro Zeile sowie Beiträge pro Seite. Die Einstellungen werden im lokalen Browserspeicher gesichert; „Standard wiederherstellen“ setzt sie zurück.

Über „Projekt hinzufügen“ kannst du mehrere noch nicht zugeordnete Datensätze mit einem neuen Projekt verknüpfen, neue Dateien direkt dafür hochladen und beliebig viele HTTPS-Referenzlinks hinterlegen. Mit „Projekt bearbeiten“ kannst du die Links eines bestehenden Projekts ergänzen, entfernen und speichern sowie ihm mehrere noch freie Datensätze hinzufügen. Projektlinks werden dauerhaft in der lokalen SQLite-Datenbank gespeichert und bleiben nach einem Serverneustart erhalten. Die Referenzlinks dienen ausschließlich zur Einordnung des Kommentartextes. Beiträge werden sofort geladen; die Quellen-Einordnung läuft im Hintergrund und wird automatisch nachgeladen. Jede Aussage wird unter dem Beitrag einzeln als durch Quellen gestützt, widersprochen oder nicht bewertbar angezeigt; Sentiment-Tags und Originalkommentare bleiben unverändert. Jeder Datensatz gehört höchstens einem Projekt; neben den Datensätzen wird die Projektzugehörigkeit angezeigt. Bei älteren oder importierten Daten wird eine Datensatzzuordnung automatisch erkannt, wenn alle vorhandenen Projektangaben seiner Beiträge zum selben bestehenden Projekt passen; diese Angaben werden dabei nicht verändert. Neue Projekte stehen danach auch in Filtern und Vergleichen zur Verfügung. Die Analyse bietet Projektfilter und Projektvergleiche; Stadtfilter und Stadtzuordnungen sind nicht mehr Teil der Oberfläche.

Für die Quellen-Einordnung einmalig die ML-Abhängigkeit installieren und das Modell lokal herunterladen:

```powershell
py -3 -m pip install -r requirements-ml.txt
py -3 -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2')"
py -3 -c "from transformers import AutoModelForSequenceClassification, AutoTokenizer; name='MoritzLaurer/multilingual-MiniLMv2-L6-mnli-xnli'; AutoTokenizer.from_pretrained(name); AutoModelForSequenceClassification.from_pretrained(name)"
```

Die Quellen werden nicht an einen externen KI-Dienst gesendet; semantische Suche und NLI-Einordnung laufen lokal. Für jede Aussage werden relevante Nachbarsätze gemeinsam mit der Fundstelle bewertet, damit Einschränkungen und Zusammenhänge erhalten bleiben. Das NLI-Modell unterscheidet stützende, widersprechende und neutrale Belege; neutrale Fundstellen gelten nicht als Widerlegung, und widersprüchliche Quellen ergeben kein eindeutiges Urteil. Die bestehenden Prüfungen für Zahlen und Messwerte bleiben als zusätzliche Schutzregeln aktiv. Wenn eine Abhängigkeit, ein lokales Modell oder eine Referenzquelle fehlt, wird der konkrete Fehler unter dem Beitrag angezeigt; fehlende Referenzlinks lassen sich über „Projekt bearbeiten“ ergänzen.

Die Analysefilter schränken Kennzahlen, Zeitreihe und Themenmuster nach Datensatz, Thema, Projekt, Sentiment und Zeitraum ein. Der Zeitraum wird über zwei Griffe auf dem Zeitstrahl gewählt; „Gesamter Zeitraum“ schließt auch undatierte Beiträge ein. Die Medienbeiträge haben zusätzlich eigene Such-, Medientyp-, Sentiment- und Sortierfilter, die nur die Beitragsliste und den CSV-Export einschränken. Mehrere Projekte lassen sich in der Vergleichsansicht gegenüberstellen. Wenn Projektangaben fehlen, einen einzelnen Datensatz auswählen und über „Projektzuordnung ergänzen“ fehlende Beitragsangaben ergänzen; vorhandene Werte werden nicht überschrieben. Stimmt der eingegebene Name mit einem bestehenden Projekt überein, wird auch der Datensatz diesem Projekt zugeordnet und die Datensatzliste aktualisiert. Einen neuen Projektnamen zunächst über „Projekt hinzufügen“ anlegen. Es werden zunächst fünf Medienbeiträge angezeigt; über „Weitere Beiträge anzeigen“ lädst du die nächsten fünf. Die Zeitreihe zeigt positive und negative Beiträge je nach gewähltem Zeitraum. Häufige Begriffe werden aus den bis zu 2.500 neuesten positiven und negativen Beiträgen ermittelt, damit das Dashboard auch bei großen Datensätzen zügig reagiert; sie sind keine automatisch geprüften Argumente. Eine Prognose ist im Entwurf als späterer Ausbauschritt notiert und wird noch nicht als Vorhersage ausgegeben.

## Webquellen

Die Verwaltung von Webquellen ist nicht mehr in der Dashboard-Oberfläche enthalten. Bereits gespeicherte, geplante Quellen können bei laufendem Backend weiterhin im Hintergrund abgerufen werden.

## Vorhandene Sentiment-Tags

Beim Import übernimmt das Dashboard die vorhandenen `sentiment`-Tags (zum Beispiel `positiv`, `neutral`, `negativ` oder `1`, `0`, `-1`); der Beitragstext wird nicht zur Sentiment-Einordnung ausgewertet. Fehlende Tags bleiben nicht klassifiziert. Ein mitgeliefertes `reason`-Feld wird klein beim Beitrag angezeigt und auch in den CSV-Export übernommen. Häufige Begriffe sind weiterhin Textmuster und keine geprüften Argumente.

## Tests

Mit installierter Python-Umgebung: `py -3 -m unittest -v`.

## Featureübersicht

Die nach Wichtigkeit sortierte Funktionsliste steht in `FEATURES.txt`. Bei jedem
neuen oder wesentlich geänderten Website-Feature ist sie im selben
Änderungsschritt zu aktualisieren und bei Bedarf neu zu priorisieren.
