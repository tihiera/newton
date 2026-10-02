"""An experiment's report as one zip: report.md, report.json and the report's files
(plots and the like), with the same paths GET /experiments/{id}/report/files serves."""

from __future__ import annotations

import io
import json
import re
import zipfile
from pathlib import Path

from ..errors import Conflict, NotFound
from ..storage.db import Database, Row, loads
from ..storage.files import UnsafeArchive, resolve_inside

OWN = ("report.md", "report.json")  # written from the stored report, not copied


def export(db: Database, reports_dir: Path, exp_id: str) -> tuple[str, bytes]:
    """(file name, zip) of a reported experiment."""
    row = db.query_one("SELECT * FROM experiments WHERE id = ?", (exp_id,))
    if row is None:
        raise NotFound(f"no experiment {exp_id}")
    if row["state"] != "reported" or not row["report_path"]:
        raise Conflict("the experiment has no report yet")
    return export_name(row), export_zip(row, reports_dir / exp_id)


def export_name(row: Row) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", str(row["title"] or "").lower()).strip("-")[:60]
    return f"{slug.strip('-') or 'experiment'}-{row['id']}.zip"


def export_zip(row: Row, report_dir: Path) -> bytes:
    """The reported experiment `row`'s report; `report_dir` is where its files live."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("report.md", Path(row["report_path"]).read_text(encoding="utf-8"))
        zf.writestr("report.json", json.dumps(loads(row["evaluation"]), indent=2))
        if report_dir.is_dir():
            for path in sorted(report_dir.rglob("*")):
                rel = path.relative_to(report_dir).as_posix()
                if rel in OWN or path.is_symlink() or not path.is_file():
                    continue
                try:
                    target = resolve_inside(report_dir, rel)  # nothing from outside
                except UnsafeArchive:
                    continue
                zf.write(target, rel)
    return buf.getvalue()
