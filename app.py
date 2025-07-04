import os
import json
from flask import Flask, jsonify, request
from flask_cors import CORS
from dotenv import load_dotenv
import logging
import requests # For Knack API calls
import time # For cache expiry
from datetime import datetime # For timestamp parsing
import openai # For LLM integration
import re # For keyword extraction and special message handling

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

# --- Helper function to extract qualification details (ported from tutorapp.py) ---
def extract_qual_details(exam_type_str, normalized_qual_type, app_logger_instance):
    """Extracts specific details (like year, size) from an exam_type_str based on its normalized type."""
    if not exam_type_str or not normalized_qual_type:
        app_logger_instance.debug(f"extract_qual_details: exam_type_str ('{exam_type_str}') or normalized_qual_type ('{normalized_qual_type}') is missing.")
        return None
    
    lower_exam_type = str(exam_type_str).lower()
    details = {}

    if normalized_qual_type == "IB HL":
        details['ib_level'] = "HL"
        return details
    if normalized_qual_type == "IB SL":
        details['ib_level'] = "SL"
        return details

    if "BTEC" in normalized_qual_type:
        if "2010" in lower_exam_type: details['year'] = "2010"
        elif "2016" in lower_exam_type: details['year'] = "2016"
        else:
            details['year'] = "2016" # Default BTEC year if not specified
            app_logger_instance.info(f"BTEC year not specified in '{exam_type_str}', defaulting to {details['year']} for MEG lookup.")
        
        # Determine BTEC size based on normalized type
        if normalized_qual_type == "BTEC Level 3 Extended Diploma": details['size'] = "EXTDIP"
        elif normalized_qual_type == "BTEC Level 3 Diploma": details['size'] = "DIP"
        elif normalized_qual_type == "BTEC Level 3 Subsidiary Diploma": details['size'] = "SUBDIP"
        elif normalized_qual_type == "BTEC Level 3 Extended Certificate": # This is often the default "BTEC Level 3"
            # Size for "Extended Certificate" can depend on the year for some ALPS tables
            if details['year'] == "2010":
                # For 2010, an "Extended Certificate" might be referred to as just "Certificate" in ALPS bands
                details['size'] = "CERT" 
            else: # For 2016, it's usually "EXTCERT"
                details['size'] = "EXTCERT"
        elif "foundation diploma" in lower_exam_type : details['size'] = "FOUNDDIP"
        elif "90 credit diploma" in lower_exam_type or "90cr" in lower_exam_type : details['size'] = "NINETY_CR"
        # Add other BTEC size mappings if necessary based on your ALPS band JSON keys
        
        if not details.get('size'):
             app_logger_instance.warning(f"Could not determine BTEC size for MEG key from '{exam_type_str}' (Normalized: '{normalized_qual_type}'). MEG lookup might fail.")
        return details

    if "Pre-U" in normalized_qual_type:
        if normalized_qual_type == "Pre-U Principal Subject": details['pre_u_type'] = "FULL"
        elif normalized_qual_type == "Pre-U Short Course": details['pre_u_type'] = "SC"
        return details

    if "WJEC" in normalized_qual_type:
        if normalized_qual_type == "WJEC Level 3 Diploma": details['wjec_size'] = "DIP"
        elif normalized_qual_type == "WJEC Level 3 Certificate": details['wjec_size'] = "CERT"
        else: # Default if not clearly diploma or certificate but identified as WJEC
            details['wjec_size'] = "CERT" 
            app_logger_instance.info(f"WJEC size not clearly diploma/certificate from '{normalized_qual_type}', defaulting to CERT for MEG lookup.")
        return details
    
    # No specific details needed for A-Level, AS-Level, UAL, CACHE for this function as per tutorapp.py structure
    # If they were needed (e.g. UAL Diploma vs ExtDip affecting MEG key), they would be added here.
    app_logger_instance.debug(f"No specific details extracted for '{normalized_qual_type}' from '{exam_type_str}'.")
    return None # Return None if no specific details are extracted for the given type

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
    app.logger.info(f"Fetching Object_29 questionnaire data for Object_10 ID: {object10_id}, Cycle: {cycle_number}")
    filters = [
        {'field': 'field_792', 'operator': 'is', 'value': object10_id}, # Connection to Object_10
        {'field': 'field_863_raw', 'operator': 'is', 'value': str(cycle_number)} # Cycle number
    ]
    response = get_knack_record("object_29", filters=filters)
    if response and response.get('records'):
        if len(response['records']) > 1:
            app.logger.warning(f"Multiple Object_29 records found for Object_10 ID {object10_id}, cycle {cycle_number}. Using the first one.")
        return response['records'][0] # Assuming one Object_29 per student per cycle
    app.logger.warning(f"No Object_29 data found for Object_10 ID {object10_id}, Cycle {cycle_number}.")
    return None

# --- Ported Academic Profile Functions from tutorapp.py ---

# Helper function to parse subjects from a given academic_profile_record (ported from tutorapp.py)
def parse_subjects_from_profile_record(academic_profile_record, app_logger_instance):
    if not academic_profile_record:
        app_logger_instance.error("parse_subjects_from_profile_record called with no record.")
        return [] 

    app_logger_instance.info(f"Parsing subjects for Object_112 record ID: {academic_profile_record.get('id')}. Record (first 500 chars): {str(academic_profile_record)[:500]}")
    subjects_summary = []
    # Subject fields are field_3080 (Sub1) to field_3094 (Sub15) in tutorapp, assuming same for student view if Obj112 is shared
    for i in range(1, 16):
        field_id_subject_json = f"field_30{79+i}" # field_3080 to field_3094
        subject_json_str = academic_profile_record.get(field_id_subject_json)
        if subject_json_str is None:
            subject_json_str = academic_profile_record.get(f"{field_id_subject_json}_raw")

        app_logger_instance.debug(f"For Obj112 ID {academic_profile_record.get('id')}, field {field_id_subject_json}: Data type: {type(subject_json_str)}, Content (brief): '{str(subject_json_str)[:100]}...'")
        
        if subject_json_str and isinstance(subject_json_str, str) and subject_json_str.strip().startswith('{'):
            app_logger_instance.info(f"Attempting to parse JSON for {field_id_subject_json}: '{subject_json_str[:200]}...'")
            try:
                subject_data = json.loads(subject_json_str)
                app_logger_instance.info(f"Parsed subject_data for {field_id_subject_json}: {subject_data}")
                summary_entry = {
                    "subject": subject_data.get("subject") or subject_data.get("subject_name") or subject_data.get("subjectName") or subject_data.get("name", "N/A"),
                    "currentGrade": subject_data.get("currentGrade") or subject_data.get("current_grade") or subject_data.get("cg") or subject_data.get("currentgrade", "N/A"),
                    "targetGrade": subject_data.get("targetGrade") or subject_data.get("target_grade") or subject_data.get("tg") or subject_data.get("targetgrade", "N/A"),
                    "effortGrade": subject_data.get("effortGrade") or subject_data.get("effort_grade") or subject_data.get("eg") or subject_data.get("effortgrade", "N/A"),
                    "examType": subject_data.get("examType") or subject_data.get("exam_type") or subject_data.get("qualificationType", "N/A")
                }
                if summary_entry["subject"] != "N/A" and summary_entry["subject"] is not None:
                    subjects_summary.append(summary_entry)
                    app_logger_instance.debug(f"Added subject: {summary_entry['subject']}")
                else:
                    app_logger_instance.info(f"Skipped adding subject for {field_id_subject_json} as subject name was invalid or N/A. Parsed data: {subject_data}")
            except json.JSONDecodeError as e:
                app_logger_instance.warning(f"JSONDecodeError for {field_id_subject_json}: {e}. Content: '{subject_json_str[:100]}...'")
        elif subject_json_str:
            app_logger_instance.info(f"Field {field_id_subject_json} was not empty but not a valid JSON string start: '{subject_json_str[:100]}...'")

    if not subjects_summary:
        app_logger_instance.info(f"No valid subject JSONs parsed from Object_112 record {academic_profile_record.get('id')}. Returning default message list.")
        return [{"subject": "No academic subjects parsed from profile.", "currentGrade": "N/A", "targetGrade": "N/A", "effortGrade": "N/A", "examType": "N/A"}]
    
    app_logger_instance.info(f"Successfully parsed {len(subjects_summary)} subjects from Object_112 record {academic_profile_record.get('id')}.")
    return subjects_summary

# Function to fetch Academic Profile (Object_112) - (ported from tutorapp.py)
def get_academic_profile(actual_student_obj3_id, student_name_for_fallback, app_logger_instance, student_obj10_id_log_ref="N/A"):
    app_logger_instance.info(f"Starting academic profile fetch. Target Student's Object_3 ID: '{actual_student_obj3_id}', Fallback Name: '{student_name_for_fallback}', Original Obj10 ID for logging: {student_obj10_id_log_ref}.")
    
    academic_profile_record = None
    subjects_summary = []

    # Attempt 1: Fetch Object_112 using actual_student_obj3_id against Object_112.field_3064 (UserId - Short Text field)
    if actual_student_obj3_id:
        app_logger_instance.info(f"Attempt 1: Fetching Object_112 where field_3064 (UserId Text) is '{actual_student_obj3_id}'.")
        filters_obj112_via_field3064 = [{'field': 'field_3064', 'operator': 'is', 'value': actual_student_obj3_id}]
        obj112_response_attempt1 = get_knack_record("object_112", filters=filters_obj112_via_field3064)

        temp_profiles_list_attempt1 = []
        if obj112_response_attempt1 and isinstance(obj112_response_attempt1, dict) and \
           'records' in obj112_response_attempt1 and isinstance(obj112_response_attempt1['records'], list):
            temp_profiles_list_attempt1 = obj112_response_attempt1['records']
            app_logger_instance.info(f"Attempt 1: Found {len(temp_profiles_list_attempt1)} candidate profiles via field_3064.")
        else:
            app_logger_instance.info(f"Attempt 1: Knack response for field_3064 query was not in expected format or no records. Response: {str(obj112_response_attempt1)[:200]}")

        if temp_profiles_list_attempt1: 
            if isinstance(temp_profiles_list_attempt1[0], dict):
                academic_profile_record = temp_profiles_list_attempt1[0]
                app_logger_instance.info(f"Attempt 1 SUCCESS: Found Object_112 record ID {academic_profile_record.get('id')} using field_3064 with Obj3 ID '{actual_student_obj3_id}'. Profile Name: {academic_profile_record.get('field_3066')}")
                subjects_summary = parse_subjects_from_profile_record(academic_profile_record, app_logger_instance)
                if not subjects_summary or (len(subjects_summary) == 1 and subjects_summary[0]["subject"].startswith("No academic subjects")):
                    app_logger_instance.info(f"Object_112 ID {academic_profile_record.get('id')} (via field_3064) yielded no valid subjects. Will try other methods.")
                    academic_profile_record = None 
                else:
                    app_logger_instance.info(f"Object_112 ID {academic_profile_record.get('id')} (via field_3064) has valid subjects. Using this profile.")
                    return {"subjects": subjects_summary, "profile_record": academic_profile_record}
            else:
                app_logger_instance.warning(f"Attempt 1: First item in profiles_via_field3064 is not a dict: {type(temp_profiles_list_attempt1[0])}")
        else:
            app_logger_instance.info(f"Attempt 1 FAILED (empty list): No Object_112 profile found where field_3064 (UserId Text) is '{actual_student_obj3_id}'.")

    # Attempt 2: Fetch Object_112 using actual_student_obj3_id against Object_112.field_3070 (Account Connection field)
    if not academic_profile_record and actual_student_obj3_id: 
        app_logger_instance.info(f"Attempt 2: Fetching Object_112 where field_3070 (Account Connection) is '{actual_student_obj3_id}'.")
        filters_obj112_via_field3070 = [{'field': 'field_3070_raw', 'operator': 'is', 'value': actual_student_obj3_id}]
        obj112_response_attempt2 = get_knack_record("object_112", filters=filters_obj112_via_field3070)
        
        temp_profiles_list_attempt2 = []
        if not (obj112_response_attempt2 and isinstance(obj112_response_attempt2, dict) and 'records' in obj112_response_attempt2 and isinstance(obj112_response_attempt2['records'], list) and obj112_response_attempt2['records']):
            app_logger_instance.info(f"Attempt 2 (field_3070_raw): No records or unexpected format. Trying 'field_3070' (non-raw). Response: {str(obj112_response_attempt2)[:200]}" )
            filters_obj112_via_field3070_alt = [{'field': 'field_3070', 'operator': 'is', 'value': actual_student_obj3_id}]
            obj112_response_attempt2 = get_knack_record("object_112", filters=filters_obj112_via_field3070_alt)

        if obj112_response_attempt2 and isinstance(obj112_response_attempt2, dict) and \
           'records' in obj112_response_attempt2 and isinstance(obj112_response_attempt2['records'], list):
            temp_profiles_list_attempt2 = obj112_response_attempt2['records']
            app_logger_instance.info(f"Attempt 2: Found {len(temp_profiles_list_attempt2)} candidate profiles via field_3070 logic.")

        if temp_profiles_list_attempt2: 
            if isinstance(temp_profiles_list_attempt2[0], dict):
                academic_profile_record = temp_profiles_list_attempt2[0]
                app_logger_instance.info(f"Attempt 2 SUCCESS: Found Object_112 record ID {academic_profile_record.get('id')} using field_3070 (Account Connection) with Obj3 ID '{actual_student_obj3_id}'. Profile Name: {academic_profile_record.get('field_3066')}")
                subjects_summary = parse_subjects_from_profile_record(academic_profile_record, app_logger_instance)
                if not subjects_summary or (len(subjects_summary) == 1 and subjects_summary[0]["subject"].startswith("No academic subjects")):
                    app_logger_instance.info(f"Object_112 ID {academic_profile_record.get('id')} (via field_3070) yielded no valid subjects. Will try name fallback.")
                    academic_profile_record = None 
                else:
                    app_logger_instance.info(f"Object_112 ID {academic_profile_record.get('id')} (via field_3070) has valid subjects. Using this profile.")
                    return {"subjects": subjects_summary, "profile_record": academic_profile_record}
            else:
                app_logger_instance.warning(f"Attempt 2: First item in profiles_via_field3070 is not a dict: {type(temp_profiles_list_attempt2[0])}")
        else:
            app_logger_instance.info(f"Attempt 2 FAILED (empty list): No Object_112 profile found where field_3070 (Account Connection) is '{actual_student_obj3_id}'.")

    # Attempt 3: Fallback to fetch by student name
    if not academic_profile_record and student_name_for_fallback and student_name_for_fallback != "N/A":
        app_logger_instance.info(f"Attempt 3: Fallback search for Object_112 by student name ('{student_name_for_fallback}') via field_3066.")
        filters_object112_name = [{'field': 'field_3066', 'operator': 'is', 'value': student_name_for_fallback}]
        obj112_response_attempt3 = get_knack_record("object_112", filters=filters_object112_name)
        
        temp_profiles_list_attempt3 = []
        if obj112_response_attempt3 and isinstance(obj112_response_attempt3, dict) and \
           'records' in obj112_response_attempt3 and isinstance(obj112_response_attempt3['records'], list):
            temp_profiles_list_attempt3 = obj112_response_attempt3['records']
            app_logger_instance.info(f"Attempt 3: Found {len(temp_profiles_list_attempt3)} candidate profiles via name fallback.")

        if temp_profiles_list_attempt3: 
            if isinstance(temp_profiles_list_attempt3[0], dict):
                academic_profile_record = temp_profiles_list_attempt3[0]
                app_logger_instance.info(f"Attempt 3 SUCCESS: Found Object_112 record ID {academic_profile_record.get('id')} via NAME fallback ('{student_name_for_fallback}'). Profile Name: {academic_profile_record.get('field_3066')}")
                subjects_summary = parse_subjects_from_profile_record(academic_profile_record, app_logger_instance)
                if not subjects_summary or (len(subjects_summary) == 1 and subjects_summary[0]["subject"].startswith("No academic subjects")):
                    app_logger_instance.info(f"Object_112 ID {academic_profile_record.get('id')} (via name fallback) yielded no valid subjects.")
                    # Fall through to default return
                else:
                    app_logger_instance.info(f"Object_112 ID {academic_profile_record.get('id')} (via name fallback) has valid subjects. Using this profile.")
                    return {"subjects": subjects_summary, "profile_record": academic_profile_record}
            else:
                app_logger_instance.warning(f"Attempt 3: First item in homepage_profiles_name_search is not a dict: {type(temp_profiles_list_attempt3[0])}")
        else:
            app_logger_instance.warning(f"Attempt 3 FAILED (empty list): Fallback search: No Object_112 found for student name: '{student_name_for_fallback}'.")
    
    app_logger_instance.warning(f"All attempts to fetch Object_112 failed (Student's Obj3 ID: '{actual_student_obj3_id}', Fallback name: '{student_name_for_fallback}').")
    default_subjects = [{"subject": "Academic profile not found by any method.", "currentGrade": "N/A", "targetGrade": "N/A", "effortGrade": "N/A", "examType": "N/A"}]
    return {"subjects": default_subjects, "profile_record": None}

# --- Data Processing Helper (Simplified for now) ---
def get_score_profile_text(score_value):
    """Maps a VESPA score to a qualitative category like High, Medium, Low, Very Low."""
    if score_value is None: return "N/A"
    try:
        score = float(score_value)
        if score >= 8: return "High"
        if score >= 6: return "Medium"
        if score >= 4: return "Low"
        if score >= 0: return "Very Low" # Catches 0, 1, 2, 3
        return "N/A" # Should not be reached if score is a number
    except (ValueError, TypeError):
        app.logger.debug(f"get_score_profile_text: Could not convert score '{score_value}' to float.")
        return "N/A"


# --- NEW: Add comprehensive data processing functions ---

def get_all_knack_records(object_key, filters=None, max_pages=20):
    """Fetches all records from a Knack object using pagination."""
    all_records = []
    current_page = 1
    total_pages = 1

    app.logger.info(f"Starting paginated fetch for {object_key} with filters: {filters}")

    while current_page <= total_pages and current_page <= max_pages:
        app.logger.info(f"Fetching page {current_page} for {object_key}...")
        response_data = get_knack_record(object_key, filters=filters, page=current_page, rows_per_page=1000)
        
        if response_data and isinstance(response_data, dict):
            records_on_page = response_data.get('records', [])
            
            if isinstance(records_on_page, list):
                all_records.extend(records_on_page)
                app.logger.info(f"Fetched {len(records_on_page)} records from page {current_page} for {object_key}. Total so far: {len(all_records)}.")
            else:
                app.logger.warning(f"'records' key in response_data for {object_key} page {current_page} is not a list. Type: {type(records_on_page)}. Stopping pagination.")
                break
            
            new_total_pages = response_data.get('total_pages')
            if new_total_pages is not None:
                try:
                    total_pages = int(new_total_pages)
                    if current_page == 1:
                        app.logger.info(f"Total pages for {object_key} identified from API: {total_pages}")
                except (ValueError, TypeError):
                    app.logger.warning(f"Could not parse 'total_pages' ('{new_total_pages}') from response for {object_key} on page {current_page}.")
            
            if not records_on_page or len(records_on_page) < 1000 or current_page >= total_pages:
                app.logger.info(f"Last page likely reached for {object_key} on page {current_page}.")
                break
            current_page += 1
        else:
            app.logger.warning(f"No response_data or unexpected format on page {current_page} for {object_key}. Stopping pagination.")
            break
            
    app.logger.info(f"Completed paginated fetch for {object_key}. Total records retrieved: {len(all_records)}.")
    return all_records

def get_school_vespa_averages(school_id):
    """Calculate average VESPA scores for all students in a school."""
    if not school_id:
        app.logger.warning("get_school_vespa_averages called with no school_id.")
        return None

    app.logger.info(f"Calculating school VESPA averages for school_id: {school_id}")
    
    # Use the correct filter from tutor app.py - field_133 is the school connection
    filters_primary = [{'field': 'field_133', 'operator': 'is', 'value': school_id}]
    app.logger.info(f"Attempting to fetch all records for object_10 with primary filter: {filters_primary}")
    
    all_student_records_for_school = get_all_knack_records("object_10", filters=filters_primary)

    if not all_student_records_for_school:
        app.logger.warning(f"No student records found for school_id {school_id} using primary filter (field_133). Trying fallback filter (field_133_raw).")
        filters_fallback = [{'field': 'field_133_raw', 'operator': 'contains', 'value': school_id}]
        app.logger.info(f"Attempting to fetch all records for object_10 with fallback filter: {filters_fallback}")
        all_student_records_for_school = get_all_knack_records("object_10", filters=filters_fallback)
        
        if not all_student_records_for_school:
            app.logger.error(f"Could not retrieve any student records for school_id: {school_id} using primary or fallback filters. Cannot calculate averages.")
            return None
        app.logger.info(f"Retrieved {len(all_student_records_for_school)} student records for school_id {school_id} using fallback filter (field_133_raw).")
    else:
        app.logger.info(f"Retrieved {len(all_student_records_for_school)} student records for school_id {school_id} using primary filter (field_133).")
    
    vespa_elements = {
        "Vision": "field_147", "Effort": "field_148",
        "Systems": "field_149", "Practice": "field_150",
        "Attitude": "field_151", "Overall": "field_152",
    }
    sums = {key: 0 for key in vespa_elements}
    counts = {key: 0 for key in vespa_elements}

    # Now all_student_records_for_school is a flat list of student record dictionaries
    for record in all_student_records_for_school:
        # Ensure record is a dictionary before trying to .get() from it
        if not isinstance(record, dict):
            app.logger.warning(f"Skipping an item in all_student_records_for_school because it is not a dictionary: {type(record)} - Content: {str(record)[:100]}...")
            continue
            
        for element_name, field_key in vespa_elements.items():
            score_value = record.get(field_key)
            if score_value is not None:
                try:
                    score = float(score_value)
                    sums[element_name] += score
                    counts[element_name] += 1
                except (ValueError, TypeError):
                    app.logger.debug(f"Could not convert score '{score_value}' for {element_name} in record {record.get('id', 'N/A')} to float.")
    
    averages = {}
    for element_name in vespa_elements:
        if counts[element_name] > 0:
            averages[element_name] = round(sums[element_name] / counts[element_name], 2)
        else:
            averages[element_name] = 0
    
    app.logger.info(f"Calculated school VESPA averages for school_id {school_id}: {averages}")
    return averages

def normalize_qualification_type(exam_type_str):
    """Normalize qualification type strings to standard format."""
    if not exam_type_str:
        return "Unknown"
    
    exam_type_str = str(exam_type_str).strip()
    
    # A-Level variations
    if any(x in exam_type_str.upper() for x in ['A LEVEL', 'A-LEVEL', 'A2', 'ALEVEL']):
        return "A Level"
    
    # AS Level
    if 'AS LEVEL' in exam_type_str.upper() or 'AS-LEVEL' in exam_type_str.upper():
        return "AS Level"
    
    # IB
    if 'IB HL' in exam_type_str.upper() or 'INTERNATIONAL BACCALAUREATE HL' in exam_type_str.upper():
        return "IB HL"
    if 'IB SL' in exam_type_str.upper() or 'INTERNATIONAL BACCALAUREATE SL' in exam_type_str.upper():
        return "IB SL"
    
    # BTEC
    if 'BTEC' in exam_type_str.upper():
        if 'EXTENDED DIPLOMA' in exam_type_str.upper():
            return "BTEC Level 3 Extended Diploma"
        elif 'DIPLOMA' in exam_type_str.upper() and 'EXTENDED' not in exam_type_str.upper():
            return "BTEC Level 3 Diploma"
        elif 'SUBSIDIARY' in exam_type_str.upper():
            return "BTEC Level 3 Subsidiary Diploma"
        elif 'CERTIFICATE' in exam_type_str.upper():
            return "BTEC Level 3 Extended Certificate"
        else:
            return "BTEC Level 3"
    
    # Pre-U
    if 'PRE-U' in exam_type_str.upper() or 'PRE U' in exam_type_str.upper():
        if 'SHORT' in exam_type_str.upper():
            return "Pre-U Short Course"
        else:
            return "Pre-U Principal Subject"
    
    # UAL
    if 'UAL' in exam_type_str.upper():
        if 'EXTENDED' in exam_type_str.upper():
            return "UAL Level 3 Extended Diploma"
        elif 'DIPLOMA' in exam_type_str.upper():
            return "UAL Level 3 Diploma"
        else:
            return "UAL Level 3"
    
    # CACHE
    if 'CACHE' in exam_type_str.upper():
        if 'EXTENDED' in exam_type_str.upper():
            return "CACHE Level 3 Extended Diploma"
        elif 'DIPLOMA' in exam_type_str.upper():
            return "CACHE Level 3 Diploma"
        elif 'CERTIFICATE' in exam_type_str.upper():
            return "CACHE Level 3 Certificate"
        elif 'AWARD' in exam_type_str.upper():
            return "CACHE Level 3 Award"
        else:
            return "CACHE Level 3"
    
    return exam_type_str  # Return original if no match

def get_points(grade, qualification_type):
    """Convert grade to UCAS points based on qualification type."""
    if not grade or grade == "N/A": # Removed check for grade_points_mapping_kb here, will check inside
        app.logger.warning(f"get_points: Invalid input - grade: {grade}, qual_type: {qualification_type}")
        return 0
    
    grade_cleaned = str(grade).strip().upper()
    normalized_qual = normalize_qualification_type(qualification_type)
    # app.logger.debug(f"get_points: Looking for grade '{grade_cleaned}' in qual '{normalized_qual}'")

    if not grade_points_mapping_kb:
        app.logger.error("get_points: grade_points_mapping_kb is not loaded.")
        return 0

    qual_specific_map = grade_points_mapping_kb.get(normalized_qual)
    
    if not qual_specific_map:
        # app.logger.warning(f"get_points: No grade point mapping found for qualification type: '{normalized_qual}'. Attempting A-Level fallback if applicable.")
        if normalized_qual == "A Level": # A-Level specific fallback if not in main map
            grade_to_points_fallback = {
                'A*': 56, 'A': 48, 'B': 40, 'C': 32, 'D': 24, 'E': 16, 'U': 0
            }
            points = grade_to_points_fallback.get(grade_cleaned)
            if points is not None:
                # app.logger.info(f"get_points: Used A-Level fallback for grade '{grade_cleaned}', points: {points}")
                return points
            else:
                # app.logger.warning(f"get_points: A-Level fallback also failed for grade '{grade_cleaned}'. Available fallback grades: {list(grade_to_points_fallback.keys())}")
                pass # Fall through to return 0 at the end
        # app.logger.warning(f"get_points: No mapping for '{normalized_qual}' and not an A-Level fallback case. Returning 0.")
        return 0

    points = qual_specific_map.get(grade_cleaned)
    
    # Handle common variations like "Dist*" vs "D*"
    if points is None:
        if grade_cleaned == "DIST*": points = qual_specific_map.get("D*")
        elif grade_cleaned == "DIST": points = qual_specific_map.get("D")
        elif grade_cleaned == "MERIT": points = qual_specific_map.get("M")
        elif grade_cleaned == "PASS": points = qual_specific_map.get("P")
        # Add other BTEC/vocational grade variations if necessary, e.g. D*D*, DD, MM etc.
        # For example, if grade_cleaned is "D*D*" and qual_specific_map has "D*D*", it will be found.

    if points is not None:
        # app.logger.debug(f"get_points: Found points: {points} for grade '{grade_cleaned}' in '{normalized_qual}'.")
        return int(points)
    else:
        # app.logger.warning(f"get_points: No points found for grade '{grade_cleaned}' (original: '{grade}') in qualification '{normalized_qual}'. Available grades in map: {list(qual_specific_map.keys()) if qual_specific_map else 'N/A'}. Returning 0 points.")
        pass # Added pass to satisfy indentation for the else block
    return 0

def get_meg_for_prior_attainment(prior_attainment_score, qualification_type, percentile=75):
    """Get MEG based on prior attainment score and qualification type."""
    if prior_attainment_score is None:
        app.logger.warning(f"get_meg_for_prior_attainment: prior_attainment_score is None for qual '{qualification_type}'.")
        return "N/A", 0

    try:
        score = float(prior_attainment_score)
    except (ValueError, TypeError):
        app.logger.warning(f"get_meg_for_prior_attainment: Could not convert prior_attainment_score '{prior_attainment_score}' to float.")
        return "N/A", 0
    
    normalized_qual = normalize_qualification_type(qualification_type)
    
    benchmark_table_data = None
    if normalized_qual == "A Level":
        if percentile == 60:
            benchmark_table_data = alps_bands_aLevel_60_kb
        elif percentile == 75:
            benchmark_table_data = alps_bands_aLevel_75_kb
        elif percentile == 90:
            benchmark_table_data = alps_bands_aLevel_90_kb
        elif percentile == 100:
            benchmark_table_data = alps_bands_aLevel_100_kb
        else:
            app.logger.warning(f"get_meg_for_prior_attainment: Unsupported percentile '{percentile}' for A-Level. Defaulting to 75th.")
            benchmark_table_data = alps_bands_aLevel_75_kb
    # Add logic for other qualification types here if they have specific percentile tables
    # For now, if not A-Level, benchmark_table_data remains None and will hit the next check.

    if not benchmark_table_data:
        app.logger.warning(f"get_meg_for_prior_attainment: No ALPS benchmark table data loaded or selected for qual '{normalized_qual}', percentile '{percentile}'.")
        # Consider a more generic fallback or error handling if needed for non-A-Level quals
        # For now, this will lead to returning "N/A", 0 if no table is found.
        # If other qual types (BTEC, IB etc.) should use a default table (e.g. alps_bands_aLevel_75_kb as a proxy)
        # that logic would go here. For this fix, we focus on A-Level path.
        # Example: if normalized_qual == "AS Level": benchmark_table_data = alps_bands_aLevel_75_kb # Using 75th as proxy
        # This function's scope is currently limited by the KBs loaded for the student app.
        # The tutor app has more comprehensive ALPS band loading and selection.
        # For now, if not A-Level or table is missing, it will proceed to the loop (which won't run if benchmark_table_data is None)
        # and then hit the final fallback.
        if normalized_qual != "A Level":
             app.logger.info(f"get_meg_for_prior_attainment: No specific ALPS percentile table logic for '{normalized_qual}'. Will use general fallback if score not in bands.")
             # Attempt to use a default like A-Level 75th for non-A-Levels if a generic lookup is desired.
             # This depends on the expectation for non-A-Level MEGs.
             # For now, it will pass through and use the final fallback if the loop doesn't match.

    if benchmark_table_data: # Only proceed if a table was selected/loaded
        for band_info in benchmark_table_data: # benchmark_table_data is a list of dicts
            min_score_val = None
            max_score_val = None
            # More robust key checking, similar to tutor app
            possible_min_keys = ["gcseMinScore", "gcseMin", "Avg GCSE score Min", "Prior Attainment Min", "lowerBound"]
            possible_max_keys = ["gcseMaxScore", "gcseMax", "Avg GCSE score Max", "Prior Attainment Max", "upperBound"]
            possible_meg_keys = ["megAspiration", "MEG Aspiration", "minimumGrade", "megGrade", "MEG"]

            for key in possible_min_keys:
                if key in band_info:
                    min_score_val = band_info[key]
                    break
            for key in possible_max_keys:
                if key in band_info:
                    max_score_val = band_info[key]
                    break
            
            meg_aspiration_grade = "N/A"
            for key in possible_meg_keys:
                if key in band_info:
                    meg_aspiration_grade = band_info[key]
                    break
            
            if min_score_val is not None:
                try:
                    min_s = float(min_score_val)
                    # Determine max_s for range check. Assume inclusive max if only min_s is defined (point score)
                    # or exclusive max if max_score_val is present (range).
                    # This needs to align with how ALPS defines their bands.
                    # Common ALPS tables are [min_score, max_score) - min inclusive, max exclusive
                    max_s_is_exclusive_upper_bound = max_score_val is not None
                    max_s = float(max_score_val) if max_score_val is not None else float('inf')
                    
                    in_band = False
                    if max_s_is_exclusive_upper_bound:
                        if score >= min_s and score < max_s:
                            in_band = True
                    else: # If max_score_val is not typical, could be point-based or inclusive upper
                        if score >= min_s and (max_score_val is None or score <= max_s): # Default to inclusive if max_s logic unsure
                             in_band = True

                    if in_band:
                        meg_points_val = get_points(meg_aspiration_grade, normalized_qual)
                        # app.logger.info(f"get_meg_for_prior_attainment: Found band for score {score}. MEG Grade: {meg_aspiration_grade}, Points: {meg_points_val}")
                        return meg_aspiration_grade, meg_points_val if meg_points_val is not None else 0
                except (ValueError, TypeError) as e_conv:
                    app.logger.warning(f"get_meg_for_prior_attainment: Error converting band scores for band {band_info}: {e_conv}")
                    continue
        app.logger.warning(f"get_meg_for_prior_attainment: Score {score} not in any band of the selected table for qual '{normalized_qual}', percentile '{percentile}'. Table (first 200 chars): {str(benchmark_table_data)[:200]}...")
    else: # If benchmark_table_data was None (e.g. missing KB or non-Alevel without specific table)
        app.logger.warning(f"get_meg_for_prior_attainment: No benchmark_table_data to process for qual '{normalized_qual}', percentile '{percentile}'.")

    # Fallback if score not in any band or no table was processed
    default_grade_fallback = "N/A" 
    default_points_fallback = 0 # get_points for "N/A" should yield 0 with the updated get_points
    app.logger.warning(f"get_meg_for_prior_attainment: Using fallback MEG '{default_grade_fallback}' ({default_points_fallback} pts) for PA {score}, Qual '{normalized_qual}', Pctl '{percentile}'.")
    return default_grade_fallback, default_points_fallback

# Load additional ALPS KBs if available
alps_bands_aLevel_60_kb = load_json_file('alpsBands_aLevel_60.json')
alps_bands_aLevel_90_kb = load_json_file('alpsBands_aLevel_90.json')
alps_bands_aLevel_100_kb = load_json_file('alpsBands_aLevel_100.json')

# --- LLM Integration for Student Insights (adapted from tutorapp.py) ---
def generate_student_insights_with_llm(student_data_dict, app_logger_instance):
    """Generate personalized insights for students using OpenAI, adapted for student-facing content."""
    if not OPENAI_API_KEY:
        app.logger.warning("OpenAI API key not set. Returning placeholder insights.")
        # Return a structure consistent with what the frontend might expect, but with error messages
        return {
            "student_overview_summary": f"AI insights for {student_data_dict.get('student_name', 'you')} are currently unavailable (AI not configured).",
            "chart_comparative_insights": "Insights unavailable (AI not configured).",
            "questionnaire_interpretation_and_reflection_summary": "Questionnaire interpretation unavailable (AI not configured).",
            "academic_benchmark_analysis": "Academic benchmark analysis unavailable (AI not configured).",
            "suggested_student_goals": ["Goal suggestions unavailable (AI not configured)."]
        }

    try:
        openai.api_key = OPENAI_API_KEY # Ensure openai object is used if you aliased it, e.g. client.api_key
        app_logger_instance.info(f"Attempting to generate LLM insights for student: {student_data_dict.get('student_name', 'N/A')}")

        student_name = student_data_dict.get('student_name', 'Student')
        student_level = student_data_dict.get('student_level', 'N/A') 
        current_cycle = student_data_dict.get('current_cycle', 'N/A')
        school_averages = student_data_dict.get('school_vespa_averages') 
        vespa_profile_for_rag = student_data_dict.get('vespa_profile', {}) 
        all_scored_questionnaire_statements = student_data_dict.get('all_scored_questionnaire_statements', [])

        prompt_parts = []
        prompt_parts.append(f"You are My VESPA AI Coach. I am '{student_name}'. This is my data:")
        prompt_parts.append(f"Current Cycle: {current_cycle}.")

        prompt_parts.append("\n--- My Current VESPA Profile (Vision, Effort, Systems, Practice, Attitude) ---")
        if student_data_dict.get('vespa_profile'):
            for element, details in student_data_dict['vespa_profile'].items():
                if element == "Overall": continue
                prompt_parts.append(f"- {element}: My score is {details.get('score_1_to_10', 'N/A')}/10, which is considered '{details.get('score_profile_text', 'N/A')}'.")

        if school_averages:
            prompt_parts.append("\n--- School's Average VESPA Scores (For Comparison) ---")
            for element, avg_score in school_averages.items():
                prompt_parts.append(f"- {element} (School Avg): {avg_score}/10")
        
        prompt_parts.append("\n--- My Academic Profile (First 3 Subjects with My Standard Expected Grade) ---")
        if student_data_dict.get('academic_profile_summary'):
            profile_data = student_data_dict['academic_profile_summary']
            valid_subjects_shown = 0
            if isinstance(profile_data, list) and profile_data and \
               not (profile_data[0].get('subject','').startswith("Academic profile not found")) and \
               not (profile_data[0].get('subject','').startswith("No academic subjects parsed")):
                for subject_info in profile_data[:3]:
                    if subject_info.get('subject') and subject_info.get('subject') != "N/A":
                        meg_text = f", My Standard Expected Grade (MEG): {subject_info.get('standard_meg', 'N/A')}" if subject_info.get('standard_meg') else ""
                        prompt_parts.append(f"- Subject: {subject_info.get('subject')}, My Current Grade: {subject_info.get('currentGrade', 'N/A')}, My Target: {subject_info.get('targetGrade', 'N/A')}{meg_text}")
                        valid_subjects_shown += 1
                if valid_subjects_shown == 0:
                    prompt_parts.append("  It looks like my detailed subject information isn't available right now.")        
            else:
                prompt_parts.append("  My detailed academic profile summary isn't available at the moment.")
        
        if student_data_dict.get('academic_megs'):
            meg_data = student_data_dict['academic_megs']
            prompt_parts.append("\n--- My Academic Benchmarks (Based on My Prior Attainment) ---")
            prompt_parts.append(f"  My GCSE Prior Attainment Score: {meg_data.get('prior_attainment_score', 'N/A')}")
            if meg_data.get('aLevel_meg_grade_75th') and meg_data.get('aLevel_meg_grade_75th') != "N/A":
                 prompt_parts.append(f"  For A-Levels, students with similar prior scores typically achieve around a grade '{meg_data.get('aLevel_meg_grade_75th')}' (this is the standard MEG or top 25% benchmark).")

        prompt_parts.append("\n--- My Reflections & Goals (Current Cycle) ---")
        reflections_goals_found_student = False
        current_rrc_text_student = "Not specified"
        current_goal_text_student = "Not specified"
        if student_data_dict.get('student_reflections_and_goals'):
            reflections = student_data_dict['student_reflections_and_goals']
            current_rrc_key_student = f"rrc{current_cycle}_comment"
            current_goal_key_student = f"goal{current_cycle}"
            rrc_val = reflections.get(current_rrc_key_student)
            goal_val = reflections.get(current_goal_key_student)

            if rrc_val and rrc_val != "Not specified":
                current_rrc_text_student = str(rrc_val)[:300].replace('\n', ' ')
                prompt_parts.append(f"- My Current Reflection (RRC{current_cycle}): {current_rrc_text_student}...")
                reflections_goals_found_student = True
            if goal_val and goal_val != "Not specified":
                current_goal_text_student = str(goal_val)[:300].replace('\n', ' ')
                prompt_parts.append(f"- My Current Goal (Goal {current_cycle}): {current_goal_text_student}...")
                reflections_goals_found_student = True
        if not reflections_goals_found_student:
            prompt_parts.append("  I haven't specified any reflections or goals for the current cycle, or they are not available.")

        prompt_parts.append("\n--- My Key Questionnaire Insights (My Top & Bottom Scoring Statements) ---")
        obj29_highlights = student_data_dict.get("object29_question_highlights")
        if obj29_highlights:
            if obj29_highlights.get("top_3") and obj29_highlights["top_3"]:
                prompt_parts.append("  Statements I Most Agreed With (1-5 scale, 5=Strongly Agree):")
                for q_data in obj29_highlights["top_3"]:
                    prompt_parts.append(f"    - Score {q_data.get('score', 'N/A')}/5 ({q_data.get('category', 'N/A')}): \"{q_data.get('text', 'N/A')}\"")
            if obj29_highlights.get("bottom_3") and obj29_highlights["bottom_3"]:
                prompt_parts.append("  Statements I Least Agreed With (Areas to think about):")
                for q_data in obj29_highlights["bottom_3"]:
                    prompt_parts.append(f"    - Score {q_data['score']}/5 ({q_data['category']}): \"{q_data['text']}\"")
        else:
            prompt_parts.append("  My top/bottom questionnaire statement highlights are not available.")

        # Overall Questionnaire Statement Response Distribution (Student view)
        prompt_parts.append("\n--- My Overall Questionnaire Statement Response Distribution ---")
        if all_scored_questionnaire_statements and isinstance(all_scored_questionnaire_statements, list):
            response_counts = {1: 0, 2: 0, 3: 0, 4: 0, 5: 0}
            for q_data in all_scored_questionnaire_statements:
                score = q_data.get("score")
                if score in response_counts: response_counts[score] += 1
            prompt_parts.append(f"  - Statements I rated '1' (e.g., Strongly Disagree): {response_counts[1]}")
            prompt_parts.append(f"  - Statements I rated '2': {response_counts[2]}")
            prompt_parts.append(f"  - Statements I rated '3': {response_counts[3]}")
            prompt_parts.append(f"  - Statements I rated '4': {response_counts[4]}")
            prompt_parts.append(f"  - Statements I rated '5' (e.g., Strongly Agree): {response_counts[5]}")
        else:
            prompt_parts.append("  My detailed questionnaire response distribution data is not available.")

        # --- TASKS FOR THE AI (Student View) ---
        prompt_parts.append("\n\n--- Coach, please help me with these things: ---")
        prompt_parts.append("Based ONLY on my data provided above, please provide the following insights FOR ME ('{student_name}').")
        prompt_parts.append("Your tone should be encouraging, supportive, and help me understand myself better. Give me practical, actionable advice. Subtly draw upon general coaching principles and insights related to mindset, goal-setting, self-reflection, and VESPA elements when formulating your responses, especially for the questionnaire analysis and overview. Frame suggestions as reflective points for me.")
        prompt_parts.append("Please format your entire response as a single JSON object with the following EXACT keys: \"student_overview_summary\", \"chart_comparative_insights\", \"questionnaire_interpretation_and_reflection_summary\", \"academic_benchmark_analysis\", \"suggested_student_goals\", \"academic_quote\", \"academic_performance_ai_summary\".")
        prompt_parts.append("Ensure all string values within the JSON are properly escaped.")
        
        # --- RAG Elements for student prompt (Simplified for now, can be expanded) ---
        # This section is less about the tutor's KB and more about general advice based on lowest VESPA or similar
        lowest_vespa_element_student = None
        lowest_score_student = 11 
        if vespa_profile_for_rag:
            for element, details in vespa_profile_for_rag.items():
                if element == "Overall": continue
                try:
                    score = float(details.get('score_1_to_10', 10))
                    if score < lowest_score_student:
                        lowest_score_student = score
                        lowest_vespa_element_student = element
                except (ValueError, TypeError): pass

        if lowest_vespa_element_student:
            prompt_parts.append("\n\n--- Some Ideas to Consider ---")
            prompt_parts.append(f"My lowest VESPA score seems to be in '{lowest_vespa_element_student}'. Can you give me some general tips or reflective questions for this area, and perhaps suggest a simple, actionable goal related to it? You can use the general reflective statements and coaching insights from your knowledge base for inspiration.")
            # We don't directly inject KB content into student prompt like we do for tutor, 
            # but we ask the LLM to use its general knowledge inspired by such KBs.

        # --- Knowledge Base Excerpts (Student View - less direct, more for LLM's internal inspiration) ---
        # We won't show the student the raw KB excerpts like we did for the tutor.
        # Instead, the prompt will guide the LLM to use this type of knowledge implicitly.
        # Example: If coaching_kb and REFLECTIVE_STATEMENTS_DATA are globally available in this backend scope:
        relevant_coaching_insights = []
        if COACHING_INSIGHTS_DATA and isinstance(COACHING_INSIGHTS_DATA, list):
            # Attempt to find a few relevant insights based on keywords or student's lowest VESPA.
            # This is a simple keyword match; more advanced RAG could be used.
            keywords_from_student_data = [student_name.lower(), lowest_vespa_element_student.lower() if lowest_vespa_element_student else ""]
            if student_data_dict.get('student_reflections_and_goals'):
                rrc_text_for_kw = student_data_dict['student_reflections_and_goals'].get(f"rrc{current_cycle}_comment", "").lower()
                goal_text_for_kw = student_data_dict['student_reflections_and_goals'].get(f"goal{current_cycle}", "").lower()
                keywords_from_student_data.extend(rrc_text_for_kw.split()[:10]) # First 10 words
                keywords_from_student_data.extend(goal_text_for_kw.split()[:10])

            # Add keywords from top/bottom questionnaire statements if available
            if obj29_highlights:
                for q_data in obj29_highlights.get("top_3", []) + obj29_highlights.get("bottom_3", []):
                    keywords_from_student_data.extend(q_data.get('text', '').lower().split()[:5])


            # Filter out very common words to make keywords more meaningful for matching insights
            common_filter_words = {"i", "me", "my", "is", "a", "the", "and", "to", "of", "it", "in", "for", "on", "with", "as", "an", "at", "by", "you", "your", "what", "how", "help", "can", "some", "this", "that", "area", "areas", "score", "scores"}
            meaningful_keywords = [kw for kw in keywords_from_student_data if kw not in common_filter_words and len(kw) > 3]


            for insight in COACHING_INSIGHTS_DATA:
                insight_text_corpus = (
                    str(insight.get('name', '')).lower() + " " +
                    str(insight.get('description', '')).lower() + " " +
                    str(insight.get('implications_for_tutor', '')).lower() + " " +
                    " ".join(insight.get('keywords', [])).lower()
                )
                # Check if any of the student's meaningful keywords appear in the insight's text corpus
                if any(m_kw in insight_text_corpus for m_kw in meaningful_keywords):
                    if len(relevant_coaching_insights) < 3: # Limit to 3 for brevity in prompt
                        insight_summary_for_prompt = f"Insight: {insight.get('name')}. Focus: {insight.get('description')[:100]}..."
                        relevant_coaching_insights.append(insight_summary_for_prompt)
            
        if relevant_coaching_insights:
            prompt_parts.append("\n\n--- General Coaching Principles (For AI's Inspiration) ---")
            prompt_parts.append("Remember to draw inspiration from general coaching principles. For example, here are a few themes from your knowledge base that might be relevant to consider when interpreting my data and suggesting reflections (do not quote these directly, but use the underlying ideas):")
            for RAG_insight_summary in relevant_coaching_insights:
                prompt_parts.append(f"- {RAG_insight_summary}")
        
        if coaching_kb: # This KB is 'coaching_questions_knowledge_base.json'
            prompt_parts.append("\n(For the AI: You also have access to a coaching questions knowledge base. Use its principles to help formulate your advice and goal suggestions, aiming for reflective and empowering questions for me, '{student_name}'.)")
        if REFLECTIVE_STATEMENTS_DATA:
            prompt_parts.append("(For the AI: You also have access to a list of general reflective statements. These can inspire the tone and nature of the S.M.A.R.T. goals you suggest for me.)")


        # --- REQUIRED OUTPUT STRUCTURE (JSON Object - Student View) ---
        prompt_parts.append("\n\n--- REQUIRED OUTPUT STRUCTURE (JSON Object) ---")
        prompt_parts.append("Please provide your response as a single, valid JSON object. Example:")
        prompt_parts.append("'''") # Start of code block marker for prompt
        prompt_parts.append("{")
        prompt_parts.append("  \"student_overview_summary\": \"A concise 2-3 sentence AI Student Snapshot for me, '{student_name}', highlighting 1-2 of my key strengths and 1-2 primary areas for development, rooted in VESPA principles and drawing from general coaching themes. Max 100-120 words. Speak directly to me (e.g., 'Your data shows...', 'You could focus on...').\",")
        prompt_parts.append("  \"chart_comparative_insights\": \"A short paragraph (max 100 words) helping me understand my VESPA scores compared to the school averages (if provided). What could these differences or similarities mean for me? If a score is significantly different, suggest a brief reflective question for me based on general coaching principles related to that VESPA element (e.g., if 'Systems' is low, 'What's one small organizational change you could try?'). Use 'you' and 'your'.\",")
        prompt_parts.append("  \"questionnaire_interpretation_and_reflection_summary\": \"A concise summary (approx. 150-200 words) interpreting my overall questionnaire responses (e.g., my tendencies towards 'Strongly Disagree' or 'Strongly Agree', as indicated by the counts of 1s, 2s, etc.). Highlight any notable patterns, such as a concentration of low or high responses in specific VESPA elements (refer to my Top/Bottom scoring statements). Subtly connect these patterns to general coaching insights about mindset, self-reflection, or goal-setting (e.g., if responses suggest a fixed mindset, gently introduce the idea of growth without being preachy). Also, briefly compare and contrast these questionnaire insights with my own RRC/Goal comments (My RRC: '{RRC_COMMENT_PLACEHOLDER}', My Goal: '{GOAL_COMMENT_PLACEHOLDER}'), noting any consistencies or discrepancies that could be valuable for me to reflect on. Use 'you' and 'your'.\",")
        prompt_parts.append("  \"academic_benchmark_analysis\": \"A supportive and encouraging analysis (approx. 150-180 words) of my academic performance. Start by looking at my current grades in relation to my Subject Target Grades and my Standard Expected Grades (MEGs). Explain that MEGs show what students with similar prior GCSE scores typically achieve (top 25%) and are aspirational. Explain that my Subject Target Grade (STG) is a more nuanced target that considers subject difficulty. Emphasize that comparing my current grades, MEGs, and STGs should help me think about my progress, strengths, and potential next steps. The goal is to use this information to identify areas for support or challenge, always considering my broader context. Use 'you' and 'your'.\",")
        prompt_parts.append("  \"suggested_student_goals\": [\"Based on the analysis, and inspired by general reflective statements and coaching principles (e.g., focusing on an area for development from the questionnaire or VESPA profile), suggest 2-3 S.M.A.R.T. goals FOR ME, reframed to my context. Make them actionable and specific.\", \"Goal 2...\"],")
        prompt_parts.append("  \"academic_quote\": \"A short, inspirational or funny quote suitable for a student. e.g., 'The expert in anything was once a beginner.' or 'Why fall in love when you can fall asleep?'\",")
        prompt_parts.append("  \"academic_performance_ai_summary\": \"A kind, encouraging, and professional AI summary (like a helpful teacher, approx. 200-250 words) analyzing my academic profile. Discuss my subject benchmarks in relation to my MEGs. If I'm not meeting MEGs, be gentle and positive, focusing on growth and understanding. Highlight strengths and areas for development based on my subject performance. The tone should be positive and empowering, even when pointing out challenges. Reference the MEG explainer text that I will see, which describes MEGs as aspirational and STGs as more personalized. Use 'you' and 'your'.\"")
        prompt_parts.append("}")
        prompt_parts.append("'''") # End of code block marker

        # Prepare cleaned versions of current_rrc_text and current_goal_text for the prompt placeholder replacement
        cleaned_rrc_placeholder_student = current_rrc_text_student[:100].replace('\n', ' ').replace("'", "\\'").replace('"', '\\"')
        cleaned_goal_placeholder_student = current_goal_text_student[:100].replace('\n', ' ').replace("'", "\\'").replace('"', '\\"')
        prompt_parts.append(f"REMEMBER to replace RRC_COMMENT_PLACEHOLDER with: '{cleaned_rrc_placeholder_student}...' and GOAL_COMMENT_PLACEHOLDER with: '{cleaned_goal_placeholder_student}...' in your actual questionnaire_interpretation_and_reflection_summary output.")

        prompt_to_send = "\n".join(prompt_parts)
        # Substitute placeholders in the final prompt string
        prompt_to_send = prompt_to_send.replace("'{RRC_COMMENT_PLACEHOLDER}'", f"'{cleaned_rrc_placeholder_student}...'")
        prompt_to_send = prompt_to_send.replace("'{GOAL_COMMENT_PLACEHOLDER}'", f"'{cleaned_goal_placeholder_student}...'")

        app_logger_instance.info(f"Generated Student LLM Prompt (first 500 chars): {prompt_to_send[:500]}")
        app_logger_instance.info(f"Generated Student LLM Prompt (last 500 chars): {prompt_to_send[-500:]}")
        app_logger_instance.info(f"Total Student LLM Prompt length: {len(prompt_to_send)} characters")

        system_message_content = (
            f"You are My VESPA AI Coach, an AI assistant designed to help students understand their VESPA profile (Vision, Effort, Systems, Practice, Attitude) "
            f"and academic performance. Your responses should be encouraging, supportive, and provide clear, actionable advice directly to the student using 'you' and 'your'. "
            f"You are speaking to '{student_name}'. Help them reflect on their data and identify steps for improvement. Your output MUST be a single JSON object with specific keys."
        )

        max_retries = 2
        for attempt in range(max_retries):
            try:
                # Using openai.chat.completions.create for newer OpenAI library versions
                response = openai.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[
                        {"role": "system", "content": system_message_content},
                        {"role": "user", "content": prompt_to_send}
                    ],
                    max_tokens=1200, # Adjusted for potentially detailed student-facing JSON, increased slightly
                    temperature=0.65, # Slightly higher for more nuanced and less wooden student advice
                    n=1,
                    stop=None,
                    response_format={"type": "json_object"} # Request JSON output
                )
                
                raw_response_content = response.choices[0].message.content.strip()
                app_logger_instance.info(f"Student LLM raw response: {raw_response_content}")

                parsed_llm_outputs = json.loads(raw_response_content)
                
                # Validate expected keys for student response
                expected_keys_student = [
                    "student_overview_summary", 
                    "chart_comparative_insights", 
                    "questionnaire_interpretation_and_reflection_summary", 
                    "academic_benchmark_analysis", 
                    "suggested_student_goals",
                    "academic_quote",
                    "academic_performance_ai_summary"
                ]
                # Fill missing keys with error messages if LLM doesn't provide them
                all_keys_present = True
                for key in expected_keys_student:
                    if key not in parsed_llm_outputs:
                        all_keys_present = False
                        parsed_llm_outputs[key] = f"Error: AI response for '{key}' was not provided."
                if not all_keys_present:
                    app_logger_instance.warning(f"Student LLM response missing one or more expected keys. Filled with defaults. Response: {raw_response_content}")
                
                app_logger_instance.info(f"Student LLM generated structured data: {parsed_llm_outputs}")
                return parsed_llm_outputs

            except json.JSONDecodeError as e_json:
                app_logger_instance.error(f"JSONDecodeError from Student LLM response (Attempt {attempt + 1}/{max_retries}): {e_json}")
                app_logger_instance.error(f"Problematic Student LLM response content: {raw_response_content}")
                if attempt == max_retries - 1:
                    return {key: f"Error parsing AI response for {key} after multiple attempts." for key in expected_keys_student}
            except Exception as e_general:
                app_logger_instance.error(f"Error calling OpenAI API or processing response for student (Attempt {attempt + 1}/{max_retries}): {e_general}")
                if attempt == max_retries - 1:
                     return {key: f"Error generating insights from AI for {key}. (Details: {str(e_general)[:50]}...)" for key in expected_keys_student}
            time.sleep(1) # Wait before retrying

        # Fallback if all retries fail
        app_logger_instance.error("Student LLM processing failed after all retries.")
        return {key: "Critical error: AI processing failed after all retries." for key in expected_keys_student}

    except Exception as e_outer:
        app_logger_instance.error(f"Outer exception in generate_student_insights_with_llm: {e_outer}")
        return {
            "student_overview_summary": "An unexpected error occurred while generating AI insights.",
            "chart_comparative_insights": "Insights unavailable due to an error.",
            "questionnaire_interpretation_and_reflection_summary": "Questionnaire interpretation unavailable due to an error.",
            "academic_benchmark_analysis": "Academic benchmark analysis unavailable due to an error.",
            "suggested_student_goals": ["Goal suggestions unavailable due to an error."],
            "academic_quote": "Quote unavailable due to an error.",
            "academic_performance_ai_summary": "Personalized academic summary unavailable due to an error."
        }

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

            # Correctly get student name from field_69 (Name field in Object_3)
            name_data_raw = student_user_data.get('field_69_raw')
            name_data_obj = student_user_data.get('field_69')

            if isinstance(name_data_raw, dict) and name_data_raw.get('full'):
                student_name_from_obj3 = name_data_raw['full']
            elif isinstance(name_data_obj, dict) and name_data_obj.get('full'):
                student_name_from_obj3 = name_data_obj['full']
            elif isinstance(name_data_raw, dict): # Fallback if 'full' isn't present but it's a dict
                first = name_data_raw.get('first', '')
                last = name_data_raw.get('last', '')
                title = name_data_raw.get('title', '')
                constructed_name = f"{title} {first} {last}".replace('  ', ' ').strip()
                if constructed_name and constructed_name != title: # Ensure some name was actually built
                    student_name_from_obj3 = constructed_name
            elif isinstance(name_data_obj, dict): # Fallback for object version
                first = name_data_obj.get('first', '')
                last = name_data_obj.get('last', '')
                title = name_data_obj.get('title', '')
                constructed_name = f"{title} {first} {last}".replace('  ', ' ').strip()
                if constructed_name and constructed_name != title:
                    student_name_from_obj3 = constructed_name
            # If it's a direct string (less common for Knack name fields but possible)
            elif isinstance(name_data_raw, str) and name_data_raw.strip():
                student_name_from_obj3 = name_data_raw.strip()
            elif isinstance(name_data_obj, str) and name_data_obj.strip():
                student_name_from_obj3 = name_data_obj.strip()
        else:
            app.logger.warning(f"Could not fetch Object_3 details for ID {student_object3_id}")
            # Return error or limited dummy if core student info fails
            return jsonify({"error": f"Could not retrieve user details for {student_object3_id}"}), 404

        # 2. Fetch Student's VESPA Profile (Object_10)
        object10_data = get_student_object10_record(student_email) if student_email else None
        current_cycle = 0
        vespa_scores_for_profile = {}
        student_reflections = {}
        school_id = None
        school_vespa_averages = None
        student_level_raw = "N/A" # For educational level
        
        if object10_data:
            student_level_raw = object10_data.get("field_568_raw", "N/A")
            current_cycle_str = object10_data.get("field_146_raw", "0")
            # Ensure current_cycle_str is treated as a string before isdigit()
            current_cycle = int(str(current_cycle_str)) if str(current_cycle_str).isdigit() else 0
            app.logger.info(f"Student's current cycle from Object_10: {current_cycle}, Student Level Raw: {student_level_raw}")
            
            # Get school ID for averages calculation (from tutor app.py)
            school_id = None
            school_connection_raw = object10_data.get("field_133_raw")
            if isinstance(school_connection_raw, list) and school_connection_raw:
                school_id = school_connection_raw[0].get('id')
                app.logger.info(f"Extracted school_id '{school_id}' from student's Object_10 field_133_raw (list).")
            elif isinstance(school_connection_raw, str):
                school_id = school_connection_raw
                app.logger.info(f"Extracted school_id '{school_id}' (string) from student's Object_10 field_133_raw.")
            else:
                # Attempt to get from non-raw field if raw is not helpful
                school_connection_obj = object10_data.get("field_133")
                if isinstance(school_connection_obj, list) and school_connection_obj:
                    school_id = school_connection_obj[0].get('id')
                    app.logger.info(f"Extracted school_id '{school_id}' from student's Object_10 field_133 (non-raw object).")
                else:
                    app.logger.warning(f"Could not determine school_id from field_133_raw or field_133. Data (raw): {school_connection_raw}, Data (obj): {school_connection_obj}")
            
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
            
            # Calculate school VESPA averages
            school_vespa_averages = None
            if school_id:
                school_vespa_averages = get_school_vespa_averages(school_id)
                if school_vespa_averages:
                    app.logger.info(f"Successfully retrieved school-wide VESPA averages for school {school_id}: {school_vespa_averages}")
                else:
                    app.logger.warning(f"Failed to retrieve school-wide VESPA averages for school {school_id}.")
            else:
                app.logger.warning("Cannot fetch school-wide VESPA averages as school_id is unknown.")
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
                            "text": q_detail.get('questionText', 'Unknown Question'), # Changed key to 'text'
                            "score": score,
                            "category": q_detail.get('vespaCategory', 'N/A')
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
        academic_summary = []
        academic_megs = {}
        prior_attainment_score = None
        object112_profile_record_data = None # To store the whole Object_112 record
        
        # Call the new get_academic_profile function
        # Pass app.logger as the app_logger_instance argument
        academic_profile_response = get_academic_profile(student_object3_id, student_name_from_obj3, app.logger)
        
        if academic_profile_response:
            academic_summary = academic_profile_response.get("subjects", [])
            object112_profile_record_data = academic_profile_response.get("profile_record")

        if object112_profile_record_data: # Check if the record itself was found
            app.logger.info(f"Fetched Object_112 data for student: {object112_profile_record_data.get('field_3066')} (Name in Obj112)")
            
            # Get prior attainment score (field_3272 from tutorapp.py)
            # Ensure robust checking for _raw and direct field, and convert to float
            prior_attainment_val = None
            raw_pa = object112_profile_record_data.get('field_3272_raw')
            direct_pa = object112_profile_record_data.get('field_3272')

            if isinstance(raw_pa, (str, int, float)) and str(raw_pa).strip() != '':
                try: prior_attainment_val = float(raw_pa)
                except (ValueError, TypeError): pass
            
            if prior_attainment_val is None and isinstance(direct_pa, (str, int, float)) and str(direct_pa).strip() != '':
                try: prior_attainment_val = float(direct_pa)
                except (ValueError, TypeError): pass

            if prior_attainment_val is not None:
                prior_attainment_score = prior_attainment_val
                app.logger.info(f"Prior attainment score: {prior_attainment_score}")
            else:
                app.logger.warning(f"Could not parse prior attainment score from field_3272/field_3272_raw in Object_112. Raw: '{raw_pa}', Direct: '{direct_pa}'.")
            
            # Calculate overall MEGs if prior attainment is available
            if prior_attainment_score is not None:
                academic_megs["prior_attainment_score"] = prior_attainment_score
                
                for percentile, label_suffix in [(60, "60th"), (75, "75th"), (90, "90th"), (100, "100th")]:
                    meg_grade, meg_points = get_meg_for_prior_attainment(prior_attainment_score, "A Level", percentile)
                    # Ensure meg_grade is not None before trying to use it, and meg_points is not None
                    academic_megs[f"aLevel_meg_grade_{label_suffix}"] = meg_grade if meg_grade is not None else "N/A"
                    academic_megs[f"aLevel_meg_points_{label_suffix}"] = meg_points if meg_points is not None else 0
            
            # The academic_summary list should now be populated by parse_subjects_from_profile_record 
            # which is called inside the new get_academic_profile. We then need to iterate it here to add points and MEGs.
            # The old logic for iterating field_3080 etc. is now inside parse_subjects_from_profile_record.
            # We now need to process the `academic_summary` list that came from `get_academic_profile`
            # to add points and detailed MEGs per subject.

            processed_academic_summary = []
            if isinstance(academic_summary, list):
                for subject_entry in academic_summary:
                    if isinstance(subject_entry, dict) and subject_entry.get("subject") and not subject_entry["subject"].startswith("Academic profile not found") and not subject_entry["subject"].startswith("No academic subjects parsed") :
                        exam_type = subject_entry.get("examType", "A Level")
                        norm_qual = normalize_qualification_type(exam_type)
                        current_grade = subject_entry.get("currentGrade", "N/A")
                        
                        current_points = get_points(current_grade, norm_qual) if current_grade != 'N/A' else 0
                        subject_entry['normalized_qualification_type'] = norm_qual
                        subject_entry['currentGradePoints'] = current_points
                        
                        standard_meg_grade, standard_meg_points_val = "N/A", 0
                        if prior_attainment_score is not None:
                            # Get details for MEG lookup if needed (e.g. BTEC year/size)
                            qual_details_for_meg = extract_qual_details(exam_type, norm_qual, app.logger)
                            # The get_meg_for_prior_attainment in tutorapp also takes qual_details. We might need to adapt it or this call.
                            # For now, assuming the student version of get_meg_for_prior_attainment primarily uses percentile for A-Level
                            # and might need future enhancement for detailed non-Alevel MEG lookup based on qual_details.
                            standard_meg_grade, standard_meg_points_val = get_meg_for_prior_attainment(prior_attainment_score, norm_qual, 75) # Default 75th for standard
                        
                        subject_entry['standard_meg'] = standard_meg_grade if standard_meg_grade is not None else "N/A"
                        subject_entry['standardMegPoints'] = standard_meg_points_val if standard_meg_points_val is not None else 0
                        
                        if norm_qual == "A Level" and prior_attainment_score is not None:
                            for percentile in [60, 90, 100]: # 75th is already standard_meg
                                meg_grade_p, meg_points_p = get_meg_for_prior_attainment(prior_attainment_score, norm_qual, percentile)
                                if meg_points_p is not None:
                                    subject_entry[f"megPoints{percentile}"] = meg_points_p
                        processed_academic_summary.append(subject_entry)
                    else: # if subject entry is not valid, still add it to maintain list structure if it was a placeholder
                        processed_academic_summary.append(subject_entry)
                academic_summary = processed_academic_summary # Replace with the processed list

        else: # if object112_profile_record_data is None (academic profile not found by any method)
            app.logger.warning(f"No Object_112 data for student {student_name_from_obj3}. Academic summary will be default.")
            # academic_summary will be the default from get_academic_profile e.g. [{"subject": "Academic profile not found..."}]
            # Ensure it's a list for consistency
            if not isinstance(academic_summary, list) or not academic_summary: # Ensure academic_summary is the default list if record was None
                academic_summary = [{"subject": "Academic profile not found by any method.", "currentGrade": "N/A", "targetGrade": "N/A", "effortGrade": "N/A", "examType": "N/A"}]


        # Generate LLM insights
        # Ensure all necessary data for the LLM is included in this dictionary
        llm_data_for_insights = {
            "student_name": student_name_from_obj3,
            "student_level": student_level_raw, # Use the raw value from field_568
            "current_cycle": current_cycle,
            "vespa_profile": vespa_scores_for_profile, # This should be the dict with score_1_to_10 and score_profile_text
            "school_vespa_averages": school_vespa_averages, # Add school averages
            "academic_profile_summary": academic_summary, # List of subject dicts
            "academic_megs": academic_megs, # Dict of overall MEGs
            "student_reflections_and_goals": student_reflections,
            "object29_question_highlights": object29_highlights_top_bottom, # Dict with top_3 and bottom_3 lists
            "all_scored_questionnaire_statements": all_scored_statements # List of all scored statements for distribution calculation
        }
        
        # Pass app.logger to the LLM function
        llm_insights = generate_student_insights_with_llm(llm_data_for_insights, app.logger)
        
        # Use LLM insights if available, otherwise fall back to placeholders
        if not llm_insights:
            llm_insights = {
                "student_overview_summary": f"Welcome {student_name_from_obj3}! Your VESPA profile shows unique strengths and opportunities for growth. Let's explore them together.",
                "chart_comparative_insights": "Your VESPA scores show your current learning approach. Compare them with the school average to see where you stand.",
                "questionnaire_interpretation_and_reflection_summary": "Your questionnaire responses reveal important insights about your learning mindset and habits.",
                "academic_benchmark_analysis": "Your grades show your current performance. The MEG benchmarks indicate what's possible with focused effort.",
                "suggested_student_goals": [
                    "Focus on improving your lowest VESPA score this week",
                    "Set a specific study goal for your most challenging subject",
                    "Track your progress daily using a simple journal"
                ]
            }

        final_response = {
            "student_name": student_name_from_obj3,
            "student_level": student_level_raw, # Send raw level to frontend
            "current_cycle": current_cycle,
            "vespa_profile": vespa_scores_for_profile,
            "academic_profile_summary": academic_summary,
            "academic_megs": academic_megs,
            "student_reflections_and_goals": student_reflections,
            "object29_question_highlights": object29_highlights_top_bottom,
            "llm_generated_insights": llm_insights, 
            "all_scored_questionnaire_statements": all_scored_statements,
            "school_vespa_averages": school_vespa_averages
        }

        # --- NEW: Save student_overview_summary to Object_10, field_3289 ---
        if llm_insights and isinstance(llm_insights, dict) and object10_data and object10_data.get('id'):
            student_overview_summary_for_knack = llm_insights.get('student_overview_summary')
            if student_overview_summary_for_knack and isinstance(student_overview_summary_for_knack, str) and \
               not student_overview_summary_for_knack.lower().startswith("error:") and \
               not student_overview_summary_for_knack.lower().startswith("ai insights for") and \
               not student_overview_summary_for_knack.lower().startswith("an unexpected error") and \
               not student_overview_summary_for_knack.lower().startswith("welcome") and \
               student_overview_summary_for_knack.strip() != "": # Check for non-empty, non-generic summaries
                
                object10_record_id_to_update = object10_data.get('id')
                payload_to_update_obj10 = {
                    "field_3289": student_overview_summary_for_knack[:10000] # Knack paragraph text limit
                }
                headers_knack_update = {
                    'X-Knack-Application-Id': KNACK_APP_ID,
                    'X-Knack-REST-API-Key': KNACK_API_KEY,
                    'Content-Type': 'application/json'
                }
                update_url_obj10 = f"{KNACK_API_BASE_URL}/object_10/records/{object10_record_id_to_update}"
                try:
                    app.logger.info(f"Attempting to update Object_10 record {object10_record_id_to_update} with student chat summary for field_3289. Summary (first 100 chars): '{student_overview_summary_for_knack[:100]}...'")
                    update_response = requests.put(update_url_obj10, headers=headers_knack_update, json=payload_to_update_obj10)
                    update_response.raise_for_status()
                    app.logger.info(f"Successfully updated field_3289 for Object_10 record {object10_record_id_to_update}.")
                except requests.exceptions.HTTPError as e_http_obj10:
                    app.logger.error(f"HTTP error updating field_3289 for Object_10 {object10_record_id_to_update}: {e_http_obj10}. Response: {update_response.content if 'update_response' in locals() and update_response else 'N/A'}")
                except requests.exceptions.RequestException as e_req_obj10:
                    app.logger.error(f"Request exception updating field_3289 for Object_10 {object10_record_id_to_update}: {e_req_obj10}")
                except Exception as e_gen_obj10:
                    app.logger.error(f"General error updating field_3289 for Object_10 {object10_record_id_to_update}: {e_gen_obj10}")
            else:
                app.logger.info(f"Skipping update of field_3289 for Object_10 as LLM student_overview_summary was an error, placeholder, or empty: '{student_overview_summary_for_knack}'")
        else:
            app.logger.warning("Could not update field_3289 for Object_10 as llm_insights or object10_data (with ID) was missing/invalid.")

        return jsonify(final_response), 200

# --- Helper function to determine student's educational level for coaching KBs ---
def get_student_educational_level(student_level_raw):
    """
    Maps raw student level (e.g., from Knack field_568_raw) to 'Level 2' or 'Level 3'
    for use with coaching_questions_knowledge_base.json.
    This is a simplified example; adjust based on actual values in field_568_raw.
    """
    if not student_level_raw or student_level_raw == "N/A":
        return "Level 3" # Default if unknown
    
    level_lower = str(student_level_raw).lower()
    
    # Keywords for Level 3 (A-Level, Year 12, Year 13, L3, etc.)
    if any(kw in level_lower for kw in ["year 12", "year 13", "a-level", "level 3", "l3", "sixth form", "a level"]):
        return "Level 3"
    # Keywords for Level 2 (GCSE, Year 10, Year 11, L2, etc.)
    elif any(kw in level_lower for kw in ["year 10", "year 11", "gcse", "level 2", "l2"]):
        return "Level 2"
    
    app.logger.warning(f"Could not map student_level_raw '{student_level_raw}' to 'Level 2' or 'Level 3'. Defaulting to Level 3 for coaching questions.")
    return "Level 3" # Default


@app.route('/api/v1/chat_turn', methods=['POST', 'OPTIONS'])
def chat_turn():
    app.logger.info(f"Received request for /api/v1/chat_turn. Method: {request.method}")
    if request.method == 'OPTIONS':
        return _build_cors_preflight_response()
    
    if request.method == 'POST':
        data = request.get_json()
        app.logger.info(f"Chat turn data received: {str(data)[:500]}...")

        student_object3_id = data.get('student_knack_id') 
        chat_history = data.get('chat_history', []) 
        current_user_message = data.get('current_user_message')
        initial_ai_context = data.get('initial_ai_context') # This is the rich student data payload
        context_type = data.get('context_type', 'student')

        if not student_object3_id or not current_user_message:
            app.logger.error("chat_turn: Missing student_knack_id (Object_3 ID) or current_user_message.")
            return jsonify({"error": "Missing student ID or message"}), 400
        
        if not OPENAI_API_KEY:
            app.logger.error("chat_turn: OpenAI API key not configured.")
            save_chat_message_to_knack(student_object3_id, "Student", current_user_message)
            return jsonify({"ai_response": "I am currently unable to respond (AI not configured). Your message has been logged."}), 200

        user_message_saved_id = save_chat_message_to_knack(student_object3_id, "Student", current_user_message)
        if not user_message_saved_id:
            app.logger.error(f"chat_turn: Failed to save student's message to Knack for student Object_3 ID {student_object3_id}.")

        student_name_for_chat = "there"
        student_vespa_profile = {}
        student_educational_level_kb = "Level 3" # Default

        if initial_ai_context:
            if initial_ai_context.get('student_name'):
                student_name_for_chat = initial_ai_context['student_name'].split(' ')[0]
            student_vespa_profile = initial_ai_context.get('vespa_profile', {}) 
            student_level_raw_from_context = initial_ai_context.get('student_level', "N/A") 
            student_educational_level_kb = get_student_educational_level(student_level_raw_from_context)
            app.logger.info(f"Chat Turn: Student Name: {student_name_for_chat}, Mapped Edu Level for KB: {student_educational_level_kb}")
            
            app.logger.info(f"Initial AI Context Keys Available: {list(initial_ai_context.keys())}")
            app.logger.info(f"VESPA Profile Data from initial_ai_context: {student_vespa_profile}")
            if initial_ai_context.get('academic_profile_summary'):
                app.logger.info(f"Academic Profile Available from initial_ai_context: {len(initial_ai_context['academic_profile_summary'])} subjects")
            if initial_ai_context.get('object29_question_highlights'):
                app.logger.info(f"Questionnaire highlights available from initial_ai_context")
        else:
            app.logger.warning("No initial_ai_context provided to chat_turn!")

        conversation_depth = len([msg for msg in chat_history if msg.get('role') == 'user'])
        app.logger.info(f"Conversation depth: {conversation_depth} user messages")

        user_asking_for_activity = any(phrase in current_user_message.lower() for phrase in [
            "suggest an activity", "recommend an activity", "what activity", "any activities",
            "activity suggestion", "activity recommendation", "yes please suggest", "yes, suggest"
        ])

        rag_context_parts = ["--- Context for My VESPA AI Coach ---"]
        suggested_activities_for_response = []
        chosen_coaching_questions_for_llm = []
        inferred_vespa_element_from_query = None
        relevant_vespa_statements = []
        relevant_coaching_insights_for_chat = []
        
        # 1. Add student's data summary to RAG from initial_ai_context
        if initial_ai_context:
            rag_context_parts.append("\n--- About Me (Student's Data Summary from Initial Context) ---")
            llm_insights_from_context = initial_ai_context.get('llm_generated_insights', {})
            
            if llm_insights_from_context.get('student_overview_summary'):
                rag_context_parts.append(f"My Overall AI Snapshot: {llm_insights_from_context['student_overview_summary']}")

            # Use student_vespa_profile which was already populated from initial_ai_context if available
            if student_vespa_profile: # This was set earlier from initial_ai_context.get('vespa_profile', {})
                rag_context_parts.append("\nMy VESPA Scores:")
                for el, el_data in student_vespa_profile.items():
                    if el != "Overall" and isinstance(el_data, dict):
                         rag_context_parts.append(f" - {el}: {el_data.get('score_1_to_10', 'N/A')}/10 (Profile: '{el_data.get('score_profile_text', 'N/A')}')")
            
            school_avgs_from_context = initial_ai_context.get('school_vespa_averages')
            if school_avgs_from_context and student_vespa_profile:
                rag_context_parts.append("\nHow I Compare to School Averages:")
                for el, el_data in student_vespa_profile.items():
                    if el != "Overall" and el in school_avgs_from_context and isinstance(el_data, dict):
                        my_score = el_data.get('score_1_to_10', 'N/A')
                        school_avg = school_avgs_from_context.get(el)
                        if my_score != 'N/A' and school_avg is not None:
                            try:
                                diff = float(my_score) - float(school_avg)
                                if diff > 0: rag_context_parts.append(f" - {el}: I'm {diff:.1f} points above school average")
                                elif diff < 0: rag_context_parts.append(f" - {el}: I'm {abs(diff):.1f} points below school average")
                                else: rag_context_parts.append(f" - {el}: I'm at the school average")
                            except (ValueError, TypeError): pass
            
            academic_data_from_context = initial_ai_context.get('academic_profile_summary')
            if isinstance(academic_data_from_context, list) and academic_data_from_context:
                rag_context_parts.append("\nMy Academic Profile (first few subjects):")
                for subject in academic_data_from_context[:3]: 
                    if isinstance(subject, dict) and subject.get('subject') and subject['subject'] != 'N/A' and not subject['subject'].lower().startswith("academic profile not found"):
                        subj_text = f" - {subject.get('subject')}: Current={subject.get('currentGrade', 'N/A')}, Target={subject.get('targetGrade', 'N/A')}"
                        if subject.get('standard_meg'): subj_text += f", MEG={subject.get('standard_meg')}"
                        rag_context_parts.append(subj_text)
            
            meg_data_from_context = initial_ai_context.get('academic_megs')
            if meg_data_from_context and meg_data_from_context.get('prior_attainment_score'):
                rag_context_parts.append(f"\nMy Prior Attainment Score: {meg_data_from_context['prior_attainment_score']} (influences MEGs)")
            
            reflections_from_context = initial_ai_context.get('student_reflections_and_goals')
            if reflections_from_context and isinstance(reflections_from_context, dict):
                added_reflection_context = False
                for key, value in reflections_from_context.items():
                    if value and str(value).strip() and str(value).lower() not in ["not specified", "n/a"]:
                        if not added_reflection_context:
                            rag_context_parts.append("\nMy Recent Reflections & Goals (from initial context):")
                            added_reflection_context = True
                        if 'rrc' in key: rag_context_parts.append(f" - Reflection: {str(value)[:150]}...")
                        elif 'goal' in key: rag_context_parts.append(f" - Goal: {str(value)[:150]}...")
            
            highlights_from_context = initial_ai_context.get('object29_question_highlights')
            if highlights_from_context and isinstance(highlights_from_context, dict):
                if highlights_from_context.get('top_3') or highlights_from_context.get('bottom_3'):
                    rag_context_parts.append("\nMy Questionnaire Insights (from initial context):")
                    if highlights_from_context.get('top_3'):
                        rag_context_parts.append(" Strengths (I strongly agreed with):")
                        for item in highlights_from_context['top_3'][:2]: rag_context_parts.append(f"  • {item.get('category')}: \"{str(item.get('text',''))[:80]}...\"")
                    if highlights_from_context.get('bottom_3'):
                        rag_context_parts.append(" Areas to consider (I disagreed with):")
                        for item in highlights_from_context['bottom_3'][:2]: rag_context_parts.append(f"  • {item.get('category')}: \"{str(item.get('text',''))[:80]}...\"")
            
            if llm_insights_from_context.get('suggested_student_goals'):
                goals = llm_insights_from_context['suggested_student_goals']
                if goals and isinstance(goals, list) and any(str(g).strip() for g in goals):
                    rag_context_parts.append(f"\nPreviously suggested goals for me (from initial context): {'; '.join([str(g) for g in goals[:2] if str(g).strip()])}")

            q_interp_from_context = llm_insights_from_context.get('questionnaire_interpretation_and_reflection_summary')
            if q_interp_from_context and len(str(q_interp_from_context)) > 50:
                rag_context_parts.append(f"\nMy Questionnaire Analysis Summary (from initial context): {str(q_interp_from_context)[:200]}...")
        else:
            rag_context_parts.append("\n(Note: Detailed student data summary from initial context is not available for this turn.)")


        # 2. Determine if it's a "Focus Area" request or infer VESPA element from query
        is_focus_area_query = "what area to focus on" in current_user_message.lower() or "focus area" in current_user_message.lower()

        if not is_focus_area_query:
            query_lower = current_user_message.lower()
            app.logger.info(f"Attempting to infer VESPA element. Query_lower: '{query_lower}'")
            keyword_to_element_map = {
                ("vision", "goal", "future", "career", "motivation", "purpose", "direction", "aspiration", "dream", "ambition", "achieve", "aims", "objectives"): "Vision",
                ("effort", "hard work", "procrastination", "trying", "persevere", "lazy", "energy", "work ethic", "dedication", "commitment"): "Effort",
                ("systems", "organization", "plan", "notes", "timetable", "deadline", "homework", "complete", "time management", "schedule", "diary", "planner", "organised", "organize", "structure", "routine"): "Systems", # "notes" is a keyword here
                ("practice", "revision", "revise", "exam prep", "test myself", "study", "memory", "technique", "method", "preparation", "learning", "highlighting", "note-taking", "flashcards", "past papers", "past paper", "exam paper", "mock exam", "question practice", "testing"): "Practice",
                ("attitude", "mindset", "stress", "pressure", "confidence", "difficult", "anxiety", "worry", "belief", "resilience", "positive", "negative"): "Attitude"
            }
            element_found = False
            for keywords_tuple, element_name in keyword_to_element_map.items():
                # app.logger.debug(f"Checking element: {element_name} with keywords: {keywords_tuple}") # Original debug
                for kw in keywords_tuple:
                    app.logger.debug(f"Checking keyword '{kw}' from element '{element_name}' against query_lower: '{query_lower[:100]}...'") # Log each keyword check
                    if kw in query_lower:
                        inferred_vespa_element_from_query = element_name
                        app.logger.info(f"SUCCESS: Inferred VESPA element '{inferred_vespa_element_from_query}' from user query using keyword: '{kw}'.")
                        element_found = True
                        break # Break from inner loop (keywords_tuple)
                if element_found:
                    break # Break from outer loop (keyword_to_element_map)
            if not element_found:
                app.logger.info(f"FAILED: Could not infer VESPA element from query: '{query_lower}'")
        
        # 3. Add relevant VESPA statements from vespa-statements.json
        if VESPA_STATEMENTS_DATA and isinstance(VESPA_STATEMENTS_DATA, dict):
            vespa_statements_list = VESPA_STATEMENTS_DATA.get('vespa_statements', {}).get('statements', [])
            if vespa_statements_list and isinstance(vespa_statements_list, list):
                if "revis" in current_user_message.lower() or "highlight" in current_user_message.lower() or "note" in current_user_message.lower():
                    for statement_obj in vespa_statements_list:
                        if isinstance(statement_obj, dict):
                            statement_id = statement_obj.get('id', '')
                            if statement_id in ['P10', 'P12', 'P18', 'P20']: 
                                relevant_vespa_statements.append({
                                    'element': 'Practice', 'type': 'positive',
                                    'text': statement_obj.get('statement', ''), 'id': statement_id
                                })
                                if len(relevant_vespa_statements) >= 4: break
                
                if len(relevant_vespa_statements) < 4:
                    for statement_obj in vespa_statements_list:
                        if isinstance(statement_obj, dict):
                            statement_category = statement_obj.get('category', '').lower()
                            if (inferred_vespa_element_from_query and statement_category == inferred_vespa_element_from_query.lower()) or \
                               (not inferred_vespa_element_from_query and any(kw in current_user_message.lower() for kw in statement_obj.get('keywords', []))):
                                if len(relevant_vespa_statements) < 4:
                                    relevant_vespa_statements.append({
                                        'element': statement_category.capitalize(),
                                        'type': 'positive' if len(relevant_vespa_statements) < 2 else 'negative',
                                        'text': statement_obj.get('statement', '')
                                    })
                            if len(relevant_vespa_statements) >= 4: break

        if relevant_vespa_statements:
            rag_context_parts.append("\n--- VESPA Framework Perspectives (General Principles) ---")
            current_element_for_statement = None
            for vs_item in relevant_vespa_statements:
                if vs_item['element'] != current_element_for_statement:
                    current_element_for_statement = vs_item['element']
                    rag_context_parts.append(f"\nOn {current_element_for_statement}:")
                statement_prefix = "✓ Effective approaches often involve..." if vs_item['type'] == 'positive' else "✗ Less effective approaches might include..."
                rag_context_parts.append(f"  {statement_prefix} {vs_item['text']}")
            rag_context_parts.append("\n(Use these general VESPA perspectives to understand common patterns and guide your questions subtly.)")

        # 4. Add relevant coaching insights - ENHANCED for revision strategies
        if COACHING_INSIGHTS_DATA and isinstance(COACHING_INSIGHTS_DATA, list):
            revision_related_keywords = ["active", "passive", "retrieval", "testing", "practice", "recall", "memory", "revision", "study strategies", "notes", "cornell"] # Added "notes", "cornell"
            
            temp_insights_with_scores = []
            for insight in COACHING_INSIGHTS_DATA: 
                if isinstance(insight, dict):
                    insight_name = insight.get('name', '').lower()
                    insight_summary = insight.get('summary', '').lower()
                    insight_tags = [str(tag).lower() for tag in insight.get('tags', []) if isinstance(tag, str)]
                    
                    relevance_score_insight = 0
                    query_l = current_user_message.lower()
                    
                    for keyword_rev in revision_related_keywords:
                        if keyword_rev in insight_name or keyword_rev in insight_summary or keyword_rev in insight_tags:
                            relevance_score_insight += 3
                    
                    insight_all_text_corpus = insight_name + " " + insight_summary + " " + " ".join(insight_tags)
                    for word_in_query in query_l.split():
                        if len(word_in_query) > 3 and word_in_query in insight_all_text_corpus:
                            relevance_score_insight += 2
                    
                    if inferred_vespa_element_from_query:
                        if inferred_vespa_element_from_query.lower() in insight_tags or \
                           inferred_vespa_element_from_query.lower() in insight_name or \
                           inferred_vespa_element_from_query.lower() in insight_summary:
                            relevance_score_insight += 3
                    
                    if relevance_score_insight > 1: # Minimum relevance
                         temp_insights_with_scores.append({
                            'name': insight.get('name'), 'summary': insight.get('summary'),
                            'key_points': insight.get('key_points', [])[:3], 'relevance': relevance_score_insight
                        })
            
            temp_insights_with_scores.sort(key=lambda x: x['relevance'], reverse=True)
            relevant_coaching_insights_for_chat = temp_insights_with_scores[:3] # Get top 3
        
        if relevant_coaching_insights_for_chat:
            rag_context_parts.append("\n--- Relevant Coaching Insights & Research (For Your Inspiration) ---")
            for ci_item in relevant_coaching_insights_for_chat:
                rag_context_parts.append(f"\nInsight: {ci_item['name']}")
                rag_context_parts.append(f"Summary: {ci_item['summary']}")
                if ci_item.get('key_points'):
                    rag_context_parts.append("Key ideas to consider for coaching:")
                    for point_text in ci_item['key_points']: rag_context_parts.append(f"  • {point_text}")
            rag_context_parts.append("\n(Subtly weave these research-backed ideas into your conversation and questions, don't quote them directly.)")
        
        if "revis" in current_user_message.lower() or "highlight" in current_user_message.lower() or "note" in current_user_message.lower():
            rag_context_parts.append("\n--- CRITICAL COACHING NOTE: Active vs Passive Learning ---")
            rag_context_parts.append("The student may be discussing highlighting or simple note-taking. These are often PASSIVE strategies. Research strongly indicates ACTIVE strategies are far more effective.")
            rag_context_parts.append("ACTIVE strategies include: Self-testing, retrieval practice, teaching content to others, past paper practice, creating & answering questions, spaced repetition, interleaving, creating concept maps or Cornell notes from memory.")
            rag_context_parts.append("GENTLY explore their current methods and guide them towards discovering more active and effective techniques through questioning. Don't preach, help them find better ways.")

        # 5. RAG based on Coaching Questions KB & Activities
        if coaching_kb and VESPA_ACTIVITIES_DATA:
            app.logger.info(f"Processing RAG with Coaching KB and Activities KB. Student Edu Level for KB: {student_educational_level_kb}")
            
            overall_profile_categories_for_framing = [details.get('score_profile_text', 'N/A') for details in student_vespa_profile.values() if isinstance(details, dict) and details.get('score_profile_text', 'N/A') != 'N/A']
            low_score_count_for_framing = sum(1 for cat_text in overall_profile_categories_for_framing if cat_text in ["Low", "Very Low"])
            
            framing_statement_to_use_rag = coaching_kb.get('conditionalFramingStatements', [{}])[0].get('statement', '') 
            if low_score_count_for_framing >= 4 and len(overall_profile_categories_for_framing) == 5 :
                framing_statement_to_use_rag = next((s_item['statement'] for s_item in coaching_kb['conditionalFramingStatements'] if s_item['id'] == 'low_4_or_5_scores'), framing_statement_to_use_rag)
            if framing_statement_to_use_rag:
                rag_context_parts.append(f"\nCoach's Opening Thought (General Framing): {framing_statement_to_use_rag}")

            target_vespa_element_for_rag = None
            target_score_category_for_rag = None

            if is_focus_area_query:
                app.logger.info("Focus Area query detected. Identifying lowest VESPA score from student_vespa_profile.")
                lowest_score_val = 11
                lowest_element_name = None
                if student_vespa_profile: # Ensure it's populated
                    for vespa_el_name, el_details in student_vespa_profile.items():
                        if vespa_el_name == "Overall" or not isinstance(el_details, dict): continue
                        try:
                            current_score = float(el_details.get('score_1_to_10', 10))
                            if current_score < lowest_score_val:
                                lowest_score_val = current_score
                                lowest_element_name = vespa_el_name
                        except (ValueError, TypeError): pass
                if lowest_element_name:
                    target_vespa_element_for_rag = lowest_element_name
                    target_score_category_for_rag = student_vespa_profile[lowest_element_name].get('score_profile_text', 'N/A')
                    rag_context_parts.append(f"\nStudent wants to focus on an area. Their lowest VESPA element is '{target_vespa_element_for_rag}' (Profile: '{target_score_category_for_rag}'). Prioritize questions/activities for this.")
                else: app.logger.warning("Focus area query, but could not determine lowest VESPA element from profile.")
            elif inferred_vespa_element_from_query:
                target_vespa_element_for_rag = inferred_vespa_element_from_query
                if student_vespa_profile and target_vespa_element_for_rag in student_vespa_profile and isinstance(student_vespa_profile[target_vespa_element_for_rag], dict):
                    target_score_category_for_rag = student_vespa_profile[target_vespa_element_for_rag].get('score_profile_text', 'N/A')
                else: # If profile doesn't have this element (should not happen ideally) or not dict
                    target_score_category_for_rag = "Medium" # Default if no score profile for inferred element
                    app.logger.warning(f"Using inferred VESPA '{target_vespa_element_for_rag}' but no score profile in student_vespa_profile. Defaulting to 'Medium' for RAG.")
                app.logger.info(f"Using inferred VESPA element '{target_vespa_element_for_rag}' for RAG. Score profile: '{target_score_category_for_rag}'.")
            else:
                app.logger.info("No specific VESPA element inferred for targeted RAG. Will rely on general keyword search for activities if applicable.")
            
            if target_vespa_element_for_rag and target_score_category_for_rag and target_score_category_for_rag != "N/A":
                questions_data_level_kb = coaching_kb.get('vespaSpecificCoachingQuestionsWithActivities', {}).get(target_vespa_element_for_rag, {}).get(student_educational_level_kb, {})
                questions_for_category_kb = questions_data_level_kb.get(target_score_category_for_rag, {})
                
                retrieved_questions_kb = questions_for_category_kb.get('questions', [])
                retrieved_activity_ids_kb = questions_for_category_kb.get('related_activity_ids', [])

                if retrieved_questions_kb:
                    chosen_coaching_questions_for_llm = [q_text for q_text in retrieved_questions_kb[:3]] 
                    rag_context_parts.append(f"\n--- Potentially Relevant Coaching Questions for '{target_vespa_element_for_rag}' (Level: {student_educational_level_kb}, Profile: {target_score_category_for_rag}) ---")
                    for q_text_item in chosen_coaching_questions_for_llm:
                        rag_context_parts.append(f"- {q_text_item}")
                
                include_activities_rag = (conversation_depth >= 1 or user_asking_for_activity) and retrieved_activity_ids_kb # MODIFIED: Threshold lowered to depth 1
                
                if include_activities_rag:
                    activity_header_text = f"\n--- Suggested Activities for '{target_vespa_element_for_rag}' (If student asks or after more discussion) ---" if user_asking_for_activity else f"\n--- Potential Activities for '{target_vespa_element_for_rag}' (Available if student expresses need) ---"
                    rag_context_parts.append(activity_header_text)
                    
                    activity_count_primary = 0
                    for act_id_kb in retrieved_activity_ids_kb:
                        if activity_count_primary >= 2: break # Limit to 2 from primary source
                        activity_detail_kb = next((act_item for act_item in VESPA_ACTIVITIES_DATA if act_item.get('id') == act_id_kb), None)
                        if activity_detail_kb:
                            activity_data_for_llm_item = {
                                "id": activity_detail_kb.get('id'), "name": activity_detail_kb.get('name'),
                                "short_summary": activity_detail_kb.get('short_summary'), "pdf_link": activity_detail_kb.get('pdf_link'),
                                "vespa_element": activity_detail_kb.get('vespa_element'), "level": activity_detail_kb.get('level')
                            }
                            suggested_activities_for_response.append(activity_data_for_llm_item)
                            pdf_text = " (Resource PDF available)" if activity_data_for_llm_item['pdf_link'] and activity_data_for_llm_item['pdf_link'] != '#' else ""
                            
                            rag_context_parts.append(f"\n- Name: {activity_data_for_llm_item['name']}{pdf_text}.\n  Summary: {activity_data_for_llm_item['short_summary']}")
                            activity_count_primary += 1
                    app.logger.info(f"Student chat RAG: Found {len(suggested_activities_for_response)} activities via coaching questions link for {target_vespa_element_for_rag}.")
                elif retrieved_activity_ids_kb and conversation_depth >= 0: 
                    rag_context_parts.append(f"\n[Coach Note: Relevant activities exist for '{target_vespa_element_for_rag}'. Consider asking if student wants suggestions later if the conversation heads that way.]")

            # Fallback: General keyword search for activities - MODIFIED threshold & scoring
            if not suggested_activities_for_response and (conversation_depth >= 1 or user_asking_for_activity): # Fallback if no primary activities and depth >= 1
                common_words_filter = {"is", "a", "the", "and", "to", "of", "it", "in", "for", "on", "with", "as", "an", "at", "by", "my", "i", "me", "what", "how", "help", "can", "some", "this", "that", "area", "areas", "score", "scores"}
                cleaned_msg_for_kw_search = current_user_message.lower()
                for char_to_replace_item in ['?', '.', ',', '\'', '"', '!']: # Ensure ' is \'
                    cleaned_msg_for_kw_search = cleaned_msg_for_kw_search.replace(char_to_replace_item, '')
                keywords_from_query = [word for word in cleaned_msg_for_kw_search.split() if word not in common_words_filter and len(word) > 3]
            
                if keywords_from_query:
                    found_activities_text_for_prompt_fallback = []
                    processed_activity_ids_student_chat_fallback = set()
                    
                    scored_activities_list = []
                    for activity_item_fallback in VESPA_ACTIVITIES_DATA:
                        relevance_score_fallback = 0
                        activity_name_l = str(activity_item_fallback.get('name', '')).lower()
                        
                        activity_keywords_l_list = activity_item_fallback.get('keywords', [])
                        if not isinstance(activity_keywords_l_list, list): activity_keywords_l_list = []
                        activity_keywords_l = [str(k_item).lower() for k_item in activity_keywords_l_list]
                        activity_summary_l = str(activity_item_fallback.get('short_summary', '')).lower()

                        for kw_usr_item in keywords_from_query:
                            if kw_usr_item in activity_name_l: relevance_score_fallback += 5
                            if kw_usr_item in activity_keywords_l: relevance_score_fallback += 4 
                            if kw_usr_item in activity_summary_l: relevance_score_fallback += 1
                        
                        if inferred_vespa_element_from_query and activity_item_fallback.get('vespa_element', '').lower() == inferred_vespa_element_from_query.lower():
                            relevance_score_fallback += 3
                        
                        context_keywords_map = {
                            "active_learning": ["flashcard", "test", "quiz", "retrieval", "practice", "leitner", "command verb", "past paper", "exam paper", "mock exam", "question practice", "self-testing", "spaced repetition", "interleaving"],
                            "organization": ["plan", "schedule", "diary", "timetable", "system", "organize", "task management", "prioritization", "notes"], # "notes" added
                            "mindset": ["confidence", "stress", "anxiety", "belief", "attitude", "resilience", "growth mindset", "coping"],
                            "goal_setting": ["goal", "target", "vision", "future", "career", "aspiration", "objective", "plan"]
                        }
                        
                        for context_type_name, context_word_items in context_keywords_map.items():
                            if any(word_ctx in current_user_message.lower() for word_ctx in context_word_items):
                                activity_corpus_theme = activity_name_l + " " + " ".join(activity_keywords_l) + " " + activity_summary_l
                                matching_ctx_score = sum(1 for word_ctx_item in context_word_items if word_ctx_item in activity_corpus_theme)
                                relevance_score_fallback += matching_ctx_score * 2 
                        
                        if relevance_score_fallback > 3: # Adjusted threshold to >3
                            scored_activities_list.append((relevance_score_fallback, activity_item_fallback))
                    
                    scored_activities_list.sort(key=lambda x_item: x_item[0], reverse=True)
                    
                    for score_val, activity_data_fb in scored_activities_list[:2]: # Take top 2 fallback
                        if activity_data_fb.get('id') not in processed_activity_ids_student_chat_fallback:
                            activity_llm_data = {
                                "id": activity_data_fb.get('id'), "name": activity_data_fb.get('name'),
                                "short_summary": activity_data_fb.get('short_summary'), "pdf_link": activity_data_fb.get('pdf_link'),
                                "vespa_element": activity_data_fb.get('vespa_element'), "level": activity_data_fb.get('level')
                            }
                            suggested_activities_for_response.append(activity_llm_data)
                            pdf_text_fb = " (Resource PDF available)" if activity_llm_data['pdf_link'] and activity_llm_data['pdf_link'] != '#' else ""
                            found_activities_text_for_prompt_fallback.append(
                                f"- Name: {activity_llm_data['name']}{pdf_text_fb}. Summary: {activity_llm_data['short_summary'][:150]}..."
                            )
                            processed_activity_ids_student_chat_fallback.add(activity_llm_data['id'])
                
                    if found_activities_text_for_prompt_fallback:
                        rag_context_parts.append("\n--- Also Consider these Activities (based on your message keywords, if primary ones aren't suitable) ---")
                        rag_context_parts.extend(found_activities_text_for_prompt_fallback)
                        app.logger.info(f"Student chat RAG: Added {len(found_activities_text_for_prompt_fallback)} fallback activities via keyword match.")
        
        system_prompt_content = f"""You are My VESPA AI Coach - a warm, supportive coach who helps students develop their Vision, Effort, Systems, Practice, and Attitude.

You're chatting with {student_name_for_chat}. Always use just their first name.

Your coaching style:
- Be conversational and natural, like a friendly mentor.
- Ask layered questions to understand their situation better before offering solutions.
- Listen actively and respond to what they're actually saying. Use their words.
- Use the VESPA framework naturally in your guidance, without being rigid or overly academic.
- Be encouraging but also gently challenging when appropriate (e.g., if they mention ineffective study habits).
- When students mention ineffective strategies (like passive highlighting), help them discover better approaches through Socratic questioning, not by directly telling them they are wrong.

IMPORTANT: Never start your responses with "{student_name_for_chat}:" or "My AI Coach:". Just respond naturally.

When responding to {student_name_for_chat}:
1.  Acknowledge and validate their feelings or situation first.
2.  Ask open-ended, clarifying questions to understand their specific challenge and current approach *in detail*.
3.  Connect their challenges to relevant VESPA elements naturally during the conversation if it flows well.
4.  Focus on practical, actionable advice *they can implement*, co-creating solutions.
5.  Let the conversation flow. Sometimes they need encouragement, sometimes practical tips, sometimes just to be heard.

ACTIVITY SUGGESTION PROTOCOL:
-   DO NOT suggest activities in the first 1-2 turns unless the student explicitly asks for one. Focus on rapport and understanding first.
-   After 2-3 turns, IF the conversation naturally leads to a point where an activity could be helpful AND you have a *directly relevant* activity from the RAG context, you can ask: "I'm wondering if you'd find it helpful for me to suggest an activity or exercise that might support you with this. Would that be useful?"
-   ONLY suggest activities if they say "yes" or have already asked.
-   CRITICAL: When suggesting an activity, ONLY pick one that DIRECTLY and CLEARLY addresses the student's *specific current need* and what they have *just been talking about*. If no RAG activities are a good fit, DO NOT suggest any. Instead, say something like, "I don't have a specific worksheet for that exact point right now, but we can definitely explore some strategies for [their specific issue] together. For example..." or continue the coaching conversation.
-   If you DO suggest an activity, briefly state WHY it's relevant to *their specific situation* (e.g., "Based on what you said about organizing your notes, the 'Cornell Notes' activity might give you a useful structure."). Introduce it naturally (e.g., "Okay, for [their specific problem], an activity like 'XYZ' could be helpful because...").

The RAG context (ADDITIONAL CONTEXT section) provides student data, VESPA principles, coaching insights, and potentially relevant activities. Use these as *inspiration and background*, not a script. Adapt them. You're a coach.

Remember: Every student is unique. Tailor your approach. Vary your response style. Avoid formulaic responses. Be genuine."""
        
        conversation_guidance = ""
        if conversation_depth < 2 and not user_asking_for_activity:
            conversation_guidance = f"""

CONVERSATION PHASE (Turn {conversation_depth + 1}): Early stage.
- Focus: Build rapport, active listening, deep understanding of their specific issue.
- Actions: Ask open-ended questions. Explore their current methods and feelings.
- Activities: DO NOT suggest activities yet unless they ask. IGNORE any activities in RAG for now.
"""
        elif conversation_depth >= 2 and not user_asking_for_activity: 
            conversation_guidance = f"""

CONVERSATION PHASE (Turn {conversation_depth + 1}): Deeper dive.
- Focus: Continue coaching. If a *highly relevant* activity exists in RAG AND it feels natural after understanding their need:
- Action (Optional): You could ask: "I have an idea for an activity that might help with [their specific issue just discussed]. Would you be interested in hearing about it?"
- Activities: Only suggest if they confirm interest AND it's directly relevant.
"""
        elif user_asking_for_activity and suggested_activities_for_response:
            conversation_guidance = f"""

ACTIVITY SUGGESTION PHASE: Student has asked for activity suggestions.
- Action: Acknowledge their request.
- Activities: Review the suggested activities in RAG. Pick ONLY 1 or MAX 2 that are *most directly relevant* to their *current specific problem*.
- Explain *briefly and clearly* how each chosen activity connects to what they've shared.
- Ask which one resonates or if they'd like to try one.
"""
        
        system_prompt_content += conversation_guidance
        
        messages_for_llm = [{"role": "system", "content": system_prompt_content}]

        if rag_context_parts and len(rag_context_parts) > 1 : 
            system_rag_content = "\n".join(rag_context_parts)
            
            activity_guidance = f"""--- How to Use Activities Effectively (Interpreting RAG Context for this Turn with {student_name_for_chat}) ---
1.  RELEVANCE IS KEY: Only suggest activities from RAG that *directly address the specific challenge* {student_name_for_chat} is discussing *right now*.
2.  NO FORCING: Don't suggest an activity just because it's in RAG if it doesn't fit the immediate conversation.
3.  EXPLAIN WHY: If you suggest an activity, briefly explain *how it connects to what they just told you*. Example: "Since you mentioned struggling with [specific issue], the '[Activity Name]' activity might help you by [briefly explain relevance]."
4.  CONVERSATIONAL FLOW: Introduce activities naturally, per the main system prompt.
5.  PRIORITIZE COACHING: Remember, your primary role is a coach. Listening and guiding questions are often more valuable than just offering activities.

RAG INTERPRETATION NOTES:
-   Student Data Summary: Use this to understand {student_name_for_chat}'s general context, but focus your response on their *current message*.
-   VESPA/Coaching Insights: Let these subtly inform your questions and understanding of potential underlying themes related to VESPA, but don't lecture.
-   Coaching Questions (from RAG): These are good starting points for your own questions if relevant to the topic. Adapt them.
-   Activities (from RAG): These are *potential tools*. Evaluate their relevance to the *current specific point* of the conversation *very carefully* before even considering asking to suggest one. If the student is talking about X, don't suggest an activity for Y. If none fit, don't suggest any, as per main prompt.
"""
            
            messages_for_llm.append({"role": "system", "content": f"ADDITIONAL CONTEXT FOR YOUR RESPONSE (Student Data, RAG Insights & Potential Activities):\n{system_rag_content}\n{activity_guidance}"})
            app.logger.info(f"Student chat: Added RAG context to LLM prompt. Length: {len(system_rag_content)} + {len(activity_guidance)}")
            app.logger.debug(f"Full RAG context for LLM (excluding main system prompt): {system_rag_content}\n{activity_guidance}")

        for message in chat_history:
            role = message.get("role", "user").lower()
            if role not in ["user", "assistant"]: role = "user"
            messages_for_llm.append({"role": role, "content": message.get("content", "")})
        
        messages_for_llm.append({"role": "user", "content": current_user_message})

        ai_response_text = "I'm having a little trouble formulating a response right now. Could you try rephrasing your question, or perhaps we can talk about something else?"
        try:
            app.logger.info(f"Student chat: Sending to LLM. Number of messages for LLM: {len(messages_for_llm)}.")
            app.logger.info(f"Student chat: Total activities available in RAG for LLM consideration this turn: {len(suggested_activities_for_response)}")
            if suggested_activities_for_response:
                app.logger.info(f"Student chat: RAG Activity IDs available: {[act_item['id'] for act_item in suggested_activities_for_response]}")
            
            llm_response = openai.chat.completions.create(
                model="gpt-4o-mini", 
                messages=messages_for_llm,
                max_tokens=450, 
                temperature=0.75, 
                n=1,
                stop=None
            )
            ai_response_text = llm_response.choices[0].message.content.strip()
            app.logger.info(f"Student chat: LLM raw response: {ai_response_text}")

        except Exception as e:
            app.logger.error(f"Student chat: Error calling OpenAI API: {e}")

        ai_message_saved_id = save_chat_message_to_knack(student_object3_id, "My AI Coach", ai_response_text)
        if not ai_message_saved_id:
            app.logger.error(f"Student chat: Failed to save AI's response to Knack for student Object_3 ID {student_object3_id}.")

        # The activities sent back to frontend are those *retrieved by RAG this turn*,
        # NOT necessarily those *suggested by the LLM in its response*.
        # The LLM decides *if and how* to use the RAG-provided activities based on its prompt.
        return jsonify({
            "ai_response": ai_response_text, 
            "suggested_activities_in_chat": suggested_activities_for_response, # These are from RAG this turn
            "ai_message_knack_id": ai_message_saved_id 
        })
    
    
@app.route('/api/v1/chat_history', methods=['POST', 'OPTIONS'])
def chat_history():
    app.logger.info(f"Received request for /api/v1/chat_history. Method: {request.method}")
    if request.method == 'OPTIONS':
        return _build_cors_preflight_response()
    
    if request.method == 'POST':
        data = request.get_json()
        student_object3_id = data.get('student_knack_id') 
        max_messages = data.get('max_messages', 50) 
        initial_ai_context = data.get('initial_ai_context')

        if not student_object3_id:
            app.logger.error("get_chat_history: Missing student_knack_id (Object_3 ID).")
            return jsonify({"error": "Missing student_knack_id"}), 400

        knack_object_key_chatlog = "object_119"
        # Filter by field_3283 (Student connection to Object_3 in object_119)
        filters = [
            {'field': 'field_3283', 'operator': 'is', 'value': student_object3_id}
        ]
        
        app.logger.info(f"Fetching chat history for student Obj3 ID {student_object3_id} from {knack_object_key_chatlog} with filters: {filters}")
        
        rows_to_fetch = max(100, max_messages * 2)
        if rows_to_fetch > 1000: rows_to_fetch = 1000

        chat_log_response = get_knack_record(
            knack_object_key_chatlog, 
            filters=filters, 
            page=1, 
            rows_per_page=rows_to_fetch 
        )

        all_student_chat_records = []
        if chat_log_response and isinstance(chat_log_response, dict) and 'records' in chat_log_response:
            all_student_chat_records = chat_log_response['records']
            app.logger.info(f"Fetched initial {len(all_student_chat_records)} chat records for student {student_object3_id} from {knack_object_key_chatlog}.")
        else:
            app.logger.warning(f"No chat records found or unexpected response format for student {student_object3_id} from {knack_object_key_chatlog}. Response: {chat_log_response}")
            return jsonify({"chat_history": [], "total_count": 0, "liked_count": 0, "summary": "No chat history found for you yet."}), 200

        # Sort records by timestamp (field_1285 in object_119 - CONFIRM THIS IS STILL CORRECT)
        def get_datetime_from_knack_ts(ts_str):
            if not ts_str: return datetime.min
            try:
                return datetime.strptime(ts_str, '%d/%m/%Y %H:%M:%S')
            except ValueError:
                try: 
                    return datetime.strptime(ts_str, '%m/%d/%Y %H:%M:%S')
                except ValueError:
                    app.logger.warning(f"Could not parse Knack timestamp: {ts_str} with common formats. Using fallback for sorting.")
                return datetime.min

        all_student_chat_records.sort(key=lambda r: get_datetime_from_knack_ts(r.get('field_3285')), reverse=True) # CORRECTED TIMESTAMP FIELD

        recent_chat_records = all_student_chat_records[:max_messages]
        
        chat_history_for_frontend = []
        liked_count = 0 
        for record in recent_chat_records:
            author = record.get('field_3282', 'Student') # Author from field_3282
            role_for_frontend = "assistant" if author == "My AI Coach" else "user"
            is_liked_val = record.get('field_3287') == "Yes" # Liked status from field_3287
            if is_liked_val:
                liked_count +=1

            chat_history_for_frontend.append({
                "id": record.get('id'),
                "role": role_for_frontend,
                "content": record.get('field_3286', ""), # Message Text from field_3286
                "is_liked": is_liked_val, 
                "timestamp": record.get('field_3285') # CORRECTED TIMESTAMP FIELD
            })
        
        chat_history_for_frontend.reverse()
        total_chat_count_for_student = len(all_student_chat_records) 

        summary_text = f"You have {total_chat_count_for_student} messages in your chat history."
        if initial_ai_context and initial_ai_context.get('llm_generated_insights', {}).get('student_overview_summary'):
            summary_text = initial_ai_context['llm_generated_insights']['student_overview_summary']

        app.logger.info(f"Returning {len(chat_history_for_frontend)} messages for student chat history. Total for student: {total_chat_count_for_student}. Liked count: {liked_count}")
        return jsonify({
            "chat_history": chat_history_for_frontend,
            "total_count": total_chat_count_for_student,
            "liked_count": liked_count, 
            "summary": summary_text
        }), 200


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

# --- Add definitions for new KBs from tutorapp.py here, after existing KB loading ---
coaching_kb = load_json_file('coaching_questions_knowledge_base.json')
COACHING_INSIGHTS_DATA = load_json_file('coaching_insights.json')
VESPA_ACTIVITIES_DATA = load_json_file('vespa_activities_kb.json')
VESPA_STATEMENTS_DATA = load_json_file('vespa-statements.json')  # Load VESPA statements KB

# Load ALPS bands (ensure these JSON files are in your student app's knowledge_base directory)
alps_bands_btec2010_kb = load_json_file('alpsBands_btec2010_main.json')
alps_bands_btec2016_kb = load_json_file('alpsBands_btec2016_main.json')
alps_bands_cache_kb = load_json_file('alpsBands_cache.json')
alps_bands_ib_kb = load_json_file('alpsBands_ib.json')
alps_bands_preU_kb = load_json_file('alpsBands_preU.json')
alps_bands_ual_kb = load_json_file('alpsBands_ual.json')
alps_bands_wjec_kb = load_json_file('alpsBands_wjec.json')

# Load Reflective Statements from text file (similar to tutorapp.py)
REFLECTIVE_STATEMENTS_DATA = []
try:
    current_dir = os.path.dirname(os.path.abspath(__file__))
    # Path should be relative to this app.py, inside knowledge_base.
    # If '100 statements - 2023.txt' is directly in 'knowledge_base' for student app:
    statements_file_path = os.path.join(current_dir, 'knowledge_base', '100 statements - 2023.txt')
    statements_file_path = os.path.normpath(statements_file_path)
    app.logger.info(f"Attempting to load 100 statements from: {statements_file_path}")
    with open(statements_file_path, 'r', encoding='utf-8') as f:
        REFLECTIVE_STATEMENTS_DATA = [line.strip() for line in f if line.strip()]
    app.logger.info(f"Successfully loaded {len(REFLECTIVE_STATEMENTS_DATA)} statements.")
except FileNotFoundError:
    app.logger.error(f"'100 statements - 2023.txt' not found at {statements_file_path}.")
    # Consider if this should be a critical error or if the app can run without it.
except Exception as e:
    app.logger.error(f"Error loading '100 statements - 2023.txt': {e}")

# Log status of newly loaded KBs
if not coaching_kb: app.logger.warning("Coaching KB (coaching_questions_knowledge_base.json) failed to load.")
else: app.logger.info("Successfully loaded Coaching KB.")
if not COACHING_INSIGHTS_DATA: app.logger.warning("Coaching Insights KB (coaching_insights.json) failed to load.")
else: app.logger.info(f"Successfully loaded {len(COACHING_INSIGHTS_DATA)} records from Coaching Insights KB.")
if not VESPA_ACTIVITIES_DATA: app.logger.warning("VESPA Activities KB (vespa_activities_kb.json) failed to load.")
else: app.logger.info(f"Successfully loaded {len(VESPA_ACTIVITIES_DATA)} records from VESPA Activities KB.")
if not VESPA_STATEMENTS_DATA: app.logger.warning("VESPA Statements KB (vespa-statements.json) failed to load.")
else: app.logger.info(f"Successfully loaded VESPA Statements KB.")
if not REFLECTIVE_STATEMENTS_DATA: app.logger.warning("Reflective Statements (100_statements.txt) failed to load or is empty.")
else: app.logger.info(f"Successfully loaded {len(REFLECTIVE_STATEMENTS_DATA)} statements from 100_statements.txt")

# Existing ALPS KBs checks (just to ensure we don't duplicate logs if they existed, but adding new ones)
if not alps_bands_aLevel_60_kb: app.logger.warning("ALPS A-Level 60th percentile KB failed to load.")
if not alps_bands_aLevel_75_kb: app.logger.warning("ALPS A-Level 75th percentile KB failed to load.") # Already loaded, but good to check
if not alps_bands_aLevel_90_kb: app.logger.warning("ALPS A-Level 90th percentile KB failed to load.")
if not alps_bands_aLevel_100_kb: app.logger.warning("ALPS A-Level 100th percentile KB failed to load.")
if not alps_bands_btec2010_kb: app.logger.warning("ALPS BTEC 2010 KB failed to load.")
if not alps_bands_btec2016_kb: app.logger.warning("ALPS BTEC 2016 KB failed to load.")
# Add checks for cache, ib, preU, ual, wjec if desired

# Ensure all required KBs for core functionality are checked critically
if not grade_points_mapping_kb: app.logger.error("CRITICAL: Grade Points Mapping KB failed to load.")
if not psychometric_question_details_kb: app.logger.warning("Psychometric Question Details KB failed to load.")
if not report_text_kb: app.logger.warning("Report Text KB (Object_33) failed to load.")


# --- Save Chat Message to Knack (Object_118) --- # Docstring needs update
# UPDATED to save to Object_119
def save_chat_message_to_knack(student_obj3_id, author, message_text, is_liked=False):
    if not KNACK_APP_ID or not KNACK_API_KEY:
        app.logger.error("Knack App ID or API Key is missing for save_chat_message_to_knack.")
        return None
    
    if not student_obj3_id:
        app.logger.error("save_chat_message_to_knack: student_obj3_id is required.")
        return None

    # Object_119 ("AIChatLog" for students) Field Mappings:
    # field_3288: Session ID (Short Text)
    # field_3281: Log Sequence (Auto-increment - Knack handles)
    # field_3282: Author (Short Text - "Student" or "My AI Coach")
    # field_3283: Student (Connection to Object_6 - Student Records)
    # field_3284: Object_10 connection (Connection to Object_10 - VESPA Results)
    # field_3285: Timestamp (Date/Time - dd/mm/yyyy HH:MM:SS)
    # field_3286: Conversation Log (Paragraph Text)
    # field_3287: Liked (Yes/No Boolean)

    student_email = None
    student_object_6_id = None
    student_object_10_id = None

    # 1. Get student_email from Object_3 using student_obj3_id
    if student_obj3_id:
        app.logger.info(f"save_chat: Fetching Object_3 record for ID: {student_obj3_id} to get email.")
        object_3_record = get_knack_record("object_3", record_id=student_obj3_id)
        if object_3_record and isinstance(object_3_record, dict):
            raw_val_field70 = object_3_record.get('field_70_raw')
            obj_val_field70 = object_3_record.get('field_70')

            if isinstance(obj_val_field70, dict) and 'email' in obj_val_field70 and isinstance(obj_val_field70['email'], str):
                student_email = obj_val_field70['email'].strip()
            elif isinstance(raw_val_field70, dict) and 'email' in raw_val_field70 and isinstance(raw_val_field70['email'], str):
                 student_email = raw_val_field70['email'].strip()
            elif isinstance(raw_val_field70, str):
                temp_email_str = raw_val_field70.strip()
                if temp_email_str.lower().startswith('<a') and 'mailto:' in temp_email_str.lower() and temp_email_str.lower().endswith('</a>'):
                    try:
                        mailto_keyword = 'mailto:'
                        mailto_start_index = temp_email_str.lower().find(mailto_keyword) + len(mailto_keyword)
                        end_char_index = len(temp_email_str)
                        quote_index = temp_email_str.find('"', mailto_start_index)
                        if quote_index != -1: end_char_index = min(end_char_index, quote_index)
                        single_quote_index = temp_email_str.find("'", mailto_start_index)
                        if single_quote_index != -1: end_char_index = min(end_char_index, single_quote_index)
                        angle_bracket_index = temp_email_str.find('>', mailto_start_index)
                        if angle_bracket_index != -1: end_char_index = min(end_char_index, angle_bracket_index)
                        extracted_from_href = temp_email_str[mailto_start_index:end_char_index].strip()
                        if '@' in extracted_from_href and ' ' not in extracted_from_href and '<' not in extracted_from_href:
                            student_email = extracted_from_href
                        if not student_email:
                            text_start_actual_index = temp_email_str.find('>') 
                            if text_start_actual_index != -1:
                                text_start_actual_index +=1
                                text_end_index = temp_email_str.lower().rfind('</a>')
                                if text_end_index > text_start_actual_index :
                                    extracted_text = temp_email_str[text_start_actual_index:text_end_index].strip()
                                    if '@' in extracted_text and ' ' not in extracted_text and '<' not in extracted_text:
                                        student_email = extracted_text
                    except Exception as e_parse:
                        app.logger.warning(f"save_chat: Error parsing HTML email string '{temp_email_str}' from Object_3: {e_parse}")
                elif '@' in temp_email_str and not '<' in temp_email_str:
                    student_email = temp_email_str
            elif isinstance(obj_val_field70, str) and '@' in obj_val_field70 and not '<' in obj_val_field70 :
                 student_email = obj_val_field70.strip()
            
            if student_email:
                app.logger.info(f"save_chat: Extracted student email '{student_email}' from Object_3.")
            else:
                app.logger.warning(f"save_chat: Could not extract email from Object_3 record {student_obj3_id}. Raw: '{raw_val_field70}', Obj: '{obj_val_field70}'")
        else:
            app.logger.warning(f"save_chat: Could not fetch Object_3 record for ID {student_obj3_id}.")

    # 2. Get student_object_6_id using student_email (for field_3283)
    if student_email:
        app.logger.info(f"save_chat: Fetching Object_6 record using email '{student_email}' (field_91).")
        filters_obj6 = [{'field': 'field_91', 'operator': 'is', 'value': student_email}]
        obj6_response = get_knack_record("object_6", filters=filters_obj6)
        if obj6_response and isinstance(obj6_response, dict) and obj6_response.get('records'):
            if obj6_response['records']: # Check if the list is not empty
                student_object_6_id = obj6_response['records'][0].get('id')
                app.logger.info(f"save_chat: Found Object_6 ID: {student_object_6_id}.")
            else:
                app.logger.warning(f"save_chat: No Object_6 record found for email '{student_email}'.")
        else:
            app.logger.warning(f"save_chat: Error or unexpected response fetching Object_6 record for email '{student_email}'. Response: {str(obj6_response)[:200]}")
    else:
        app.logger.warning(f"save_chat: No student_email available to fetch Object_6 ID for student_obj3_id {student_obj3_id}.")

    # 3. Get student_object_10_id using student_email (for field_3284)
    if student_email:
        app.logger.info(f"save_chat: Fetching Object_10 record using email '{student_email}' (field_197).")
        filters_obj10 = [{'field': 'field_197', 'operator': 'is', 'value': student_email}]
        obj10_response = get_knack_record("object_10", filters=filters_obj10)
        if obj10_response and isinstance(obj10_response, dict) and obj10_response.get('records'):
            if obj10_response['records']: # Check if the list is not empty
                student_object_10_id = obj10_response['records'][0].get('id')
                app.logger.info(f"save_chat: Found Object_10 ID: {student_object_10_id} for field_3284.")
            else:
                app.logger.warning(f"save_chat: No Object_10 record found for email '{student_email}' for field_3284.")
        else:
            app.logger.warning(f"save_chat: Error or unexpected response fetching Object_10 record for email '{student_email}' for field_3284. Response: {str(obj10_response)[:200]}")
    else:
        app.logger.warning(f"save_chat: No student_email available to fetch Object_10 ID for student_obj3_id {student_obj3_id}.")

    # 4. Construct Payload
    session_id = f"{student_obj3_id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    current_timestamp_knack_format = datetime.now().strftime('%d/%m/%Y %H:%M:%S')
    
    payload = {
        "field_3282": author,
        "field_3286": message_text[:10000], # Max length for paragraph text
        "field_3285": current_timestamp_knack_format,
        "field_3287": "Yes" if is_liked else "No",
        "field_3288": session_id
    }

    if student_object_6_id:
        payload["field_3283"] = student_object_6_id # Knack connection field expects direct ID string for "to one"
    else:
        app.logger.warning(f"save_chat: student_object_6_id is None. field_3283 will not be set for chat log related to student_obj3_id {student_obj3_id}.")
        # If field_3283 is mandatory in Knack, this omission will cause a 400 Bad Request.
        # Consider returning None early or handling the error if this connection is critical.

    if student_object_10_id:
        payload["field_3284"] = student_object_10_id # Knack connection field
    else:
        app.logger.warning(f"save_chat: student_object_10_id is None. field_3284 will not be set for chat log related to student_obj3_id {student_obj3_id}.")
        
    headers = {
        'X-Knack-Application-Id': KNACK_APP_ID,
        'X-Knack-REST-API-Key': KNACK_API_KEY,
        'Content-Type': 'application/json'
    }
    
    url = f"{KNACK_API_BASE_URL}/object_119/records"
    app.logger.info(f"Saving chat message to Knack ({url}): Payload Author='{author}', StudentObj3ID='{student_obj3_id}', SessionID='{session_id}', Obj6ID='{student_object_6_id}', Obj10ID='{student_object_10_id}'")

    try:
        response = requests.post(url, headers=headers, json=payload)
        response.raise_for_status() # Will raise HTTPError for 4xx/5xx responses
        response_data = response.json()
        app.logger.info(f"Chat message saved successfully to Knack (object_119). Record ID: {response_data.get('id')}")
        return response_data.get('id')
    except requests.exceptions.HTTPError as e:
        # Log the full response content if available for better debugging
        response_content = "No response content available"
        if e.response is not None:
            try:
                response_content = e.response.text # or e.response.json() if it's JSON
            except Exception as ex_resp:
                response_content = f"Could not decode response content: {ex_resp}"
        app.logger.error(f"HTTP error saving chat message to object_119: {e}. Status: {e.response.status_code if e.response else 'N/A'}. Response: {response_content}")
    except requests.exceptions.RequestException as e:
        app.logger.error(f"Request exception saving chat message to object_119: {e}")
    except json.JSONDecodeError as e_json: # Catch JSONDecodeError specifically
        # This can happen if response.json() fails because the response is not valid JSON
        response_text_for_log = "Response object not available or text could not be read."
        if 'response' in locals() and response is not None: # Check if response variable exists and is not None
             try:
                response_text_for_log = response.text
             except Exception:
                pass # Keep the default message
        app.logger.error(f"JSON decode error for Knack save chat (object_119) response: {e_json}. Response text: {response_text_for_log}")
    return None

# --- NEW ENDPOINT FOR LIKING/UNLIKING A CHAT MESSAGE ---
@app.route('/api/v1/chat_message_like_toggle', methods=['POST', 'OPTIONS'])
def chat_message_like_toggle():
    app.logger.info(f"Received request for /api/v1/chat_message_like_toggle. Method: {request.method}")
    if request.method == 'OPTIONS':
        return _build_cors_preflight_response()

    if request.method == 'POST':
        data = request.get_json()
        message_knack_id = data.get('message_knack_id')
        like_status = data.get('like_status') # Expected to be boolean true for like, false for unlike

        if not message_knack_id or like_status is None:
            app.logger.error("chat_message_like_toggle: Missing message_knack_id or like_status.")
            return jsonify({"error": "Missing message_knack_id or like_status"}), 400

        if not KNACK_APP_ID or not KNACK_API_KEY:
            app.logger.error("Knack App ID or API Key is missing for chat_message_like_toggle.")
            return jsonify({"error": "Knack API credentials not configured"}), 500

        knack_object_key_chatlog = "object_119" 
        payload = {
            "field_3287": "Yes" if like_status else "No" # Corrected Liked field for object_119
        }
        
        headers = {
            'X-Knack-Application-Id': KNACK_APP_ID,
            'X-Knack-REST-API-Key': KNACK_API_KEY,
            'Content-Type': 'application/json'
        }
        
        url = f"{KNACK_API_BASE_URL}/{knack_object_key_chatlog}/records/{message_knack_id}"
        app.logger.info(f"Updating like status for message {message_knack_id} in {knack_object_key_chatlog} to {payload['field_3287']}. URL: {url}")

        try:
            response = requests.put(url, headers=headers, json=payload)
            response.raise_for_status()
            app.logger.info(f"Successfully updated like status for message {message_knack_id}.")
            return jsonify({"success": True, "message_id": message_knack_id, "liked": like_status}), 200
        except requests.exceptions.HTTPError as e:
            app.logger.error(f"HTTP error updating like status for message {message_knack_id}: {e}. Response: {response.content if response else 'No response object'}")
            return jsonify({"error": f"Failed to update like status: {e}"}), 500
        except requests.exceptions.RequestException as e:
            app.logger.error(f"Request exception updating like status for message {message_knack_id}: {e}")
            return jsonify({"error": f"Failed to update like status: {e}"}), 500
        except json.JSONDecodeError:
            app.logger.error(f"JSON decode error for Knack update like status response. Response text: {response.text if response else 'No response object'}")
            return jsonify({"error": "Failed to parse Knack response after updating like status"}), 500

if __name__ == '__main__':
    # For local development. Heroku uses Procfile.
    port = int(os.environ.get('PORT', 5002)) # Use a different port than tutor coach if running locally
    app.run(debug=True, port=port, host='0.0.0.0') 