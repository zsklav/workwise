"""Compare index plans on temporary tables; never mutate application tables."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import platform
from statistics import median

from sqlalchemy import create_engine, text


def nodes(plan):
    yield plan
    for child in plan.get("Plans", []):
        yield from nodes(child)


def summarize(samples):
    plan = samples[-1]["Plan"]
    return {
        "median_execution_ms": median(sample["Execution Time"] for sample in samples),
        "rows_returned": plan["Actual Rows"],
        "buffer_accesses": sum(plan.get(key, 0) for key in ("Shared Hit Blocks", "Shared Read Blocks", "Local Hit Blocks", "Local Read Blocks")),
        "rows_removed_by_filter": sum(node.get("Rows Removed by Filter", 0) * node.get("Actual Loops", 1) for node in nodes(plan)),
        "node_types": sorted({node["Node Type"] for node in nodes(plan)}),
        "indexes": sorted({node["Index Name"] for node in nodes(plan) if "Index Name" in node}),
    }


def profile(connection, *, rows=50000, companies=100, repeats=7):
    if not 1000 <= rows <= 1000000 or not 2 <= companies <= min(rows // 20, 1000) or not 1 <= repeats <= 20:
        raise ValueError("Use 1000..1000000 jobs, 2..1000 companies with >=20 jobs each, and 1..20 repeats")
    connection.execute(text("SET LOCAL statement_timeout = '30s'"))
    connection.execute(text("CREATE TEMP TABLE ww_perf_companies (id integer PRIMARY KEY, name varchar(200) NOT NULL, description text, owner_id integer, version integer NOT NULL DEFAULT 1) ON COMMIT DROP"))
    connection.execute(text("CREATE TEMP TABLE ww_perf_jobs (id integer PRIMARY KEY, title text NOT NULL, company_id integer NOT NULL, location text NOT NULL, description text, version integer NOT NULL DEFAULT 1) ON COMMIT DROP"))
    connection.execute(text("CREATE INDEX ww_perf_jobs_company ON ww_perf_jobs(company_id)"))
    connection.execute(text("INSERT INTO ww_perf_companies(id,name) SELECT i, 'Fixture company '||i FROM generate_series(1,:companies) i"), {"companies": companies})
    connection.execute(text("INSERT INTO ww_perf_jobs(id,title,company_id,location,description) SELECT i, 'Fixture job '||i, ((i-1)%:companies)+1, 'Remote', repeat('x',120) FROM generate_series(1,:rows) i"), {"rows":rows,"companies":companies})
    connection.execute(text("ANALYZE ww_perf_jobs"))
    connection.execute(text("ANALYZE ww_perf_companies"))
    select = """SELECT j.id,j.title,j.company_id,j.location,j.description,j.version,
        c.id AS employer_id,c.name,c.description AS company_description,c.owner_id,c.version AS company_version
        FROM ww_perf_jobs j LEFT JOIN ww_perf_companies c ON c.id=j.company_id """
    cases = {
        "company_first_page": (select + "WHERE j.company_id=:company ORDER BY j.id LIMIT 20", {"company":companies}),
        "company_deep_offset": (select + "WHERE j.company_id=:company ORDER BY j.id LIMIT 20 OFFSET :offset", {"company":companies,"offset":max(0,rows//companies-20)}),
        "unfiltered_deep_offset": (select + "ORDER BY j.id LIMIT 20 OFFSET :offset", {"offset":rows-20}),
    }
    report = {"created_at":datetime.now(timezone.utc).isoformat(), "postgresql":connection.scalar(text("SELECT version()")),
              "python":platform.python_version(), "fixture":{"jobs":rows,"companies":companies,"description_bytes":120},
              "repeats":repeats, "measurement":"EXPLAIN ANALYZE, BUFFERS, TIMING OFF; warmed cache; temporary tables", "phases":{}}
    for phase in ("single_column", "composite"):
        if phase == "composite":
            connection.execute(text("CREATE INDEX ww_perf_jobs_company_order ON ww_perf_jobs(company_id,id)"))
            connection.execute(text("DROP INDEX ww_perf_jobs_company"))
            connection.execute(text("ANALYZE ww_perf_jobs"))
        measured = {}
        for name,(query,params) in cases.items():
            connection.execute(text(query),params).all()  # Warm up outside measurement.
            samples = [connection.scalar(text("EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, FORMAT JSON) "+query),params)[0] for _ in range(repeats)]
            measured[name] = {"summary":summarize(samples), "last_plan":samples[-1]}
        index = "ww_perf_jobs_company" if phase == "single_column" else "ww_perf_jobs_company_order"
        report["phases"][phase] = {"index_bytes":connection.scalar(text("SELECT pg_relation_size(CAST(:name AS regclass))"),{"name":index}),"queries":measured}
    return report


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rows",type=int,default=50000)
    parser.add_argument("--companies",type=int,default=100)
    parser.add_argument("--repeats",type=int,default=7)
    parser.add_argument("--output",type=Path,default=Path("performance-results/query-plan.json"))
    args=parser.parse_args()
    from workwise.db.session import database_url
    database=create_engine(database_url.set(database="workwise_test"),connect_args={"connect_timeout":5})
    try:
        with database.begin() as connection:
            report=profile(connection,rows=args.rows,companies=args.companies,repeats=args.repeats)
        args.output.parent.mkdir(parents=True,exist_ok=True)
        args.output.write_text(json.dumps(report,indent=2)+"\n",encoding="utf-8")
        for phase,data in report["phases"].items():
            print(phase,json.dumps({name:row["summary"] for name,row in data["queries"].items()}))
        print("Query-plan report written to",args.output)
    finally:
        database.dispose()


if __name__ == "__main__":
    main()
