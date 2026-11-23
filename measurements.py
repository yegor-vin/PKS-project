import argparse
import csv
import importlib.util
import json
import os
import random
import secrets
import statistics
import struct
import zlib


UDP, TCP, IPV4, ETH_HDR, ETH_FCS, ETH_MIN_PAYLOAD = 8, 32, 20, 14, 4, 46
TCP_SYN = 40


def l2_frame(app_len, transport):
    ip_packet = IPV4 + transport + app_len
    padding = max(0, ETH_MIN_PAYLOAD - ip_packet)
    return ETH_HDR + ip_packet + padding + ETH_FCS


def layers(app_len, transport):
    ip_packet = IPV4 + transport + app_len
    padding = max(0, ETH_MIN_PAYLOAD - ip_packet)
    return {"app": app_len, "transport": transport, "ip": IPV4,
            "eth": ETH_HDR + ETH_FCS, "padding": padding,
            "frame": ETH_HDR + ip_packet + padding + ETH_FCS}


def load_tester(path):
    spec = importlib.util.spec_from_file_location("tester", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


JSON_IDS = {"ThermoNode": "T-01", "WindSense": "W-01", "RainDetect": "R-01", "AirQuality": "A-01"}


def json_poc(message):
    return json.dumps(message).encode("utf-8")


def json_poc_crc(message):
    body = {k: v for k, v in message.items() if k != "crc"}
    return zlib.crc32(json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8"))


def json_messages(st, values, seq):
    sid, token = JSON_IDS[st], secrets.token_hex(6)
    data = {"type": "DATA", "sequence": seq, "sensorId": sid, "token": token}
    data.update(values)
    data["crc"] = json_poc_crc(data)
    return {
        "REGISTER":      {"type": "REGISTER", "sequence": seq, "sensorType": st, "sensorId": sid},
        "ACK (register)": {"type": "ACK", "ack-for": seq, "token": token, "sequence": seq},
        "DATA":          data,
        "ACK (data)":    {"type": "ACK", "ack-for": seq, "sequence": seq},
        "KEEPALIVE":     {"type": "KEEPALIVE", "sequence": seq, "sensorId": sid, "token": token},
        "ACK (keepalive)": {"type": "ACK", "sequence": seq, "ack-for": seq, "sensorId": sid, "token": token},
        "RETRANSMIT":    {"type": "RETRANSMIT", "ack-for": seq, "sequence": seq},
        "REJECT":        {"type": "REJECT", "ack-for": seq, "reason": "Invalid token", "sequence": seq},
    }


def binary_messages(t, st, values, seq):
    token = secrets.randbits(32)
    data = {"type": "DATA", "sequence": seq, "sensorType": st, "sensorId": 1, "token": token}
    data.update(values)
    return {
        "REGISTER":       {"type": "REGISTER", "sequence": seq, "sensorType": st, "sensorId": 1},
        "ACK (register)": {"type": "ACK", "sequence": seq, "ack-for": seq, "token": token},
        "DATA":           data,
        "ACK (data)":     {"type": "ACK", "sequence": seq, "ack-for": seq},
        "KEEPALIVE":      {"type": "KEEPALIVE", "sequence": seq, "token": token, "sensorId": 1},
        "ACK (keepalive)": {"type": "ACK", "sequence": seq, "ack-for": seq, "token": token},
        "RETRANSMIT":     {"type": "RETRANSMIT", "sequence": seq, "ack-for": seq},
        "REJECT":         {"type": "REJECT", "sequence": seq, "ack-for": seq, "reason": "Invalid token"},
    }


def coap_option(delta, value):
    def nib(n):
        if n < 13:
            return n, b""
        if n < 269:
            return 13, bytes([n - 13])
        return 14, struct.pack("!H", n - 269)
    d, dx = nib(delta)
    ln, lx = nib(len(value))
    return bytes([(d << 4) | ln]) + dx + lx + value


def coap_post(path, payload, content_format, message_id, token):
    out = bytes([0x40 | len(token), 0x02]) + struct.pack("!H", message_id) + token
    last = 0
    for segment in path.split("/"):
        out += coap_option(11 - last, segment.encode()); last = 11
    out += coap_option(12 - last, bytes([content_format]))
    return out + b"\xff" + payload


def coap_ack(message_id, token):
    return bytes([0x60 | len(token), 0x44]) + struct.pack("!H", message_id) + token


def mqtt_len(n):
    out = b""
    while True:
        byte, n = n % 128, n // 128
        out += bytes([byte | (0x80 if n else 0)])
        if not n:
            return out


def mqtt_str(s):
    return struct.pack("!H", len(s)) + s.encode()


def mqtt_publish(topic, payload, packet_id):
    body = mqtt_str(topic) + struct.pack("!H", packet_id) + payload
    return b"\x32" + mqtt_len(len(body)) + body


def mqtt_puback(packet_id):
    return b"\x40\x02" + struct.pack("!H", packet_id)


def mqtt_connect(client_id, keepalive=60):
    body = mqtt_str("MQTT") + b"\x04\x02" + struct.pack("!H", keepalive) + mqtt_str(client_id)
    return b"\x10" + mqtt_len(len(body)) + body


MQTT_CONNACK, MQTT_PINGREQ, MQTT_PINGRESP = b"\x20\x02\x00\x00", b"\xc0\x00", b"\xd0\x00"


def values_json(values):
    return json.dumps(values, separators=(",", ":")).encode()


def per_message_table(t, samples, rng):
    rows = []
    for st in t.SENSOR_TYPES:
        sensor = t.Sensor(st, 1)
        sizes = {}
        for _ in range(samples):
            values, seq = sensor.generateValues(), rng.randint(1, 9999)
            topic = f"fiitmeteo/{st}/1"
            packed = t.pack_values(st, values)
            variants = {
                "JSON PoC": {k: len(json_poc(m)) for k, m in json_messages(st, values, seq).items()},
                "Binary":   {k: len(t.encode(m)) for k, m in binary_messages(t, st, values, seq).items()},
            }
            for proto, kinds in variants.items():
                for kind, n in kinds.items():
                    sizes.setdefault((proto, kind), []).append(n)
            tok = rng.randbytes(2)
            for name, payload, cf in (("CoAP + JSON", values_json(values), 50),
                                      ("CoAP + binary", packed, 42)):
                sizes.setdefault((name, "DATA"), []).append(len(coap_post(topic, payload, cf, seq, tok)))
                sizes.setdefault((name, "ACK (data)"), []).append(len(coap_ack(seq, tok)))
            for name, payload in (("MQTT + JSON", values_json(values)), ("MQTT + binary", packed)):
                sizes.setdefault((name, "DATA"), []).append(len(mqtt_publish(topic, payload, seq)))
                sizes.setdefault((name, "ACK (data)"), []).append(len(mqtt_puback(seq)))
        for (proto, kind), ns in sizes.items():
            transport = TCP if proto.startswith("MQTT") else UDP
            app = statistics.mean(ns)
            rows.append({"sensor": st, "protocol": proto, "message": kind,
                         "app_min": min(ns), "app_max": max(ns), "app_avg": round(app, 1),
                         "with_udp_tcp": round(app + transport, 1),
                         "with_ip": round(app + transport + IPV4, 1),
                         "l2_frame": round(statistics.mean(l2_frame(n, transport) for n in ns), 1)})
    return rows


def breakdown(t, rng):
    st, sensor = "ThermoNode", t.Sensor("ThermoNode", 1)
    values = {"temperature": 21.4, "humidity": 55.0, "dew_point": 12.1, "pressure": 1013.25}
    packed, vjson, topic, tok = t.pack_values(st, values), values_json(values), "fiitmeteo/ThermoNode/1", b"\x12\x34"
    seq = 1234
    poc = json_poc(json_messages(st, values, seq)["DATA"])
    poc_values = len(json.dumps(values)) - 2
    cases = [
        ("JSON PoC", len(poc), poc_values, UDP),
        ("Binary", len(t.encode(binary_messages(t, st, values, seq)["DATA"])), len(packed), UDP),
        ("CoAP + JSON", len(coap_post(topic, vjson, 50, seq, tok)), len(vjson), UDP),
        ("CoAP + binary", len(coap_post(topic, packed, 42, seq, tok)), len(packed), UDP),
        ("MQTT + JSON", len(mqtt_publish(topic, vjson, seq)), len(vjson), TCP),
        ("MQTT + binary", len(mqtt_publish(topic, packed, seq)), len(packed), TCP),
    ]
    rows = []
    for name, app, data, transport in cases:
        lay = layers(app, transport)
        rows.append({"protocol": name, "data": data, "app_header": app - data,
                     "transport_ip": transport + IPV4, "ethernet": lay["eth"] + lay["padding"],
                     "frame": lay["frame"], "app": app,
                     "data_share_app_%": round(100 * data / app, 1),
                     "data_share_frame_%": round(100 * data / lay["frame"], 1),
                     "useful_bits_share_frame_%": round(100 * 47 / (8 * lay["frame"]), 1)})
    return rows


def write_pcap(t, path):
    def ip(proto, src, dst, payload):
        h = struct.pack("!BBHHHBBH4s4s", 0x45, 0, 20 + len(payload), 0, 0x4000, 64, proto, 0,
                        bytes(src), bytes(dst))
        csum = sum(struct.unpack("!10H", h)); csum = (csum & 0xFFFF) + (csum >> 16)
        h = h[:10] + struct.pack("!H", ~csum & 0xFFFF) + h[12:]
        return h + payload

    def eth(packet):
        return b"\x02\x00\x00\x00\x00\x02" + b"\x02\x00\x00\x00\x00\x01" + b"\x08\x00" + packet

    S, C = [192, 168, 1, 10], [192, 168, 1, 20]
    frames = []

    def udp(src, dst, sport, dport, payload):
        frames.append(eth(ip(17, src, dst, struct.pack("!HHHH", sport, dport, 8 + len(payload), 0) + payload)))

    seqs = {"c": 1000, "s": 5000}

    def tcp(fromclient, payload, flags=0x18):
        src, dst, sp, dp = (C, S, 50001, 1883) if fromclient else (S, C, 1883, 50001)
        me, other = ("c", "s") if fromclient else ("s", "c")
        hdr = struct.pack("!HHIIBBHHH", sp, dp, seqs[me], seqs[other], 8 << 4, flags, 64240, 0, 0)
        hdr += b"\x01\x01\x08\x0a" + struct.pack("!II", 1, 1)
        seqs[me] += len(payload)
        frames.append(eth(ip(6, src, dst, hdr + payload)))

    values = {"temperature": 21.4, "humidity": 55.0, "dew_point": 12.1, "pressure": 1013.25}
    from_sensor = ("REGISTER", "DATA", "ACK (keepalive)")

    for msgs, enc, port in ((json_messages("ThermoNode", values, 7), json_poc, 5501),
                            (binary_messages(t, "ThermoNode", values, 7), t.encode, 5500)):
        for kind, m in msgs.items():
            if kind in from_sensor:
                udp(C, S, 50000, port, enc(m))
            else:
                udp(S, C, port, 50000, enc(m))
    packed, vjson = t.pack_values("ThermoNode", values), values_json(values)
    for mid, payload, cf in ((7, vjson, 50), (8, packed, 42)):
        udp(C, S, 50002, 5683, coap_post("fiitmeteo/ThermoNode/1", payload, cf, mid, b"\x12\x34"))
        udp(S, C, 5683, 50002, coap_ack(mid, b"\x12\x34"))
    tcp(True, mqtt_connect("ThermoNode-1")); tcp(False, MQTT_CONNACK)
    for payload in (vjson, packed):
        tcp(True, mqtt_publish("fiitmeteo/ThermoNode/1", payload, 7)); tcp(False, mqtt_puback(7))
    tcp(True, MQTT_PINGREQ); tcp(False, MQTT_PINGRESP)

    with open(path, "wb") as f:
        f.write(struct.pack("<IHHiIII", 0xA1B2C3D4, 2, 4, 0, 0, 65535, 1))
        for i, fr in enumerate(frames):
            f.write(struct.pack("<IIII", 1700000000 + i, 0, len(fr), len(fr)) + fr)


INK, INK2, GRID, SURFACE = "#0b0b0b", "#52514e", "#e4e3df", "#ffffff"
BLUE, MUTED = "#2a78d6", "#b9b8b2"
STACK = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100"]


def style(ax):
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color(GRID)
    ax.tick_params(colors=INK2, length=0)
    ax.xaxis.grid(True, color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)


def charts(per_msg, brk, out):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    plt.rcParams.update({"font.size": 10, "font.family": "DejaVu Sans"})
    order = ["JSON PoC", "Binary", "CoAP + JSON", "CoAP + binary", "MQTT + JSON", "MQTT + binary"]


    fig, axes = plt.subplots(1, 4, figsize=(13, 3.6), sharey=True)
    for ax, st in zip(axes, ["ThermoNode", "WindSense", "RainDetect", "AirQuality"]):
        vals = [next(r["l2_frame"] for r in per_msg if r["sensor"] == st and r["protocol"] == p
                     and r["message"] == "DATA") for p in order]
        bars = ax.barh(order, vals, color=[BLUE if p == "Binary" else MUTED for p in order], height=0.6)
        for b, v in zip(bars, vals):
            ax.text(v + 4, b.get_y() + b.get_height() / 2, f"{v:.0f}", va="center", color=INK, fontsize=9)
        ax.set_title(st, color=INK, fontsize=10, loc="left")
        style(ax); ax.set_xlim(0, max(vals) * 1.2)
    axes[0].invert_yaxis()
    axes[0].set_xlabel("Ethernet frame [B]", color=INK2)
    fig.suptitle("Size of one DATA message on Ethernet (incl. UDP/TCP, IPv4, Ethernet header, FCS, padding)",
                 color=INK, x=0.01, ha="left", fontsize=11)
    fig.tight_layout(); fig.savefig(os.path.join(out, "chart_data_frame.png"), dpi=200); plt.close(fig)


    fig, ax = plt.subplots(figsize=(10, 4.4))
    parts = [("data", "Measured values"), ("app_header", "Application header (type, seq, token, CRC, keys, topic...)"),
             ("transport_ip", "UDP/TCP + IPv4"), ("ethernet", "Ethernet header + FCS + padding")]
    names = [r["protocol"] for r in brk]
    left = [0] * len(brk)
    for (key, label), color in zip(parts, STACK):
        widths = [r[key] for r in brk]
        ax.barh(names, widths, left=left, color=color, height=0.6, label=label,
                edgecolor=SURFACE, linewidth=1.5)
        left = [a + b for a, b in zip(left, widths)]
    for i, r in enumerate(brk):
        ax.text(left[i] + 4, i, f"{r['frame']} B  (values {r['data_share_frame_%']} %)", va="center",
                color=INK, fontsize=9)
    ax.invert_yaxis(); style(ax); ax.set_xlim(0, max(left) * 1.3)
    ax.set_xlabel("bytes in one Ethernet frame", color=INK2)
    ax.set_title("ThermoNode DATA: management vs. data", color=INK, loc="left", fontsize=11)
    ax.legend(loc="upper center", bbox_to_anchor=(0.5, -0.2), ncol=2, frameon=False, fontsize=8,
              labelcolor=INK2)
    fig.tight_layout(); fig.savefig(os.path.join(out, "chart_breakdown.png"), dpi=200); plt.close(fig)


def write_csv(rows, path):
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader(); w.writerows(rows)


def md_table(rows, cols, headers):
    lines = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    for r in rows:
        lines.append("| " + " | ".join(str(r[c]) for c in cols) + " |")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    here = os.path.dirname(os.path.abspath(__file__))
    ap.add_argument("--tester", default=os.path.join(here, "tester.py"))
    ap.add_argument("--out", default=os.path.join(here, "results"))
    ap.add_argument("--samples", type=int, default=100)
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)

    random.seed(26); rng = random.Random(26)
    secrets.token_hex = lambda n: "".join(rng.choice("0123456789abcdef") for _ in range(2 * n))
    secrets.randbits = rng.getrandbits
    t = load_tester(args.tester)

    per_msg = per_message_table(t, args.samples, rng)
    brk = breakdown(t, rng)

    write_csv(per_msg, os.path.join(args.out, "per_message.csv"))
    write_csv(brk, os.path.join(args.out, "breakdown_thermonode.csv"))
    write_pcap(t, os.path.join(args.out, "measurement.pcap"))

    ours = [r for r in per_msg if r["protocol"] in ("JSON PoC", "Binary")]
    table1 = []
    for st in t.SENSOR_TYPES:
        for kind in ["REGISTER", "ACK (register)", "DATA", "ACK (data)", "KEEPALIVE", "ACK (keepalive)",
                     "RETRANSMIT", "REJECT"]:
            j = next(r for r in ours if r["sensor"] == st and r["protocol"] == "JSON PoC" and r["message"] == kind)
            b = next(r for r in ours if r["sensor"] == st and r["protocol"] == "Binary" and r["message"] == kind)
            if kind != "DATA" and st != "ThermoNode":
                continue
            table1.append({"sensor": st if kind == "DATA" else "any", "message": kind,
                           "json_app": j["app_avg"], "bin_app": b["app_avg"],
                           "app_saving": f"{100 - 100 * b['app_avg'] / j['app_avg']:.0f} %",
                           "json_l2": j["l2_frame"], "bin_l2": b["l2_frame"],
                           "l2_saving": f"{100 - 100 * b['l2_frame'] / j['l2_frame']:.0f} %"})
    md = ["# FIITMeteo efficiency measurement", "",
          f"Averages over {args.samples} random messages per sensor (seed 26). "
          "app = application payload, L2 = Ethernet frame incl. header, FCS and padding.", "",
          "## 1. JSON PoC vs. binary, every message", "",
          md_table(table1, list(table1[0]), ["Sensor", "Message", "JSON app [B]", "Binary app [B]", "Saving",
                                             "JSON L2 [B]", "Binary L2 [B]", "Saving"]), "",
          "## 2. ThermoNode DATA: management vs. data", "",
          md_table(brk, ["protocol", "data", "app_header", "app", "transport_ip", "ethernet", "frame",
                         "data_share_app_%", "data_share_frame_%"],
                   ["Protocol", "Values [B]", "App header [B]", "App total [B]", "UDP/TCP+IP [B]",
                    "Ethernet [B]", "Frame [B]", "Values / app", "Values / frame"]), ""]
    with open(os.path.join(args.out, "tables.md"), "w") as f:
        f.write("\n".join(md))
    charts(per_msg, brk, args.out)
    print("\n".join(md))
    print(f"\nFiles written to {args.out}")


if __name__ == "__main__":
    main()