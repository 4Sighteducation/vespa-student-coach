import os
import json
from flask import Flask, jsonify, request
from flask_cors import CORS
from dotenv import load_dotenv
import logging
import requests # For Knack API calls
import time # For cache expiry
from datetime import datetime # For timestamp parsing
# import openai # Placeholder for when LLM is integrated

# Load environment variables from .env file (optional, Heroku uses config vars)
load_dotenv()

app = Flask(__name__)

# --- CORS Configuration ---
# Allow requests ONLY from your Knack domain for security.
CORS(app, resources={r"/api/*": {"origins": "https://vespaacademy.knack.com"}})

# --- Logging Configuration ---
# Basic logging setup
if not app.debug:
    app.logger.setLevel(logging.INFO)
    handler = logging.StreamHandler()
    handler.setLevel(logging.INFO)
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    handler.setFormatter(formatter)
    if not app.logger.handlers:
        app.logger.addHandler(handler)

app.logger.info("Flask Student Coach App Initializing...")

# --- Environment Variables (placeholders, to be set in Heroku) ---
KNACK_APP_ID = os.getenv('KNACK_APP_ID')
KNACK_API_KEY = os.getenv('KNACK_API_KEY')
OPENAI_API_KEY = os.getenv('OPENAI_API_KEY')

# Knack base URL for API calls - good to define once
KNACK_API_BASE_URL = "https://api.knack.com/v1/objects"

if not KNACK_APP_ID or not KNACK_API_KEY:
    app.logger.warning("KNACK_APP_ID or KNACK_API_KEY is not set. Knack integration will fail.")
if not OPENAI_API_KEY:
    app.logger.warning("OPENAI_API_KEY is not set. OpenAI integration will fail.")

# --- Load Knowledge Bases (Copied from Tutor app.py, adapt paths if needed) ---
def load_json_file(file_path):
    try:
        # Assuming KB files are in a 'knowledge_base' subdirectory relative to this app.py
        current_dir = os.path.dirname(os.path.abspath(__file__))
        full_path = os.path.join(current_dir, 'knowledge_base', file_path)
        full_path = os.path.normpath(full_path)
        app.logger.info(f"Attempting to load JSON KB: {full_path}")
        with open(full_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        # Check if data is in Knack 'records' format for some files
        if isinstance(data, dict) and 'records' in data and isinstance(data['records'], list) and file_path in ['reporttext.json']:
            app.logger.info(f"Extracted {len(data['records'])} records from {file_path}")
            return data['records']
        app.logger.info(f"Loaded {file_path} (data type: {type(data)})")
        return data
    except FileNotFoundError:
        app.logger.error(f"Knowledge base file not found: {file_path} (looked in {full_path})")
    except json.JSONDecodeError:
        app.logger.error(f"Error decoding JSON from file: {file_path}")
    except Exception as e:
        app.logger.error(f"Error loading JSON file {file_path}: {e}")
    return None

# Load relevant KBs - adjust file names/paths as per your student coach's KB structure
psychometric_question_details_kb = load_json_file('psychometric_question_details.json')
report_text_kb = load_json_file('reporttext.json') # Object_33 content
grade_points_mapping_kb = load_json_file('grade_to_points_mapping.json')
# Add ALPS band KBs if academic benchmarks are to be calculated in detail
alps_bands_aLevel_75_kb = load_json_file('alpsBands_aLevel_75.json') # Example for standard MEG
# ... load other ALPS KBs as needed (60th, 90th, 100th, BTEC, etc.)

# --- Knack API Helper Functions (Adapted from Tutor app.py) ---
def get_knack_record(object_key, record_id=None, filters=None, page=1, rows_per_page=1000):
    if not KNACK_APP_ID or not KNACK_API_KEY:
        app.logger.error("Knack App ID or API Key is missing for get_knack_record.")
        return None
    headers = {
        'X-Knack-Application-Id': KNACK_APP_ID,
        'X-Knack-REST-API-Key': KNACK_API_KEY,
        'Content-Type': 'application/json'
    }
    params = {'page': page, 'rows_per_page': rows_per_page}
    if filters:
        params['filters'] = json.dumps(filters)

    url_path = f"/{object_key}/records"
    if record_id:
        url_path = f"/{object_key}/records/{record_id}"
        current_params = {} # No params for specific ID fetch usually
    else:
        current_params = params
    
    full_url = f"{KNACK_API_BASE_URL}{url_path}"
    app.logger.info(f"Knack API call: URL={full_url}, Params={current_params}")

    try:
        response = requests.get(full_url, headers=headers, params=current_params)
        response.raise_for_status()
        data = response.json()
        app.logger.info(f"Knack API success for {object_key}. Records: {len(data.get('records', [])) if not record_id else '1 (specific ID)'}")
        return data
    except requests.exceptions.HTTPError as e:
        app.logger.error(f"HTTP error fetching Knack data ({object_key}): {e}. Response: {response.content if response else 'No response object'}")
    except requests.exceptions.RequestException as e:
        app.logger.error(f"Request exception fetching Knack data ({object_key}): {e}")
    except json.JSONDecodeError:
        app.logger.error(f"JSON decode error for Knack response ({object_key}). Response text: {response.text if response else 'No response object'}")
    return None

# --- Student Data Specific Fetching Functions ---

def get_student_user_details(student_object3_id):
    "Fetches Object_3 record for the student."
    if not student_object3_id: return None
    return get_knack_record("object_3", record_id=student_object3_id)

def get_student_object10_record(student_email):
    "Fetches student's Object_10 (VESPA Results) record using their email."
    if not student_email: return None
    filters = [{'field': 'field_197', 'operator': 'is', 'value': student_email}] # field_197 is student email in Object_10
    response = get_knack_record("object_10", filters=filters)
    if response and response.get('records'):
        if len(response['records']) > 1:
            app.logger.warning(f"Multiple Object_10 records found for email {student_email}. Using the first one.")
        return response['records'][0]
    app.logger.warning(f"No Object_10 record found for email {student_email}.")
    return None

def get_student_object29_questionnaire_data(object10_id, cycle_number):
    "Fetches Object_29 (Questionnaire) data for a given Object_10 ID and cycle."
    if not object10_id or cycle_number is None: return None
    filters = [
        {'field': 'field_792', 'operator': 'is', 'value': object10_id}, # Connection to Object_10
        {'field': 'field_863_raw', 'operator': 'is', 'value': str(cycle_number)} # Cycle number
    ]
    response = get_knack_record("object_29", filters=filters)
    if response and response.get('records'):
        return response['records'][0] # Assuming one Object_29 per student per cycle
    app.logger.warning(f"No Object_29 data found for Object_10 ID {object10_id}, Cycle {cycle_number}.")
    return None

def get_student_academic_profile(student_object3_id):
    "Fetches Object_112 (Academic Profile) using student's Object_3 ID."
    if not student_object3_id: return None
    # field_3070 in Object_112 is the Account connection to Object_3
    filters = [{'field': 'field_3070', 'operator': 'is', 'value': student_object3_id}]
    response = get_knack_record("object_112", filters=filters)
    if response and response.get('records'):
        return response['records'][0]
    app.logger.warning(f"No Object_112 (Academic Profile) found for student Object_3 ID {student_object3_id}.")
    return None    

# --- Data Processing Helper (Simplified for now) ---
def get_score_profile_text(score_value):
    if score_value is None: return "N/A"
    try:
        score = float(score_value)
        if score >= 8: return "High"
        if score >= 6: return "Medium"
        if score >= 4: return "Low"
        if score >= 0: return "Very Low"
        return "N/A"
    except (ValueError, TypeError):
        return "N/A"

# --- Main API Endpoint --- 
@app.route('/api/v1/student_coaching_data', methods=['POST', 'OPTIONS'])
def student_coaching_data():
    app.logger.info(f"Received request for /api/v1/student_coaching_data. Method: {request.method}")
    if request.method == 'OPTIONS':
        return _build_cors_preflight_response()
    
    if request.method == 'POST':
        data = request.get_json()
        student_object3_id = data.get('student_object3_id')
        if not student_object3_id:
            app.logger.error("Missing 'student_object3_id' in request.")
            return jsonify({"error": "Missing student_object3_id"}), 400

        app.logger.info(f"Processing data for student Object_3 ID: {student_object3_id}")

        # 1. Fetch Student User Details (Object_3)
        student_user_data = get_student_user_details(student_object3_id)
        student_email = None
        student_name_from_obj3 = f"Student_{student_object3_id[:6]}" # Default
        if student_user_data:
            # field_70 in Object_3 is the email field.
            # Knack email fields when fetched as objects can be like: {'email': 'actual.email@example.com', 'label': 'actual.email@example.com'}
            # Or the _raw version might sometimes just be the string, or an HTML string as observed.
            raw_val_field70 = student_user_data.get('field_70_raw') # Knack raw value
            obj_val_field70 = student_user_data.get('field_70') # Knack object value

            # Priority 1: Try to get email from field_70 if it's a Knack email object
            if isinstance(obj_val_field70, dict) and 'email' in obj_val_field70 and isinstance(obj_val_field70['email'], str):
                student_email = obj_val_field70['email'].strip()
            # Priority 2: Try to get email from field_70_raw if it's a Knack email object (less common for _raw to be object)
            elif isinstance(raw_val_field70, dict) and 'email' in raw_val_field70 and isinstance(raw_val_field70['email'], str):
                 student_email = raw_val_field70['email'].strip()
            # Priority 3: If field_70_raw is a string (as suggested by logs)
            elif isinstance(raw_val_field70, str):
                temp_email_str = raw_val_field70.strip()
                # Check if it's an HTML string like <a href="mailto:email@example.com">text</a>
                if temp_email_str.lower().startswith('<a') and 'mailto:' in temp_email_str.lower() and temp_email_str.lower().endswith('</a>'):
                    try:
                        # Extract from within mailto:"..." part of href
                        mailto_keyword = 'mailto:'
                        mailto_start_index = temp_email_str.lower().find(mailto_keyword) + len(mailto_keyword)
                        
                        # Find the end of the email address in href. It could be terminated by ", ', >.
                        end_char_index = len(temp_email_str) 
                        
                        quote_index = temp_email_str.find('"', mailto_start_index)
                        if quote_index != -1:
                            end_char_index = min(end_char_index, quote_index)
                        
                        single_quote_index = temp_email_str.find("'", mailto_start_index)
                        if single_quote_index != -1:
                            end_char_index = min(end_char_index, single_quote_index)
                            
                        angle_bracket_index = temp_email_str.find('>', mailto_start_index)
                        if angle_bracket_index != -1: # Should be present if parsing href part of <a> tag
                            end_char_index = min(end_char_index, angle_bracket_index)

                        extracted_from_href = temp_email_str[mailto_start_index:end_char_index].strip()
                        
                        if '@' in extracted_from_href and ' ' not in extracted_from_href and '<' not in extracted_from_href:
                            student_email = extracted_from_href
                        
                        # Fallback: if mailto parsing didn't yield a good email, try getting link text
                        if not student_email:
                            text_start_actual_index = temp_email_str.find('>') 
                            if text_start_actual_index != -1:
                                text_start_actual_index +=1 # move past '>'
                                text_end_index = temp_email_str.lower().rfind('</a>') # Use rfind for last occurrence
                                if text_end_index > text_start_actual_index :
                                    extracted_text = temp_email_str[text_start_actual_index:text_end_index].strip()
                                    if '@' in extracted_text and ' ' not in extracted_text and '<' not in extracted_text:
                                        student_email = extracted_text
                                        app.logger.info(f"Used email from link text: {student_email}")

                    except Exception as e_parse:
                        app.logger.warning(f"Error parsing specific HTML email string '{temp_email_str}': {e_parse}")
                # If it's just a plain email string (no HTML detected or parsing failed)
                elif '@' in temp_email_str and not '<' in temp_email_str:
                    student_email = temp_email_str
            # Priority 4: Fallback to field_70 if it's a plain string and others failed
            elif isinstance(obj_val_field70, str) and '@' in obj_val_field70 and not '<' in obj_val_field70 :
                 student_email = obj_val_field70.strip()
            
            if student_email:
                app.logger.info(f"Extracted student email: {student_email} for Object_3 ID {student_object3_id}")
            else:
                app.logger.warning(f"Could not extract plain email from Object_3 field_70 for ID {student_object3_id}. "
                                   f"field_70_raw: '{raw_val_field70}', field_70: '{obj_val_field70}'")

            name_parts = student_user_data.get('field_66_raw') # Assuming field_66 is Name
            if name_parts and isinstance(name_parts, dict):
                student_name_from_obj3 = name_parts.get('full', student_name_from_obj3)
        else:
            app.logger.warning(f"Could not fetch Object_3 details for ID {student_object3_id}")
            # Return error or limited dummy if core student info fails
            return jsonify({"error": f"Could not retrieve user details for {student_object3_id}"}), 404

        # 2. Fetch Student's VESPA Profile (Object_10)
        object10_data = get_student_object10_record(student_email) if student_email else None
        current_cycle = 0
        vespa_scores_for_profile = {}
        student_reflections = {}
        if object10_data:
            current_cycle_str = object10_data.get("field_146_raw", "0")
            # Ensure current_cycle_str is treated as a string before isdigit()
            current_cycle = int(str(current_cycle_str)) if str(current_cycle_str).isdigit() else 0
            app.logger.info(f"Student's current cycle from Object_10: {current_cycle}")
            vespa_scores_for_profile = {
                "Vision": {"score_1_to_10": object10_data.get("field_147"), "score_profile_text": get_score_profile_text(object10_data.get("field_147"))},
                "Effort": {"score_1_to_10": object10_data.get("field_148"), "score_profile_text": get_score_profile_text(object10_data.get("field_148"))},
                "Systems": {"score_1_to_10": object10_data.get("field_149"), "score_profile_text": get_score_profile_text(object10_data.get("field_149"))},
                "Practice": {"score_1_to_10": object10_data.get("field_150"), "score_profile_text": get_score_profile_text(object10_data.get("field_150"))},
                "Attitude": {"score_1_to_10": object10_data.get("field_151"), "score_profile_text": get_score_profile_text(object10_data.get("field_151"))}
            }
            student_reflections = {
                f"rrc{current_cycle}_comment": object10_data.get(f"field_{2301+current_cycle}"), # RRC1=2302, RRC2=2303, RRC3=2304
                f"goal{current_cycle}": object10_data.get(f"field_{2498+current_cycle}" if current_cycle==1 else f"field_{2491+current_cycle}") # Goal1=2499, Goal2=2493, Goal3=2494
            }
        else:
            app.logger.warning(f"No Object_10 data for student {student_name_from_obj3} (Email: {student_email})")
            # Populate with N/A or defaults if Object_10 is missing
            for v_element in ["Vision", "Effort", "Systems", "Practice", "Attitude"]:
                vespa_scores_for_profile[v_element] = {"score_1_to_10": "N/A", "score_profile_text": "N/A"}
            student_reflections = {"rrc_comment": "Not available", "goal": "Not available"}

        # 3. Fetch Questionnaire Data (Object_29)
        all_scored_statements = []
        object29_highlights_top_bottom = {"top_3": [], "bottom_3": []}
        if object10_data and current_cycle > 0 and psychometric_question_details_kb:
            object29_data = get_student_object29_questionnaire_data(object10_data.get('id'), current_cycle)
            if object29_data:
                for q_detail in psychometric_question_details_kb:
                    field_id = q_detail.get('currentCycleFieldId') # These are generic like field_794
                    if not field_id: continue
                    raw_score = object29_data.get(field_id) # Or field_id + "_raw" depending on Knack field type
                    if raw_score is None and field_id.startswith("field_"):
                        score_obj = object29_data.get(field_id + '_raw')
                        if isinstance(score_obj, dict):
                            raw_score = score_obj.get('value') 
                        elif score_obj is not None:
                            raw_score = score_obj
                    
                    try:
                        score = int(raw_score)
                        all_scored_statements.append({
                            "question_text": q_detail.get('questionText', 'Unknown Question'),
                            "score": score,
                            "vespa_category": q_detail.get('vespaCategory', 'N/A')
                        })
                    except (ValueError, TypeError):
                        app.logger.debug(f"Could not parse score '{raw_score}' for {field_id} in Object_29.")
                
                if all_scored_statements:
                    all_scored_statements.sort(key=lambda x: x["score"])
                    object29_highlights_top_bottom["bottom_3"] = all_scored_statements[:3]
                    object29_highlights_top_bottom["top_3"] = all_scored_statements[-3:][::-1]
            else:
                 app.logger.warning(f"No Object_29 data retrieved for student {student_name_from_obj3}, cycle {current_cycle}")       
        elif not psychometric_question_details_kb:
            app.logger.error("Psychometric Question Details KB not loaded. Cannot process Object_29.")

        # 4. Fetch Academic Profile (Object_112)
        academic_summary = [] # Placeholder
        object112_data = get_student_academic_profile(student_object3_id)
        if object112_data:
            app.logger.info(f"Fetched Object_112 data for student: {object112_data.get('field_3066')} (Name in Obj112)")
            # Basic parsing, needs to match the more complex logic from tutor app.py for full detail
            for i in range(1, 16): # Sub1 to Sub15
                subject_json_str = object112_data.get(f"field_30{79+i}") # e.g. field_3080
                if subject_json_str and isinstance(subject_json_str, str) and subject_json_str.strip().startswith('{'):
                    try:
                        s_data = json.loads(subject_json_str)
                        # Basic extraction, needs more robust point/MEG calculation later
                        norm_qual = s_data.get('examType', 'A Level') # Simplified for now
                        current_grade = s_data.get('currentGrade', 'N/A')
                        academic_summary.append({
                            "subject": s_data.get('subject', f'Subject {i}'),
                            "currentGrade": current_grade,
                            "targetGrade": s_data.get('targetGrade', 'N/A'),
                            "effortGrade": s_data.get('effortGrade', 'N/A'),
                            "examType": s_data.get('examType', 'N/A'),
                            "normalized_qualification_type": norm_qual,
                            "currentGradePoints": 0, # Placeholder, need get_points logic
                            "standardMegPoints": 0 # Placeholder, need get_meg logic
                        })
                    except json.JSONDecodeError:
                        app.logger.warning(f"Could not parse subject JSON from field_30{79+i} in Object_112.")
        else:
            app.logger.warning(f"No Object_112 data for student {student_name_from_obj3}.")
            academic_summary.append({"subject": "Academic data not found.", "currentGrade": "N/A"})


        # TODO: Integrate LLM call here later. For now, use placeholders or basic derived insights.
        llm_insights_placeholder = {
            "student_overview_summary": f"Snapshot for {student_name_from_obj3}: Focus on areas from your VESPA profile and questionnaire.",
            "chart_comparative_insights": "Compare your scores to understand your strengths.",
            "most_important_coaching_questions": ["What is one thing you want to improve?"],
            "student_comment_analysis": "Your reflections are a good starting point.",
            "suggested_student_goals": ["Set a small, achievable goal for this week."],
            "academic_benchmark_analysis": "Review your grades against any targets you have.",
            "questionnaire_interpretation_and_reflection_summary": "Think about why you answered the questionnaire statements the way you did."
        }

        final_response = {
            "student_name": student_name_from_obj3,
            "student_level": object10_data.get("field_568_raw", "N/A") if object10_data else "N/A",
            "current_cycle": current_cycle,
            "vespa_profile": vespa_scores_for_profile,
            "academic_profile_summary": academic_summary,
            "student_reflections_and_goals": student_reflections,
            "object29_question_highlights": object29_highlights_top_bottom,
            "llm_generated_insights": llm_insights_placeholder, 
            "all_scored_questionnaire_statements": all_scored_statements,
            "school_vespa_averages": None # Placeholder, add if get_school_vespa_averages is used
        }
        return jsonify(final_response), 200

@app.route('/api/v1/chat_turn', methods=['POST', 'OPTIONS'])
def chat_turn():
    app.logger.info(f"Received request for /api/v1/chat_turn. Method: {request.method}")
    if request.method == 'OPTIONS':
        return _build_cors_preflight_response()
    if request.method == 'POST':
        data = request.get_json()
        student_knack_id = data.get('student_knack_id', 'Unknown Student')
        current_user_message = data.get('current_user_message', 'No message')
        app.logger.info(f"Chat turn for student {student_knack_id}. Message: {current_user_message}")
        
        # Dummy AI response
        dummy_response = {
            "ai_response": f"Hello {student_knack_id}! You said: '{current_user_message}'. I am a placeholder AI. Full chat functionality coming soon!",
            "suggested_activities_in_chat": [],
            "ai_message_knack_id": "dummy_chat_id_123" # Placeholder for potential future use
        }
        return jsonify(dummy_response), 200

# Add other placeholder endpoints as needed by vespa-student-coach.js, e.g., for chat history
@app.route('/api/v1/chat_history', methods=['POST', 'OPTIONS'])
def chat_history():
    app.logger.info(f"Received request for /api/v1/chat_history. Method: {request.method}")
    if request.method == 'OPTIONS':
        return _build_cors_preflight_response()
    if request.method == 'POST':
        data = request.get_json()
        student_knack_id = data.get('student_knack_id', 'Unknown Student')
        app.logger.info(f"Chat history request for student {student_knack_id}")
        dummy_response = {
            "chat_history": [
                {"role": "assistant", "content": "Welcome to the placeholder chat history!"}
            ],
            "total_count": 1,
            "liked_count": 0,
            "summary": "This is a placeholder summary."
        }
        return jsonify(dummy_response), 200

# Helper function for CORS preflight responses
def _build_cors_preflight_response():
    response = jsonify(success=True)
    # These headers are important for CORS preflight
    response.headers.add("Access-Control-Allow-Origin", "https://vespaacademy.knack.com")
    response.headers.add('Access-Control-Allow-Headers', "Content-Type,Authorization")
    response.headers.add('Access-Control-Allow-Methods', "GET,PUT,POST,DELETE,OPTIONS")
    app.logger.info("Built CORS preflight response.")
    return response

# Basic health check endpoint
@app.route('/', methods=['GET'])
@app.route('/health', methods=['GET'])
def health_check():
    app.logger.info("Health check endpoint was hit.")
    return jsonify({"status": "Healthy", "message": "Student Coach Backend is running!"}), 200

if __name__ == '__main__':
    # For local development. Heroku uses Procfile.
    port = int(os.environ.get('PORT', 5002)) # Use a different port than tutor coach if running locally
    app.run(debug=True, port=port, host='0.0.0.0') 