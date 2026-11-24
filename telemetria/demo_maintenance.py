"""Ensaio offline de duas salas com o mesmo período de interrupção observada."""
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import mkdtemp

from . import maintenance, timeline
from .communication import Observer, stamp
from .storage import History, Reading


def demonstrate_maintenance(base):
    parent = Path(base) / "demonstracoes"
    parent.mkdir(parents=True, exist_ok=True)
    directory = Path(mkdtemp(prefix="manutencao-", dir=parent))
    origin = datetime(2026,1,15,13,55,tzinfo=UTC)
    def at(second):
        return origin + timedelta(seconds=second)

    rooms = ["sala-01", "sala-02"]
    history = History(directory / "central.sqlite3")
    monitor = Observer(history)
    monitor.start(origin)
    for name in rooms:
        monitor.advance(origin, name, "primeira")
        history.record(Reading.sample(name, origin))
    window = {"origin":maintenance.agenda(history, now=origin)["origin"], "key":"1"*32,
              "sensor":rooms[0], "start":stamp(at(300)), "end":stamp(at(1500)), "reason":"Troca de bateria"}
    maintenance.update(history, window, now=origin)
    initial = maintenance.agenda(history, now=origin)
    # A última presença é às 14h04min45s. A detecção ocorre 15 segundos depois.
    for second in range(5,1921,5):
        if second <= 585 or second == 1920:
            for name in rooms:
                monitor.advance(at(second), name, str(second))
        else:
            monitor.advance(at(second))
    for name in rooms:
        history.record(Reading.sample(name, at(1920)))
    # Inclui a própria retomada no relatório, cujo fim é exclusivo.
    monitor.advance(at(1921))
    monitor.stop()
    start, end = stamp(at(300)), stamp(at(1921))
    data = timeline.capture_period(history, rooms, start, end, now=at(1921))
    comparison = data[rooms[0]]["availability"]
    for room in comparison["rooms"]:
        if room["outages"] != [[stamp(at(600)),stamp(at(1920))]]:
            raise RuntimeError("O ensaio não reproduziu a queda das 14h05 às 14h27.")
    for room in data.values():
        del room["availability"]
    example = {"start":start, "end":end, "availability":comparison, "data":data}
    # O documento interno usa exatamente o histórico, o gráfico e o CSV da central.
    template = timeline.render_report(timeline.PAGE, timeline.SCRIPT, None)
    marker = '<script type="application/json" id="bootstrap">'
    prefix, suffix = template.split(marker + "null", 1)
    path = directory / "manutencao.html"
    path.write_text(maintenance.page({"agenda":initial, "example":example, "start":window["start"],
                    "end":window["end"], "reportPrefix":prefix + marker, "reportSuffix":suffix}), encoding="utf-8")
    print(f"Manutenção por sala offline: {path}")
    return path
