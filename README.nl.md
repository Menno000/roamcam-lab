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

### 📡 Basisstation — *de radio werkt, het bereik niet*

Een vaste unit thuis die de auto via LoRa opvangt. Twee SX1262's die rechtstreeks met elkaar praten
— geen LoRaWAN-provider, geen abonnement, geen dekkingskaart, niets om je voor aan te melden.

**Wat er is, en gemeten** (op twee HDC's, september 2026):

- Eigen SX1262-driver, zonder de LoRaWAN-stack van de fabrikant ertussen. 100% ontvangst over
  SF7 t/m SF12 bij BW125/250, in beide richtingen, met een gemeten zendtijd die tot op 1–2 ms met
  de formule overeenkomt
- Versleutelde link met authenticatie; een verkeerde sleutel wordt als `auth=False` gemeld en niet
  stil geaccepteerd
- EU-duty-cycle-grenzen worden getoond en standaard gehandhaafd, en zijn uit te zetten — eigen
  zender, eigen verantwoordelijkheid. Dat handhaven werkt aantoonbaar: een pong werd geweigerd
  toen het 1%-budget op was
- Zendvermogen loopt lineair van −9 tot **21 dBm**. 22 dBm is reproduceerbaar ~3 dB *zwakker* dan
  21 — de eindversterker verzadigt op dit bord, dus 21 dBm is het praktische maximum
- Combi-modus: bewaakt het eigen kanaal elke 40–50 ms en scant daartussen andere kanalen
  (gemeten: 18/18 pings beantwoord, 0,08% van de controles te laat)
- Bereiktest met GPS-posities, een RSSI/SNR-log en een kaart; LED-terugkoppeling per unit
- De kristallen van de twee units schelen ongeveer 5,35 kHz (≈6 ppm), stabiel — geen probleem bij
  BW ≥ 62,5 kHz

**Wat nog niet werkt, eerlijk: het bereik in de auto.** Over 643 positiepings op een echte rit was
de verste beantwoorde ping **21 meter**, en de basis hoorde de auto tot 29 m. Op 21 m was het
signaal −46 tot −51 dBm — zo'n 70 dB marge, in theorie goed voor kilometers — en daarna niets. Dat
patroon past niet bij afscherming: dat zou overal een zwak signaal geven, niet een sterk signaal
met een klif erachter. **De oorzaak is onbekend en wordt nog uitgezocht.**

Ook nog onbewezen: de scanner heeft nog nooit een pakket van een vreemd LoRa-apparaat kunnen
decoderen, en 446 Meshtastic-groeten leverden geen antwoord van een echte Meshtastic-node op.
Unit-naar-unit werkt; samenwerken met andermans apparatuur niet.

De code staat in [`roles/base/`](roles/base). Doorgeven aan Home Assistant moet nog.

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

Daarna open je **`http://<ip-van-de-camera>:8080`**. Op veel van deze camera's werkt
`http://<hostnaam>.local:8080` ook, omdat `avahi-daemon` draait — maar reken er niet op: `avahi`
staat alleen in de *dev*-defconfigs van de publieke firmware, dus een camera met een andere build
heeft het misschien niet. Het IP werkt altijd.

Verwijderen: `sh uninstall.sh`.

---

## Stand van zaken

**Reis** is af en op hardware nagelopen: de opnamelus, film maken, GPX-export, de offline kaart,
klok gelijkzetten via GPS, en het overleven van een herstart.

**Basisstation** is gebouwd en de radio is op twee units bewezen — maar het bereik in de auto is
niet opgelost, en dat staat hierboven eerlijk beschreven. De Home Assistant-camera en de
achtercamera zijn ontworpen en nog niet gebouwd.

Beide rollen delen een kern (`roles/core/roamcam_core.py`) met **46 tests die zonder camera
draaien**. De AES-128 daarin wordt getoetst aan de officiële vectoren uit FIPS-197 en NIST
SP 800-38A in plaats van aan onze eigen eerdere uitkomst, want die code versleutelt echte pakketten
op twee toestellen.

**Bekende beperkingen, eerlijk:**

- De interfaces zijn voorlopig **alleen Nederlands**. Engels staat op de roadmap.
- Het bereik over LoRa stort in na ongeveer 20 meter in een auto, om redenen die we nog niet
  begrijpen. De radio zelf meet goed.
- De scanner heeft nog nooit een pakket van andermans LoRa-apparaat kunnen decoderen, en er heeft
  nog nooit een echte Meshtastic-node geantwoord. Unit-naar-unit werkt; samenwerken niet.
- De koude start van de GPS is niet gemeten met echte stroomonderbrekingen. Zie het GNSS-hoofdstuk
  in [docs/VENDOR-FIRMWARE.md](docs/VENDOR-FIRMWARE.md) voor wat we wel en niet weten.
- mDNS (`<hostnaam>.local`) werkt op onze camera's, maar niet gegarandeerd op die van jou — zie
  Installeren.

---

## Verwant

- **[Roamcam](https://github.com/Menno000/Roamcam)** — dezelfde hardware als gewone privé dashcam.
  Wil je een dashcam, dan wil je die.

## Licentie

MIT — zie [LICENSE](LICENSE).
