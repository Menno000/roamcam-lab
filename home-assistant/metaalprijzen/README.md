# Metaalprijzen in Home Assistant

*A Home Assistant package + dashboard that estimates what your scrap metal pays out.
Dutch only, like the interface it belongs to.*

Een dashboard dat laat zien wat de stapel metaal in de schuur ongeveer oplevert als je
hem wegbrengt — met de beursprijzen erbij, en een grafiek van het verloop.

De rekensom is bewust simpel:

```
beursprijs (€/kg)  ×  uitbetalingsfactor (%)  ×  jouw kilo's  =  opbrengst
```

De beursprijs komt binnen via een API. De uitbetalingsfactor is wat je er zelf van maakt:
een schroothandel betaalt nooit de beursprijs, en hoeveel eraf gaat verschilt per handelaar
en per soort. Die factor is de knop waar je aan draait tot het klopt met de prijslijst van
je eigen handelaar.

---

## Wat er in zit

| Metaal | Prijsbron |
|---|---|
| Koper | beurs (LME/spot via metals.dev) |
| Aluminium | beurs |
| Lood | beurs |
| Zink | beurs |
| Messing | afgeleid: 60% koper + 40% zink |
| RVS | handmatig — staat niet op de beurs |
| IJzer / staal | handmatig |
| Loodaccu's | handmatig |

Per metaal krijg je drie entiteiten: `sensor.beursprijs_<metaal>`,
`sensor.inkoopprijs_<metaal>` (wat jij per kilo krijgt) en `sensor.opbrengst_<metaal>`
(kilo's × inkoopprijs). Plus `sensor.metaal_opbrengst_totaal` en
`sensor.metaal_gewicht_totaal`.

Entity-ID's volgen uit de sensornamen. Hernoem je iets, pas dan ook het dashboard aan.

---

## Installeren

**1. Package plaatsen**

```sh
cp packages/metaalprijzen.yaml /config/packages/
```

En in `configuration.yaml`:

```yaml
homeassistant:
  packages: !include_dir_named packages
```

**2. API-sleutel** — gratis account op [metals.dev](https://metals.dev), dan in
`secrets.yaml`:

```yaml
metals_dev_api_key: "jouw-sleutel"
```

Het package haalt drie keer per dag alle vier de metalen in één call op: ongeveer
90 calls per maand, net onder de 100 van de gratis laag. Prijzen die maar drie keer per
dag ververst worden zijn hier prima — je brengt je koper niet intraday weg.

**3. Herstarten**, en controleren dat `sensor.beursprijs_koper` een getal laat zien.

**4. Dashboard** — Instellingen → Dashboards → nieuw dashboard → ⋮ → *Onbewerkte
configuratie-editor*, en de inhoud van één van deze twee erin plakken:

- `dashboard/metaalprijzen.yaml` — met grafieken. Vereist
  [apexcharts-card](https://github.com/RomRider/apexcharts-card) via HACS.
- `dashboard/metaalprijzen-basis.yaml` — alleen ingebouwde kaarten, niets te
  installeren. Zelfde gegevens, soberder plaatje.

### Zonder API-sleutel

Laat het hele `rest:`-blok uit het package weg. Vul dan per metaal een vaste prijs in bij
*Instellingen → Vaste prijs per kilo*; alles verder blijft werken, alleen de
`sensor.beursprijs_*` sensoren bestaan dan niet (haal ze ook uit de
`statistics-graph`-kaart in het basisdashboard).

---

## Kalibreren — dit is de belangrijkste stap

De startwaarden voor de uitbetalingsfactor (koper 70%, messing 65%, aluminium 50%,
lood 50%, zink 40%) zijn **schattingen**, geen marktgegeven. Doe dit één keer goed:

1. Pak de prijslijst van je handelaar, of bel ze.
2. Kijk wat `sensor.beursprijs_koper` op dat moment zegt.
3. Factor = betaalde prijs ÷ beursprijs × 100. Betaalt hij €6,10 bij een beurs van €8,60,
   dan is dat 71%.
4. Zelfde trucje voor de rest.

Klopt een metaal principieel niet met de beurs — vervuild koper, kabel waarvan het
kopergehalte verrekend wordt — vul dan gewoon een vaste prijs in. Zodra die boven 0 staat,
negeert het package de beursprijs voor dat metaal.

---

## Waar dit getal *niet* voor staat

- **Het is een schatting, geen offerte.** De handelaar weegt, sorteert en beoordeelt op
  vervuiling; magneet en oog winnen het van je invulveld.
- **Non-ferro inleveren gaat met legitimatie en giraal**, niet contant. Reken erop dat het
  op je rekening komt, niet in je hand.
- **Onder de paar kilo is de rit duurder dan de partij.** Het dashboard weet niet wat de
  benzine kost.
- **Messing is een rekenmodel**, geen notering. 60/40 is gewoon messing; een schroefkoppeling
  of een oude kraan zit er anders in.
- **De ijzerprijs beweegt niet mee** met de beurs in dit package — die staat er handmatig in
  en zul je zelf een paar keer per jaar moeten bijstellen.

---

## Kleuren

Elk metaal heeft in alle grafieken dezelfde kleur. De set is getoetst op
kleurenblindheid (naastliggende paren, lichte achtergrond). Op een donker thema zijn dit
de bijpassende stappen — zoek-vervang ze in `dashboard/metaalprijzen.yaml`:

| Metaal | Licht | Donker |
|---|---|---|
| Koper | `#2a78d6` | `#3987e5` |
| Messing | `#eb6834` | `#d95926` |
| Aluminium | `#1baf7a` | `#199e70` |
| Lood | `#eda100` | `#c98500` |
| Zink | `#e87ba4` | `#d55181` |
| RVS | `#008300` | `#008300` |
| IJzer / staal | `#4a3aa7` | `#9085e9` |
| Loodaccu's | `#e34948` | `#e66767` |

De dure en de goedkope metalen staan in **twee aparte grafieken**. Samen in één plaatje
zou ijzer (~€0,14) plat tegen de nullijn liggen naast koper (~€6), en een tweede y-as
maakt zo'n grafiek onleesbaar in plaats van completer.

---

## Status

Wat gecontroleerd is: de YAML parseert, alle Jinja-templates renderen zonder fout tegen
teststates, en elke entiteit in beide dashboards bestaat in het package.

Wat **niet** gecontroleerd is: dit is nog niet op een draaiende Home Assistant geladen en
er is geen call naar de live API gedaan. Twee dingen om bij de eerste keer op te letten:

- De sleutelnamen in het API-antwoord (`metals.copper` versus `metals.lme_copper`). Het
  template vangt beide varianten af, maar als beide ontbreken blijft de sensor op 0 staan —
  controleer dan het echte antwoord in *Ontwikkelhulpmiddelen → Templates*.
- Of het gratis quotum van metals.dev inderdaad per maand telt zoals hierboven aangenomen.
