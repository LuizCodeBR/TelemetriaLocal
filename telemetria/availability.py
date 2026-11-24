"""Intervalos comprovados para comparar salas na mesma transação do histórico."""


def sensor_names(db):
    catalog = db.execute("""
        SELECT sensor FROM thermal_state UNION SELECT sensor FROM comm_contacts
        UNION SELECT sensor FROM comm_sensor_windows ORDER BY sensor
    """)
    return [row[0] for row in catalog]


def evidence(db, start, end, gaps):
    rooms = {name: {"sensor": name, "evaluated": [], "outages": []} for name in sensor_names(db)}
    blind = []
    if start < end:
        params = {"start": start, "end": end}
        for row in db.execute("""
            SELECT s.sensor, max(s.classified_from,:start), min(w.seen,:end)
            FROM comm_sensor_windows s JOIN comm_windows w ON w.id=s.window_id
            WHERE w.seen>:start AND s.classified_from<:end AND w.seen>s.classified_from
            ORDER BY w.id,s.sensor
        """, params):
            rooms[row[0]]["evaluated"].append([row[1], row[2]])
        for row in db.execute("""
            SELECT e.sensor, max(e.detected,:start), min(coalesce(e.ended,w.seen),:end)
            FROM comm_events e JOIN comm_windows w ON w.id=e.window_id
            WHERE e.detected<:end AND coalesce(e.ended,w.seen)>:start
              AND coalesce(e.ended,w.seen)>e.detected
            ORDER BY e.window_id,e.detected,e.id
        """, params):
            rooms[row[0]]["outages"].append([row[1], row[2]])
        for row in db.execute("""SELECT sensor,start,end,reason FROM maintenance_windows
                    WHERE canceled_at IS NULL AND start<:end AND end>:start ORDER BY sensor,start""", params):
            rooms[row["sensor"]].setdefault("maintenance", []).append(
                {"start":row["start"], "end":row["end"], "reason":row["reason"]})
        for gap in gaps:
            left, right = max(start, gap["start"]), min(end, gap["until"])
            if left < right:
                blind.append([left, right])
    return {"rooms": list(rooms.values()), "blind": blind}
