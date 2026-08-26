import streamlit as st
import pandas as pd
import networkx as nx

# ==========================================
# 1. DATA MODELS & FIDE LOGIC
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
        self.is_active = True  # Used if withdrawn after round 1
        
        # Tiebreaks
        self.tb_mwb = 0       # Most Wins by Black
        self.tb_h2h = 0       # Head to Head
        self.tb_buchholz = 0  # Sum of Opponent Scores
        self.tb_buchholz_cut = 0 # Sum of Opponent Scores (Least removed)

    def color_preference(self):
        if len(self.color_history) >= 2:
            if self.color_history[-1] == self.color_history[-2]:
                return ('B' if self.color_history[-1] == 'W' else 'W', True)
        
        w_count = self.color_history.count('W')
        b_count = self.color_history.count('B')
        
        if w_count > b_count: return ('B', False)
        elif b_count > w_count: return ('W', False)
        
        if self.color_history:
            return ('B' if self.color_history[-1] == 'W' else 'W', False)
        return (None, False)

class Tournament:
    def __init__(self, name: str):
        self.name = name
        self.players = {}
        self.next_player_id = 1
        self.current_round = 1
        self.match_history = []
        self.is_finished = False
        self.tiebreaks = [
            "TB3: Buchholz (Sum of opponent scores)", 
            "TB4: Buchholz Cut-1 (Least opponent removed)",
            "TB2: Head-to-Head",
            "TB1: Most Wins by Black"
        ]

def assign_colors(p1: Player, p2: Player):
    pref1, abs1 = p1.color_preference()
    pref2, abs2 = p2.color_preference()

    if pref1 and pref2 and pref1 != pref2:
        return (p1, p2) if pref1 == 'W' else (p2, p1)
    if abs1 and not abs2:
        return (p1, p2) if pref1 == 'W' else (p2, p1)
    if abs2 and not abs1:
        return (p2, p1) if pref2 == 'W' else (p1, p2)
    
    if not p1.color_history:
        return (p1, p2) if p1.rating >= p2.rating else (p2, p1)
    
    higher, lower = (p1, p2) if p1.rating >= p2.rating else (p2, p1)
    pref, _ = higher.color_preference()
    return (higher, lower) if pref == 'W' else (lower, higher)

# ==========================================
# 2. STATE MANAGEMENT & MATH
# ==========================================
def recalculate_standings(t: Tournament):
    """Calculates scores and all tiebreaks retroactively."""
    # 1. Reset all player stats
    for p in t.players.values():
        p.score = 0.0
        p.opponents = set()
        p.color_history = []
        p.has_had_bye = False
        p.tb_mwb = p.tb_h2h = p.tb_buchholz = p.tb_buchholz_cut = 0

    # 2. Re-apply base scores and colors from history
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
            
            if match['result'] == '1-0':
                w_player.score += 1.0
            elif match['result'] == '0-1':
                b_player.score += 1.0
                b_player.tb_mwb += 1 # Tiebreak 1 Calculation
            elif match['result'] == '0.5-0.5':
                w_player.score += 0.5
                b_player.score += 0.5

    # 3. Calculate Advanced Tiebreaks (Buchholz & Head-to-Head)
    score_groups = {}
    for p in t.players.values():
        score_groups.setdefault(p.score, []).append(p)
        
        # Buchholz Calculations (TB3 & TB4)
        opp_scores = [t.players[oid].score for oid in p.opponents if oid in t.players]
        if opp_scores:
            p.tb_buchholz = sum(opp_scores)
            p.tb_buchholz_cut = p.tb_buchholz - min(opp_scores) if len(opp_scores) > 1 else p.tb_buchholz

    # Head-to-Head Calculation (TB2) - Points against tied players
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
    """Sorts players based on score and selected tiebreaks."""
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
    
    for p in active_players:
        G.add_node(p.id)
        
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
                weight = 10000000 
                weight -= (abs(p1.score - p2.score) * 100000)
                
                p1_pref, p1_abs = p1.color_preference()
                p2_pref, p2_abs = p2.color_preference()
                if p1_abs and p2_abs and p1_pref == p2_pref: weight -= 50000
                elif p1_pref == p2_pref and p1_pref is not None: weight -= 10000
                    
                weight -= abs(p1.rating - p2.rating)
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
# 3. STREAMLIT UI & DASHBOARD
# ==========================================
st.set_page_config(page_title="Swiss Pairing Manager", layout="wide")
st.title("♟️ Swiss Pairing Manager - Jameel Ahamed")

# Init Multi-Tournament Session State
if "tournaments" not in st.session_state:
    st.session_state.tournaments = {"Main Tournament": Tournament("Main Tournament")}
    st.session_state.active_t_name = "Main Tournament"

# --- SIDEBAR: Tournament Selection ---
with st.sidebar:
    st.header("🏆 Tournaments")
    
    t_names = list(st.session_state.tournaments.keys())
    selected_t = st.selectbox("Select Active Tournament", t_names, index=t_names.index(st.session_state.active_t_name))
    st.session_state.active_t_name = selected_t
    
    st.divider()
    new_t_name = st.text_input("New Tournament Name")
    if st.button("Create Tournament", use_container_width=True) and new_t_name:
        if new_t_name not in st.session_state.tournaments:
            st.session_state.tournaments[new_t_name] = Tournament(new_t_name)
            st.session_state.active_t_name = new_t_name
            st.rerun()
        else:
            st.error("Name already exists.")

# Get Active Tournament Object
t = st.session_state.tournaments[st.session_state.active_t_name]

if t.is_finished:
    st.success(f"🏆 {t.name} has concluded!")
    st.header("Final Standings")
    final_players = get_sorted_players(t)
    df = pd.DataFrame([
        {
            "Rank": i+1, "Name": p.name, "Rating": p.rating, "Score": p.score, 
            "TB-1": p.tb_mwb, "TB-2": p.tb_h2h, "TB-3": p.tb_buchholz, "TB-4": p.tb_buchholz_cut
        } for i, p in enumerate(final_players)
    ])
    st.dataframe(df, hide_index=True, use_container_width=True)
    if st.button("Resume Tournament (Undo End)"):
        t.is_finished = False
        st.rerun()
    st.stop() # Hide the rest of the dashboard for finished tournaments

# --- MAIN DASHBOARD TABS ---
tab1, tab2, tab3, tab4 = st.tabs([
    "📊 Registration & Standings", 
    "⚔️ Round Manager", 
    "⏪ Edit All Results",
    "⚙️ Settings & Tiebreaks"
])

# ----------------- TAB 1: REGISTRATION -----------------
with tab1:
    col1, col2 = st.columns([1, 2])
    with col1:
        st.header("Add Player")
        with st.form("add_player", clear_on_submit=True):
            name = st.text_input("Player Name")
            rating = st.number_input("Elo Rating", min_value=100, max_value=3500, value=1500)
            if st.form_submit_button("Add") and name:
                pid = t.next_player_id
                t.players[pid] = Player(pid, name, rating)
                t.next_player_id += 1
                st.success(f"Added {name}")
                st.rerun()
                
        st.divider()
        st.header("Edit / Delete Player")
        player_opts = {f"{p.name} ({p.rating}) - {'Active' if p.is_active else 'Withdrawn'}": p for p in t.players.values()}
        sel_p_name = st.selectbox("Select Player", ["-- Select --"] + list(player_opts.keys()))
        
        if sel_p_name != "-- Select --":
            sel_p = player_opts[sel_p_name]
            new_name = st.text_input("Edit Name", value=sel_p.name)
            new_rating = st.number_input("Edit Rating", value=sel_p.rating, min_value=100, max_value=3500)
            
            c_ed, c_del = st.columns(2)
            if c_ed.button("Update Profile"):
                sel_p.name, sel_p.rating = new_name, new_rating
                st.toast("Profile Updated!")
                st.rerun()
                
            if t.current_round == 1:
                if c_del.button("❌ Hard Delete", type="primary"):
                    del t.players[sel_p.id]
                    st.toast("Player Deleted.")
                    st.rerun()
            else:
                st.info("Tournament started. Players can only be withdrawn.")
                btn_txt = "Re-Activate" if not sel_p.is_active else "Withdraw Player"
                if c_del.button(btn_txt, type="primary"):
                    sel_p.is_active = not sel_p.is_active
                    st.rerun()

    with col2:
        st.header(f"Live Standings (Round {t.current_round})")
        sorted_players = get_sorted_players(t)
        if sorted_players:
            df = pd.DataFrame([
                {
                    "Rank": i+1, "Name": p.name, "Score": p.score, 
                    "TB1": p.tb_mwb, "TB2": p.tb_h2h, "TB3": p.tb_buchholz, "TB4": p.tb_buchholz_cut,
                    "Status": "Active" if p.is_active else "Withdrawn"
                } for i, p in enumerate(sorted_players)
            ])
            st.dataframe(df, hide_index=True, use_container_width=True)

# ----------------- TAB 2: ROUND MANAGER -----------------
with tab2:
    st.header(f"Round {t.current_round} Manager")
    
    current_matches = [m for m in t.match_history if m['round'] == t.current_round]
    active_count = len([p for p in t.players.values() if p.is_active])
    
    if active_count < 2:
        st.warning("Register at least 2 active players to start.")
        
    elif not current_matches:
        if st.button(f"🚀 Generate Pairings for Round {t.current_round}", type="primary", use_container_width=True):
            with st.spinner("Calculating optimal FIDE pairings..."):
                generate_graph_pairings(t)
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

            c_sub, c_end = st.columns(2)
            if c_sub.form_submit_button("Submit Results & Next Round", type="primary"):
                if any(r == "Pending" for r in results_dict.values()):
                    st.error("⚠️ Enter a result for every board.")
                else:
                    for match in current_matches:
                        if match['board'] != 'BYE': match['result'] = results_dict[match['board']]
                    t.current_round += 1
                    st.rerun()
                    
            if c_end.form_submit_button("🏁 End Tournament Now"):
                if any(r == "Pending" for r in results_dict.values()):
                    st.error("⚠️ Finish scoring the current round first, or edit them to a result.")
                else:
                    for match in current_matches:
                        if match['board'] != 'BYE': match['result'] = results_dict[match['board']]
                    t.is_finished = True
                    st.rerun()

# ----------------- TAB 3: EDIT ALL RESULTS -----------------
with tab3:
    st.header("⏪ Edit Matches (All Rounds)")
    st.write("Updates instantly recalculate all standings and tiebreaks.")
    
    if not t.match_history:
        st.info("No matches have been generated yet.")
    
    # Group matches by round for easy viewing
    rounds = sorted(list(set([m['round'] for m in t.match_history])), reverse=True)
    
    for r in rounds:
        with st.expander(f"Round {r}", expanded=(r == t.current_round)):
            r_matches = [m for m in t.match_history if m['round'] == r]
            for match in r_matches:
                w_p = t.players[match['white_id']]
                if match['board'] == 'BYE':
                    st.write(f"**BYE:** {w_p.name}")
                else:
                    b_p = t.players[match['black_id']]
                    new_res = st.selectbox(
                        f"Board {match['board']}: ⚪ {w_p.name} vs ⚫ {b_p.name}",
                        options=["Pending", "1-0", "0-1", "0.5-0.5"],
                        index=["Pending", "1-0", "0-1", "0.5-0.5"].index(match['result']),
                        key=f"all_edit_R{r}_B{match['board']}"
                    )
                    if new_res != match['result']:
                        match['result'] = new_res
                        st.toast("Match updated!")
                        st.rerun()

# ----------------- TAB 4: SETTINGS & TIEBREAKS -----------------
with tab4:
    st.header("⚙️ Tiebreak Priorities")
    st.markdown("""
    **Available Tiebreaks:**
    *   **TB1: Most Wins by Black:** Favors players who scored wins while playing with the Black pieces.
    *   **TB2: Head-to-Head:** Points scored specifically against tied opponents.
    *   **TB3: Buchholz:** The sum of the final scores of all of a player's opponents.
    *   **TB4: Buchholz Cut-1:** Same as Buchholz, but removes the lowest-scoring opponent to protect against penalization for playing a weak opponent in round 1.
    """)
    
    st.write("**Drag or remove items in the multiselect box to change the sorting priority:**")
    all_tb_options = [
        "TB1: Most Wins by Black", "TB2: Head-to-Head", 
        "TB3: Buchholz (Sum of opponent scores)", "TB4: Buchholz Cut-1 (Least opponent removed)"
    ]
    
    new_tbs = st.multiselect("Active Tiebreaks (In Order of Priority)", all_tb_options, default=t.tiebreaks)
    
    if new_tbs != t.tiebreaks:
        t.tiebreaks = new_tbs
        st.toast("Tiebreaks updated!")
        st.rerun()
