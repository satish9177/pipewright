"""
Print recent Pipewright pipeline runs.

Safe operator helper: does not print secrets.
"""

import argparse
import sys
from pathlib import Path

from sqlalchemy import text

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.db.database import engine, init_db


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="List recent Pipewright runs")
    parser.add_argument("--status", help="Filter by pipeline_runs.status")
    parser.add_argument("--project-id", help="Filter by project_id")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    init_db()

    filters = []
    params = {}
    if args.status:
        filters.append("status = :status")
        params["status"] = args.status
    if args.project_id:
        filters.append("project_id = :project_id")
        params["project_id"] = args.project_id

    where_clause = f"WHERE {' AND '.join(filters)}" if filters else ""
    query = text(f"""
        SELECT
            id,
            project_id,
            status,
            chunk_plan_status,
            total_chunks,
            current_chunk_number,
            branch_name,
            pr_url,
            created_at
        FROM pipeline_runs
        {where_clause}
        ORDER BY created_at DESC
        LIMIT 20
    """)

    with engine.connect() as conn:
        rows = conn.execute(query, params).fetchall()

    if not rows:
        print("No runs found.")
        return 0

    for row in rows:
        data = dict(row._mapping)
        print(
            f"id={data.get('id')} | "
            f"project_id={data.get('project_id')} | "
            f"status={data.get('status')} | "
            f"chunk_plan_status={data.get('chunk_plan_status')} | "
            f"total_chunks={data.get('total_chunks')} | "
            f"current_chunk_number={data.get('current_chunk_number')} | "
            f"branch_name={data.get('branch_name')} | "
            f"pr_url={data.get('pr_url')} | "
            f"created_at={data.get('created_at')}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
