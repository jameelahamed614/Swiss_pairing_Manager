import streamlit as st
import pandas as pd
import networkx as nx
import streamlit.components.v1 as components
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
        st.error("⚠️ Database connection failed. Check your Streamlit Secrets.")
        st.stop()

supabase = init_supabase()

def load_data_from_cloud():
    tournaments = {}
    
    res_t = supabase.table("tournaments").select("*").execute()
    for row in res_t.data:
        t = Tournament(row['name'])
        t.tiebreaks = row['tiebreaks'].split(',') if row['tiebreaks'] else []
        t.is_finished = row['is_finished']
        tournaments[t.name] = t

    res_p = supabase.table("players").select("*").execute()
    for row in res_p.data:
        t_name = row['tournament']
        if t_name in tournaments:
            p = Player(row['id'], row['name'], row['rating'])
            p.is_active = bool(row['is_active'])
            tournaments[t_name].players[p.id] = p
            if p.id >= tournaments[t_name].next_player_id:
                tournaments[t_name].next_player_id = p.id + 1

    res_m = supabase.table("matches").select("*").execute()
    for row in res_m.data:
        t_name = row['tournament']
        if t_name in tournaments:
            tournaments[t_name].match_history.append({
                'round': row['round'],
                'board': 'BYE' if row['board'] == 'BYE' else int(row['board']),
                'white_id': row['white_id'],
                'black_id': None if row['black_id'] is None else int(row['black_id']),
                'result': row['result']
            })
            if row['round'] >= tournaments[t_name].current_round:
                tournaments[t_name].current_round = row['round']
            
    for t in tournaments.values():
        recalculate_standings(t)
        current_matches = [m for m in t.match_history if m['round'] == t.current_round]
        if current_matches and all(m['result'] != 'Pending' for m in current_matches):
            t.current_round += 1

    return tournaments

def sync_to_cloud(t):
    tb_str = ",".join(t.tiebreaks)
    supabase.table("tournaments").upsert({"name": t.name, "tiebreaks": tb_str, "is_finished": t.is_finished}).execute()
    
    p_data = [{"tournament": t.name, "id": p.id, "name": p.name, "rating": p.rating, "is_active": p.is_active} for p in t.players.values()]
    m_data = [{"tournament": t.name, "round": m['round'], "board": str(m['board']), "white_id": m['white_id'], "black_id": m['black_id'], "result": m['result']} for m in t.match_history]
    
    if p_data: supabase.table("players").upsert(p_data).execute()
    if m_data: supabase.table("matches").upsert(m_data).execute()

def delete_tournament_from_cloud(t_name):
    supabase.table("matches").delete().eq("tournament", t_name).execute()
    supabase.table("players").delete().eq("tournament", t_name).execute()
    supabase.table("tournaments").delete().eq("name", t_name).execute()

def delete_round_pairings(t_name, round_num, t):
    supabase.table("matches").delete().eq("tournament", t_name).eq("round", round_num).execute()
    t.match_history = [m for m in t.match_history if m['round'] != round_num]
    recalculate_standings(t)

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
        self.tb_mwb = self.tb_h2h = self.tb_buchholz = self.tb_buchholz_cut = self.tb_wins = 0

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
        self.tiebreaks = []
        self.is_finished = False

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
        p.score = p.tb_mwb = p.tb_h2h = p.tb_buchholz = p.tb_buchholz_cut = p.tb_wins = 0
        p.opponents = set()
        p.color_history = []
        p.has_had_bye = False

    for match in t.match_history:
        if match['result'] == 'Pending': continue
        w_player = t.players[match['white_id']]
        if match['board'] == 'BYE':
            w_player.has_had_bye = True
            w_player.score += 1.0
            w_player.tb_wins += 1 
        else:
            b_player = t.players[match['black_id']]
            w_player.opponents.add(b_player.id)
            b_player.opponents.add(w_player.id)
            w_player.color_history.append('W')
            b_player.color_history.append('B')
            if match['result'] == '1-0': 
                w_player.score += 1.0
                w_player.tb_wins += 1
            elif match['result'] == '0-1': 
                b_player.score += 1.0
                b_player.tb_wins += 1
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
            if tb == "Buchholz Cut 1": key.append(p.tb_buchholz_cut)
            elif tb == "Buchholz Total": key.append(p.tb_buchholz)
            elif tb == "Greater Number of Wins with Black": key.append(p.tb_mwb)
            elif tb == "Direct Encounter (Head-to-Head)": key.append(p.tb_h2h)
            elif tb == "Greater Number of Wins": key.append(p.tb_wins)
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
                bye_weight = 100000000 - (p.score * 100000) - p.rating
                G.add_edge(p.id, DUMMY_BYE_ID, weight=bye_weight)

    for i in range(len(active_players)):
        for j in range(i + 1, len(active_players)):
            p1, p2 = active_players[i], active_players[j]
            weight = 10000000 - (abs(p1.score - p2.score) * 100000) - abs(p1.rating - p2.rating)
            if p2.id in p1.opponents:
                weight -= 500000000  
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
# 3. STREAMLIT UI
# ==========================================
st.set_page_config(page_title="Swiss Pairing Manager", layout="wide")

st.session_state.setdefault("is_arbiter", False)

if "tournaments" not in st.session_state:
    with st.spinner("Fetching live data from Supabase..."):
        st.session_state.tournaments = load_data_from_cloud()
        
if "active_t_name" not in st.session_state:
    t_list = list(st.session_state.tournaments.keys())
    st.session_state.active_t_name = t_list[0] if t_list else None

tb_opts = [
    "None",
    "Buchholz Cut 1", 
    "Buchholz Total", 
    "Greater Number of Wins with Black", 
    "Direct Encounter (Head-to-Head)", 
    "Greater Number of Wins"
]

# --- SIDEBAR ---
with st.sidebar:
    st.header("🏆 Live Tournaments")
    if st.button("🔄 Refresh Data"):
        st.session_state.tournaments = load_data_from_cloud()
        st.rerun()

    t_names = list(st.session_state.tournaments.keys())
    if t_names:
        idx = t_names.index(st.session_state.active_t_name) if st.session_state.active_t_name in t_names else 0
        selected_t = st.selectbox("Select Tournament", t_names, index=idx)
        st.session_state.active_t_name = selected_t
    else:
        st.info("No tournaments available. Please create one below.")
        st.session_state.active_t_name = None

    st.divider()

    st.header("➕ Create Tournament")
    new_t_name = st.text_input("Tournament Name")
    
    st.write("**Select Tiebreak Priority:**")
    tb1 = st.selectbox("Tiebreak 1", tb_opts, index=1)
    tb2 = st.selectbox("Tiebreak 2", tb_opts, index=2)
    tb3 = st.selectbox("Tiebreak 3", tb_opts, index=3)
    tb4 = st.selectbox("Tiebreak 4", tb_opts, index=0)
    
    raw_tbs = [tb1, tb2, tb3, tb4]
    sel_tbs = [tb for tb in raw_tbs if tb != "None" and tb not in locals().get('sel_tbs', [])]
    
    final_tbs = []
    for x in sel_tbs:
        if x not in final_tbs: final_tbs.append(x)
    
    if st.button("Create Tournament", use_container_width=True) and new_t_name:
        if new_t_name not in st.session_state.tournaments:
            new_t = Tournament(new_t_name)
            new_t.tiebreaks = final_tbs
            st.session_state.tournaments[new_t_name] = new_t
            sync_to_cloud(new_t)
            st.session_state.active_t_name = new_t_name
            st.rerun()
        else:
            st.error("Name already exists.")
            
    st.divider()

    st.header("🔐 Arbiter System")
    if not st.session_state.is_arbiter:
        arbiter_pin = st.text_input("Enter PIN to manage tournaments", type="password")
        if arbiter_pin == "admin123":
            st.session_state.is_arbiter = True
            st.rerun()
    else:
        st.success("✅ Arbiter Logged In")
        if st.button("Logout", use_container_width=True):
            st.session_state.is_arbiter = False
            st.rerun()

if not st.session_state.active_t_name:
    st.warning("👈 Please create a tournament in the sidebar to get started.")
    st.stop()

t = st.session_state.tournaments[st.session_state.active_t_name]

def draw_standings(t_obj):
    sorted_players = get_sorted_players(t_obj)
    if not sorted_players:
        st.write("No players registered yet.")
        return
        
    df_data = []
    prev_key = None
    current_rank = 1
    
    for i, p in enumerate(sorted_players):
        # Build comparison key (excludes Rating so players with different ratings tie)
        key = [p.score]
        for tb in t_obj.tiebreaks:
            if tb == "Buchholz Cut 1": key.append(p.tb_buchholz_cut)
            elif tb == "Buchholz Total": key.append(p.tb_buchholz)
            elif tb == "Greater Number of Wins with Black": key.append(p.tb_mwb)
            elif tb == "Direct Encounter (Head-to-Head)": key.append(p.tb_h2h)
            elif tb == "Greater Number of Wins": key.append(p.tb_wins)
            
        # Update Rank offset if the key is different from the previous player
        if key != prev_key:
            current_rank = i + 1
            prev_key = key
            
        row = {"Rank": current_rank, "Name": p.name, "Score": p.score}
        for tb in t_obj.tiebreaks:
            if tb == "Buchholz Cut 1": row["BC1"] = p.tb_buchholz_cut
            elif tb == "Buchholz Total": row["BT"] = p.tb_buchholz
            elif tb == "Greater Number of Wins with Black": row["MWB"] = p.tb_mwb
            elif tb == "Direct Encounter (Head-to-Head)": row["H2H"] = p.tb_h2h
            elif tb == "Greater Number of Wins": row["Wins"] = p.tb_wins
            
        row["Status"] = "Active" if p.is_active else "Withdrawn"
        df_data.append(row)
        
    st.dataframe(pd.DataFrame(df_data), hide_index=True, use_container_width=True)

st.title(f"♟️ {t.name}")

# ==========================================
# PUBLIC VIEW (Auto-Refreshing)
# ==========================================
if not st.session_state.is_arbiter:
    components.html(
        "<script>setTimeout(function(){window.parent.location.reload();}, 30000);</script>",
        height=0, width=0
    )
    
    if t.is_finished:
        st.success("🏁 **Tournament Completed**")
    else:
        st.info("ℹ️ **Public View:** Showing live standings and pairings. (Auto-refreshes every 30s)")
        
    tab1, tab2 = st.tabs(["📊 Live Standings", "⚔️ Matches"])
    with tab1: 
        st.header(f"Live Standings ({t.current_round - 1} Rounds Completed)")
        draw_standings(t)
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
tab1, tab2, tab3, tab4 = st.tabs(["📝 Registration", "📊 Standings", "⚔️ Round Manager", "⏪ Settings & Edits"])

with tab1:
    st.header("Player Management")
    if not t.is_finished:
        with st.form("add_player", clear_on_submit=True):
            st.write("**Add Player (Late Entries Allowed)**")
            name = st.text_input("Player Name")
            rating = st.number_input("Elo Rating", min_value=100, max_value=3500, value=1500)
            if st.form_submit_button("Register Player") and name:
                pid = t.next_player_id
                t.players[pid] = Player(pid, name, rating)
                t.next_player_id += 1
                sync_to_cloud(t)
                st.rerun()
                
        st.divider()
        st.write("**Edit or Withdraw Player**")
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
                    supabase.table("players").delete().eq("tournament", t.name).eq("id", sel_p.id).execute()
                    del t.players[sel_p.id]
                    sync_to_cloud(t)
                    st.rerun()
            else:
                if c_del.button("Withdraw Player", type="primary"):
                    sel_p.is_active = not sel_p.is_active
                    sync_to_cloud(t)
                    st.rerun()
    else:
        st.info("Tournament is finished. Registration is locked.")

with tab2:
    st.header(f"Live Standings ({t.current_round - 1} Rounds Completed)")
    draw_standings(t)

with tab3:
    if t.is_finished:
        st.success(f"🏆 {t.name} has concluded! Final Standings are locked.")
    else:
        current_matches = [m for m in t.match_history if m['round'] == t.current_round]
        active_count = len([p for p in t.players.values() if p.is_active])
        
        if active_count < 2: 
            st.warning("Register at least 2 active players to start.")
        elif not current_matches:
            if t.current_round > 1:
                st.subheader(f"✅ Results from Round {t.current_round - 1}")
                prev_matches = [m for m in t.match_history if m['round'] == t.current_round - 1]
                for m in prev_matches:
                    w_p = t.players[m['white_id']]
                    if m['board'] == 'BYE':
                        st.write(f"**BYE:** {w_p.name}")
                    else:
                        b_p = t.players[m['black_id']]
                        st.write(f"**Board {m['board']}:** ⚪ {w_p.name} vs ⚫ {b_p.name} **[{m['result']}]**")
                st.divider()

            c1, c2 = st.columns(2)
            if c1.button(f"🚀 Generate Pairings for Round {t.current_round}", type="primary"):
                with st.spinner("Calculating FIDE pairings & Syncing..."):
                    generate_graph_pairings(t)
                    sync_to_cloud(t)
                st.rerun()
                
            if c2.button("🏁 End Tournament", type="primary"):
                t.is_finished = True
                sync_to_cloud(t)
                st.rerun()
        else:
            with st.form("round_results"):
                st.subheader(f"Round {t.current_round} Matches")
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

                if st.form_submit_button("Submit Results", type="primary"):
                    if any(r == "Pending" for r in results_dict.values()): 
                        st.error("⚠️ Enter a result for every board.")
                    else:
                        for match in current_matches:
                            if match['board'] != 'BYE': match['result'] = results_dict[match['board']]
                        t.current_round += 1
                        with st.spinner("Saving results to Supabase..."):
                            sync_to_cloud(t)
                        st.rerun()
            
            with st.expander("🛠️ Manual Pairings & Color Overrides"):
                st.write("Use this tool to swap colors or change matchups completely before submitting results.")
                with st.form("override_form"):
                    new_override_matches = []
                    active_players = {p.id: p.name for p in t.players.values() if p.is_active}
                    player_opts = list(active_players.keys())
                    
                    for match in current_matches:
                        if match['board'] == 'BYE':
                            w_p = t.players[match['white_id']]
                            st.write(f"**BYE:** {w_p.name}")
                            new_override_matches.append(match)
                            continue
                        
                        col1, col2, col3 = st.columns([1, 4, 4])
                        col1.write(f"**Brd {match['board']}**")
                        
                        w_id = col2.selectbox(
                            "White", 
                            options=player_opts, 
                            format_func=lambda x: active_players[x], 
                            index=player_opts.index(match['white_id']), 
                            key=f"ow_{match['board']}"
                        )
                        b_id = col3.selectbox(
                            "Black", 
                            options=player_opts, 
                            format_func=lambda x: active_players[x], 
                            index=player_opts.index(match['black_id']), 
                            key=f"ob_{match['board']}"
                        )
                        
                        new_override_matches.append({
                            'round': match['round'],
                            'board': match['board'],
                            'white_id': w_id,
                            'black_id': b_id,
                            'result': match['result']
                        })
                    
                    if st.form_submit_button("Save Overrides", type="primary"):
                        t.match_history = [m for m in t.match_history if m['round'] != t.current_round]
                        t.match_history.extend(new_override_matches)
                        sync_to_cloud(t)
                        st.success("Pairings overridden successfully!")
                        st.rerun()

            st.divider()
            st.write("⚠️ **Mistake in pairings?**")
            if st.button("❌ Delete Current Round Pairings"):
                delete_round_pairings(t.name, t.current_round, t)
                st.toast(f"Round {t.current_round} deleted successfully!")
                st.rerun()

with tab4:
    st.header("⏪ Edit Matches")
    rounds = sorted(list(set([m['round'] for m in t.match_history])), reverse=True)
    for r in rounds:
        with st.expander(f"Round {r}"):
            r_matches = [m for m in t.match_history if m['round'] == r]
            
            if r == t.current_round and not t.is_finished:
                st.info("ℹ️ This round is currently active. Please submit results in the **Round Manager** tab.")
                for match in r_matches:
                    w_p = t.players[match['white_id']]
                    if match['board'] == 'BYE': 
                        st.write(f"**BYE:** {w_p.name}")
                    else:
                        b_p = t.players[match['black_id']]
                        st.write(f"Board {match['board']}: ⚪ {w_p.name} vs ⚫ {b_p.name} **[{match['result']}]**")
            else:
                for match in r_matches:
                    w_p = t.players[match['white_id']]
                    if match['board'] == 'BYE': st.write(f"**BYE:** {w_p.name}")
                    else:
                        b_p = t.players[match['black_id']]
                        new_res = st.selectbox(f"Board {match['board']}: ⚪ {w_p.name} vs ⚫ {b_p.name}", ["Pending", "1-0", "0-1", "0.5-0.5"], index=["Pending", "1-0", "0-1", "0.5-0.5"].index(match['result']), key=f"all_edit_R{r}_B{match['board']}")
                        if new_res != match['result']:
                            match['result'] = new_res
                            sync_to_cloud(t)
                            st.toast("Match updated!")
                            st.rerun()

    st.divider()
    st.header("⚙️ Tournament Settings")
    st.subheader("Update Tiebreak Priority")
    
    def get_tb_index(tb_list, idx):
        if idx < len(tb_list) and tb_list[idx] in tb_opts:
            return tb_opts.index(tb_list[idx])
        return 0
        
    upd_tb1 = st.selectbox("Update Tiebreak 1", tb_opts, index=get_tb_index(t.tiebreaks, 0))
    upd_tb2 = st.selectbox("Update Tiebreak 2", tb_opts, index=get_tb_index(t.tiebreaks, 1))
    upd_tb3 = st.selectbox("Update Tiebreak 3", tb_opts, index=get_tb_index(t.tiebreaks, 2))
    upd_tb4 = st.selectbox("Update Tiebreak 4", tb_opts, index=get_tb_index(t.tiebreaks, 3))
    
    if st.button("Save Tiebreaks"):
        new_raw = [upd_tb1, upd_tb2, upd_tb3, upd_tb4]
        t.tiebreaks = []
        for x in new_raw:
            if x != "None" and x not in t.tiebreaks:
                t.tiebreaks.append(x)
        sync_to_cloud(t)
        st.success("Tiebreaks updated successfully!")
        st.rerun()
    
    st.divider()
    if t.is_finished:
        if st.button("⏪ Undo End Tournament"):
            t.is_finished = False
            sync_to_cloud(t)
            st.rerun()
            
    st.write("⚠️ **Danger Zone**")
    if st.button("🗑️ Delete Tournament & Clear Database"):
        delete_tournament_from_cloud(t.name)
        del st.session_state.tournaments[t.name]
        st.session_state.active_t_name = None
        st.success("Tournament Deleted.")
        st.rerun()
