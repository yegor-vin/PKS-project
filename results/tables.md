# FIITMeteo efficiency measurement

Averages over 100 random messages per sensor (seed 26). app = application payload, L2 = Ethernet frame incl. header, FCS and padding.

## 1. JSON PoC vs. binary, every message

| Sensor | Message | JSON app [B] | Binary app [B] | Saving | JSON L2 [B] | Binary L2 [B] | Saving |
|---|---|---|---|---|---|---|---|
| any | REGISTER | 85.9 | 6 | 93 % | 131.9 | 64 | 51 % |
| any | ACK (register) | 74.8 | 11 | 85 % | 120.8 | 64 | 47 % |
| ThermoNode | DATA | 176.3 | 16 | 91 % | 222.3 | 64 | 71 % |
| any | ACK (data) | 49.8 | 7 | 86 % | 95.8 | 64 | 33 % |
| any | KEEPALIVE | 83.9 | 10 | 88 % | 129.9 | 64 | 51 % |
| any | ACK (keepalive) | 94.8 | 11 | 88 % | 140.8 | 64 | 55 % |
| any | RETRANSMIT | 56.8 | 7 | 88 % | 102.8 | 64 | 38 % |
| any | REJECT | 79.8 | 8 | 90 % | 125.8 | 64 | 49 % |
| WindSense | DATA | 178.1 | 14 | 92 % | 224.1 | 64 | 71 % |
| RainDetect | DATA | 177.2 | 14 | 92 % | 223.2 | 64 | 71 % |
| AirQuality | DATA | 152.1 | 15 | 90 % | 198.1 | 64 | 68 % |

## 2. ThermoNode DATA: management vs. data

| Protocol | Values [B] | App header [B] | App total [B] | UDP/TCP+IP [B] | Ethernet [B] | Frame [B] | Values / app | Values / frame |
|---|---|---|---|---|---|---|---|---|
| JSON PoC | 77 | 100 | 177 | 28 | 18 | 223 | 43.5 | 34.5 |
| Binary | 6 | 10 | 16 | 28 | 20 | 64 | 37.5 | 9.4 |
| CoAP + JSON | 72 | 32 | 104 | 28 | 18 | 150 | 69.2 | 48.0 |
| CoAP + binary | 6 | 32 | 38 | 28 | 18 | 84 | 15.8 | 7.1 |
| MQTT + JSON | 72 | 28 | 100 | 52 | 18 | 170 | 72.0 | 42.4 |
| MQTT + binary | 6 | 28 | 34 | 52 | 18 | 104 | 17.6 | 5.8 |

## 3. One hour, 4 sensors, data every 10 s

| Protocol | Frames | Total [B] | Total [kB] | Sent by sensors [kB] | vs. JSON PoC |
|---|---|---|---|---|---|
| JSON PoC | 2888 | 446066 | 446.1 | 311.1 | 100.0 |
| Binary | 2888 | 184832 | 184.8 | 92.4 | 41.4 |
| MQTT + JSON | 4344 | 446123 | 446.1 | 339.0 | 100.0 |
| MQTT + binary | 4344 | 356823 | 356.8 | 249.7 | 80.0 |
| CoAP + JSON | 2880 | 300260 | 300.3 | 208.1 | 67.3 |
| CoAP + binary | 2880 | 210960 | 211.0 | 118.8 | 47.3 |

## 4. One hour, 4 sensors, idle (liveness check only)

| Protocol | Frames | Total [B] | Total [kB] | Sent by sensors [kB] | vs. JSON PoC |
|---|---|---|---|---|---|
| JSON PoC | 5752 | 753439 | 753.4 | 388.2 | 100.0 |
| Binary | 5752 | 368128 | 368.1 | 184.1 | 48.9 |
| MQTT + JSON | 744 | 53223 | 53.2 | 35.3 | 7.1 |
| MQTT + binary | 744 | 53223 | 53.2 | 35.3 | 7.1 |
| CoAP + JSON | 0 | 0 | 0.0 | 0.0 | 0.0 |
| CoAP + binary | 0 | 0 | 0.0 | 0.0 | 0.0 |
