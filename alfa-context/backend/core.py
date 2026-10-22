"""Company context foundation. Python standard library only; no LLM calls."""
import argparse
import csv
import json
import sqlite3
from collections import defaultdict
from decimal import Decimal, InvalidOperation
from pathlib import Path


def read_csv(path):
    with open(path, encoding="utf-8-sig", newline="") as stream:
        sample = stream.read(16384)
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
        stream.seek(0)
        yield from csv.DictReader(stream, dialect=dialect)


def dump(value):
    return json.dumps(value, ensure_ascii=False, indent=2)


def connect(path):
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    return db


def ingest(db, directory):
    """Atomic replacement, retaining raw conflicting company rows and payment rows."""
    directory = Path(directory)
    company_path = directory / "df_Uid_data_hack.csv"
    payment_path = directory / "df_Uid_payment_hack.csv"
    links = list(directory.glob("df_Fid_Uid_link_hack*.csv"))
    if len(links) != 1:
        raise ValueError("Expected exactly one df_Fid_Uid_link_hack*.csv")
    db.executescript('''
        CREATE TABLE IF NOT EXISTS companies(id TEXT PRIMARY KEY, profile TEXT, conflicts TEXT);
        CREATE TABLE IF NOT EXISTS company_rows(id TEXT, source_row INTEGER, body TEXT);
        CREATE TABLE IF NOT EXISTS payments(payer TEXT, beneficiary TEXT, amount TEXT, count INTEGER);
        CREATE TABLE IF NOT EXISTS people(person TEXT, company TEXT);
        CREATE INDEX IF NOT EXISTS payer_idx ON payments(payer);
        CREATE INDEX IF NOT EXISTS beneficiary_idx ON payments(beneficiary);
        CREATE INDEX IF NOT EXISTS people_company_idx ON people(company);
        CREATE INDEX IF NOT EXISTS people_person_idx ON people(person);
        CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY, value TEXT);
    ''')
    with db:
        for table in ("companies", "company_rows", "payments", "people", "metadata"):
            db.execute(f"DELETE FROM {table}")
        records = defaultdict(list)
        for row_number, row in enumerate(read_csv(company_path), 2):
            uid = row["SYNTHETIC_U"]
            if not uid:
                raise ValueError(f"Missing company ID at line {row_number}")
            records[uid].append(row)
            db.execute("INSERT INTO company_rows VALUES(?,?,?)", (uid, row_number, dump(row)))
        conflict_count = 0
        for uid, versions in records.items():
            conflicts = {key: sorted({v[key] for v in versions}) for key in versions[0]
                         if len({v[key] for v in versions}) > 1}
            profile = {key: (None if key in conflicts or versions[0][key] == "" else versions[0][key])
                       for key in versions[0]}
            conflict_count += bool(conflicts)
            db.execute("INSERT INTO companies VALUES(?,?,?)", (uid, dump(profile), dump(conflicts)))
        counts = defaultdict(int)
        def payment_rows():
            for line, row in enumerate(read_csv(payment_path), 2):
                try:
                    amount = Decimal(row["PAY_AMT"])
                    count_decimal = Decimal(row["PAY_CNT"])
                    if not amount.is_finite() or amount < 0 or not count_decimal.is_finite():
                        raise ValueError()
                    if count_decimal < 0 or count_decimal != count_decimal.to_integral_value():
                        raise ValueError()
                    count = int(count_decimal)
                except (InvalidOperation, ValueError):
                    raise ValueError(f"Invalid payment at CSV line {line}") from None
                if not row["PAYER"] or not row["BENEFICIARY"]:
                    raise ValueError(f"Missing endpoint at CSV line {line}")
                counts["payment_rows"] += 1
                counts["missing_payer_profile_rows"] += row["PAYER"] not in records
                counts["missing_beneficiary_profile_rows"] += row["BENEFICIARY"] not in records
                counts["self_payment_rows"] += row["PAYER"] == row["BENEFICIARY"]
                yield row["PAYER"], row["BENEFICIARY"], str(amount), count
        db.executemany("INSERT INTO payments VALUES(?,?,?,?)", payment_rows())
        for row in read_csv(links[0]):
            db.execute("INSERT INTO people VALUES(?,?)", (row["SYNTHETIC_F"], row["SYNTHETIC_U"]))
            counts["person_link_rows"] += 1
        counts["company_rows"] = sum(map(len, records.values()))
        counts["unique_companies"] = len(records)
        counts["companies_with_conflicting_profiles"] = conflict_count
        counts["repeated_payment_pairs"] = db.execute('''SELECT COALESCE(SUM(n-1),0) FROM
            (SELECT COUNT(*) n FROM payments GROUP BY payer,beneficiary HAVING COUNT(*)>1)''').fetchone()[0]
        db.execute("INSERT INTO metadata VALUES('quality',?)", (dump(dict(counts)),))
    return dict(counts)


def profile(db, uid):
    record = db.execute("SELECT * FROM companies WHERE id=?", (uid,)).fetchone()
    if not record:
        raise ValueError("Company profile not found")
    company = json.loads(record["profile"])
    result = {"company_id": uid, "features": company,
              "conflicting_fields": json.loads(record["conflicts"]),
              "source": "df_Uid_data_hack.csv", "payments": {}}
    for direction, endpoint, counterparty in (("incoming", "beneficiary", "payer"),
                                               ("outgoing", "payer", "beneficiary")):
        edges = defaultdict(lambda: [Decimal(0), 0, 0])
        for row in db.execute(f"SELECT * FROM payments WHERE {endpoint}=? AND payer<>beneficiary", (uid,)):
            edge = edges[row[counterparty]]
            edge[0] += Decimal(row["amount"])
            edge[1] += row["count"]
            edge[2] += 1
        total = sum((e[0] for e in edges.values()), Decimal(0))
        known = Decimal(0)
        sectors = defaultdict(Decimal)
        ranked = []
        for other, (amount, count, source_rows) in edges.items():
            record = db.execute("SELECT profile FROM companies WHERE id=?", (other,)).fetchone()
            other_profile = json.loads(record[0]) if record else {}
            sector = other_profile.get("MAINOKVED_NAME")
            if sector:
                known += amount
                sectors[sector] += amount
            ranked.append({"company_id": other, "amount": str(amount), "payment_count": count,
                           "source_rows": source_rows, "sector": sector,
                           "share": float(amount / total) if total else None})
        ranked.sort(key=lambda e: Decimal(e["amount"]), reverse=True)
        result["payments"][direction] = {
            "total_amount": str(total), "counterparty_count": len(edges),
            "sector_coverage": float(known / total) if total else None,
            "top_counterparties": ranked[:10],
            "sector_shares": [{"sector": s, "share": float(a / total)}
                              for s, a in sorted(sectors.items(), key=lambda v: v[1], reverse=True)] if total else [],
            "top_5_concentration": float(sum((Decimal(e["amount"]) for e in ranked[:5]), Decimal(0)) / total) if total else None,
        }
    result["shared_person_companies"] = [r[0] for r in db.execute('''
        SELECT DISTINCT b.company FROM people a JOIN people b ON a.person=b.person
        WHERE a.company=? AND b.company<>? ORDER BY b.company''', (uid, uid))]
    group = company.get("GROUP_ID")
    result["group_id"] = group
    result["group_members"] = [r[0] for r in db.execute(
        "SELECT id FROM companies WHERE json_extract(profile,'$.GROUP_ID')=? AND id<>? ORDER BY id", (group, uid))] if group else []
    result["limitations"] = [
        "Payment dates, currency, period and purpose are not specified in supplied CSV.",
        "Amounts retain source units; do not label as rubles without confirmation.",
        "Shares apply to supplied payments only, not all company revenue or expenses.",
        "Self-payments excluded; repeated endpoint pairs retained and aggregated, not deduplicated.",
        "Shared person does not establish ownership, control or supply-chain dependency.",
        "Missing or conflicting company features remain unknown.",
    ]
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="context.sqlite")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("import").add_argument("directory")
    commands.add_parser("profile").add_argument("company")
    commands.add_parser("quality")
    args = parser.parse_args()
    with connect(args.db) as db:
        if args.command == "import":
            output = ingest(db, args.directory)
        elif args.command == "profile":
            output = profile(db, args.company)
        else:
            output = json.loads(db.execute("SELECT value FROM metadata WHERE key='quality'").fetchone()[0])
    print(dump(output))


if __name__ == "__main__":
    main()
