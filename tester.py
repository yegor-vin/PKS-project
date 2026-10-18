import socket
import ipaddress
import json
from datetime import timezone, datetime
import threading
import random


class Logger:
    def __init__(self, filename, console=False):
        self.filename = filename
        self.lock = threading.Lock()
        self.console = console

    def _getTime(self):
        now = datetime.now(timezone.utc)
        return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"

    def log(self, type, level, source, text):
        line = f"{self._getTime()}| {type} | {level} | {source} | {text}"
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

sensorSpecification = {
    "ThermoNode": [
        ("temperature", -50.0, 60.0, 1),
        ("humidity", 0.0, 100.0, 1),
        ("dew_point", -50.0, 60.0, 1),
        ("pressure", 800.0, 1100.0, 2),
    ],
    "WindSense": [
        ("wind_speed", 0.0, 50.0, 1),
        ("wind_gust", 0.0, 70.0, 1),
        ("wind_direction", 0, 359, 0),
        ("turbulence", 0.0, 1.0, 1),
    ],
    "RainDetect": [
        ("rainfall", 0.0, 500.0, 1),
        ("soil_moisture", 0.0, 100.0, 1),
        ("flood_risk", 0, 3, 0),
        ("rain_duration", 0, 60, 0),
    ],
    "AirQuality": [
        ("co2", 300, 5000, 0),
        ("ozone", 0.0, 500.0, 1),
        ("air_quality_index", 0, 500, 0),
    ],
}


class Sensor:
    def __init__(self, type, id):
        self.sensorType = type
        self.sensorId = id
        self.stop = threading.Event()
        self.token = None
        self.send = True

    def getType(self):
        return self.sensorType

    def source(self):
        return f"TESTER:{self.sensorType}:{self.sensorId}"


    def registerMessage(self, sequence):
        return {"type": "REGISTER", "sequence": sequence, "sensorType": self.sensorType, "sensorId": self.sensorId}

    def generateValues(self):
        values = {}
        for name, low, high, decimals in sensorSpecification[self.sensorType]:
            if decimals == 0:
                values[name] = random.randint(low, high)
            else:
                values[name] = round(random.uniform(low, high), decimals)
        return values

    def dataMessage(self, sequence, values=None):
        if values is None:
            values = self.generateValues()
        message = {"type": "DATA", "sequence": sequence, "sensorId": self.sensorId, "token": self.token}
        message.update(values)
        return message



SERVER_IP = "127.0.0.1"
SERVER_PORT = 5500
CLIENT_IP = "0.0.0.0"
CLIENT_PORT = 0


class Client:
    def __init__(self, clientIP = CLIENT_IP, clientPort = CLIENT_PORT):
        self.logger = Logger("tester.log")
        self.sensors = {}
        self.server = (SERVER_IP, SERVER_PORT)
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sequence = 0
        self.pending = {}
        self.running = False
        self.lock = threading.Lock()

        try:
            ipaddress.ip_address(clientIP)
            self.ip = clientIP
        except ValueError:
            raise ValueError(f"Invalid IP address: {clientIP}")

        try:
            self.socket.bind((self.ip, clientPort))
            self.port = self.socket.getsockname()[1]
        except OSError as error:

            if error.errno in (48, 98, 10048):
                raise ConnectionError(f"port{clientPort} is already occupied") from error
            else:
                self.socket.close()
                raise error

    def nextSequenceNumber(self):
        with self.lock:
            self.sequence += 1
            return self.sequence

    def setServer(self, serverIp, serverPort):
        self.server = (serverIp, serverPort)
        self.logger.system("INFO", "TESTER", f"Server address set to {serverIp}:{serverPort}")



    def register(self, sensorType, sensorId):
        if sensorType not in sensorSpecification:
            print("Unknown sensor type")
            return
        key = (sensorType, sensorId)
        with self.lock:
            sensor = self.sensors.get(key)
            if sensor is not None and sensor.token is not None:
                return "Sensor already registered"
            sensor = Sensor(sensorType, sensorId)
            self.sensors[key] = sensor
        message = sensor.registerMessage(self.nextSequenceNumber())
        self.sendForSensor(sensor, message)
        return f"REGISTER sent for {sensorType}:{sensorId}"

    def start(self):
        self.running = True
        threading.Thread(target=self.listening, daemon=True).start()

    def listening(self):
        while self.running:
            try:
                reply, _ = self.receive()
            except OSError:

                if not self.running:
                    break
                continue
            if reply is None:
                self.logger.system("WARNING", "TESTER", "Invalid message received")
                continue
            self.handleReply(reply)

    def handleReply(self, reply):
        with self.lock:
            entry = self.pending.pop(reply.get("ack-for"), None)
        if entry is None:
            self.logger.message("TESTER", "Received (unexpected)", reply)
            return

        sensor, original = entry
        self.logger.message(sensor.source(), "Received", reply)
        replyType = reply.get("type")

        if original["type"] == "REGISTER":
            if replyType == "ACK" and reply.get("token"):
                sensor.token = reply["token"]
                self.logger.system("INFO", sensor.source(), f"Registered, token {sensor.token}")
                self.startSending(sensor)

            else:
                self.logger.system("WARNING", sensor.source(),
                                   f"Registration rejected: {reply.get('reason')}")
                with self.lock:
                    self.sensors.pop((sensor.sensorType, sensor.sensorId), None)

        elif original["type"] == "DATA":
            if replyType == "ACK":
                self.logger.system("INFO", sensor.source(), f"Data {original['sequence']} acknowledged")
            else:
                self.logger.system("WARNING", sensor.source(),
                                   f"Data {original['sequence']} rejected: {reply.get('reason')}")


    def startSending(self, sensor):
        threading.Thread(target=self.sendLoop, args=(sensor,), daemon=True).start()

    def sendLoop(self, sensor):
        while not sensor.stop.wait(10):
            if sensor.send and sensor.token:
                self.sendData(sensor)

    def sendData(self, sensor, values=None):
        message = sensor.dataMessage(self.nextSequenceNumber(), values)
        self.sendForSensor(sensor, message)


    def sendForSensor(self, sensor, message):
        with self.lock:
            self.pending[message["sequence"]] = (sensor, message)
        try:
            self.send(message, self.server)
        except OSError as error:
            self.logger.system("ERROR", sensor.source(), f"Send failed: {error}")
            return
        self.logger.message(sensor.source(), "Sent", message)

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


    def toggleSending(self, sensor):
        sensor.send = not sensor.send
        state = 1 if sensor.send else 0
        self.logger.system("INFO", sensor.source(), f"Automatic sending {state}")
        return state

    def sensorList(self):
        with self.lock:
            return list(self.sensors.values())



    def close(self):
        self.running = False
        with self.lock:
            for sensor in self.sensors.values():
                sensor.stop.set()
        self.socket.close()



class Cancel(Exception):
    pass


def ask( prompt):
    text = input(prompt).strip()
    if text.lower() == 'q':
        raise Cancel()
    return text

def askIP(prompt, default):
    while True:
        text = ask(f"{prompt} [{default}] ") or default
        try:
            ipaddress.ip_address(text)
            return text
        except ValueError:
            print("Invalid IP address, try again (example: 192.168.1.15).")


def askPort(prompt, default):
    while True:
        text = ask(f"{prompt} [{default}] ") or str(default)
        if text.isdigit() and 1024 <= int(text) <= 65535:
            return int(text)
        print("Invalid port address, try again, port must be between 1024 and 65535")


def askSensorType():
    types = list(sensorSpecification)
    for i, t in enumerate(types, 1):
        print(f"  {i}) {t}")
    while True:
        text = ask("Sensor type (number): ")
        if text.isdigit() and 1 <= int(text) <= len(types):
            return types[int(text) - 1]
        print("Choose one of the numbers above.")


def askSensorId():
    while True:
        text = ask("Sensor ID (e.g. T-01): ")
        if text and len(text) <= 16 and " " not in text:
            return text
        print("ID must be 1-16 characters without spaces.")

def chooseSensor(client, onlyRegistered=True):
    sensors = [s for s in client.sensorList() if s.token or not onlyRegistered]
    if not sensors:
        print("No registered sensors.")
        return None
    for i, s in enumerate(sensors, 1):
        print(f"  {i}) {s.sensorType}:{s.sensorId}  sending {'ON' if s.send else 'OFF'}")
    while True:
        text = ask("Sensor (number): ")
        if text.isdigit() and 1 <= int(text) <= len(sensors):
            return sensors[int(text) - 1]
        print("Choose one of the numbers above.")


def askValue(name, low, high, decimals):
    while True:
        text = ask(f"  {name} ({low} - {high}, "
                   f"{'integer' if decimals == 0 else f'{decimals} decimal place(s)'}): ")
        try:
            if decimals == 0:
                value = int(text)
            else:
                value = float(text)
                if "." in text and len(text.split(".")[1]) > decimals:
                    print(f"  Use at most {decimals} decimal place(s).")
                    continue
                value = round(value, decimals)
        except ValueError:
            print("  Not a valid number.")
            continue
        if not (low <= value <= high):
            print(f"  Value must be between {low} and {high}.")
            continue
        return value


def showSensors(client):
    sensors = client.sensorList()
    if not sensors:
        print("No sensors.")
        return
    for s in sensors:
        status = f"token {s.token}" if s.token else "waiting for registration ACK"
        print(f"  {s.sensorType}:{s.sensorId}  {status}  sending {'ON' if s.send else 'OFF'}")


def main():
    client = Client()
    client.start()

    while True:
        print(f"\n=== TESTER - server {client.server[0]}:{client.server[1]} ===")
        print("1) Set server IP and port")
        print("2) Register sensor")
        print("3) Register one sensor of each type (T-01, W-01, R-01, A-01)")
        print("4) Show sensors")
        print("5) Turn automatic sending on/off for a sensor")
        print("6) Send custom data")
        print(f"7) Console logging: {'ON' if client.logger.console else 'OFF'} (toggle)")
        print("0) Exit")
        print("(type q in any prompt to cancel)")

        try:
            choice = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            choice = "0"

        try:
            match(int(choice) if choice.isdigit() else -1):
                case 1:
                 ip = askIP("Server IP", client.server[0])
                 port = askPort("Server port", client.server[1])
                 client.setServer(ip, port)

                case 2:
                   sensorType = askSensorType()
                   sensorId = askSensorId()
                   print(client.register(sensorType, sensorId))


                case 3:
                    for sensorType, sensorId in (("ThermoNode", "T-01"), ("WindSense", "W-01"),
                                                 ("RainDetect", "R-01"), ("AirQuality", "A-01")):
                        print(client.register(sensorType, sensorId))

                case 4:
                    showSensors(client)

                case 5:
                    sensor = chooseSensor(client)
                    if sensor:
                        state = client.toggleSending(sensor)
                        print(f"{sensor.sensorType}:{sensor.sensorId} sending {state}")


                case 6:
                    sensor = chooseSensor(client)
                    if sensor:
                        print(f"Enter values for {sensor.sensorType}:{sensor.sensorId}")
                        values = {}
                        for name, low, high, decimals in sensorSpecification[sensor.sensorType]:
                            values[name] = askValue(name, low, high, decimals)
                        client.sendData(sensor, values)
                        print(f"Custom data sent: {values}")

                case 7:
                    client.logger.console = not client.logger.console
                    print(f"Console logging {'ON' if client.logger.console else 'OFF'}")

                case 0:
                    client.close()
                    print("Bye.")
                    break
                case _:
                    print("Unknown option.")

        except Cancel:
            print("Cancelled.")
        except (EOFError, KeyboardInterrupt):
            print("\nCancelled.")


if __name__ == "__main__":
    main()