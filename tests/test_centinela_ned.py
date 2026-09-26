#!/usr/bin/env python3
"""test_centinela_ned.py — el centinela NED (surface-en-el-momento, F4.0).

Verifica: primera pasada = línea base (NO avisa, evita burst de correos viejos); correo NED-crítico
NUEVO → avisa nombrando al remitente; idempotente; cambio de FOCO de cumbre → avisa; plazo NED que
entra a hoy → avisa; el marcador NO guarda PII. Aislado en tmp; salida/correo/cumbre/seguimiento
mockeados de forma determinista (cero red, cero egress real)."""
import contextlib
import datetime
import importlib.util
import io
import unittest
from unittest import mock
import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TMP = tempfile.mkdtemp(prefix="centinela_")
os.environ["BTP_STATE_DIR"] = _TMP
os.environ["BTP_REPO"] = _TMP                      # que NO toque casa base
sys.path.insert(0, os.path.join(ROOT, "tools"))
import centinela_ned as cn          # noqa: E402
import correo                       # noqa: E402
import cumbre                       # noqa: E402
import seguimiento                  # noqa: E402
import salida                       # noqa: E402

_sent = []
salida.report_to_titular = lambda text, **k: _sent.append(text)

# Controlables deterministas (el centinela los llama vía `import` → mismo objeto módulo).
correo.es_ned_critico = lambda sender="", subject="": "BIOPSIA" in (sender + subject).upper()
_FOCO = {"id": "biopsia", "titulo": "Re-biopsia L1 Zúrich", "estado": "en_curso", "bloqueo": "cita por confirmar"}
cumbre.foco = lambda: _FOCO
seguimiento.recopilar = lambda: {"items": []}

_pass = _fail = 0


def ok(cond, name):
    global _pass, _fail
    if cond:
        _pass += 1
    else:
        _fail += 1
        print("  ✗ %s" % name)


def buzon(msgs):
    os.makedirs(os.path.join(_TMP, "correo"), exist_ok=True)
    json.dump(msgs, open(os.path.join(_TMP, "correo", "buzon.json"), "w"))


def main():
    # 1. PRIMERA pasada: hay correo crítico, pero NO avisa (fija la línea base)
    buzon([{"id": "m1", "remitente": "{{CONTACTO}} {{CONTACTO}}", "asunto": "biopsia confirmada"},
           {"id": "m2", "remitente": "spam", "asunto": "oferta"}])
    cn.run()
    ok(len(_sent) == 0, "primera pasada = línea base, NO avisa (%d)" % len(_sent))

    # 2. sin cambios → silencio
    cn.run()
    ok(len(_sent) == 0, "sin cambios → silencio")

    # 3. correo NED-crítico NUEVO → avisa nombrando al remitente
    buzon([{"id": "m1", "remitente": "{{CONTACTO}} {{CONTACTO}}", "asunto": "biopsia confirmada"},
           {"id": "m3", "remitente": "{{CONTACTO}} {{CONTACTO}}", "asunto": "re: biopsia neoantígenos"}])
    cn.run()
    ok(len(_sent) == 1, "correo NED-crítico nuevo → 1 aviso (%d)" % len(_sent))
    ok(_sent and "{{CONTACTO}} {{CONTACTO}}" in _sent[-1], "el aviso NOMBRA al remitente")
    ok(_sent and "📩" in _sent[-1], "marca de correo")

    # 4. idempotente: mismo buzón → no re-avisa
    cn.run()
    ok(len(_sent) == 1, "idempotente: no re-avisa el mismo correo")

    # 5. cambio de FOCO de cumbre → avisa
    cumbre.foco = lambda: {"id": "dianas", "titulo": "Identificar dianas", "estado": "en_curso", "bloqueo": "depende de biopsia"}
    cn.run()
    ok(len(_sent) == 2 and "cambio" in _sent[-1].lower(), "cambio de foco de cumbre → avisa")

    # 6. plazo NED que entra a HOY → avisa; y es idempotente
    # El centinela lee seguimiento.json del disco (no el módulo seguimiento) → hay que escribirlo.
    hoy = datetime.date.today().isoformat()
    seg_data = {"items": [
        {"id": "cita-contacto", "titulo": "Cita biopsia Zúrich", "estado": "en_curso", "categoria": "NED", "vence": hoy}]}
    json.dump(seg_data, open(os.path.join(_TMP, "seguimiento.json"), "w"))
    cn.run()
    ok(len(_sent) == 3 and "se acerca" in _sent[-1].lower(), "plazo NED que entra a hoy → avisa")
    cn.run()
    ok(len(_sent) == 3, "plazo idempotente: no re-avisa")

    # 7. el marcador NO guarda PII (solo hashes/ids)
    mark = json.load(open(os.path.join(_TMP, "centinela", "last_seen.json")))
    blob = json.dumps(mark, ensure_ascii=False)
    ok("{{CONTACTO}}" not in blob and "{{CONTACTO}}" not in blob and "biopsia confirmada" not in blob,
       "el marcador NO guarda PII")

    # 8. fail-soft: sin buzón (gate App Password) no peta ni inventa
    os.remove(os.path.join(_TMP, "correo", "buzon.json"))
    seguimiento.recopilar = lambda: {"items": []}
    cumbre.foco = lambda: {"id": "dianas", "titulo": "Identificar dianas", "estado": "en_curso", "bloqueo": "depende de biopsia"}
    antes = len(_sent)
    cn.run()
    ok(len(_sent) == antes, "sin buzón → señal de correo dormida, no peta")

    # 9. anti-duplicado ENTRE daemons: si el poller (correo_imap) ya reclamó el aviso de un correo
    #    vía correo.reclamar_aviso, el centinela NO lo repite (bug 3/7: «📬 Correo nuevo…» +
    #    «📩 Oye, te ha escrito…» del mismo correo). Requiere que el mensaje traiga remitente_email
    #    (la clave del ledger, tal como lo escribe correo_imap en el buzón).
    base = len(_sent)
    ya = {"id": "m9", "remitente": "Elizabeth Vega", "remitente_email": "e.vega@{{CENTRO}}.org",
          "asunto": "re: biopsia BIO121619 {{CENTRO}}"}
    buzon([ya])
    correo.reclamar_aviso("e.vega@{{CENTRO}}.org", "re: biopsia BIO121619 {{CENTRO}}")  # poller avisa 1º
    cn.run()
    ok(len(_sent) == base, "correo ya reclamado por el poller → centinela NO duplica (%d)" % (len(_sent) - base))

    # 9b. el mismo mecanismo NO calla un NED-crítico que nadie reclamó antes
    nuevo = {"id": "m10", "remitente": "Nuevo Lab", "remitente_email": "hola@lab-nuevo.org",
             "asunto": "re: biopsia — hueco de cita"}
    buzon([ya, nuevo])
    cn.run()
    ok(len(_sent) == base + 1 and "Nuevo Lab" in _sent[-1],
       "correo no reclamado por nadie → centinela SÍ avisa (%d)" % (len(_sent) - base))

    _frescura_del_cache()
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(FrescuraNctCache)
    resultado = unittest.TextTestRunner(verbosity=1).run(suite)
    ok(resultado.wasSuccessful(), "frescura por NCT: %d pruebas" % resultado.testsRun)

    print("RESULTADO centinela NED: %d OK, %d fallos" % (_pass, _fail))
    print("✅ CENTINELA EN VERDE" if _fail == 0 else "❌ revisar fallos")
    return _fail


def _escribir_cache(estados, edad_h):
    """Caché de NCTs con una antigüedad concreta (None = sin _ts_consulta)."""
    os.makedirs(os.path.join(_TMP, "centinela"), exist_ok=True)
    raw = {k: {"overallStatus": v} for k, v in estados.items()}
    if edad_h is not None:
        raw["_ts_consulta"] = (datetime.datetime.now()
                               - datetime.timedelta(hours=edad_h)).isoformat(timespec="seconds")
    json.dump(raw, open(cn.NCT_CACHE, "w", encoding="utf-8"))


def _frescura_del_cache():
    """Regresión: el caché llevaba 29 días congelado y el centinela comparaba fósil contra
    fósil sin enterarse, porque `_ts_consulta` la descartaba el filtro de claves y nadie la leía."""
    NCT = "NCT05098210"
    cn._nct_ids_vigilados = lambda: {NCT}          # determinista, sin leer los JSON vivos
    mark = os.path.join(_TMP, "centinela", "last_seen.json")

    def _pasada(estados, edad_h, prev, dia_avisado=None):
        _escribir_cache(estados, edad_h)
        os.makedirs(os.path.dirname(mark), exist_ok=True)
        json.dump({"ts": "2026-01-01T00:00:00", "nct_estados": prev,
                   "nct_rancio_avisado": dia_avisado}, open(mark, "w"))
        return cn.detectar()

    # Caché fresco + cambio real → avisa del cambio, y NO de rancidez.
    av, est = _pasada({NCT: "SUSPENDED"}, 3, {NCT: "RECRUITING"})
    tipos = [t for t, _ in av]
    ok("nct" in tipos and "nct-rancio" not in tipos, "caché fresco + cambio → avisa del cambio")
    ok(est["nct_estados"].get(NCT) == "SUSPENDED", "caché fresco → sella el estado nuevo")

    # Caché rancio → avisa de que NO puede mirarlo, y NO compara (el «sin cambios» mentiría).
    av, est = _pasada({NCT: "RECRUITING"}, 24 * 29, {NCT: "SUSPENDED"})
    tipos = [t for t, _ in av]
    ok("nct-rancio" in tipos, "caché rancio → avisa 'no puedo vigilarlo'")
    ok("nct" not in tipos, "caché rancio → NO compara estados (no finge 'sin cambios')")
    # Y no sella: sellar un fósil enterraría el cambio real bajo nuestra propia línea base.
    ok(est["nct_estados"].get(NCT) == "SUSPENDED", "caché rancio → NO sella el fósil como visto")

    # Dedup: corre cada 150 s, así que el aviso es UNO al día, no 576.
    av, _ = _pasada({NCT: "RECRUITING"}, 24 * 29, {}, dia_avisado=cn._hoy_iso())
    ok("nct-rancio" not in [t for t, _ in av], "rancio ya avisado hoy → no repite")

    # Sin _ts_consulta → se trata como rancio (fail-closed): no sabemos si el dato sirve.
    av, _ = _pasada({NCT: "RECRUITING"}, None, {NCT: "SUSPENDED"})
    ok("nct-rancio" in [t for t, _ in av], "caché sin _ts_consulta → rancio (fail-closed)")

    # `status` deja de decir la verdad tranquilizadora («existe») y dice si el dato SIRVE.
    _escribir_cache({NCT: "RECRUITING"}, 24 * 29)
    _, edad = cn._nct_estados_desde_cache(con_edad=True)
    ok(edad is not None and edad > cn.NCT_CACHE_STALE_H, "la edad del caché se lee de verdad")


# IDs y reloj exclusivamente sintéticos; no se consulta ningún ensayo real.
NCT_A, NCT_B = "NCT99000001", "NCT99000002"
NCT_AHORA = datetime.datetime(2026, 9, 26, 12, tzinfo=datetime.timezone.utc)


class RelojNct(datetime.datetime):
    @classmethod
    def now(cls, tz=None):
        return NCT_AHORA.astimezone(tz) if tz else NCT_AHORA.astimezone().replace(tzinfo=None)


class FrescuraNctCache(unittest.TestCase):
    """Actualizador -> JSON local -> centinela. Solo HTTP y señales ajenas son dobles."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="nct-cache-")
        self.addCleanup(self.tmp.cleanup)
        state = self.tmp.name
        # Módulos nuevos por caso: los dobles del test histórico no contaminan esta suite.
        self.up = self._modulo("actualizar_nct_cache")
        self.cn = self._modulo("centinela_ned")
        for m in (self.up, self.cn):
            m.STATE = state
            m.NCT_CACHE = os.path.join(state, "centinela", "nct_cache.json")
            m.SEGUIMIENTO_JSON = os.path.join(state, "seguimiento.json")
            self._patch(m, "datetime", RelojNct)
        self.cn.MARK = os.path.join(state, "centinela", "last_seen.json")
        self._patch(self.cn, "_correo_estado", lambda: ({}, None))
        self._patch(self.cn, "_foco", lambda: (None, None))
        self._patch(self.cn, "_plazos_seguimiento", lambda: {})
        self._patch(self.cn, "_hoy_iso", lambda: NCT_AHORA.date().isoformat())
        self._vigilar(NCT_A, NCT_B)
        self.cn._guardar_mark({"ts": "2026-01-01T00:00:00",
                               "nct_estados": {NCT_A: "RECRUITING", NCT_B: "RECRUITING"}})
        self.http = self._patch(self.up.urllib.request, "urlopen")
        self.http.side_effect = AssertionError("HTTP sin respuesta simulada")

    def _modulo(self, nombre):
        spec = importlib.util.spec_from_file_location(
            "prueba_" + nombre, os.path.join(ROOT, "tools", nombre + ".py"))
        modulo = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(modulo)
        return modulo

    def _patch(self, obj, nombre, valor=None):
        p = mock.patch.object(obj, nombre) if valor is None else mock.patch.object(obj, nombre, valor)
        result = p.start()
        self.addCleanup(p.stop)
        return result

    def _vigilar(self, *ncts):
        with open(self.up.SEGUIMIENTO_JSON, "w", encoding="utf-8") as f:
            json.dump({"items": [{"id": n} for n in ncts]}, f)

    def _cache(self, edad=72, propia=False):
        fecha = (NCT_AHORA - datetime.timedelta(hours=edad)).isoformat()
        datos = {"_ts_consulta": fecha}
        for n in (NCT_A, NCT_B):
            datos[n] = {"overallStatus": "RECRUITING"}
        if propia:
            datos["_ts_consultas"] = {NCT_A: fecha, NCT_B: fecha}
        self.up._guardar_cache(datos)
        return datos

    def _actualizar(self, respuestas, forzar=False):
        def http(req, timeout=None):
            nct = req.full_url.split("/studies/", 1)[1].split("?", 1)[0]
            estado = respuestas[nct]
            if estado is None:
                raise self.up.urllib.error.URLError("fallo sintetico")
            r = mock.MagicMock()
            r.__enter__.return_value.read.return_value = json.dumps({
                "protocolSection": {"statusModule": {"overallStatus": estado}}}).encode()
            return r
        self.http.side_effect = http
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            resultado = self.up.actualizar(forzar=forzar)
        return resultado, self.up._cargar_cache()

    def _status(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(self.cn.main(["status"]), 0)
        return json.loads(buf.getvalue())

    def test_exito_total_renueva_estado_fecha_y_respeta_ttl(self):
        self._cache()
        _, c = self._actualizar({NCT_A: "SUSPENDED", NCT_B: "COMPLETED"})
        self.assertEqual(c["_ts_consulta"], NCT_AHORA.isoformat())
        self.assertEqual(c["_ts_consultas"][NCT_A], NCT_AHORA.isoformat())
        self.assertEqual(c[NCT_B]["overallStatus"], "COMPLETED")
        self.assertEqual(self.http.call_count, 2)
        self._actualizar({})  # no debe consultar durante el TTL
        self.assertEqual(self.http.call_count, 2)
        self.assertTrue(self._status()["nct_cache_fresco"])

    def test_fallo_total_no_rejuvenece_y_reintenta(self):
        anterior = self._cache()
        _, c = self._actualizar({NCT_A: None, NCT_B: None})
        self.assertEqual(c["_ts_consulta"], anterior["_ts_consulta"])
        self.assertEqual(c[NCT_A]["overallStatus"], "RECRUITING")
        av, _ = self.cn.detectar()
        self.assertIn("nct-rancio", [t for t, _ in av])
        self.assertFalse(self._status()["nct_cache_fresco"])
        self._actualizar({NCT_A: None, NCT_B: None})
        self.assertEqual(self.http.call_count, 4)

    def test_parcial_avisa_del_fresco_y_no_sella_el_rancio(self):
        self._cache()
        self.cn._guardar_mark({"ts": "2026-01-01", "nct_estados": {
            NCT_A: "SUSPENDED", NCT_B: "RECRUITING"}})
        self._actualizar({NCT_A: None, NCT_B: "COMPLETED"})
        av, mark = self.cn.detectar()
        cambios = [txt for t, txt in av if t == "nct"]
        self.assertEqual(len(cambios), 1)
        self.assertIn(NCT_B, cambios[0])
        self.assertIn("nct-rancio", [t for t, _ in av])
        self.assertEqual(mark["nct_estados"][NCT_A], "SUSPENDED")
        self.assertEqual(mark["nct_estados"][NCT_B], "COMPLETED")
        self.assertEqual(self._status()["nct_cache_edades_h"], {NCT_A: 72.0, NCT_B: 0.0})

    def test_recuperacion_no_entierra_el_cambio_y_es_idempotente(self):
        self._cache()
        self._actualizar({NCT_A: None, NCT_B: "COMPLETED"})
        _, mark = self.cn.detectar()
        self.cn._guardar_mark(mark)
        self._actualizar({NCT_A: "SUSPENDED", NCT_B: "COMPLETED"})
        av, mark = self.cn.detectar()
        self.assertEqual(len(av), 1)
        self.assertEqual(av[0][0], "nct")
        self.assertIn(NCT_A, av[0][1])
        self.assertIsNone(mark["nct_rancio_avisado"])
        self.cn._guardar_mark(mark)
        self.assertEqual(self.cn.detectar()[0], [])

    def test_fallo_reciente_conserva_edad_no_invalida_dato_aun_fresco(self):
        self._cache(edad=2, propia=True)
        _, c = self._actualizar({NCT_A: None, NCT_B: "RECRUITING"}, forzar=True)
        self.assertEqual(c["_ts_consultas"][NCT_A], (NCT_AHORA-datetime.timedelta(hours=2)).isoformat())
        self.assertTrue(self._status()["nct_cache_fresco"])
        self.assertNotIn("nct-rancio", [t for t, _ in self.cn.detectar()[0]])

    def test_primera_consulta_fallida_no_inventa_datos(self):
        _, c = self._actualizar({NCT_A: None, NCT_B: None})
        self.assertNotIn("_ts_consulta", c)
        self.assertNotIn(NCT_A, c)
        self.assertFalse(self._status()["nct_cache_fresco"])

    def test_primera_consulta_parcial_no_oculta_nct_ausente(self):
        self._actualizar({NCT_A: None, NCT_B: "COMPLETED"})
        av, mark = self.cn.detectar()
        self.assertIn("nct-rancio", [t for t, _ in av])
        self.assertIn("nct", [t for t, _ in av])
        self.assertEqual(mark["nct_estados"][NCT_A], "RECRUITING")

    def test_primera_pasada_del_centinela_sigue_sin_avisar(self):
        os.remove(self.cn.MARK)
        self._cache()
        self._actualizar({NCT_A: None, NCT_B: "COMPLETED"})
        av, mark = self.cn.detectar()
        self.assertEqual(av, [])
        self.assertNotIn(NCT_A, mark["nct_estados"])
        self.assertEqual(mark["nct_estados"][NCT_B], "COMPLETED")

    def test_compatibilidad_lectura_cache_legado(self):
        for horas in (3, 72):
            with self.subTest(horas=horas):
                self._cache(edad=horas)
                estados, edad = self.cn._nct_estados_desde_cache(con_edad=True)
                self.assertEqual(edad, horas)
                self.assertEqual(estados[NCT_A], "RECRUITING")
                self.assertEqual(self._status()["nct_cache_fresco"], horas <= 48)

    def test_cache_legado_sin_fecha_no_hereda_exito_ajeno(self):
        c = self._cache(); del c["_ts_consulta"]; self.up._guardar_cache(c)
        _, c = self._actualizar({NCT_A: None, NCT_B: "COMPLETED"})
        self.assertIsNone(c["_ts_consultas"][NCT_A])
        self.assertIsNone(self._status()["nct_cache_edades_h"][NCT_A])

    def test_fecha_propia_mala_no_cae_a_global_reciente(self):
        for fecha in (None, "", "ayer", "99999-01-01", True, [], {},
                      (NCT_AHORA + datetime.timedelta(hours=1)).isoformat()):
            with self.subTest(fecha=fecha):
                c = self._cache(edad=1, propia=True)
                c["_ts_consultas"][NCT_A] = fecha; self.up._guardar_cache(c)
                self.assertTrue(self.up._cache_necesita_actualizacion(c, {NCT_A, NCT_B}))
                self.assertFalse(self._status()["nct_cache_fresco"])
                self.assertIn("nct-rancio", [t for t, _ in self.cn.detectar()[0]])

    def test_reincorporado_no_hereda_fecha_global(self):
        self._cache()
        self._vigilar(NCT_B)
        self._actualizar({NCT_B: "COMPLETED"})
        self.assertTrue(self._status()["nct_cache_fresco"])
        self._vigilar(NCT_A, NCT_B)
        c = self.up._cargar_cache()
        self.assertTrue(self.up._cache_necesita_actualizacion(c, {NCT_A, NCT_B}))
        self.assertFalse(self._status()["nct_cache_fresco"])
        self.assertEqual(self._status()["nct_cache_edades_h"][NCT_A], 72.0)

    def test_umbral_consulta_seis_horas(self):
        for horas, esperado in ((0, False), (5.99, False), (6, True), (6.01, True), (-1, True)):
            with self.subTest(horas=horas):
                c = self._cache(edad=horas, propia=True)
                self.assertEqual(self.up._cache_necesita_actualizacion(c, {NCT_A}), esperado)

    def test_umbral_centinela_cuarenta_y_ocho_horas(self):
        for horas, esperado in ((0, True), (47.99, True), (48, True), (48.01, False), (-1, False)):
            with self.subTest(horas=horas):
                self._cache(edad=horas, propia=True)
                self.assertEqual(self._status()["nct_cache_fresco"], esperado)

    def test_husos_horarios_y_z(self):
        fecha = NCT_AHORA - datetime.timedelta(hours=3)
        for ts in (fecha.isoformat(), fecha.isoformat().replace("+00:00", "Z"),
                   fecha.astimezone(datetime.timezone(datetime.timedelta(hours=2))).isoformat()):
            with self.subTest(ts=ts):
                c = self._cache(propia=True)
                for n in (NCT_A, NCT_B):
                    c["_ts_consultas"][n] = ts
                self.up._guardar_cache(c)
                self.assertFalse(self.up._cache_necesita_actualizacion(c, {NCT_A, NCT_B}))
                self.assertEqual(self._status()["nct_cache_edades_h"], {NCT_A: 3.0, NCT_B: 3.0})

    def test_no_repite_aviso_rancio_en_el_mismo_dia(self):
        self._cache()
        self._actualizar({NCT_A: None, NCT_B: "RECRUITING"})
        av, mark = self.cn.detectar()
        self.assertIn("nct-rancio", [t for t, _ in av])
        self.cn._guardar_mark(mark)
        self.assertNotIn("nct-rancio", [t for t, _ in self.cn.detectar()[0]])

    def test_aviso_rancio_se_renueva_al_cambiar_de_dia(self):
        self._cache()
        self._actualizar({NCT_A: None, NCT_B: None})
        self.cn._guardar_mark({"ts": "2026-01-01", "nct_estados": {},
                               "nct_rancio_avisado": "2026-09-25"})
        av, mark = self.cn.detectar()
        self.assertIn("nct-rancio", [t for t, _ in av])
        self.assertEqual(mark["nct_rancio_avisado"], "2026-09-26")
        self.cn._guardar_mark(mark)
        self.assertNotIn("nct-rancio", [t for t, _ in self.cn.detectar()[0]])

    def test_sin_vigilados_no_consulta_ni_modifica_cache(self):
        previo = self._cache()
        self._vigilar()
        self._actualizar({})
        self.http.assert_not_called()
        self.assertEqual(self.up._cargar_cache(), previo)
        self.assertNotIn("nct-rancio", [t for t, _ in self.cn.detectar()[0]])

    def test_cache_corrupto_se_declara_sin_cobertura(self):
        for texto in ("{roto", "[]", "null"):
            with self.subTest(texto=texto):
                with open(self.up.NCT_CACHE, "w", encoding="utf-8") as f:
                    f.write(texto)
                self.assertFalse(self._status()["nct_cache_fresco"])
                self.assertIn("nct-rancio", [t for t, _ in self.cn.detectar()[0]])

    def test_forzar_consulta_aunque_cache_fresco(self):
        self._cache(edad=1, propia=True)
        self._actualizar({NCT_A: "COMPLETED", NCT_B: "RECRUITING"}, forzar=True)
        self.assertEqual(self.http.call_count, 2)

    def test_respeta_fecha_propia_antes_del_fallo(self):
        c = self._cache(edad=72, propia=True)
        c["_ts_consultas"][NCT_A] = (NCT_AHORA-datetime.timedelta(hours=24)).isoformat()
        self.up._guardar_cache(c)
        _, nuevo = self._actualizar({NCT_A: None, NCT_B: "COMPLETED"})
        self.assertEqual(nuevo[NCT_A], c[NCT_A])
        self.assertEqual(nuevo["_ts_consultas"][NCT_A], c["_ts_consultas"][NCT_A])
        self.assertEqual(self._status()["nct_cache_edades_h"][NCT_A], 24.0)

    def test_no_oculta_cambio_de_foco_o_plazo(self):
        self._cache()
        self._actualizar({NCT_A: None, NCT_B: None})
        self._patch(self.cn, "_foco", lambda: ("nuevo", {"titulo": "tarea sintetica", "estado": "en_curso"}))
        self._patch(self.cn, "_plazos_seguimiento", lambda: {"p": ("tarea sintetica", "2026-09-26", "T-0", 0)})
        self.cn._guardar_mark({"ts": "2026-01-01", "foco_sig": "anterior"})
        av, _ = self.cn.detectar()
        self.assertTrue({"cumbre", "plazo", "nct-rancio"}.issubset({t for t, _ in av}))

    def test_respuestas_invalidas_no_rejuvenecen(self):
        for estado in (None, "", "   ", [], {}):
            with self.subTest(estado=estado):
                previo = self._cache()
                _, nuevo = self._actualizar({NCT_A: estado, NCT_B: "COMPLETED"})
                self.assertEqual(nuevo["_ts_consulta"], previo["_ts_consulta"])
                self.assertEqual(nuevo[NCT_A]["overallStatus"], "RECRUITING")

    def test_estado_ausente_o_corrupto_no_cuenta_como_fresco(self):
        for estado in (None, "", "   ", True, 123, [], {}):
            with self.subTest(estado=estado):
                c = self._cache(edad=1, propia=True)
                c[NCT_A]["overallStatus"] = estado
                self.up._guardar_cache(c)
                self.assertTrue(self.up._cache_necesita_actualizacion(c, {NCT_A, NCT_B}))
                self.assertNotIn(NCT_A, self.cn._nct_estados_desde_cache())
                self.assertFalse(self._status()["nct_cache_fresco"])
                self.assertIn("nct-rancio", [t for t, _ in self.cn.detectar()[0]])

    def test_objeto_nct_conserva_forma_historica(self):
        self._cache()
        _, c = self._actualizar({NCT_A: "COMPLETED", NCT_B: "RECRUITING"})
        self.assertEqual(c[NCT_A], {"overallStatus": "COMPLETED"})
        self.assertEqual(c[NCT_B], {"overallStatus": "RECRUITING"})
        self.assertIn(NCT_A, c["_ts_consultas"])

    def test_metadata_no_es_un_ensayo_y_no_cambia_estado(self):
        c = self._cache(edad=1, propia=True)
        c["_extra"] = {"overallStatus": "COMPLETED", "_ts_consulta": "ayer"}
        self.up._guardar_cache(c)
        self.assertEqual(set(self.cn._nct_estados_desde_cache()), {NCT_A, NCT_B})
        self.assertTrue(self._status()["nct_cache_fresco"])

    def test_guardado_fallido_no_reemplaza_cache_bueno(self):
        previo = self._cache()
        self._patch(self.up.os, "replace", mock.Mock(side_effect=OSError("disco sintetico")))
        with self.assertRaises(OSError):
            self._actualizar({NCT_A: "COMPLETED", NCT_B: "COMPLETED"})
        self.assertEqual(self.up._cargar_cache(), previo)


if __name__ == "__main__":
    sys.exit(main())
