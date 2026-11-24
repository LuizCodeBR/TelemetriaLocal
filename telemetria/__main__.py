import argparse
import asyncio
import logging
import math
from pathlib import Path

from .alerts import seconds_between
from .communication import OUTCOMES, STATES
from .dashboard import STATUS, dashboard, duration
from .demo import demonstrate, demonstrate_alerts
from .demo_maintenance import demonstrate_maintenance
from .demo_freshness import demonstrate_freshness
from .demo_communication import demonstrate_communication, demonstrate_deliveries, demonstrate_incidents, demonstrate_timeline
from .storage import History, Outbox, database_call, valid_sensor
from .transport import run_sensor, station


def parse():
    parser = argparse.ArgumentParser(description="Telemetria Local · simulador, coletor e painel")
    parser.add_argument("modo", nargs="?", default="iniciar", choices=("iniciar", "demo", "demo-alertas", "demo-comunicacao", "demo-historico", "demo-disponibilidade", "demo-entregas", "demo-incidentes", "demo-manutencao", "demo-atualidade", "comunicacao", "central", "sensor", "conflitos", "regra", "ocorrencias"))
    parser.add_argument("--dados", type=Path, default=Path(__file__).resolve().parent.parent / "dados")
    parser.add_argument("--mqtt", type=int, default=1883, metavar="PORTA")
    parser.add_argument("--web", type=int, default=8765, metavar="PORTA")
    parser.add_argument("--sensor", default="sala-01")
    parser.add_argument("--intervalo", type=float, default=2, metavar="SEGUNDOS")
    parser.add_argument("--alta", type=float, help="Temperatura para confirmar alta em três instantes (°C)")
    parser.add_argument("--recuperacao", type=float, help="Temperatura de recuperação (°C)")
    parser.add_argument("--lacuna", type=float, help="Intervalo máximo entre medições consecutivas (segundos)")
    args = parser.parse_args()
    if args.modo != "regra" and any(value is not None for value in (args.alta, args.recuperacao, args.lacuna)):
        parser.error("Use os parâmetros de temperatura e lacuna com o comando regra.")
    if not valid_sensor(args.sensor):
        parser.error("Use de 1 a 40 letras sem acento, dígitos, hífens ou sublinhados em --sensor.")
    if not math.isfinite(args.intervalo) or args.intervalo < 0.1:
        parser.error("O intervalo mínimo é 0,1 s.")
    if not all(1 <= port <= 65535 for port in (args.mqtt, args.web)):
        parser.error("As portas devem estar entre 1 e 65535.")
    return args


async def run(args):
    if args.modo == "demo":
        await demonstrate(args.dados)
        return
    if args.modo in ("demo-historico", "demo-disponibilidade"):
        await demonstrate_timeline(args.dados, availability_demo=args.modo == "demo-disponibilidade")
        return
    if args.modo == "demo-atualidade":
        demonstrate_freshness(args.dados)
        return
    if args.modo == "demo-manutencao":
        demonstrate_maintenance(args.dados)
        return
    if args.modo == "demo-incidentes":
        await demonstrate_incidents(args.dados)
        return
    if args.modo == "demo-entregas":
        await demonstrate_deliveries(args.dados)
        return
    if args.modo == "demo-comunicacao":
        await demonstrate_communication(args.dados)
        return
    if args.modo == "demo-alertas":
        await demonstrate_alerts(args.dados)
        return
    if args.modo == "conflitos":
        outbox = await database_call(Outbox, args.dados / "sensor.sqlite3")
        rows = await database_call(outbox.conflicts, args.sensor)
        print(f"Conflitos do sensor {args.sensor} (até 50 mais recentes): {len(rows)} exibidos.")
        for reading, marked in rows:
            print(f"{reading.id} | medido {reading.measured_at} | {reading.temperature} °C | "
                  f"{reading.humidity}% | conflito {marked}")
        return
    if args.modo == "sensor":
        print(f"Sensor {args.sensor} · fila em {args.dados} · Ctrl+C para encerrar.", flush=True)
        outbox = await database_call(Outbox, args.dados / "sensor.sqlite3")
        await run_sensor(outbox, args.sensor, args.mqtt, args.intervalo)
        return
    history = await database_call(History, args.dados / "central.sqlite3")
    if args.modo == "comunicacao":
        data = await database_call(history.communication, args.sensor, live=True)
        print(f"{args.sensor} | {len(data['events'])} interrupção(ões) | UTC")
        for row in data["sensors"]:
            print(f"{STATES[row['state']]} | último sinal: {row['last_contact'] or 'ainda não recebido'}")
        for event in data["events"]:
            end = event["ended"] or data["seen"]
            print(f"{OUTCOMES[event['outcome']]} | último sinal nesta janela: {event['last_contact'] or 'nenhum'} | "
                  f"detecção: {event['detected']} | fim: {event['ended'] or 'em aberto'} | "
                  f"duração observada: {duration(seconds_between(event['detected'], end))}")
        for gap in data["gaps"]:
            print(f"Central sem observação: {gap['started']} até {gap['ended'] or 'nova observação'}")
        return
    if args.modo == "regra":
        rule = await database_call(history.configure, args.sensor, high=args.alta,
                                   recovery=args.recuperacao, gap_seconds=args.lacuna)
        print(f"{args.sensor} | alta ≥ {rule.high:g} °C em 3 instantes | recuperação ≤ {rule.recovery:g} °C | "
              f"intervalo máximo {rule.gap_seconds:g} s")
        return
    if args.modo == "ocorrencias":
        data = await database_call(history.occurrences, args.sensor)
        print(f"{args.sensor} | {len(data['events'])} ocorrência(s) | {len(data['gaps'])} lacuna(s) | UTC")
        for event in data["events"]:
            until = event["observed_until"]
            print(f"{STATUS[event['status']]} | {event['started']} até {until} | "
                  f"{duration(seconds_between(event['started'], until))} | pico {event['peak']:g} °C")
        for gap in data["gaps"]:
            print(f"Lacuna | {gap['before_at']} até {gap['after_at']}")
        return
    with dashboard(history, args.web) as web:
        async with station(history, args.mqtt) as port:
            print(f"Painel: http://127.0.0.1:{web}   |   MQTT local: {port}   |   Ctrl+C para encerrar.", flush=True)
            if args.modo == "iniciar":
                outbox = await database_call(Outbox, args.dados / "sensor.sqlite3")
                await run_sensor(outbox, args.sensor, port, args.intervalo)
            else:
                await asyncio.Event().wait()


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    try:
        asyncio.run(run(parse()))
    except KeyboardInterrupt:
        pass
    except ValueError as error:
        raise SystemExit(str(error))
    except OSError as error:
        raise SystemExit(f"Falha local: {error}. Confira as portas (--mqtt, --web) e a pasta --dados.")
