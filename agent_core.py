# agent_core.py
import os
import requests
import time
import json

AGRO_BASE = "http://api.agromonitoring.com/agro/1.0"

class Agent:
    def __init__(self):
        self.memory = {}     # stores structured keys: area_acres, soil_type, ph, moisture, location, polygons, last_polygon, soil_moisture...
        self.logs = []
        self.pending_questions = []
        self.last_user_input = ""
        self.last_result = None

    # ---------- Public API ----------
    def handle_user_input(self, text: str):
        self.last_user_input = text or ""
        self.logs.append(f"[{time.strftime('%H:%M:%S')}] Received user query: {text}")

        plan = self.simple_planner(text)
        self.logs.append(f"[{time.strftime('%H:%M:%S')}] Planner steps: {plan}")

        missing = self.identify_missing_fields(plan)
        if missing:
            self.pending_questions = missing
            self.logs.append(f"[{time.strftime('%H:%M:%S')}] Missing fields: {missing}")
            return {"type": "followup", "questions": missing, "logs": self.logs}

        final = self.execute_plan(plan)
        self.logs.append(f"[{time.strftime('%H:%M:%S')}] Execution completed.")
        self.last_result = final
        return {"type": "final", "final": final, "logs": self.logs}

    def provide_followup_answers(self, answers: dict):
        # Store answers mapping question text -> field keys
        for q_text, answer in (answers or {}).items():
            field = self.question_to_field(q_text)
            if field is None:
                field = self._safe_key_from_question(q_text)
            parsed = self._parse_answer_for_field(field, answer)
            self.memory[field] = parsed
            self.logs.append(f"[{time.strftime('%H:%M:%S')}] Stored {field} = {parsed}")

        # If a polygon id (polyid) was provided as location value, detect & use it
        if "location" in self.memory:
            loc = self.memory.get("location")
            # detect polyid pattern (short alphanumeric) or lat,lon
            if isinstance(loc, str) and "," not in loc and len(loc) >= 6:
                # treat as polyid
                self.memory["polyid"] = loc
                self.logs.append(f"[{time.strftime('%H:%M:%S')}] Interpreting location as polyid: {loc}")
            elif isinstance(loc, str) and "," in loc:
                # lat,lon provided -> we can fetch agro using convenience function
                try:
                    lat_s, lon_s = loc.split(",")
                    lat = float(lat_s.strip()); lon = float(lon_s.strip())
                    agro = self.fetch_agro_for_location(lat, lon, cleanup=True)
                    self.logs.append(f"[{time.strftime('%H:%M:%S')}] Agro fetched for lat,lon: {bool(agro)}")
                except Exception as e:
                    self.logs.append(f"[{time.strftime('%H:%M:%S')}] Bad location format: {loc} -> {e}")

        # If polyid present in memory, fetch and store agro data
        if "polyid" in self.memory:
            polyid = self.memory.get("polyid")
            if polyid:
                soil = self.get_soil_by_polyid(polyid)
                weather = self.get_weather_by_polyid(polyid)
                # parse store minimal helpful fields
                self._store_agro_payloads(soil, weather)

        # Recompute and return final
        plan = self.simple_planner(self.last_user_input)
        final = self.execute_plan(plan)
        self.last_result = final
        return {"type": "final", "final": final, "logs": self.logs}

    # ---------- Planner ----------
    def simple_planner(self, text: str):
        required = []
        txt = (text or "").lower()
        if "acre" in txt or "area" in txt:
            required.append("area_acres")
        required.append("soil_type")
        required.append("ph")
        required.append("moisture")
        # location optional but helpful; we ask so user can paste lat,lon or choose polygon
        required.append("location")

        steps = [
            {"name": "collect_info", "requires": required},
            {"name": "call_crop_tool", "requires": ["soil_type", "ph", "area_acres", "moisture"]},
            {"name": "build_plan", "requires": ["crop_recommendation"]}
        ]
        return steps

    def identify_missing_fields(self, plan):
        missing = []
        for step in plan:
            for field in step.get("requires", []):
                if field not in self.memory:
                    missing.append(field.replace("_", " "))
        # Convert to human questions and dedupe
        out = []
        seen = set()
        for m in missing:
            if m not in seen:
                out.append(self.field_to_question(m))
                seen.add(m)
        return out

    # ---------- Field <-> Question mapping ----------
    def field_to_question(self, field_name):
        mapping = {
            "area acres": "Area in acres (e.g., 2)",
            "soil type": "Soil type (e.g., clay, sandy, loam, silty, peaty, chalky)",
            "ph": "Soil pH (e.g., 6.5)",
            "moisture": "Moisture level (low / medium / high)",
            "location": "Location (lat, lon) — comma separated (e.g., 12.9716,77.5946)"
        }
        if isinstance(field_name, str):
            return mapping.get(field_name, "Please provide " + field_name)
        return "Please provide " + str(field_name)

    def question_to_field(self, question_text):
        q = self._normalize(question_text)
        mapping = {
            "area_acres": self._normalize("Area in acres (e.g., 2)"),
            "soil_type": self._normalize("Soil type (e.g., clay, sandy, loam, silty, peaty, chalky)"),
            "ph": self._normalize("Soil pH (e.g., 6.5)"),
            "moisture": self._normalize("Moisture level (low / medium / high)"),
            "location": self._normalize("Location (lat, lon) — comma separated (e.g., 12.9716,77.5946)")
        }
        for field, qnorm in mapping.items():
            if q == qnorm:
                return field
        # heuristics
        if "area" in q:
            return "area_acres"
        if "soil" in q and "type" in q:
            return "soil_type"
        if "ph" in q or "pH" in question_text:
            return "ph"
        if "moisture" in q:
            return "moisture"
        if "location" in q or ("lat" in q and "lon" in q):
            return "location"
        return None

    # ---------- Execution ----------
    def execute_plan(self, plan):
        area = float(self.memory.get("area_acres", 1))
        soil_type = str(self.memory.get("soil_type", "clay")).lower()
        ph = float(self.memory.get("ph", 6.5))
        moisture = str(self.memory.get("moisture", "medium")).lower()

        # Use agro soil_moisture (if present) to adjust qualitative moisture
        if "soil_moisture" in self.memory:
            try:
                sm = float(self.memory.get("soil_moisture"))
                if sm < 0.15:
                    moisture = "low"
                elif sm < 0.35:
                    moisture = "medium"
                else:
                    moisture = "high"
                self.logs.append(f"[{time.strftime('%H:%M:%S')}] agro soil_moisture={sm} -> moisture={moisture}")
            except Exception:
                pass

        inputs = {
            "area_acres": area,
            "soil_type": soil_type,
            "ph": ph,
            "moisture": moisture
        }
        self.logs.append(f"[{time.strftime('%H:%M:%S')}] Using inputs: {inputs}")

        rec = self.crop_recommendation_tool(inputs)
        self.logs.append(f"[{time.strftime('%H:%M:%S')}] Crop tool returned: {rec}")

        plan_out = self.build_action_plan(inputs, rec)
        final = {
            "recommendation": f"Recommended crop: {rec['crop']} (confidence: {rec['confidence']})",
            "plan": plan_out,
            "costs": rec.get("estimated_costs", {}),
            "rationale": rec.get("reason", "")
        }
        return final

    # ---------- Crop logic (robust) ----------
    def crop_recommendation_tool(self, inputs: dict):
        soil_raw = (inputs.get("soil_type") or "").lower()
        ph = float(inputs.get("ph", 6.5))
        moisture = (inputs.get("moisture") or "medium").lower()
        area = float(inputs.get("area_acres", 1))

        # map variants to canonical soil keys
        soil_aliases = {
            "clay": ["clay"],
            "loam": ["loam"],
            "sandy": ["sand", "sandy"],
            "silty": ["silt", "silty"],
            "peaty": ["peat", "peaty"],
            "chalky": ["chalk", "chalky"]
        }
        soil_key = None
        for k, vals in soil_aliases.items():
            if any(v in soil_raw for v in vals):
                soil_key = k
                break
        if soil_key is None:
            soil_key = "unknown"

        crop = None
        reason = ""
        conf = "low"
        est_cost = {}

        # Decision rules (clear, avoids always-groundnut)
        if soil_key in ["clay", "loam"]:
            if 5.5 <= ph <= 7.5 and moisture in ["medium", "high"]:
                crop = "Maize"
                reason = "Loamy/clay soil with near-neutral pH and sufficient moisture suits maize."
                conf = "high"
                est_cost = {"seeds": round(1000 * area, 2), "fertilizers": round(800 * area, 2)}
            elif moisture == "low":
                crop = "Pulses (e.g., Pigeon pea)"
                reason = "Drier conditions favor drought-tolerant pulses."
                conf = "medium"
                est_cost = {"seeds": round(700 * area, 2), "fertilizers": round(400 * area, 2)}
            else:
                crop = "Maize"
                reason = "General good fit for loamy/clay soils."
                conf = "medium"
                est_cost = {"seeds": round(1000 * area, 2)}

        elif soil_key == "sandy":
            if moisture == "low" and 5.5 <= ph <= 7.0:
                crop = "Millet"
                reason = "Sandy + low moisture favors millet."
                conf = "high"
                est_cost = {"seeds": round(600 * area, 2), "fertilizers": round(400 * area, 2)}
            elif moisture in ["medium", "high"] and 5.5 <= ph <= 7.5:
                crop = "Groundnut"
                reason = "Groundnut performs well in sandy soils with moderate moisture."
                conf = "medium"
                est_cost = {"seeds": round(700 * area, 2), "fertilizers": round(500 * area, 2)}
            else:
                crop = "Millet"
                reason = "Conservative choice for sandy when uncertain."
                conf = "medium"
                est_cost = {"seeds": round(650 * area, 2)}

        elif soil_key == "silty":
            if 6.0 <= ph <= 7.5 and moisture in ["medium", "high"]:
                crop = "Wheat"
                reason = "Silty + moist + near-neutral pH suits wheat."
                conf = "high"
                est_cost = {"seeds": round(1200 * area, 2), "fertilizers": round(900 * area, 2)}
            else:
                crop = "Barley"
                reason = "Barley tolerates variable silty conditions."
                conf = "medium"
                est_cost = {"seeds": round(1100 * area, 2), "fertilizers": round(850 * area, 2)}

        elif soil_key == "peaty":
            if moisture == "high":
                crop = "Rice"
                reason = "Peaty soils retain water — rice is well suited."
                conf = "high"
                est_cost = {"seeds": round(1300 * area, 2), "fertilizers": round(1000 * area, 2)}
            else:
                crop = "Maize"
                reason = "Peaty but drier -> maize possible."
                conf = "medium"
                est_cost = {"seeds": round(1000 * area, 2)}

        elif soil_key == "chalky":
            if ph > 7.0 and moisture in ["medium", "high"]:
                crop = "Sugarcane"
                reason = "Alkaline chalky soils with moisture suit sugarcane."
                conf = "medium"
                est_cost = {"seeds": round(1400 * area, 2), "fertilizers": round(1100 * area, 2)}
            else:
                crop = "Wheat"
                reason = "Wheat tolerates chalky conditions."
                conf = "low"
                est_cost = {"seeds": round(1200 * area, 2), "fertilizers": round(900 * area, 2)}

        else:
            if moisture == "low":
                crop = "Millet"
                reason = "Default drought-tolerant choice when soil unknown."
                conf = "medium"
                est_cost = {"seeds": round(700 * area, 2)}
            elif moisture in ["medium", "high"] and 6.0 <= ph <= 7.5:
                crop = "Maize"
                reason = "General-purpose choice for neutral pH and adequate moisture."
                conf = "medium"
                est_cost = {"seeds": round(1000 * area, 2)}
            else:
                crop = "Maize"
                reason = "Fallback recommendation."
                conf = "low"
                est_cost = {"seeds": round(1000 * area, 2)}

        return {"crop": crop, "reason": reason, "confidence": conf, "estimated_costs": est_cost}

    # ---------- Plan builder ----------
    def build_action_plan(self, inputs, rec):
        area = inputs.get("area_acres", 1)
        crop = rec.get("crop", "Unknown")
        plan = [
            {"task": f"Land preparation and seed purchase for {crop} on {area} acres",
             "weeks": 1,
             "notes": "Prepare field, buy certified seeds."},
            {"task": f"Sowing and initial fertilizer application for {crop}",
             "weeks": 2,
             "notes": "Sow seeds at recommended spacing. Apply basal fertilizer."},
            {"task": f"Crop maintenance and harvest planning for {crop}",
             "weeks": 10,
             "notes": "Irrigation as needed, pest checks, harvesting timeline planning."}
        ]
        return plan

    # ---------- Agro (polygon) helpers ----------
    def _get_api_key(self):
        return os.getenv("OPENWEATHER_API_KEY", "").strip()

    def create_polygon_from_geojson(self, geojson_feature_or_collection, name=None):
        api_key = self._get_api_key()
        if not api_key:
            self.logs.append(f"[{time.strftime('%H:%M:%S')}] Agro API key missing.")
            return None
        # ensure Feature
        if isinstance(geojson_feature_or_collection, dict) and geojson_feature_or_collection.get("type") == "FeatureCollection":
            feats = geojson_feature_or_collection.get("features", [])
            if not feats:
                self.logs.append(f"[{time.strftime('%H:%M:%S')}] Empty FeatureCollection")
                return None
            geo_feature = feats[0]
        elif isinstance(geojson_feature_or_collection, dict) and geojson_feature_or_collection.get("type") == "Feature":
            geo_feature = geojson_feature_or_collection
        else:
            geo_feature = {"type": "Feature", "properties": {}, "geometry": geojson_feature_or_collection}

        payload = {"name": name or f"field_{int(time.time())}", "geo_json": geo_feature}
        url = f"{AGRO_BASE}/polygons?appid={api_key}"
        try:
            resp = requests.post(url, json=payload, timeout=20)
            if resp.ok:
                obj = resp.json()
                polyid = obj.get("id")
                self.memory.setdefault("polygons", {})[payload["name"]] = polyid
                self.memory["last_polygon"] = obj
                self.logs.append(f"[{time.strftime('%H:%M:%S')}] Created polygon {payload['name']} -> {polyid}")
                return obj
            else:
                self.logs.append(f"[{time.strftime('%H:%M:%S')}] Polygon creation failed: {resp.status_code} {resp.text}")
                return None
        except Exception as e:
            self.logs.append(f"[{time.strftime('%H:%M:%S')}] Polygon create error: {e}")
            return None

    def delete_polygon_by_id(self, polyid):
        api_key = self._get_api_key()
        if not api_key or not polyid:
            return False
        url = f"{AGRO_BASE}/polygons/{polyid}?appid={api_key}"
        try:
            resp = requests.delete(url, timeout=10)
            if resp.ok:
                self.logs.append(f"[{time.strftime('%H:%M:%S')}] Deleted polygon {polyid}")
                # remove from memory if present
                if "polygons" in self.memory:
                    for k, v in list(self.memory["polygons"].items()):
                        if v == polyid:
                            del self.memory["polygons"][k]
                return True
            else:
                self.logs.append(f"[{time.strftime('%H:%M:%S')}] Polygon delete failed: {resp.status_code}")
                return False
        except Exception as e:
            self.logs.append(f"[{time.strftime('%H:%M:%S')}] Polygon delete error: {e}")
            return False

    def get_soil_by_polyid(self, polyid):
        api_key = self._get_api_key()
        if not api_key or not polyid:
            return None
        url = f"{AGRO_BASE}/soil?polyid={polyid}&appid={api_key}"
        try:
            resp = requests.get(url, timeout=15)
            if resp.ok:
                return resp.json()
            else:
                self.logs.append(f"[{time.strftime('%H:%M:%S')}] Soil fetch failed: {resp.status_code} {resp.text}")
                return None
        except Exception as e:
            self.logs.append(f"[{time.strftime('%H:%M:%S')}] Soil fetch error: {e}")
            return None

    def get_weather_by_polyid(self, polyid):
        api_key = self._get_api_key()
        if not api_key or not polyid:
            return None
        url = f"{AGRO_BASE}/weather?polyid={polyid}&appid={api_key}"
        try:
            resp = requests.get(url, timeout=15)
            if resp.ok:
                return resp.json()
            else:
                self.logs.append(f"[{time.strftime('%H:%M:%S')}] Weather fetch failed: {resp.status_code} {resp.text}")
                return None
        except Exception as e:
            self.logs.append(f"[{time.strftime('%H:%M:%S')}] Weather fetch error: {e}")
            return None

    def fetch_agro_for_location(self, lat, lon, cleanup=True):
        api_key = self._get_api_key()
        if not api_key:
            self.logs.append(f"[{time.strftime('%H:%M:%S')}] Agro API key missing; cannot fetch by location.")
            return None
        delta = 0.001
        poly_geojson = {
            "name": f"field_{int(time.time())}",
            "geo_json": {
                "type": "Feature",
                "properties": {},
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [[
                        [lon - delta, lat - delta],
                        [lon + delta, lat - delta],
                        [lon + delta, lat + delta],
                        [lon - delta, lat + delta],
                        [lon - delta, lat - delta]
                    ]]
                }
            }
        }
        created = self.create_polygon_from_geojson(poly_geojson["geo_json"], name=poly_geojson["name"])
        polyid = None
        if created:
            polyid = created.get("id")
        if not polyid:
            return None
        soil = self.get_soil_by_polyid(polyid)
        weather = self.get_weather_by_polyid(polyid)
        self._store_agro_payloads(soil, weather)
        if cleanup:
            try:
                self.delete_polygon_by_id(polyid)
            except Exception:
                pass
        return {"soil": soil, "weather": weather}

    def _store_agro_payloads(self, soil, weather):
        try:
            if soil:
                s = soil[0] if isinstance(soil, list) and soil else soil
                if isinstance(s, dict):
                    if "soil_moisture" in s:
                        try:
                            self.memory["soil_moisture"] = float(s.get("soil_moisture"))
                        except:
                            self.memory["soil_moisture"] = s.get("soil_moisture")
                    if "soil_temp" in s:
                        try:
                            self.memory["soil_temp_k"] = float(s.get("soil_temp"))
                        except:
                            self.memory["soil_temp_k"] = s.get("soil_temp")
            if weather:
                w = weather[0] if isinstance(weather, list) and weather else weather
                if isinstance(w, dict):
                    if "rain" in w:
                        self.memory["agro_precip"] = w.get("rain")
                    if "temperature" in w:
                        self.memory["agro_temp_k"] = w.get("temperature")
                    if "ts" in w:
                        self.memory["agro_weather_ts"] = w.get("ts")
            self.logs.append(f"[{time.strftime('%H:%M:%S')}] Stored agro payloads into memory.")
        except Exception as e:
            self.logs.append(f"[{time.strftime('%H:%M:%S')}] Error storing agro payloads: {e}")

    # ---------- Utilities ----------
    def _normalize(self, text: str) -> str:
        if text is None:
            return ""
        return " ".join(str(text).strip().lower().split())

    def _safe_key_from_question(self, question_text: str) -> str:
        q = self._normalize(question_text)
        return q.replace(" ", "_").replace("(", "").replace(")", "").replace("/", "_").replace(",", "")

    def _parse_answer_for_field(self, field: str, answer):
        if answer is None:
            return answer
        try:
            if field == "area_acres":
                return float(answer)
            if field == "ph":
                return float(answer)
            if field == "moisture":
                return str(answer).strip().lower()
            if field == "soil_type":
                return str(answer).strip().lower()
            if field == "location":
                return str(answer).strip()
        except Exception:
            pass
        try:
            s = str(answer).strip()
            if "." in s:
                return float(s)
            return int(s)
        except Exception:
            return str(answer).strip()
