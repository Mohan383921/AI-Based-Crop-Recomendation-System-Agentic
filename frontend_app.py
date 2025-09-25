# frontend_app.py
import streamlit as st
import hashlib
import json
from agent_core import Agent  # make sure agent_core.py is in same folder

st.set_page_config(page_title="Crop Recommendation Tool", page_icon="🌱")
st.title("🌾 Interactive Crop Recommendation Tool")

st.markdown("""
This tool asks only the necessary questions and recommends the best crop based on your inputs.
It supports creating/reusing Agro polygons (OpenWeather Agromonitoring) to fetch soil/weather data.
""")

# Initialize agent
if 'agent' not in st.session_state:
    st.session_state.agent = Agent()
agent = st.session_state.agent

def _stable_key(question: str, index: int) -> str:
    short_hash = hashlib.md5(str(question).encode("utf-8")).hexdigest()[:8]
    return f"{index}_{short_hash}"

def normalize_question(q: str) -> str:
    if not isinstance(q, str):
        q = str(q)
    return " ".join(q.strip().split()).lower()

def dedupe_questions(questions):
    seen = set()
    deduped = []
    for q in (questions or []):
        q_norm = normalize_question(q)
        if q_norm not in seen:
            deduped.append(q)
            seen.add(q_norm)
    return deduped

def ask_followups(questions):
    responses = {}
    questions = dedupe_questions(questions or [])
    for i, q in enumerate(questions):
        key = _stable_key(q, i)
        q_lower = q.lower()

        if "area" in q_lower:
            val = st.number_input(q, min_value=0.01, value=1.0, step=0.1, key=key)
        elif "location" in q_lower or ("lat" in q_lower and "lon" in q_lower):
            # Accept either a polyid selection (if available) or lat,lon text
            # We'll show both: if agent has polygons saved, let user choose one
            pol_list = agent.memory.get("polygons", {})
            if pol_list:
                options = ["(Use polygon)"] + [f"{name} -> {pid}" for name, pid in pol_list.items()]
                sel = st.selectbox(f"{q} — choose polygon or paste lat,lon", options, key=key + "_sel")
                if sel and sel != "(Use polygon)":
                    # extract polyid
                    chosen = sel.split("->")[-1].strip()
                    val = chosen
                else:
                    val = st.text_input(q + " (or paste lat,lon)", value="", key=key + "_txt", help="Enter lat,lon like: 12.9716,77.5946 (if not using polygon)")
            else:
                val = st.text_input(q, value="", key=key, help="Enter lat,lon like: 12.9716,77.5946")
        elif ("ph" in q_lower) or ("soil pH".lower() in q_lower):
            val = st.number_input(q, min_value=3.0, max_value=9.0, value=6.5, step=0.1, key=key)
        elif "moisture" in q_lower:
            val = st.selectbox(q, ["Low", "Medium", "High"], key=key)
        elif "soil type" in q_lower:
            val = st.selectbox(q, ["clay", "sandy", "loam", "silty", "peaty", "chalky", "other"], index=0, key=key)
        else:
            val = st.text_input(q, key=key)
        responses[q] = val
    return responses

# --- Polygon management UI ---
st.sidebar.header("Polygon / Agro (optional)")
st.sidebar.markdown("You may create a polygon from a GeoJSON file and reuse it for agro fetches.")

geo_upload = st.sidebar.file_uploader("Upload GeoJSON file", type=["geojson", "json"])
geo_text = st.sidebar.text_area("Or paste GeoJSON here (optional)", height=200)

if st.sidebar.button("Create Polygon on Agro", key="sidebar_create_poly"):
    raw = None
    if geo_upload is not None:
        try:
            raw = json.load(geo_upload)
        except Exception as e:
            st.sidebar.error("Invalid GeoJSON uploaded: " + str(e))
            raw = None
    elif geo_text and geo_text.strip():
        try:
            raw = json.loads(geo_text)
        except Exception as e:
            st.sidebar.error("Invalid GeoJSON pasted: " + str(e))
            raw = None
    else:
        st.sidebar.error("Please upload or paste a GeoJSON Feature/FeatureCollection or geometry")
        raw = None

    if raw:
        res = agent.create_polygon_from_geojson(raw, name=f"frontend_{int(time.time())}")
        if res:
            polyid = res.get("id")
            st.sidebar.success(f"Polygon created. polyid: {polyid}")
            st.sidebar.write(res)
        else:
            st.sidebar.error("Polygon creation failed. Check agent logs in debug.")

# --- Main flow state ---
if 'step' not in st.session_state:
    st.session_state.step = 0

if st.session_state.step == 0:
    user_query = st.text_input("Describe your farm/focus area (optional):", value="Get crop recommendation", key="initial_query")
    if st.button("Start", key="start_btn"):
        result = agent.handle_user_input(user_query)
        st.session_state.result = result
        st.session_state.step = 1

if st.session_state.step == 1:
    result = st.session_state.get('result', {})
    if result.get('type') == "followup":
        st.warning("Please provide the following details:")
        responses = ask_followups(result.get('questions', []))
        if st.button("Submit Answers", key="submit_btn"):
            # If user chose polygon polyid (value is polyid string), pass as value for the location question
            final_result = agent.provide_followup_answers(responses)
            st.session_state.final_result = final_result
            st.session_state.step = 2
            st.write("Submitted answers:", responses)
    else:
        st.session_state.final_result = result
        st.session_state.step = 2

if st.session_state.step == 2:
    final_obj = st.session_state.final_result or {}
    final = final_obj.get('final', {}) if isinstance(final_obj, dict) else {}

    recommendation_text = final.get('recommendation', '')
    try:
        rec_display = recommendation_text.split(': ')[1]
    except Exception:
        rec_display = recommendation_text

    st.success(f"✅ Recommended Crop: **{rec_display}**")
    st.info(f"**Reason:** {final.get('rationale', 'No rationale provided')}")

    st.subheader("Estimated Costs")
    costs = final.get("costs", {})
    if costs:
        for k, v in costs.items():
            st.write(f"{k.capitalize()}: ₹{v}")
    else:
        st.write("No cost estimate available.")

    st.subheader("Suggested Plan")
    plan = final.get("plan", [])
    if plan:
        for step_item in plan:
            st.write(f"- **{step_item.get('task','Task')}** (Duration: {step_item.get('weeks','N/A')} weeks)")
            st.write(f"  Notes: {step_item.get('notes','')}")
    else:
        st.write("No plan available.")

    if st.checkbox("Show agent logs & memory (debug)", value=False):
        st.write("Logs:", final_obj.get('logs', agent.logs))
        st.write("Memory:", agent.memory)

    if st.button("Start Over", key="startover_btn"):
        st.session_state.step = 0
        st.session_state.agent = Agent()
        for k in ['result', 'final_result']:
            if k in st.session_state:
                del st.session_state[k]
