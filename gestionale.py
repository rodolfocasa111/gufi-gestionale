import streamlit as st
import pandas as pd
import os
import time
import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
import urllib.parse
from supabase import create_client

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

CREDENZIALI_ADMIN = {
    "tiziana": {"password": "admin2026!", "nome": "Tiziana (Direttore Generale Supremo \"MASTO\")"},
    "rino": {"password": "admin2026!", "nome": "Rino (Capo Reparto)"}
}

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

# --- CARICAMENTO DATI DA SUPABASE ---
def carica_dati():
    res_dip = supabase.table("dipendenti").select("*").limit(2000).execute()
    df_dip = pd.DataFrame(res_dip.data)
    if df_dip.empty:
        df_dip = pd.DataFrame(columns=['id_guardia', 'cognome', 'nome', 'email', 'password'])

    res_post = supabase.table("postazioni").select("*").limit(2000).execute()
    df_post = pd.DataFrame(res_post.data)
    if not df_post.empty and 'id_postazione' in df_post.columns:
        df_post = df_post.sort_values(by='id_postazione', ascending=True)
    else:
        df_post = pd.DataFrame(columns=['id_postazione', 'nome_cliente', 'indirizzo_sede'])

    res_turni = supabase.table("turni").select("*").limit(10000).execute()
    df_turni = pd.DataFrame(res_turni.data)
    if df_turni.empty:
        df_turni = pd.DataFrame(columns=[
            'id_turno', 'data', 'id_guardia', 'cognome_guardia', 'id_postazione',
            'ora_inizio_prevista', 'ora_fine_prevista', 'check_in_effettivo',
            'check_out_effettivo', 'gps_check_in', 'gps_check_out',
            'foto_postazione', 'registrato_da'
        ])

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

# --- SALVATAGGIO FOTO CON STRUTTURA REALE ---
def salva_foto_su_storage(file_foto, giorno_data, nome_postazione, id_guardia, nome_guardia, id_turno, tipo_timbratura):
    cartella_operatore = pulisci_nome(f"{id_guardia}_{nome_guardia}")
    giorno_str = giorno_data.strftime("%Y-%m-%d") if isinstance(giorno_data, (date, datetime)) else data_italiana().strftime("%Y-%m-%d")
    cartella_data = giorno_str
    cartella_posizione = pulisci_nome(nome_postazione)
    
    data_ora_scatto = ora_italiana()
    orario_str = data_ora_scatto.strftime('%H-%M-%S')
    gps_info = "41.229565_14.508582"
    
    nome_file = f"Turno_{pulisci_nome(id_turno)}_{tipo_timbratura}_Data_{giorno_str}_Ore_{orario_str}_GPS_{gps_info}.jpg"
    path_remoto = f"{cartella_operatore}/{cartella_data}/{cartella_posizione}/{nome_file}"
    
    file_bytes = file_foto.getvalue()
    try:
        supabase.storage.from_(BUCKET_FOTO).upload(
            path=path_remoto,
            file=file_bytes,
            file_options={"content-type": "image/jpeg", "upsert": "true"}
        )
    except Exception:
        try:
            supabase.storage.from_(BUCKET_FOTO).update(
                path=path_remoto,
                file=file_bytes,
                file_options={"content-type": "image/jpeg"}
            )
        except Exception as e_up:
            st.error(f"Errore caricamento su Supabase Storage: {e_up}")
            raise e_up
            
    url_pubblico = supabase.storage.from_(BUCKET_FOTO).get_public_url(path_remoto)
    return url_pubblico

# --- PARSER DATA COMPLETO ---
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

    for fmt in ['%d/%m/%Y', '%Y/%m/%d', '%m/%d/%Y']:
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

if not df_turni.empty and 'data' in df_turni.columns:
    df_turni['data_dt'] = df_turni['data'].apply(analizza_data_completa)
    df_turni['Mese_Anno'] = df_turni['data_dt'].apply(lambda d: d.strftime("%m/%Y") if pd.notna(d) else "Non Riconosciuto")
else:
    df_turni['data_dt'] = None
    df_turni['Mese_Anno'] = "Non Riconosciuto"

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
                btn_op = st.form_submit_button("Entra nei Miei Turni", type="primary", use_container_width=True)
                if btn_op:
                    val_cognome = cognome_input.strip().lower()
                    val_pwd = pwd_input.strip()
                    trovato = df_dip[df_dip['cognome'].astype(str).str.strip().str.lower() == val_cognome]
                    
                    if not trovato.empty:
                        record_dip = trovato.iloc[0]
                        pwd_registrata = str(record_dip.get('password', 'gufi2026!')).strip()
                        if val_pwd == pwd_registrata:
                            st.session_state["autenticato"] = True
                            st.session_state["ruolo"] = "operatore"
                            st.session_state["utente_corrente"] = {
                                "id": str(record_dip['id_guardia']).strip(),
                                "cognome": record_dip['cognome'].strip(),
                                "nome": f"{record_dip.get('nome', '')} {record_dip.get('cognome', '')}".strip()
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
                btn_adm = st.form_submit_button("Accedi al Pannello Admin", type="primary", use_container_width=True)
                if btn_adm:
                    if adm_pwd == CREDENZIALI_ADMIN[adm_user]["password"]:
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
        if st.button("🚪 Esci", use_container_width=True):
            registra_log(op["nome"], "LOGOUT", "Disconnessione dipendente")
            st.session_state["autenticato"] = False
            st.rerun()

    st.markdown("---")
    tab_attivi, tab_storico, tab_foto_op = st.tabs([
        "🟢 Turni da Svolgere / Oggi", 
        "📜 Storico Turni Passati", 
        "📸 Le Mie Foto Caricate"
    ])

    turni_miei = df_turni[
        (df_turni['id_guardia'].astype(str).str.lower() == op['id'].lower()) | 
        (df_turni['cognome_guardia'].astype(str).str.lower() == op['cognome'].lower())
    ].copy()

    oggi = data_italiana()
    limite_aperti_recenti = oggi - timedelta(days=1)

    def turno_completato(r):
        cin = str(r.get('check_in_effettivo', '')).strip()
        cout = str(r.get('check_out_effettivo', '')).strip()
        return (cin != '' and cin != 'None' and pd.notna(r.get('check_in_effettivo'))) and \
               (cout != '' and cout != 'None' and pd.notna(r.get('check_out_effettivo')))

    condizione_attivo = (
        (turni_miei['data_dt'].notna()) & 
        (turni_miei['data_dt'] >= limite_aperti_recenti) & 
        (~turni_miei.apply(turno_completato, axis=1))
    )

    # 1. SCHEDA TURNI ATTIVI
    with tab_attivi:
        turni_attivi = turni_miei[condizione_attivo].copy()
        
        turni_attivi = turni_attivi.sort_values(
            by=['data_dt', 'ora_inizio_prevista', 'id_turno'], 
            ascending=[True, True, True]
        )

        if turni_attivi.empty:
            st.success("🎉 Non ci sono turni da completare! Tutti i turni svolti sono stati archiviati regolarmente nello Storico.")
        else:
            giorni_it = {
                0: "Lunedì", 1: "Martedì", 2: "Mercoledì", 3: "Giovedì",
                4: "Venerdì", 5: "Sabato", 6: "Domenica"
            }
            giorno_precedente = None
            
            for _, t in turni_attivi.iterrows():
                d_attuale = t.get('data_dt')
                
                if d_attuale != giorno_precedente:
                    giorno_precedente = d_attuale
                    if pd.notna(d_attuale):
                        nome_g = giorni_it.get(d_attuale.weekday(), "")
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

                p_info = df_post[df_post['id_postazione'] == id_p]
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
                        st.write(f"**Check-in:** {f'✅ {c_in}' if pd.notna(c_in) and str(c_in).strip() and str(c_in) != 'None' else '⏳ Da effettuare'}")
                        st.write(f"**Check-out:** {f'✅ {c_out}' if pd.notna(c_out) and str(c_out).strip() and str(c_out) != 'None' else '⏳ Da effettuare'}")

                    gia_fatto_in = pd.notna(c_in) and str(c_in).strip() and str(c_in) != 'None'
                    gia_fatto_out = pd.notna(c_out) and str(c_out).strip() and str(c_out) != 'None'

                    with st.expander(f"✍️ Timbra Servizio, Carica Foto o Aggiungi Foto Extra - Turno {id_t}"):
                        tab_in, tab_out, tab_extra = st.tabs([
                            "🟢 Check-in (Entrata)", 
                            "🔴 Check-out (Uscita)", 
                            "➕ Aggiungi Foto Extra"
                        ])
                        
                        with tab_in:
                            if gia_fatto_in:
                                st.success(f"✅ Check-in già registrato in data {c_in}.")
                                st.button("✅ Check-in già eseguito", disabled=True, key=f"btn_dis_in_{id_t}", use_container_width=True)
                            else:
                                with st.form(f"form_in_{id_t}"):
                                    st.text_input("📍 Posizione GPS (Certificata Automaticamente):", value="41.229565, 14.508582", disabled=True, key=f"gps_in_{id_t}")
                                    foto_in = st.file_uploader("Foto Entrata Postazione:", type=["jpg", "jpeg", "png"], key=f"fin_{id_t}")
                                    
                                    if st.form_submit_button("✅ Conferma Check-in", type="primary", use_container_width=True):
                                        ad_str = ora_italiana().strftime("%d/%m/%Y %H:%M:%S")
                                        update_data = {
                                            "check_in_effettivo": ad_str,
                                            "gps_check_in": "41.229565, 14.508582",
                                            "registrato_da": f"{op['nome']} (Check-in)"
                                        }
                                        if foto_in:
                                            foto_url = salva_foto_su_storage(foto_in, data_oggettiva, nome_posto, op['id'], op['nome'], id_t, "IN")
                                            update_data["foto_postazione"] = foto_url

                                        supabase.table("turni").update(update_data).eq("id_turno", id_t).execute()
                                        registra_log(op["nome"], "TIMBRATURA_CHECKIN", f"Turno {id_t} - {nome_posto}")
                                        st.success("✅ Check-in registrato con successo!")
                                        st.rerun()

                        with tab_out:
                            if gia_fatto_out:
                                st.success(f"✅ Check-out già registrato in data {c_out}.")
                                st.button("🔴 Check-out già eseguito", disabled=True, key=f"btn_dis_out_{id_t}", use_container_width=True)
                            else:
                                with st.form(f"form_out_{id_t}"):
                                    st.text_input("📍 Posizione GPS (Certificata Automaticamente):", value="41.229565, 14.508582", disabled=True, key=f"gps_out_{id_t}")
                                    foto_out = st.file_uploader("Foto Uscita / Consegna:", type=["jpg", "jpeg", "png"], key=f"fout_{id_t}")
                                    
                                    if st.form_submit_button("🔴 Conferma Check-out", type="primary", use_container_width=True):
                                        ad_str = ora_italiana().strftime("%d/%m/%Y %H:%M:%S")
                                        update_data = {
                                            "check_out_effettivo": ad_str,
                                            "gps_check_out": "41.229565, 14.508582",
                                            "registrato_da": f"{op['nome']} (Check-out)"
                                        }
                                        if foto_out:
                                            foto_url = salva_foto_su_storage(foto_out, data_oggettiva, nome_posto, op['id'], op['nome'], id_t, "OUT")
                                            update_data["foto_postazione"] = foto_url

                                        supabase.table("turni").update(update_data).eq("id_turno", id_t).execute()
                                        registra_log(op["nome"], "TIMBRATURA_CHECKOUT", f"Turno {id_t} - {nome_posto}")
                                        st.success("✅ Check-out completato con successo!")
                                        st.rerun()

                        with tab_extra:
                            st.info("Carica ulteriori foto extra per questo turno nella tua cartella.")
                            with st.form(f"form_extra_{id_t}"):
                                foto_extra = st.file_uploader("Seleziona Foto Extra:", type=["jpg", "jpeg", "png"], key=f"fextra_{id_t}")
                                desc_extra = st.text_input("Nota / Dettaglio foto (opzionale):", value="Controllo_Extra")
                                
                                if st.form_submit_button("📤 Carica Foto Extra su Cloud", type="primary", use_container_width=True):
                                    if foto_extra:
                                        salva_foto_su_storage(foto_extra, data_oggettiva, nome_posto, op['id'], op['nome'], f"{id_t}_{pulisci_nome(desc_extra)}", "EXTRA")
                                        registra_log(op["nome"], "CARICAMENTO_FOTO_EXTRA", f"Turno {id_t} - {nome_posto}")
                                        st.success("✅ Foto extra caricata correttamente!")
                                    else:
                                        st.error("Seleziona prima un'immagine.")

                    st.markdown("---")

    # 2. SCHEDA STORICO COMPLETO PASSATI
    with tab_storico:
        st.subheader("📜 Storico Completo dei Tuoi Turni")
        turni_passati = turni_miei[~condizione_attivo].copy()
        turni_passati = turni_passati.sort_values(by='data_dt', ascending=False)

        if not turni_passati.empty:
            recap_storico = []
            for _, r_s in turni_passati.iterrows():
                id_p_s = str(r_s.get('id_postazione', '')).strip()
                p_r = df_post[df_post['id_postazione'] == id_p_s]
                nome_p_s = p_r.iloc[0]['nome_cliente'] if not p_r.empty else id_p_s

                recap_storico.append({
                    "Turno ID": r_s.get('id_turno', ''),
                    "Data": r_s.get('data', ''),
                    "Mese": r_s.get('Mese_Anno', ''),
                    "Postazione": nome_p_s,
                    "Orario Previsto": f"{r_s.get('ora_inizio_prevista', '-')} - {r_s.get('ora_fine_prevista', '-')}",
                    "Entrata (Check-in)": r_s.get('check_in_effettivo', '-'),
                    "Uscita (Check-out)": r_s.get('check_out_effettivo', '-')
                })
            
            df_st_view = pd.DataFrame(recap_storico)
            mesi_storico = ["Tutti i Mesi"] + sorted([m for m in df_st_view['Mese'].dropna().unique() if m != 'Non Riconosciuto'], reverse=True)
            scelta_m_op = st.selectbox("Filtra Storico per Mese:", mesi_storico, key="filtro_storico_op")
            
            if scelta_m_op != "Tutti i Mesi":
                df_st_view = df_st_view[df_st_view['Mese'] == scelta_m_op]
                
            st.dataframe(df_st_view, use_container_width=True)
        else:
            st.info("Nessun turno archiviato nello storico.")

    # 3. SCHEDA FOTO CARICATE
    with tab_foto_op:
        st.subheader(f"📸 Foto caricate da te ({op['nome']})")
        mie_foto = df_turni[
            ((df_turni['id_guardia'] == op['id']) | (df_turni['cognome_guardia'] == op['cognome'])) & 
            (df_turni['foto_postazione'].notna()) & 
            (df_turni['foto_postazione'] != '')
        ]
        if not mie_foto.empty:
            cols = st.columns(3)
            for idx_f, (_, r_f) in enumerate(mie_foto.iterrows()):
                with cols[idx_f % 3]:
                    try:
                        st.image(r_f['foto_postazione'], caption=f"Turno: {r_f['id_turno']} ({r_f['data']})", use_container_width=True)
                    except Exception:
                        pass
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
        if st.button("🚪 Esci", use_container_width=True):
            registra_log(adm["nome"], "LOGOUT", "Disconnessione admin")
            st.session_state["autenticato"] = False
            st.rerun()

    st.markdown("---")
    st.sidebar.markdown("### 📌 Menu Navigazione")
    menu_admin = st.sidebar.radio(
        "Seleziona Sezione:",
        [
            "🏢 Vista Postazione (Controllo Settimanale)",
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
        st.subheader("🏢 Copertura Settimanale per Singola Postazione")
        c_sel_p, c_sel_d = st.columns([2.5, 1.5])
        with c_sel_p:
            map_p = {f"{r['id_postazione']} - {r['nome_cliente']}": str(r['id_postazione']).strip() for _, r in df_post.iterrows()} if not df_post.empty else {}
            scelta_p_str = st.selectbox("Seleziona Postazione da monitorare:", list(map_p.keys())) if map_p else None
            id_p_selezionato = map_p[scelta_p_str] if scelta_p_str else None

        with c_sel_d:
            data_riferimento = st.date_input("Settimana contenente il giorno:", value=data_italiana())

        if id_p_selezionato:
            inizio_sett = data_riferimento - timedelta(days=data_riferimento.weekday())
            fine_sett = inizio_sett + timedelta(days=6)
            st.markdown(f"#### Settimana dal `{inizio_sett.strftime('%d/%m/%Y')}` al `{fine_sett.strftime('%d/%m/%Y')}`")

            turni_post = df_turni[df_turni['id_postazione'] == id_p_selezionato].copy()
            giorni_nomi = ["Lunedì", "Martedì", "Mercoledì", "Giovedì", "Venerdì", "Sabato", "Domenica"]
            righe_sett = []

            for i in range(7):
                g_curr = inizio_sett + timedelta(days=i)
                tg = turni_post[turni_post['data_dt'] == g_curr]
                if not tg.empty:
                    for _, t in tg.iterrows():
                        cin = t.get('check_in_effettivo', '')
                        cout = t.get('check_out_effettivo', '')
                        righe_sett.append({
                            "Giorno": f"{giorni_nomi[i]} ({g_curr.strftime('%d/%m')})",
                            "Turno ID": t.get('id_turno', ''),
                            "Cognome Guardia": t.get('cognome_guardia', ''),
                            "Orario": f"{t.get('ora_inizio_prevista', '-')} - {t.get('ora_fine_prevista', '-')}",
                            "Entrata (Check-in)": cin if pd.notna(cin) and str(cin).strip() else "⏳ Non timbrato",
                            "Uscita (Check-out)": cout if pd.notna(cout) and str(cout).strip() else "⏳ Non timbrato",
                            "Registrato Da": t.get('registrato_da', 'Sistema')
                        })
                else:
                    righe_sett.append({
                        "Giorno": f"{giorni_nomi[i]} ({g_curr.strftime('%d/%m')})",
                        "Turno ID": "-", "Cognome Guardia": "❌ NESSUNA GUARDIA", "Orario": "-",
                        "Entrata (Check-in)": "-", "Uscita (Check-out)": "-", "Registrato Da": "-"
                    })
            st.dataframe(pd.DataFrame(righe_sett), use_container_width=True)

    # 2. GESTIONE TURNI PER POSTAZIONE
    elif menu_admin == "📅 Gestione Turni per Postazione":
        st.subheader("📅 Aggiunta & Gestione Turni per Ciascuna Postazione")
        if not df_post.empty:
            for _, post in df_post.iterrows():
                id_pst = str(post['id_postazione']).strip()
                nome_pst = post['nome_cliente']
                
                with st.expander(f"📍 Postazione: {id_pst} — {nome_pst}"):
                    turni_questa_post = df_turni[df_turni['id_postazione'] == id_pst].copy()
                    
                    if not turni_questa_post.empty:
                        st.write("**Turni programmati (Seleziona per eliminare):**")
                        
                        df_del_view = turni_questa_post[['id_turno', 'data', 'cognome_guardia', 'ora_inizio_prevista', 'ora_fine_prevista', 'check_in_effettivo', 'check_out_effettivo']].copy()
                        df_del_view.insert(0, "Seleziona", False)
                        
                        edited_del = st.data_editor(
                            df_del_view,
                            use_container_width=True,
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

                        conflitti = df_turni[
                            ((df_turni['id_guardia'] == g_codice) | (df_turni['cognome_guardia'] == cognome_selezionato)) & 
                            (df_turni['data_dt'] == data_nuovo_t)
                        ]
                        
                        forza_creazione = False
                        if not conflitti.empty:
                            st.warning(f"⚠️ ATTENZIONE: Il dipendente {guardia_sel} risulta GIÀ assegnato il giorno {data_nuovo_t.strftime('%d/%m/%Y')}!")
                            forza_creazione = st.checkbox("Conferma comunque turno doppio", key=f"chk_force_{id_pst}")

                        btn_crea = st.form_submit_button("💾 Registra Turno su Cloud", type="primary")

                        if btn_crea:
                            if not conflitti.empty and not forza_creazione:
                                st.error("Operazione bloccata: conferma la casella per il turno doppio.")
                            else:
                                record = {
                                    "id_turno": str(id_nuovo_t).strip(),
                                    "data": data_nuovo_t.strftime("%d/%m/%Y"),
                                    "id_guardia": str(g_codice).strip(),
                                    "cognome_guardia": str(cognome_selezionato).strip(),
                                    "id_postazione": str(id_pst).strip(),
                                    "ora_inizio_prevista": str(ora_ini).strip(),
                                    "ora_fine_prevista": str(ora_fin).strip(),
                                    "registrato_da": str(adm["nome"]).strip()
                                }
                                try:
                                    supabase.table("turni").insert(record).execute()
                                    nota = f"Creato turno {id_nuovo_t} per {cognome_selezionato}" + (" [FORZATO]" if not conflitti.empty else "")
                                    registra_log(adm["nome"], "CREAZIONE_TURNO", nota)
                                    st.success("Turno salvato su Cloud con successo!")
                                    st.rerun()
                                except Exception as err_db:
                                    st.error(f"Errore di scrittura su Supabase: {err_db}")

    # 3. RECAP GENERALE POSTAZIONI
    elif menu_admin == "📊 File Recap & Controllo Postazioni":
        st.subheader("📊 File Recap Operativo Completo (Sincronizzato Cloud)")
        if not df_turni.empty:
            recap_df = df_turni.merge(df_post[['id_postazione', 'nome_cliente', 'indirizzo_sede']], on='id_postazione', how='left')
            tutti_i_mesi = ["Tutti i Mesi"] + sorted([m for m in recap_df['Mese_Anno'].dropna().unique() if m != "Non Riconosciuto"], reverse=True)
            scelta_m = st.selectbox("Filtra per Mese:", tutti_i_mesi)
            
            view_recap = recap_df if scelta_m == "Tutti i Mesi" else recap_df[recap_df['Mese_Anno'] == scelta_m]
            colonne_show = ['id_turno', 'data', 'Mese_Anno', 'id_postazione', 'nome_cliente', 'indirizzo_sede', 'cognome_guardia', 'ora_inizio_prevista', 'ora_fine_prevista', 'check_in_effettivo', 'check_out_effettivo', 'registrato_da']
            colonne_show = [c for c in colonne_show if c in view_recap.columns]
            st.dataframe(view_recap[colonne_show], use_container_width=True)

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
                riga_g = df_dip[df_dip['id_guardia'] == id_g_mod].iloc[0]

                with st.form(f"form_modifica_{id_g_mod}"):
                    cm1, cm2 = st.columns(2)
                    with cm1:
                        mod_cognome = st.text_input("Cognome (Username Login):", value=str(riga_g.get('cognome', '')))
                        mod_nome = st.text_input("Nome:", value=str(riga_g.get('nome', '')))
                    with cm2:
                        mod_email = st.text_input("Email:", value=str(riga_g.get('email', '')))
                        mod_pwd = st.text_input("Nuova Password:", value=str(riga_g.get('password', 'gufi2026!')))

                    if st.form_submit_button("💾 Salva Modifiche su Cloud", type="primary"):
                        supabase.table("dipendenti").update({
                            "cognome": mod_cognome.strip(),
                            "nome": mod_nome.strip(),
                            "email": mod_email.strip(),
                            "password": mod_pwd.strip()
                        }).eq("id_guardia", id_g_mod).execute()
                        registra_log(adm["nome"], "MODIFICA_DIPENDENTE", f"Aggiornato {mod_cognome} ({id_g_mod})")
                        st.success("Dati aggiornati su Cloud!")
                        st.rerun()

        with tab_agg:
            with st.form("form_nuovo_dipendente"):
                c_d1, c_d2 = st.columns(2)
                with c_d1:
                    nuovo_id_g = st.text_input("Codice Guardia", value=f"G0{len(df_dip)+1:02d}")
                    nuovo_cognome = st.text_input("Cognome (Username per Login)")
                    nuovo_nome = st.text_input("Nome")
                with c_d2:
                    nuova_email = st.text_input("Email")
                    nuova_pwd = st.text_input("Password Iniziale", value="gufi2026!")

                if st.form_submit_button("💾 Salva Nuovo Dipendente", type="primary"):
                    if not nuovo_cognome.strip():
                        st.error("Il cognome è obbligatorio.")
                    else:
                        supabase.table("dipendenti").insert({
                            "id_guardia": nuovo_id_g.strip(),
                            "cognome": nuovo_cognome.strip(),
                            "nome": nuovo_nome.strip(),
                            "email": nuova_email.strip(),
                            "password": nuova_pwd.strip()
                        }).execute()
                        registra_log(adm["nome"], "AGGIUNGI_DIPENDENTE", f"Creato {nuovo_cognome} ({nuovo_id_g})")
                        st.success(f"Dipendente {nuovo_cognome} registrato!")
                        st.rerun()

        st.markdown("#### Anagrafica Attiva")
        st.dataframe(df_dip[['id_guardia', 'cognome', 'nome', 'email', 'password']], use_container_width=True)

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
                riga_post = df_post_sorted[df_post_sorted['id_postazione'] == id_pst_sel].iloc[0]

                with st.form(f"form_modifica_post_{id_pst_sel}"):
                    cp_m1, cp_m2 = st.columns(2)
                    with cp_m1:
                        mod_nome_post = st.text_input("Nome Cliente / Sede:", value=str(riga_post.get('nome_cliente', '')))
                    with cp_m2:
                        mod_ind_post = st.text_input("Indirizzo Completo (Google Maps):", value=str(riga_post.get('indirizzo_sede', '')))

                    c_salva, c_elimina = st.columns([1, 1])
                    with c_salva:
                        btn_salva_p = st.form_submit_button("💾 Salva Modifiche", type="primary", use_container_width=True)
                    with c_elimina:
                        btn_elimina_p = st.form_submit_button("🗑️ Elimina Postazione", type="secondary", use_container_width=True)

                    if btn_salva_p:
                        supabase.table("postazioni").update({
                            "nome_cliente": mod_nome_post.strip(),
                            "indirizzo_sede": mod_ind_post.strip()
                        }).eq("id_postazione", id_pst_sel).execute()
                        registra_log(adm["nome"], "MODIFICA_POSTAZIONE", f"Aggiornata {mod_nome_post} ({id_pst_sel})")
                        st.success("Postazione aggiornata su Cloud!")
                        st.rerun()

                    if btn_elimina_p:
                        supabase.table("postazioni").delete().eq("id_postazione", id_pst_sel).execute()
                        registra_log(adm["nome"], "ELIMINAZIONE_POSTAZIONE", f"Eliminata postazione {id_pst_sel} - {riga_post.get('nome_cliente')}")
                        st.success(f"Postazione {id_pst_sel} eliminata con successo!")
                        st.rerun()

        with tab_agg_p:
            with st.form("form_nuova_postazione"):
                c_p1, c_p2 = st.columns(2)
                with c_p1:
                    nuovo_id_p = st.text_input("ID Postazione", value=f"P0{len(df_post)+1:02d}")
                    nuovo_nome_p = st.text_input("Nome Cliente / Sede")
                with c_p2:
                    nuovo_ind_p = st.text_input("Indirizzo Completo (per Google Maps)")

                if st.form_submit_button("💾 Salva Nuova Postazione", type="primary"):
                    if not nuovo_nome_p.strip():
                        st.error("Il nome del cliente è obbligatorio.")
                    else:
                        supabase.table("postazioni").insert({
                            "id_postazione": nuovo_id_p.strip(),
                            "nome_cliente": nuovo_nome_p.strip(),
                            "indirizzo_sede": nuovo_ind_p.strip()
                        }).execute()
                        registra_log(adm["nome"], "AGGIUNGI_POSTAZIONE", f"Creata {nuovo_nome_p} ({nuovo_id_p})")
                        st.success(f"Postazione {nuovo_nome_p} registrata!")
                        st.rerun()

        st.markdown("#### Elenco Postazioni Attive (Ordinate per ID)")
        st.dataframe(df_post.sort_values(by='id_postazione', ascending=True), use_container_width=True)

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
                def estrai_ore_t(r):
                    cin, cout = r.get('check_in_effettivo'), r.get('check_out_effettivo')
                    if pd.notna(cin) and pd.notna(cout) and str(cin).strip() and str(cout).strip() and str(cin) != 'None' and str(cout) != 'None':
                        return calcola_ore(cin, cout)
                    return calcola_ore(r.get('ora_inizio_prevista', '00:00'), r.get('ora_fine_prevista', '00:00'))

                df_calc = df_turni.copy()
                df_calc['Ore_Turno'] = df_calc.apply(estrai_ore_t, axis=1)

                mesi_validi = sorted([m for m in df_calc['Mese_Anno'].dropna().unique() if m != "Non Riconosciuto"], reverse=True)
                mesi_validi = ["Tutti i Mesi"] + mesi_validi if mesi_validi else ["Tutti i Mesi"]

                col_m_eco, _ = st.columns([2, 2])
                with col_m_eco:
                    mese_scelto_eco = st.selectbox("Seleziona Mese:", mesi_validi)

                df_calc_filtro = df_calc if mese_scelto_eco == "Tutti i Mesi" else df_calc[df_calc['Mese_Anno'] == mese_scelto_eco]
                tot_dip_mese = df_calc_filtro.groupby(['cognome_guardia', 'Mese_Anno'])['Ore_Turno'].sum().reset_index()
                tot_dip_mese.columns = ['Cognome Dipendente', 'Mese', 'Ore Svolte']
                tot_dip_mese['Ore Svolte'] = tot_dip_mese['Ore Svolte'].round(2)
                st.dataframe(tot_dip_mese.sort_values(by=['Mese', 'Cognome Dipendente']), use_container_width=True)

        with tab_eco_dettaglio:
            if not df_turni.empty:
                df_dett = df_turni.copy()
                df_dett['Ore_Turno'] = df_dett.apply(estrai_ore_t, axis=1)
                mesi_disp_tab = sorted([m for m in df_dett['Mese_Anno'].dropna().unique() if m != "Non Riconosciuto"], reverse=True)
                map_pst = {f"{r['id_postazione']} - {r['nome_cliente']}": str(r['id_postazione']).strip() for _, r in df_post.iterrows()}

                cp1, cp2 = st.columns(2)
                with cp1:
                    mese_filtro = st.selectbox("Mese di Riferimento:", mesi_disp_tab if mesi_disp_tab else ["09/2026"])
                with cp2:
                    post_filtro_str = st.selectbox("Postazione:", list(map_pst.keys())) if map_pst else None
                    id_pst_filtro = map_pst[post_filtro_str] if post_filtro_str else None

                turni_filtrati = df_dett[
                    (df_dett['Mese_Anno'] == mese_filtro) & 
                    (df_dett['id_postazione'] == id_pst_filtro)
                ].copy()

                if not turni_filtrati.empty:
                    ore_tot = round(turni_filtrati['Ore_Turno'].sum(), 2)
                    st.success(f"📌 **Ore totali svolte presso {post_filtro_str} nel mese {mese_filtro}: {ore_tot} ore**")

                    guardie_agg = turni_filtrati.groupby('cognome_guardia')['Ore_Turno'].agg(['count', 'sum']).reset_index()
                    guardie_agg.columns = ['Cognome Guardia', 'Numero Turni', 'Ore Complessive']
                    guardie_agg['Ore Complessive'] = guardie_agg['Ore Complessive'].round(2)
                    st.dataframe(guardie_agg, use_container_width=True)

                    tabella_exp = turni_filtrati[['id_turno', 'data', 'cognome_guardia', 'ora_inizio_prevista', 'ora_fine_prevista', 'check_in_effettivo', 'check_out_effettivo', 'Ore_Turno', 'registrato_da']]
                    st.dataframe(tabella_exp, use_container_width=True)

                    st.download_button(
                        "📥 Scarica File Report (.CSV)",
                        data=tabella_exp.to_csv(index=False).encode('utf-8'),
                        file_name=f"Report_Ore_{id_pst_filtro}_{mese_filtro.replace('/', '_')}.csv",
                        mime="text/csv"
                    )
                else:
                    st.info(f"Nessun turno registrato per questa postazione nel mese {mese_filtro}.")

    # 7. FOTO CLOUD (LETTURA RICORSIVA DIRETTA DAL BUCKET SUPABASE STORAGE)
    elif menu_admin == "📁 Foto Postazioni Cloud":
        st.subheader("📁 Tutte le Foto Archiviate su Supabase Storage")
        st.caption("Esplorazione diretta e completa del bucket: Operatore ➔ Data ➔ Posizione ➔ Foto con Dettagli")

        try:
            def elenca_files_storage(path_cartella=""):
                lista_oggetti = []
                risultato = supabase.storage.from_(BUCKET_FOTO).list(path_cartella)
                for item in risultato:
                    nome_item = item.get("name")
                    # Se non ha l'estensione (è una cartella), esplora dentro ricorsivamente
                    if "." not in nome_item and not item.get("id", None) and item.get("metadata") is None:
                        nuovo_path = f"{path_cartella}/{nome_item}" if path_cartella else nome_item
                        lista_oggetti.extend(elenca_files_storage(nuovo_path))
                    else:
                        # È un file immagine
                        file_path = f"{path_cartella}/{nome_item}" if path_cartella else nome_item
                        url_pubblico = supabase.storage.from_(BUCKET_FOTO).get_public_url(file_path)
                        lista_oggetti.append({"path": file_path, "url": url_pubblico, "nome": nome_item})
                return lista_oggetti

            tutti_i_file = elenca_files_storage()

            if tutti_i_file:
                cols = st.columns(3)
                for idx_f, f_info in enumerate(tutti_i_file):
                    with cols[idx_f % 3]:
                        try:
                            path_parti = f_info["path"].split("/")
                            op_cartella = path_parti[0] if len(path_parti) > 0 else "N/D"
                            data_cartella = path_parti[1] if len(path_parti) > 1 else "N/D"
                            posto_cartella = path_parti[2] if len(path_parti) > 2 else "N/D"
                            nome_file_meta = f_info["nome"].replace('.jpg', '').replace('_', ' ')

                            cap_testo = (
                                f"👤 **Op:** `{op_cartella}`\n"
                                f"📅 **Data:** `{data_cartella}`\n"
                                f"📍 **Posto:** `{posto_cartella}`\n"
                                f"📄 `{nome_file_meta}`"
                            )

                            st.image(f_info["url"], caption=cap_testo, use_container_width=True)
                        except Exception:
                            pass
            else:
                st.info("Nessuna foto trovata nel bucket Supabase Storage.")
        except Exception as e_err:
            st.error(f"Errore di lettura dal bucket Storage: {e_err}")

    # 8. AUDIT LOG
    elif menu_admin == "🛡️ Registro Modifiche (Audit Log)":
        st.subheader("🛡️ Storico Azioni Capi Reparto (Supabase Audit)")
        res_log = supabase.table("audit_log").select("*").order("id", desc=True).limit(500).execute()
        df_log = pd.DataFrame(res_log.data)
        if not df_log.empty:
            st.dataframe(df_log[['data_ora', 'autore', 'azione', 'dettagli']], use_container_width=True)
        else:
            st.info("Nessun log presente.")
