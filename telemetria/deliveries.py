"""Entregas únicas selecionadas pelo primeiro recebimento na central."""
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs

from . import availability, timeline
from .communication import stamp


def query_params(query):
    fields = parse_qs(query, keep_blank_values=True, strict_parsing=True, max_num_fields=2)
    if set(fields) == {"periodo"} and fields["periodo"] in (["1h"], ["24h"]):
        return {"hours": int(fields["periodo"][0][:-1])}
    if set(fields) == {"inicio", "fim"} and all(len(v) == 1 for v in fields.values()):
        return {"start": fields["inicio"][0], "end": fields["fim"][0]}
    raise ValueError("Informe um período de recebimento válido.")


def capture(history, start=None, end=None, *, hours=None, now=None):
    if hours is None:
        start, end = timeline.utc_value(start), timeline.utc_value(end)
        if start >= end:
            raise ValueError("O fim deve ser posterior ao início.")
    elif hours not in (1, 24) or start is not None or end is not None:
        raise ValueError("Período relativo inválido.")
    with history.connect() as db:
        db.execute("BEGIN")
        if hours is not None:
            # Fixa a versão do SQLite por uma leitura curta antes de amostrar o relógio.
            db.execute("SELECT 1 FROM sqlite_schema LIMIT 1").fetchone()
            clock = now if now is not None else datetime.now(UTC)
            anchor = clock.replace(microsecond=clock.microsecond // 1000 * 1000)
            start, end = stamp(anchor - timedelta(hours=hours)), stamp(anchor)
        names = set(availability.sensor_names(db))
        names.update(row[0] for row in db.execute("SELECT sensor FROM thermal_rules"))
        rooms = {name: [] for name in sorted(names)}
        for row in db.execute("""
            SELECT sensor, id, measured_at, received_at FROM readings
            WHERE received_at >= ? AND received_at < ? ORDER BY received_at, sensor, id
        """, (start, end)):
            rooms[row["sensor"]].append({"id": row["id"], "measured_at": row["measured_at"],
                                         "received_at": row["received_at"]})
    return {"start": start, "end": end,
            "rooms": [{"sensor": name, "readings": readings} for name, readings in rooms.items()]}


PAGE = (timeline.DIRECTORY / "entregas.html").read_text(encoding="utf-8")
SCRIPT = (timeline.DIRECTORY / "entregas.js").read_text(encoding="utf-8")


def page(example=None):
    return timeline.render_report(PAGE, SCRIPT, example)
