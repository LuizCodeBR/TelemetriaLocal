"""Demonstração reproduzível com MQTT real e relógio de observação controlado."""
import asyncio
import tempfile
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

from . import deliveries, incidents, timeline
from .communication import stamp
from .dashboard import render
from .storage import History, Outbox, Reading, database_call
from .transport import answer_presence, client, deliver, flush, station, topic


@asynccontextmanager
async def responding(sensor, port):
    worker = asyncio.create_task(answer_presence(sensor, port))
    try:
        yield
    finally:
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)


async def wait_contact(history, sensor, moment):
    expected = moment.isoformat(timespec="microseconds")
    async with asyncio.timeout(10):
        while True:
            data = await database_call(history.communication, sensor)
            if data["sensors"] and data["sensors"][0]["last_contact"] == expected:
                return
            await asyncio.sleep(0.025)


async def demonstrate_communication(base):
    base = Path(base) / "demonstracoes"
    base.mkdir(parents=True, exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix="comunicacao-", dir=base))
    queue = Outbox(directory / "sensor.sqlite3")
    origin = datetime(2026, 1, 15, 14, tzinfo=UTC)
    moment = origin
    history = History(directory / "central.sqlite3", received_clock=lambda: moment)
    interrupted, stable = "sala-01", "sala-02"
    buffered = []
    async with station(history, 0, clock=lambda: moment, check_interval=0.05) as port:
        async with responding(stable, port), client(port, topic(stable, "recibos")) as online:
            async with responding(interrupted, port):
                await wait_contact(history, interrupted, moment)
                await wait_contact(history, stable, moment)
                async with client(port, topic(interrupted, "recibos")) as initial:
                    await deliver(initial, Reading.sample(interrupted, moment))
            print("14h00min00s UTC · dois sensores presentes. A sala-01 perde a conexão.")
            for elapsed in range(5, 31, 5):
                moment = origin + timedelta(seconds=elapsed)
                row = Reading.sample(interrupted, moment)
                queue.put(row)
                buffered.append(row)
                await deliver(online, Reading.sample(stable, moment))
                await wait_contact(history, stable, moment)
                if elapsed == 15:
                    event, = history.communication(interrupted)["events"]
                    if event["ended"] is not None:
                        raise RuntimeError("A interrupção deveria estar aberta.")
                    print("14h00min15s UTC · interrupção detectada somente na sala-01.")

            moment = origin + timedelta(seconds=32)
            async with client(port, topic(interrupted, "recibos")) as restored:
                if await flush(restored, queue, interrupted) != len(buffered):
                    raise RuntimeError("A fila de temperaturas não foi entregue.")
            if history.communication(interrupted)["sensors"][0]["state"] != "offline":
                raise RuntimeError("Uma temperatura alterou o estado de comunicação.")
            async with responding(interrupted, port):
                await wait_contact(history, interrupted, moment)
                snapshot = history.snapshot()
    data = snapshot["communication"]
    event, = data["events"]
    expected_times = [(origin + timedelta(seconds=n)).isoformat(timespec="microseconds") for n in (0, 15, 32)]
    if ([event["last_contact"],event["detected"],event["ended"]] != expected_times
            or event["sensor"] != interrupted or event["outcome"] != "retomada"
            or {r["sensor"]:r["state"] for r in data["sensors"]} != {interrupted:"online", stable:"online"}
            or queue.count(interrupted) or not {(r.id, r.measured_at) for r in buffered}.issubset(
                {(r["id"], r["measured_at"]) for r in snapshot["rows"]})):
        raise RuntimeError("O histórico divergiu do cenário simulado.")
    print("14h00min32s UTC · sinal novo da sala-01. 32 s desde o último sinal, 17 s desde a detecção.")
    print("Sala-02: zero interrupções. Seis temperaturas recuperadas com seus horários originais.")
    report = directory / "relatorio.html"
    report.write_text(render(snapshot, live=False), encoding="utf-8")
    print(f"Relatório de comunicação: {report}")
    return report


async def demonstrate_deliveries(base):
    communication_report = await demonstrate_communication(base)
    directory = communication_report.parent
    origin = datetime(2026, 1, 15, 14, tzinfo=UTC)
    sample = deliveries.capture(History(directory / "central.sqlite3"), stamp(origin),
                                stamp(origin + timedelta(seconds=42)))
    path = directory / "entregas.html"
    path.write_text(deliveries.page(sample), encoding="utf-8")
    print(f"Relatório de atraso de entrega offline: {path}")
    return path


async def demonstrate_incidents(base):
    directory = (await demonstrate_communication(base)).parent
    history = History(directory / "central.sqlite3")
    captured = incidents.capture(history)
    event = captured["detail"]
    if len(captured["items"]) != 1 or event["outcome"] != "retomada":
        raise RuntimeError("A sessão deveria conter uma queda seguida de retomada.")
    start, end = incidents.delivery_range(event)
    receipts = deliveries.capture(history, start, end)
    sample = {"rooms":captured["rooms"], "incident":event, "deliveries_html":deliveries.page(receipts)}
    path = directory / "incidentes.html"
    path.write_text(incidents.page(sample), encoding="utf-8")
    print(f"Acompanhamento de incidentes offline: {path}")
    return path


async def demonstrate_timeline(base, *, availability_demo=False):
    report = await demonstrate_communication(base)
    directory = report.parent
    origin = datetime(2026, 1, 15, 14, tzinfo=UTC)
    moment = origin + timedelta(seconds=42)
    history = History(directory / "central.sqlite3", received_clock=lambda: moment)
    names = ["sala-01", "sala-02"]
    start, end = stamp(origin), stamp(origin + timedelta(seconds=48))
    async with station(history, 0, clock=lambda: moment, check_interval=0.05) as port:
        async with responding(names[0], port), responding(names[1], port):
            for name in names:
                await wait_contact(history, name, moment)
                async with client(port, topic(name, "recibos")) as connection:
                    await deliver(connection, Reading.sample(name, moment))
            moment += timedelta(seconds=5)
            for name in names:
                await wait_contact(history, name, moment)
            data = timeline.capture_period(history, names, start, end, now=moment)
    first, second = (data[name] for name in names)
    recovered = [r for r in first["rows"] if r["received_at"] == stamp(origin + timedelta(seconds=32))]
    if (len(recovered) != 6 or len(first["events"]) != 1 or second["events"] or
            first["gaps"] != [{"start": stamp(origin + timedelta(seconds=32)),
                               "until": stamp(origin + timedelta(seconds=42))}]):
        raise RuntimeError("O histórico do ensaio de dois sensores divergiu dos eventos esperados.")
    comparison = first["availability"]
    for room in data.values():
        del room["availability"]
    example = {"start": start, "end": end, "data": data, "availability": comparison}
    if availability_demo:
        example["initialEnd"] = stamp(origin + timedelta(seconds=42))
    path = directory / ("disponibilidade.html" if availability_demo else "historico.html")
    path.write_text(timeline.page(names, example=example), encoding="utf-8")
    print("A central retomou a observação às 14h00min42s UTC após 10 s sem observar as duas salas.")
    if availability_demo:
        print(f"Relatório de disponibilidade offline: {path}")
    else:
        print(f"Histórico interativo offline: {path}")
    return path
