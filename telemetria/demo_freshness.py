"""Demonstração local com relógio controlado e o mesmo SQLite usado pela central."""
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import mkdtemp

from . import freshness_report, maintenance
from .communication import Observer, stamp
from .storage import History, Reading


def demonstrate_freshness(base):
    parent = Path(base)/"demonstracoes"
    parent.mkdir(parents=True,exist_ok=True)
    directory = Path(mkdtemp(prefix="atualidade-",dir=parent))
    origin = datetime(2026,1,15,14,20,tzinfo=UTC)
    def at(seconds):
        return origin + timedelta(seconds=seconds)

    moment = at(-10)
    history = History(directory/"central.sqlite3",received_clock=lambda:moment)
    monitor = Observer(history)
    monitor.start(moment)
    rooms = ("sala-01","sala-02")
    def presence(n):
        for name in rooms:
            monitor.advance(at(n),name,str(n))
    def reading(name, second, number):
        return Reading(name,f"{number:032x}",stamp(at(second)),24.0,52.0)

    presence(-10)
    maintenance.update(history, {"origin":maintenance.agenda(history,now=moment)["origin"],"key":"a"*32,
        "sensor":rooms[0],"start":stamp(at(-5)),"end":stamp(at(20)),"reason":"Troca de bateria"},now=moment)
    steps = []
    descriptions = {
        0:"14h20min00s · As duas salas respondem e entregam medições de 24 °C. A sala-01 está em manutenção.",
        15:"14h20min15s · A sala-01 continua respondendo. Sua última medição tem 15 s e o limite é 10 s. A sala-02 continua medindo.",
        20:"14h20min20s · Chega à sala-01 a leitura das 14h19min50s, seguida de um reenvio idêntico. A manutenção termina e a ocorrência permanece aberta.",
        25:"14h20min25s · A leitura das 14h20min24s chega à central. Ela também marca 24 °C e confirma a recuperação da sala-01."
    }
    for n in (0,5,10,15,20,25):
        moment = at(n)
        presence(n)
        history.record(reading(rooms[1],n,200+n))
        if n == 0:
            history.record(reading(rooms[0],0,1))
        elif n == 20:
            late = reading(rooms[0],-10,2)
            if history.record(late) != "gravada" or history.record(late) != "repetida":
                raise RuntimeError("O ensaio não confirmou o reenvio da leitura atrasada.")
        elif n == 25:
            history.record(reading(rooms[0],24,3))
        if n in descriptions:
            steps.append({"description":descriptions[n],"report":freshness_report.capture(history,now=moment)})
    monitor.stop()
    event, = steps[-1]["report"]["events"]
    if (event["recovery_id"] != f"{3:032x}" or event["detected"] != stamp(at(15))
            or event["ended"] != stamp(at(25)) or len(event["maintenance"]) != 1):
        raise RuntimeError("A ocorrência não corresponde ao cenário de recuperação esperado.")
    path = directory/"atualidade.html"
    path.write_text(freshness_report.page(steps),encoding="utf-8")
    print(f"Atualidade das medições offline: {path}")
    return path
