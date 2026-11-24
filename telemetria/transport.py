"""Transporte MQTT 5 local. A confirmação da aplicação vem após o commit."""
import asyncio
import hashlib
import logging
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from uuid import uuid4

import mqttools

from .communication import Observer, PERIOD, exclusive_observer
from .storage import Reading, ReadingConflict, database_call, json_bytes, valid_sensor

HOST = "127.0.0.1"
NETWORK_ERRORS = (OSError, mqttools.TimeoutError, mqttools.ConnectError, mqttools.SubscribeError)
LOG = logging.getLogger(__name__)


def topic(sensor, kind):
    return f"telemetria/v1/{sensor}/{kind}"


PRESENCE_REQUEST = "telemetria/central/rodada"


def receipt(reading, result):
    return json_bytes({"id": reading.id, "sha256": hashlib.sha256(reading.encode()).hexdigest(), "result": result})


@asynccontextmanager
async def broker(port):
    # O broker processa MQTT. Este contexto gerencia o ciclo de vida dos sockets,
    # inclusive conexões abertas quando a central é desligada.
    protocol = mqttools.Broker((HOST, port))
    connections = {}

    async def handle(reader, writer):
        try:
            await protocol.serve_client(reader, writer)
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass
            connections.pop(writer, None)

    def accept(reader, writer):
        connections[writer] = asyncio.create_task(handle(reader, writer))

    server = await asyncio.start_server(accept, HOST, port)
    try:
        yield server.sockets[0].getsockname()[1]
    finally:
        server.close()
        sessions = list(connections.items())
        for writer, _ in sessions:
            writer.close()
        await asyncio.gather(*(task for _, task in sessions), return_exceptions=True)
        await server.wait_closed()


@asynccontextmanager
async def client(port, subscription):
    connection = mqttools.Client(HOST, port, client_id=uuid4().hex, subscriptions=[subscription],
                                  connect_delays=[], response_timeout=3)
    await connection.start()
    try:
        yield connection
    finally:
        # O cliente MQTT absorve cancelamentos internos em stop(). Mantemos o
        # cancelamento do serviço separado enquanto o cliente fecha o socket.
        closing = asyncio.create_task(connection.stop())
        try:
            await asyncio.shield(closing)
        except asyncio.CancelledError:
            await closing
            raise


async def receive_loop(connection, history):
    while True:
        message = await connection.messages.get()
        if message is None:
            raise ConnectionError("Coletor desconectado")
        try:
            reading = Reading.decode(message.message)
            if message.topic != topic(reading.sensor, "leituras") or message.retain:
                raise ValueError("Tópico incompatível com a leitura")
            result = await database_call(history.record, reading)
        except ReadingConflict:
            LOG.warning("Conflito de conteúdo no sensor %s, leitura %s.", reading.sensor, reading.id)
            result = "conflito"
        except (ValueError, OverflowError, RecursionError) as error:
            LOG.warning("Leitura recusada: %s", error)
            continue
        connection.publish(mqttools.Message(topic(reading.sensor, "recibos"), receipt(reading, result)))


async def collect(history, port, ready):
    while True:
        try:
            async with client(port, topic("+", "leituras")) as connection:
                ready.set()
                await receive_loop(connection, history)
        except NETWORK_ERRORS as error:
            if asyncio.current_task().cancelling():
                raise asyncio.CancelledError from error
            LOG.warning("Coletor MQTT desconectado. Nova tentativa em 1 s.")
            await asyncio.sleep(1)


async def monitor_session(connection, observer, ready, clock, check_interval):
    loop = asyncio.get_running_loop()
    token = None
    issued = None
    next_check = loop.time()
    while True:
        now = clock()
        if token is None or loop.time() >= next_check:
            prior = observer.window
            await database_call(observer.advance, now)
            now = clock()
            if token is None or prior != observer.window or not 0 <= (now - issued).total_seconds() < PERIOD:
                token = uuid4().hex.encode("ascii")
                issued = now
                # Um sensor que acabou de conectar pode responder já na rodada atual.
                connection.publish(mqttools.Message(PRESENCE_REQUEST, token, retain=True))
                ready.set()
            next_check = loop.time() + check_interval
        try:
            async with asyncio.timeout_at(next_check):
                message = await connection.messages.get()
        except TimeoutError:
            continue
        if message is None:
            raise ConnectionError("Monitor MQTT desconectado")
        now = clock()
        parts = message.topic.split("/")
        if (message.retain or message.message != token or len(parts) != 4 or parts[:2] != ["telemetria", "v1"]
                or parts[3] != "presenca" or not valid_sensor(parts[2]) or not 0 <= (now-issued).total_seconds() < PERIOD):
            continue
        previous = observer.window
        await database_call(observer.advance, now, parts[2], token.decode("ascii"))
        if observer.window != previous:
            token = None


async def monitor_presence(history, port, ready, clock, check_interval):
    observer = Observer(history, clock)
    while True:
        try:
            async with client(port, topic("+", "presenca")) as connection:
                try:
                    await database_call(observer.start, clock())
                    await monitor_session(connection, observer, ready, clock, check_interval)
                finally:
                    await database_call(observer.stop)
        except NETWORK_ERRORS as error:
            if asyncio.current_task().cancelling():
                raise asyncio.CancelledError from error
            LOG.warning("Monitor de presença desconectado. Nova tentativa em 1 s.")
            await asyncio.sleep(1)


async def answer_presence(sensor, port):
    while True:
        try:
            async with client(port, PRESENCE_REQUEST) as connection:
                while True:
                    message = await connection.messages.get()
                    if message is None:
                        raise ConnectionError("Canal de presença desconectado")
                    if message.topic == PRESENCE_REQUEST and len(message.message) == 32:
                        connection.publish(mqttools.Message(topic(sensor, "presenca"), message.message))
        except NETWORK_ERRORS as error:
            if asyncio.current_task().cancelling():
                raise asyncio.CancelledError from error
            await asyncio.sleep(1)


@asynccontextmanager
async def station(history, port, *, clock=None, check_interval=1.0):
    if clock is None:
        clock = lambda: datetime.now(UTC)
    with exclusive_observer(history.path):
        async with broker(port) as actual_port:
            ready, monitoring = asyncio.Event(), asyncio.Event()
            async with asyncio.TaskGroup() as group:
                collector = group.create_task(collect(history, actual_port, ready))
                monitor = group.create_task(monitor_presence(history, actual_port, monitoring, clock, check_interval))
                try:
                    await ready.wait()
                    await monitoring.wait()
                    yield actual_port
                finally:
                    monitor.cancel()
                    collector.cancel()


async def deliver(connection, reading, timeout=3):
    valid = {receipt(reading, result): result for result in ("gravada", "repetida", "conflito")}
    connection.publish(mqttools.Message(topic(reading.sensor, "leituras"), reading.encode()))
    async with asyncio.timeout(timeout):
        while True:
            message = await connection.messages.get()
            if message is None:
                raise ConnectionError("Conexão perdida antes da confirmação")
            if message.topic == topic(reading.sensor, "recibos") and not message.retain:
                result = valid.get(message.message)
                if result:
                    return result


async def flush(connection, outbox, sensor, timeout=3):
    confirmed = 0
    for reading in await database_call(outbox.batch, sensor):
        result = await deliver(connection, reading, timeout)
        if result == "conflito":
            await database_call(outbox.mark_conflict, reading)
        elif await database_call(outbox.acknowledge, reading):
            confirmed += 1
    return confirmed


async def wait_for_work(connection, interval):
    # Entre lotes não há recibo pendente. Uma desconexão deve acordar o remetente.
    try:
        async with asyncio.timeout(interval):
            while True:
                if await connection.messages.get() is None:
                    raise ConnectionError("Sensor desconectado")
    except TimeoutError:
        return


async def synchronize(outbox, sensor, port, retry_min=1, retry_max=8):
    delay = retry_min
    while True:
        try:
            async with client(port, topic(sensor, "recibos")) as connection:
                while True:
                    sent = await flush(connection, outbox, sensor)
                    if sent:
                        delay = retry_min
                    else:
                        await wait_for_work(connection, retry_min)
        except NETWORK_ERRORS as error:
            if asyncio.current_task().cancelling():
                raise asyncio.CancelledError from error
            LOG.info("Tentativa de entrega adiada (%s), nova tentativa em %s s", type(error).__name__, delay)
            await asyncio.sleep(delay)
            delay = min(retry_max, delay * 2)


async def sample_forever(outbox, sensor, interval):
    while True:
        await database_call(outbox.put, Reading.sample(sensor))
        await asyncio.sleep(interval)


async def run_sensor(outbox, sensor, port, interval):
    async with asyncio.TaskGroup() as group:
        group.create_task(sample_forever(outbox, sensor, interval))
        group.create_task(synchronize(outbox, sensor, port))
        group.create_task(answer_presence(sensor, port))
