"""Data Explorer routes — schema introspection and paginated table reads."""

from fastapi import APIRouter, Depends
from sqlalchemy import inspect, text
from sqlalchemy.orm import Session

from database import get_db

router = APIRouter(prefix="/api/explorer", tags=["explorer"])


@router.get("/schema")
def get_schema(db: Session = Depends(get_db)):
    inspector = inspect(db.bind)
    tables = {}
    for table_name in inspector.get_table_names():
        columns = []
        for col in inspector.get_columns(table_name):
            columns.append({
                "name": col["name"],
                "type": str(col["type"]),
                "nullable": col.get("nullable", True),
                "primary_key": col.get("autoincrement", False) or col["name"] == "id",
            })
        tables[table_name] = columns
    return {"tables": tables}


@router.get("/tables/{table_name}")
def get_table_data(
    table_name: str,
    page: int = 1,
    per_page: int = 50,
    db: Session = Depends(get_db),
):
    # Validate table exists
    inspector = inspect(db.bind)
    valid_tables = inspector.get_table_names()
    if table_name not in valid_tables:
        return {"error": f"Table '{table_name}' not found"}

    offset = (page - 1) * per_page

    # Get total count
    count_result = db.execute(text(f"SELECT COUNT(*) FROM [{table_name}]"))
    total = count_result.scalar()

    # Get rows
    result = db.execute(
        text(f"SELECT * FROM [{table_name}] LIMIT :limit OFFSET :offset"),
        {"limit": per_page, "offset": offset},
    )
    columns = list(result.keys())
    rows = [dict(zip(columns, row)) for row in result.fetchall()]

    # Serialize datetime objects
    for row in rows:
        for key, val in row.items():
            if hasattr(val, "isoformat"):
                row[key] = val.isoformat()

    return {
        "table": table_name,
        "columns": columns,
        "rows": rows,
        "total": total,
        "page": page,
        "per_page": per_page,
        "total_pages": (total + per_page - 1) // per_page if total else 0,
    }


@router.get("/stats")
def get_stats(db: Session = Depends(get_db)):
    inspector = inspect(db.bind)
    stats = {}
    for table_name in inspector.get_table_names():
        result = db.execute(text(f"SELECT COUNT(*) FROM [{table_name}]"))
        stats[table_name] = result.scalar()
    return {"stats": stats}
