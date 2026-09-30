import streamlit as st
import pandas as pd
import os
import time
import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
import urllib.parse
import hashlib
import hmac
import secrets
from supabase import create_client
from streamlit_js_eval import streamlit_js_eval

st.set_page_config(page_title="I Gufi della Notte - Gestione Turni", layout="wide", initial_sidebar_state="expanded")

# --- GESTIONE FUSO ORARIO ITALIA ---
FUSO_ITALIA = ZoneInfo("Europe/Rome")

def ora_italiana():
    return datetime.now(FUSO_ITALIA)

def data_italiana():
    return ora_italiana().date()

# --- CONNESSIONE SUPABASE ---
@st.cache_resource
def init_supabase():
    url = st.secrets["SUPABASE_URL"]
    key = st.secrets["SUPABASE_KEY"]
    return create_client(url, key)

supabase = init_supabase()
BUCKET_FOTO = "foto-turni"

# --- CONFIGURAZIONI & SICUREZZA ---
TIMEOUT_MINUTI = 15

# Le password admin si impostano SOLO nei Secrets di Streamlit (ADMIN_PWD_TIZIANA, ADMIN_PWD_RINO):
# nel codice non c'è più nessuna password di riserva.
CREDENZIALI_ADMIN = {
    "tiziana": {
        "password": str(st.secrets.get("ADMIN_PWD_TIZIANA", "")).strip(),
        "secret": "ADMIN_PWD_TIZIANA",
        "nome": "Tiziana (Direttore Generale Supremo \"MASTO\")"
    },
    "rino": {
        "password": str(st.secrets.get("ADMIN_PWD_RINO", "")).strip(),
        "secret": "ADMIN_PWD_RINO",
        "nome": "Rino (Capo Reparto)"
    }
}

# Password dipendente usata quando nel database il campo è vuoto
PASSWORD_PREDEFINITA_DIP = "gufi2026!"

# --- 3. PASSWORD CIFRATE (PBKDF2) ---
# Le password dei dipendenti non vengono più salvate in chiaro. Le vecchie password
# in chiaro continuano a funzionare e vengono cifrate automaticamente al primo accesso.
PREFISSO_HASH = "pbkdf2_sha256"

def cifra_password(pwd):
    sale = secrets.token_hex(16)
    iterazioni = 200_000
    impronta = hashlib.pbkdf2_hmac("sha256", pwd.encode("utf-8"), bytes.fromhex(sale), iterazioni).hex()
    return f"{PREFISSO_HASH}${iterazioni}${sale}${impronta}"

def password_cifrata(salvata):
    return str(salvata).startswith(PREFISSO_HASH + "$")

def verifica_password(pwd, salvata):
    if password_cifrata(salvata):
        try:
            _, iterazioni, sale, impronta = str(salvata).split("$")
            calcolata = hashlib.pbkdf2_hmac("sha256", pwd.encode("utf-8"), bytes.fromhex(sale), int(iterazioni)).hex()
            return hmac.compare_digest(calcolata, impronta)
        except Exception:
            return False
    return hmac.compare_digest(pwd.encode("utf-8"), str(salvata).encode("utf-8"))

GIORNI_IT = ["Lunedì", "Martedì", "Mercoledì", "Giovedì", "Venerdì", "Sabato", "Domenica"]
GIORNI_BREVI = ["Lun", "Mar", "Mer", "Gio", "Ven", "Sab", "Dom"]

# Controllo Inattività Sessione (15 minuti)
ora_attuale_ts = time.time()
if "ultimo_accesso" in st.session_state:
    if (ora_attuale_ts - st.session_state["ultimo_accesso"]) > (TIMEOUT_MINUTI * 60):
        st.session_state["autenticato"] = False
        st.session_state["ruolo"] = ""
        st.session_state["utente_corrente"] = None
        st.warning("Sessione scaduta per inattività (15 minuti). Effettua nuovamente l'accesso.")

st.session_state["ultimo_accesso"] = ora_attuale_ts

if "autenticato" not in st.session_state:
    st.session_state["autenticato"] = False
    st.session_state["ruolo"] = ""
    st.session_state["utente_corrente"] = None

def pulisci_nome(testo):
    return re.sub(r'[^a-zA-Z0-9_-]', '_', str(testo).strip())

def registra_log(autore, azione, dettagli):
    adesso_str = ora_italiana().strftime("%d/%m/%Y %H:%M:%S")
    try:
        supabase.table("audit_log").insert({
            "data_ora": adesso_str,
            "autore": autore,
            "azione": azione,
            "dettagli": dettagli
        }).execute()
    except Exception as e:
        st.error(f"Errore registrazione log: {e}")

# --- PARSER DATA ROBUSTO ---
def analizza_data_completa(val):
    if pd.isna(val) or not str(val).strip():
        return None
    s = str(val).strip().lower()
    for g in ['lunedì', 'martedì', 'mercoledì', 'giovedì', 'venerdì', 'sabato', 'domenica']:
        s = s.replace(g, '').strip()

    mesi = {
        'gennaio': '01', 'febbraio': '02', 'marzo': '03', 'aprile': '04',
        'maggio': '05', 'giugno': '06', 'luglio': '07', 'agosto': '08',
        'settembre': '09', 'ottobre': '10', 'novembre': '11', 'dicembre': '12'
    }
    for m_it, m_num in mesi.items():
        if m_it in s:
            s = s.replace(m_it, m_num)
            break

    s = s.replace("-", "/").replace(".", "/")
    parti = s.split()

    if len(parti) >= 3 and parti[0].isdigit() and parti[1].isdigit() and parti[2].isdigit():
        s_data = f"{int(parti[0]):02d}/{int(parti[1]):02d}/{parti[2]}"
    else:
        s_data = parti[0] if parti else s

    for fmt in ['%d/%m/%Y', '%Y-%m-%d', '%m/%d/%Y']:
        try:
            return datetime.strptime(s_data, fmt).date()
        except Exception:
            pass
    try:
        dt = pd.to_datetime(val, dayfirst=True, errors='coerce')
        if pd.notna(dt):
            return dt.date()
    except Exception:
        pass
    return None

# --- LETTURA A PAGINE (supera il limite di 1000 righe di Supabase) ---
def leggi_tutto(tabella, ordine=None, pagina=1000):
    tutte = []
    inizio = 0
    while True:
        query = supabase.table(tabella).select("*")
        if ordine:
            query = query.order(ordine)
        res = query.range(inizio, inizio + pagina - 1).execute()
        blocco = res.data or []
        tutte.extend(blocco)
        if len(blocco) < pagina:
            break
        inizio += pagina
    return tutte

# --- CARICAMENTO DATI DA SUPABASE ---
def carica_dati():
    df_dip = pd.DataFrame(leggi_tutto("dipendenti", ordine="id_guardia"))
    if df_dip.empty:
        df_dip = pd.DataFrame(columns=['id_guardia', 'cognome', 'nome', 'email', 'password'])

    df_post = pd.DataFrame(leggi_tutto("postazioni", ordine="id_postazione"))
    if not df_post.empty and 'id_postazione' in df_post.columns:
        df_post = df_post.sort_values(by='id_postazione', ascending=True)
    else:
        df_post = pd.DataFrame(columns=['id_postazione', 'nome_cliente', 'indirizzo_sede'])

    df_turni = pd.DataFrame(leggi_tutto("turni", ordine="id_turno"))
    if df_turni.empty:
        df_turni = pd.DataFrame(columns=[
            'id_turno', 'data', 'id_guardia', 'cognome_guardia', 'id_postazione',
            'ora_inizio_prevista', 'ora_fine_prevista', 'check_in_effettivo',
            'check_out_effettivo', 'gps_check_in', 'gps_check_out',
            'foto_postazione', 'registrato_da'
        ])

    # Aggiungi colonna data_dt standardizzata
    if not df_turni.empty and 'data' in df_turni.columns:
        df_turni['data_dt'] = df_turni['data'].apply(analizza_data_completa)
        df_turni['Mese_Anno'] = df_turni['data_dt'].apply(lambda d: d.strftime("%m/%Y") if pd.notna(d) else "Non Riconosciuto")
    else:
        df_turni['data_dt'] = None
        df_turni['Mese_Anno'] = "Non Riconosciuto"

    return df_turni, df_dip, df_post

df_turni, df_dip, df_post = carica_dati()

# --- MAPPATURA AUTOMATICA COGNOMI ---
mappa_id_cognome = {}
if not df_dip.empty:
    for _, r_d in df_dip.iterrows():
        id_g = str(r_d['id_guardia']).strip().lower()
        cogn = str(r_d['cognome']).strip()
        if id_g:
            mappa_id_cognome[id_g] = cogn

# --- MAPPATURA POSTAZIONI ---
mappa_id_postazione = {}
if not df_post.empty:
    for _, r_p in df_post.iterrows():
        mappa_id_postazione[str(r_p['id_postazione']).strip()] = str(r_p.get('nome_cliente', '')).strip()

def nome_postazione_da_id(id_p):
    id_p = str(id_p).strip()
    return mappa_id_postazione.get(id_p, id_p if id_p else "N/D")

def risolvi_cognome_effettivo(r):
    val_cogn = str(r.get('cognome_guardia', '')).strip()
    val_id = str(r.get('id_guardia', '')).strip().lower()

    if val_cogn.lower() in mappa_id_cognome:
        return mappa_id_cognome[val_cogn.lower()]
    if val_id in mappa_id_cognome:
        return mappa_id_cognome[val_id]
    if "-" in val_cogn:
        parti = val_cogn.split("-")
        possibile_id = parti[0].strip().lower()
        if possibile_id in mappa_id_cognome:
            return mappa_id_cognome[possibile_id]
        return parti[-1].strip()

    return val_cogn if val_cogn else "N/D"

if not df_turni.empty:
    df_turni['cognome_guardia'] = df_turni.apply(risolvi_cognome_effettivo, axis=1)

# --- LETTURA CARTELLE STORAGE (Supabase restituisce max 100 elementi per chiamata) ---
def lista_storage(percorso=None, pagina=1000):
    tutti = []
    offset = 0
    while True:
        blocco = supabase.storage.from_(BUCKET_FOTO).list(percorso, {"limit": pagina, "offset": offset}) or []
        tutti.extend(blocco)
        if len(blocco) < pagina:
            return tutti
        offset += pagina

# --- SALVATAGGIO FOTO CON STRUTTURA REALE ---
def salva_foto_su_storage(file_foto, giorno_data, nome_postazione, id_guardia, nome_guardia, id_turno, tipo_timbratura, gps_info="GPS_NON_RILEVATO"):
    cartella_operatore = pulisci_nome(f"{id_guardia}_{nome_guardia}")
    giorno_str = giorno_data.strftime("%Y-%m-%d") if isinstance(giorno_data, (date, datetime)) else data_italiana().strftime("%Y-%m-%d")
    cartella_data = giorno_str
    cartella_posizione = pulisci_nome(nome_postazione)

    data_ora_scatto = ora_italiana()
    orario_str = data_ora_scatto.strftime('%H-%M-%S')
    gps_info = re.sub(r'[^0-9A-Za-z_.-]', '_', str(gps_info))
    tipo_mime = getattr(file_foto, "type", None) or "image/jpeg"

    estensione = "png" if tipo_mime == "image/png" else "jpg"
    nome_file = f"Turno_{pulisci_nome(id_turno)}_{tipo_timbratura}_Data_{giorno_str}_Ore_{orario_str}_GPS_{gps_info}.{estensione}"
    path_remoto = f"{cartella_operatore}/{cartella_data}/{cartella_posizione}/{nome_file}"

    file_bytes = file_foto.getvalue()
    try:
        supabase.storage.from_(BUCKET_FOTO).upload(
            path=path_remoto,
            file=file_bytes,
            file_options={"content-type": tipo_mime, "upsert": "true"}
        )
    except Exception:
        try:
            supabase.storage.from_(BUCKET_FOTO).update(
                path=path_remoto,
                file=file_bytes,
                file_options={"content-type": tipo_mime}
            )
        except Exception as e_up:
            st.error(f"Errore caricamento su Supabase Storage: {e_up}")
            raise e_up

    url_pubblico = supabase.storage.from_(BUCKET_FOTO).get_public_url(path_remoto)
    return url_pubblico

def calcola_ore(ora_inizio, ora_fine):
    try:
        if pd.isna(ora_inizio) or pd.isna(ora_fine):
            return 0.0
        str_i = str(ora_inizio).strip().split()[-1]
        str_f = str(ora_fine).strip().split()[-1]
        t_ini, t_fin = None, None
        for fmt in ["%H:%M:%S", "%H:%M"]:
            try:
                t_ini = datetime.strptime(str_i, fmt)
                break
            except Exception:
                pass
        for fmt in ["%H:%M:%S", "%H:%M"]:
            try:
                t_fin = datetime.strptime(str_f, fmt)
                break
            except Exception:
                pass
        if t_ini and t_fin:
            diff = (t_fin - t_ini).total_seconds() / 3600.0
            if diff < 0:
                diff += 24.0
            return round(diff, 2)
        return 0.0
    except Exception:
        return 0.0

def timbrato(valore):
    return pd.notna(valore) and str(valore).strip() != '' and str(valore).strip() != 'None'

def testo(valore):
    # Converte in stringa pulita: celle vuote/NaN/None diventano ''
    if valore is None or (not isinstance(valore, str) and pd.isna(valore)):
        return ""
    v = str(valore).strip()
    return "" if v in ("None", "nan") else v

def ordina_mesi(mesi):
    # I mesi sono stringhe "MM/AAAA": ordina dal più recente per anno e poi per mese
    def chiave(m):
        try:
            mm, aa = str(m).split("/")
            return (int(aa), int(mm))
        except Exception:
            return (0, 0)
    return sorted([m for m in mesi if m != "Non Riconosciuto"], key=chiave, reverse=True)

def prossimo_codice(codici_esistenti, prefisso):
    # Primo codice libero dopo il numero più alto già usato (es. G012 -> G013)
    numeri = [int(m.group(1)) for c in codici_esistenti if (m := re.fullmatch(rf"{prefisso}(\d+)", testo(c), re.IGNORECASE))]
    return f"{prefisso}{(max(numeri) + 1 if numeri else 1):03d}"

def colonna_uguale(serie, valore):
    # Confronto robusto tra colonna e ID (ignora spazi e tipi diversi, es. numeri)
    return serie.astype(str).str.strip() == str(valore).strip()

def estrai_ore_t(r):
    cin, cout = r.get('check_in_effettivo'), r.get('check_out_effettivo')
    if timbrato(cin) and timbrato(cout):
        return calcola_ore(cin, cout)
    return calcola_ore(r.get('ora_inizio_prevista', '00:00'), r.get('ora_fine_prevista', '00:00'))

# --- 1. POSIZIONE GPS REALE DEL TELEFONO ---
# Il browser chiede al dipendente il permesso di usare la posizione. Il risultato
# arriva con un rerun: finché non arriva vale None ("in attesa").
JS_GPS = """new Promise(function (risolvi) {
  if (!navigator.geolocation) { risolvi({error: {code: 0, message: "GPS non supportato dal browser"}}); return; }
  navigator.geolocation.getCurrentPosition(
    function (p) { risolvi({coords: {latitude: p.coords.latitude, longitude: p.coords.longitude, accuracy: p.coords.accuracy}, timestamp: Date.now()}); },
    function (e) { risolvi({error: {code: e.code, message: e.message}}); },
    {enableHighAccuracy: true, timeout: 20000, maximumAge: 30000}
  );
})"""

MOTIVI_GPS = {
    1: "permesso negato: consenti l'accesso alla posizione nelle impostazioni del browser",
    2: "posizione non disponibile: attiva la localizzazione del telefono",
    3: "tempo scaduto: riprova all'aperto o vicino a una finestra",
}
GPS_VALIDITA_MINUTI = 10

def rileva_gps():
    # Restituisce {"stato": "attesa"|"ok"|"errore", "testo": ..., "file": ..., "motivo": ...}
    tentativo = st.session_state.get("gps_tentativo", 0)
    ris = streamlit_js_eval(js_expressions=JS_GPS, key=f"gps_rilevamento_{tentativo}")
    if not ris:
        return {"stato": "attesa", "testo": "NON RILEVATO (in attesa del GPS)", "file": "GPS_NON_RILEVATO", "motivo": "rilevamento in corso"}
    if isinstance(ris, dict) and ris.get("coords"):
        c = ris["coords"]
        lat, lon, prec = float(c["latitude"]), float(c["longitude"]), c.get("accuracy")
        prec_txt = f" (±{int(round(prec))} m)" if prec is not None else ""
        eta_min = (time.time() * 1000 - float(ris.get("timestamp") or 0)) / 60000
        return {
            "stato": "ok" if eta_min <= GPS_VALIDITA_MINUTI else "vecchio",
            "testo": f"{lat:.6f}, {lon:.6f}{prec_txt}",
            "file": f"{lat:.6f}_{lon:.6f}",
            "link": f"https://www.google.com/maps/search/?api=1&query={lat:.6f},{lon:.6f}",
            "motivo": f"posizione rilevata {int(eta_min)} minuti fa"
        }
    errore = (ris or {}).get("error", {}) if isinstance(ris, dict) else {}
    motivo = MOTIVI_GPS.get(errore.get("code"), errore.get("message") or "errore sconosciuto")
    return {"stato": "errore", "testo": f"NON DISPONIBILE ({motivo})", "file": "GPS_NON_DISPONIBILE", "motivo": motivo}

def aggiorna_gps():
    st.session_state["gps_tentativo"] = st.session_state.get("gps_tentativo", 0) + 1

# --- 4. PIÙ FOTO PER TURNO ---
# Nella colonna foto_postazione le foto sono salvate una dopo l'altra separate da " | "
# (così la foto del check-out non cancella più quella del check-in).
SEPARATORE_FOTO = " | "

def elenco_foto(valore):
    return [u.strip() for u in testo(valore).split(SEPARATORE_FOTO.strip()) if u.strip()]

def tipo_foto(url):
    if "_OUT_" in url:
        return "Uscita"
    if "_IN_" in url:
        return "Entrata"
    return "Foto"

# --- TIMBRATURA (CHECK-IN / CHECK-OUT) ---
# Niente st.form: il file_uploader fuori dal form fa un rerun solo quando la foto
# è arrivata al server, quindi il pulsante resta disabilitato finché l'upload non
# è completo. Ogni foto può essere usata per una sola timbratura: se il browser
# del telefono si riconnette (es. dopo aver aperto la fotocamera) e ripete un
# vecchio click, la timbratura non viene registrata di nuovo in automatico.
def render_timbratura(tipo, id_t, op, data_oggettiva, nome_posto):
    if tipo == "IN":
        campo_ora, campo_gps = "check_in_effettivo", "gps_check_in"
        etichetta_foto, etichetta_btn = "Foto Entrata Postazione:", "✅ Conferma Check-in"
        azione_log, nome_timbr = "TIMBRATURA_CHECKIN", "Check-in"
    else:
        campo_ora, campo_gps = "check_out_effettivo", "gps_check_out"
        etichetta_foto, etichetta_btn = "Foto Uscita / Consegna:", "🔴 Conferma Check-out"
        azione_log, nome_timbr = "TIMBRATURA_CHECKOUT", "Check-out"

    chiave = f"{tipo}_{id_t}"
    foto_usate = st.session_state.setdefault("foto_timbrature_usate", {})

    gps = st.session_state.get("gps_corrente") or {"stato": "attesa", "testo": "NON RILEVATO", "file": "GPS_NON_RILEVATO", "motivo": ""}
    if gps["stato"] == "ok":
        st.success(f"📍 Posizione GPS rilevata: {gps['testo']}")
    elif gps["stato"] == "attesa":
        st.info("📍 Rilevamento posizione in corso... Se il telefono lo chiede, premi **Consenti**.")
    else:
        st.warning(f"📍 GPS: {gps['motivo']}. Premi **Aggiorna posizione** in alto prima di timbrare.")

    if tipo == "OUT" and isinstance(data_oggettiva, date) and data_oggettiva < data_italiana() - timedelta(days=1):
        st.warning("⏰ Check-out in ritardo: verrà registrato l'orario di adesso. Avvisa il responsabile dell'orario reale di fine turno.")

    foto = st.file_uploader(etichetta_foto, type=["jpg", "jpeg", "png"], key=f"f{tipo.lower()}_{id_t}")

    id_foto = getattr(foto, "file_id", None) or (f"{foto.name}_{foto.size}" if foto else None)
    # Pulsante attivo solo con foto caricata e GPS rilevato (o con errore dichiarato); se la posizione è vecchia va aggiornata
    foto_pronta = foto is not None and foto_usate.get(chiave) != id_foto and gps["stato"] in ("ok", "errore")

    if foto is None:
        st.warning("📸 Scatta o seleziona la foto e attendi che il caricamento finisca: il pulsante si attiverà da solo.")

    if not st.button(etichetta_btn, type="primary", width="stretch", disabled=not foto_pronta, key=f"btn_{chiave}"):
        return

    # Ricontrolla sul database: se il turno risulta già timbrato non sovrascrivere l'orario
    riga = supabase.table("turni").select(f"{campo_ora}, foto_postazione").eq("id_turno", id_t).execute().data or []
    if riga and timbrato(riga[0].get(campo_ora)):
        st.warning(f"{nome_timbr} già registrato per questo turno.")
        st.rerun()

    try:
        foto_url = salva_foto_su_storage(foto, data_oggettiva, nome_posto, op['id'], op['nome'], id_t, tipo, gps["file"])
    except Exception:
        st.error("❌ Foto non caricata: timbratura NON registrata. Controlla la connessione e riprova.")
        return
    update_data = {
        campo_ora: ora_italiana().strftime("%d/%m/%Y %H:%M:%S"),
        campo_gps: gps["testo"],
        "registrato_da": f"{op['nome']} ({nome_timbr})",
        "foto_postazione": SEPARATORE_FOTO.join(elenco_foto(riga[0].get("foto_postazione") if riga else "") + [foto_url])
    }
    supabase.table("turni").update(update_data).eq("id_turno", id_t).execute()
    foto_usate[chiave] = id_foto
    registra_log(op["nome"], azione_log, f"Turno {id_t} - {nome_posto}")
    st.success(f"✅ {nome_timbr} registrato con successo!")
    aggiorna_gps()  # la prossima timbratura rileva di nuovo la posizione
    st.rerun()

# --- FUNZIONI VISTA SETTIMANALE DIPENDENTE ---
def maschera_dipendente(df, id_guardia, cognome):
    # Il codice guardia è univoco; il cognome si usa solo per i turni senza codice
    # (così due dipendenti con lo stesso cognome non vedono i turni l'uno dell'altro)
    ids = df['id_guardia'].apply(testo).str.lower() if 'id_guardia' in df.columns else pd.Series("", index=df.index)
    stesso_id = ids == str(id_guardia).strip().lower()
    stesso_cognome = (ids == "") & (df['cognome_guardia'].astype(str).str.strip().str.lower() == str(cognome).strip().lower())
    return stesso_id | stesso_cognome

def turni_del_dipendente(id_guardia, cognome):
    if df_turni.empty:
        return df_turni.iloc[0:0].copy()
    return df_turni[maschera_dipendente(df_turni, id_guardia, cognome)].copy()

def render_settimana_dipendente(turni_dip, data_rif, etichetta):
    inizio = data_rif - timedelta(days=data_rif.weekday())
    fine = inizio + timedelta(days=6)
    st.markdown(f"#### {etichetta} — Settimana dal `{inizio.strftime('%d/%m/%Y')}` al `{fine.strftime('%d/%m/%Y')}`")

    if turni_dip.empty:
        settimana = turni_dip
    else:
        maschera = turni_dip['data_dt'].apply(
            lambda d: d is not None and pd.notna(d) and inizio <= d <= fine
        ).astype(bool)
        settimana = turni_dip[maschera].copy()

    if settimana.empty:
        st.info("Nessun turno assegnato in questa settimana.")
        return

    settimana['Ore_Turno'] = settimana.apply(estrai_ore_t, axis=1)
    settimana['Nome_Postazione'] = settimana['id_postazione'].apply(nome_postazione_da_id)

    # Metriche riepilogo
    m1, m2, m3 = st.columns(3)
    m1.metric("Turni nella settimana", len(settimana))
    m2.metric("Ore complessive", round(settimana['Ore_Turno'].sum(), 2))
    m3.metric("Postazioni diverse", settimana['Nome_Postazione'].nunique())

    # Matrice Postazione x Giorno
    st.markdown("##### 🗺️ Riepilogo: postazioni per giorno")
    colonne_giorni = []
    for i in range(7):
        g = inizio + timedelta(days=i)
        colonne_giorni.append((g, f"{GIORNI_BREVI[i]} {g.strftime('%d/%m')}"))

    postazioni_sett = sorted(settimana['Nome_Postazione'].unique())
    righe_matrice = []
    for nome_p in postazioni_sett:
        riga = {"Postazione": nome_p}
        for g, etichetta_g in colonne_giorni:
            t_gp = settimana[(settimana['data_dt'] == g) & (settimana['Nome_Postazione'] == nome_p)]
            if t_gp.empty:
                riga[etichetta_g] = "—"
            else:
                orari = [f"{t.get('ora_inizio_prevista', '-')}-{t.get('ora_fine_prevista', '-')}" for _, t in t_gp.sort_values(by='ora_inizio_prevista').iterrows()]
                riga[etichetta_g] = " / ".join(orari)
        righe_matrice.append(riga)
    st.dataframe(pd.DataFrame(righe_matrice), width="stretch", hide_index=True)

    # Dettaglio giorno per giorno
    st.markdown("##### 📋 Dettaglio giorno per giorno")
    righe_dett = []
    for i in range(7):
        g = inizio + timedelta(days=i)
        etichetta_g = f"{GIORNI_IT[i]} ({g.strftime('%d/%m')})"
        tg = settimana[settimana['data_dt'] == g]
        if tg.empty:
            righe_dett.append({
                "Giorno": etichetta_g, "Turno ID": "-", "Postazione": "🏠 RIPOSO / NESSUN TURNO",
                "Orario": "-", "Ore": None, "Entrata (Check-in)": "-", "Uscita (Check-out)": "-"
            })
        else:
            for _, t in tg.sort_values(by='ora_inizio_prevista').iterrows():
                cin = t.get('check_in_effettivo', '')
                cout = t.get('check_out_effettivo', '')
                righe_dett.append({
                    "Giorno": etichetta_g,
                    "Turno ID": t.get('id_turno', ''),
                    "Postazione": t['Nome_Postazione'],
                    "Orario": f"{t.get('ora_inizio_prevista', '-')} - {t.get('ora_fine_prevista', '-')}",
                    "Ore": t['Ore_Turno'],
                    "Entrata (Check-in)": cin if timbrato(cin) else "⏳ Non timbrato",
                    "Uscita (Check-out)": cout if timbrato(cout) else "⏳ Non timbrato"
                })
    st.dataframe(pd.DataFrame(righe_dett), width="stretch", hide_index=True)

# -------------------------------------------------------------------------------------------------
# LOGIN
# -------------------------------------------------------------------------------------------------
if not st.session_state["autenticato"]:
    st.markdown("<h2 style='text-align: center; color: #1E3A8A;'>🦉 I Gufi della Notte</h2>", unsafe_allow_html=True)
    st.markdown("<p style='text-align: center; color: #64748B;'>Accesso Portale Operativo Guardie & Coordinamento</p>", unsafe_allow_html=True)
    st.markdown("---")

    c1, c2, c3 = st.columns([1, 1.8, 1])
    with c2:
        tab_user, tab_admin = st.tabs(["👤 Area Personale Dipendente", "🔐 Accesso Responsabili (Tiziana / Rino)"])

        with tab_user:
            st.info("Accedi con il tuo Cognome e la tua Password personale.")
            with st.form("form_login_op"):
                cognome_input = st.text_input("Cognome:")
                pwd_input = st.text_input("Password:", type="password")
                btn_op = st.form_submit_button("Entra nei Miei Turni", type="primary", width="stretch")
                if btn_op:
                    val_cognome = cognome_input.strip().lower()
                    val_pwd = pwd_input.strip()
                    trovato = df_dip[df_dip['cognome'].astype(str).str.strip().str.lower() == val_cognome]

                    # Con più dipendenti con lo stesso cognome entra quello con la password corretta
                    record_dip = None
                    for _, r_dip in trovato.iterrows():
                        pwd_salvata = testo(r_dip.get('password')) or PASSWORD_PREDEFINITA_DIP
                        if verifica_password(val_pwd, pwd_salvata):
                            record_dip = r_dip
                            # Vecchia password in chiaro: la cifro subito nel database
                            if not password_cifrata(pwd_salvata):
                                try:
                                    supabase.table("dipendenti").update({"password": cifra_password(val_pwd)}).eq("id_guardia", r_dip['id_guardia']).execute()
                                except Exception:
                                    pass
                            break

                    if not trovato.empty:
                        if record_dip is not None:
                            st.session_state["autenticato"] = True
                            st.session_state["ruolo"] = "operatore"
                            st.session_state["utente_corrente"] = {
                                "id": testo(record_dip['id_guardia']),
                                "cognome": testo(record_dip['cognome']),
                                "nome": f"{testo(record_dip.get('nome'))} {testo(record_dip.get('cognome'))}".strip()
                            }
                            registra_log(st.session_state["utente_corrente"]["nome"], "LOGIN_DIPENDENTE", f"Accesso {record_dip['cognome']}")
                            st.rerun()
                        else:
                            st.error("Password errata.")
                    else:
                        st.error("Nessun dipendente trovato con questo cognome.")

        with tab_admin:
            with st.form("form_login_adm"):
                adm_user = st.selectbox("Seleziona Utente:", list(CREDENZIALI_ADMIN.keys()), format_func=lambda x: CREDENZIALI_ADMIN[x]["nome"])
                adm_pwd = st.text_input("Password:", type="password")
                btn_adm = st.form_submit_button("Accedi al Pannello Admin", type="primary", width="stretch")
                if btn_adm:
                    pwd_admin = CREDENZIALI_ADMIN[adm_user]["password"]
                    if not pwd_admin:
                        st.error(f"Password non configurata. Aggiungi {CREDENZIALI_ADMIN[adm_user]['secret']} nei Secrets dell'app su Streamlit Cloud.")
                    elif hmac.compare_digest(adm_pwd.encode("utf-8"), pwd_admin.encode("utf-8")):
                        st.session_state["autenticato"] = True
                        st.session_state["ruolo"] = "admin"
                        st.session_state["utente_corrente"] = {
                            "id": adm_user,
                            "nome": CREDENZIALI_ADMIN[adm_user]["nome"]
                        }
                        registra_log(CREDENZIALI_ADMIN[adm_user]["nome"], "LOGIN_ADMIN", "Accesso eseguito")
                        st.rerun()
                    else:
                        st.error("Password errata.")
    st.stop()

# -------------------------------------------------------------------------------------------------
# AREA DIPENDENTE
# -------------------------------------------------------------------------------------------------
if st.session_state["ruolo"] == "operatore":
    op = st.session_state["utente_corrente"]
    c_h1, c_h2 = st.columns([4, 1.2])
    with c_h1:
        st.markdown(f"### 👋 Operatore: **{op['nome']}** `[{op['id']}]`")
        st.caption("Portale operativo personale guardie giurate")
    with c_h2:
        if st.button("🚪 Esci", width="stretch"):
            registra_log(op["nome"], "LOGOUT", "Disconnessione dipendente")
            st.session_state["autenticato"] = False
            st.rerun()

    gps_corrente = rileva_gps()
    st.session_state["gps_corrente"] = gps_corrente
    c_g1, c_g2 = st.columns([4, 1.2])
    with c_g1:
        if gps_corrente["stato"] == "ok":
            st.caption(f"📍 Posizione attuale: [{gps_corrente['testo']}]({gps_corrente['link']})")
        elif gps_corrente["stato"] == "attesa":
            st.caption("📍 Rilevamento posizione in corso... Se il telefono lo chiede, premi **Consenti**.")
        elif gps_corrente["stato"] == "vecchio":
            st.caption(f"📍 {gps_corrente['motivo'].capitalize()}: premi **Aggiorna posizione** prima di timbrare.")
        else:
            st.caption(f"⚠️ GPS: {gps_corrente['motivo']}.")
    with c_g2:
        st.button("🔄 Aggiorna posizione", on_click=aggiorna_gps, width="stretch", key="btn_aggiorna_gps")

    st.markdown("---")
    tab_attivi, tab_settimana_op, tab_storico, tab_foto_op = st.tabs([
        "🟢 Turni da Svolgere / Oggi",
        "📆 La Mia Settimana",
        "📜 Storico Turni Passati",
        "📸 Le Mie Foto Caricate"
    ])

    turni_miei = turni_del_dipendente(op['id'], op['cognome'])

    oggi = data_italiana()
    limite_aperti_recenti = oggi - timedelta(days=1)
    # Un turno con check-in ma senza check-out resta tra quelli da svolgere per qualche giorno,
    # così il dipendente può ancora chiuderlo
    limite_checkout_dimenticato = oggi - timedelta(days=3)

    def turno_completato(r):
        return timbrato(r.get('check_in_effettivo')) and timbrato(r.get('check_out_effettivo'))

    if turni_miei.empty:
        condizione_attivo = pd.Series([], dtype=bool)
    else:
        recente = turni_miei['data_dt'].apply(lambda d: d is not None and pd.notna(d) and d >= limite_aperti_recenti).astype(bool)
        checkout_mancante = turni_miei.apply(
            lambda r: timbrato(r.get('check_in_effettivo')) and not timbrato(r.get('check_out_effettivo'))
            and r.get('data_dt') is not None and pd.notna(r.get('data_dt')) and r.get('data_dt') >= limite_checkout_dimenticato,
            axis=1
        ).astype(bool)
        condizione_attivo = (recente | checkout_mancante) & (~turni_miei.apply(turno_completato, axis=1))

    # 1. SCHEDA TURNI ATTIVI
    with tab_attivi:
        turni_attivi = turni_miei[condizione_attivo].copy() if not turni_miei.empty else turni_miei.copy()

        if not turni_attivi.empty:
            turni_attivi = turni_attivi.sort_values(
                by=['data_dt', 'ora_inizio_prevista', 'id_turno'],
                ascending=[True, True, True]
            )

        if turni_attivi.empty:
            st.success("🎉 Non ci sono turni da completare! Tutti i turni svolti sono stati archiviati regolarmente nello Storico.")
        else:
            giorno_precedente = None

            for _, t in turni_attivi.iterrows():
                d_attuale = t.get('data_dt')

                if d_attuale != giorno_precedente:
                    giorno_precedente = d_attuale
                    if pd.notna(d_attuale):
                        nome_g = GIORNI_IT[d_attuale.weekday()]
                        d_label = f"{nome_g} {d_attuale.strftime('%d/%m/%Y')}"
                        if d_attuale == oggi:
                            d_label += " (OGGI)"
                    else:
                        d_label = "Data Non Riconosciuta"
                    st.markdown(f"### 🗓️ Turni di {d_label}")
                    st.markdown("---")

                id_t = str(t['id_turno']).strip()
                id_p = str(t.get('id_postazione', '')).strip()
                d_mostrata = t.get('data', 'Data N/D')
                data_oggettiva = d_attuale if pd.notna(d_attuale) else data_italiana()
                ora_ini = t.get('ora_inizio_prevista', '-')
                ora_fin = t.get('ora_fine_prevista', '-')
                c_in = t.get('check_in_effettivo', '')
                c_out = t.get('check_out_effettivo', '')

                p_info = df_post[colonna_uguale(df_post['id_postazione'], id_p)]
                nome_posto = p_info.iloc[0]['nome_cliente'] if not p_info.empty else id_p
                indirizzo_posto = str(p_info.iloc[0]['indirizzo_sede']).strip() if not p_info.empty else ""

                url_maps = f"https://www.google.com/maps/search/?api=1&query={urllib.parse.quote(indirizzo_posto if indirizzo_posto else nome_posto)}"

                with st.container():
                    st.markdown(f"#### 📍 {nome_posto} — Turno `{id_t}`")
                    c_inf1, c_inf2 = st.columns([3, 1.5])
                    with c_inf1:
                        st.write(f"📅 **Data:** {d_mostrata} | 🕒 **Orario:** {ora_ini} - {ora_fin}")
                        st.write(f"🏢 **Indirizzo:** {indirizzo_posto if indirizzo_posto else 'Non specificato'}")
                        st.link_button("🗺️ Apri Navigatore Google Maps", url_maps)
                    with c_inf2:
                        st.write(f"**Check-in:** {f'✅ {c_in}' if timbrato(c_in) else '⏳ Da effettuare'}")
                        st.write(f"**Check-out:** {f'✅ {c_out}' if timbrato(c_out) else '⏳ Da effettuare'}")

                    gia_fatto_in = timbrato(c_in)
                    gia_fatto_out = timbrato(c_out)

                    with st.expander(f"✍️ Timbra Servizio, Carica Foto o Aggiungi Foto Extra - Turno {id_t}"):
                        tab_in, tab_out, tab_extra = st.tabs([
                            "🟢 Check-in (Entrata)",
                            "🔴 Check-out (Uscita)",
                            "➕ Aggiungi Foto Extra"
                        ])

                        with tab_in:
                            if gia_fatto_in:
                                st.success(f"✅ Check-in già registrato in data {c_in}.")
                                st.button("✅ Check-in già eseguito", disabled=True, key=f"btn_dis_in_{id_t}", width="stretch")
                            else:
                                render_timbratura("IN", id_t, op, data_oggettiva, nome_posto)

                        with tab_out:
                            if gia_fatto_out:
                                st.success(f"✅ Check-out già registrato in data {c_out}.")
                                st.button("🔴 Check-out già eseguito", disabled=True, key=f"btn_dis_out_{id_t}", width="stretch")
                            else:
                                render_timbratura("OUT", id_t, op, data_oggettiva, nome_posto)

                        with tab_extra:
                            st.info("Carica ulteriori foto extra per questo turno nella tua cartella.")
                            foto_extra = st.file_uploader("Seleziona Foto Extra:", type=["jpg", "jpeg", "png"], key=f"fextra_{id_t}")
                            desc_extra = st.text_input("Nota / Dettaglio foto (opzionale):", value="Controllo_Extra", key=f"desc_extra_{id_t}")

                            if st.button("📤 Carica Foto Extra su Cloud", type="primary", width="stretch", disabled=foto_extra is None, key=f"btn_extra_{id_t}"):
                                try:
                                    salva_foto_su_storage(foto_extra, data_oggettiva, nome_posto, op['id'], op['nome'], f"{id_t}_{pulisci_nome(desc_extra)}", "EXTRA", (st.session_state.get("gps_corrente") or {}).get("file", "GPS_NON_RILEVATO"))
                                    registra_log(op["nome"], "CARICAMENTO_FOTO_EXTRA", f"Turno {id_t} - {nome_posto}")
                                    st.success("✅ Foto extra caricata correttamente!")
                                except Exception:
                                    st.error("❌ Foto non caricata. Controlla la connessione e riprova.")

                    st.markdown("---")

    # 2. LA MIA SETTIMANA
    with tab_settimana_op:
        st.subheader("📆 Le Mie Postazioni della Settimana")
        data_rif_op = st.date_input("Settimana contenente il giorno:", value=data_italiana(), key="dt_ref_op_sett")
        render_settimana_dipendente(turni_miei, data_rif_op, f"{op['nome']}")

    # 3. SCHEDA STORICO COMPLETO PASSATI
    with tab_storico:
        st.subheader("📜 Storico Completo dei Tuoi Turni")
        if turni_miei.empty:
            turni_passati = turni_miei.copy()
        else:
            turni_passati = turni_miei[~condizione_attivo].copy()
            turni_passati = turni_passati.sort_values(by='data_dt', ascending=False)

        if not turni_passati.empty:
            recap_storico = []
            for _, r_s in turni_passati.iterrows():
                id_p_s = str(r_s.get('id_postazione', '')).strip()
                p_r = df_post[colonna_uguale(df_post['id_postazione'], id_p_s)]
                nome_p_s = p_r.iloc[0]['nome_cliente'] if not p_r.empty else id_p_s

                recap_storico.append({
                    "Turno ID": r_s.get('id_turno', ''),
                    "Data": r_s.get('data', ''),
                    "Mese": r_s.get('Mese_Anno', ''),
                    "Postazione": nome_p_s,
                    "Orario Previsto": f"{r_s.get('ora_inizio_prevista', '-')} - {r_s.get('ora_fine_prevista', '-')}",
                    "Entrata (Check-in)": r_s.get('check_in_effettivo') if timbrato(r_s.get('check_in_effettivo')) else "❌ Non timbrato",
                    "Uscita (Check-out)": r_s.get('check_out_effettivo') if timbrato(r_s.get('check_out_effettivo')) else "❌ Non timbrato"
                })

            df_st_view = pd.DataFrame(recap_storico)
            mesi_storico = ["Tutti i Mesi"] + ordina_mesi(df_st_view['Mese'].dropna().unique())
            scelta_m_op = st.selectbox("Filtra Storico per Mese:", mesi_storico, key="filtro_storico_op")

            if scelta_m_op != "Tutti i Mesi":
                df_st_view = df_st_view[df_st_view['Mese'] == scelta_m_op]

            st.dataframe(df_st_view, width="stretch")
        else:
            st.info("Nessun turno archiviato nello storico.")

    # 4. SCHEDA FOTO CARICATE
    with tab_foto_op:
        st.subheader(f"📸 Foto caricate da te ({op['nome']})")
        if turni_miei.empty:
            mie_foto = turni_miei
        else:
            mie_foto = turni_miei[
                (turni_miei['foto_postazione'].notna()) &
                (turni_miei['foto_postazione'] != '')
            ]
        if not mie_foto.empty:
            cols = st.columns(3)
            idx_f = 0
            for _, r_f in mie_foto.iterrows():
                for url_f in elenco_foto(r_f['foto_postazione']):
                    with cols[idx_f % 3]:
                        try:
                            st.image(url_f, caption=f"{tipo_foto(url_f)} — Turno: {r_f['id_turno']} ({r_f['data']})", width="stretch")
                        except Exception:
                            pass
                    idx_f += 1
        else:
            st.info("Nessuna foto salvata su Supabase Storage associata ai tuoi turni.")

    st.stop()

# -------------------------------------------------------------------------------------------------
# AREA ADMIN (TIZIANA & RINO)
# -------------------------------------------------------------------------------------------------
if st.session_state["ruolo"] == "admin":
    adm = st.session_state["utente_corrente"]
    c_top1, c_top2 = st.columns([4, 1.2])
    with c_top1:
        st.markdown(f"### 🦉 Controllo Operativo Cloud — **{adm['nome']}**")
    with c_top2:
        if st.button("🚪 Esci", width="stretch"):
            registra_log(adm["nome"], "LOGOUT", "Disconnessione admin")
            st.session_state["autenticato"] = False
            st.rerun()

    st.markdown("---")
    st.sidebar.markdown("### 📌 Menu Navigazione")
    menu_admin = st.sidebar.radio(
        "Seleziona Sezione:",
        [
            "🏢 Vista Postazione (Controllo Settimanale)",
            "👤 Posizioni Dipendente (Settimana)",
            "📅 Gestione Turni per Postazione",
            "📊 File Recap & Controllo Postazioni",
            "👥 Gestione Dipendenti (Modifica/Aggiungi)",
            "📍 Gestione Postazioni (Modifica/Aggiungi)",
            "💶 Piano Economico (Fatturato & Ore)",
            "📁 Foto Postazioni Cloud",
            "🛡️ Registro Modifiche (Audit Log)"
        ],
        label_visibility="collapsed"
    )

    # 1. VISTA POSTAZIONE SETTIMANALE
    if menu_admin == "🏢 Vista Postazione (Controllo Settimanale)":
        st.subheader("🏢 Copertura Settimanale per Postazione")

        c_sel_p, c_sel_d = st.columns([2.5, 1.5])
        with c_sel_p:
            map_p = {f"{r['id_postazione']} - {r['nome_cliente']}": str(r['id_postazione']).strip() for _, r in df_post.iterrows()} if not df_post.empty else {}
            scelta_p_str = st.selectbox("Seleziona Postazione da monitorare:", list(map_p.keys())) if map_p else None
            id_p_selezionato = map_p[scelta_p_str] if scelta_p_str else None

        with c_sel_d:
            data_riferimento = st.date_input("Settimana contenente il giorno:", value=data_italiana(), key="dt_ref_post")

        if id_p_selezionato:
            inizio_sett = data_riferimento - timedelta(days=data_riferimento.weekday())
            fine_sett = inizio_sett + timedelta(days=6)
            st.markdown(f"#### Settimana dal `{inizio_sett.strftime('%d/%m/%Y')}` al `{fine_sett.strftime('%d/%m/%Y')}`")

            turni_post = df_turni[colonna_uguale(df_turni['id_postazione'], id_p_selezionato)].copy()
            righe_sett = []

            for i in range(7):
                g_curr = inizio_sett + timedelta(days=i)
                tg = turni_post[turni_post['data_dt'] == g_curr]

                if not tg.empty:
                    tg = tg.sort_values(by='ora_inizio_prevista', ascending=True)
                    for _, t in tg.iterrows():
                        cin = t.get('check_in_effettivo', '')
                        cout = t.get('check_out_effettivo', '')
                        righe_sett.append({
                            "Giorno": f"{GIORNI_IT[i]} ({g_curr.strftime('%d/%m')})",
                            "Turno ID": t.get('id_turno', ''),
                            "Cognome Guardia": t.get('cognome_guardia', ''),
                            "Orario": f"{t.get('ora_inizio_prevista', '-')} - {t.get('ora_fine_prevista', '-')}",
                            "Entrata (Check-in)": cin if timbrato(cin) else "⏳ Non timbrato",
                            "GPS Entrata": testo(t.get('gps_check_in')) or "-",
                            "Uscita (Check-out)": cout if timbrato(cout) else "⏳ Non timbrato",
                            "GPS Uscita": testo(t.get('gps_check_out')) or "-",
                            "Registrato Da": t.get('registrato_da', 'Sistema')
                        })
                else:
                    righe_sett.append({
                        "Giorno": f"{GIORNI_IT[i]} ({g_curr.strftime('%d/%m')})",
                        "Turno ID": "-", "Cognome Guardia": "❌ NESSUNA GUARDIA", "Orario": "-",
                        "Entrata (Check-in)": "-", "GPS Entrata": "-", "Uscita (Check-out)": "-", "GPS Uscita": "-", "Registrato Da": "-"
                    })
            st.dataframe(pd.DataFrame(righe_sett), width="stretch")

    # 1-bis. POSIZIONI DIPENDENTE (SETTIMANA)  <-- NUOVA SEZIONE
    elif menu_admin == "👤 Posizioni Dipendente (Settimana)":
        st.subheader("👤 Posizioni Ricoperte da un Dipendente durante la Settimana")
        st.caption("Scegli un dipendente e una settimana: vedrai in quali postazioni lavora ogni giorno, gli orari, le ore totali e le timbrature.")

        if df_dip.empty:
            st.info("Nessun dipendente presente in anagrafica.")
        else:
            df_dip_ord = df_dip.sort_values(by='cognome', key=lambda s: s.astype(str).str.lower())
            map_dip_sett = {
                f"{r['cognome']} {r.get('nome', '')} ({r['id_guardia']})": (str(r['id_guardia']).strip(), str(r['cognome']).strip())
                for _, r in df_dip_ord.iterrows()
            }

            c_dip, c_data = st.columns([2.5, 1.5])
            with c_dip:
                scelta_dip_sett = st.selectbox("Seleziona Dipendente:", list(map_dip_sett.keys()), key="sel_dip_sett")
            with c_data:
                data_rif_dip = st.date_input("Settimana contenente il giorno:", value=data_italiana(), key="dt_ref_dip_sett")

            id_dip_sel, cogn_dip_sel = map_dip_sett[scelta_dip_sett]
            turni_dip_sel = turni_del_dipendente(id_dip_sel, cogn_dip_sel)
            render_settimana_dipendente(turni_dip_sel, data_rif_dip, scelta_dip_sett)

    # 2. GESTIONE TURNI PER POSTAZIONE
    elif menu_admin == "📅 Gestione Turni per Postazione":
        st.subheader("📅 Aggiunta & Gestione Turni per Ciascuna Postazione")
        if not df_post.empty:
            for _, post in df_post.iterrows():
                id_pst = str(post['id_postazione']).strip()
                nome_pst = post['nome_cliente']

                with st.expander(f"📍 Postazione: {id_pst} — {nome_pst}"):
                    turni_questa_post = df_turni[colonna_uguale(df_turni['id_postazione'], id_pst)].copy()

                    if not turni_questa_post.empty:
                        st.write("**Turni programmati (Seleziona per eliminare):**")

                        df_del_view = turni_questa_post[['id_turno', 'data', 'cognome_guardia', 'ora_inizio_prevista', 'ora_fine_prevista', 'check_in_effettivo', 'check_out_effettivo']].copy()
                        df_del_view.insert(0, "Seleziona", False)

                        edited_del = st.data_editor(
                            df_del_view,
                            width="stretch",
                            hide_index=True,
                            key=f"editor_del_{id_pst}"
                        )

                        if st.button(f"🗑️ Elimina Turni Selezionati da {nome_pst}", key=f"btn_del_{id_pst}", type="secondary"):
                            selezionati_del = edited_del[edited_del['Seleziona'] == True]
                            if not selezionati_del.empty:
                                ids_del = selezionati_del['id_turno'].tolist()
                                for tid in ids_del:
                                    supabase.table("turni").delete().eq("id_turno", tid).execute()

                                registra_log(adm["nome"], "CANCELLAZIONE_TURNI", f"Eliminati turni da {nome_pst}: {ids_del}")
                                st.success(f"✅ Eliminati con successo {len(ids_del)} turni!")
                                time.sleep(0.5)
                                st.rerun()
                            else:
                                st.warning("Spunta almeno un turno da eliminare nella tabella.")
                    else:
                        st.info("Nessun turno programmato su questa postazione.")

                    st.markdown("---")
                    st.markdown("##### ➕ Aggiungi Turno:")
                    with st.form(f"form_add_turno_{id_pst}"):
                        c_t1, c_t2, c_t3 = st.columns(3)
                        with c_t1:
                            id_nuovo_t = f"T{int(time.time() * 1000)}"
                            st.text_input("ID Turno (Generato Auto)", value=id_nuovo_t, disabled=True, key=f"id_t_{id_pst}")
                            data_nuovo_t = st.date_input("Data Servizio", value=data_italiana(), key=f"d_t_{id_pst}")
                        with c_t2:
                            scelte_g = [f"{r['cognome']} {r['nome']} ({r['id_guardia']})" for _, r in df_dip.iterrows()] if not df_dip.empty else ["Mirra Girolamo (G001)"]
                            guardia_sel = st.selectbox("Dipendente Assegnato:", scelte_g, key=f"g_sel_{id_pst}")
                        with c_t3:
                            ora_ini = st.text_input("Ora Inizio Prevista", value="22:00", key=f"oi_{id_pst}")
                            ora_fin = st.text_input("Ora Fine Prevista", value="06:00", key=f"of_{id_pst}")

                        match_id = re.search(r'\((.*?)\)', guardia_sel)
                        g_codice = match_id.group(1) if match_id else guardia_sel.split()[0]

                        dip_trovato = df_dip[df_dip['id_guardia'].astype(str).str.strip().str.lower() == g_codice.lower()]
                        cognome_selezionato = dip_trovato.iloc[0]['cognome'].strip() if not dip_trovato.empty else guardia_sel.split()[0]

                        data_formattata_db = data_nuovo_t.strftime("%d/%m/%Y")

                        # Controllo conflitti (confronto sulla data normalizzata)
                        if df_turni.empty:
                            conflitti = df_turni
                        else:
                            conflitti = df_turni[
                                ((df_turni['id_guardia'].astype(str).str.strip().str.lower() == g_codice.lower()) |
                                 (df_turni['cognome_guardia'].astype(str).str.strip().str.lower() == cognome_selezionato.lower())) &
                                (df_turni['data_dt'] == data_nuovo_t)
                            ]

                        forza_creazione = False
                        if not conflitti.empty:
                            st.warning(f"⚠️ ATTENZIONE: Il dipendente {guardia_sel} risulta GIÀ assegnato il giorno {data_formattata_db}!")
                            forza_creazione = st.checkbox("Conferma comunque turno doppio", key=f"chk_force_{id_pst}")

                        btn_crea = st.form_submit_button("💾 Registra Turno su Cloud", type="primary")

                        if btn_crea:
                            if not conflitti.empty and not forza_creazione:
                                st.error("Operazione bloccata: conferma la casella per il turno doppio.")
                            else:
                                record = {
                                    "id_turno": str(id_nuovo_t).strip(),
                                    "data": data_formattata_db,
                                    "id_guardia": str(g_codice).strip(),
                                    "cognome_guardia": str(cognome_selezionato).strip(),
                                    "id_postazione": str(id_pst).strip(),
                                    "ora_inizio_prevista": str(ora_ini).strip(),
                                    "ora_fine_prevista": str(ora_fin).strip(),
                                    "registrato_da": str(adm["nome"]).strip()
                                }
                                try:
                                    supabase.table("turni").insert(record).execute()
                                    nota = f"Creato turno {id_nuovo_t} per {cognome_selezionato} in data {data_formattata_db}" + (" [FORZATO]" if not conflitti.empty else "")
                                    registra_log(adm["nome"], "CREAZIONE_TURNO", nota)
                                    st.success("✅ Turno salvato su Cloud con successo!")
                                    time.sleep(0.8)
                                    st.rerun()
                                except Exception as err_db:
                                    st.error(f"❌ Errore critico di scrittura su Supabase: {err_db}")

    # 3. RECAP GENERALE POSTAZIONI
    elif menu_admin == "📊 File Recap & Controllo Postazioni":
        st.subheader("📊 File Recap Operativo Completo (Sincronizzato Cloud)")
        if not df_turni.empty:
            recap_df = df_turni.merge(df_post[['id_postazione', 'nome_cliente', 'indirizzo_sede']], on='id_postazione', how='left')
            tutti_i_mesi = ["Tutti i Mesi"] + ordina_mesi(recap_df['Mese_Anno'].dropna().unique())
            scelta_m = st.selectbox("Filtra per Mese:", tutti_i_mesi)

            view_recap = recap_df if scelta_m == "Tutti i Mesi" else recap_df[recap_df['Mese_Anno'] == scelta_m]
            colonne_show = ['id_turno', 'data', 'Mese_Anno', 'id_postazione', 'nome_cliente', 'indirizzo_sede', 'cognome_guardia', 'ora_inizio_prevista', 'ora_fine_prevista', 'check_in_effettivo', 'gps_check_in', 'check_out_effettivo', 'gps_check_out', 'registrato_da']
            colonne_show = [c for c in colonne_show if c in view_recap.columns]
            st.dataframe(view_recap[colonne_show], width="stretch")

            st.download_button(
                "📥 Scarica Recap (.CSV)",
                data=view_recap[colonne_show].to_csv(index=False).encode('utf-8'),
                file_name="recap_turni_supabase.csv",
                mime="text/csv"
            )

    # 4. GESTIONE DIPENDENTI
    elif menu_admin == "👥 Gestione Dipendenti (Modifica/Aggiungi)":
        st.subheader("👥 Gestione Personale Dipendente")
        tab_mod, tab_agg = st.tabs(["✏️ Modifica Dipendente Esistente / Password", "➕ Aggiungi Nuovo Dipendente"])

        with tab_mod:
            if not df_dip.empty:
                opzioni_mod = {f"{r['cognome']} {r['nome']} ({r['id_guardia']})": str(r['id_guardia']).strip() for _, r in df_dip.iterrows()}
                scelta_guardia_mod = st.selectbox("Seleziona Dipendente:", list(opzioni_mod.keys()))
                id_g_mod = opzioni_mod[scelta_guardia_mod]
                riga_g = df_dip[colonna_uguale(df_dip['id_guardia'], id_g_mod)].iloc[0]

                with st.form(f"form_modifica_{id_g_mod}"):
                    cm1, cm2 = st.columns(2)
                    with cm1:
                        mod_cognome = st.text_input("Cognome (Username Login):", value=testo(riga_g.get('cognome')))
                        mod_nome = st.text_input("Nome:", value=testo(riga_g.get('nome')))
                    with cm2:
                        mod_email = st.text_input("Email:", value=testo(riga_g.get('email')))
                        mod_pwd = st.text_input("Nuova Password (lascia vuoto per non cambiarla):", value="", type="password")

                    if st.form_submit_button("💾 Salva Modifiche su Cloud", type="primary"):
                        if not mod_cognome.strip():
                            st.error("Il cognome è obbligatorio.")
                        else:
                            dati_mod = {
                                "cognome": mod_cognome.strip(),
                                "nome": mod_nome.strip(),
                                "email": mod_email.strip()
                            }
                            if mod_pwd.strip():
                                dati_mod["password"] = cifra_password(mod_pwd.strip())
                            try:
                                supabase.table("dipendenti").update(dati_mod).eq("id_guardia", riga_g['id_guardia']).execute()
                                registra_log(adm["nome"], "MODIFICA_DIPENDENTE", f"Aggiornato {mod_cognome} ({id_g_mod})" + (" + password" if mod_pwd.strip() else ""))
                                st.success("Dati aggiornati su Cloud!")
                                time.sleep(0.8)
                                st.rerun()
                            except Exception as err_db:
                                st.error(f"❌ Errore di scrittura su Supabase: {err_db}")

        with tab_agg:
            with st.form("form_nuovo_dipendente"):
                c_d1, c_d2 = st.columns(2)
                with c_d1:
                    nuovo_id_g = st.text_input("Codice Guardia", value=prossimo_codice(df_dip['id_guardia'] if not df_dip.empty else [], "G"))
                    nuovo_cognome = st.text_input("Cognome (Username per Login)")
                    nuovo_nome = st.text_input("Nome")
                with c_d2:
                    nuova_email = st.text_input("Email")
                    nuova_pwd = st.text_input("Password Iniziale", value=PASSWORD_PREDEFINITA_DIP)

                if st.form_submit_button("💾 Salva Nuovo Dipendente", type="primary"):
                    id_esistenti = set(df_dip['id_guardia'].apply(testo).str.lower()) if not df_dip.empty else set()
                    if not nuovo_cognome.strip() or not nuovo_id_g.strip():
                        st.error("Codice guardia e cognome sono obbligatori.")
                    elif nuovo_id_g.strip().lower() in id_esistenti:
                        st.error(f"Il codice {nuovo_id_g.strip()} è già usato da un altro dipendente: scegline uno diverso.")
                    else:
                        try:
                            supabase.table("dipendenti").insert({
                                "id_guardia": nuovo_id_g.strip(),
                                "cognome": nuovo_cognome.strip(),
                                "nome": nuovo_nome.strip(),
                                "email": nuova_email.strip(),
                                "password": cifra_password(nuova_pwd.strip() or PASSWORD_PREDEFINITA_DIP)
                            }).execute()
                            registra_log(adm["nome"], "AGGIUNGI_DIPENDENTE", f"Creato {nuovo_cognome} ({nuovo_id_g})")
                            st.success(f"Dipendente {nuovo_cognome} registrato!")
                            time.sleep(0.8)
                            st.rerun()
                        except Exception as err_db:
                            st.error(f"❌ Errore di scrittura su Supabase: {err_db}")

        st.markdown("#### Anagrafica Attiva")
        # La colonna password non viene più mostrata in tabella
        colonne_anagrafica = [c for c in ['id_guardia', 'cognome', 'nome', 'email'] if c in df_dip.columns]
        st.dataframe(df_dip[colonne_anagrafica], width="stretch")

    # 5. GESTIONE POSTAZIONI
    elif menu_admin == "📍 Gestione Postazioni (Modifica/Aggiungi)":
        st.subheader("📍 Gestione Postazioni di Lavoro")
        tab_mod_p, tab_agg_p = st.tabs(["✏️ Modifica / Elimina Postazione Esistente", "➕ Aggiungi Nuova Postazione"])

        with tab_mod_p:
            if not df_post.empty:
                df_post_sorted = df_post.sort_values(by='id_postazione', ascending=True)
                map_mod_p = {f"{r['id_postazione']} - {r['nome_cliente']}": str(r['id_postazione']).strip() for _, r in df_post_sorted.iterrows()}

                scelta_p_mod = st.selectbox("Seleziona Postazione:", list(map_mod_p.keys()))
                id_pst_sel = map_mod_p[scelta_p_mod]
                riga_post = df_post_sorted[colonna_uguale(df_post_sorted['id_postazione'], id_pst_sel)].iloc[0]

                with st.form(f"form_modifica_post_{id_pst_sel}"):
                    cp_m1, cp_m2 = st.columns(2)
                    with cp_m1:
                        mod_nome_post = st.text_input("Nome Cliente / Sede:", value=str(riga_post.get('nome_cliente', '')))
                    with cp_m2:
                        mod_ind_post = st.text_input("Indirizzo Completo (Google Maps):", value=str(riga_post.get('indirizzo_sede', '')))

                    conferma_elimina_p = st.checkbox("Confermo di voler eliminare definitivamente questa postazione")
                    c_salva, c_elimina = st.columns([1, 1])
                    with c_salva:
                        btn_salva_p = st.form_submit_button("💾 Salva Modifiche", type="primary", width="stretch")
                    with c_elimina:
                        btn_elimina_p = st.form_submit_button("🗑️ Elimina Postazione", type="secondary", width="stretch")

                    if btn_salva_p:
                        supabase.table("postazioni").update({
                            "nome_cliente": mod_nome_post.strip(),
                            "indirizzo_sede": mod_ind_post.strip()
                        }).eq("id_postazione", id_pst_sel).execute()
                        registra_log(adm["nome"], "MODIFICA_POSTAZIONE", f"Aggiornata {mod_nome_post} ({id_pst_sel})")
                        st.success("Postazione aggiornata su Cloud!")
                        st.rerun()

                    if btn_elimina_p and not conferma_elimina_p:
                        st.error("Per eliminare la postazione spunta prima la casella di conferma.")
                    elif btn_elimina_p:
                        supabase.table("postazioni").delete().eq("id_postazione", id_pst_sel).execute()
                        registra_log(adm["nome"], "ELIMINAZIONE_POSTAZIONE", f"Eliminata postazione {id_pst_sel} - {riga_post.get('nome_cliente')}")
                        st.success(f"Postazione {id_pst_sel} eliminata con successo!")
                        st.rerun()

        with tab_agg_p:
            with st.form("form_nuova_postazione"):
                c_p1, c_p2 = st.columns(2)
                with c_p1:
                    nuovo_id_p = st.text_input("ID Postazione", value=prossimo_codice(df_post['id_postazione'] if not df_post.empty else [], "P"))
                    nuovo_nome_p = st.text_input("Nome Cliente / Sede")
                with c_p2:
                    nuovo_ind_p = st.text_input("Indirizzo Completo (per Google Maps)")

                if st.form_submit_button("💾 Salva Nuova Postazione", type="primary"):
                    id_p_esistenti = set(df_post['id_postazione'].apply(testo).str.lower()) if not df_post.empty else set()
                    if not nuovo_nome_p.strip() or not nuovo_id_p.strip():
                        st.error("ID postazione e nome del cliente sono obbligatori.")
                    elif nuovo_id_p.strip().lower() in id_p_esistenti:
                        st.error(f"L'ID {nuovo_id_p.strip()} è già usato da un'altra postazione: scegline uno diverso.")
                    else:
                        try:
                            supabase.table("postazioni").insert({
                                "id_postazione": nuovo_id_p.strip(),
                                "nome_cliente": nuovo_nome_p.strip(),
                                "indirizzo_sede": nuovo_ind_p.strip()
                            }).execute()
                            registra_log(adm["nome"], "AGGIUNGI_POSTAZIONE", f"Creata {nuovo_nome_p} ({nuovo_id_p})")
                            st.success(f"Postazione {nuovo_nome_p} registrata!")
                            time.sleep(0.8)
                            st.rerun()
                        except Exception as err_db:
                            st.error(f"❌ Errore di scrittura su Supabase: {err_db}")

        st.markdown("#### Elenco Postazioni Attive (Ordinate per ID)")
        st.dataframe(df_post.sort_values(by='id_postazione', ascending=True), width="stretch")

    # 6. PIANO ECONOMICO
    elif menu_admin == "💶 Piano Economico (Fatturato & Ore)":
        st.subheader("💶 Piano Economico & Monitoraggio Finanziario")
        tab_eco_fatturato, tab_eco_dettaglio = st.tabs([
            "📈 1. Ore Lavorate per Mese",
            "🏢 2. Dettaglio Ore Postazione & Elenco Guardie"
        ])

        with tab_eco_fatturato:
            if not df_turni.empty:
                st.markdown("#### 🕒 Ore Svolte per Dipendente Suddivise per Mese")

                df_calc = df_turni.copy()
                df_calc['Ore_Turno'] = df_calc.apply(estrai_ore_t, axis=1)

                mesi_validi = ordina_mesi(df_calc['Mese_Anno'].dropna().unique())
                mesi_validi = ["Tutti i Mesi"] + mesi_validi if mesi_validi else ["Tutti i Mesi"]

                col_m_eco, _ = st.columns([2, 2])
                with col_m_eco:
                    mese_scelto_eco = st.selectbox("Seleziona Mese:", mesi_validi)

                df_calc_filtro = df_calc if mese_scelto_eco == "Tutti i Mesi" else df_calc[df_calc['Mese_Anno'] == mese_scelto_eco]
                tot_dip_mese = df_calc_filtro.groupby(['cognome_guardia', 'Mese_Anno'])['Ore_Turno'].sum().reset_index()
                tot_dip_mese.columns = ['Cognome Dipendente', 'Mese', 'Ore Svolte']
                tot_dip_mese['Ore Svolte'] = tot_dip_mese['Ore Svolte'].round(2)
                ordine_mesi = {m: i for i, m in enumerate(ordina_mesi(tot_dip_mese['Mese'].unique()))}
                tot_dip_mese['_pos'] = tot_dip_mese['Mese'].map(ordine_mesi).fillna(len(ordine_mesi))
                tot_dip_mese = tot_dip_mese.sort_values(by=['_pos', 'Cognome Dipendente']).drop(columns=['_pos'])
                st.dataframe(tot_dip_mese, width="stretch", hide_index=True)

        with tab_eco_dettaglio:
            if not df_turni.empty:
                df_dett = df_turni.copy()
                df_dett['Ore_Turno'] = df_dett.apply(estrai_ore_t, axis=1)
                mesi_disp_tab = ordina_mesi(df_dett['Mese_Anno'].dropna().unique())
                map_pst = {f"{r['id_postazione']} - {r['nome_cliente']}": str(r['id_postazione']).strip() for _, r in df_post.iterrows()}

                cp1, cp2 = st.columns(2)
                with cp1:
                    mese_filtro = st.selectbox("Mese di Riferimento:", mesi_disp_tab if mesi_disp_tab else ["09/2026"])
                with cp2:
                    post_filtro_str = st.selectbox("Postazione:", list(map_pst.keys())) if map_pst else None
                    id_pst_filtro = map_pst[post_filtro_str] if post_filtro_str else None

                turni_filtrati = df_dett[
                    (df_dett['Mese_Anno'] == mese_filtro) &
                    (colonna_uguale(df_dett['id_postazione'], id_pst_filtro))
                ].copy()

                if not turni_filtrati.empty:
                    ore_tot = round(turni_filtrati['Ore_Turno'].sum(), 2)
                    st.success(f"📌 **Ore totali svolte presso {post_filtro_str} nel mese {mese_filtro}: {ore_tot} ore**")

                    guardie_agg = turni_filtrati.groupby('cognome_guardia')['Ore_Turno'].agg(['count', 'sum']).reset_index()
                    guardie_agg.columns = ['Cognome Guardia', 'Numero Turni', 'Ore Complessive']
                    guardie_agg['Ore Complessive'] = guardie_agg['Ore Complessive'].round(2)
                    st.dataframe(guardie_agg, width="stretch")

                    tabella_exp = turni_filtrati[['id_turno', 'data', 'cognome_guardia', 'ora_inizio_prevista', 'ora_fine_prevista', 'check_in_effettivo', 'check_out_effettivo', 'Ore_Turno', 'registrato_da']]
                    st.dataframe(tabella_exp, width="stretch")

                    st.download_button(
                        "📥 Scarica File Report (.CSV)",
                        data=tabella_exp.to_csv(index=False).encode('utf-8'),
                        file_name=f"Report_Ore_{id_pst_filtro}_{mese_filtro.replace('/', '_')}.csv",
                        mime="text/csv"
                    )
                else:
                    st.info(f"Nessun turno registrato per questa postazione nel mese {mese_filtro}.")

    # 7. FOTO CLOUD (SUDDIVISE PER CARTELLA: OPERATORE ➔ DATA ➔ POSTAZIONE)
    elif menu_admin == "📁 Foto Postazioni Cloud":
        st.subheader("📁 Foto Archiviate su Supabase Storage (Visualizzazione a Cartelle)")
        st.caption("Naviga tra le cartelle degli operatori, le date e le postazioni per visualizzare o eliminare le foto.")

        try:
            root_items = lista_storage()
            operatori_cartelle = [item["name"] for item in root_items if "." not in item["name"]]

            if operatori_cartelle:
                op_scelto = st.selectbox("👤 Seleziona Operatore:", sorted(operatori_cartelle))

                if op_scelto:
                    date_items = lista_storage(op_scelto)
                    date_cartelle = [item["name"] for item in date_items if "." not in item["name"]]

                    if date_cartelle:
                        data_scelta = st.selectbox("📅 Seleziona Data:", sorted(date_cartelle, reverse=True))

                        if data_scelta:
                            path_pos = f"{op_scelto}/{data_scelta}"
                            pos_items = lista_storage(path_pos)
                            pos_cartelle = [item["name"] for item in pos_items if "." not in item["name"]]

                            if pos_cartelle:
                                pos_scelta = st.selectbox("📍 Seleziona Postazione:", sorted(pos_cartelle))

                                if pos_scelta:
                                    path_file_finali = f"{op_scelto}/{data_scelta}/{pos_scelta}"
                                    file_items = lista_storage(path_file_finali)

                                    immagini_trovate = [item for item in file_items if item.get("name") and "." in item.get("name")]

                                    if immagini_trovate:
                                        st.markdown(f"#### Immagini in: `{path_file_finali}`")
                                        st.write("Spunta le foto che desideri eliminare definitivamente:")

                                        with st.form("form_elimina_foto_gerarchico"):
                                            paths_selezionati = []
                                            cols = st.columns(3)

                                            for idx_f, f_item in enumerate(immagini_trovate):
                                                nome_f = f_item["name"]
                                                f_path_rel = f"{path_file_finali}/{nome_f}"
                                                url_pub = supabase.storage.from_(BUCKET_FOTO).get_public_url(f_path_rel)

                                                with cols[idx_f % 3]:
                                                    st.image(url_pub, caption=f"📄 {nome_f.rsplit('.', 1)[0].replace('_', ' ')}", width="stretch")
                                                    if st.checkbox("Seleziona foto", key=f"chk_ger_{idx_f}_{nome_f}"):
                                                        paths_selezionati.append(f_path_rel)

                                            st.markdown("---")
                                            btn_del_ger = st.form_submit_button("🗑️ Elimina Foto Selezionate", type="primary", width="stretch")

                                            if btn_del_ger:
                                                if paths_selezionati:
                                                    try:
                                                        supabase.storage.from_(BUCKET_FOTO).remove(paths_selezionati)
                                                        registra_log(adm["nome"], "CANCELLAZIONE_FOTO_CLOUD", f"Eliminate {len(paths_selezionati)} foto dal percorso {path_file_finali}")
                                                        st.success(f"✅ {len(paths_selezionati)} foto eliminate con successo dal cloud!")
                                                        time.sleep(1)
                                                        st.rerun()
                                                    except Exception as e_del:
                                                        st.error(f"Errore durante l'eliminazione: {e_del}")
                                                else:
                                                    st.warning("Seleziona almeno una foto spuntando la casella.")
                                    else:
                                        st.info("Nessuna foto presente in questa cartella postazione.")
                            else:
                                st.info("Nessuna postazione trovata per questa data.")
                    else:
                        st.info("Nessuna data registrata per questo operatore.")
            else:
                st.info("Nessuna cartella operatore trovata nel bucket Supabase Storage.")
        except Exception as e_err:
            st.error(f"Errore di lettura dalle cartelle Storage: {e_err}")

    # 8. AUDIT LOG
    elif menu_admin == "🛡️ Registro Modifiche (Audit Log)":
        st.subheader("🛡️ Storico Azioni Capi Reparto (Supabase Audit)")
        res_log = supabase.table("audit_log").select("*").order("id", desc=True).limit(500).execute()
        df_log = pd.DataFrame(res_log.data)
        if not df_log.empty:
            st.dataframe(df_log[['data_ora', 'autore', 'azione', 'dettagli']], width="stretch")
        else:
            st.info("Nessun log presente.")
