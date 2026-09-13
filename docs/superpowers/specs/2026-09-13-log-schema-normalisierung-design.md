# Log-Schema-Normalisierung — Design

Datum: 2026-09-13
Status: Entwurf, wartet auf Freigabe
Branch: `feat/proxmox-lxc`

## Ziel

Die Zeilengröße der Tabelle `logs` von gemessenen 745 Byte (473 Byte Heap plus
282 Byte Indexanteil) auf etwa 210 Byte senken, ohne die API-Antwort zu
verändern. Die Partitionierung und das Flow-Rollup bleiben ausdrücklich außen
vor — dieser Schritt schafft nur die Grundlage.

## Messgrundlage

Container 206, 13.09.2026, 4,5 Mio Zeilen, 61 Zeilen/s Ingest.

| Bereich | Byte/Zeile | Anteil |
|---|---|---|
| `raw_log` | 262 | 55 % |
| `rule_name` + `rule_desc` | 53 | 11 % |
| Tupel-Overhead, Padding, Rest | 55 | 12 % |
| kurze Strings (6 Spalten) | 34 | 7 % |
| Kernfelder | 38 | 8 % |
| Gerätenamen + `hostname` | 25 | 5 % |
| `created_at` | 8 | 2 % |
| `service_name` | 6 | 1 % |
| Geo, ASN, rDNS | 5 | 1 % |
| Abuse- und Threat-Block | 1 | 0 % |

Kardinalitäten über den gesamten Bestand:

| Spalte | distinkt | NULL-Anteil |
|---|---|---|
| `rule_name` | 41 | 3 % |
| `rule_desc` | 48 | 1 % |
| `interface_in` | 7 | 86 % |
| `interface_out` | 8 | 9 % |
| `direction` | 5 | 3 % |
| `protocol` | 4 | 9 % |
| `src_device_name` | 27 | 87 % |
| `dst_device_name` | 19 | 21 % |
| `hostname` | 7 | ~100 % |
| `service_name` | 1 964 | 11 % |

## Zwei Befunde, die Entscheidungen tragen

**Der Parser ist fehlerfrei.** In der Stichprobe einer Stunde (219 675 Zeilen)
gab es keine einzige Firewall-Zeile ohne Quell-IP, ohne Zielport oder ohne
Regelnamen. `raw_log` enthält damit nichts, was nicht in den geparsten Spalten
steht.

**Die Denormalisierung der IP-Anreicherung kostet nichts.** Geo, ASN, rDNS und
der achtspaltige Abuse-Block schlagen zusammen mit 6 Byte zu Buche, weil sie auf
96 % der Zeilen `NULL` sind — Anreicherung läuft nur für öffentliche IPs und
blockierten Verkehr. Diese Spalten bleiben, wie sie sind. Eine Normalisierung
nach `ip_threats` würde Aufwand erzeugen und nichts sparen.

## Zielschema

Drei neue Nachschlagetabellen. Alle Schlüssel sind `SMALLINT`, weil keine
Kardinalität über 50 liegt.

```sql
CREATE TABLE rules (
    id        SMALLINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name      VARCHAR(100) NOT NULL,
    descr     VARCHAR(255),
    UNIQUE (name, descr)
);

CREATE TABLE interfaces (
    id        SMALLINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name      VARCHAR(20) NOT NULL UNIQUE
);

CREATE TABLE device_names (
    id        SMALLINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name      TEXT NOT NULL UNIQUE
);
```

`log_type`, `rule_action`, `direction` und `protocol` bekommen **keine** Tabelle.
Ihre Wertemengen sind im Code festgelegt (5, 3, 5 und 4 Werte) und ändern sich
nur mit einem Release. Sie werden zu `SMALLINT` mit Konstanten in Python — das
spart den Join und macht Gleichheitsfilter schneller als auf `varchar`.

Für `protocol` wird die IANA-Protokollnummer verwendet (6 = TCP, 17 = UDP,
1 = ICMP), nicht eine eigene Nummerierung: sie ist stabil, standardisiert und
steht ohnehin in der Syslog-Zeile.

Die Spaltenreihenfolge in `logs` wird nach Alignment sortiert — 8-Byte-Typen
zuerst, dann 4, dann 2, dann 1, variable Länge zuletzt. Das ist bei vierzig
Spalten zweistellig viel und kostet nichts, weil die Tabelle ohnehin neu
angelegt wird.

### Änderungen an `logs`

| Spalte | vorher | nachher | Byte |
|---|---|---|---|
| `raw_log` | `TEXT NOT NULL` | `TEXT NULL`, nur bei Parse-Fehler gefüllt | −262 |
| `rule_name`, `rule_desc` | `VARCHAR(100)`, `VARCHAR(255)` | `rule_id SMALLINT` → `rules` | −51 |
| `interface_in`, `interface_out` | `VARCHAR(20)` | `SMALLINT` → `interfaces` | −14 |
| `src_device_name`, `dst_device_name` | `TEXT` | `SMALLINT` → `device_names` | −21 |
| `log_type` | `VARCHAR(20)` | `SMALLINT` | −5 |
| `rule_action` | `VARCHAR(20)` | `SMALLINT` | −5 |
| `direction` | `VARCHAR(20)` | `SMALLINT` | −6 |
| `protocol` | `VARCHAR(10)` | `SMALLINT` (IANA-Nummer) | −3 |
| `hostname` | `VARCHAR(255)` | `SMALLINT` → `device_names` | −3 |
| `created_at` | `TIMESTAMPTZ` | entfällt | −8 |
| `service_name` | `TEXT` | entfällt, wird berechnet | −6 |
| Spaltenreihenfolge | gewachsen | nach Alignment | ~−10 |

Erwartetes Ergebnis: **473 → etwa 105 Byte Heap**, Faktor 4,5.

### `service_name` entfällt ersatzlos

Der Wert ist eine reine Funktion aus `dst_port` und `protocol`, nachgeschlagen
in der IANA-Tabelle, die `services.py` ohnehin vollständig im Speicher hält.
Gespeichert wird er trotzdem in jeder Zeile, mit eigenem Backfill-Job
(`db.py:1651`) und zwei Indizes.

Statt zu speichern wird beim Serialisieren nachgeschlagen. Der Filter
`service=https` wird zur Portmenge aufgelöst und als `dst_port IN (...)`
ausgeführt — das trifft den vorhandenen Port-Index und ist schneller als der
Textvergleich heute. Für `group_by=service` im Aggregat wird nach der
Gruppierung über `(dst_port, protocol)` abgebildet.

Entfallen damit: die Spalte, der Backfill-Job, `idx_logs_service_name` (30 MB)
und `idx_logs_fw_service_name_null_id`.

### Index-Diät

Gemessene Nutzung nach einem Tag Betrieb:

| Index | Scans | Größe | Entscheidung |
|---|---|---|---|
| `idx_logs_type_id` | 1 | 319 MB | streichen |
| `idx_logs_spgist_dst_ip_firewall` | 19 | 289 MB | **behalten, prüfen** |
| `idx_logs_src_port` | 0 | 31 MB | streichen |
| `idx_logs_protocol` | 0 | 29 MB | streichen |
| `idx_logs_service_name` | 6 | 30 MB | entfällt mit der Spalte |
| `idx_logs_flow_agg` | 10 | 100 MB | **behalten** |
| alle übrigen | > 40 | — | behalten |

Die Zähler decken nur einen Tag ab, und Threat-Map, Flow-Ansicht, CSV-Export und
MCP waren in dieser Zeit nicht in Gebrauch. `idx_logs_spgist_dst_ip_firewall`
bedient die CIDR-Gruppierung im Aggregat, `idx_logs_flow_agg` den Sankey — beide
bleiben trotz niedriger Zähler.

`idx_logs_type_id`, `idx_logs_src_port` und `idx_logs_protocol` werden
gestrichen. Zusammen 379 MB und drei Indizes weniger Schreiblast pro INSERT.

`idx_logs_type_id` bedient laut Kommentar in `init.sql:100` die typweise
Löschaktion aus der Oberfläche (`routes/setup.py:910`) sowie die `COUNT`- und
`MAX`-Schnappschüsse daneben. Das ist eine Admin-Aktion, kein heißer Pfad — der
einzige gezählte Scan stammt vermutlich aus der Einrichtung. Ohne den Index
wird sie zu einem sequenziellen Scan über die Zeilen des betroffenen Typs;
dafür entfällt die Pflege bei jedem der 61 INSERTs pro Sekunde.

`idx_logs_protocol` ist bei vier verschiedenen Werten ohne Selektivitätsgewinn,
und nach der Umstellung auf `SMALLINT` erst recht: ein Scan über die neueren
Zeilen ist billiger als der Indexzugriff.

Nach der Umtypisierung schrumpfen die verbleibenden Indizes zusätzlich, weil
`smallint`-Schlüssel schmaler sind als `varchar`.

## Die API-Antwort bleibt unverändert

Das ist die tragende Einschränkung des Entwurfs. `routes/logs.py` löst die
Fremdschlüssel beim Serialisieren wieder auf und liefert dieselben Feldnamen mit
denselben Werten wie heute. Das Frontend wird nicht angefasst, und die Änderung
endet an der Backend-Grenze.

Die Auflösung kostet nichts Nennenswertes: alle drei Nachschlagetabellen
zusammen haben unter hundert Zeilen und werden beim Start in ein Dictionary
geladen, mit Invalidierung über das vorhandene `SIGUSR2`-Signal.

Für Filter gilt dasselbe rückwärts: `rule_name=LAN-to-WAN` wird vor dem Bauen
der `WHERE`-Klausel zu `rule_id IN (...)` aufgelöst. Wildcards und Negation
bleiben möglich, weil die Auflösung im Python-Dictionary stattfindet und nicht
in SQL.

## Migrationspfad

Vierzig Spalten umzutypisieren bedeutet vierzig Tabellen-Rewrites. Stattdessen:

1. Nachschlagetabellen anlegen und aus dem Bestand füllen
   (`INSERT INTO rules SELECT DISTINCT rule_name, rule_desc FROM logs`).
2. `logs_v2` mit dem Zielschema anlegen.
3. Bestand in Blöcken kopieren und dabei übersetzen. Bei 4,5 Mio Zeilen und
   Blöcken zu 100 000 dauert das wenige Minuten.
4. Receiver anhalten, Restblock kopieren, `logs` → `logs_old`,
   `logs_v2` → `logs`, Receiver starten. Ausfall unter einer Minute.
5. `logs_old` nach einer Bestätigungsphase löschen.

Der Kopierlauf braucht kurzzeitig Platz für beide Tabellen. Bei 3,3 GB Bestand
und einer Zieltabelle von etwa 0,9 GB sind das 4,2 GB — auf der 16-GB-Platte
machbar, aber die Aufbewahrung sollte vorher gesenkt werden.

**Alternative:** sauberer Schnitt ohne Migration. Der Bestand ist einen Tag alt.
Wenn dir die 4,5 Mio Zeilen nichts wert sind, entfallen Schritt 1, 3 und 5, und
der Umbau wird zu einer Schema-Neuanlage.

## Betroffene Stellen

| Datei | Änderung |
|---|---|
| `init.sql` | Zielschema, Nachschlagetabellen, Index-Diät |
| `receiver/db.py` | Insert-Pfad, Nachschlage-Cache, Backfill-Updates |
| `receiver/parsers.py` | liefert IDs statt Text |
| `receiver/query_helpers.py` | Filter lösen Text zu IDs auf |
| `receiver/routes/logs.py` | Serialisierung löst IDs zu Text auf |
| `receiver/routes/stats.py`, `flows.py` | Aggregate über IDs |
| `receiver/services.py` | liefert `service_name` zur Laufzeit |
| `receiver/backfill.py` | Service-Backfill entfällt |
| `ui/` | **keine Änderung** |

## Erwartetes Ergebnis

| | vorher | nachher | Faktor |
|---|---|---|---|
| Heap je Zeile | 473 B | ~105 B | 4,5× |
| Index je Zeile | 282 B | ~105 B | 2,7× |
| **gesamt je Zeile** | **745 B** | **~210 B** | **3,5×** |
| pro Tag bei 61/s | 3,9 GB | 1,1 GB | 3,5× |

Das allein macht sechzig Tage Aufbewahrung noch nicht möglich — dafür braucht es
weiterhin das Flow-Rollup. Es macht aber die Rohschicht bezahlbar, und das ist
der Posten, den das Rollup nicht anfassen kann.

## Offene Punkte

1. **Migration oder sauberer Schnitt?** Der Bestand ist einen Tag alt.
2. **`service_name` wirklich streichen?** Spart wenig Speicher, entfernt aber
   einen Backfill-Job und zwei Indizes. Das Risiko liegt in `group_by=service`
   im Aggregat.
3. **Aufbewahrung vor der Migration senken.** Der Kopierlauf braucht kurzzeitig
   Platz für Quell- und Zieltabelle. Bei heutigem Bestand unkritisch, bei
   gewachsenem nicht mehr.
