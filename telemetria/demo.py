import tempfile
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .dashboard import render
from .storage import History, Outbox, Reading
from .transport import NETWORK_ERRORS, client, deliver, flush, station, topic


async def demonstrate(base):
    base = Path(base) / "demonstracoes"
    base.mkdir(parents=True, exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix="sessao-", dir=base))
    outbox = Outbox(directory / "sensor.sqlite3")
    history = History(directory / "central.sqlite3")
    sensor = "sala-01"
    subscription = topic(sensor, "recibos")

    async with station(history, 0) as port:
        async with client(port, subscription):
            print("1. Sensor conectado à central local.")
    first = datetime.now(UTC) - timedelta(seconds=9)
    readings = [Reading.sample(sensor, first + timedelta(seconds=i)) for i in range(10)]
    for reading in readings:
        outbox.put(reading)
    try:
        async with client(port, subscription):
            raise RuntimeError("A central deveria estar desligada")
    except NETWORK_ERRORS:
        print(f"2. Central desligada. Fila no disco: {outbox.count(sensor)}. Histórico: 0.")

    # Novas instâncias reabrem os dois arquivos como após uma reinicialização.
    outbox, history = Outbox(outbox.path), History(history.path)
    async with station(history, port):
        async with client(port, subscription) as connection:
            sent = await flush(connection, outbox, sensor)
            print(f"3. Reconexão. Leituras confirmadas: {sent}. Pendentes: {outbox.count(sensor)}.")
            replay = await deliver(connection, readings[0])
    snapshot = history.snapshot()
    found = {(r["id"], r["measured_at"]) for r in snapshot["rows"]}
    expected = {(r.id, r.measured_at) for r in readings}
    if found != expected or snapshot["total"] != 10 or outbox.count(sensor) or replay != "repetida":
        raise RuntimeError("O resultado não corresponde às leituras do sensor")
    print("4. Primeira leitura reenviada. Histórico: 10 leituras únicas com os horários originais.")
    report = directory / "relatorio.html"
    report.write_text(render(snapshot, live=False), encoding="utf-8")
    print(f"Painel da demonstração: {report}")
    return report


async def demonstrate_alerts(base):
    base = Path(base) / "demonstracoes"
    base.mkdir(parents=True, exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix="alertas-", dir=base))
    history = History(directory / "central.sqlite3")
    outbox = Outbox(directory / "sensor.sqlite3")
    sensor = "sala-01"
    start = datetime(2026, 1, 15, 14, 2, tzinfo=UTC)
    readings = {}
    for offset in range(-10, 311, 10):
        value = 29.0
        if offset == 90:
            value = 31.4
        elif offset < 0 or offset > 300:
            value = 25.5
        elif offset == 0:
            value = 28.0
        elif offset == 300:
            value = 26.0
        elif offset >= 240:
            value = 27.2
        at = start + timedelta(seconds=offset)
        readings[offset] = replace(Reading.sample(sensor, at), temperature=value)

    # Dois instantes só ficam disponíveis depois do primeiro lote.
    for offset, reading in readings.items():
        if offset not in (0, 90):
            outbox.put(reading)
    print(f"1. Central desligada. {outbox.count(sensor)} medições simuladas aguardam envio.")
    async with station(history, 0) as port:
        async with client(port, topic(sensor, "recibos")) as connection:
            await flush(connection, outbox, sensor)
            initial = history.occurrences(sensor)
            print(f"2. Reconexão. Histórico provisório com {len(initial['events'])} ocorrências e "
                  f"{len(initial['gaps'])} lacunas.")
            for offset in (90, 0):
                outbox.put(readings[offset])
            await flush(connection, outbox, sensor)
            replay = await deliver(connection, readings[90])
    snapshot = history.snapshot()
    expected = {"sensor": sensor, "started": readings[0].measured_at, "ended": readings[300].measured_at,
                "observed_until": readings[300].measured_at, "status": "recuperada", "peak": 31.4}
    if (snapshot["events"] != [expected] or snapshot["gaps"] or snapshot["total"] != len(readings)
            or replay != "repetida" or outbox.count(sensor)):
        raise RuntimeError("O resultado da demonstração de alertas divergiu do perfil simulado.")
    print("3. Duas medições atrasadas e um reenvio. Uma ocorrência de 14h02 a 14h07 UTC, 5 min, pico de 31,4 °C.")
    report = directory / "relatorio.html"
    report.write_text(render(snapshot, live=False), encoding="utf-8")
    print(f"Relatório de alertas: {report}")
    return report
