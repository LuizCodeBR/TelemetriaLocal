"""Consulta por sensor com o mesmo recorte de dados para gráfico, tabela e CSV."""
import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs

from . import availability, freshness
from .communication import TIMEOUT, stamp
from .storage import valid_sensor


def utc_value(value):
    try:
        # O SQLite armazena microssegundos. Evita truncar silenciosamente um limite entre dois instantes.
        if re.search(r"[.,]\d{6}\d*[1-9]", value):
            raise ValueError
        moment = datetime.fromisoformat(value)
        if moment.utcoffset() != timedelta(0):
            raise ValueError
        return stamp(moment)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("Informe uma data e hora em UTC.") from exc


def query_params(query):
    fields = parse_qs(query, keep_blank_values=True, strict_parsing=True, max_num_fields=3)
    if any(len(v) != 1 for v in fields.values()):
        raise ValueError("Parâmetro de filtro repetido.")
    if set(fields) == {"sensor", "inicio", "fim"}:
        return {"sensor": fields["sensor"][0], "start": fields["inicio"][0], "end": fields["fim"][0]}
    if set(fields) == {"sensor", "periodo"} and fields["periodo"][0] in ("1h", "24h"):
        return {"sensor": fields["sensor"][0], "hours": int(fields["periodo"][0][:-1])}
    raise ValueError("Informe um sensor e um período válido.")


def sensors(history):
    with history.connect() as db:
        return availability.sensor_names(db)


def period(history, sensor, start=None, end=None, *, hours=None, now=None):
    return capture_period(history, [sensor], start, end, hours=hours, now=now)[sensor]


def capture_period(history, names, start=None, end=None, *, hours=None, now=None):
    """Fixa uma única imagem de leitura, inclusive ao gerar o HTML de várias salas."""
    names = tuple(names)
    if not all(valid_sensor(name) for name in names):
        raise ValueError("Identificador de sensor inválido.")
    if hours is None:
        start, end = utc_value(start), utc_value(end)
        if start >= end:
            raise ValueError("O fim deve ser posterior ao início.")
    elif hours not in (1, 24) or start is not None or end is not None:
        raise ValueError("Período relativo inválido.")
    with history.connect() as db:
        db.execute("BEGIN")
        # A primeira leitura fixa a versão do SQLite antes de consultar o relógio.
        window = db.execute("SELECT seen, closed FROM comm_windows ORDER BY id DESC LIMIT 1").fetchone()
        now = now if now is not None else datetime.now(UTC)
        if hours is not None:
            anchor = now.replace(microsecond=now.microsecond // 1000 * 1000)
            start, end = stamp(anchor - timedelta(hours=hours)), stamp(anchor)
        seen = window["seen"] if window else None
        stagnant = window and (window["closed"] or now - datetime.fromisoformat(seen) >= timedelta(seconds=TIMEOUT))
        gaps = [dict(row) for row in db.execute("""
            SELECT a.seen AS start, b.started AS until FROM comm_windows a
            JOIN comm_windows b ON b.id=a.id+1
            WHERE b.started>a.seen AND a.seen<? AND b.started>? ORDER BY a.id
        """, (end, start))]
        clock_end = stamp(now)
        if stagnant and seen < min(end, clock_end) and clock_end > start:
            gaps.append({"start": seen, "until": clock_end, "ongoing": True})

        # O relógio atual limita a lacuna ainda aberta. As janelas gravadas têm limites próprios.
        comparison = availability.evidence(db, start, end, gaps)
        result = {}
        for name in names:
            rows = db.execute("""
                SELECT id, measured_at, received_at, temperature, humidity FROM readings
                WHERE sensor=? AND measured_at>=? AND measured_at<? ORDER BY measured_at, id
            """, (name, start, end))
            events = []
            for row in db.execute("""
                SELECT detected, ended, outcome FROM comm_events
                WHERE sensor=:name AND detected<:end AND
                    (coalesce(ended,:seen)>:start OR detected>=:start OR (ended=:start AND outcome='retomada'))
                ORDER BY detected, id
            """, {"name":name, "end":end, "start":start, "seen":seen}):
                ended, outcome = row["ended"], row["outcome"]
                if ended is None and stagnant:
                    ended, outcome = seen, "sem_observacao"
                events.append({"start": row["detected"], "until": ended or seen, "outcome": outcome})
            result[name] = {"sensor": name, "start": start, "end": end, "rows": [dict(r) for r in rows],
                            "events": events, "gaps": gaps, "availability": comparison,
                            "freshness":freshness.period_events(db,name,start,end,stagnant)}
    return result

DIRECTORY = Path(__file__).parent
PAGE = (DIRECTORY / "historico.html").read_text(encoding="utf-8")
SCRIPT = (DIRECTORY / "historico.js").read_text(encoding="utf-8")
COMMON_SCRIPT = (DIRECTORY / "relatorios.js").read_text(encoding="utf-8")
STYLE = (DIRECTORY / "relatorios.css").read_text(encoding="utf-8")


def render_report(template, script, bootstrap):
    payload = json.dumps(bootstrap, ensure_ascii=False, allow_nan=False).replace("<", r"\u003c")
    return (template.replace("<!--APP_STYLE-->", STYLE)
            .replace("<!--APP_SCRIPT-->", COMMON_SCRIPT + "\n" + script)
            .replace("<!--APP_DATA-->", payload))


def page(sensor_names, *, example=None):
    return render_report(PAGE, SCRIPT, {"sensors": sensor_names, "example": example})
