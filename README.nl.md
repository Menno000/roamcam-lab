# Roamcam Lab

[![License](https://img.shields.io/github/license/Menno000/roamcam-lab)](LICENSE)

**Wat een Hivemapper HDC nog meer kan zijn.**

[Roamcam](https://github.com/Menno000/Roamcam) maakt van zo'n camera een privé dashcam die offline
werkt. Deze repo is de andere helft van het idee: dezelfde hardware die iets heel anders doet — een
reisrecorder, een LoRa-basisstation, een Home Assistant-camera — waarbij je de rol in de interface
kiest en niet door iets anders te installeren.

> Gemaakt in Nederland 🇳🇱, voor iedereen met zo'n camera in een la.

> **⚠️ Hardware:** gebouwd en getest op de **HDC** (met Raspberry Pi CM4). Werkt vermoedelijk ook op
> de **HDC-S**. De nieuwere **Bee** is andere hardware en wordt niet ondersteund.

---

## Het idee: meeliften in plaats van overnemen

Roamcam duwt de originele firmware opzij en stuurt de camera zelf aan. Dat werkt, maar je vecht om
de hardware en je bouwt dingen na die de fabrikant al goed doet.

Roamcam Lab doet het omgekeerde. De originele firmware blijft draaien en wij lezen mee.

Dat blijkt de betere afspraak:

- `libcamera-bridge` levert al **10 fps MJPEG op 2028×1024** in een RAM-buffer, met steeds ongeveer
  **100 seconden geschiedenis** beschikbaar. Het nieuwste beeldje oppakken kost **nul CPU** — de
  frames zijn al geëncodeerd.
- De GPS staat al **twee keer per seconde** uitgelezen in een JSON-bestand: fix, positie, snelheid,
  koers, nauwkeurigheid, aantal satellieten. Idem voor de versnellingsmeter.
- Omdat we de camera niets vragen, loopt de originele opname gewoon door. Live kijken, opnemen en
  timelapse tegelijk, zonder dat ze elkaar in de weg zitten.

Het volledige uitzoekwerk aan de originele firmware — inclusief de valkuilen — staat in
**[docs/VENDOR-FIRMWARE.md](docs/VENDOR-FIRMWARE.md)**. Bouw je zelf iets op zo'n camera, begin daar.

En **[docs/IDEAS.md](docs/IDEAS.md)** verzamelt wat deze hardware blijkt te kunnen maar wat nog
niemand gebouwd heeft — onder andere honderd seconden beeld van vóór een aanrijding, die de
originele firmware al voor je in het geheugen bewaart. Niets op die pagina is gebouwd; per idee
staat erbij wat we echt getest hebben en wat nog gok is.

---

## Rollen

### 🚗 Reis — *werkt*

Een roadtrip-recorder. Hij fotografeert **elke N meter** in plaats van elke N seconden, zodat een
stoplicht niet je halve film opeet en het resultaat gelijkmatig loopt of je nu 30 of 130 reed.
Zonder GPS-fix valt hij vanzelf terug op een tijdinterval.

- Groot live beeld, afstand / aantal foto's / verstreken tijd, één startknop
- Reisarchief met offline kaart, fotoraster, GPX-export en met één klik een film
  (hardware-H.264, 1920×970 mp4)
- Begint desgewenst zelf een reis zodra je wegrijdt en sluit hem na vijf minuten stilstand af
- Klok zet zichzelf gelijk met GPS, tijdzone instelbaar
- Ongeveer **7 MB per kilometer** bij 50 meter interval — ruim 3000 km in de vrije ruimte van een
  standaard toestel

### 📡 Basisstation — *gepland*

Een vaste unit thuis die de auto via LoRa opvangt en doorgeeft aan Home Assistant. Twee SX1262's
die rechtstreeks met elkaar praten — geen LoRaWAN-provider, geen abonnement, geen dekkingskaart.
"Auto is nog twee kilometer weg" is genoeg om een garagedeur te openen.

### 🏠 Home Assistant-camera — *gepland*

Camera voor de oprit of de tuin: bewegingsdetectie op de bestaande beeldstroom, snapshots in Home
Assistant. Kentekenherkenning is *hier* wel realistisch — een vast beeld op bekende afstand — en
vanuit een rijdende auto niet. De objectdetectiemodellen staan al op het toestel.

### 🔙 Achtercamera — *gepland*

Een tweede unit die op het accesspoint van de auto-unit inklimt, zodat één dashboard beide beelden
en één rittenlijst toont.

Zie [ROADMAP.md](ROADMAP.md) voor wat elke rol nog nodig heeft.

---

## Installeren

Op de camera, als root:

```sh
scp -r roles install.sh root@192.168.0.10:/tmp/
ssh root@192.168.0.10 'cd /tmp && sh install.sh'
```

Het installatiescript vraagt welke rol je wilt, zet die op `/mnt/data` en meldt hem aan in de
`cron_config` van de firmware zelf, zodat hij bij het opstarten begint en zichzelf terugzet als hij
ooit omvalt. Geen systemd-unit — die overleeft op dit toestel geen herstart.

Daarna open je **`http://<ip-van-de-camera>:8080`**, of `http://<hostnaam>.local:8080` (mDNS werkt
op deze camera's al vanzelf).

Verwijderen: `sh uninstall.sh`.

---

## Stand van zaken

De rol Reis is af en op hardware nagelopen: de opnamelus, film maken, GPX-export, de offline kaart,
klok gelijkzetten via GPS, en het overleven van een herstart. De andere drie rollen zijn ontworpen
maar nog niet gebouwd.

**Bekende beperkingen, eerlijk:**

- De interface van Reis is voorlopig **alleen Nederlands**. Engels staat op de roadmap.
- De gedeelde kern is nog niet losgetrokken. Met één rol zou je de juiste indeling toch gokken; dat
  gebeurt zodra de tweede rol er is.
- De koude start van de GPS is niet gemeten met echte stroomonderbrekingen. Zie het GNSS-hoofdstuk
  in [docs/VENDOR-FIRMWARE.md](docs/VENDOR-FIRMWARE.md) voor wat we wel en niet weten.

---

## Verwant

- **[Roamcam](https://github.com/Menno000/Roamcam)** — dezelfde hardware als gewone privé dashcam.
  Wil je een dashcam, dan wil je die.

## Licentie

MIT — zie [LICENSE](LICENSE).
