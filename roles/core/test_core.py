# -*- coding: utf-8 -*-
"""Tests voor de gedeelde kern. Draait op een laptop, zonder camera.

    cd roles/core
    python -m unittest -v test_core.py

De AES wordt getoetst aan de officiele testvectoren uit FIPS-197 en NIST SP 800-38A, niet aan
onze eigen uitkomst. Dat is het hele punt: deze code versleutelt echte LoRa-pakketten op twee
toestellen, en "het werkte gisteren ook" is geen bewijs.
"""

import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import roamcam_core as core  # noqa: E402


def h(s):
    return bytes.fromhex(s)


class TestAES(unittest.TestCase):
    """AES-128 tegen de officiele vectoren."""

    def test_fips197_appendix_c1(self):
        # FIPS-197, bijlage C.1 -- de standaard AES-128-vector
        key = h("000102030405060708090a0b0c0d0e0f")
        plain = h("00112233445566778899aabbccddeeff")
        verwacht = h("69c4e0d86a7b0430d8cdb78070b4c55a")
        self.assertEqual(core.aes128_encrypt_block(key, plain), verwacht)

    def test_fips197_appendix_b(self):
        # FIPS-197, bijlage B -- het uitgewerkte voorbeeld
        key = h("2b7e151628aed2a6abf7158809cf4f3c")
        plain = h("3243f6a8885a308d313198a2e0370734")
        verwacht = h("3925841d02dc09fbdc118597196a0b32")
        self.assertEqual(core.aes128_encrypt_block(key, plain), verwacht)

    def test_alles_nul(self):
        # bekende vector: nulsleutel, nulblok
        self.assertEqual(
            core.aes128_encrypt_block(bytes(16), bytes(16)),
            h("66e94bd4ef8a2c3b884cfa59ca342b2e"))

    def test_ctr_sp800_38a(self):
        # NIST SP 800-38A, F.5.1 CTR-AES128.Encrypt -- alle vier de blokken
        key = h("2b7e151628aed2a6abf7158809cf4f3c")
        teller = h("f0f1f2f3f4f5f6f7f8f9fafbfcfdfeff")
        plain = h("6bc1bee22e409f96e93d7e117393172a"
                  "ae2d8a571e03ac9c9eb76fac45af8e51"
                  "30c81c46a35ce411e5fbc1191a0a52ef"
                  "f69f2445df4f9b17ad2b417be66c3710")
        verwacht = h("874d6191b620e3261bef6864990db6ce"
                     "9806f66b7970fdff8617187bb9fffdff"
                     "5ae4df3edbd5d35e5b4f09020db03eab"
                     "1e031dda2fbe03d1792170a0f3009cee")
        self.assertEqual(core.aes_ctr(key, teller, plain), verwacht)

    def test_ctr_is_omkeerbaar(self):
        key = h("00112233445566778899aabbccddeeff")
        nonce = core.meshtastic_nonce(0x12345678, 0xdeadbeef)
        bericht = b"Roamcam basisstation, pakketje van 37 bytes."
        cipher = core.aes_ctr(key, nonce, bericht)
        self.assertNotEqual(cipher, bericht)
        self.assertEqual(core.aes_ctr(key, nonce, cipher), bericht)

    def test_ctr_rest_niet_opgevuld(self):
        # een bericht dat geen veelvoud van 16 is mag niet langer worden
        key = bytes(range(16))
        nonce = bytes(16)
        for n in (1, 15, 16, 17, 33):
            self.assertEqual(len(core.aes_ctr(key, nonce, b"x" * n)), n)

    def test_nonce_opbouw(self):
        # pakket-id en afzender little-endian, dan vier nullen
        self.assertEqual(
            core.meshtastic_nonce(1, 2),
            h("0100000000000000") + h("02000000") + bytes(4))


class TestHaversine(unittest.TestCase):
    """Afstand, getoetst aan gevallen waarvan de uitkomst analytisch bekend is."""

    def test_zelfde_punt(self):
        self.assertEqual(core.haversine(52.0, 5.0, 52.0, 5.0), 0.0)

    def test_een_graad_op_de_evenaar(self):
        # 1 graad = R * 1 rad/57,2958 = 111.194,9 m op een bol van 6.371 km
        self.assertAlmostEqual(core.haversine(0, 0, 0, 1), 111194.9, delta=1.0)

    def test_een_graad_breedte(self):
        # langs een meridiaan moet exact hetzelfde uitkomen
        self.assertAlmostEqual(core.haversine(0, 0, 1, 0), 111194.9, delta=1.0)

    def test_tegenovergestelde_punten(self):
        # halve omtrek = pi * R
        self.assertAlmostEqual(core.haversine(0, 0, 0, 180), 20015086.0, delta=2.0)

    def test_symmetrisch(self):
        a = core.haversine(52.3676, 4.9041, 51.9244, 4.4777)
        b = core.haversine(51.9244, 4.4777, 52.3676, 4.9041)
        self.assertAlmostEqual(a, b, places=6)

    def test_amsterdam_rotterdam(self):
        # hemelsbreed ongeveer 57,5 km; ruime marge, dit is een plausibiliteitstest
        d = core.haversine(52.3676, 4.9041, 51.9244, 4.4777)
        self.assertTrue(55000 < d < 60000, "kreeg %.0f m" % d)

    def test_vijftig_meter_wordt_herkend(self):
        # de reis-rol fotografeert elke 50 m; 0,00045 graad breedte is ongeveer 50 m
        d = core.haversine(52.0, 5.0, 52.00045, 5.0)
        self.assertTrue(45 < d < 55, "kreeg %.1f m" % d)


class TestInstellingen(unittest.TestCase):
    def setUp(self):
        self.map = tempfile.mkdtemp()
        self.pad = os.path.join(self.map, "settings.json")

    def tearDown(self):
        shutil.rmtree(self.map, ignore_errors=True)

    def test_ontbrekend_bestand_geeft_standaarden(self):
        std = {"lang": "en", "units": "kmh"}
        cfg = core.load_section(self.pad, "ui", std)
        self.assertEqual(cfg, std)

    def test_heen_en_terug(self):
        std = {"lang": "en", "units": "kmh"}
        cfg = dict(std)
        cfg["lang"] = "nl"
        self.assertTrue(core.save_section(self.pad, "ui", cfg))
        terug = core.load_section(self.pad, "ui", std)
        self.assertEqual(terug["lang"], "nl")
        self.assertEqual(terug["units"], "kmh")

    def test_andere_secties_blijven_staan(self):
        # dit is waar de acht oude functies het om deden: ze delen een bestand
        core.save_section(self.pad, "ui", {"lang": "nl"})
        core.save_section(self.pad, "lora", {"backend": "kpn"})
        core.save_section(self.pad, "wifi", {"home_ssid": "NASA"})
        with open(self.pad) as f:
            rauw = json.load(f)
        self.assertEqual(set(rauw.keys()), {"ui", "lora", "wifi"})
        self.assertEqual(rauw["ui"]["lang"], "nl")
        self.assertEqual(rauw["lora"]["backend"], "kpn")

    def test_onbekende_sleutels_worden_genegeerd(self):
        with open(self.pad, "w") as f:
            json.dump({"ui": {"lang": "nl", "rommel": 1}}, f)
        cfg = core.load_section(self.pad, "ui", {"lang": "en"})
        self.assertEqual(cfg, {"lang": "nl"})
        self.assertNotIn("rommel", cfg)

    def test_kapot_bestand_breekt_niets(self):
        with open(self.pad, "w") as f:
            f.write("{dit is geen json")
        cfg = core.load_section(self.pad, "ui", {"lang": "en"})
        self.assertEqual(cfg, {"lang": "en"})

    def test_target_wordt_ter_plekke_bijgewerkt(self):
        # de bestaande code deelt een modulevariabele; die moet dezelfde dict blijven
        std = {"lang": "en", "units": "kmh"}
        levend = dict(std)
        with open(self.pad, "w") as f:
            json.dump({"ui": {"lang": "nl"}}, f)
        terug = core.load_section(self.pad, "ui", std, target=levend)
        self.assertIs(terug, levend)
        self.assertEqual(levend["lang"], "nl")
        self.assertEqual(levend["units"], "kmh")

    def test_schrijven_naar_onmogelijk_pad_geeft_false(self):
        self.assertFalse(core.save_section(os.path.join(self.map, "weg", "x.json"), "ui", {}))


class TestShell(unittest.TestCase):
    def test_uitvoer(self):
        uit = core.sh('%s -c "print(42)"' % sys.executable)
        self.assertEqual(uit, "42")

    def test_timeout_geeft_leeg(self):
        start = time.time()
        uit = core.sh('%s -c "import time; time.sleep(5); print(1)"' % sys.executable, timeout=1)
        duur = time.time() - start
        self.assertEqual(uit, "")
        self.assertTrue(duur < 4, "timeout werkte niet, duurde %.1f s" % duur)

    def test_langere_timeout_laat_het_wel_af(self):
        uit = core.sh('%s -c "import time; time.sleep(1); print(7)"' % sys.executable, timeout=10)
        self.assertEqual(uit, "7")

    def test_mislukte_opdracht_geeft_leeg(self):
        self.assertEqual(core.sh("ditcommandobestaatniet_abc123"), "")


class TestBestanden(unittest.TestCase):
    def setUp(self):
        self.map = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.map, ignore_errors=True)

    def test_read_met_standaard(self):
        self.assertEqual(core.read(os.path.join(self.map, "weg.txt"), "leeg"), "leeg")

    def test_read_trimt(self):
        p = os.path.join(self.map, "a.txt")
        with open(p, "w") as f:
            f.write("  hallo\n\n")
        self.assertEqual(core.read(p), "hallo")

    def test_json_heen_en_terug(self):
        p = os.path.join(self.map, "a.json")
        self.assertTrue(core.save_json(p, {"a": [1, 2, 3]}))
        self.assertEqual(core.load_json(p), {"a": [1, 2, 3]})

    def test_save_json_overschrijft_op_zijn_plek(self):
        # mag geen nieuw inode maken: de vendor-led-controller volgt /tmp/led.json met inotify
        p = os.path.join(self.map, "led.json")
        core.save_json(p, {"n": 1})
        inode1 = os.stat(p).st_ino
        core.save_json(p, {"n": 2})
        self.assertEqual(os.stat(p).st_ino, inode1)
        self.assertEqual(core.load_json(p), {"n": 2})


class TestKlok(unittest.TestCase):
    def setUp(self):
        self.map = tempfile.mkdtemp()
        self.pad = os.path.join(self.map, "lastclock")

    def tearDown(self):
        shutil.rmtree(self.map, ignore_errors=True)

    def test_klok_van_nu_is_geloofwaardig(self):
        self.assertTrue(core.clock_is_sane())

    def test_2022_is_niet_geloofwaardig(self):
        self.assertFalse(core.clock_is_sane(min_year=3000))

    def test_restore_raakt_een_goede_klok_niet_aan(self):
        core.write_text(self.pad, "%d" % (time.time() + 999999))
        self.assertFalse(core.clock_restore(self.pad))

    def test_restore_zonder_bestand(self):
        self.assertFalse(core.clock_restore(os.path.join(self.map, "weg")))

    def test_save_schrijft_een_tijdstempel(self):
        self.assertTrue(core.clock_save(self.pad))
        self.assertTrue(int(core.read(self.pad)) > 1700000000)

    def test_tijdzone_zetten(self):
        oud = os.environ.get("TZ")
        try:
            core.apply_tz("Europe/Amsterdam")
            self.assertEqual(os.environ["TZ"], "Europe/Amsterdam")
            core.apply_tz(None)
            self.assertEqual(os.environ["TZ"], "UTC")
        finally:
            if oud is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = oud
            try:
                time.tzset()
            except Exception:
                pass


class TestVendor(unittest.TestCase):
    def setUp(self):
        self.map = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.map, ignore_errors=True)

    def test_nieuwste_beeldje_op_naam(self):
        for naam in ("1699999999_000001.jpg", "1700000001_000002.jpg", "1700000000_000003.jpg"):
            with open(os.path.join(self.map, naam), "w") as f:
                f.write("x")
        self.assertTrue(core.newest_frame(self.map).endswith("1700000001_000002.jpg"))

    def test_halve_bestanden_worden_overgeslagen(self):
        # de vendor schrijft .jpg.tmp en hernoemt daarna; een .tmp is nooit compleet
        with open(os.path.join(self.map, "1700000000_000001.jpg"), "w") as f:
            f.write("x")
        with open(os.path.join(self.map, "1799999999_000009.jpg.tmp"), "w") as f:
            f.write("half")
        self.assertTrue(core.newest_frame(self.map).endswith("1700000000_000001.jpg"))

    def test_lege_map(self):
        self.assertIsNone(core.newest_frame(self.map))

    def test_map_bestaat_niet(self):
        self.assertIsNone(core.newest_frame(os.path.join(self.map, "weg")))

    def test_gps_fix_controle(self):
        self.assertTrue(core.gps_has_fix({"fix": "3D"}))
        self.assertTrue(core.gps_has_fix({"fix": "2D"}))
        self.assertTrue(core.gps_has_fix({"fix_ok": True}))
        self.assertFalse(core.gps_has_fix({"fix": "none"}))
        self.assertFalse(core.gps_has_fix({}))
        self.assertFalse(core.gps_has_fix(None))

    def test_oud_bestand_wordt_niet_als_vers_geaccepteerd(self):
        # dit is de bug die ons een klok van 25 seconden verkeerd kostte
        p = os.path.join(self.map, "latest.log")
        core.save_json(p, {"timestamp": "2022-12-20T18:15:00Z", "fix": "3D"})
        self.assertIsNone(core.gps_is_live(p, tries=2, pause=0.05))

    def test_veranderend_bestand_wordt_wel_geaccepteerd(self):
        p = os.path.join(self.map, "latest.log")
        core.save_json(p, {"timestamp": "2026-11-19T21:00:00Z", "fix": "3D"})

        def later():
            time.sleep(0.15)
            core.save_json(p, {"timestamp": "2026-11-19T21:00:01Z", "fix": "3D"})

        t = threading.Thread(target=later)
        t.start()
        try:
            vers = core.gps_is_live(p, tries=10, pause=0.1)
        finally:
            t.join()
        self.assertIsNotNone(vers)
        self.assertEqual(vers["timestamp"], "2026-11-19T21:00:01Z")

    def test_ontbrekend_gps_bestand(self):
        self.assertIsNone(core.gps_is_live(os.path.join(self.map, "weg"), tries=2, pause=0.01))

    def test_eigen_lezer_wordt_gebruikt(self):
        # de reis-rol verrijkt zijn GPS-regel met fix_ok, kmh en satellieten
        p = os.path.join(self.map, "latest.log")
        core.save_json(p, {"timestamp": "t0"})
        aanroepen = []

        def mijn_lezer(pad):
            aanroepen.append(pad)
            d = core.gps_read(pad)
            if d:
                d["verrijkt"] = True
            return d

        # tijdstempel verandert nooit -> None, maar de eigen lezer moet wel gebruikt zijn
        self.assertIsNone(core.gps_is_live(p, tries=2, pause=0.01, reader=mijn_lezer))
        self.assertTrue(len(aanroepen) >= 2)
        self.assertEqual(aanroepen[0], p)

    def test_eigen_lezer_geeft_verrijkte_regel_terug(self):
        p = os.path.join(self.map, "latest.log")
        core.save_json(p, {"timestamp": "t0"})

        teller = {"n": 0}

        def mijn_lezer(pad):
            teller["n"] += 1
            return {"timestamp": "t%d" % teller["n"], "verrijkt": True}

        vers = core.gps_is_live(p, tries=3, pause=0.01, reader=mijn_lezer)
        self.assertIsNotNone(vers)
        self.assertTrue(vers["verrijkt"])


class TestKopieGelijk(unittest.TestCase):
    """De repo roamcam draagt een kopie van dit bestand. Die moet byte-identiek zijn."""

    def test_kopie_in_roamcam_is_gelijk(self):
        hier = os.path.dirname(os.path.abspath(__file__))
        bron = os.path.join(hier, "roamcam_core.py")
        kopie = os.path.abspath(os.path.join(hier, "..", "..", "..", "roamcam", "roamcam_core.py"))
        if not os.path.exists(kopie):
            self.skipTest("repo roamcam staat niet naast deze; kopie niet te controleren")
        with open(bron, "rb") as f:
            a = f.read()
        with open(kopie, "rb") as f:
            b = f.read()
        self.assertEqual(a, b, "roamcam/roamcam_core.py loopt uit de pas met de bron in "
                               "roamcam-lab/roles/core/ -- kopieer hem opnieuw")


if __name__ == "__main__":
    unittest.main(verbosity=2)
