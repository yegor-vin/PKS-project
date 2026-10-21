import socket
import ipaddress
import json
from datetime import timezone, datetime
import threading
import secrets
import time
import zlib
from collections import deque


class Logger:
    def __init__(self, filename, console=False):
        self.filename = filename
        self.lock = threading.Lock()
        self.console = console

    def _getTime(self):
        now = datetime.now(timezone.utc)
        return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"

    def log(self, type, level, source, text):
        line = f"{self._getTime()}| {type} | {level} | {source} | {text} "
        with self.lock:
            try:
                with open(self.filename, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
            except OSError:
                pass
            if self.console:
                print(line)

    def system(self, level, source, text):
        self.log("SYSTEM", level, source, text)

    def message(self, source, direction, message):
        self.log("MESSAGE", "INFO", source, f"{direction}: {json.dumps(message, separators=(',', ':'))}")


def computeCrc(message):
    body = {key: value for key, value in message.items() if key != "crc"}
    data = json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return zlib.crc32(data)


class Server:
    SERVER_IP = "0.0.0.0"
    SERVER_PORT = 5500
    SENSOR_FIELDS = {
        "ThermoNode": ("temperature", "humidity", "dew_point", "pressure"),
        "WindSense": ("wind_speed", "wind_gust", "wind_direction", "turbulence"),
        "RainDetect": ("rainfall", "soil_moisture", "flood_risk", "rain_duration"),
        "AirQuality": ("co2", "ozone", "air_quality_index"),
    }

    def __init__(self, logger, serverIP=SERVER_IP, serverPort=SERVER_PORT):
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.logger = logger
        self.lock = threading.Lock()
        self.running = False
        self.sequence = 0
        self.sessions = {}

        try:
            ipaddress.ip_address(serverIP)
            self.ip = serverIP
        except ValueError:
            raise ValueError(f"Invalid IP address: {serverIP}")

        try:
            self.socket.bind((self.ip, serverPort))
            self.port = serverPort
        except OSError as error:
            self.socket.close()
            if error.errno in (48, 98, 10048, 10013):
                raise ConnectionError(f"port{serverPort} is already occupied") from error
            raise ConnectionError(f"cannot bind {serverIP}:{serverPort} ({error})") from error

    def nextSequenceNumber(self):
        with self.lock:
            self.sequence += 1
            return self.sequence

    def start(self):
        self.running = True
        self.socket.settimeout(0.5)
        self.logger.system("INFO", "SERVER", f"Server listening on {self.ip}:{self.port}")
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def send(self, message, destinationAddress):
        self.socket.sendto(json.dumps(message).encode("utf-8"), destinationAddress)

    def receive(self):
        data, address = self.socket.recvfrom(1200)

        try:
            message = json.loads(data.decode("utf-8"))
            if not isinstance(message, dict):
                return None, address

            return message, address
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None, address

    def run(self):
        while self.running:
            try:
                message, address = self.receive()
            except socket.timeout:
                continue
            except OSError:
                if not self.running:
                    break
                continue
            if message is None:
                self.logger.system("WARNING", "SERVER", f"Invalid message from {address}")
                continue
            self.logger.message("SERVER", "Received", message)
            try:
                self.handleMessage(message, address)
            except Exception as error:
                self.logger.system("ERROR", "SERVER", f"Cannot process message from {address}: {error}")

    def handleMessage(self, message, address):
        mType = message.get("type")
        if mType == "REGISTER":
            self.handleReg(message, address)
        elif mType == "DATA":
            self.handleData(message, address)

    def handleReg(self, message, address):
        sensorType = message.get("sensorType")
        sensorId = message.get("sensorId")
        if sensorType not in self.SENSOR_FIELDS or not sensorId:
            self.reply({"type": "REJECT", "ack-for": message.get("sequence"), "reason": "Invalid sensor"}, address)
            return
        with self.lock:
            duplicate = any(s["sensorType"] == sensorType and s["sensorId"] == sensorId
                            for s in self.sessions.values())
            if not duplicate:
                token = secrets.token_hex(6)
                self.sessions[token] = {"sensorType": sensorType, "sensorId": sensorId,
                                        "address": address, "lastData": time.monotonic(), "lastValues": None,
                                        "token": token, "SuppressACKs": 0, "recentSequences": deque(maxlen=50)}
        if duplicate:
            self.reply({"type": "REJECT", "ack-for": message.get("sequence"), "reason": "Already registered"}, address)
            return
        self.logger.system("INFO", "SERVER", f"Sensor registered: {sensorType}:{sensorId}, token {token}")

        return self.reply({"type": "ACK", "ack-for": message.get("sequence"), "token": token}, address)

    def handleData(self, message, address):
        sequence = message.get("sequence")
        with self.lock:
            session = self.sessions.get(message.get("token"))

        if message.get("crc") != computeCrc(message):
            name = f"{session['sensorType']}:{session['sensorId']}" if session else str(message.get("sensorId"))
            self.logger.system("WARNING", "SERVER",
                               f"Integrity check failed for {name} retransmission requested")
            self.reply({"type": "RETRANSMIT", "ack-for": sequence}, address)
            return

        if session is None:
            self.reply({"type": "REJECT", "ack-for": sequence, "reason": "Invalid token"}, address)
            return

        if message.get("sensorId") != session["sensorId"]:
            self.reply({"type": "REJECT", "ack-for": sequence, "reason": "token doesn't match sensor Id"}, address)
            return

        fields = self.SENSOR_FIELDS[session["sensorType"]]
        if any(field not in message for field in fields):
            self.reply({"type": "REJECT", "ack-for": sequence, "reason": "Missing values"}, address)
            return

        values = {field: message[field] for field in fields}
        name = f"{session['sensorType']}:{session['sensorId']}"
        with self.lock:
            session["lastData"] = time.monotonic()
            session["address"] = address
            duplicate = sequence in session["recentSequences"]
            if not duplicate:
                session["recentSequences"].append(sequence)
                session["lastValues"] = values

            suppress = session["suppressACKs"] > 0
            if suppress:
                session[("SuppressACKs")] -= 1
                left = session["SuppressACKs"]

        if duplicate:
            self.logger.system("INFO", "SERVER", f"Repeated DATA {sequence} from {name} (already stored)")
        if suppress:
                self.logger.system("WARNING", "SERVER",
                                   f"ACK suppressed for {name}, DATA {sequence} ({left} more to suppress)")
                return

        self.reply({"type": "ACK", "ack-for": sequence}, address)

    def reply(self, message, address):
        try:
            message["sequence"] = self.nextSequenceNumber()
            self.send(message, address)
            self.logger.message("SERVER", "Sent", message)
        except OSError as error:
            self.logger.system("ERROR", "SERVER", f"Send to {address} failed: {error}")

    def sessionList(self):
        with self.lock:
            return list(dict(s) for s in self.sessions.values())

    def suppressAcks(self, token, count):
        with self.lock:
            session = self.sessions.get(token)
            if session is None:
               return False
            session["SuppressACKs"] = count
        self.logger.system("INFO", "SERVER",
                           f"Next {count} DATA message(s) of {session['sensorType']}:{session['sensorId']}" f"will not be acknowledged")
        return True

    def close(self):
        self.running = False
        self.thread.join(timeout=2)
        self.socket.close()
        self.logger.system("INFO", "SERVER", "Server stopped")


def askIP(prompt, default):
    while True:
        text = input(f"{prompt} [{default}]: ").strip() or default
        try:
            ipaddress.ip_address(text)
            return text
        except ValueError:
            print("Invalid IP address, try again (example: 192.168.1.15).")


def askPort(prompt, default):
    while True:
        text = input(f"{prompt} [{default}]: ").strip() or str(default)
        if text.isdigit() and 1024 <= int(text) <= 65535:
            return int(text)
        print("Port must be a number between 1024 and 65535.")


def showSensors(server):
    sessions = server.sessionList()
    if not sessions:
        print("No registered sensors.")
        return
    now = time.monotonic()
    for s in sessions:
        ago = now - s["lastData"]
        values = s["lastValues"] if s["lastValues"] is not None else "no data yet"
        print(f"  {s['sensorType']}:{s['sensorId']}  {s['address'][0]}:{s['address'][1]}  "
              f"last data {ago:.0f}s ago  {values}")


def chooseSession(server):
    sessions = server.sessionList()
    if not sessions:
        print("No registered sensors.")
        return None
    for i, s in enumerate(sessions, 1):
        print(f"  {i}) {s['sensorType']}:{s['sensorId']}")
    while True:
        text = input("Sensor (number): ").strip()
        if text.isdigit() and 1 <= int(text) <= len(sessions):
            return sessions[int(text) - 1]
        print("Choose one of the numbers above.")


def askCount(prompt):
    while True:
        text = input(prompt).strip()
        if text.isdigit() and 1 <= int(text) <= 5:
            return int(text)
        print("Enter a number between 1 and 5.")


def main():
    logger = Logger("server.log")
    ip = Server.SERVER_IP
    port = Server.SERVER_PORT
    server = None

    while True:
        state = f"LISTENING on {server.ip}:{server.port}" if server else f"STOPPED (settings {ip}:{port})"
        print(f"\n=== SERVER - {state} ===")
        print("1) Set IP and port")
        print("2) Start listening")
        print("3) Show sensors and latest data")
        print(f"4) Console logging: {'ON' if logger.console else 'OFF'} (toggle)")
        print("5) Stop listening")
        print("6) Do not ACK the next x DATA messages of a sensor")
        print("0) Exit")

        try:
            choice = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            choice = "0"

        try:
            match (int(choice) if choice.isdigit() else -1):
                case 1:
                    if server:
                        print("Stop listening first (option 5).")
                        continue
                    ip = askIP("Server IP (0.0.0.0 = all interfaces)", ip)
                    port = askPort("Server port", port)

                case 2:
                    if server:
                        print("Already listening.")
                        continue
                    try:
                        server = Server(logger, ip, port)
                    except (ValueError, ConnectionError) as error:
                        print(f"Cannot start: {error}")
                        logger.system("ERROR", "SERVER", f"Cannot start: {error}")
                        continue
                    server.start()
                    print(f"Listening on {server.ip}:{server.port}")

                case 3:
                    if server:
                        showSensors(server)
                    else:
                        print("Server is not running.")

                case 4:
                    logger.console = not logger.console
                    print(f"Console logging {'ON' if logger.console else 'OFF'}")

                case 5:
                    if server:
                        server.close()
                        server = None
                    else:
                        print("Server is not running.")
                case 6:
                    if not server:
                        print("Server is not running.")
                        continue
                    session = chooseSession(server)
                    if session:
                        count = askCount("How many DATA messages (incl. repeats) not to ACK (1-20):")
                        if server.suppressAcks(session["token"], count):
                            print(f"Next {count} DATA message(s) of {session['sensorType']}:{session['sensorId']} "
                                  f"will not be acknowledged.")
                    else:
                        print("Sensor is no longer registered.")
                case 0:
                    if server:
                        server.close()
                    print("Bye.")
                    break
                case _:
                    print("Unknown option.")
        except (EOFError, KeyboardInterrupt):
            print("\nCancelled.")


if __name__ == "__main__":
    main()
