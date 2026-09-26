#!/usr/bin/env python3
"""tools/centinela_ned.py — el SEGUNDO reloj de Vega: surface-en-el-momento (F4.1).

`vega_gate` decide "¿PIENSO?" (corre el LLM si hay novedad). El centinela decide "¿INTERRUMPO YA?":
mira, cada ~2-3 min, SOLO señales NED-CRÍTICAS y, si hay una NUEVA, avisa a {{TITULAR}} AL MOMENTO — él
mismo, SIN LLM, $0. Porque el momento crítico (la confirmación de la biopsia por correo, el "sí" de
un coordinador de ensayo) NO puede depender del saldo ni de que un modelo decida.

Cierra el casi-fallo (25/6): la biopsia se confirmó por correo y no se avisó EN EL MOMENTO.
Amplía el watchdog (26/6): ahora vigila también los PLAZOS de la agenda y el ESTADO de los ensayos.

Señales NED-CRÍTICAS (todas LOCALES, deterministas, $0):
  1. Correo NUEVO que casa `correo.es_ned_critico(remitente, asunto)` (buzon.json, IMAP RO). Si el
     buzón aún no existe (gate App Password), esta señal queda DORMIDA (fail-soft, no inventa).
  2. Cambio en el FOCO de la ruta (`cumbre.foco()`: id/estado/bloqueo) → un eslabón se resolvió o bloqueó.
  3. Plazos de seguimiento.json que entran a la ventana T-7/T-3/T-1/T-0 (TODOS los hilos no
     cerrados con campo de fecha, no solo los marcados NED). Anti-spam por clave estable
     sha(id + "@" + fecha + "@" + hito) → 1 aviso por hito (T-7, T-3, T-1, T-0), no re-spamea.
  4. Cambio de overallStatus de NCTs en seguimiento/cumbre. El centinela lee el caché local
     state/centinela/nct_cache.json (actualizado por tools/actualizar_nct_cache.py que usa BioMCP,
     fuera del ciclo $0). Si el estado cambia respecto al marcador, o pasa a estado de alarma
     (NOT_YET_RECRUITING/COMPLETED/SUSPENDED/TERMINATED/WITHDRAWN), avisa. Trata el caché como
     DATO EXTERNO (anti-inyección): solo extrae el campo overallStatus, nada más.

Idempotente: state/centinela/last_seen.json guarda HASHES/IDs vistos (NUNCA contenido/PII). Solo
avisa de lo que NO había avisado. PRIMERA pasada = fija la línea base, NO avisa (evita un burst de
correos viejos). El aviso sale por salida.report_to_titular (urgente=False → respeta el SILENCIO
NOCTURNO: se retiene y sale en el resumen de la mañana; solo 🔴 código-rojo lo atraviesa, y eso es
otro mecanismo). No nombra a terceros (es_ned_critico filtra a remitentes clínicos/científicos
por construcción).

Muro / privacidad: $0, 0 LLM, 0 red propia; ÚNICO egress = salida.py; no persiste PII (hashes/ids,
no cuerpos). Resuelve el estado a CASA BASE (BTP_REPO o ~/claudecode), como el resto del estado vivo.
Espejo de tools/vega_gate.py.

Uso:
  python3 tools/centinela_ned.py run     # detecta y AVISA lo NED-crítico nuevo; sella "visto"
  python3 tools/centinela_ned.py check   # rc 0 = hay algo que avisar (NO avisa); rc 1 = nada
  python3 tools/centinela_ned.py status  # qué ve, sin tocar el marcador ni avisar
"""
import hashlib
import json
import os
import re
import sys
from datetime import datetime, date, timedelta

# El estado vivo (marcador) y las fuentes viven en CASA BASE (gitignored, no viaja a worktrees).
REPO = os.environ.get("BTP_REPO") or os.path.expanduser("~/claudecode")
STATE = os.environ.get("BTP_STATE_DIR") or os.path.join(REPO, "tools", "state")
MARK = os.path.join(STATE, "centinela", "last_seen.json")
BUZON = os.path.join(STATE, "correo", "buzon.json")            # tools/correo_imap.py (IMAP RO)
NCT_CACHE = os.path.join(STATE, "centinela", "nct_cache.json") # tools/actualizar_nct_cache.py
SEGUIMIENTO_JSON = os.path.join(STATE, "seguimiento.json")
MAX_ASUNTO = 80

# Estados de ensayo que generan alerta (además de cualquier cambio inesperado)
NCT_ESTADOS_ALARMA = {
    "COMPLETED", "SUSPENDED", "TERMINATED", "WITHDRAWN",
    "NOT_YET_RECRUITING",
}
# Estado que NO alarma aunque sea nuevo (recruiting activo = bueno)
NCT_ESTADOS_OK = {"RECRUITING", "ACTIVE_NOT_RECRUITING", "ENROLLING_BY_INVITATION"}

# Edad máxima del caché de estados de ensayo. El refresco lo hace tools/actualizar_nct_cache.py
# (daemon com.btp.nct-cache, diario) — este módulo NO toca la red. 48 h = dos cadencias, margen
# para un fallo suelto. Si se pasa: NO comparamos (contrastar contra un fósil produce un «sin
# cambios» que no significa nada) y lo decimos. El 25-jul-2026 el caché llevaba 29 días
# congelado y `status` seguía diciendo que existía, que se leía como «todo bien».
NCT_CACHE_STALE_H = 48

# Hitos de plazo (días de antelación → etiqueta para la clave de dedup)
# Los NEGATIVOS son el arreglo del 25-jul-26: el centinela dejaba de mirar un plazo EXACTAMENTE
# cuando vencía (`if dias < 0: continue`), y un plazo vencido aprieta más, no menos. Sobre el
# seguimiento.json vivo había 21 hilos vencidos abiertos y el segundo reloj —el que interrumpe al
# momento y no depende del saldo— callaba en todos. El parte de las 8:12 sí los recoge, pero compite
# con todo lo demás. Cada hito tiene su propia clave de dedup, así que esto no re-spamea: son 3
# avisos más por hilo a lo largo de un mes, no uno por pasada.
HITOS_PLAZO = [  # ordenado de menor a mayor: el 1er umbral >= dias_restantes es el hito
    (-7, "T+7"),   # vencido hace una semana
    (-3, "T+3"),
    (-1, "T+1"),   # se pasó ayer
    (0, "T-0"),
    (1, "T-1"),
    (3, "T-3"),
    (7, "T-7"),
]

# Suelo del retraso: pasado un mes, un plazo que sigue abierto ya no es un plazo, es otra cosa (o se
# reprogramó y nadie tocó la ficha). Seguir latiendo sobre él es ruido, y el ruido tapa la señal.
VENCIDO_SUELO_DIAS = 30

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def _sha(s):
    return hashlib.sha256(str(s).encode("utf-8", "replace")).hexdigest()[:16]


# ── marcador "último visto" (hashes/ids, NUNCA contenido) ────────────────────────
def _cargar_mark():
    """dict del marcador, o {} si no existe/corrupto. Corrupto→{} = re-detecta (fail-LOUD: para
    NED es mejor re-avisar que perder). Pero la PRIMERA pasada (marker sin 'ts') NO avisa."""
    try:
        with open(MARK, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception:
        return {}


def _guardar_mark(d):
    os.makedirs(os.path.dirname(MARK), mode=0o700, exist_ok=True)
    tmp = MARK + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)
    os.replace(tmp, MARK)


# ── señales (deterministas, $0) ──────────────────────────────────────────────────
def _correo_estado():
    """(criticos, buzon_ids): `criticos` = {id: (remitente, asunto)} de los correos NED-críticos
    presentes ahora; `buzon_ids` = set de TODOS los ids del buzón (para podar el marcador). Si no
    hay buzón (gate App Password) o es ilegible → ({}, None): señal dormida, no inventa."""
    try:
        import correo
    except Exception:
        return {}, None
    try:
        with open(BUZON, encoding="utf-8") as f:
            d = json.load(f)
    except FileNotFoundError:
        return {}, None
    except Exception:
        return {}, None
    msgs = d if isinstance(d, list) else next(
        (d[k] for k in ("mensajes", "messages", "items", "correos", "buzon") if isinstance(d.get(k), list)), [])
    criticos, buzon_ids = {}, set()
    for m in msgs:
        if not isinstance(m, dict):
            continue
        mid = str(m.get("id") or m.get("uid") or m.get("message_id") or m.get("messageId")
                  or _sha(json.dumps(m, sort_keys=True)))
        buzon_ids.add(mid)
        rem = str(m.get("remitente") or m.get("from") or m.get("sender") or m.get("de") or "")
        asu = str(m.get("asunto") or m.get("subject") or m.get("titulo") or "")
        rem_email = str(m.get("remitente_email") or "")   # dirección exacta = clave del ledger compartido
        try:
            # Lista de RUIDO (filtros de Gmail): spam/newsletters/recibos NO suben al triaje.
            # Fail-safe TOTAL: sin config / enabled=false → es_ruido()==False → no filtra nada.
            # NED-crítico gana siempre dentro de es_ruido (no perdemos lo clínico); egarante nunca es ruido.
            if correo.es_ruido(rem, asu):
                continue
            if correo.es_ned_critico(rem, asu):
                criticos[mid] = (rem, asu, rem_email)
        except Exception:
            pass
    return criticos, buzon_ids


def _foco():
    """(hash, foco_dict) del eslabón actual de la ruta, o (None, None)."""
    try:
        import cumbre
        f = cumbre.foco()
    except Exception:
        return None, None
    if not isinstance(f, dict):
        return None, None
    return _sha("|".join([str(f.get("id")), str(f.get("estado")), str(f.get("bloqueo"))[:200]])), f


def _plazos_seguimiento():
    """{clave_hito: (titulo, fecha_iso, hito_label, dias_restantes)} de hilos con fecha que
    entran a alguno de los hitos T-7/T-3/T-1/T-0, no cerrados.

    Clave de dedup: sha(id + "@" + fecha + "@" + hito_label) → estable mientras no cambie la
    fecha. Un mismo hilo puede generar hasta 4 claves distintas (una por hito) pero cada clave
    solo se avisa UNA vez. Determinista, $0, sin LLM.

    Lee directamente de seguimiento.json en STATE (no importa el módulo seguimiento para evitar
    dependencias cruzadas; el JSON es la fuente de verdad del estado vivo).
    """
    hoy = date.today()
    out = {}
    try:
        with open(SEGUIMIENTO_JSON, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return out

    items = data if isinstance(data, list) else data.get("hilos") or data.get("items") or []
    for it in items:
        if not isinstance(it, dict):
            continue
        estado = str(it.get("estado", "")).lower()
        if estado in ("hecho", "done", "cerrado"):
            continue

        # Extrae la fecha del primer campo de fecha encontrado
        fv = None
        for campo in ("vence", "plazo", "deadline", "fecha", "cuando", "due"):
            v = it.get(campo)
            if v:
                try:
                    fv = datetime.fromisoformat(str(v)[:10]).date()
                except Exception:
                    fv = None
                if fv is not None:
                    break
        if fv is None:
            continue

        dias = (fv - hoy).days
        # Ventana T-7 → T+30: lo que ya pasó SÍ genera aviso (ver HITOS_PLAZO), con suelo al mes.
        if dias < -VENCIDO_SUELO_DIAS or dias > 7:
            continue

        iid = str(it.get("id") or it.get("titulo") or "")
        titulo = str(it.get("titulo") or "")[:80]

        for umbral, label in HITOS_PLAZO:
            if dias <= umbral:
                # Clave estable para este hilo en este hito concreto
                clave = _sha(iid + "@" + fv.isoformat() + "@" + label)
                if clave not in out:
                    out[clave] = (titulo, fv.isoformat(), label, dias)
                break  # solo el hito más cercano que aplica por pasada

    return out


def _hoy_iso():
    return date.today().isoformat()


def _dia_rancio_sellar(mark, rancio):
    """Sella el DÍA del aviso de caché rancio (dedup: uno al día, no uno cada 150 s). Si el
    caché vuelve a estar fresco se limpia, para que el próximo bache vuelva a avisar."""
    if not rancio:
        return None
    # Si sigue rancio al cambiar de día, la primera alerta del nuevo día debe sellar HOY.
    # Conservar la fecha de ayer haría que el centinela reavisara en cada pasada (~150 s).
    return _hoy_iso()


def _nct_cache_edad_h(raw):
    """Horas desde la última consulta real a clinicaltrials.gov, o None si no se puede saber.
    `_ts_consulta` la escribe actualizar_nct_cache.py; el filtro de claves de abajo la descarta
    (no tiene pinta de NCT), así que antes NADIE la leía nunca: el caché podía llevar meses
    congelado y el centinela comparaba fósil contra fósil sin enterarse."""
    ts = (raw or {}).get("_ts_consulta")
    if not ts:
        return None
    try:
        d = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except Exception:
        return None
    edad = (datetime.now(d.tzinfo) - d).total_seconds() / 3600.0
    return edad if edad >= 0 else None   # una fecha futura no demuestra frescura


def _nct_estados_desde_cache(con_edad=False, edades=None):
    """{nct_id: estado_str} leído del caché local (DATO EXTERNO: solo extrae overallStatus).
    Devuelve {} si el caché no existe o está corrupto. Anti-inyección: no interpreta el contenido
    más allá de extraer el string del campo 'overallStatus'.

    `con_edad=True` devuelve (estados, edad máxima|None); None si alguna edad es desconocida.
    `edades`, si se pasa un dict, recibe la edad POR NCT de esta misma lectura. Los registros
    legados sin fecha propia usan la global; una fecha propia ilegible NO cae a la global."""
    if edades is not None:
        edades.clear()
    try:
        with open(NCT_CACHE, encoding="utf-8") as f:
            raw = json.load(f)
    except Exception:
        return ({}, None) if con_edad else {}
    if not isinstance(raw, dict):
        return ({}, None) if con_edad else {}
    resultado, por_nct = {}, {}
    fechas = raw.get("_ts_consultas")
    if not isinstance(fechas, dict):
        fechas = {}
    for nct_id, info in raw.items():
        # nct_id debe tener pinta de NCT (prefijo + 6+ dígitos); ignora cualquier otra cosa
        nct_id = str(nct_id).strip()
        if not re.match(r'^NCT\d{6,}$', nct_id, re.IGNORECASE):
            continue
        # Extrae SOLO overallStatus; ignora cualquier otro campo (anti-inyección)
        if not isinstance(info, dict):
            continue
        status = info.get("overallStatus")
        if isinstance(status, str) and status.strip():
            clave = nct_id.upper()
            resultado[clave] = status.strip()[:64]
            fuente_fecha = ({"_ts_consulta": fechas[clave]}
                            if clave in fechas else raw)
            por_nct[clave] = _nct_cache_edad_h(fuente_fecha)
    if edades is not None:
        edades.update(por_nct)
    valores = list(por_nct.values())
    edad = max(valores) if valores and None not in valores else None
    return (resultado, edad) if con_edad else resultado


def _nct_ids_vigilados():
    """Extrae los NCTs que aparecen en seguimiento.json y cumbre.json. Solo NCTxxxxxxxx.
    Determinista, sin red."""
    ids = set()
    _re_nct = re.compile(r'\bNCT\d{6,}\b', re.IGNORECASE)

    # seguimiento.json
    try:
        with open(SEGUIMIENTO_JSON, encoding="utf-8") as f:
            raw = f.read()
        ids.update(m.upper() for m in _re_nct.findall(raw))
    except Exception:
        pass

    # cumbre.json
    try:
        cumbre_path = os.path.join(STATE, "cumbre.json")
        with open(cumbre_path, encoding="utf-8") as f:
            raw = f.read()
        ids.update(m.upper() for m in _re_nct.findall(raw))
    except Exception:
        pass

    return ids


# ── decisión + aviso ─────────────────────────────────────────────────────────────
def detectar():
    """Devuelve (avisos, estado_actual). `avisos` = lista de (tipo, texto) NED-críticos NUEVOS
    (vacía en la primera pasada). `estado_actual` = lo que hay que sellar como visto."""
    mark = _cargar_mark()
    primera = not mark.get("ts")
    vistos_mail = set(mark.get("emails_avisados") or [])
    vistos_plazo = set(mark.get("plazos_avisados") or [])
    nct_estados_prev = dict(mark.get("nct_estados") or {})   # {NCT_ID: estado_str}
    foco_prev = mark.get("foco_sig")

    criticos, buzon_ids = _correo_estado()
    foco_hash, foco = _foco()
    plazos = _plazos_seguimiento()
    nct_edades = {}
    nct_actuales = _nct_estados_desde_cache(edades=nct_edades)
    nct_vigilados = _nct_ids_vigilados()
    nct_frescos = {n for n in nct_vigilados if nct_edades.get(n) is not None
                   and nct_edades[n] <= NCT_CACHE_STALE_H}
    nct_rancios = nct_vigilados - nct_frescos
    nct_rancio = bool(nct_rancios)

    def _nombre_remitente(raw):
        """Saca el nombre visible de un 'from'. Si hay nombre propio ("{{CONTACTO}} {{CONTACTO}} <y@h.com>")
        devuelve solo el nombre; si no, la parte local del email sin arroba ni dominio."""
        raw = (raw or "").strip()
        # Patron "Nombre Apellido <email@dominio>" o "Nombre <email>"
        m_nombre = re.match(r'^"?([^"<@][^"<]{1,50}?)"?\s*<', raw)
        if m_nombre:
            nombre = m_nombre.group(1).strip().strip('"')
            if nombre and not nombre.lower().startswith(("noreply", "no-reply", "donotreply")):
                return nombre
        # Fallback: parte local del email (ej. "contacto.contacto" -> "contacto.contacto")
        m_email = re.search(r"([\w.+-]+)@", raw)
        if m_email:
            return m_email.group(1).replace(".", " ").replace("_", " ").title()
        return raw or "alguien"

    avisos = []
    if not primera:
        # --- Señal 1: correos NED-críticos nuevos ---
        for mid in (set(criticos) - vistos_mail):
            rem, asu, rem_email = criticos[mid]
            # Anti-duplicado ENTRE daemons: el poller (correo_imap) avisa del MISMO correo con
            # «📬 Correo nuevo…» pasando por correo.reclamar_aviso (ledger compartido con flock).
            # Reclamamos aquí la misma clave: si el poller ya avisó (o avisa después), solo sale UN
            # mensaje. El sellado de `emails_avisados` ocurre igual (emails_sellar cubre todo
            # `criticos`), así que no reintentamos aunque nos saltemos el aviso. Fix 3/7: doble aviso
            # («📬 Correo nuevo…» + «📩 Oye, te ha escrito…») del mismo correo (Elizabeth Vega/BIO121619).
            if rem_email:
                try:
                    import correo
                    if not correo.reclamar_aviso(rem_email, asu):
                        continue
                except Exception:
                    pass   # fail-open: si no alcanzo el ledger, prefiero avisar de más que callar lo NED-crítico
            quien = _nombre_remitente(rem)
            asu_limpio = (asu or "").strip()
            if asu_limpio and quien.lower() not in asu_limpio.lower():
                cola = " sobre «%s»" % asu_limpio[:MAX_ASUNTO]
            else:
                cola = ""
            avisos.append(("correo",
                           "📩 Oye, te ha escrito %s%s. Vale la pena verlo." % (quien, cola)))

        # --- Señal 2: cambio de foco ---
        if foco_hash and foco_prev is not None and foco_hash != foco_prev:
            titulo_foco = str(foco.get("titulo") or "")[:80]
            estado_foco = foco.get("estado") or ""
            avisos.append(("cumbre",
                           "Hay un cambio en lo que mas aprieta ahora mismo: «%s» (%s)." % (titulo_foco, estado_foco)))

        # --- Señal 3: plazos de la agenda (T+7/T+3/T+1 vencidos · T-0/T-1/T-3/T-7 por venir) ---
        vencidos = []
        for clave, (titulo, fecha, hito, dias) in plazos.items():
            if clave in vistos_plazo:
                continue
            if dias < 0:
                vencidos.append((dias, titulo, fecha))
                continue
            if dias == 0:
                when = "es HOY"
            elif dias == 1:
                when = "es manana"
            else:
                when = "es en %d dias (%s)" % (dias, fecha)
            avisos.append(("plazo",
                           "Plazo %s — «%s» se acerca: %s. No lo pierdas de vista." % (hito, titulo, when)))

        # Los VENCIDOS van en UN SOLO aviso, no uno por hilo. El día que esto se encendió había 15
        # esperando: quince Telegram seguidos no son quince avisos, son cero (los lee como ruido y
        # deja de mirar el canal). Va la lista COMPLETA, sin top-N — un recorte silencioso se leería
        # como «eso es todo lo que hay», que es justo la mentira que este centinela existe para no
        # contar. Cada hilo sigue teniendo su clave de dedup propia, así que un vencido ya avisado no
        # vuelve a entrar hasta su siguiente hito.
        if vencidos:
            vencidos.sort()   # el más pasado de fecha, primero
            lineas = "\n".join(
                "· «%s» — vencio %s (%s)" % (t, "ayer" if d == -1 else "hace %d dias" % (-d), f)
                for d, t, f in vencidos)
            avisos.append(("plazo-vencido",
                           "%d plazo(s) VENCIDO(s) y todavia abiertos:\n%s\n\n"
                           "De cada uno: o se hace, o se le pone fecha nueva." % (len(vencidos), lineas)))

        # --- Señal 4: cambio de estado de NCTs ---
        # Fail-loud: con el caché rancio NO se compara. «Sin cambios» contra un dato de hace
        # semanas no significa nada, y es justo el silencio que hace invisible que un ensayo
        # haya pasado a SUSPENDED/TERMINATED. Preferimos decir «no pude mirarlo».
        if nct_rancio and nct_vigilados and mark.get("nct_rancio_avisado") != _hoy_iso():
            avisos.append(("nct-rancio",
                           "No puedo vigilar el estado reciente de %d de %d ensayos: faltan "
                           "datos o su última consulta exitosa supera %d h (%s). Revisa el daemon "
                           "com.btp.nct-cache o lanza `python3 tools/actualizar_nct_cache.py --forzar`."
                           % (len(nct_rancios), len(nct_vigilados), NCT_CACHE_STALE_H,
                              ", ".join(sorted(nct_rancios)))))
        # Un NCT rancio no impide avisar de un cambio confirmado en OTRO NCT.
        for nct_id in sorted(nct_frescos):
            estado_nuevo = nct_actuales[nct_id]
            estado_viejo = nct_estados_prev.get(nct_id)
            if estado_viejo is None:
                # Primera vez que vemos este NCT en el caché; establece línea base, no avisa
                continue
            if estado_nuevo == estado_viejo:
                continue
            # Hay cambio: siempre alertamos
            if estado_nuevo in NCT_ESTADOS_ALARMA:
                tono = "ALERTA"
            else:
                tono = "Novedad"
            avisos.append(("nct",
                           "%s en ensayo %s: estado cambio de '%s' a '%s'. Comprueba tu elegibilidad." % (
                               tono, nct_id, estado_viejo, estado_nuevo)))

    # ── estado a sellar ──────────────────────────────────────────────────────────
    if buzon_ids is None:
        emails_sellar = sorted(vistos_mail) if not primera else []
    else:
        emails_sellar = sorted((vistos_mail | set(criticos)) & buzon_ids) if not primera else sorted(set(criticos))

    # Plazos a sellar: los que siguen presentes (fecha aun en ventana) + los ya avisados que
    # siguen en ventana. Los que salen de la ventana (fecha pasada) se purgan solos.
    plazos_sellar = sorted(
        (vistos_plazo | set(plazos)) & set(plazos)
    ) if not primera else sorted(set(plazos))

    # NCT estados a sellar: merge de previos + actuales (de NCTs vigilados con caché). Con el
    # caché rancio NO se sella: sellar un fósil como «visto» haría que, al refrescarse, el
    # cambio real quedara enterrado bajo la línea base que acabamos de escribir nosotros.
    nct_sellar = dict(nct_estados_prev)
    for nct_id in nct_frescos:
        nct_sellar[nct_id] = nct_actuales[nct_id]

    estado = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "emails_avisados": emails_sellar,
        "foco_sig": foco_hash if foco_hash is not None else foco_prev,
        "plazos_avisados": plazos_sellar,
        "nct_estados": nct_sellar,
        # Día del último aviso de caché rancio: el centinela corre cada 150 s y esto no puede
        # gritar 576 veces al día. Uno diario basta para que no se olvide.
        "nct_rancio_avisado": _dia_rancio_sellar(mark, nct_rancio),
    }
    return avisos, estado


def _heartbeat(estado="ok"):
    """Latido para el watchdog de healthcheck (frescura). Best-effort, sin PII."""
    try:
        hbdir = os.path.join(STATE, "heartbeat")
        os.makedirs(hbdir, exist_ok=True)
        tmp = os.path.join(hbdir, ".centinela-ned.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"agente": "centinela-ned",
                       "ts": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"), "estado": estado}, f)
        os.replace(tmp, os.path.join(hbdir, "centinela-ned.json"))
    except Exception:
        pass


def run(dry=False):
    """Detecta lo NED-crítico nuevo, AVISA (salida.report_to_titular, urgente=False) y sella visto.
    Devuelve la lista de (tipo, texto) avisada."""
    avisos, estado = detectar()
    if not dry and avisos:
        try:
            import salida
            for _tipo, texto in avisos:
                salida.report_to_titular(texto, urgente=False, fuente="centinela")
        except Exception as e:
            sys.stderr.write("centinela: no pude avisar (%r); NO sello para reintentar.\n" % e)
            return avisos          # no sellar si la salida falló → reintenta en el próximo ciclo
    if not dry:
        _guardar_mark(estado)
        _heartbeat("ok")
    return avisos


# ── CLI ──────────────────────────────────────────────────────────────────────────
def main(argv):
    cmd = argv[0] if argv else "run"
    if cmd in ("-h", "--help"):
        print(__doc__)
        return 0
    if cmd == "run":
        avisos = run()
        print("avise %d senal(es) NED-critica(s)" % len(avisos) if avisos else "nada NED-critico nuevo")
        return 0
    if cmd == "check":
        avisos, _estado = detectar()
        print(("HAY %d aviso(s) NED-critico(s) nuevo(s)" % len(avisos)) if avisos else "nada NED-critico nuevo")
        return 0 if avisos else 1
    if cmd == "status":
        criticos, buzon_ids = _correo_estado()
        foco_hash, foco = _foco()
        plazos = _plazos_seguimiento()
        nct_edades = {}
        nct_actuales = _nct_estados_desde_cache(edades=nct_edades)
        nct_vigilados = _nct_ids_vigilados()
        edades = [nct_edades.get(n) for n in nct_vigilados]
        nct_edad_h = max(edades) if edades and None not in edades else None
        info = {
            "marcador": _cargar_mark().get("ts", "ausente"),
            "buzon": "ausente (gate App Password)" if buzon_ids is None else "%d msgs, %d NED-criticos" % (len(buzon_ids), len(criticos)),
            "foco": (foco.get("titulo") if foco else None),
            "plazos_en_ventana": {
                v[2] + " | " + v[0]: "%d dias (%s)" % (v[3], v[1])
                for v in plazos.values()
            },
            "nct_vigilados": sorted(nct_vigilados),
            "nct_en_cache": {k: v for k, v in nct_actuales.items() if k in nct_vigilados},
            # «existe» era una verdad tranquilizadora que no significaba nada: el fichero existía
            # con datos de hace 29 días. Lo que hay que ver de un vistazo es si el dato SIRVE.
            "nct_cache_edad_h": round(nct_edad_h, 1) if nct_edad_h is not None else None,
            "nct_cache_fresco": (nct_edad_h is not None and nct_edad_h <= NCT_CACHE_STALE_H),
            "nct_cache_edades_h": {n: round(nct_edades[n], 1) if nct_edades.get(n) is not None
                                   else None for n in sorted(nct_vigilados)},
        }
        print(json.dumps(info, ensure_ascii=False, indent=2))
        return 0
    print("uso: centinela_ned.py [run | check | status]", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
