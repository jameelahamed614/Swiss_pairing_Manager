import streamlit as st
import pandas as pd
import networkx as nx
from supabase import create_client, Client

# ==========================================
# 1. SUPABASE DATABASE SYNC
# ==========================================
@st.cache_resource
def init_supabase() -> Client:
    try:
        url = st.secrets["SUPABASE_URL"]
        key = st.secrets["SUPABASE_KEY"]
        return create_client(url, key)
    except Exception as e:
        st.error("⚠️ Database connection failed. Ensure SUPABASE_URL and SUPABASE_KEY are in Streamlit Secrets.")
        st.stop()

supabase = init_supabase()

def load_data_from_cloud():
    """Fetches all data from Supabase and rebuilds the Tournament states."""
    tournaments = {}
    
    # 1. Load Players
    res_p = supabase.table("players").select("*").execute()
    for row in res_p.data:
        t_name = row['tournament']
        if t_name not in tournaments: tournaments[t_name] = Tournament(t_name)
        
        p = Player(row['id'], row['name'], row['rating'])
        p.is_active = bool(row['is_active'])
        tournaments[t_name].players[p.id] = p
        if p.id >= tournaments[t_name].next_player_id:
            tournaments[t_name].next_player_id = p.id + 1

    # 2. Load Matches
    res_m = supabase.table("matches").select("*").execute()
    for row in res_m.data:
        t_name = row['tournament']
        if t_name not in tournaments: tournaments[t_name] = Tournament(t_name)
        
        tournaments[t_name].match_history.append({
            'round': row['round'],
            'board': 'BYE' if row['board'] == 'BYE' else int(row['board']),
            'white_id': row['white_id'],
            'black_id': None if row['black_id'] is None else int(row['black_id']),
            'result': row['result']
        })
        if row['round'] >= tournaments[t_name].current_round:
            tournaments[t_name].current_round = row['round']
            
    # Forward the round if previous round is fully resulted
    for t in tournaments.values():
        recalculate_standings(t)
        current_matches = [m for m in t.match_history if m['round'] == t.current_round]
        if current_matches and all(m['result'] != 'Pending' for m in current_matches):
            t.current_round += 1

    return tournaments

def sync_to_cloud(t):
    """Upserts the current tournament data directly to Supabase."""
    p_data = [{"tournament": t.name, "id": p.id, "name": p.name, "rating": p.rating, "is_active": p.is_active} for p in t.players.values()]
    m_data = [{"tournament": t.name, "round": m['round'], "board": str(m['board']), "white_id": m['white_id'], "black_id": m['black_id'], "result": m['result']} for m in t.match_history]
    
    if p_data: supabase.table("players").upsert(p_data).execute()
    if m_data: supabase.table("matches").upsert(m_data).execute()

# ==========================================
# 2. DATA MODELS & FIDE LOGIC
# ==========================================
class Player:
    def __init__(self, player_id: int, name: str, rating: int):
        self.id = player_id
        self.name = name
        self.rating = rating
        self.score = 0.0
        self.opponents = set()
        self.color_history = []
        self.has_had_bye = False
        self.is_active = True  
        self.tb_mwb = self.tb_h2h = self.tb_buchholz = self.tb_buchholz_cut = 0

    def color_preference(self):
        if len(self.color_history) >= 2:
            if self.color_history[-1] == self.color_history[-2]:
                return ('B' if self.color_history[-1] == 'W' else 'W', True)
        w_count = self.color_history.count('W')
        b_count = self.color_history.count('B')
        if w_count > b_count: return ('B', False)
        elif b_count > w_count: return ('W', False)
        if self.color_history: return ('B' if self.color_history[-1] == 'W' else 'W', False)
        return (None, False)

class Tournament:
    def __init__(self, name: str):
        self.name = name
        self.players = {}
        self.next_player_id = 1
        self.current_round = 1
        self.match_history = []
        self.tiebreaks = ["TB3: Buchholz", "TB4: Buchholz Cut-1", "TB2: Head-to-Head", "TB1: Most Wins by Black"]

def assign_colors(p1: Player, p2: Player):
    pref1, abs1 = p1.color_preference()
    pref2, abs2 = p2.color_preference()
    if pref1 and pref2 and pref1 != pref2: return (p1, p2) if pref1 == 'W' else (p2, p1)
    if abs1 and not abs2: return (p1, p2) if pref1 == 'W' else (p2, p1)
    if abs2 and not abs1: return (p2, p1) if pref2 == 'W' else (p1, p2)
    if not p1.color_history: return (p1, p2) if p1.rating >= p2.rating else (p2, p1)
    higher, lower = (p1, p2) if p1.rating >= p2.rating else (p2, p1)
    pref, _ = higher.color_preference()
    return (higher, lower) if pref == 'W' else (lower, higher)

def recalculate_standings(t: Tournament):
    for p in t.players.values():
        p.score = p.tb_mwb = p.tb_h2h = p.tb_buchholz = p.tb_buchholz_cut = 0
        p.opponents = set()
        p.color_history = []
        p.has_had_bye = False

    for match in t.match_history:
        if match['result'] == 'Pending': continue
        w_player = t.players[match['white_id']]
        if match['board'] == 'BYE':
            w_player.has_had_bye = True
            w_player.score += 1.0
        else:
            b_player = t.players[match['black_id']]
            w_player.opponents.add(b_player.id)
            b_player.opponents.add(w_player.id)
            w_player.color_history.append('W')
            b_player.color_history.append('B')
            if match['result'] == '1-0': w_player.score += 1.0
            elif match['result'] == '0-1': 
                b_player.score += 1.0
                b_player.tb_mwb += 1
            elif match['result'] == '0.5-0.5':
                w_player.score += 0.5
                b_player.score += 0.5

    score_groups = {}
    for p in t.players.values():
        score_groups.setdefault(p.score, []).append(p)
        opp_scores = [t.players[oid].score for oid in p.opponents if oid in t.players]
        if opp_scores:
            p.tb_buchholz = sum(opp_scores)
            p.tb_buchholz_cut = p.tb_buchholz - min(opp_scores) if len(opp_scores) > 1 else p.tb_buchholz

    for score, group in score_groups.items():
        if len(group) > 1:
            group_ids = {p.id for p in group}
            for p in group:
                h2h_pts = 0
                for match in t.match_history:
                    if match['result'] in ['Pending', 'BYE'] or match['board'] == 'BYE': continue
                    w, b = match['white_id'], match['black_id']
                    if p.id == w and b in group_ids:
                        if match['result'] == '1-0': h2h_pts += 1
                        elif match['result'] == '0.5-0.5': h2h_pts += 0.5
                    elif p.id == b and w in group_ids:
                        if match['result'] == '0-1': h2h_pts += 1
                        elif match['result'] == '0.5-0.5': h2h_pts += 0.5
                p.tb_h2h = h2h_pts

def get_sorted_players(t: Tournament):
    recalculate_standings(t)
    players_list = list(t.players.values())
    def sort_key(p):
        key = [p.score]
        for tb in t.tiebreaks:
            if "TB1" in tb: key.append(p.tb_mwb)
            elif "TB2" in tb: key.append(p.tb_h2h)
            elif "TB3" in tb: key.append(p.tb_buchholz)
            elif "TB4" in tb: key.append(p.tb_buchholz_cut)
        key.append(p.rating)
        return tuple(key)
    players_list.sort(key=sort_key, reverse=True)
    return players_list

def generate_graph_pairings(t: Tournament):
    active_players = [p for p in t.players.values() if p.is_active]
    G = nx.Graph()
    for p in active_players: G.add_node(p.id)
    DUMMY_BYE_ID = -999
    if len(active_players) % 2 != 0:
        G.add_node(DUMMY_BYE_ID)
        for p in active_players:
            if not p.has_had_bye:
                bye_weight = 10000000 - (p.score * 100000) - p.rating
                G.add_edge(p.id, DUMMY_BYE_ID, weight=bye_weight)

    for i in range(len(active_players)):
        for j in range(i + 1, len(active_players)):
            p1, p2 = active_players[i], active_players[j]
            if p2.id not in p1.opponents:
                weight = 10000000 - (abs(p1.score - p2.score) * 100000) - abs(p1.rating - p2.rating)
                p1_pref, p1_abs = p1.color_preference()
                p2_pref, p2_abs = p2.color_preference()
                if p1_abs and p2_abs and p1_pref == p2_pref: weight -= 50000
                elif p1_pref == p2_pref and p1_pref is not None: weight -= 10000
                G.add_edge(p1.id, p2.id, weight=weight)

    matching = nx.max_weight_matching(G, maxcardinality=True)
    board = 1
    for u, v in matching:
        if u == DUMMY_BYE_ID or v == DUMMY_BYE_ID:
            bye_id = u if v == DUMMY_BYE_ID else v
            t.match_history.append({'round': t.current_round, 'board': 'BYE', 'white_id': bye_id, 'black_id': None, 'result': 'BYE'})
        else:
            p1, p2 = t.players[u], t.players[v]
            white, black = assign_colors(p1, p2)
            t.match_history.append({'round': t.current_round, 'board': board, 'white_id': white.id, 'black_id': black.id, 'result': 'Pending'})
            board += 1

# ==========================================
# 3. STREAMLIT UI (ARBITER vs PUBLIC)
# ==========================================
st.set_page_config(page_title="Swiss Pairing Supabase Manager", layout="wide")

# Fetch data on first load
if "tournaments" not in st.session_state:
    with st.spinner("Fetching live data from Supabase..."):
        st.session_state.tournaments = load_data_from_cloud()
        if not st.session_state.tournaments:
            st.session_state.tournaments = {"Main Tournament": Tournament("Main Tournament")}
        st.session_state.active_t_name = list(st.session_state.tournaments.keys())[0]

# --- SIDEBAR & AUTHENTICATION ---
with st.sidebar:
    st.header("🏆 Live Tournaments")
    if st.button("🔄 Refresh Live Data"):
        st.session_state.tournaments = load_data_from_cloud()
        st.rerun()

    t_names = list(st.session_state.tournaments.keys())
    selected_t = st.selectbox("Select Tournament", t_names, index=t_names.index(st.session_state.active_t_name))
    st.session_state.active_t_name = selected_t
    
    st.divider()
    st.header("🔐 Arbiter Login")
    arbiter_pin = st.text_input("Enter PIN to unlock manager", type="password")
    is_arbiter = (arbiter_pin == "admin123")
    
    if is_arbiter:
        st.success("Arbiter Unlocked")
        new_t_name = st.text_input("New Tournament Name")
        if st.button("Create Tournament", use_container_width=True) and new_t_name:
            if new_t_name not in st.session_state.tournaments:
                new_t = Tournament(new_t_name)
                st.session_state.tournaments[new_t_name] = new_t
                sync_to_cloud(new_t)
                st.session_state.active_t_name = new_t_name
                st.rerun()
            else:
                st.error("Name already exists.")

t = st.session_state.tournaments[st.session_state.active_t_name]
st.title(f"♟️ {t.name}")

# ==========================================
# PUBLIC VIEW
# ==========================================
if not is_arbiter:
    st.info("ℹ️ **Public View:** Showing live standings and pairings. (Refresh page to update)")
    tab1, tab2 = st.tabs(["📊 Live Standings", "⚔️ Matches"])
    
    with tab1:
        sorted_players = get_sorted_players(t)
        if sorted_players:
            df = pd.DataFrame([{"Rank": i+1, "Name": p.name, "Rating": p.rating, "Score": p.score, "TB1": p.tb_mwb, "TB2": p.tb_h2h, "TB3": p.tb_buchholz, "TB4": p.tb_buchholz_cut} for i, p in enumerate(sorted_players)])
            st.dataframe(df, hide_index=True, use_container_width=True)
        else:
            st.write("No players registered yet.")
            
    with tab2:
        rounds = sorted(list(set([m['round'] for m in t.match_history])), reverse=True)
        for r in rounds:
            with st.expander(f"Round {r}", expanded=(r == t.current_round)):
                r_matches = [m for m in t.match_history if m['round'] == r]
                for m in r_matches:
                    w_p = t.players[m['white_id']]
                    if m['board'] == 'BYE': st.write(f"**BYE:** {w_p.name}")
                    else:
                        b_p = t.players[m['black_id']]
                        st.write(f"**Board {m['board']}:** ⚪ {w_p.name} vs ⚫ {b_p.name} **[{m['result']}]**")
    st.stop()

# ==========================================
# ARBITER VIEW
# ==========================================
tab1, tab2, tab3 = st.tabs(["📊 Registration & Standings", "⚔️ Round Manager", "⏪ Edit All Results"])

with tab1:
    col1, col2 = st.columns([1, 2])
    with col1:
        with st.form("add_player", clear_on_submit=True):
            name = st.text_input("Player Name")
            rating = st.number_input("Elo Rating", min_value=100, max_value=3500, value=1500)
            if st.form_submit_button("Add Player") and name:
                pid = t.next_player_id
                t.players[pid] = Player(pid, name, rating)
                t.next_player_id += 1
                sync_to_cloud(t)
                st.rerun()
                
        st.divider()
        player_opts = {f"{p.name} ({p.rating}) - {'Active' if p.is_active else 'Withdrawn'}": p for p in t.players.values()}
        sel_p_name = st.selectbox("Select Player to Edit/Withdraw", ["-- Select --"] + list(player_opts.keys()))
        if sel_p_name != "-- Select --":
            sel_p = player_opts[sel_p_name]
            new_name = st.text_input("Edit Name", value=sel_p.name)
            new_rating = st.number_input("Edit Rating", value=sel_p.rating)
            c_ed, c_del = st.columns(2)
            
            if c_ed.button("Update Profile"):
                sel_p.name, sel_p.rating = new_name, new_rating
                sync_to_cloud(t)
                st.rerun()
                
            if t.current_round == 1:
                if c_del.button("❌ Hard Delete", type="primary"):
                    # Delete explicitly from Supabase so it stops showing up in cloud sync
                    supabase.table("players").delete().eq("tournament", t.name).eq("id", sel_p.id).execute()
                    del t.players[sel_p.id]
                    sync_to_cloud(t)
                    st.rerun()
            else:
                if c_del.button("Withdraw Player", type="primary"):
                    sel_p.is_active = not sel_p.is_active
                    sync_to_cloud(t)
                    st.rerun()
    with col2:
        st.header(f"Live Standings (Round {t.current_round})")
        sorted_players = get_sorted_players(t)
        if sorted_players:
            df = pd.DataFrame([{"Rank": i+1, "Name": p.name, "Score": p.score, "TB1": p.tb_mwb, "TB2": p.tb_h2h, "TB3": p.tb_buchholz, "TB4": p.tb_buchholz_cut, "Status": "Active" if p.is_active else "Withdrawn"} for i, p in enumerate(sorted_players)])
            st.dataframe(df, hide_index=True, use_container_width=True)

with tab2:
    current_matches = [m for m in t.match_history if m['round'] == t.current_round]
    active_count = len([p for p in t.players.values() if p.is_active])
    
    if active_count < 2: st.warning("Register at least 2 active players to start.")
    elif not current_matches:
        if st.button(f"🚀 Generate Pairings for Round {t.current_round}", type="primary"):
            with st.spinner("Calculating FIDE pairings & Syncing..."):
                generate_graph_pairings(t)
                sync_to_cloud(t)
            st.rerun()
    else:
        with st.form("round_results"):
            results_dict = {}
            for match in current_matches:
                w_p = t.players[match['white_id']]
                if match['board'] == 'BYE':
                    st.info(f"🛌 **BYE (1 Point):** {w_p.name}")
                else:
                    b_p = t.players[match['black_id']]
                    c_w, c_res, c_b = st.columns([3, 3, 3])
                    c_w.write(f"⚪ **{w_p.name}** *(Score: {w_p.score})*")
                    res = c_res.selectbox("Result", ["Pending", "1-0", "0-1", "0.5-0.5"], key=f"res_{match['board']}", label_visibility="collapsed")
                    results_dict[match['board']] = res
                    c_b.write(f"⚫ **{b_p.name}** *(Score: {b_p.score})*")
                    st.divider()

            if st.form_submit_button("Submit Results & Next Round", type="primary"):
                if any(r == "Pending" for r in results_dict.values()): st.error("⚠️ Enter a result for every board.")
                else:
                    for match in current_matches:
                        if match['board'] != 'BYE': match['result'] = results_dict[match['board']]
                    t.current_round += 1
                    with st.spinner("Saving results to Supabase..."):
                        sync_to_cloud(t)
                    st.rerun()

with tab3:
    rounds = sorted(list(set([m['round'] for m in t.match_history])), reverse=True)
    for r in rounds:
        with st.expander(f"Round {r}", expanded=(r == t.current_round)):
            r_matches = [m for m in t.match_history if m['round'] == r]
            for match in r_matches:
                w_p = t.players[match['white_id']]
                if match['board'] == 'BYE': st.write(f"**BYE:** {w_p.name}")
                else:
                    b_p = t.players[match['black_id']]
                    new_res = st.selectbox(f"Board {match['board']}: ⚪ {w_p.name} vs ⚫ {b_p.name}", ["Pending", "1-0", "0-1", "0.5-0.5"], index=["Pending", "1-0", "0-1", "0.5-0.5"].index(match['result']), key=f"all_edit_R{r}_B{match['board']}")
                    if new_res != match['result']:
                        match['result'] = new_res
                        sync_to_cloud(t)
                        st.toast("Match updated in Supabase!")
                        st.rerun()
